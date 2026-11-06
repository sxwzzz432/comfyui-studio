#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comfy_smoke.py — 对任意 ComfyUI 地址跑一次最简出图，顺便打印每一跳的 CORS 响应头。

它干四件事：
  1. 验证工作流能提交、能跑完、能取回图
  2. 每个请求都带 Origin 头，并打印服务端回的 CORS 头 —— 这正是浏览器要看的东西
  3. 执行失败时**立刻**报出是哪个节点、什么异常（不再干等超时）
  4. --list 列出服务端真实的模型清单，告诉你内置工作流适不适用

重要：内置工作流是 SD1.5/SDXL 的经典 7 节点管线（CheckpointLoaderSimple）。
如果你的 ComfyUI 装的是 Flux / Qwen-Image / Anima 这类分离式架构
（diffusion_models + text_encoders + vae 分开），checkpoints 里通常没有可用模型，
内置工作流会直接失败 —— 这时请用 --workflow 指定你自己从 ComfyUI 导出的 API 工作流。

用法：
  python comfy_smoke.py --list                           # 只看模型清单
  python comfy_smoke.py                                   # 内置 SD 工作流打本机 8188
  python comfy_smoke.py --workflow mine_api.json          # 用你自己的工作流
  python comfy_smoke.py --base http://192.168.1.6:8188 --origin http://localhost:5500
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
UA = "comfy-smoke/0.2"

# 常见的加载器节点 -> 输入字段。用来枚举服务端真实装了什么。
LOADERS = [
    ("CheckpointLoaderSimple", "ckpt_name", "checkpoints"),
    ("UNETLoader", "unet_name", "diffusion_models"),
    ("LoraLoader", "lora_name", "loras"),
    ("VAELoader", "vae_name", "vae"),
    ("CLIPLoader", "clip_name", "text_encoders"),
    ("DualCLIPLoader", "clip_name1", "text_encoders"),
]


def http(method, url, body=None, origin=None, timeout=180):
    hdrs = {"User-Agent": UA}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if origin:
        hdrs["Origin"] = origin
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k: v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k: v for k, v in e.headers.items()}, e.read(200_000) if e.fp else b""
    except (urllib.error.URLError, OSError) as e:
        return 0, {}, str(e).encode()


def cors_brief(headers):
    get = lambda n: next((v for k, v in headers.items() if k.lower() == n.lower()), None)
    parts = []
    for label, key in (("ACAO", "Access-Control-Allow-Origin"),
                       ("ACAC", "Access-Control-Allow-Credentials"),
                       ("ACAH", "Access-Control-Allow-Headers"),
                       ("ACEH", "Access-Control-Expose-Headers")):
        v = get(key)
        if v is not None:
            parts.append(f"{label}={v if len(v) < 42 else v[:39] + '...'}")
    return "  ".join(parts) if parts else "(无 CORS 头)"


def list_inventory(base, origin):
    """返回 {类别: [文件名]}。缺失的节点直接跳过（不是每套安装都装了所有加载器）。"""
    inv = {}
    for node, field, label in LOADERS:
        st, _, body = http("GET", f"{base}/object_info/{node}", origin=origin, timeout=60)
        if st != 200:
            continue
        try:
            spec = json.loads(body)[node]["input"]["required"][field][0]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        names = spec if isinstance(spec, list) else []
        if names:
            inv.setdefault(label, [])
            for n in names:
                if n not in inv[label]:
                    inv[label].append(n)
    return inv


def build_sd_workflow(ckpt, pos, neg, steps, w, h, seed, prefix):
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": 6.0, "sampler_name": "euler",
                         "scheduler": "normal", "denoise": 1.0, "model": ["4", 0],
                         "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": prefix, "images": ["8", 0]}},
    }


def coerce(v):
    """命令行来的值：数字就转数字，像 JSON 就解析，其余当字符串。"""
    s = v.strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if s[:1] in "{[":
        try:
            return json.loads(s)
        except ValueError:
            pass
    return v


