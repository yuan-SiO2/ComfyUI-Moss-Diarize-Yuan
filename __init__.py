"""ComfyUI-YuanCut —— 把 YuanCut 视频编辑器搬进 ComfyUI。

实现方式沿用 ComfyUI-OpenCut：
  * 纯静态的 YuanCut 前端放在插件目录 `yuancut-ui/`，由 ComfyUI 自己的 aiohttp
    服务在 `/yuancut` 下提供，不需要额外进程、不上传素材、不开数据库。
  * 一个 OUTPUT 节点 + 顶栏按钮，用 iframe 弹层打开编辑器，关闭后实例保留，
    再次打开是秒开，项目（IndexedDB）与编辑状态都不会丢。
"""

import logging
import re
from pathlib import Path

from aiohttp import web

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

# ComfyUI >= 0.3 会把这个目录挂到 /extensions/ComfyUI-YuanCut/ 下
WEB_DIRECTORY = "./js"

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
UI_DIR = PLUGIN_DIR / "yuancut-ui"

LOCALES = (
    "en",
    "zh",
    "ja",
    "ko",
    "es",
    "pt",
    "fr",
    "de",
    "id",
    "vi",
    "ru",
    "it",
)
DEFAULT_LOCALE = "en"

_MIME_OVERRIDES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".ico": "image/x-icon",
}

_MISSING_UI_HTML = """<!doctype html>
<meta charset="utf-8">
<title>YuanCut UI 未构建</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: hsl(0 0% 7%);
         color: hsl(0 0% 92%); font: 14px/1.7 Inter, -apple-system, "Segoe UI", sans-serif; }
  .box { max-width: 640px; padding: 28px 32px; border: 1px solid hsl(0 0% 16%); border-radius: 14px;
         background: hsl(0 0% 9%); }
  h1 { margin: 0 0 12px; font-size: 18px; }
  code { padding: 2px 6px; border-radius: 6px; background: hsl(0 0% 16%); color: #009dff; }
  a { color: #009dff; }
</style>
<div class="box">
  <h1>YuanCut UI 还没有构建</h1>
  <p>插件目录里缺少 <code>yuancut-ui/index.html</code> 等静态文件。</p>
  <p>在插件根目录执行 <code>tools/build_ui.sh /path/to/webapp-source</code>，
     或把已有的 <code>out/</code> 导出目录内容复制到 <code>yuancut-ui/</code>。</p>
</div>
"""


def _headers_for(path: Path) -> dict:
    """HTML / RSC payload 不缓存，hash 化的 _next 静态资源长缓存。"""
    parts = path.parts
    if "_next" in parts:
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    return {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }


_PREFETCH_CACHE: dict[str, tuple[float, bytes]] = {}


def _prefetch_tags(locale: str) -> bytes:
    """把编辑器页面的脚本/CSS 预取到浏览器缓存，进入项目时不用现下载。"""
    editor_html = UI_DIR / locale / "editor" / "index.html"
    try:
        mtime = editor_html.stat().st_mtime
    except OSError:
        return b""

    cached = _PREFETCH_CACHE.get(locale)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        html = editor_html.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return b""

    assets = sorted(
        set(re.findall(r'"(/yuancut/_next/static/[^"]+?\.(?:js|css))"', html))
    )
    tags = "".join(
        '<link rel="prefetch" href="{href}" as="{kind}">'.format(
            href=asset, kind="style" if asset.endswith(".css") else "script"
        )
        for asset in assets
    ).encode()
    _PREFETCH_CACHE[locale] = (mtime, tags)
    return tags


def _html_response(path: Path, extra_head: bytes = b"") -> web.StreamResponse:
    try:
        body = path.read_bytes()
    except OSError:
        return web.Response(text="Not found", status=404)

    if extra_head and b"</head>" in body:
        body = body.replace(b"</head>", extra_head + b"</head>", 1)

    headers = _headers_for(path)
    headers["Content-Type"] = "text/html; charset=utf-8"
    return web.Response(body=body, headers=headers)


def _page_response(path: Path) -> web.StreamResponse:
    if path.name == "index.html" and path.parent.name == "projects":
        return _html_response(path, _prefetch_tags(path.parent.parent.name))
    return _file_response(path)


def _file_response(path: Path) -> web.StreamResponse:
    headers = _headers_for(path)
    content_type = _MIME_OVERRIDES.get(path.suffix.lower())
    if content_type:
        headers["Content-Type"] = content_type
    return web.FileResponse(path, headers=headers)


def _editor_shell(locale: str) -> Path | None:
    shell = UI_DIR / locale / "editor" / "index.html"
    return shell if shell.is_file() else None

def _projects_index(locale: str) -> Path | None:
    """首页已移除：插件入口直接落到「所有项目」。"""
    page = UI_DIR / locale / "projects" / "index.html"
    return page if page.is_file() else None

def _serve_relative(rel_path: str) -> web.StreamResponse:
    if not UI_DIR.is_dir():
        return web.Response(
            text=_MISSING_UI_HTML, content_type="text/html", status=503
        )

    rel_path = (rel_path or "").strip("/")
    segments = [segment for segment in rel_path.split("/") if segment not in ("", ".")]
    if any(segment == ".." for segment in segments):
        return web.Response(text="Invalid path", status=400)

    target = UI_DIR.joinpath(*segments) if segments else UI_DIR

    if target.is_dir():
        index = target / "index.html"
        if index.is_file():
            return _page_response(index)
    elif target.is_file():
        return _page_response(target)

    # 无扩展名的客户端路由：编辑器回落编辑器外壳，其余回落到「所有项目」
    if segments and segments[0] in LOCALES and "." not in segments[-1]:
        shell = (
            _editor_shell(segments[0])
            if segments[-1] == "editor"
            else _projects_index(segments[0])
        )
        if shell is not None:
            return _page_response(shell)

    if not segments:
        shell = _projects_index(DEFAULT_LOCALE)
        if shell is not None:
            return _page_response(shell)

    return web.Response(text="Not found", status=404)


try:
    from server import PromptServer

    _routes = PromptServer.instance.routes

    # 音视频合成：浏览器没有 AAC 编码器时，由本机 ffmpeg 完成「音轨编译 + 交织封装」
    from . import yuancut_mux

    yuancut_mux.register_routes(_routes)

    # 服务端渲染：时间线 -> ffmpeg filter_complex
    from . import yuancut_render

    yuancut_render.register_routes(_routes)

    # 字幕转录：统一走本地 MOSS-Transcribe-Diarize 模型（原先浏览器内的 Whisper 模型已下线）

    # 人声美化：神经网络离线增强 + DSP 增强链（独立 venv，不污染 ComfyUI 环境）
    from . import yuancut_voice

    yuancut_voice.register_routes(_routes)
    from . import yuancut_transcribe

    yuancut_transcribe.register_routes(_routes)

    @_routes.get("/yuancut")
    async def yuancut_root(request):  # noqa: ANN001 - aiohttp handler
        return _serve_relative("")

    @_routes.get("/yuancut/{path:.*}")
    async def yuancut_static(request):  # noqa: ANN001 - aiohttp handler
        return _serve_relative(request.match_info.get("path", ""))

    logger.info("[YuanCut] YuanCut editor served at /yuancut")
except Exception as exc:  # pragma: no cover - ComfyUI 之外导入时不应崩溃
    logger.warning("[YuanCut] ComfyUI routes unavailable: %s", exc)
