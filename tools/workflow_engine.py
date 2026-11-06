#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workflow_engine.py — 从任意 ComfyUI 工作流自动推导可调参数，并支持应用

核心是 scan_workflow()：不依赖任何手写的映射文件，直接从工作流结构里推导出
「哪些节点输入可以做成表单」。这样用户导入自己的 workflow.json 就能直接出界面。

推导规则：
  1. 提示词：从每个 KSampler 的 positive/negative 出发向上游 BFS，
     按打分挑出最像提示词的字符串输入（节点标题/类名/字段名/是否非空 都参与打分）。
     会跳过 LoRA 加载器（它的 text 是一串 <lora:..>，不是提示词）。
  2. 模型：unet_name / ckpt_name / clip_name / clip_name1 / clip_name2 / vae_name
  3. LoRA：
     - LoraManager 类节点（有 loras 字段）→ 结构化列表编辑器
     - 普通 LoraLoader → lora_name 下拉 + strength_model
  4. 采样器：每个 KSampler / KSamplerAdvanced / SamplerCustom 各成一组，
     组内把它所有标量输入都暴露出来（steps/cfg/denoise/sampler_name/scheduler/…）。
     seed 若连到上游节点（比如 rgthree 的 Seed 节点），会跟到上游去改真正的那个。
  5. 尺寸：找带 width/height 的 latent 节点；若宽高连到 ResolutionPreset 这类
     带 resolution_json 的节点，就用适配器整体改写。
  6. 输出：SaveImage 的 filename_prefix

用法：
  python workflow_engine.py scan                      # 看内置工作流推导出什么
  python workflow_engine.py scan -w 自定义.json --json
  python workflow_engine.py read
  python workflow_engine.py apply --set 1134.steps=24 --set 1160.positive="1girl"
  python workflow_engine.py run   --set 1134.steps=24 --set 1160.positive="1girl"
  python workflow_engine.py selftest
