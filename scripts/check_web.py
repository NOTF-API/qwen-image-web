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
    print("语法检查请另行执行: node --check <--dump 出来的文件>")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
