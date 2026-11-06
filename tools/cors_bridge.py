#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cors_bridge.py — 给本地 ComfyUI 套一层正确的 CORS 反向代理

为什么需要它（而不是加 --enable-cors-header）：
  ComfyUI 自带的 CORS 中间件有四个硬伤，都已在源码里确认（server.py: create_cors_middleware）：
    1. Access-Control-Allow-Origin 写死成常量。配 --enable-cors-header 时不带参数就是 "*"，
       而它同时又发 Access-Control-Allow-Credentials: true —— 这是非法组合，
       浏览器对带凭据的请求会直接拒绝（"must not be the wildcard '*'"）。
    2. Access-Control-Allow-Headers 只有 Content-Type, Authorization。
       前端一旦带自定义头（X-Session-Id、Comfy-Usage-Source …）预检就失败。
    3. 没有 Access-Control-Expose-Headers，前端读不到 Content-Disposition 之类的响应头。
    4. 没有 Access-Control-Allow-Private-Network，Chrome 的 Private Network Access
       预检（本地页面访问 192.168.x.x 时）会被拦。

  更要命的是：不开 flag 时它走 create_origin_only_middleware()，
  只要 Host 与 Origin 的主机名不一致就返回 403 —— 也就是 localhost:5173 的页面
  访问 127.0.0.1:8188 必然 403。所以「不改 ComfyUI 启动参数」这条路本身不通，
  得有一层代理把 Origin 相关头摘掉、再由代理自己补正确的 CORS 响应头。

本脚本做的就是这样一层桥：
  - 按 Origin 白名单精确回显（不是 "*"），因此带凭据也能正常工作
  - 预检本地直接应答：回显允许的方法/头 + Max-Age + Allow-Private-Network
  - 透传时摘掉 Origin / Referer / Sec-Fetch-*，避开上游的 origin_only 校验；
    原 Origin 改名成 X-Bridge-Origin 转发，不丢调试信息
  - /ws 走裸字节隧道（WebSocket 本身不受 CORS 约束，但统一到同一地址更方便）
  - 纯标准库，零依赖

用法：
  python cors_bridge.py                                  # 默认：8188 -> 8199，允许任意来源
  python cors_bridge.py --allow-origin http://localhost:5173,http://localhost:5500
  python cors_bridge.py --upstream http://127.0.0.1:8188 --port 8199 --verbose

前端把 base URL 从 http://127.0.0.1:8188 换成 http://127.0.0.1:8199 即可，
WebSocket 用 ws://127.0.0.1:8199/ws?clientId=xxx。

注意：--allow-origin * 只适合本机开发。一旦这个端口能被局域网访问，
任意网页都能往你的 ComfyUI 里塞任务，所以演示/分享时请改成明确的白名单。
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

# 转发给上游时必须摘掉的头：它们是给上游做「同源校验」用的，留着就会 403。
STRIP_TO_UPSTREAM = {
    "host", "content-length",
    "origin", "referer",
    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user",
}

DEFAULT_ALLOW_HEADERS = "Content-Type, Authorization, Comfy-Usage-Source, X-Session-Id"
DEFAULT_EXPOSE = "Content-Disposition, Content-Length, Content-Range, ETag, X-Request-Id"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(verbose, tag, msg):
    if verbose or tag in ("ERR ", "WARN", "BRIDGE"):
        print(f"[{tag}] {msg}", flush=True)


def parse_upstream(url):
    p = urllib.parse.urlsplit(url if "//" in url else "http://" + url)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"upstream 只支持 http/https：{url}")
    return p.scheme, p.hostname or "127.0.0.1", p.port or (443 if p.scheme == "https" else 80)


