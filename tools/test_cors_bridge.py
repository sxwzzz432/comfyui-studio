#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_cors_bridge.py — 验证 cors_bridge.py 是否真的解决了跨域，以及是否真的绕过了上游的同源校验。

关键设计：假上游**模仿 ComfyUI 的 create_origin_only_middleware()**（server.py:158 附近）：
  - 只要带 Origin 且 Host 与 Origin 的主机名不一致 -> 403
  - 只要 Sec-Fetch-Site: cross-site -> 403
这样如果桥接层没把这两个头摘掉，测试就会挂 —— 而不是「看起来通过了其实没测到」。

跑法：python test_cors_bridge.py
"""

import base64
import hashlib
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cors_bridge as cb

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da63f8cfc0000003010100b5b8f5c60000000049454e44ae426082"
)
GUID = cb.WS_GUID
HITS = {"upstream_403": 0, "seen_origin": [], "prompt_body": None, "ws": 0}


class FakeComfy(BaseHTTPRequestHandler):
    """假 ComfyUI：带 origin_only 中间件 + 必要路由 + WS 回声。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _guard(self):
        """复刻 create_origin_only_middleware 的判定。"""
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs == "cross-site":
            HITS["upstream_403"] += 1
            return False
        host, origin = self.headers.get("Host"), self.headers.get("Origin")
        if host and origin:
            HITS["seen_origin"].append(origin)
            host_name = urllib.parse.urlsplit("//" + host).hostname
            origin_name = urllib.parse.urlsplit(origin).hostname
            if host_name and origin_name and host_name != origin_name:
                HITS["upstream_403"] += 1
                return False
        return True

    def _send(self, code, payload=b"", ctype="application/json"):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if not self._guard():
            return self._send(403, {"error": "host/origin mismatch"})
        path = urllib.parse.urlsplit(self.path).path
        if path == "/system_stats":
            return self._send(200, {"system": {"comfyui_version": "0.3.40"},
                                    "devices": [{"name": "cuda:0 NVIDIA GeForce RTX 4090",
                                                 "vram_total": 24 * 1024 ** 3}]})
        if path == "/object_info":
            return self._send(200, {"CheckpointLoaderSimple": {"input": {"required": {
                "ckpt_name": [["sd_xl_base_1.0.safetensors"]]}}}})
        if path == "/event":
            return self._send(200, {"fake": "image"}, "image/png") if False else self._send(200, PNG_1PX, "image/png")
        if path == "/echo-headers":
            return self._send(200, {"headers": {k: v for k, v in self.headers.items()}})
        if path == "/ws":
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True
            HITS["ws"] += 1
            try:
                while True:
                    data = self.connection.recv(4096)
                    if not data:
                        break
                    self.connection.sendall(b"echo:" + data)
            except OSError:
                pass
            return
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return self._send(403, {"error": "host/origin mismatch"})
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = urllib.parse.urlsplit(self.path).path
        if path == "/prompt":
            HITS["prompt_body"] = body
            return self._send(200, {"prompt_id": "pid-1", "number": 1, "node_errors": {}})
        if path == "/echo-body":
            return self._send(200, {"len": len(body), "sha": hashlib.sha256(body).hexdigest()})
        return self._send(404, {"error": "not found"})


