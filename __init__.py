"""ComfyUI-Alber-DirectorStage —— Alber-导演台：从一段 MP4 重建可编辑的 3D 导演台，并把机位预演打包成 MiniMax H3 的参考输入。

安装：把整个目录放进 ComfyUI/custom_nodes/ 后重启，节点在「Alber-导演台」分类下。
"""
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import dir_nodes  # noqa: E402
from dir_nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS  # noqa: E402

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

_ROUTE_READY = False


def _register_routes():
    """给前端「打开导演台」提供取场景的接口（同源，免 CORS）。"""
    global _ROUTE_READY
    if _ROUTE_READY:
        return
    try:
        from aiohttp import web
        from server import PromptServer

        routes = PromptServer.instance.routes

        @routes.get("/director3d/scene")
        async def director3d_scene(request):
            node_id = str(request.rel_url.query.get("node_id", ""))
            store = dir_nodes.LAST_SCENE
            scene = store.get(node_id) or store.get("_latest")
            if not scene:
                return web.json_response(
                    {"error": "还没有场景。请先运行一次「Alber-导演台 · 视频转3D」，再打开 Alber-3D 导演台。"},
                    status=404,
                )
            return web.json_response({"scene": scene})

        @routes.get("/director3d/status")
        async def director3d_status(request):
            store = dir_nodes.LAST_SCENE
            return web.json_response({
                "scenes": [k for k in store if k != "_latest"],
                "latest": bool(store.get("_latest")),
            })

        _ROUTE_READY = True
    except Exception as exc:  # 不在 ComfyUI 里（单测/独立脚本）时静默跳过
        print(f"[DirectorStage] 路由未注册：{exc}")


_register_routes()
