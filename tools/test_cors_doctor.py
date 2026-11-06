#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_cors_doctor.py — 验证体检工具能正确识别三种 ComfyUI 跨域配置形态。

三种形态都照着真实源码的行为造假：
  A. wildcard   = 加了 --enable-cors-header 但不带参数（ACAO='*' + ACAC='true'）
  B. origin_only = 没加 flag（create_origin_only_middleware，Host≠Origin 就 403）
  C. echo       = 桥接层/自定义反代，回显 Origin

跑法：python test_cors_doctor.py
"""

import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cors_doctor as doc

CORS_OK = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, DELETE, PUT, OPTIONS, PATCH",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Credentials": "true",
}


def make_handler(kind):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _origin(self):
            return self.headers.get("Origin")

        def _blocked(self):
            if kind != "origin_only":
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return True
            host, origin = self.headers.get("Host"), self._origin()
            if host and origin:
                hn = urllib.parse.urlsplit("//" + host).hostname
                on = urllib.parse.urlsplit(origin).hostname
                if hn and on and hn != on:
                    return True
            return False

        def _cors(self, extra=None):
            if kind == "wildcard":
                return dict(CORS_OK)
            if kind == "echo":
                o = self._origin() or "http://localhost:5500"
                d = {
                    "Access-Control-Allow-Origin": o,
                    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": self.headers.get(
                        "Access-Control-Request-Headers") or "Content-Type, Authorization",
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Expose-Headers": "Content-Disposition, Content-Length",
                    "Vary": "Origin",
                }
                if self.headers.get("Access-Control-Request-Private-Network"):
                    d["Access-Control-Allow-Private-Network"] = "true"
                return d
            return {}

        def _send(self, code, payload=b"", extra=None):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for k, v in self._cors().items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self):
            if kind == "origin_only":
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, b"{}")

        def do_GET(self):
            if self._blocked():
                return self._send(403, {"error": "host/origin mismatch"})
            p = urllib.parse.urlsplit(self.path).path
            if p == "/system_stats":
                return self._send(200, {"devices": [{"name": "cuda:0 RTX 4090",
                                                     "vram_total": 24 * 1024 ** 3}]})
            if p == "/view":
                return self._send(404, {"error": "no such file"})
            return self._send(404, {"error": "not found"})

    return H


def start(kind):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kind))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def by_key(res, key):
    for c in res["checks"]:
        if c["key"] == key:
            return c
    return {}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    fails = []

    def check(cond, name, detail=""):
        print(f"  [{'pass' if cond else 'FAIL'}] {name}" + (f"   {detail}" if not cond else ""))
        if not cond:
            fails.append(name)

    print("-" * 70)
    print("A) 官方 --enable-cors-header（ACAO='*' + ACAC='true'）")
    srv, base = start("wildcard")
    res = doc.probe(base)
    check(res["mode"] == "wildcard", "识别为 wildcard 模式", res["mode"])
    check(by_key(res, "reach")["status"] == "pass", "连通性通过")
    check(by_key(res, "cors")["status"] == "warn", "简单请求标为警告（非法组合）")
    check(by_key(res, "cred")["status"] == "fail", "带凭据被判为必失败")
    check(by_key(res, "custom-header")["status"] == "warn", "自定义头被判为不支持")
    check(by_key(res, "pna")["status"] == "warn", "PNA 被判为缺失")
    check(by_key(res, "preflight")["status"] == "pass", "预检本身能通过")
    srv.shutdown()

    print("\nB) 不加 flag（origin_only 中间件）")
    srv, base = start("origin_only")
    res = doc.probe(base)
    check(res["mode"] == "origin_only", "识别为 origin_only 模式", res["mode"])
    check(by_key(res, "cors")["status"] == "fail", "简单请求被判为失败")
    check("403" in by_key(res, "cors")["detail"], "错误信息里点出 403")
    srv.shutdown()

    print("\nC) 桥接层（回显 Origin）")
    srv, base = start("echo")
    res = doc.probe(base)
    check(res["mode"] == "echo", "识别为 echo 模式", res["mode"])
    check(by_key(res, "cred")["status"] == "pass", "带凭据可用")
    check(by_key(res, "custom-header")["status"] == "pass", "自定义头被放行")
    check(by_key(res, "pna")["status"] == "pass", "PNA 已支持")
    check(by_key(res, "vary")["status"] == "pass", "Vary 已存在")
    check(by_key(res, "expose")["status"] == "pass", "Expose-Headers 已设置")
    check(not [c for c in res["checks"] if c["status"] == "fail"], "echo 模式下没有 fail 项")
    text = doc.render(res)
    check("回显模式" in text, "报告里标出了模式")
    check("全绿" in text, "全绿时给出正面结论")
    srv.shutdown()

    print("\nD) 完全连不上")
    res = doc.probe("http://127.0.0.1:9")
    check(res["ok"] is False, "连不上时 ok=False")
    check(doc.render(res).count("❌") >= 1, "报告里明确标失败")

    print("-" * 70)
    if fails:
        print(f"失败 {len(fails)} 项：" + ", ".join(fails))
        return 1
    print("体检工具在三种形态下判定均正确")
    return 0


if __name__ == "__main__":
    sys.exit(main())
