"""本地演示服务：修正 Windows 注册表导致的 CSS/JS MIME 误判，避免浏览器拒绝加载样式。

    python demo/serve.py            # 默认 http://127.0.0.1:8899/demo/director.html
    python demo/serve.py 9000
"""
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MIME = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
}


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, **MIME}

    def guess_type(self, path):
        ext = os.path.splitext(str(path))[1].lower()
        if ext in MIME:
            return MIME[ext]
        return super().guess_type(path)

    def log_message(self, fmt, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    handler = partial(Handler, directory=ROOT)
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"演示服务已启动： http://127.0.0.1:{port}/demo/director.html")
    print("按 Ctrl+C 停止")
    srv.serve_forever()


if __name__ == "__main__":
    main()
