#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_lora_field.py — 对活着的 ComfyUI 做行为实验，确认 LoraManager 到底读哪个字段。

背景：comfyui-lora-manager 的 load_loras() 第一行是 `del text`，理论上它只认 loras
这个结构化 widget 字段。但「源码上看是这样」和「服务端实际这样」是两件事，
所以这里做两组对照实验（各出一张 2 步 512x512 的图，几秒钟）：

  A. loras=真LoRA（active） + text=假名字   → 成功  ⇒ 证明 text 被忽略
  B. loras=假LoRA（active） + text=真名字   → 报错  ⇒ 证明 loras 才是权威
  C. loras=假LoRA（active=false） + text=空  → 成功  ⇒ 证明 active=false 会被跳过

如果 A 报错或 B 成功，说明结论反了，页面就得改成以 text 为准。

跑法：python test_lora_field.py [--base http://127.0.0.1:8188]
"""

import argparse
import json
import os
import sys
import time

import comfy_smoke as cs

HERE = os.path.dirname(os.path.abspath(__file__))
WF = os.path.join(os.path.dirname(HERE), "workflow", "moss.json")
# 这个 LoRA 在 moss.json 里本来就有，且 active=true，服务端确认存在
GOOD_LORA = "ba_style_moss_uma_v1_1000"
BAD_LORA = "THIS_LORA_DOES_NOT_EXIST_XYZ"


def entry(name, strength="0.50", active=True):
    return {"name": name, "strength": strength, "active": active,
            "expanded": False, "clipStrength": strength, "locked": False}


def submit(base, origin, loras_value, text_value, steps=2, size=512):
    wf = json.load(open(WF, encoding="utf-8"))
    wf["1135"]["inputs"]["loras"] = {"__value__": loras_value}
    wf["1135"]["inputs"]["text"] = text_value
    wf["1160"]["inputs"]["positive"] = "a red apple"
    wf["1134"]["inputs"]["steps"] = steps
    wf["1140"]["inputs"]["resolution_json"] = json.dumps({"version": 1, "width": size, "height": size})
    wf["960"]["inputs"]["filename_prefix"] = "lora_field_test"

    st, _, body = cs.http("POST", base + "/prompt",
                          {"prompt": wf, "client_id": "lora-" + str(int(time.time()))},
                          origin=origin)
    if st != 200:
        return None, f"提交被拒 HTTP {st}: {body[:300].decode('utf-8', 'replace')}"
    pid = json.loads(body)["prompt_id"]
    img, err = cs.wait_result(base, pid, origin, time.time() + 180)
    if err:
        return False, f"{err.get('node_type')}: {str(err.get('exception_message'))[:220]}"
    return True, img["filename"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8188")
    ap.add_argument("--origin", default="http://127.0.0.1:5500")
    args = ap.parse_args()
    base, origin = args.base.rstrip("/"), args.origin

    st, _, _ = cs.http("GET", base + "/system_stats", origin=origin)
    if st != 200:
        print("ComfyUI 不可达，跳过（这个测试需要活着的服务）")
        return 2
    inv = cs.list_inventory(base, origin)
    names = [n.replace("\\", "/").split("/")[-1].rsplit(".", 1)[0] for n in inv.get("loras", [])]
    if GOOD_LORA not in names:
        print(f"[SKIP] 服务端没有 {GOOD_LORA}，换一个存在的 LoRA 再跑")
        print("       现有前 5 个：" + ", ".join(names[:5]))
        return 2

    fails = []

    def check(cond, name, detail=""):
        print(f"  [{'pass' if cond else 'FAIL'}] {name}" + (f"   {detail}" if not cond else ""))
        if not cond:
            fails.append(name)

    print("-" * 70)
    print("A) loras=真LoRA + text=假名字    （若成功 ⇒ text 被忽略）")
    ok, info = submit(base, origin, [entry(GOOD_LORA)],
                      f"<lora:{BAD_LORA}:0.50>")
    check(ok is True, "A 出图成功，说明 text 里的假名字没被读取", str(info))

    print("\nB) loras=假LoRA + text=真名字    （若报错 ⇒ loras 才是权威）")
    ok, info = submit(base, origin, [entry(BAD_LORA)],
                      f"<lora:{GOOD_LORA}:0.50>")
    check(ok is False, "B 报错，说明服务端确实读了 loras", str(info))
    if ok is False:
        check(BAD_LORA in str(info), "错误信息里出现的就是 loras 里的假名字", str(info))

    print("\nC) loras=假LoRA 但 active=false   （若成功 ⇒ 未启用的被跳过）")
    ok, info = submit(base, origin, [entry(GOOD_LORA, "0.50", True),
                                     entry(BAD_LORA, "0.50", False)], "")
    check(ok is True, "C 出图成功，说明 active=false 的条目不会加载", str(info))

    print("-" * 70)
    if fails:
        print(f"失败 {len(fails)} 项：" + ", ".join(fails))
        print("→ 结论与源码不一致，页面应以 text 为准")
        return 1
    print("结论确认：LoraManager 只认 loras 结构化字段，text 被丢弃，active=false 会被跳过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
