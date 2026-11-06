#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计项目里各语言的代码量：文件数 / 总行 / 有效行（去掉空行与纯注释行）"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {"__pycache__", "out", ".git", "node_modules"}
LANG = {
    ".py": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".html": "HTML/CSS/JS",
    ".ps1": "PowerShell",
    ".bat": "Batch",
    ".json": "JSON（数据）",
    ".md": "Markdown（文档）",
}

COMMENT = {
    "Python": (("#",), ('"""', "'''")),
    "JavaScript": (("//",), ("/*", "*/")),
    "PowerShell": (("#",), ("<#", "#>")),
    "Batch": (("rem ", "::"), ()),
}


def count(path, lang):
    try:
        text = open(path, encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        return 0, 0
    lines = text.splitlines()
    total = len(lines)
    one = COMMENT.get(lang, ((), ()))[0]
    eff = 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if any(s.startswith(c) for c in one):
            continue
        eff += 1
    return total, eff


def main():
    agg = {}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            lang = LANG.get(ext)
            if not lang:
                continue
            p = os.path.join(base, f)
            rel = os.path.relpath(p, ROOT)
            t, e = count(p, lang)
            a = agg.setdefault(lang, {"files": 0, "total": 0, "eff": 0, "list": []})
            a["files"] += 1
            a["total"] += t
            a["eff"] += e
            a["list"].append((rel, t, e))

    order = ["Python", "JavaScript", "HTML/CSS/JS", "PowerShell", "Batch", "JSON（数据）", "Markdown（文档）"]
    print(f"{'语言':<14}{'文件':>5}{'总行':>8}{'有效行':>8}")
    print("-" * 38)
    for lang in order:
        a = agg.get(lang)
        if not a:
            continue
        print(f"{lang:<14}{a['files']:>5}{a['total']:>8}{a['eff']:>8}")
    print("-" * 38)

    code_langs = ["Python", "JavaScript", "HTML/CSS/JS", "PowerShell", "Batch"]
    tot = sum(agg[l]["eff"] for l in code_langs if l in agg)
    print("\n代码语言有效行合计：", tot)
    for l in code_langs:
        if l in agg:
            print(f"  {l:<14}{agg[l]['eff']:>6}  {agg[l]['eff'] / tot * 100:5.1f}%")

    # Java
    java = 0
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        java += sum(1 for f in files if f.lower().endswith(".java"))
    print(f"\nJava 文件数：{java}")

    # 前端文件里 script 块的体量
    html = os.path.join(ROOT, "web", "app.html")
    if os.path.isfile(html):
        t = open(html, encoding="utf-8").read()
        m = re.search(r"<script>(.*)</script>", t, re.S)
        if m:
            js = len(m.group(1).splitlines())
            print(f"web/app.html 里 <script> 占 {js} 行，其余 {len(t.splitlines()) - js} 行是 HTML+CSS")


if __name__ == "__main__":
    main()
