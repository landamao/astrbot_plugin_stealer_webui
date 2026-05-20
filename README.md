# 表情包管理独立 WebUI

为 `astrbot_plugin_stealer`（表情包小偷）插件提供的独立 Web 管理面板。

## 特点

- **原生数据操作**：通过引用原插件实例，直接调用 Python 方法操作数据，不经过 HTTP API
- **独立端口**：不依赖 AstrBot 内置 WebUI，运行在独立端口上
- **密码登录**：支持密码认证，登录后保持会话，安全便捷
- **功能完整**：支持原 AstrBot 内置 WebUI 的所有功能

## 安装

将 `astrbot_plugin_stealer_webui` 文件夹放入 AstrBot 的插件目录，重启 AstrBot 即可。

## 配置

在 AstrBot WebUI 配置页面中设置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `port` | `8765` | Web 服务器端口 |
| `host` | `0.0.0.0` | 监听地址，`0.0.0.0` 允许所有地址访问 |
| `password` | `""` | 登录密码，为空则不鉴权 |

## 访问方式

启动后访问：`http://你的IP:端口`

如果设置了密码，会显示登录页面，输入密码即可进入。登录状态保持 24 小时。

## 功能列表

### 查看与浏览

| 功能 | 说明 |
|------|------|
| 表情列表 | 分页浏览，支持搜索、排序（最新/最早） |
| 分类筛选 | 侧边栏点击分类过滤表情 |
| 表情详情 | 点击查看完整信息（分类、标签、场景、描述、作用域、来源群号、哈希值、添加时间） |
| 统计数据 | 查看总数、分类数、今日新增 |

### 编辑与管理

| 功能 | 说明 |
|------|------|
| 编辑表情 | 修改分类、作用域、描述、标签、场景 |
| 删除表情 | 单个删除 |
| 拉黑表情 | 删除并加入黑名单，防止再次被收集 |
| 切换作用域 | 公共(public) / 本群限定(local) 切换 |

### 批量操作

| 功能 | 说明 |
|------|------|
| 批量选择 | 进入批量模式，勾选多个表情 |
| 批量删除 | 删除选中的所有表情 |
| 批量移动 | 将选中表情移动到指定分类 |
| 批量设为公共 | 将选中表情设为公共作用域 |
| 批量设为本群 | 将选中表情设为本群限定 |

### 上传与导入

| 功能 | 说明 |
|------|------|
| 单个上传 | 上传单张图片，指定分类 |
| VLM 识别 | 上传时调用视觉模型自动识别分类和标签 |
| 批量导入 | 一次上传多张图片 |
| 批量自动分析 | 批量导入时可选 VLM 自动分析每张图片 |

### 分类管理

| 功能 | 说明 |
|------|------|
| 查看分类 | 侧边栏显示所有分类及数量 |
| 新增分类 | 通过分类管理添加新分类 |
| 删除分类 | 删除分类及其下所有表情 |

## 工作原理

```
┌─────────────────────────────────┐
│  astrbot_plugin_stealer_webui   │
│  (独立 Web 服务器)               │
│                                 │
│  ┌───────────┐  ┌────────────┐  │
│  │ web_server│──│ 原插件实例  │  │
│  │ (aiohttp) │  │ (引用)     │  │
│  └───────────┘  └────────────┘  │
│        │              │         │
│        ▼              ▼         │
│   HTTP API     db_service       │
│   (前端调用)   cache_service    │
│                image_processor  │
│                plugin_config    │
└─────────────────────────────────┘
```

通过 `context.get_all_stars()` 获取 `astrbot_plugin_stealer` 插件实例，直接访问其服务进行数据操作。

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` | 登录（JSON: password） |
| GET | `/api/logout` | 登出 |
| GET | `/api/check-auth` | 检查是否需要鉴权 |
| GET | `/api/health` | 健康检查 |
| GET | `/api/stats` | 获取统计信息 |
| GET | `/api/images` | 获取表情列表（?category=&q=&sort=&page=&size=） |
| GET | `/api/image-data` | 获取表情 base64 数据（?hash=） |
| GET | `/api/serve-image` | 直接返回图片文件（?path=） |
| GET | `/api/categories` | 获取分类列表及数量 |
| GET | `/api/emotions` | 获取情绪分类信息 |
| GET | `/api/images/batch-upload-status` | 查询批量上传进度（?task_id=） |
| POST | `/api/images/upload` | 上传表情（FormData: file, category） |
| POST | `/api/images/update` | 更新表情（JSON: hash, category, scope_mode, desc, tags, scenes） |
| POST | `/api/images/delete` | 删除表情（JSON: hash, blacklist） |
| POST | `/api/images/batch-delete` | 批量删除（JSON: hashes[]） |
| POST | `/api/images/batch-move` | 批量移动（JSON: hashes[], category） |
| POST | `/api/images/batch-scope` | 批量修改作用域（JSON: hashes[], scope_mode） |
| POST | `/api/images/batch-upload` | 批量上传（FormData: files[], category, auto_analyze） |
| POST | `/api/categories/update` | 更新分类列表（JSON: categories[]） |
| POST | `/api/categories/delete` | 删除分类（JSON: key） |
| POST | `/api/analyze` | VLM 分析图片（JSON: hash 或 base64） |

## 依赖

- `aiohttp>=3.9.0`（AstrBot 已内置）
