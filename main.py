import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star

from .web_server import WebServer


class Main(Star):
    """表情包管理独立WebUI插件。
    
    通过引用 astrbot_plugin_stealer 插件实例，提供独立的Web管理界面。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        
        # 配置项
        self.port = int(self.config.get("port", 8765))
        self.host = str(self.config.get("host", "0.0.0.0"))
        self.password = str(self.config.get("password", "")).strip()
        
        # 目标插件实例（延迟获取）
        self._stealer_plugin: Optional[Any] = None
        self._web_server: Optional[WebServer] = None
        self._server_task: Optional[asyncio.Task] = None
        
        logger.info(f"[StealerWebUI] 插件初始化完成，端口: {self.port}")

    def _find_stealer_plugin(self) -> Optional[Any]:
        """动态获取 astrbot_plugin_stealer 插件实例"""
        try:
            all_stars = self.context.get_all_stars()
            for star_meta in all_stars:
                if hasattr(star_meta, 'star_cls') and star_meta.name == "astrbot_plugin_stealer":
                    logger.info(f"[StealerWebUI] 找到目标插件: {star_meta.name}")
                    return star_meta.star_cls
        except Exception as e:
            logger.error(f"[StealerWebUI] 获取插件失败: {e}", exc_info=True)
        return None

    async def initialize(self):
        """异步初始化，启动独立Web服务器"""
        self._stealer_plugin = self._find_stealer_plugin()
        if not self._stealer_plugin:
            logger.error("[StealerWebUI] 未找到 astrbot_plugin_stealer 插件！请确保该插件已加载")
            return
        
        logger.info("[StealerWebUI] 成功获取 stealer 插件实例")
        
        # 启动独立Web服务器
        self._web_server = WebServer(
            plugin=self._stealer_plugin,
            host=self.host,
            port=self.port,
            password=self.password,
            data_dir=Path(__file__).parent / "pages"
        )
        self._server_task = asyncio.create_task(self._web_server.start())
        logger.info(f"[StealerWebUI] 独立Web服务器已启动: http://{self.host}:{self.port}")

    async def terminate(self):
        """插件禁用/重载时清理资源"""
        if self._web_server:
            await self._web_server.stop()
            logger.info("[StealerWebUI] Web服务器已停止")
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
