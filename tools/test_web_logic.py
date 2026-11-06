#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_web_logic.py — 校验「网页 JS 实现」与「workflow_engine.py」完全等价。

工作流的参数绑定现在是**自动扫描**出来的（不再有手写映射文件），
所以扫描器和应用器都必须两边一致。这个测试把 app.html 里的纯逻辑交给 Node 执行，然后：

  1. scanWorkflow：推导出的参数表（key/标签/类型/落点）必须一致
  2. readParams：从工作流还原的参数值必须一致
  3. applyParams：写入后的整份工作流必须一致（数值按语义比，4 与 4.0 等同）
  4. LoRA：loras 结构化字段与 text 字段两边都要一致（含 "0.80" 字符串格式）
  5. 换一份「多采样器 + 字面量宽高 + 普通 LoraLoader」的工作流再比一遍

跑法：python test_web_logic.py       依赖 node v18+
"""

import json
import os
import subprocess
import sys

import workflow_engine as we

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MOSS = os.path.join(REPO, "workflow", "moss.json")
PROBE = os.path.join(HERE, "web_logic_probe.js")

MINI = {
    "1": {"class_type": "KSampler", "inputs": {
        "steps": 20, "cfg": 7, "denoise": 1, "sampler_name": "euler",
        "scheduler": "normal", "seed": 1, "model": ["2", 0],
        "positive": ["3", 0], "negative": ["4", 0], "latent_image": ["5", 0]}},
    "1b": {"class_type": "KSamplerAdvanced", "inputs": {
        "steps": 12, "cfg": 2, "seed": 3, "add_noise": "enable",
        "return_with_leftover_noise": "disable"}},
    "2": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "m.safetensors"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["2", 1]}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "ugly", "clip": ["2", 1]}},
    "5": {"class_type": "EmptyLatentImage",
          "inputs": {"width": 512, "height": 768, "batch_size": 1}},
    "6": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}},
    "7": {"class_type": "LoraLoader", "inputs": {
        "lora_name": "a.safetensors", "strength_model": 0.7,
        "model": ["2", 0], "clip": ["2", 1]}},
}


def probe(mode, wf_path=MOSS):
    r = subprocess.run(["node", PROBE, mode, wf_path],
                       capture_output=True, cwd=HERE, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("node 探针失败：" + r.stderr.decode("utf-8", "replace")[:700])
    return json.loads(r.stdout.decode("utf-8"))


def norm_spec(spec):
    """只比驱动行为的部分：key / 标签 / 类型 / 落点 / 适配器。"""
    out = []
    for f in we.flatten_fields(spec):
        out.append((f["key"], f.get("label"), f.get("kind"),
                    [(t["node"], t["input"], t.get("adapter")) for t in f["targets"]]))
    return out


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

    moss = json.load(open(MOSS, encoding="utf-8"))
    py_spec = we.scan_workflow(moss)
    print("-" * 70)

    # ---- 1. 扫描结果一致
    js_spec = probe("scan")["spec"]
    a, b = norm_spec(js_spec), norm_spec(py_spec)
    if a != b:
        for i in range(min(len(a), len(b))):
            if a[i] != b[i]:
                print(f"       首个差异 #{i}: JS={a[i]}")
                print(f"                     PY={b[i]}")
                break
        if len(a) != len(b):
            print(f"       数量不同 JS={len(a)} PY={len(b)}")
    check(a == b, f"scanWorkflow：两边推导出同样的 {len(b)} 个参数")
    check(len(b) == 16, f"内置工作流推导出 16 个参数（实际 {len(b)}）")
    check(py_spec["stats"]["samplers"] == 1, "识别 1 个采样器")
    check(js_spec["warnings"] == py_spec["warnings"], "两边 warnings 一致",
          f"JS={js_spec['warnings']} PY={py_spec['warnings']}")

    # ---- 2. readParams 一致
    js_read = probe("read")["params"]
    py_read = we.read_params(moss, py_spec)
    diff = [k for k in set(js_read) | set(py_read) if js_read.get(k) != py_read.get(k)]
    if diff:
        for k in diff[:4]:
            print(f"       差异 {k}: JS={js_read.get(k)!r} PY={py_read.get(k)!r}")
    check(not diff, "readParams：两边还原的参数值一致", str(diff))
    check(py_read["1140.resolution_json"] == "1024x1536", "分辨率解析为 1024x1536")
    check(len(py_read["1135.loras"]) == 13, "13 个 LoRA")
    check(sum(1 for x in py_read["1135.loras"] if x["active"]) == 10, "其中 10 个启用")
    check(py_read["1160.positive"] == "", "正向提示词原值为空串")

    # ---- 3. applyParams 一致
    values = {
        "1160.positive": "1girl, warm sunlight", "1153.positive": "bad quality",
        "1122.unet_name": "U.safetensors", "770.clip_name": "C.safetensors",
        "770.type": "qwen_image", "769.vae_name": "V.safetensors",
        "1135.loras": [
            {"name": "styleA", "strength": 0.8, "active": True},
            {"name": "styleB", "strength": 0.55, "active": False},
            {"name": "styleC", "strength": 1, "active": True},
        ],
        "1134.steps": "28", "1134.cfg": "3.5", "1134.sampler_name": "dpmpp_2m",
        "1134.scheduler": "beta", "1134.denoise": "0.95", "784.seed": "123456",
        "1140.resolution_json": "832x1216", "1146.batch_size": "3",
        "960.filename_prefix": "web/test",
    }
    js = probe(json.dumps(values, ensure_ascii=False))
    py_wf, py_changes, _ = we.apply_params(moss, py_spec, values)

    check(js["workflow"] == json.loads(json.dumps(py_wf)),
          "applyParams：两边生成的工作流完全一致")
    if js["workflow"] != json.loads(json.dumps(py_wf)):
        for nid in sorted(js["workflow"]):
            x = json.dumps(js["workflow"][nid], sort_keys=True, ensure_ascii=False)
            y = json.dumps(py_wf[nid], sort_keys=True, ensure_ascii=False)
            if x != y:
                print(f"       首个差异节点 {nid}:")
                print(f"         JS {x[:170]}")
                print(f"         PY {y[:170]}")
                break

    w = js["workflow"]
    check(w["1160"]["inputs"]["positive"] == "1girl, warm sunlight", "正向提示词")
    check(w["1134"]["inputs"]["steps"] == 28, "steps 是数字")
    check(float(w["1134"]["inputs"]["cfg"]) == 3.5, "cfg 是数字")
    check(w["784"]["inputs"]["seed"] == 123456, "种子写到上游 784")
    check(json.loads(w["1140"]["inputs"]["resolution_json"])
          == {"version": 1, "width": 832, "height": 1216}, "分辨率 + 保留 version")

    # ---- 4. LoRA
    lw = w["1135"]["inputs"]["loras"]
    check(isinstance(lw, dict) and "__value__" in lw, "loras 是 {'__value__': [...]}")
    check([x["name"] for x in lw["__value__"]] == ["styleA", "styleB", "styleC"], "LoRA 顺序")
    check(lw["__value__"][0]["strength"] == "0.80", "权重写成两位小数字符串",
          repr(lw["__value__"][0]["strength"]))
    check(lw["__value__"][1]["active"] is False, "未启用的 active=false")
    check(w["1135"]["inputs"]["text"] == "<lora:styleA:0.80> <lora:styleC:1.00>",
          "text 只含启用的", repr(w["1135"]["inputs"]["text"]))

    # ---- 5. 换一份工作流再比一遍（多采样器 / 字面量宽高 / 普通 LoraLoader）
    mini_path = os.path.join(HERE, "out", "_mini_wf.json")
    os.makedirs(os.path.dirname(mini_path), exist_ok=True)
    json.dump(MINI, open(mini_path, "w", encoding="utf-8"))
    try:
        py_mini_spec = we.scan_workflow(MINI)
        js_mini = probe("scan", mini_path)["spec"]
        check(norm_spec(js_mini) == norm_spec(py_mini_spec), "minimal 工作流：扫描结果一致",
              f"{len(norm_spec(js_mini))} vs {len(norm_spec(py_mini_spec))}")
        keys = set(f["key"] for f in we.flatten_fields(py_mini_spec))
        check(py_mini_spec["stats"]["samplers"] == 2, "minimal：2 个采样器各成一組")
        check("3.text" in keys and "4.text" in keys, "minimal：标准 CLIPTextEncode 正负提示词")
        check("5.width" in keys and "5.height" in keys, "minimal：字面量宽高")
        check("7.lora_name" in keys and "7.strength_model" in keys, "minimal：普通 LoraLoader")
        check("1b.steps" in keys and "1b.cfg" in keys, "minimal：第二个采样器的参数")

        mini_vals = {"3.text": "a dog", "5.width": "640", "1b.steps": "9",
                     "7.strength_model": "0.35"}
        js2 = probe(json.dumps(mini_vals), mini_path)
        py2, _, _ = we.apply_params(MINI, py_mini_spec, mini_vals)
        check(js2["workflow"] == json.loads(json.dumps(py2)),
              "minimal：applyParams 结果一致")
        w2 = js2["workflow"]
        check(w2["3"]["inputs"]["text"] == "a dog", "minimal：写提示词")
        check(w2["5"]["inputs"]["width"] == 640, "minimal：写宽度")
        check(w2["1b"]["inputs"]["steps"] == 9, "minimal：写第二个采样器的步数")
        check(abs(float(w2["7"]["inputs"]["strength_model"]) - 0.35) < 1e-9, "minimal：写 LoRA 权重")
    finally:
        try:
            os.remove(mini_path)
        except OSError:
            pass

    print("-" * 70)
    if fails:
        print(f"失败 {len(fails)} 项：" + ", ".join(fails))
        return 1
    print("网页 JS 与 workflow_engine.py 等价（扫描、读值、应用三处都一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
