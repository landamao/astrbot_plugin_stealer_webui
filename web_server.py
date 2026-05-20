import asyncio
import base64
import json
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from astrbot.api import logger


class WebServer:
    """独立的Web服务器，直接引用 stealer 插件实例操作数据"""

    ALLOWED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

    def __init__(self, plugin: Any, host: str = "0.0.0.0", port: int = 8765, password: str = "", data_dir: Path = None):
        self.plugin = plugin
        self.host = host
        self.port = port
        self.password = password
        self.data_dir = data_dir or Path(__file__).parent / "pages"
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        # 登录会话存储 {session_id: expire_time}
        self._sessions: dict[str, float] = {}
        # 会话有效期 24 小时
        self._session_ttl = 86400

    # ── 便捷属性：直接访问原插件的服务 ──────────────────────

    @property
    def _db(self):
        return getattr(self.plugin, "db_service", None)

    @property
    def _cache(self):
        return getattr(self.plugin, "cache_service", None)

    @property
    def _cfg(self):
        return getattr(self.plugin, "plugin_config", None)

    @property
    def _base_dir(self) -> Path:
        return self.plugin.base_dir

    def _get_index(self) -> dict[str, Any]:
        """获取表情索引"""
        db = self._db
        if db:
            idx = db.get_index_cache_readonly()
            if idx:
                return idx
        return self._cache.get_index_cache_readonly()

    def _get_category_keys(self) -> list[str]:
        """获取分类列表"""
        cfg = self._cfg
        if cfg:
            raw = list(getattr(cfg, "categories", []) or [])
        else:
            raw = list(getattr(self.plugin, "categories", []) or [])
        seen: set[str] = set()
        keys: list[str] = []
        for item in raw:
            key = str(item or "").strip()
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
        return keys

    def _file_base64(self, file_path: str) -> str:
        """读取文件转 base64"""
        with open(file_path, "rb") as f:
            raw = f.read()
        ext = Path(file_path).suffix.lower()
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        }
        mime = mime_map.get(ext, "image/png")
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    @staticmethod
    def _norm_scope(scope_mode: object) -> str:
        raw = str(scope_mode or "").strip().lower()
        if raw in {"public", "global", "all"}:
            return "public"
        if raw in {"local", "private", "scoped"}:
            return "local"
        return "public"

    @staticmethod
    def _split_csv(tags_raw: str) -> list[str]:
        return [t.strip() for t in str(tags_raw).split(",") if t.strip()]

    @staticmethod
    def _split_scenes(scene_raw: Any) -> list[str]:
        if scene_raw is None:
            return []
        if isinstance(scene_raw, list):
            raw_items = scene_raw
        else:
            raw_items = str(scene_raw).replace("、", ",").replace("，", ",").replace("；", ",").split(",")
        seen: set[str] = set()
        result: list[str] = []
        for item in raw_items:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result

    def _build_image_item(self, path_str: str, meta: dict) -> dict | None:
        try:
            Path(path_str)
            return {
                "hash": meta.get("hash", ""),
                "category": meta.get("category", "unknown"),
                "tags": meta.get("tags", []),
                "desc": meta.get("desc", ""),
                "scenes": self._split_scenes(meta.get("scenes", [])),
                "scope_mode": self._norm_scope(meta.get("scope_mode")),
                "origin_target": str(meta.get("origin_target", "") or ""),
                "created_at": meta.get("created_at", 0),
            }
        except ValueError:
            return None

    def _build_categories_list(self, counts: dict[str, int]) -> list[dict]:
        result: list[dict] = []
        if hasattr(self.plugin, "plugin_config"):
            for cat in self._cfg.get_category_info():
                key = cat["key"]
                result.append({"key": key, "name": cat["name"], "count": counts.get(key, 0)})
            known = {c["key"] for c in result}
            for cat_key, count in counts.items():
                if cat_key not in known:
                    result.append({"key": cat_key, "name": cat_key, "count": count})
        result.sort(key=lambda x: x["count"], reverse=True)
        return result

    # ── 路由注册 ──────────────────────────────────────────

    def _setup_routes(self, app: web.Application):
        """注册所有路由"""
        import hashlib
        import secrets
        
        def _check_session(request: web.Request) -> bool:
            """检查是否已登录"""
            if not self.password:
                return True
            session_id = request.cookies.get("session_id", "")
            if not session_id:
                return False
            expire = self._sessions.get(session_id, 0)
            if time.time() > expire:
                self._sessions.pop(session_id, None)
                return False
            return True
        
        # Token 鉴权中间件
        @web.middleware
        async def auth_middleware(request: web.Request, handler):
            # 登录相关路由跳过鉴权
            if request.path in ("/", "/api/login", "/api/check-auth"):
                return await handler(request)
            
            # 检查登录状态
            if not _check_session(request):
                if request.path.startswith("/api/"):
                    return web.json_response({"success": False, "error": "未登录"}, status=401)
                else:
                    raise web.HTTPFound("/")
            
            return await handler(request)

        app = web.Application(middlewares=[auth_middleware])
        self._app = app
        # 静态文件
        app.router.add_get("/", self._handle_index)
        app.router.add_post("/api/login", self._handle_login)
        app.router.add_get("/api/logout", self._handle_logout)
        app.router.add_get("/api/check-auth", self._handle_check_auth)
        app.router.add_static("/assets/", path=self.data_dir, name="static")
        
        # API 路由
        app.router.add_get("/api/health", self._handle_health)
        app.router.add_get("/api/stats", self._handle_stats)
        app.router.add_get("/api/images", self._handle_list_images)
        app.router.add_get("/api/image-data", self._handle_image_data)
        app.router.add_get("/api/serve-image", self._handle_serve_image)
        app.router.add_get("/api/categories", self._handle_categories)
        app.router.add_get("/api/emotions", self._handle_emotions)
        
        app.router.add_post("/api/images/upload", self._handle_upload)
        app.router.add_post("/api/images/update", self._handle_update)
        app.router.add_post("/api/images/delete", self._handle_delete)
        app.router.add_post("/api/images/batch-delete", self._handle_batch_delete)
        app.router.add_post("/api/images/batch-move", self._handle_batch_move)
        app.router.add_post("/api/images/batch-scope", self._handle_batch_scope)
        app.router.add_post("/api/images/batch-upload", self._handle_batch_upload)
        app.router.add_get("/api/images/batch-upload-status", self._handle_batch_upload_status)
        app.router.add_post("/api/categories/update", self._handle_categories_update)
        app.router.add_post("/api/categories/delete", self._handle_delete_category)
        app.router.add_post("/api/analyze", self._handle_analyze)

    # ── 生命周期 ──────────────────────────────────────────

    async def start(self):
        """启动服务器"""
        self._setup_routes(None)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"[StealerWebUI] HTTP服务器监听: {self.host}:{self.port}")
        if self.password:
            logger.info("[StealerWebUI] 密码鉴权已启用")
        else:
            logger.warning("[StealerWebUI] 密码鉴权未启用，建议设置 password")

    async def stop(self):
        """停止服务器"""
        if self._runner:
            await self._runner.cleanup()

    # ── 页面路由 ──────────────────────────────────────────

    async def _handle_index(self, request: web.Request) -> web.Response:
        """服务主页面"""
        index_path = self.data_dir / "index.html"
        if not index_path.exists():
            return web.Response(text="页面文件不存在", status=404)
        
        content = index_path.read_text(encoding="utf-8")
        return web.Response(text=content, content_type="text/html")
    
    async def _handle_login(self, request: web.Request) -> web.Response:
        """处理登录请求"""
        import secrets
        try:
            data = await request.json()
            password = data.get("password", "")
            
            if password != self.password:
                return web.json_response({"success": False, "error": "密码错误"})
            
            # 创建会话
            session_id = secrets.token_hex(32)
            self._sessions[session_id] = time.time() + self._session_ttl
            
            resp = web.json_response({"success": True})
            resp.set_cookie("session_id", session_id, max_age=self._session_ttl, httponly=True)
            return resp
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})
    
    async def _handle_logout(self, request: web.Request) -> web.Response:
        """处理登出请求"""
        session_id = request.cookies.get("session_id", "")
        self._sessions.pop(session_id, None)
        resp = web.json_response({"success": True})
        resp.del_cookie("session_id")
        return resp
    
    async def _handle_check_auth(self, request: web.Request) -> web.Response:
        """检查登录状态"""
        if not self.password:
            return web.json_response({"success": True, "auth_required": False})
        return web.json_response({"success": True, "auth_required": True})

    # ── API 路由处理 ──────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"success": True, "status": "ok", "service": "stealer-webui"})

    async def _handle_stats(self, request: web.Request) -> web.Response:
        try:
            index = self._get_index()
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            today_count = sum(1 for m in index.values() if isinstance(m, dict) and m.get("created_at", 0) >= today_start)
            cat_count = len(self._get_category_keys())
            return web.json_response({
                "success": True,
                "stats": {"total": len(index), "categories": cat_count, "today": today_count}
            })
        except Exception as e:
            logger.error(f"[StealerWebUI] 获取统计失败: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_list_images(self, request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", 1))
            page_size = int(request.query.get("size", 50))
            cat_filter = request.query.get("category", None)
            search = request.query.get("q", "").lower()
            sort_order = request.query.get("sort", "newest")

            db = self._db
            get_paginated = getattr(db, "get_emojis_paginated", None) if db else None

            if db and callable(get_paginated) and db.count_total() > 0:
                raw, total, cat_counts = get_paginated(
                    page=page, page_size=page_size, category=cat_filter,
                    sort_order=sort_order, search_query=search if search else None,
                )
                images = [item for item in (self._build_image_item(i["path"], i) for i in raw) if item]
                cats = self._build_categories_list(cat_counts)
                return web.json_response({
                    "success": True, "total": total, "page": page,
                    "size": page_size, "images": images, "categories": cats,
                })

            index = self._get_index()
            images: list[dict] = []
            cat_counts: dict[str, int] = {}

            for path_str, meta in index.items():
                if not Path(path_str).exists():
                    continue
                item = self._build_image_item(path_str, meta)
                if not item:
                    continue
                if search and not (
                    any(search in str(t).lower() for t in item["tags"])
                    or search in item["desc"].lower()
                    or any(search in str(s).lower() for s in item.get("scenes", []))
                ):
                    continue
                cat = item["category"]
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
                if cat_filter and item["category"] != cat_filter:
                    continue
                images.append(item)

            images.sort(
                key=lambda x: (int(x.get("created_at", 0) or 0), str(x.get("hash", ""))),
                reverse=(sort_order != "oldest"),
            )
            total = len(images)
            start = (page - 1) * page_size
            paged = images[start:start + page_size]
            cats = self._build_categories_list(cat_counts)

            return web.json_response({
                "success": True, "total": total, "page": page,
                "size": page_size, "images": paged, "categories": cats,
            })
        except Exception as e:
            logger.error(f"[StealerWebUI] 列表图片失败: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_image_data(self, request: web.Request) -> web.Response:
        try:
            image_hash = request.query.get("hash", "").strip()
            if not image_hash:
                return web.json_response({"success": False, "error": "缺少 hash"})
            for path_str, meta in self._get_index().items():
                if isinstance(meta, dict) and meta.get("hash") == image_hash:
                    if os.path.isfile(path_str):
                        data_url = self._file_base64(path_str)
                        return web.json_response({"success": True, "hash": image_hash, "url": data_url})
                    break
            return web.json_response({"success": False, "error": "图片未找到"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_serve_image(self, request: web.Request) -> web.Response:
        file_path = request.query.get("path", "")
        if not file_path or not os.path.isfile(file_path):
            return web.Response(status=404)
        try:
            Path(file_path).resolve().relative_to(self._base_dir.resolve())
        except ValueError:
            return web.Response(status=403)
        return web.FileResponse(file_path)

    async def _handle_categories(self, request: web.Request) -> web.Response:
        try:
            cats = {key: 0 for key in self._get_category_keys()}
            for meta in self._get_index().values():
                if isinstance(meta, dict):
                    c = str(meta.get("category", "unknown"))
                    cats[c] = cats.get(c, 0) + 1
            return web.json_response({"success": True, "categories": cats})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_emotions(self, request: web.Request) -> web.Response:
        try:
            info = self._cfg.get_category_info()
            return web.json_response({"success": True, "emotions": info})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_upload(self, request: web.Request) -> web.Response:
        try:
            reader = await request.multipart()
            field = await reader.next()
            if not field or field.name != "file":
                return web.json_response({"success": False, "error": "没有上传文件"})
            
            filename = field.filename or "upload.png"
            ext = Path(filename).suffix.lower()
            if ext not in self.ALLOWED_IMAGE_EXTS:
                return web.json_response({"success": False, "error": f"不支持的文件类型: {ext}"})
            
            content = await field.read()
            if not content:
                return web.json_response({"success": False, "error": "文件为空"})

            # 读取其他表单字段
            category = "unknown"
            async for part in reader:
                if part.name == "category":
                    category = (await part.read()).decode()

            # 持久化图片
            img_hash = self._cache.compute_hash(content)
            ts = int(datetime.now().timestamp())
            filename = f"{ts}_{uuid.uuid4().hex[:8]}{ext}"
            cat_dir = self._cfg.ensure_category_dir(category)
            file_path = cat_dir / filename
            await asyncio.to_thread(file_path.write_bytes, content)

            data = {
                "hash": img_hash, "path": str(file_path), "category": category,
                "tags": [], "desc": "", "scenes": [], "created_at": ts,
            }
            await self._cache.update_index(lambda cur: cur.__setitem__(str(file_path), data))
            
            # 同步到数据库
            db = self._db
            if db and hasattr(db, "sync_index"):
                idx = self._cache.get_index_cache_readonly()
                await db.sync_index(idx)

            return web.json_response({"success": True, "hash": img_hash})
        except Exception as e:
            logger.error(f"[StealerWebUI] 上传失败: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_update(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            img_hash = data.get("hash")
            if not img_hash:
                return web.json_response({"success": False, "error": "缺少 hash"})

            new_cat = data.get("category")
            new_tags = data.get("tags")
            new_desc = data.get("desc")
            new_scenes = data.get("scenes", data.get("scene"))
            new_scope = self._norm_scope(data.get("scope_mode"))

            index = dict(self._get_index())
            target = None
            meta = None
            for p, m in index.items():
                if isinstance(m, dict) and m.get("hash") == img_hash:
                    target, meta = p, m
                    break
            if not target or not meta:
                return web.json_response({"success": False, "error": "图片未找到"})

            if new_tags is not None:
                meta["tags"] = self._split_csv(new_tags) if isinstance(new_tags, str) else new_tags
            if new_desc is not None:
                meta["desc"] = new_desc
            if new_scenes is not None:
                meta["scenes"] = self._split_scenes(new_scenes)
            if new_scope:
                meta["scope_mode"] = new_scope
            if new_cat and new_cat != meta.get("category"):
                old_path = Path(target)
                if old_path.exists():
                    target_dir = self._cfg.ensure_category_dir(new_cat)
                    new_path = target_dir / old_path.name
                    await asyncio.to_thread(shutil.move, str(old_path), str(new_path))
                    del index[target]
                    meta["path"] = str(new_path)
                    meta["category"] = new_cat
                    index[str(new_path)] = meta
            else:
                index[target] = meta

            await self._cache.set_cache("index_cache", index, persist=False)
            db = self._db
            if db and hasattr(db, "sync_index"):
                await db.sync_index(index)

            return web.json_response({"success": True})
        except Exception as e:
            logger.error(f"[StealerWebUI] 更新失败: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_delete(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            img_hash = (data.get("hash", "") or "").strip()
            if not img_hash:
                return web.json_response({"success": False, "error": "缺少 hash"})
            blacklist = data.get("blacklist", False)

            index = dict(self._get_index())
            removed_path = None
            for p, m in list(index.items()):
                if isinstance(m, dict) and m.get("hash") == img_hash:
                    removed_path = p
                    del index[p]
                    break

            if not removed_path:
                return web.json_response({"success": False, "error": "图片未找到"})

            await self._cache.set_cache("index_cache", index, persist=False)
            db = self._db
            if db and hasattr(db, "sync_index"):
                await db.sync_index(index)

            try:
                os.unlink(removed_path)
            except Exception as e:
                logger.warning(f"[StealerWebUI] 删除文件失败: {e}")

            if blacklist:
                await self._cache.set("blacklist_cache", img_hash, int(time.time()), persist=True)

            if hasattr(self.plugin, "image_processor_service"):
                self.plugin.image_processor_service.invalidate_cache(img_hash)

            return web.json_response({"success": True})
        except Exception as e:
            logger.error(f"[StealerWebUI] 删除失败: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_batch_delete(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            hashes = set(data.get("hashes", []))
            if not hashes:
                return web.json_response({"success": True, "count": 0})

            index = dict(self._get_index())
            removed_paths: list[str] = []
            for p, m in list(index.items()):
                if isinstance(m, dict) and m.get("hash") in hashes:
                    removed_paths.append(p)
                    del index[p]

            await self._cache.set_cache("index_cache", index, persist=False)
            db = self._db
            if db and hasattr(db, "sync_index"):
                await db.sync_index(index)

            deleted = 0
            for p in removed_paths:
                try:
                    os.unlink(p)
                    deleted += 1
                except Exception as e:
                    logger.warning(f"[StealerWebUI] 删除文件失败 {p}: {e}")

            return web.json_response({"success": True, "count": deleted})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_batch_move(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            hashes = set(data.get("hashes", []))
            target_cat = data.get("category")
            if not hashes or not target_cat:
                return web.json_response({"success": False, "error": "缺少参数"})

            index = dict(self._get_index())
            moved = 0
            target_dir = self._cfg.ensure_category_dir(target_cat)

            for p, m in list(index.items()):
                if not isinstance(m, dict) or m.get("hash") not in hashes:
                    continue
                if m.get("category") == target_cat:
                    continue
                old = Path(p)
                if not old.exists():
                    continue
                new = target_dir / old.name
                await asyncio.to_thread(shutil.move, str(old), str(new))
                del index[p]
                m["path"] = str(new)
                m["category"] = target_cat
                index[str(new)] = m
                moved += 1

            await self._cache.set_cache("index_cache", index, persist=False)
            db = self._db
            if db and hasattr(db, "sync_index"):
                await db.sync_index(index)

            return web.json_response({"success": True, "count": moved})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_batch_scope(self, request: web.Request) -> web.Response:
        """批量修改作用域"""
        try:
            data = await request.json()
            hashes = set(data.get("hashes", []))
            scope = self._norm_scope(data.get("scope_mode"))
            if not hashes or not scope:
                return web.json_response({"success": False, "error": "缺少参数"})

            index = dict(self._get_index())
            updated = 0
            skipped = 0

            for _, m in index.items():
                if not isinstance(m, dict) or m.get("hash") not in hashes:
                    continue
                if scope == "local" and not str(m.get("origin_target", "")).strip():
                    skipped += 1
                    continue
                m["scope_mode"] = scope
                updated += 1

            await self._cache.set_cache("index_cache", index, persist=False)
            db = self._db
            if db and hasattr(db, "sync_index"):
                await db.sync_index(index)

            return web.json_response({"success": True, "count": updated, "skipped": skipped})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_batch_upload(self, request: web.Request) -> web.Response:
        """批量上传"""
        try:
            reader = await request.multipart()
            files_data = []
            category = ""
            auto_analyze = False

            async for field in reader:
                if field.name == "category":
                    category = (await field.read()).decode()
                elif field.name == "auto_analyze":
                    auto_analyze = (await field.read()).decode().lower() == "true"
                elif field.name == "files" or field.name == "file":
                    filename = field.filename or "upload.png"
                    ext = Path(filename).suffix.lower()
                    if ext not in self.ALLOWED_IMAGE_EXTS:
                        continue
                    content = await field.read()
                    if content:
                        files_data.append({
                            "filename": filename,
                            "content": content,
                            "hash": self._cache.compute_hash(content),
                            "ext": ext,
                        })

            if not files_data:
                return web.json_response({"success": False, "error": "没有上传有效的图片文件"})

            fallback = category or (self._get_category_keys()[0] if self._get_category_keys() else None)
            if not fallback:
                return web.json_response({"success": False, "error": "未配置任何分类"})

            task_id = str(uuid.uuid4())
            if not hasattr(self, '_batch_tasks'):
                self._batch_tasks = {}
            self._batch_tasks[task_id] = {
                "status": "processing",
                "total": len(files_data),
                "processed": 0,
                "success": 0,
                "failed": 0,
                "results": [],
            }
            asyncio.create_task(self._process_batch(task_id, files_data, category, auto_analyze, fallback))
            return web.json_response({"success": True, "task_id": task_id, "total": len(files_data)})
        except Exception as e:
            logger.error(f"[StealerWebUI] 批量上传失败: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)})

    async def _process_batch(self, task_id: str, files_data: list, category: str, auto_analyze: bool, fallback: str):
        """处理批量上传任务"""
        try:
            task = self._batch_tasks.get(task_id)
            if not task:
                return
            for fd in files_data:
                try:
                    tags, desc, scenes = [], "", []
                    final_cat = category or fallback
                    if auto_analyze:
                        try:
                            proc = getattr(self.plugin, "image_processor_service", None)
                            if proc:
                                import tempfile
                                tmp = tempfile.NamedTemporaryFile(suffix=fd['ext'], delete=False)
                                tmp.write(fd["content"])
                                tmp.close()
                                rc, rt, rd, _, rs = await proc.classify_image(
                                    event=None, file_path=tmp.name,
                                    categories=list(self._cfg.categories or []),
                                    content_filtration=False,
                                )
                                try:
                                    os.unlink(tmp.name)
                                except Exception:
                                    pass
                                if rc:
                                    final_cat = rc
                                    tags = rt or []
                                    desc = rd or ""
                                    scenes = rs or []
                        except Exception as e:
                            logger.warning(f"[StealerWebUI] 自动分析失败: {e}")

                    ts = int(datetime.now().timestamp())
                    filename = f"{ts}_{uuid.uuid4().hex[:8]}{fd['ext']}"
                    cat_dir = self._cfg.ensure_category_dir(final_cat)
                    file_path = cat_dir / filename
                    await asyncio.to_thread(file_path.write_bytes, fd["content"])

                    data = {
                        "hash": fd["hash"], "path": str(file_path), "category": final_cat,
                        "tags": tags, "desc": desc, "scenes": scenes, "created_at": ts,
                    }
                    await self._cache.update_index(lambda cur: cur.__setitem__(str(file_path), data))

                    task["results"].append({"hash": fd["hash"], "category": final_cat, "success": True})
                    task["success"] += 1
                except Exception as e:
                    logger.error(f"[StealerWebUI] 处理文件 {fd['filename']} 失败: {e}")
                    task["results"].append({"filename": fd["filename"], "success": False, "error": str(e)})
                    task["failed"] += 1
                task["processed"] += 1

            db = self._db
            if db and hasattr(db, "sync_index"):
                idx = self._cache.get_index_cache_readonly()
                await db.sync_index(idx)
            task["status"] = "completed"
        except Exception as e:
            logger.error(f"[StealerWebUI] 批量上传任务 {task_id} 失败: {e}")
            if task_id in self._batch_tasks:
                self._batch_tasks[task_id]["status"] = "failed"
                self._batch_tasks[task_id]["error"] = str(e)

    async def _handle_batch_upload_status(self, request: web.Request) -> web.Response:
        """查询批量上传任务状态"""
        task_id = request.query.get("task_id", "").strip()
        if not task_id:
            return web.json_response({"success": False, "error": "无效的任务ID"})
        if not hasattr(self, '_batch_tasks'):
            self._batch_tasks = {}
        task = self._batch_tasks.get(task_id)
        if not task:
            return web.json_response({"success": False, "error": "任务不存在或已过期"})
        return web.json_response({
            "success": True, "task_id": task_id, "status": task["status"],
            "total": task["total"], "processed": task["processed"],
            "success_count": task["success"], "failed_count": task["failed"],
            "error": task.get("error", ""), "results": task.get("results", []),
        })

    async def _handle_categories_update(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            items = data.get("categories", [])
            if not isinstance(items, list) or not items:
                return web.json_response({"success": False, "error": "分类列表无效"})

            keys: list[str] = []
            info: dict[str, dict] = {}
            seen: set[str] = set()
            for item in items:
                if isinstance(item, dict) and item.get("key"):
                    key = str(item["key"]).strip()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    keys.append(key)
                    name = str(item.get("name", "")).strip()
                    desc = str(item.get("desc", "")).strip()
                    if name or desc:
                        info[key] = {"name": name, "desc": desc}
                elif isinstance(item, str):
                    key = item.strip()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    keys.append(key)

            if not keys:
                return web.json_response({"success": False, "error": "分类列表无效"})

            if hasattr(self.plugin, "_update_config_from_dict"):
                self.plugin._update_config_from_dict({"categories": keys})
            else:
                self._cfg.categories = keys
                if hasattr(self.plugin, "categories"):
                    self.plugin.categories = keys

            cur_info = dict(getattr(self._cfg, "category_info", {}) or {})
            self._cfg.category_info = {k: cur_info.get(k, {}) for k in keys}
            self._cfg.category_info.update(info)
            self._cfg.ensure_category_dirs(keys)
            self._cfg.save_category_info()

            return web.json_response({"success": True, "categories": keys})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_delete_category(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            key = str(data.get("key", "")).strip()
            if not key:
                return web.json_response({"success": False, "error": "分类Key无效"})

            cur_cats = list(self._cfg.categories or [])
            if key not in cur_cats:
                return web.json_response({"success": False, "error": "分类不存在"})
            if len(cur_cats) <= 1:
                return web.json_response({"success": False, "error": "至少需要保留1个分类"})

            updated = [c for c in cur_cats if c != key]
            index = dict(self._get_index())
            deleted = 0

            for p, m in list(index.items()):
                if isinstance(m, dict) and m.get("category") == key:
                    try:
                        os.unlink(p)
                        deleted += 1
                    except Exception as ex:
                        logger.warning(f"[StealerWebUI] 删除分类文件失败: {p}, {ex}")
                    del index[p]

            await self._cache.set_cache("index_cache", index, persist=False)
            db = self._db
            if db and hasattr(db, "sync_index"):
                await db.sync_index(index)

            cat_dir = self._base_dir / "categories" / key
            try:
                if cat_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, cat_dir, True)
            except Exception as e:
                logger.warning(f"[StealerWebUI] 删除分类目录失败: {cat_dir}, {e}")

            if key in getattr(self._cfg, "category_info", {}):
                del self._cfg.category_info[key]
                self._cfg.save_category_info()

            if hasattr(self.plugin, "_update_config_from_dict"):
                self.plugin._update_config_from_dict({"categories": updated})
            else:
                self._cfg.categories = updated
                if hasattr(self.plugin, "categories"):
                    self.plugin.categories = updated

            return web.json_response({"success": True, "deleted": key, "categories": updated, "deleted_files": deleted})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def _handle_analyze(self, request: web.Request) -> web.Response:
        try:
            proc = getattr(self.plugin, "image_processor_service", None)
            if not proc:
                return web.json_response({"success": False, "error": "图片处理服务不可用"})

            data = await request.json()
            img_hash = (data.get("hash", "") or "").strip()
            img_base64 = (data.get("base64", "") or "").strip()

            file_path = None
            tmp_file = None

            if img_hash:
                for p, m in self._get_index().items():
                    if isinstance(m, dict) and m.get("hash") == img_hash:
                        if os.path.isfile(p):
                            file_path = p
                        break

            if not file_path and img_base64:
                import tempfile
                b64_data = img_base64
                if "," in b64_data:
                    b64_data = b64_data.split(",", 1)[1]
                file_content = base64.b64decode(b64_data)
                ext = ".png"
                if "jpeg" in img_base64 or "jpg" in img_base64:
                    ext = ".jpg"
                elif "gif" in img_base64:
                    ext = ".gif"
                elif "webp" in img_base64:
                    ext = ".webp"
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                tmp.write(file_content)
                tmp.close()
                file_path = tmp.name
                tmp_file = file_path

            if not file_path:
                return web.json_response({"success": False, "error": "缺少图片数据"})

            try:
                cat, tags, desc, _, scenes = await proc.classify_image(
                    event=None, file_path=file_path,
                    categories=list(self._cfg.categories or []),
                    content_filtration=False,
                )
                if not cat:
                    return web.json_response({"success": False, "error": "无法识别图片分类"})
                return web.json_response({
                    "success": True, "category": cat, "tags": tags,
                    "description": desc, "scenes": scenes or [],
                })
            finally:
                if tmp_file:
                    try:
                        os.unlink(tmp_file)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[StealerWebUI] VLM分析失败: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)})
