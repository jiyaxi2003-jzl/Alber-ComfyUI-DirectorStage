"""最小 CDP 客户端：不依赖 selenium/playwright，直接用 websocket 驱动 Chrome。

只做几件事：新开标签页、导航、跑 JS、收控制台报错、截图。
"""
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEBUG_PORT = 9333


class WS:
    """够用就好的 websocket 客户端（不处理分片，CDP 消息都在 2^14 以内或走流式拼接）。"""

    def __init__(self, url: str):
        assert url.startswith("ws://")
        rest = url[5:]
        host_port, path = rest.split("/", 1)
        path = "/" + path
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        head, _, self.rest = buf.partition(b"\r\n\r\n")
        assert b"101" in head.split(b"\r\n")[0], head[:200]
        expect = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        assert expect.encode() in head

    def _read(self, n: int) -> bytes:
        while len(self.rest) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed")
            self.rest += chunk
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def send(self, obj: dict):
        data = json.dumps(obj).encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> dict:
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 0x1:
                return json.loads(payload.decode())
            if opcode == 0x8:
                raise ConnectionError("ws close frame")
            # 其它（ping/pong/binary）忽略

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class Page:
    def __init__(self, port=DEBUG_PORT):
        self.port = port
        self.seq = 0
        self.errors = []
        self.console = []

    def new_target(self, url="about:blank"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/new?{urllib.parse.quote(url, safe=':/?=&')}",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.load(r)
        self.ws = WS(info["webSocketDebuggerUrl"])
        return info

    def call(self, method, **params):
        self.seq += 1
        mid = self.seq
        self.ws.send({"id": mid, "method": method, "params": params})
        while True:
            msg = self.ws.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self._event(msg)

    def _event(self, msg):
        m = msg.get("method")
        if m == "Runtime.exceptionThrown":
            d = msg["params"]["exceptionDetails"]
            text = d.get("exception", {}).get("description") or d.get("text")
            self.errors.append(str(text).split("\n")[0])
        elif m == "Runtime.consoleAPICalled":
            if msg["params"]["type"] in ("error", "warning"):
                parts = []
                for a in msg["params"].get("args", []):
                    parts.append(str(a.get("value", a.get("description", ""))))
                self.console.append(f"[{msg['params']['type']}] " + " ".join(parts)[:300])

    def pump(self, seconds):
        """空转若干秒，顺便收事件。"""
        end = time.time() + seconds
        self.ws.sock.settimeout(0.3)
        while time.time() < end:
            try:
                self._event(self.ws.recv())
            except (socket.timeout, TimeoutError):
                continue
            except Exception:
                break
        self.ws.sock.settimeout(30)

    def eval(self, expr, await_promise=False, timeout=30):
        r = self.call(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
        )
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError(d.get("exception", {}).get("description", d.get("text")))
        return r.get("result", {}).get("value")

    def shot(self, path, full=False):
        r = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=full)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        return path


def launch(port=DEBUG_PORT, headless=True):
    prof = os.path.join(tempfile.gettempdir(), f"cdp-prof-{port}")
    # 每次用干净 profile，避免浏览器缓存住旧的前端代码
    shutil.rmtree(prof, ignore_errors=True)
    args = [
        CHROME,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={prof}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu-sandbox",
        "--window-size=1680,1050",
        "--enable-unsafe-swiftshader",
        "--use-gl=angle",
        "--use-angle=swiftshader",
    ]
    if headless:
        args.append("--headless=new")
    args.append("about:blank")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("chrome 没起来")


if __name__ == "__main__":
    print(__doc__)
