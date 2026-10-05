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

    # dialog 默认隐藏: 解析 CSS 层叠, 确认未打开时 display 最终是 none。
    # (曾经给 #viewer 设了 display:flex, 覆盖掉浏览器默认的 dialog{display:none},
    #  导致页面一加载图片查看器就常驻显示 —— 这个检查就是为了不再犯。)
    rules = []          # (selector, [declarations], 顺序)
    style_blocks = re.findall(r"<style>(.*?)</style>", html, re.S)
    if not style_blocks:
        print("[FAIL] 页面里没有 <style> 块")
        return 1
    # 先去掉注释, 否则块前的注释会被当成选择器的一部分
    css = re.sub(r"/\*.*?\*/", "", style_blocks[0], flags=re.S)
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors, body = block.group(1), block.group(2)
        selectors = selectors.split("}")[-1].strip()
        if not selectors or selectors.startswith("@"):
            continue
        decls = [d.strip() for d in body.split(";") if d.strip()]
        for sel in selectors.split(","):
            sel = " ".join(sel.split())
            if not sel:
                continue
            rules.append((sel, decls, len(rules)))

    def display_for(dialog_id, with_open_attr=False):
        """作者样式里, 该 dialog 在指定状态下命中的最终 display(没有命中则 None)。

        基座是浏览器默认样式 ``dialog { display: none }``(未带 open 属性时生效):
        - 作者样式没有声明 display      -> 沿用 UA 的 none, 页面加载时隐藏(正确)
        - 作者样式声明了 display:flex   -> 覆盖 UA, 页面一加载就常驻显示(错误)
        所以下面只看「作者样式是否会覆盖」。
        """
        ident = f"#{dialog_id}"
        applicable = {f"{ident}[open]": (1, 1, 0), f"{ident}:not([open])": (1, 1, 0),
                      ident: (1, 0, 0), "dialog": (0, 0, 1)}
        winner, win_key = None, (-1, -1, -1)
        for sel, decls, order in rules:
            spec = applicable.get(sel)
            if spec is None:
                continue
            if sel == f"{ident}[open]" and not with_open_attr:
                continue
            if sel == f"{ident}:not([open])" and with_open_attr:
                continue
            for d in decls:
                if d.lower().startswith("display"):
                    value = d.split(":", 1)[1].strip()
                    important = 1 if "!important" in value.lower() else 0
                    value = value.replace("!important", "").strip()
                    key = (important,) + spec + (order,)
                    if key >= win_key:
                        win_key, winner = key, value
        return winner

    for dialog_id in ("viewer", "editDialog"):
        author_closed = display_for(dialog_id)
        author_open = display_for(dialog_id, with_open_attr=True)
        closed_ok = author_closed is None or "none" in author_closed.replace(" ", "").lower()
        opened_ok = author_open is None or "none" not in author_open.replace(" ", "").lower()
        print(f"[{'ok' if closed_ok else 'FAIL'}] {dialog_id} 未打开: "
              f"作者样式 display={author_closed or '(未声明, 沿用浏览器默认 none)'}"
              + ("" if closed_ok else " —— 覆盖了浏览器默认值, 页面加载就常驻显示"))
        print(f"[{'ok' if opened_ok else 'FAIL'}] {dialog_id} 打开时: "
              f"作者样式 display={author_open or '(未声明, 用浏览器默认)'}"
              + ("" if opened_ok else " —— 打开后却是 none, 会看不见"))
        if not (closed_ok and opened_ok):
            ok = False

    print("语法检查请另行执行: node --check <--dump 出来的文件>")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