def apply_overrides(wf, args):
    """只覆盖用户显式指定的参数，没指定就保留工作流原值（这点很重要）。"""
    changed = []
    for nid, node in wf.items():
        ins = node.get("inputs") or {}
        if args.seed is not None and "seed" in ins and not isinstance(ins["seed"], list):
            ins["seed"] = args.seed
            changed.append(f"{nid}.seed={args.seed}")
        if args.steps is not None and "steps" in ins and isinstance(ins["steps"], (int, str)):
            ins["steps"] = args.steps
            changed.append(f"{nid}.steps={args.steps}")
        if args.size is not None and node.get("class_type") == "EmptyLatentImage":
            ins["width"] = ins["height"] = args.size
            changed.append(f"{nid}.size={args.size}")
        if args.size is not None and node.get("class_type") == "ResolutionPreset":
            try:
                rj = json.loads(ins.get("resolution_json") or "{}")
                rj["width"] = rj["height"] = args.size
                ins["resolution_json"] = json.dumps(rj)
                changed.append(f"{nid}.resolution={args.size}")
            except (ValueError, TypeError):
                pass

    # 通用逃生口：--set 节点id.字段=值
    for item in (args.set or []):
        if "=" not in item or "." not in item.split("=", 1)[0]:
            print(f"[WARN] --set 格式应为 节点id.字段=值，忽略：{item}")
            continue
        key, val = item.split("=", 1)
        nid, field = key.split(".", 1)
        node = wf.get(nid)
        if not node:
            print(f"[WARN] --set 指向的节点 {nid} 不在工作流里，忽略")
            continue
        node.setdefault("inputs", {})[field] = coerce(val)
        changed.append(f"{nid}.{field}={val[:24]}")
    return changed