def start_upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfy)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def start_bridge(upstream, allow):
    bridge = cb.Bridge(upstream, allow, verbose=False)
    srv = cb.make_server(bridge, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def req(method, url, body=None, headers=None, timeout=20):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def ws_tunnel(hostport, path, origin, payload=b"ping-through-tunnel"):
    host, port = hostport.split(":")
    s = socket.create_connection((host, int(port)), timeout=10)
    key = base64.b64encode(b"0123456789abcdef").decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n"
    )
    s.sendall(handshake.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    s.sendall(payload)
    s.settimeout(5)
    got = rest
    while len(got) < len(b"echo:") + len(payload):
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        got += chunk
    s.close()
    return head.decode("utf-8", "replace"), got


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    up_srv, upstream = start_upstream()
    br_srv, bridge = start_bridge(upstream, ["http://localhost:5500"])
    br_hostport = bridge.split("//")[1]
    ORIGIN = "http://localhost:5500"
    H = {"Origin": ORIGIN}

    fails = []

    def check(cond, name, detail=""):
        print(f"  [{'pass' if cond else 'FAIL'}] {name}" + (f"   {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    print("-" * 70)
    print("1) 证明「不加桥接就是 403」—— 直接打上游，带 Origin")
    st, _, _ = req("GET", upstream + "/system_stats", headers=H)
    check(st == 403, "直连上游带 Origin 被 origin_only 中间件拒绝", f"实际 {st}")
    st, _, _ = req("GET", upstream + "/system_stats", headers={"Sec-Fetch-Site": "cross-site"})
    check(st == 403, "直连上游带 Sec-Fetch-Site: cross-site 被拒", f"实际 {st}")

    print("\n2) 预检（OPTIONS）")
    st, h, _ = req("OPTIONS", bridge + "/prompt", headers={
        "Origin": ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type, x-session-id",
    })
    check(st == 204, "预检返回 204", f"实际 {st}")
    check(h.get("Access-Control-Allow-Origin") == ORIGIN, "回显 Origin（不是 *）", h.get("Access-Control-Allow-Origin"))
    check("POST" in (h.get("Access-Control-Allow-Methods") or ""), "允许 POST")
    check("x-session-id" in (h.get("Access-Control-Allow-Headers") or "").lower(), "回显自定义请求头")
    check(h.get("Access-Control-Max-Age") == "600", "有 Max-Age（减少预检次数）")
    check(h.get("Access-Control-Allow-Credentials") == "true", "允许凭据")
    check("Origin" in (h.get("Vary") or ""), "带 Vary: Origin")

    print("\n3) Private Network Access 预检")
    st, h, _ = req("OPTIONS", bridge + "/prompt", headers={
        "Origin": ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Private-Network": "true",
    })
    check(h.get("Access-Control-Allow-Private-Network") == "true",
          "回 Access-Control-Allow-Private-Network（ComfyUI 官方没有的）")

    print("\n4) 实际请求：跨域 + 上游 origin 校验被绕过")
    st, h, body = req("GET", bridge + "/system_stats", headers=H)
    check(st == 200, "GET /system_stats 通过桥接成功", f"实际 {st}")
    check(h.get("Access-Control-Allow-Origin") == ORIGIN, "实际响应带 ACAO")
    check("Content-Disposition" in (h.get("Access-Control-Expose-Headers") or ""), "Expose-Headers 已设置")
    check(json.loads(body)["devices"][0]["name"].endswith("RTX 4090"), "响应内容完整透传")

    st, _, body = req("GET", bridge + "/echo-headers", headers=dict(H, **{"Sec-Fetch-Site": "cross-site"}))
    seen = json.loads(body)["headers"]
    check(st == 200, "带 Sec-Fetch-Site: cross-site 也能通过", f"实际 {st}")
    check("Origin" not in seen, "Origin 已被摘掉（否则上游 403）")
    check("Sec-Fetch-Site" not in seen, "Sec-Fetch-Site 已被摘掉")
    check(seen.get("X-Bridge-Origin") == ORIGIN, "原 Origin 改名 X-Bridge-Origin 转发，信息不丢")
    check(seen.get("Host") == upstream.split("//")[1], "Host 改写为上游地址")

    print("\n5) POST JSON 转发")
    wf = {"prompt": {"4": {"class_type": "CheckpointLoaderSimple",
                           "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}}},
          "client_id": "test-1"}
    st, _, body = req("POST", bridge + "/prompt", wf, headers=dict(H, **{"Content-Type": "application/json"}))
    check(st == 200 and json.loads(body)["prompt_id"] == "pid-1", "POST /prompt 成功")
    check(HITS["prompt_body"] == json.dumps(wf).encode(), "上游收到的 body 与发出的完全一致")

    print("\n6) 二进制透传（/view 图片）")
    st, h, data = req("GET", bridge + "/event", headers=H)
    check(st == 200 and data == PNG_1PX, "图片字节完全一致")
    check(h.get("Content-Type") == "image/png", "Content-Type 保留")

    print("\n7) 大 body 完整性与上限")
    big = b"X" * (2 * 1024 * 1024)
    st, _, body = req("POST", bridge + "/echo-body", big, headers=dict(H, **{"Content-Type": "application/octet-stream"}))
    got = json.loads(body)
    check(st == 200 and got["len"] == len(big) and got["sha"] == hashlib.sha256(big).hexdigest(),
          f"2MB 上传无损（{got.get('len')} 字节）")

    print("\n8) WebSocket 隧道")
    head, got = ws_tunnel(br_hostport, "/ws?clientId=test", ORIGIN)
    check(head.startswith("HTTP/1.1 101"), "WS 握手 101", head.splitlines()[0] if head else "无响应")
    check("Sec-WebSocket-Accept" in head, "Accept 头原样带回")
    check(got == b"echo:ping-through-tunnel", "隧道内字节双向可达", repr(got[:40]))

    print("\n9) 错误与边界")
    st, _, _ = req("GET", bridge + "/not-exist", headers=H)
    check(st == 404, "上游 404 原样透传")
    st, h, _ = req("GET", bridge + "/system_stats", headers={"Origin": "http://evil.example"})
    check("Access-Control-Allow-Origin" not in h, "非白名单来源不给 ACAO（浏览器侧会被拦）")
    st, h, body = req("GET", bridge + "/__bridge/health", headers=H)
    info = json.loads(body)
    check(st == 200 and info["ok"] is True, "健康检查可用")
    check(info.get("device", "").endswith("RTX 4090"), "健康检查带回设备信息")

    # 上游挂掉
    dead_srv, dead_up = start_upstream()
    dead_srv.shutdown()
    dead_srv.server_close()
    dead_bridge, dead_url = start_bridge(dead_up, ["*"])
    st, _, _ = req("GET", dead_url + "/system_stats", headers=H, timeout=10)
    check(st >= 500, "上游不可达时返回 5xx 而不是挂死", f"实际 {st}")

    print("\n10) 上游确实一次都没收到被拦的请求")
    check(HITS["upstream_403"] == 2, f"上游只拒绝了最初两次直连（{HITS['upstream_403']}），"
                                     "经桥接的请求全部放行")

    br_srv.shutdown()
    up_srv.shutdown()
    dead_bridge.shutdown()
    print("-" * 70)
    if fails:
        print(f"失败 {len(fails)} 项：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过：桥接层正确实现了 CORS，并成功绕过了上游的 origin 校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
