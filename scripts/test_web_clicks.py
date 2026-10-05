# -*- coding: utf-8 -*-
"""验证任务行的点击语义: 点结果缩览图只打开图片, 不会弹出编辑弹窗。

用 Node 跑真实的 index.html 内联脚本片段, 手工搭一个最小 DOM 模拟, 断言:
  1. 点击结果 <a>/<img>          -> 不调用 openEditor, 链接默认行为保留
  2. 点击复选框 .pick            -> 只切换选中, 不弹窗
  3. 点击行内按钮 button[data-act]-> 只触发该动作, 不弹窗
  4. 点击行内其它区域(提示词等)   -> 才打开编辑弹窗
  5. 点击行内空白容器 .outputs    -> 打开编辑弹窗(没有任何图片时)

用法: python scripts/test_web_clicks.py     (需要 node)
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HTML = BASE / "static" / "index.html"
NODE = Path(r"C:\Users\NOTF\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe")

FAILED = []


def check(cond, label, extra=""):
    print(("  [ok] " if cond else "  [FAIL] ") + label + ("" if cond else f"  {extra}"))
    if not cond:
        FAILED.append(label)


def extract_js():
    html = HTML.read_text(encoding="utf-8")
    js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    # 取出 list 的点击处理器那一段(从 $("list").addEventListener("click" 到对应的结尾) )
    start = js.index('$("list").addEventListener("click"')
    end = js.index("$(\"selectAll\").addEventListener")
    return js[start:end]


HARNESS = r"""
// ---- 最小 DOM 模拟 ----
function makeEl(attrs) {
  const cls = (attrs.class || "").split(" ").filter(Boolean);
  const el = {
    tagName: (attrs.tag || "DIV").toUpperCase(),
    dataset: attrs.dataset || {},
    checked: !!attrs.checked,
    _classes: new Set(cls),
    _parent: null,
    _listeners: {},
    get className() { return [...this._classes].join(" "); },
    classList: {
      add: (c) => el._classes.add(c),
      remove: (c) => el._classes.delete(c),
      contains: (c) => el._classes.has(c),
      toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
    },
    appendChild(child) { child._parent = el; return child; },
    addEventListener(type, fn) { (el._listeners[type] ||= []).push(fn); },
    closest(selector) {
      const sels = selector.split(",").map((s) => s.trim());
      let node = el;
      while (node) {
        for (const sel of sels) {
          if (sel.startsWith(".") && node._classes.has(sel.slice(1))) return node;
          if (sel === "+ '*'") continue;
          const m = sel.match(/^([a-z]+)(?:\[([a-z-]+)\])?$/i);
          if (m && node.tagName === m[1].toUpperCase()) {
            if (!m[2] || node.dataset[m[2].replace("data-", "")] !== undefined) return node;
          }
        }
        node = node._parent;
      }
      return null;
    },
  };
  return el;
}

const called = { openEditor: [], taskAction: [], renderList: 0, selected: new Set() };
const state = { selected: new Set() };
function openEditor(id) { called.openEditor.push(id); }
function taskAction(id, act) { called.taskAction.push([id, act]); }
function renderList() { called.renderList++; }

// 行结构: .task-row > [.pick, button[data-act], a > img, .clip-title, .outputs]
const handlers = [];
const listEl = {
  addEventListener(type, fn) { if (type === "click") handlers.push(fn); },
};
const $ = (id) => (id === "list" ? listEl : {});

__HANDLER__

function buildRow() {
  const row = makeEl({ class: "task-row", dataset: { id: "T1" } });
  const pick = makeEl({ tag: "input", class: "pick", dataset: { id: "T1" } });
  const btn = makeEl({ tag: "button", class: "icon-btn regen", dataset: { act: "retry", id: "T1" } });
  const link = makeEl({ tag: "a", class: "" });
  const img = makeEl({ tag: "img" });
  const title = makeEl({ class: "clip-title" });
  const outputs = makeEl({ class: "outputs" });
  const more = makeEl({ class: "more" });
  row.appendChild(pick);
  row.appendChild(btn);
  link.appendChild(img);
  row.appendChild(link);
  row.appendChild(title);
  outputs.appendChild(more);
  row.appendChild(outputs);
  return { row, pick, btn, img, link, title, outputs, more };
}

function click(el) {
  called.openEditor.length = 0;
  called.taskAction.length = 0;
  listEl._event = { target: el };
  handlers.forEach((fn) => fn({ target: el }));
}

const parts = buildRow();
const results = {};

click(parts.img);
results.clickImage = called.openEditor.length === 0 && called.taskAction.length === 0;
click(parts.link);
results.clickLink = called.openEditor.length === 0;
click(parts.pick);
results.clickCheckbox = called.openEditor.length === 0 && called.renderList > 0;
click(parts.btn);
results.clickButton = called.openEditor.length === 0
  && called.taskAction.length === 1 && called.taskAction[0][1] === "retry";
click(parts.title);
results.clickTitle = called.openEditor.length === 1 && called.openEditor[0] === "T1";
click(parts.more);
results.clickOutputsArea = called.openEditor.length === 1;

console.log(JSON.stringify(results));
"""


def main():
    js_handler = extract_js()
    harness = HARNESS.replace("__HANDLER__", js_handler)
    tmp = Path(tempfile.mkdtemp(prefix="qwen-click-test-"))
    try:
        f = tmp / "click_test.js"
        f.write_text(harness, encoding="utf-8")
        node = str(NODE) if NODE.is_file() else shutil.which("node")
        if not node:
            print("[SKIP] 找不到 node, 无法执行 DOM 模拟")
            return 0
        proc = subprocess.run([node, str(f)], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        if proc.returncode != 0:
            print("[FAIL] node 执行失败:\n" + (proc.stderr or "")[-2000:])
            return 1
        line = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")][-1]
        res = json.loads(line)
        check(res["clickImage"], "点结果图片只打开图片, 不弹编辑窗")
        check(res["clickLink"], "点结果链接只打开图片, 不弹编辑窗")
        check(res["clickCheckbox"], "点复选框只切换选中")
        check(res["clickButton"], "点行内按钮只触发该动作")
        check(res["clickTitle"], "点提示词区域才打开编辑窗")
        check(res["clickOutputsArea"], "点结果区域空白处打开编辑窗")
        print()
        if FAILED:
            print(f"失败 {len(FAILED)} 项: {FAILED}")
            return 1
        print("全部通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
