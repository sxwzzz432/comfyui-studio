#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cors_doctor.py — 给你正在跑的 ComfyUI 做一次跨域体检

它不猜，直接按浏览器的规则发请求、读响应头，然后告诉你：
  - ComfyUI 的 CORS 到底开没开（以及是哪种模式）
  - 浏览器侧哪些请求会成功、哪些会被拦，为什么
  - 每个问题对应哪种改法

注意：这个脚本是从「HTTP 客户端」的角度审计响应头，它复刻的是浏览器的判定逻辑；
真正的浏览器级验证请用 cors_demo.html（那个才是真跑一遍）。

用法：
  python cors_doctor.py                                  # 默认体检 127.0.0.1:8188
  python cors_doctor.py --base http://192.168.1.20:8188
  python cors_doctor.py --origin http://localhost:5500 --check-lan
  python cors_doctor.py --json               # 机器可读输出

字段说明（都在 Response Headers 里）：
  ACAO  = Access-Control-Allow-Origin
  ACAC  = Access-Control-Allow-Credentials
  ACAM  = Access-Control-Allow-Methods
  ACAH  = Access-Control-Allow-Headers
  ACEH  = Access-Control-Expose-Headers
  ACAPN = Access-Control-Allow-Private-Network
"""

import argparse
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ORIGIN = "http://localhost:5500"
UA = "comfy-cors-doctor/0.1"


def http(method, url, headers=None, body=None, timeout=15):
    r = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, {k: v for k, v in resp.headers.items()}, resp.read(2_000_000)
    except urllib.error.HTTPError as e:
        return e.code, {k: v for k, v in e.headers.items()}, e.read(200_000) if e.fp else b""
    except (urllib.error.URLError, OSError) as e:
        return 0, {}, str(e).encode()


def h(headers, name):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # 不发包，只为拿到出口网卡地址
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def probe(base, origin=DEFAULT_ORIGIN, check_lan=False):
    """返回结构化体检结果，供 render() 与测试共用。"""
    base = base.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    port = parsed.port or 80
    res = {"base": base, "origin": origin, "checks": [], "mode": None, "ok": False}

    def add(key, title, status, detail, fix=""):
        res["checks"].append({"key": key, "title": title, "status": status,
                              "detail": detail, "fix": fix})

    # --- 0. 连通性
    st, _, body = http("GET", base + "/system_stats")
    if st != 200:
        add("reach", "ComfyUI 可达", "fail",
            f"GET /system_stats -> {st or '连接失败'} {body[:120].decode('utf-8', 'replace')}",
            "先确认 ComfyUI 已启动、端口正确")
        return res
    res["ok"] = True
    try:
        dev = (json.loads(body).get("devices") or [{}])[0]
        add("reach", "ComfyUI 可达", "pass",
            f"{dev.get('name', '?')} / 显存 {round((dev.get('vram_total') or 0) / 1024 ** 3)}GB")
    except (ValueError, IndexError):
        add("reach", "ComfyUI 可达", "pass", "HTTP 200（响应不是预期 JSON）")

    # --- 1. 简单请求带 Origin：判定 CORS 模式
    st, hdrs, _ = http("GET", base + "/system_stats", headers={"Origin": origin})
    acao = h(hdrs, "Access-Control-Allow-Origin")
    acac = h(hdrs, "Access-Control-Allow-Credentials")
    vary = h(hdrs, "Vary")
    aceh = h(hdrs, "Access-Control-Expose-Headers")

    if st == 403:
        res["mode"] = "origin_only"
        add("cors", "简单跨域请求", "fail",
            f"带 Origin 的 GET 被 403 拒绝 —— 命中 create_origin_only_middleware()",
            "说明 --enable-cors-header 没生效。要么加该参数，要么用 cors_bridge.py 代理")
    elif acao is None:
        res["mode"] = "none"
        add("cors", "简单跨域请求", "fail",
            f"响应里没有 Access-Control-Allow-Origin（GET -> {st}）",
            "加 --enable-cors-header，或用 cors_bridge.py")
    elif acao == "*":
        res["mode"] = "wildcard"
        if (acac or "").lower() == "true":
            add("cors", "简单跨域请求", "warn",
                "ACAO='*' 且 ACAC='true' —— 这是非法组合。不带凭据的普通 fetch 能过，"
                "但前端一旦用 credentials:'include' 必被浏览器拒绝",
                "前端别用 credentials:'include'；或改用 cors_bridge.py 回显 Origin")
            add("cred", "带凭据的跨域请求", "fail",
                "浏览器会报：The value of the 'Access-Control-Allow-Origin' header "
                "must not be the wildcard '*' when the request's credentials mode is 'include'",
                "需要 cookie 就必须回显 Origin，官方中间件做不到，只能上代理层")
        else:
            add("cors", "简单跨域请求", "pass", "ACAO='*'（不带凭据，安全可用于开发）")
            add("cred", "带凭据的跨域请求", "warn", "ACAO='*' 时浏览器不允许带凭据")
    else:
        res["mode"] = "echo"
        add("cors", "简单跨域请求", "pass", f"ACAO='{acao}'（回显 Origin）")
        add("cred", "带凭据的跨域请求", "pass" if (acac or "").lower() == "true" else "warn",
            f"ACAC={acac}")

    if vary:
        add("vary", "Vary: Origin", "pass", f"Vary={vary}")
    elif st == 200:
        add("vary", "Vary: Origin", "warn", "没有 Vary，中间若有缓存会把 A/B 来源的响应串味",
            "代理层加上 Vary: Origin")

    # --- 2. 预检：自定义头
    st, hdrs, _ = http("OPTIONS", base + "/prompt", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type, x-session-id",
    })
    acam, acah = h(hdrs, "Access-Control-Allow-Methods"), h(hdrs, "Access-Control-Allow-Headers")
    if st not in (200, 204):
        add("preflight", "预检 OPTIONS", "fail", f"OPTIONS -> {st}",
            "预检必须返回 2xx 且带 CORS 头")
    elif acah is None:
        add("preflight", "预检 OPTIONS", "fail", "预检响应没有 ACAH",
            "加 --enable-cors-header 或用代理")
    else:
        allowed = [x.strip().lower() for x in acah.split(",")]
        add("preflight", "预检 OPTIONS", "pass",
            f"ACAM={acam}；ACAH={acah}")
        if "x-session-id" in allowed or "*" in allowed:
            add("custom-header", "自定义请求头", "pass", "x-session-id 被放行")
        else:
            add("custom-header", "自定义请求头", "warn",
                f"ACAH 只有 [{acah}] —— 前端带 x-session-id / Comfy-Usage-Source 之类会预检失败",
                "要么别加自定义头，要么用 cors_bridge.py（它会回显请求头）")

    # --- 3. Private Network Access
    st, hdrs, _ = http("OPTIONS", base + "/prompt", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Private-Network": "true",
    })
    acapn = h(hdrs, "Access-Control-Allow-Private-Network")
    if acapn and acapn.lower() == "true":
        add("pna", "Private Network Access", "pass", "ACAPN=true")
    else:
        add("pna", "Private Network Access", "warn",
            "没有 ACAPN —— 页面从公网域名访问 192.168.x.x:8188 时，Chrome 会拦",
            "纯本地开发用不上；要演示给外网看就走 https 反代，或用 cors_bridge.py")

    # --- 4. Host/Origin 不一致（origin_only 的典型触发条件）
    lan_ips = local_ips()
    st, _, _ = http("GET", base + "/system_stats",
                    headers={"Origin": f"http://{lan_ips[0]}:5500" if lan_ips else origin})
    add("hostorigin", "Host/Origin 一致性检查", "pass" if st == 200 else "warn",
        f"Origin 改成局域网地址后 -> {st}", "403 说明还在 origin_only 模式")

    # --- 5. 下载文件名能不能读到
    st, hdrs, _ = http("GET", base + "/view?filename=__nonexistent__.png&type=output",
                       headers={"Origin": origin})
    cd = h(hdrs, "Content-Disposition")
    if aceh and ("content-disposition" in aceh.lower() or "*" in aceh):
        add("expose", "响应头可读性", "pass", f"ACEH={aceh}")
    else:
        add("expose", "响应头可读性", "warn",
            f"没有 ACEH，前端读不到 Content-Disposition（/view 下载时的文件名）",
            "本项目用不到：文件名从 /history 的 JSON 里拿就行")

    # --- 6. 监听面
    if check_lan and lan_ips:
        reachable = []
        for ip in lan_ips:
            s, _, _ = http("GET", f"http://{ip}:{port}/system_stats", timeout=3)
            if s == 200:
                reachable.append(ip)
        if reachable:
            add("lan", "局域网暴露面", "warn",
                f"这些地址也能直连 ComfyUI：{', '.join(reachable)}"
                + ("；且 CORS 是 * 时任意网页都能驱动它" if res["mode"] == "wildcard" else ""),
                "只在单机用就 --listen 127.0.0.1；要给手机用就走 cors_bridge.py + Origin 白名单")
        else:
            add("lan", "局域网暴露面", "pass", "局域网地址不可达（只监听本机）")
    return res


SYMBOL = {"pass": "✅", "warn": "⚠️ ", "fail": "❌"}


def render(res):
    lines = []
    lines.append("=" * 74)
    lines.append(f"ComfyUI 跨域体检   {res['base']}")
    lines.append(f"模拟页面来源       {res['origin']}")
    lines.append("=" * 74)
    if not res["ok"]:
        for c in res["checks"]:
            lines.append(f"{SYMBOL[c['status']]} {c['title']}：{c['detail']}")
        return "\n".join(lines)

    mode_cn = {
        "origin_only": "未开 CORS（origin_only 中间件在拦）",
        "none": "无 CORS 头",
        "wildcard": "官方模式：ACAO='*'（--enable-cors-header 不带参数）",
        "echo": "回显模式：ACAO=请求 Origin（最佳，通常是代理层做的）",
    }.get(res["mode"], res["mode"])
    lines.append(f"判定模式           {mode_cn}")
    lines.append("-" * 74)
    for c in res["checks"]:
        lines.append(f"{SYMBOL[c['status']]} {c['title']}")
        lines.append(f"     {c['detail']}")
        if c["fix"] and c["status"] != "pass":
            lines.append(f"     → {c['fix']}")
    fails = [c for c in res["checks"] if c["status"] == "fail"]
    warns = [c for c in res["checks"] if c["status"] == "warn"]
    # 「带凭据」这一项失败是硬性的，但只在用 credentials:'include' 时才触发
    hard = [c for c in fails if c["key"] != "cred"]
    lines.append("-" * 74)
    if not fails and not warns:
        lines.append("结论：这套配置下，普通浏览器跨域请求全绿，可以直接开发前端。")
    elif not hard:
        lines.append("结论：普通跨域请求全通，可以直接开发前端。")
        if fails:
            lines.append("     唯一硬伤是「带凭据的跨域请求」（ACAO='*' 与 ACAC 冲突）——"
                         "本项目不用 credentials:'include'，可以忽略。")
        if warns:
            lines.append(f"     另有 {len(warns)} 项警告只影响特定场景，见上面的 → 提示。")
    elif not fails:
        lines.append(f"结论：能用。{len(warns)} 项警告只影响特定场景（见上面的 → 提示）。")
    else:
        lines.append(f"结论：有 {len(hard)} 项会直接让浏览器拦请求，必须先解决。")
    lines.append("=" * 74)
    return "\n".join(lines)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="ComfyUI 跨域配置体检")
    ap.add_argument("--base", default="http://127.0.0.1:8188", help="ComfyUI 地址")
    ap.add_argument("--origin", default=DEFAULT_ORIGIN, help="模拟的前端页面来源")
    ap.add_argument("--check-lan", action="store_true", help="顺带探测局域网是否可达")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    res = probe(args.base, args.origin, args.check_lan)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render(res))
    if not res["ok"]:
        return 2
    hard_fail = [c for c in res["checks"] if c["status"] == "fail" and c["key"] != "cred"]
    return 0 if not hard_fail else 1


if __name__ == "__main__":
    sys.exit(main())