def fmt_bytes(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0


class Bridge:
    def __init__(self, upstream, allow_origins, allow_credentials=True,
                 max_request_mb=128, max_response_mb=256, verbose=False,
                 allow_ws=True, allow_headers=None, expose_headers=None):
        self.scheme, self.host, self.port = parse_upstream(upstream)
        self.netloc = f"{self.host}:{self.port}"
        self.allow_origins = allow_origins
        self.allow_credentials = allow_credentials
        self.max_request = int(max_request_mb * 1024 * 1024)
        self.max_response = int(max_response_mb * 1024 * 1024)
        self.verbose = verbose
        self.allow_ws = allow_ws
        self.allow_headers = allow_headers or DEFAULT_ALLOW_HEADERS
        self.expose_headers = expose_headers or DEFAULT_EXPOSE
        self.server = None

    # ---- 策略 -------------------------------------------------------------

    def origin_allowed(self, origin):
        if not origin:
            return False
        if "*" in self.allow_origins:
            return True
        return origin in self.allow_origins

    def cors_headers(self, origin):
        """回显 Origin 而不是 '*'，这样 Allow-Credentials 才合法。"""
        h = {}
        if not self.origin_allowed(origin):
            return h
        h["Access-Control-Allow-Origin"] = origin
        h["Vary"] = "Origin"
        if self.allow_credentials:
            h["Access-Control-Allow-Credentials"] = "true"
        if self.expose_headers != "-":
            h["Access-Control-Expose-Headers"] = self.expose_headers
        return h

    # ---- HTTP ------------------------------------------------------------

    def forward(self, method, path, header_pairs, body):
        """返回 (status, headers, body_bytes)。缓冲式转发，够用且不易出错。"""
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=300)
        incoming = {}
        for k, v in header_pairs:
            incoming[k.lower()] = (k, v)
        hdrs = {}
        for lk, (k, v) in incoming.items():
            if lk in HOP_BY_HOP or lk in STRIP_TO_UPSTREAM or lk == "x-bridge-origin":
                continue
            hdrs[k] = v
        origin_pair = incoming.get("origin")
        if origin_pair:
            # 不丢信息：上游日志里仍然看得到是谁在调。
            hdrs["X-Bridge-Origin"] = origin_pair[1]
        hdrs["Host"] = self.netloc
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read(self.max_response + 1)
            if len(data) > self.max_response:
                return (502, [("Content-Type", "application/json; charset=utf-8")],
                        json.dumps({"error": "response_too_large",
                                    "limit_mb": self.max_response // 1024 // 1024}).encode())
            out = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP_BY_HOP]
            return resp.status, out, data
        except (OSError, http.client.HTTPException) as e:
            # 上游没起来 / 半路断开：给浏览器一个明确的 502，而不是让连接悬着。
            return (502, [("Content-Type", "application/json; charset=utf-8")],
                    json.dumps({"error": "upstream_unreachable", "upstream": self.netloc,
                                "detail": f"{type(e).__name__}: {e}"}, ensure_ascii=False).encode())
        finally:
            conn.close()

    def health(self):
        try:
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            conn = cls(self.host, self.port, timeout=8)
            conn.request("GET", "/system_stats", headers={"Host": self.netloc})
            r = conn.getresponse()
            raw = r.read(400000)
            conn.close()
            if r.status != 200:
                return {"ok": False, "upstream": self.netloc, "status": r.status}
            info = json.loads(raw.decode("utf-8", "replace"))
            dev = (info.get("devices") or [{}])[0]
            return {"ok": True, "upstream": self.netloc,
                    "device": dev.get("name"),
                    "vram_gb": round((dev.get("vram_total") or 0) / 1024 ** 3, 1),
                    "comfyui": (info.get("system") or {}).get("comfyui_version")}
        except Exception as e:
            return {"ok": False, "upstream": self.netloc, "error": f"{type(e).__name__}: {e}"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cors-bridge/0.1"
    bridge: Bridge = None  # 由 make_server 注入

    def log_message(self, fmt, *args):
        pass  # 自己打印，格式更好看

    # ---- 任意方法统一入口 -------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    # ---- 核心 ------------------------------------------------------------

    def _dispatch(self, method):
        b = self.bridge
        origin = self.headers.get("Origin")
        t0 = time.time()

        # 本地健康检查：不碰上游也能看到代理是否活着
        if self.path.split("?")[0] == "/__bridge/health":
            info = b.health()
            info["bridge"] = {"allow_origins": b.allow_origins, "upstream": b.netloc}
            return self._send_json(200, info, origin)

        # WebSocket 升级：字节隧道，不加 CORS 头（WS 不受 CORS 约束）
        if b.allow_ws and "websocket" in (self.headers.get("Upgrade") or "").lower():
            return self._tunnel_ws(origin)

        # 预检：本地直接应答，不必打扰上游
        if method == "OPTIONS" and self.headers.get("Access-Control-Request-Method"):
            return self._preflight(origin)

        # 请求体
        body = self._read_body()
        if body is None:
            return
        if len(body) > b.max_request:
            return self._send_plain(413, "request body too large", origin)

        status, resp_headers, data = b.forward(method, self.path, self.headers.items(), body)

        self.send_response(status)
        for k, v in resp_headers:
            self.send_header(k, v)
        for k, v in b.cors_headers(origin).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)
        log(b.verbose, "OK  ", f"{method} {self.path[:70]} {status} "
                               f"{(time.time() - t0) * 1000:.0f}ms {fmt_bytes(len(data))}"
                               f"{'  <- ' + origin if origin else ''}")

    def _preflight(self, origin):
        b = self.bridge
        if not b.origin_allowed(origin):
            log(b.verbose or True, "WARN", f"预检被拒，来源不在白名单：{origin}")
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        req_headers = self.headers.get("Access-Control-Request-Headers") or b.allow_headers
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", req_headers)
        self.send_header("Access-Control-Max-Age", "600")
        if b.allow_credentials:
            self.send_header("Access-Control-Allow-Credentials", "true")
        # Chrome Private Network Access：本地页面访问 192.168.x.x 时命中
        if self.headers.get("Access-Control-Request-Private-Network"):
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Vary", "Origin, Access-Control-Request-Headers, Access-Control-Request-Method")
        self.send_header("Content-Length", "0")
        self.end_headers()
        log(b.verbose, "PRE ", f"OPTIONS {self.path[:60]} 204  <- {origin}")

    def _read_body(self):
        n = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self._send_plain(411, "chunked request body not supported by this bridge")
            return None
        if not n:
            return b""
        try:
            n = int(n)
        except ValueError:
            self._send_plain(400, "bad Content-Length")
            return None
        return self.rfile.read(n) if n else b""

    def _send_plain(self, code, text, origin=None):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if origin:
            for k, v in self.bridge.cors_headers(origin).items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, code, obj, origin=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if origin:
            for k, v in self.bridge.cors_headers(origin).items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # ---- WebSocket 裸隧道 ------------------------------------------------

    def _tunnel_ws(self, origin):
        b = self.bridge
        try:
            up = socket.create_connection((b.host, b.port), timeout=30)
        except OSError as e:
            log(True, "ERR ", f"WS 上游连不上 {b.netloc}: {e}")
            self._send_plain(502, "upstream unreachable")
            return

        lines = [f"GET {self.path} HTTP/1.1", f"Host: {b.netloc}"]
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "connection", "origin", "referer"):
                continue
            if lk.startswith("sec-fetch"):
                continue
            lines.append(f"{k}: {v}")
        if origin:
            lines.append(f"X-Bridge-Origin: {origin}")
        lines.append("Connection: Upgrade")
        up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        # 把上游的 101 响应原样转给浏览器（含 Sec-WebSocket-Accept）
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = up.recv(4096)
            if not chunk:
                break
            buf += chunk
        self.connection.sendall(buf)
        log(b.verbose, "WS  ", f"{self.path[:60]} 已建立隧道 <- {origin}")

        self.close_connection = True
        stop = threading.Event()

        def up_to_client():
            try:
                while not stop.is_set():
                    data = up.recv(65536)
                    if not data:
                        break
                    self.connection.sendall(data)
            except OSError:
                pass
            finally:
                stop.set()

        def client_to_up():
            try:
                while not stop.is_set():
                    data = self.rfile.read1(65536)
                    if not data:
                        break
                    up.sendall(data)
            except OSError:
                pass
            finally:
                stop.set()

        t = threading.Thread(target=up_to_client, daemon=True)
        t.start()
        client_to_up()
        t.join(timeout=5)
        try:
            up.close()
        except OSError:
            pass
        log(b.verbose, "WS  ", "隧道关闭")


def make_server(bridge, bind, port):
    Handler.bridge = bridge
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    bridge.server = srv
    return srv


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description="本地 ComfyUI 的 CORS 反向代理（替代 --enable-cors-header）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python cors_bridge.py\n"
               "  python cors_bridge.py --allow-origin http://localhost:5173\n"
               "  python cors_bridge.py --upstream http://127.0.0.1:8188 --port 8199 -v",
    )
    ap.add_argument("--upstream", default="http://127.0.0.1:8188", help="ComfyUI 地址")
    ap.add_argument("--port", type=int, default=8199, help="桥接监听端口")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="绑定地址。默认只听本机；要让局域网访问填 0.0.0.0（务必配白名单）")
    ap.add_argument("--allow-origin", default="*",
                    help="允许的来源，逗号分隔，或 * 全放行（仅本机开发用）")
    ap.add_argument("--no-credentials", action="store_true", help="不发 Allow-Credentials")
    ap.add_argument("--max-request-mb", type=float, default=128.0)
    ap.add_argument("--max-response-mb", type=float, default=256.0)
    ap.add_argument("--no-ws", action="store_true", help="不代理 /ws（则前端直连上游 WS）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每条请求")
    args = ap.parse_args(argv)

    origins = [o.strip() for o in args.allow_origin.split(",") if o.strip()]
    bridge = Bridge(args.upstream, origins,
                    allow_credentials=not args.no_credentials,
                    max_request_mb=args.max_request_mb,
                    max_response_mb=args.max_response_mb,
                    verbose=args.verbose, allow_ws=not args.no_ws)

    try:
        srv = make_server(bridge, args.bind, args.port)
    except OSError as e:
        print(f"[ERR ] 端口 {args.port} 绑定失败：{e}")
        print("[TIP ] 换个 --port，或者先关掉占用的程序")
        return 2

    url = f"http://{args.bind}:{args.port}"
    print(f"[BRIDGE] 桥接已启动  {url}  ->  {bridge.scheme}://{bridge.netloc}")
    print(f"[BRIDGE] 允许来源    {', '.join(origins) if origins else '(空)'}")
    print(f"[BRIDGE] 凭据模式    {'开（回显 Origin）' if bridge.allow_credentials else '关'}")
    print(f"[BRIDGE] WebSocket   {'透传' if bridge.allow_ws else '不代理，请直连上游'}")
    print(f"[BRIDGE] 健康检查    {url}/__bridge/health")
    probe = bridge.health()
    if probe.get("ok"):
        print(f"[BRIDGE] 上游正常    {probe.get('device')}  "
              f"显存 {probe.get('vram_gb')}GB  ComfyUI {probe.get('comfyui')}")
    else:
        print(f"[WARN ] 上游还没就绪：{probe.get('error') or probe.get('status')}"
              f" —— 桥接已在跑，等 ComfyUI 起来即可")
    print("[BRIDGE] 前端把 base URL 换成上面那个地址即可；Ctrl+C 停止\n")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[BRIDGE] 已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
