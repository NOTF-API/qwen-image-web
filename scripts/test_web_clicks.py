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
    # 渲染用的纯函数段: 从 paramsLine 起(含 statusBadge/rowActions/outputsCell/
    # progressCell/rowHTML), 到 renderList 前。
    fns = js[js.index("function paramsLine"):js.index("function renderList")]
    # 点击处理器
    start = js.index('$("list").addEventListener("click"')
    end = js.index('$("selectAll").addEventListener')
    handler = js[start:end]
    # 图片查看器整段(常量 + 函数 + 事件绑定)
    vstart = js.index("const viewer = {")
    vend = js.index("// 编辑弹窗")
    viewer = js[vstart:vend]
    return fns + "\n" + handler + "\n" + viewer


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
          const m = sel.match(/^([a-z]+)(?:\[([a-z-]+)\])?$/i);
          if (m && node.tagName === m[1].toUpperCase()) {
            if (!m[2]) return node;
            // 支持 [data-view] 这类属性选择器(dataset 用驼峰键)
            const key = m[2].replace(/^data-/, "").replace(/-([a-z])/g, (s, c) => c.toUpperCase());
            if (node.dataset && node.dataset[key] !== undefined) return node;
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
// rowHTML 依赖的小工具(与页面同语义)
function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function fmtTime(ts) { return ts ? "12-31 08:00" : ""; }
function fmtDur(sec) { return sec == null ? "" : sec.toFixed(1) + "s"; }
const STATUS_LABEL = { pending: "待开始", running: "生成中", canceling: "取消中",
  done: "已完成", failed: "失败", canceled: "已取消" };

// 行结构: .task-row > [.pick, button[data-act], a > img, .clip-title, .outputs]
const handlers = [];
const listEl = {
  addEventListener(type, fn) { if (type === "click") handlers.push(fn); },
};
const clickHandlers = [];

function makeStub(id) {
  const el = {
    id, textContent: "", innerHTML: "", src: "", hidden: false, open: false, title: "",
    children: [], attrs: {}, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(type, fn) { (el._listeners[type] ||= []).push(fn); },
    _listeners: {},
    setAttribute(k, v) { el.attrs[k] = v; if (k === "src") el.src = v; },
    getAttribute(k) { return el.attrs[k]; },
    showModal() { el.open = true; },
    // 真实浏览器里 dialog.close() 会派发 close 事件(Esc/按钮关闭同理),
    // 页面正是靠它做清理, 所以这里必须一起派发。
    close() {
      el.open = false;
      (el._listeners.close || []).forEach((fn) => fn({ type: "close" }));
    },
    removeAttribute(k) { delete el.attrs[k]; if (k === "src") el.src = ""; },
  };
  return el;
}
const byId = {};
function stub(id) { return (byId[id] ||= makeStub(id)); }

// 结果列缩览图(带 data-view), 用于验证点击后走查看器而不是跳转/弹窗
const thumbLink = makeEl({ tag: "a", dataset: { view: "T9", index: "1" } });
const thumbImg = makeEl({ tag: "img" });
thumbLink.appendChild(thumbImg);

const $ = (id) => (id === "list" ? listEl : stub(id));
// 查看器会往 document 上挂 ← → 快捷键
const documentKeyHandlers = [];
const document = {
  addEventListener(type, fn) { if (type === "keydown") documentKeyHandlers.push(fn); },
};

__HANDLER__

function buildRow(withOutputs) {
  const row = makeEl({ class: "task-row", dataset: { id: "T1" } });
  const pick = makeEl({ tag: "input", class: "pick", dataset: { id: "T1" } });
  const btn = makeEl({ tag: "button", class: "icon-btn regen", dataset: { act: "retry", id: "T1" } });
  const link = makeEl({ tag: "a", class: "" });
  const img = makeEl({ tag: "img" });
  const title = makeEl({ class: "clip-title" });
  const outCell = makeEl({ class: "cell" });
  let placeholder = null;
  if (withOutputs) {
    const outputs = makeEl({ class: "outputs" });
    outputs.appendChild(link);
    link.appendChild(img);
    outCell.appendChild(outputs);
  } else {
    placeholder = makeEl({ class: "out-placeholder" });
    outCell.appendChild(placeholder);
  }
  row.appendChild(pick);
  row.appendChild(btn);
  row.appendChild(title);
  row.appendChild(outCell);
  return { row, pick, btn, img, link, title, outCell, placeholder };
}

function click(el) {
  called.openEditor.length = 0;
  called.taskAction.length = 0;
  listEl._event = { target: el };
  handlers.forEach((fn) => fn({ target: el }));
}

const parts = buildRow(true);
const empty = buildRow(false);
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
// 结果列: 有图时图片链接在上面已测; 未出图的占位符属于行内, 点它应打开编辑窗
click(empty.placeholder);
results.clickPlaceholder = called.openEditor.length === 1;
click(empty.outCell);
results.clickEmptyCell = called.openEditor.length === 1;

// ---- rowHTML 的结果列: 有图 -> 图片链接; 无图 -> 占位符, 且列数与表头一致 ----
const doneTask = {
  id: "T9", seq: 9, status: "done", staged: false, kind: "generation",
  prompt: "a red cube", negative_prompt: null, refs: [], origin: null,
  created_at: 1789000000, error: null, warnings: [],
  progress: { step: 4, total: 4 }, queue_position: null,
  params: { width: 512, height: 512, steps: 4, n: 1, seed: 7, true_cfg_scale: 1 },
  usage: { elapsed_sec: 11, queue_sec: 0, vram_peak_mb: 6400 },
  outputs: [{ url: "/outputs/a.png", width: 512, height: 512, seed: 7 }],
};
const pendingTask = Object.assign({}, doneTask, {
  id: "T8", seq: 8, status: "pending", staged: true, outputs: [],
  usage: null, queue_position: 1,
});
const doneHTML = rowHTML(doneTask);
const pendingHTML = rowHTML(pendingTask);
results.renderDoneHasThumb = doneHTML.includes('href="/outputs/a.png"')
  && doneHTML.includes("draggable=\"false\"") && doneHTML.includes("data-view=\"T9\"");
results.renderDoneNoNewTab = !doneHTML.includes("target=\"_blank\"");
results.renderPendingHasPlaceholder = pendingHTML.includes("out-placeholder")
  && !pendingHTML.includes("<img");
// 结果列在参数列之后、进度列之前(用顶层单元格顺序断言)
results.renderColumnOrder = /clip-info">[\s\S]*clip-meta">[\s\S]*class="(?:outputs|out-placeholder)"[\s\S]*class="cell">[\s\S]*data-act="retry"/
  .test(doneHTML) && /class="(?:outputs|out-placeholder)"[\s\S]*class="cell">[\s\S]*data-act="retry"/
  .test(pendingHTML);

// ---- 图片查看器 ----
// 多图任务(3 张)用于验证横向列表与切换
const multi = Object.assign({}, doneTask, {
  id: "T10", seq: 10,
  outputs: [
    { url: "/outputs/a.png", width: 512, height: 512, seed: 1, steps: 4 },
    { url: "/outputs/b.png", width: 512, height: 512, seed: 2, steps: 4 },
    { url: "/outputs/c.png", width: 512, height: 512, seed: 3, steps: 4 },
  ],
});

// 点缩览图: 拦截默认跳转, 并打开当前页查看器(先重置状态, 避免依赖前面用例)
results.thumbOpensViewer = (() => {
  state.tasks = [doneTask];
  called.openEditor.length = 0;
  let prevented = false;
  let idAtClick = null;
  listEl._event = { target: thumbImg };
  handlers.forEach((fn) => fn({
    target: thumbImg,
    preventDefault() {
      prevented = true;
      idAtClick = state.tasks[0] && state.tasks[0].id;
    },
  }));
  return prevented && idAtClick === "T9"
    && called.openEditor.length === 0
    && $("viewerImage").src === "/outputs/a.png"
    && $("viewer").open === true;
})();
results.thumbViewerTitle = $("viewerTitle").textContent.includes("#9")
  && $("viewerTitle").textContent.includes("a red cube")
  && !$("viewerTitle").textContent.includes("#10");
results.thumbPicksClickedIndex = (() => {
  state.tasks = [multi];
  openViewer("T10", 1);
  const ok = $("viewerCount").textContent === "2 / 3"
    && $("viewerImage").src === "/outputs/b.png";
  closeViewer();
  return ok;
})();

// 多图任务: 显示横向列表与左右切换
state.tasks = [multi];
openViewer("T10", 0);
results.multiShowsStrip = $("viewerStrip").hidden === false
  && ($("viewerStrip").innerHTML.match(/<img /g) || []).length === 3
  && $("viewerPrev").hidden === false && $("viewerNext").hidden === false;
results.multiCountText = $("viewerCount").textContent === "1 / 3"
  && $("viewerMeta").textContent.includes("seed 1");
stepViewer(1);
results.multiNext = $("viewerImage").src === "/outputs/b.png"
  && $("viewerCount").textContent === "2 / 3";
stepViewer(-1);
stepViewer(-1);
results.multiWrap = $("viewerImage").src === "/outputs/c.png"
  && $("viewerCount").textContent === "3 / 3";
results.multiDownloadName = $("viewerDownload").getAttribute("download") === "c.png";
// 关闭按钮走的是 form method="dialog" 的原生提交; 这里直接触发 close 事件路径
closeViewer();
results.closeClears = $("viewer").open === false && !$("viewerImage").src
  && $("viewerStrip").hidden === true && $("viewerStrip").innerHTML === "";
// 关闭按钮的点击处理器本身也要能关(handler 已注册)
$("viewerClose")._listeners.click[0]();
results.closeButtonWorks = $("viewer").open === false;

// 单图任务: 隐藏列表与切换按钮
state.tasks = [doneTask];
openViewer("T9", 0);
results.singleHidesStrip = $("viewerStrip").hidden === true
  && $("viewerPrev").hidden === true && $("viewerNext").hidden === true;
results.singleCountText = $("viewerCount").textContent === "1 / 1";

// 键盘: 查看器打开时 ← → 切换(单图应无变化, 多图才生效)
state.tasks = [multi];
openViewer("T10", 0);
documentKeyHandlers.forEach((fn) => fn({ key: "ArrowRight", preventDefault() {} }));
results.keyboardNext = $("viewerImage").src === "/outputs/b.png";
closeViewer();
results.keyboardIgnoredWhenClosed = (() => {
  const before = $("viewerImage").src;
  documentKeyHandlers.forEach((fn) => fn({ key: "ArrowRight", preventDefault() {} }));
  return $("viewerImage").src === before;
})();

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
        check(res["clickPlaceholder"], "点结果列的占位符打开编辑窗")
        check(res["clickEmptyCell"], "点结果列空白处打开编辑窗")
        check(res["renderDoneHasThumb"], "已出图任务在结果列渲染可点击的缩览图")
        check(res["renderDoneNoNewTab"], "缩览图不再 target=_blank(改为本页查看)")
        check(res["renderPendingHasPlaceholder"], "未出图任务在结果列渲染占位符")
        check(res["renderColumnOrder"], "结果列位于参数列与进度列之间")
        check(res["thumbOpensViewer"], "点缩览图打开当前页查看器, 不跳转不弹编辑窗")
        check(res["thumbViewerTitle"], "查看器标题显示任务号与提示词")
        check(res["thumbPicksClickedIndex"], "点第 2 张缩览图直接显示第 2 张")
        check(res["multiShowsStrip"], "多图任务显示横向图片列表与切换按钮")
        check(res["multiCountText"], "查看器显示 1/3 与尺寸/seed 信息")
        check(res["multiNext"], "下一张切换到第 2 张")
        check(res["multiWrap"], "上一张可循环到第 3 张")
        check(res["multiDownloadName"], "下载按钮带上正确的文件名")
        check(res["closeClears"], "关闭查看器后清空图片与列表状态")
        check(res["closeButtonWorks"], "「关闭」按钮可以关闭查看器")
        check(res["singleHidesStrip"], "单图任务隐藏列表与切换按钮")
        check(res["singleCountText"], "单图任务显示 1/1")
        check(res["keyboardNext"], "← → 键可切换图片")
        check(res["keyboardIgnoredWhenClosed"], "查看器关闭时不响应方向键")
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