"""

import argparse
import copy
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_WF = os.path.join(REPO, "workflow", "moss.json")

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".sft", ".gguf", ".bin")

SAMPLER_CLASSES = ("KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced")
SAMPLER_LABELS = {
    "steps": ("步数", 1, 150, 1),
    "cfg": ("CFG", 0, 30, 0.1),
    "denoise": ("降噪强度", 0, 1, 0.01),
    "sampler_name": ("采样器", None, None, None),
    "scheduler": ("调度器", None, None, None),
    "seed": ("种子", -1, 4294967295, 1),
    "noise_seed": ("噪声种子", -1, 4294967295, 1),
    "start_at_step": ("起始步", 0, 10000, 1),
    "end_at_step": ("结束步", 0, 10000, 1),
    "add_noise": ("加噪", None, None, None),
    "return_with_leftover_noise": ("保留剩余噪声", None, None, None),
}
MODEL_LABELS = {
    "unet_name": "UNET 模型",
    "ckpt_name": "Checkpoint",
    "clip_name": "文本编码器",
    "clip_name1": "文本编码器 1",
    "clip_name2": "文本编码器 2",
    "clip_name3": "文本编码器 3",
    "vae_name": "VAE",
    "lora_name": "LoRA",
}
MODEL_ORDER = ["unet_name", "ckpt_name", "clip_name", "clip_name1", "clip_name2",
               "clip_name3", "type", "vae_name", "lora_name"]
# 采样器里这几个字段的类型跟 JSON 字面量无关，必须固定
SAMPLER_KINDS = {"cfg": "float", "denoise": "float", "steps": "int", "seed": "int",
                 "noise_seed": "int", "start_at_step": "int", "end_at_step": "int"}
PROMPT_FIELD_CANDIDATES = ("text", "positive", "prompt", "text_g", "text_l", "string")
PROMPT_CLASS_HINTS = ("cliptextencode", "easyp", "positive", "promptui", "prompt",
                      "textencode", "wildcard", "string")
# 这些字段是纯前端状态，绝不当作提示词
NEVER_PROMPT_FIELDS = ("temp_str", "orinalMessage", "random_template", "filename_prefix",
                       "打开提示词编辑器", "placeholder")
LORA_TAG_ONLY = re.compile(r"^(\s*<lora:[^>]+>\s*)+$")


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------

def adapt_plain(v, current=None):
    return v


def adapt_int(v, current=None):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        raise ValueError(f"需要整数，收到 {v!r}")


def adapt_float(v, current=None):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        raise ValueError(f"需要小数，收到 {v!r}")


def adapt_bool(v, current=None):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def adapt_resolution_json(v, current=None):
    """'1024x1536' -> '{"version":1,"width":1024,"height":1536}'（字符串，保留其它键）"""
    base = {"version": 1}
    if isinstance(current, str):
        try:
            p = json.loads(current)
            if isinstance(p, dict):
                base = p
        except ValueError:
            pass
    if isinstance(v, dict):
        base.update(v)
    elif isinstance(v, str):
        m = re.match(r"^\s*(\d+)\s*[xX*×]\s*(\d+)\s*$", v)
        if not m:
            raise ValueError(f"分辨率格式应为 宽x高（如 1024x1536），收到 {v!r}")
        base["width"] = int(m.group(1))
        base["height"] = int(m.group(2))
    else:
        raise ValueError(f"分辨率值类型不支持：{type(v).__name__}")
    return json.dumps(base, separators=(",", ":"))


def as_lora_list(value):
    if isinstance(value, dict) and "__value__" in value:
        value = value["__value__"]
    if isinstance(value, str):
        out = []
        for m in re.finditer(r"<lora:([^:>]+):([0-9]*\.?[0-9]+)(?::([0-9]*\.?[0-9]+))?>", value):
            s = float(m.group(2))
            out.append({"name": m.group(1), "strength": s,
                        "clip_strength": float(m.group(3)) if m.group(3) else s,
                        "active": True})
        return out
    if not isinstance(value, list):
        raise ValueError(f"LoRA 列表格式不支持：{type(value).__name__}")
    out = []
    for it in value:
        if not isinstance(it, dict) or not it.get("name"):
            raise ValueError(f"LoRA 条目缺少 name：{it!r}")
        s = float(it.get("strength", 1.0))
        out.append({"name": str(it["name"]), "strength": s,
                    "clip_strength": float(it.get("clipStrength", it.get("strength", 1.0))),
                    "active": bool(it.get("active", True))})
    return out


def adapt_loras_widget(v, current=None):
    return {"__value__": [{
        "name": it["name"], "strength": f"{it['strength']:.2f}", "active": it["active"],
        "expanded": False, "clipStrength": f"{it['clip_strength']:.2f}", "locked": False,
    } for it in as_lora_list(v)]}


def adapt_lora_text(v, current=None):
    return " ".join(f"<lora:{it['name']}:{it['strength']:.2f}>"
                    for it in as_lora_list(v) if it["active"])


ADAPTERS = {
    "resolution_json": adapt_resolution_json,
    "loras_widget": adapt_loras_widget,
    "lora_text": adapt_lora_text,
    "int": adapt_int,
    "float": adapt_float,
    "bool": adapt_bool,
}
KIND_ADAPTER = {"int": adapt_int, "float": adapt_float, "bool": adapt_bool}


# ---------------------------------------------------------------------------
# 扫描：推导可调参数
# ---------------------------------------------------------------------------

def _is_lora_loader(node):
    ct = node.get("class_type", "")
    ins = node.get("inputs") or {}
    return ("loras" in ins) or ("lora" in ct.lower() and "withoutlora" not in ct.lower())


def _link_target(value):
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
        return str(value[0])
    return None


def _upstream_text(wf, sampler, which, used):
    """从 KSampler 的 positive/negative 向上游找最像提示词的字符串输入。"""
    start = _link_target((sampler.get("inputs") or {}).get(which))
    if not start:
        lit = (sampler.get("inputs") or {}).get(which)
        if isinstance(lit, str):
            return None  # 采样器上直接挂着字符串，极少见，不处理
        return None

    seen, candidates = set(), []
    queue = [(start, 0)]
    while queue and len(seen) < 80:
        nid, depth = queue.pop(0)
        if nid in seen or depth > 6 or not nid:
            continue
        seen.add(nid)
        node = wf.get(nid)
        if not node:
            continue
        if _is_lora_loader(node):
            continue                       # LoRA 节点的 text 是 <lora:..> 串，不是提示词
        ins = node.get("inputs") or {}
        title = str((node.get("_meta") or {}).get("title", ""))
        ct = str(node.get("class_type", "")).lower()
        for fname in PROMPT_FIELD_CANDIDATES:
            v = ins.get(fname)
            if not isinstance(v, str):
                continue
            if fname in NEVER_PROMPT_FIELDS:
                continue
            if LORA_TAG_ONLY.match(v):
                continue
            if (nid, fname) in used:
                continue
            score = 0
            if fname in ("text", "positive", "prompt"):
                score += 6
            if any(h in ct for h in PROMPT_CLASS_HINTS):
                score += 6
            if any(h in title for h in ("提示词", "正面", "负面", "prompt", "Prompt")):
                score += 4
            if v.strip():
                score += 3
            score += max(0, 3 - depth)
            candidates.append((score, depth, nid, fname, v, title, node.get("class_type")))
        # 继续向上游扩散：提示词常藏在 JoinString / 拼接 / 触发词节点后面
        for _v in ins.values():
            _nxt = _link_target(_v)
            if _nxt:
                queue.append((_nxt, depth + 1))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[0], c[1]))
    score, depth, nid, fname, v, title, cls = candidates[0]
    return {"node": nid, "input": fname, "class_type": cls, "title": title,
            "value": v, "score": score}


def _resolve_scalar(wf, node_id, input_name, depth=0):
    """如果某个输入是连线，顺着找到上游真正的字面量（用于 sampler.seed 这类）。"""
    node = wf.get(str(node_id))
    if not node or depth > 4:
        return None
    v = (node.get("inputs") or {}).get(input_name)
    if _link_target(v):
        up_id = _link_target(v)
        up = wf.get(up_id)
        if not up:
            return None
        for fname, fv in (up.get("inputs") or {}).items():
            if isinstance(fv, (int, float, str, bool)) and not isinstance(fv, bool):
                if isinstance(fv, str) and not fv.strip():
                    continue
                return {"node": up_id, "input": fname, "value": fv,
                        "class_type": up.get("class_type"),
                        "title": (up.get("_meta") or {}).get("title", "")}
        return None
    if isinstance(v, (int, float, bool)) or (isinstance(v, str) and re.match(r"^-?\d+$", v.strip())):
        return {"node": str(node_id), "input": input_name, "value": v,
                "class_type": node.get("class_type"),
                "title": (node.get("_meta") or {}).get("title", "")}
    return None


def _humanize(name):
    return {"filename_prefix": "输出文件名前缀", "batch_size": "批量张数",
            "weight_dtype": "权重精度", "type": "编码器类型"}.get(name, name)


def _kind_of(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string"


def scan_workflow(wf, title="workflow"):
    """返回 {title, node_count, groups, warnings, stats}。"""
    warnings = []
    groups = []
    used = set()

    samplers = [(str(nid), n) for nid, n in wf.items()
                if n.get("class_type") in SAMPLER_CLASSES]
    samplers.sort(key=lambda x: (len(x[0]), x[0]))

    def mkfield(key, label, node, inp, kind, **kw):
        f = {"key": key, "label": label, "node": str(node), "input": inp, "kind": kind,
             "targets": [{"node": str(node), "input": inp}], **kw}
        return f

    # ---------- 1. 提示词 ----------
    prompt_fields = []
    if samplers:
        pos = _upstream_text(wf, samplers[0][1], "positive", used)
        if pos:
            used.add((pos["node"], pos["input"]))
            prompt_fields.append(mkfield(f'{pos["node"]}.{pos["input"]}', "正向提示词",
                                         pos["node"], pos["input"], "text", rows=4,
                                         multiline=True, value=pos["value"]))
        else:
            warnings.append("没找到正向提示词（positive 上游没有可编辑的字符串节点）")
        neg = _upstream_text(wf, samplers[0][1], "negative", used)
        if neg:
            used.add((neg["node"], neg["input"]))
            prompt_fields.append(mkfield(f'{neg["node"]}.{neg["input"]}', "负向提示词",
                                         neg["node"], neg["input"], "text", rows=3,
                                         multiline=True, value=neg["value"]))
        else:
            warnings.append("没找到负向提示词")
    else:
        warnings.append("工作流里没有 KSampler，提示词/采样参数推导跳过")

    if prompt_fields:
        groups.append({"id": "prompt", "title": "提示词", "fields": prompt_fields})

    # ---------- 2. 模型加载器 ----------
    model_fields = []
    for nid, node in sorted(wf.items(), key=lambda kv: kv[0]):
        ins = node.get("inputs") or {}
        for fname, label in MODEL_LABELS.items():
            v = ins.get(fname)
            if not isinstance(v, str) or not v:
                continue
            ct = node.get("class_type", "")
            if fname == "lora_name" and _is_lora_loader(node):
                continue                       # 单独在 LoRA 组里处理
            kind = "string"
            model_fields.append(mkfield(f"{nid}.{fname}", f"{label} · {ct}", nid, fname, kind,
                                        enum=True, enum_class=ct, enum_field=fname,
                                        value=v, class_type=ct))
        # CLIPLoader 的 type 是枚举，值得单独暴露
        if node.get("class_type") in ("CLIPLoader", "DualCLIPLoader", "TripleCLIPLoader") \
                and isinstance(ins.get("type"), str):
            model_fields.append(mkfield(f"{nid}.type", f"编码器类型 · {node['class_type']}",
                                        nid, "type", "string", enum=True,
                                        enum_class=node["class_type"], enum_field="type",
                                        value=ins["type"], class_type=node["class_type"]))
    if model_fields:
        model_fields.sort(key=lambda f: MODEL_ORDER.index(f["input"])
                          if f["input"] in MODEL_ORDER else 99)
        groups.append({"id": "model", "title": "模型", "fields": model_fields})

    # ---------- 3. LoRA ----------
    lora_fields = []
    for nid, node in sorted(wf.items(), key=lambda kv: kv[0]):
        ins = node.get("inputs") or {}
        ct = node.get("class_type", "")
        if "loras" in ins:
            cur = ins.get("loras")
            has_text = "text" in ins
            targets = [{"node": str(nid), "input": "loras", "adapter": "loras_widget"}]
            if has_text:
                targets.append({"node": str(nid), "input": "text", "adapter": "lora_text"})
            lora_fields.append({
                "key": f"{nid}.loras", "label": f"LoRA · {ct}", "node": str(nid),
                "input": "loras", "kind": "loras", "value": cur, "class_type": ct,
                "enum_class": "LoraLoader", "enum_field": "lora_name", "enum": True,
                "targets": targets,
            })
        elif ct in ("LoraLoader", "LoraLoaderModelOnly") and isinstance(ins.get("lora_name"), str):
            lora_fields.append(mkfield(f"{nid}.lora_name", f"LoRA · {ct}", nid, "lora_name",
                                       "string", enum=True, enum_class="LoraLoader",
                                       enum_field="lora_name", value=ins["lora_name"],
                                       class_type=ct))
            if isinstance(ins.get("strength_model"), (int, float)):
                lora_fields.append(mkfield(f"{nid}.strength_model", "LoRA 权重", nid,
                                           "strength_model", "float", min=-2, max=2, step=0.05,
                                           value=ins["strength_model"], class_type=ct))
    if lora_fields:
        groups.append({"id": "lora", "title": "LoRA", "fields": lora_fields})

    # ---------- 4. 每个采样器一组 ----------
    for idx, (nid, node) in enumerate(samplers, 1):
        title = (node.get("_meta") or {}).get("title") or node.get("class_type")
        fields = []
        for fname, (label, mn, mx, st) in SAMPLER_LABELS.items():
            raw = (node.get("inputs") or {}).get(fname)
            if raw is None:
                continue
            if "seed" in fname:
                src = _resolve_scalar(wf, nid, fname)
                if src:
                    key = f'{src["node"]}.{src["input"]}'
                    if (src["node"], src["input"]) in used or any(
                            f["key"] == key for f in fields):
                        continue
                    used.add((src["node"], src["input"]))
                    fields.append(mkfield(key, label, src["node"], src["input"], "int",
                                          min=mn, max=mx, step=st, random_if_negative=True,
                                          value=src["value"],
                                          class_type=src.get("class_type")))
                continue
            if fname in ("sampler_name", "scheduler"):
                fields.append(mkfield(f"{nid}.{fname}", label, nid, fname, "string",
                                      enum=True, enum_class=node.get("class_type"),
                                      enum_field=fname, value=raw, class_type=node.get("class_type")))
                continue
            if fname in ("add_noise", "return_with_leftover_noise"):
                fields.append(mkfield(f"{nid}.{fname}", label, nid, fname, "bool",
                                      value=raw, class_type=node.get("class_type")))
                continue
            kind = SAMPLER_KINDS.get(fname) or _kind_of(raw)
            if kind in ("int", "float"):
                fields.append(mkfield(f"{nid}.{fname}", label, nid, fname, kind,
                                      min=mn, max=mx, step=st, value=raw,
                                      class_type=node.get("class_type")))
        # 该采样器上其它标量输入也一并暴露（KSamplerAdvanced 之类会用到）
        for fname, raw in (node.get("inputs") or {}).items():
            if fname in SAMPLER_LABELS or _link_target(raw):
                continue
            if not isinstance(raw, (int, float, bool, str)) or isinstance(raw, bool):
                continue
            kind = SAMPLER_KINDS.get(fname) or _kind_of(raw)
            if kind not in ("int", "float", "string"):
                continue
            if any(f["input"] == fname for f in fields):
                continue
            fields.append(mkfield(f"{nid}.{fname}", _humanize(fname), nid, fname, kind,
                                  enum=True, enum_class=node.get("class_type"), enum_field=fname,
                                  value=raw, class_type=node.get("class_type")))
        groups.append({"id": f"sampler_{nid}", "title": f"{title} · #{nid}", "fields": fields})

    # ---------- 5. 尺寸 / 批量 ----------
    size_fields = []
    for nid, node in sorted(wf.items(), key=lambda kv: kv[0]):
        ct = node.get("class_type", "")
        ins = node.get("inputs") or {}
        if not ("Latent" in ct and "width" in ins and "height" in ins):
            continue
        w_link, h_link = _link_target(ins.get("width")), _link_target(ins.get("height"))
        if w_link and w_link == h_link:
            up = wf.get(w_link) or {}
            rj = (up.get("inputs") or {}).get("resolution_json")
            if isinstance(rj, str):
                size_fields.append({
                    "key": f"{w_link}.resolution_json", "label": "分辨率",
                    "node": w_link, "input": "resolution_json", "kind": "size",
                    "value": rj, "class_type": up.get("class_type"),
                    "presets": ["1024x1536", "1536x1024", "1024x1024", "832x1216",
                                "768x1024", "512x512"],
                    "targets": [{"node": w_link, "input": "resolution_json",
                                 "adapter": "resolution_json"}],
                })
                used.add((w_link, "resolution_json"))
            else:
                warnings.append(f"latent 节点的宽高连到 {up.get('class_type')}，但找不到 resolution_json")
        elif isinstance(ins.get("width"), int) and isinstance(ins.get("height"), int):
            size_fields.append(mkfield(f"{nid}.width", "宽度", nid, "width", "int",
                                       min=64, max=4096, step=64, value=ins["width"], class_type=ct))
            size_fields.append(mkfield(f"{nid}.height", "高度", nid, "height", "int",
                                       min=64, max=4096, step=64, value=ins["height"], class_type=ct))
        if isinstance(ins.get("batch_size"), int) and not any(
                f["input"] == "batch_size" for f in size_fields):
            size_fields.append(mkfield(f"{nid}.batch_size", "批量张数", nid, "batch_size", "int",
                                       min=1, max=16, step=1, value=ins["batch_size"], class_type=ct))
    if size_fields:
        groups.append({"id": "size", "title": "尺寸与批量", "fields": size_fields})

    # ---------- 6. 输出 ----------
    out_fields = []
    for nid, node in sorted(wf.items(), key=lambda kv: kv[0]):
        if node.get("class_type") in ("SaveImage", "Image Save", "SaveImageWebsocket"):
            v = (node.get("inputs") or {}).get("filename_prefix")
            if isinstance(v, str):
                out_fields.append(mkfield(f"{nid}.filename_prefix", "输出文件名前缀",
                                          nid, "filename_prefix", "string", value=v,
                                          class_type=node["class_type"]))
    if out_fields:
        groups.append({"id": "output", "title": "输出", "fields": out_fields})

    stats = {
        "samplers": len(samplers),
        "fields": sum(len(g["fields"]) for g in groups),
        "loras": sum(1 for g in groups if g["id"] == "lora" for f in g["fields"]
                     if f["kind"] == "loras"),
    }
    return {"title": title, "node_count": len(wf), "groups": groups,
            "warnings": warnings, "stats": stats}


def flatten_fields(spec):
    return [f for g in spec.get("groups", []) for f in g.get("fields", [])]


def find_field(spec, key):
    for f in flatten_fields(spec):
        if f["key"] == key:
            return f
    return None


# ---------------------------------------------------------------------------
# 读 / 写
# ---------------------------------------------------------------------------

def _read_field(wf, f):
    t = (f.get("targets") or [{}])[0]
    node = wf.get(str(t.get("node")))
    if not node:
        return None
    raw = (node.get("inputs") or {}).get(t.get("input"))
    ad = t.get("adapter")
    if ad == "resolution_json":
        try:
            d = json.loads(raw) if isinstance(raw, str) else (raw or {})
            return f"{int(d.get('width', 0))}x{int(d.get('height', 0))}"
        except (ValueError, TypeError):
            return raw
    if ad in ("loras_widget", "lora_text"):
        try:
            return [{"name": x["name"], "strength": x["strength"], "active": x["active"]}
                    for x in as_lora_list(raw)]
        except (ValueError, TypeError):
            return []
    if f.get("kind") == "int" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    if f.get("kind") == "float" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return raw


def read_params(wf, spec):
    return {f["key"]: _read_field(wf, f) for f in flatten_fields(spec)}


def random_seed():
    return random.randint(0, 2 ** 32 - 1)


def apply_params(wf, spec, values=None, strict=True):
    values = dict(values or {})
    fields = flatten_fields(spec)
    known = {f["key"] for f in fields}
    if strict:
        unknown = [k for k in values if k not in known]
        if unknown:
            sample = sorted(known)[:6]
            raise ValueError(f"没有这些参数：{unknown}；可用（部分）：{sample}")

    wf2 = copy.deepcopy(wf)
    changes, resolved = [], {}

    for f in fields:
        key = f["key"]
        value = values.get(key, _read_field(wf, f))
        if f.get("random_if_negative"):
            num = value if isinstance(value, (int, float)) else None
            if (num is not None and num < 0) or (isinstance(value, str) and value.strip() in ("", "-1")):
                value = random_seed()
        for t in (f.get("targets") or []):
            nid = str(t["node"])
            node = wf2.get(nid)
            if node is None:
                raise ValueError(f"参数 {key} 指向的节点 {nid} 不存在")
            inputs = node.setdefault("inputs", {})
            before = inputs.get(t["input"])
            ad = t.get("adapter")
            if ad:
                fn = ADAPTERS.get(ad)
                if fn is None:
                    raise ValueError(f"未知适配器 {ad}")
                after = fn(value, before)
            else:
                after = KIND_ADAPTER.get(f.get("kind"), adapt_plain)(value, before)
            inputs[t["input"]] = after
            changes.append({"key": key, "label": f.get("label", key), "node": nid,
                            "class_type": node.get("class_type"), "input": t["input"],
                            "adapter": ad or f.get("kind") or "plain",
                            "before": before, "after": after})
        resolved[key] = value
    return wf2, changes, resolved


# ---------------------------------------------------------------------------
# 模型校验
# ---------------------------------------------------------------------------

def _norm(name):
    n = (name or "").replace("\\", "/").split("/")[-1]
    low = n.lower()
    for ext in MODEL_EXTS:
        if low.endswith(ext):
            return n[: -len(ext)]
    return n


CLASS_TO_INVENTORY = {
    "CheckpointLoaderSimple": "checkpoints",
    "UNETLoader": "diffusion_models",
    "UnetLoaderGGUF": "diffusion_models",
    "CLIPLoader": "text_encoders",
    "DualCLIPLoader": "text_encoders",
    "VAELoader": "vae",
    "LoraLoader": "loras",
}


def collect_model_refs(wf):
    refs = []
    for nid, node in (wf or {}).items():
        ct = node.get("class_type", "")
        ins = node.get("inputs") or {}
        cat = CLASS_TO_INVENTORY.get(ct)
        if cat:
            for f in ("ckpt_name", "unet_name", "clip_name", "clip_name1", "clip_name2", "vae_name"):
                v = ins.get(f)
                if isinstance(v, str) and v:
                    refs.append({"node": str(nid), "class_type": ct, "kind": cat, "value": v})
        loras = ins.get("loras")
        if isinstance(loras, dict) and "__value__" in loras:
            for it in loras["__value__"]:
                if it.get("active"):
                    refs.append({"node": str(nid), "class_type": ct, "kind": "loras",
                                 "value": str(it.get("name"))})
        if ct in ("LoraLoader", "LoraLoaderModelOnly") and isinstance(ins.get("lora_name"), str):
            refs.append({"node": str(nid), "class_type": ct, "kind": "loras",
                         "value": ins["lora_name"]})
    return refs


def check_models(wf, inventory):
    missing, found = [], []
    pools = {k: {_norm(n) for n in v} for k, v in (inventory or {}).items()}
    for ref in collect_model_refs(wf):
        pool = (inventory or {}).get(ref["kind"])
        if pool is None:
            ref["status"] = "unknown_category"
            missing.append(ref)
        elif ref["value"] in pool or _norm(ref["value"]) in pools.get(ref["kind"], set()):
            found.append(ref)
        else:
            ref["status"] = "missing"
            missing.append(ref)
    return missing, found


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_workflow(path):
    wf = json.load(open(path, encoding="utf-8"))
    if isinstance(wf, dict) and "prompt" in wf and isinstance(wf["prompt"], dict):
        wf = wf["prompt"]           # 兼容 {"prompt": {...}} 包装
    if isinstance(wf, dict) and "nodes" in wf and isinstance(wf["nodes"], list):
        raise SystemExit("这是 ComfyUI 的 UI 格式工作流（含 nodes/links）。"
                         "请在 ComfyUI 里用「导出 (API)」重新导出。")
    return wf


def parse_sets(pairs):
    out = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"--set 格式应为 key=value，收到：{item}")
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def cmd_scan(args):
    wf = load_workflow(args.workflow)
    spec = scan_workflow(wf, os.path.basename(args.workflow))
    if args.json:
        print(json.dumps(spec, ensure_ascii=False, indent=2))
        return 0
    print(f"{spec['title']}   {spec['node_count']} 节点   推导出 {spec['stats']['fields']} 个参数"
          f"   采样器 {spec['stats']['samplers']} 个")
    print("-" * 74)
    for g in spec["groups"]:
        print(f"[{g['title']}]")
        for f in g["fields"]:
            extra = "  adapter=" + f["targets"][0]["adapter"] if f["targets"][0].get("adapter") else ""
            enum = "  (下拉)" if f.get("enum") else ""
            v = f.get("value")
            s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            s = s if len(s) <= 40 else s[:37] + "..."
            print(f"  {f['key']:<24} {f['kind']:<7} {f['label']:<22} {s}{enum}{extra}")
    if spec["warnings"]:
        print("-" * 74)
        for w in spec["warnings"]:
            print("  [WARN] " + w)
    return 0


def cmd_read(args):
    wf = load_workflow(args.workflow)
    print(json.dumps(read_params(wf, scan_workflow(wf)), ensure_ascii=False, indent=2))
    return 0


def cmd_apply(args):
    wf = load_workflow(args.workflow)
    spec = scan_workflow(wf)
    wf2, changes, _ = apply_params(wf, spec, parse_sets(args.set))
    print(f"{len(changes)} 处变更")
    print("-" * 74)
    for c in changes:
        b = json.dumps(c["before"], ensure_ascii=False)
        a = json.dumps(c["after"], ensure_ascii=False)
        if len(b) > 44:
            b = b[:41] + "..."
        if len(a) > 62:
            a = a[:59] + "..."
        print(f"{c['node']}.{c['input']:<16} {b}  ->  {a}")
    if args.out:
        json.dump(wf2, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n已写出 {args.out}")
    return 0


def cmd_run(args):
    import time
    import urllib.parse
    import comfy_smoke as cs

    wf = load_workflow(args.workflow)
    spec = scan_workflow(wf)
    wf2, changes, resolved = apply_params(wf, spec, parse_sets(args.set))
    print(f"{len(changes)} 处写入")
    print("-" * 74)

    base = args.base.rstrip("/")
    inv = cs.list_inventory(base, args.origin)
    missing, found = check_models(wf2, inv)
    print(f"模型引用 {len(found) + len(missing)}：确认 {len(found)}")
    if missing and not args.skip_check:
        for m in missing[:8]:
            print(f"  [MISSING] {m['class_type']} #{m['node']} -> {m['value']}")
        return 1

    t0 = time.time()
    st, hdrs, body = cs.http("POST", base + "/prompt",
                             {"prompt": wf2, "client_id": "engine-" + str(int(t0))},
                             origin=args.origin)
    if st != 200:
        print(f"[FAIL] POST /prompt -> {st}\n{body[:600].decode('utf-8', 'replace')}")
        return 1
    pid = json.loads(body)["prompt_id"]
    print(f"[ OK] 入队 {pid}  CORS {cs.cors_brief(hdrs)}")
    img, err = cs.wait_result(base, pid, args.origin, time.time() + args.timeout)
    if err:
        print(f"[FAIL] {err.get('node_type') or err.get('node_id')}: "
              f"{str(err.get('exception_message'))[:300]}")
        return 1
    print(f"[ OK] 出图 {time.time() - t0:.1f}s")
    q = urllib.parse.urlencode({"filename": img["filename"],
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")})
    st, _, data = cs.http("GET", f"{base}/view?{q}", origin=args.origin)
    os.makedirs(cs.OUT_DIR, exist_ok=True)
    path = os.path.join(cs.OUT_DIR, "wf_" + img["filename"].replace("/", "_"))
    open(path, "wb").write(data)
    print(f"[ OK] {len(data)/1024:.1f}KB  {path}")
    return 0


def cmd_selftest(args):
    ok = 0

    def ck(cond, name, detail=""):
        nonlocal ok
        assert cond, name + ("  " + detail if detail else "")
        ok += 1
        print(f"  [pass] {name}")

    wf = load_workflow(args.workflow)
    spec = scan_workflow(wf)
    keys = {f["key"] for f in flatten_fields(spec)}

    ck(spec["stats"]["samplers"] == 1, f"识别出 1 个采样器（{spec['stats']['samplers']}）")
    ck("1160.positive" in keys, "正向提示词推导到 1160.positive")
    ck("1153.positive" in keys, "负向提示词推导到 1153.positive")
    ck("1135.loras" in keys, "LoRA 列表推导到 1135.loras")
    ck("1122.unet_name" in keys and "770.clip_name" in keys and "769.vae_name" in keys,
       "三个加载器都推导出来")
    ck("770.type" in keys, "CLIP 编码器类型也暴露了")
    ck("1134.steps" in keys and "1134.cfg" in keys and "1134.denoise" in keys,
       "KSampler 的 steps/cfg/denoise")
    ck("1134.sampler_name" in keys and "1134.scheduler" in keys, "采样器/调度器")
    ck("784.seed" in keys, "种子跟到了上游的 784.seed 而不是 1134.seed")
    ck("1140.resolution_json" in keys, "分辨率推导到 ResolutionPreset")
    ck("1146.batch_size" in keys, "批量张数")
    ck("960.filename_prefix" in keys, "输出前缀")
    ck(len(keys) == 16, f"共 16 个参数（实际 {len(keys)}）")

    # 值读取
    params = read_params(wf, spec)
    ck(params["1160.positive"] == "", "正向提示词原值为空")
    ck(params["1134.steps"] == 50, "steps=50")
    ck(params["1140.resolution_json"] == "1024x1536", "分辨率 1024x1536")
    ck(len(params["1135.loras"]) == 13, "13 个 LoRA")
    ck(sum(1 for x in params["1135.loras"] if x["active"]) == 10, "其中 10 个启用")

    # 写入
    wf2, changes, resolved = apply_params(wf, spec, {
        "1160.positive": "1girl", "1153.positive": "bad", "1134.steps": "24",
        "1134.cfg": "3.0", "1134.sampler_name": "dpmpp_2m", "1134.scheduler": "beta",
        "1134.denoise": "0.9", "784.seed": "42", "1140.resolution_json": "768x1024",
        "1146.batch_size": "2", "960.filename_prefix": "T/x",
        "1122.unet_name": "U.safetensors", "770.clip_name": "C.safetensors",
        "770.type": "qwen_image", "769.vae_name": "V.safetensors",
        "1135.loras": [{"name": "A", "strength": 0.8, "active": True},
                       {"name": "B", "strength": 0.5, "active": False}],
    })
    ck(wf2["1160"]["inputs"]["positive"] == "1girl", "写正向提示词")
    ck(wf2["1134"]["inputs"]["steps"] == 24, "写 steps（int）")
    ck(wf2["1134"]["inputs"]["cfg"] == 3.0, "写 cfg（float）")
    ck(wf2["784"]["inputs"]["seed"] == 42, "写种子到 784")
    ck(json.loads(wf2["1140"]["inputs"]["resolution_json"])
       == {"version": 1, "width": 768, "height": 1024}, "写分辨率且保留 version")
    lw = wf2["1135"]["inputs"]["loras"]
    ck(lw["__value__"][0]["strength"] == "0.80" and lw["__value__"][1]["active"] is False,
       "写 loras（含两位小数与 active）")
    ck(wf2["1135"]["inputs"]["text"] == "<lora:A:0.80>", "同步 text 只含启用的")
    ck(wf["1160"]["inputs"]["positive"] == "", "原工作流未被就地修改")

    # 边界
    try:
        apply_params(wf, spec, {"nope": 1})
        ck(False, "未知参数应报错")
    except ValueError:
        ck(True, "未知参数报错")
    try:
        apply_params(wf, spec, {"1140.resolution_json": "abc"})
        ck(False, "非法分辨率应报错")
    except ValueError:
        ck(True, "非法分辨率报错")
    _, _, r = apply_params(wf, spec, {"784.seed": -1})
    ck(r["784.seed"] >= 0, "seed=-1 转随机")

    # 多采样器 & 极简工作流
    mini = {
        "1": {"class_type": "KSampler", "inputs": {"steps": 20, "cfg": 7, "denoise": 1,
              "sampler_name": "euler", "scheduler": "normal", "seed": 1,
              "model": ["2", 0], "positive": ["3", 0], "negative": ["4", 0],
              "latent_image": ["5", 0]}},
        "1b": {"class_type": "KSamplerAdvanced", "inputs": {"steps": 12, "cfg": 2, "seed": 3}},
        "2": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["2", 1]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "ugly", "clip": ["2", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 768,
              "batch_size": 1}},
        "6": {"class_type": "SaveImage", "inputs": {"filenames_prefix_typo": "x",
              "filename_prefix": "ComfyUI"}},
        "7": {"class_type": "LoraLoader", "inputs": {"lora_name": "a.safetensors",
              "strength_model": 0.7, "model": ["2", 0], "clip": ["2", 1]}},
    }
    s2 = scan_workflow(mini)
    k2 = {f["key"] for f in flatten_fields(s2)}
    ck(s2["stats"]["samplers"] == 2, "两个采样器各成一组")
    ck(any(g["id"] == "sampler_1b" for g in s2["groups"]), "第二组是 sampler_1b")
    ck("3.text" in k2 and "4.text" in k2, "标准 CLIPTextEncode 正负提示词")
    ck("5.width" in k2 and "5.height" in k2, "字面量宽高单独暴露")
    ck("2.ckpt_name" in k2, "CheckpointLoader")
    ck("7.lora_name" in k2 and "7.strength_model" in k2, "普通 LoraLoader 的两个字段")
    ck("1b.steps" in k2 and "1b.cfg" in k2, "第二个采样器自己的 steps/cfg")

    print(f"\n自检通过：{ok} 项")
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="工作流扫描 + 参数应用")
    ap.add_argument("-w", "--workflow", default=DEFAULT_WF, help="工作流 JSON 路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_scan)

    sub.add_parser("read").set_defaults(fn=cmd_read)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)

    s = sub.add_parser("apply")
    s.add_argument("--set", action="append", metavar="key=value")
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("run")
    s.add_argument("--set", action="append", metavar="key=value")
    s.add_argument("--base", default="http://127.0.0.1:8188")
    s.add_argument("--origin", default="http://127.0.0.1:5500")
    s.add_argument("--timeout", type=float, default=900.0)
    s.add_argument("--skip-check", action="store_true")
    s.set_defaults(fn=cmd_run)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