def wait_result(base, pid, origin, deadline):
    """返回 (image_dict, error_dict)。谁先到返回谁。"""
    while time.time() < deadline:
        st, _, body = http("GET", f"{base}/history/{pid}", origin=origin, timeout=30)
        if st == 200:
            entry = json.loads(body).get(pid)
            if entry:
                stt = entry.get("status") or {}
                if stt.get("status_str") == "error":
                    for msg in stt.get("messages") or []:
                        if msg and msg[0] == "execution_error":
                            return None, msg[1]
                    return None, {"exception_message": "未知错误", "raw": stt}
                for out in (entry.get("outputs") or {}).values():
                    for im in out.get("images") or []:
                        if im.get("type", "output") == "output":
                            return im, None
        time.sleep(0.4)
    return None, {"exception_message": "等待超时"}


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="ComfyUI 出图冒烟测试 + CORS 头观察")
    ap.add_argument("--base", default="http://127.0.0.1:8188")
    ap.add_argument("--origin", default="http://localhost:5500")
    ap.add_argument("--workflow", default=None, help="API 格式工作流 JSON 文件路径")
    ap.add_argument("--ckpt", default=None, help="内置工作流的 checkpoint，默认取第一个")
    ap.add_argument("--steps", type=int, default=None, help="只在内置工作流或显式指定时生效")
    ap.add_argument("--size", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--set", action="append", metavar="节点id.字段=值",
                    help="通用参数覆盖，可重复，例如 --set 1134.steps=6 --set 784.seed=123")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--list", action="store_true", help="只列模型清单")
    args = ap.parse_args(argv)

    base = args.base.rstrip("/")
    t_all = time.time()

    print(f"目标      {base}")
    print(f"模拟来源  {args.origin}")
    print("-" * 74)

    # 1. /system_stats
    t = time.time()
    st, hdrs, body = http("GET", base + "/system_stats", origin=args.origin)
    if st != 200:
        print(f"[FAIL] /system_stats -> {st or '连接失败'} {body[:160].decode('utf-8', 'replace')}")
        return 2
    dev = (json.loads(body).get("devices") or [{}])[0]
    print(f"[ OK ] /system_stats       200  {(time.time()-t)*1000:.0f}ms")
    print(f"       设备 {dev.get('name')}  显存 {round((dev.get('vram_total') or 0)/1024**3)}GB")
    print(f"       CORS {cors_brief(hdrs)}")

    # 2. 模型清单
    t = time.time()
    inv = list_inventory(base, args.origin)
    print(f"[ OK ] 模型清单           {(time.time()-t)*1000:.0f}ms")
    for label, names in inv.items():
        print(f"       {label:<16} {len(names):>3} 个   " +
              ", ".join(names[:3]) + (" …" if len(names) > 3 else ""))
    if not inv:
        print("       (一个加载器都没枚举到，检查 /object_info 是否可用)")

    if args.list:
        print("-" * 74)
        print("仅列清单，未出图。")
        return 0

    # 3. 决定用哪个工作流
    if args.workflow:
        with open(args.workflow, encoding="utf-8") as f:
            wf = json.load(f)
        if "prompt" in wf and isinstance(wf["prompt"], dict):
            wf = wf["prompt"]        # 兼容 {"prompt": {...}} 的包装格式
        changed = apply_overrides(wf, args)
        print(f"[ OK ] 载入工作流        {os.path.basename(args.workflow)}  {len(wf)} 节点")
        if changed:
            print(f"       覆盖参数 {', '.join(changed[:6])}")
        source = f"自定义 {os.path.basename(args.workflow)}"
    else:
        ckpts = inv.get("checkpoints") or []
        if not ckpts:
            print("[FAIL] checkpoints 为空 —— 内置 SD 工作流不可用")
            print("       → 用 --workflow 指定你从 ComfyUI 导出的 API 工作流")
            return 1
        ckpt = args.ckpt or ckpts[0]
        if args.ckpt and args.ckpt not in ckpts:
            print(f"[WARN] 指定 ckpt 不存在，改用 {ckpts[0]}")
            ckpt = ckpts[0]
        if len(ckpts) == 1 and any(k in ckpt.lower() for k in ("sam", "seg", "depth", "clip_vision")):
            print(f"[WARN] 唯一的 checkpoint 看起来不是扩散模型（{ckpt}）")
            print("       内置工作流大概会在 KSampler 报错。建议用 --workflow")
        wf = build_sd_workflow(ckpt, "a red apple on a white table, product photo",
                               "blurry, lowres, watermark",
                               args.steps if args.steps is not None else 8,
                               args.size if args.size is not None else 512,
                               args.size if args.size is not None else 512,
                               args.seed if args.seed is not None else 42,
                               "cors_verify")
        print(f"[ OK ] 使用内置 SD 工作流  ckpt={ckpt}")
        source = f"内置 / {ckpt}"

    # 4. 提交
    t = time.time()
    st, hdrs, body = http("POST", base + "/prompt",
                          {"prompt": wf, "client_id": "smoke-" + str(int(t))},
                          origin=args.origin)
    if st != 200:
        print(f"[FAIL] POST /prompt -> {st}\n       {body[:500].decode('utf-8', 'replace')}")
        return 1
    j = json.loads(body)
    pid = j["prompt_id"]
    print(f"[ OK ] POST /prompt        200  {(time.time()-t)*1000:.0f}ms  prompt_id={pid}")
    print(f"       CORS {cors_brief(hdrs)}")

    # 5. 等结果（失败立刻返回，不再干等）
    t = time.time()
    img, err = wait_result(base, pid, args.origin, time.time() + args.timeout)
    if err:
        node = err.get("node_type") or err.get("node_id") or "?"
        print(f"[FAIL] 执行失败           {(time.time()-t):.1f}s  节点 {node}")
        msg = err.get("exception_message") or json.dumps(err, ensure_ascii=False)[:300]
        print(f"       {str(msg).strip()[:500]}")
        if "KSampler" in str(node):
            print("       提示：KSampler 拿到的 model 不是扩散模型。"
                  "内置工作流只适用 SD1.5/SDXL 的 checkpoint；")
            print("             Flux / Qwen-Image / Anima 请导出 API 工作流后用 --workflow。")
        return 1
    print(f"[ OK ] 执行完成           {(time.time()-t):.2f}s")

    # 6. 取图
    q = urllib.parse.urlencode({"filename": img["filename"],
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")})
    t = time.time()
    st, hdrs, data = http("GET", f"{base}/view?{q}", origin=args.origin)
    if st != 200 or not data:
        print(f"[FAIL] GET /view -> {st}")
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "smoke_" + img["filename"].replace("/", "_"))
    with open(path, "wb") as f:
        f.write(data)
    print(f"[ OK ] GET /view           200  {(time.time()-t)*1000:.0f}ms  {len(data)/1024:.1f}KB")
    print(f"       CORS {cors_brief(hdrs)}")
    print("-" * 74)
    print(f"全部通过  总耗时 {time.time()-t_all:.2f}s  来源 {source}")
    print(f"图片已保存：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
