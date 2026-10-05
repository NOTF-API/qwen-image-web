# -*- coding: utf-8 -*-
"""抽取 static/index.html 里的内联脚本做静态检查(语法/元素 id 引用一致性)。

用法: python scripts/check_web.py            # 只做检查
      python scripts/check_web.py --dump out.js
需要 node 时由调用方另行执行 node --check。
"""
import argparse
import re
import sys
from pathlib import Path

HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default="")
    args = ap.parse_args()

    html = HTML.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    if len(scripts) != 1:
        print(f"[FAIL] 期望 1 段内联脚本, 实际 {len(scripts)}")
        return 1
    js = scripts[0]
    if args.dump:
        Path(args.dump).write_text(js, encoding="utf-8")

    ids = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
    used = set(re.findall(r'\$\("([A-Za-z0-9_\-]+)"\)', js))
    missing = sorted(used - ids)
    unused = sorted(ids - used)

    print(f"内联脚本 {len(js)} 字符, {len(ids)} 个 id, 引用 {len(used)} 个")
    ok = True
    if missing:
        print(f"[FAIL] JS 引用了不存在的 id: {missing}")
        ok = False
    else:
        print("[ok] JS 引用的 id 全部存在")
    if unused:
        print(f"[提示] 未被 JS 直接引用的 id: {unused}")

    # 常见低级错误: 关键接口路径有没有写错
    for path in ["/v1/images/generations", "/v1/images/edits/json", "/api/tasks",
                 "/api/queue", "/api/queue/auto-start", "/health"]:
        if path not in js:
            print(f"[FAIL] 脚本里没有用到 {path}")
            ok = False
    print("[ok] 关键接口路径均已出现" if ok else "[FAIL] 接口路径缺失")

    # 表格列对齐: grid-template-columns 的列数 == 表头格数 == 行模板产生的格数
    m = re.search(r"\.list-header,\s*\.task-row\s*\{[^}]*grid-template-columns:\s*"
                  r"((?:[^;]|\n)+?);", html, re.S)
    if not m:
        print("[FAIL] 找不到 .list-header/.task-row 的 grid-template-columns")
        ok = False
    else:
        spec = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
        # 按顶层空白切分: minmax(150px, 1fr) 里的空格不能算成两列
        toks, depth, buf = [], 0, ""
        for ch in spec.strip():
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch.isspace() and depth == 0:
                if buf:
                    toks.append(buf)
                    buf = ""
            else:
                buf += ch
        if buf:
            toks.append(buf)
        cols = len(toks)
        header = re.search(r'<div class="list-header">\n(.*?)\n\s*</div>\s*\n\s*<div class="list"',
                           html, re.S)
        heads = len(re.findall(r'<div(?: class="[^"]*")?>', header.group(1))) if header else -1
        row = re.search(r"function rowHTML\(task\) \{(.*?)\n    \}", js, re.S)
        body = row.group(1) if row else ""
        tpl = re.search(r'return `<div class="\$\{classes\.join\(" "\)\}"[^>]*>(.*?)</div>`;',
                        body, re.S)
        # 模板里每个顶层单元格都顶格缩进 8 空格, 内部元素缩进更多
        tops = re.findall(r"\n {8}<(div|input)", tpl.group(1)) if tpl else []
        cells = len(tops)
        print(f"[{'ok' if cols == heads == cells else 'FAIL'}] 列对齐: "
              f"grid {cols} 列 / 表头 {heads} 格 / 行 {cells} 格")
        if not (cols == heads == cells):
            ok = False

    print("语法检查请另行执行: node --check <--dump 出来的文件>")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
