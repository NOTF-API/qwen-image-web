# -*- coding: utf-8 -*-
"""任务队列页面的交互测试(Node + 极简 DOM)。

用真实 page 里的代码 + 真实渲染出的 HTML 断言, 而不是另写一份逻辑:
  * 点击语义: 行内只有三处可点 —— 最左复选框(只切换选中)、右侧操作按钮(只做该动作)、
    结果缩览图(本页看图); 点提示词/参数/进度/占位符/整行空白一律无操作,
    编辑窗只能由铅笔按钮 ✎ 打开
  * 固定顺序: 列表按任务号降序重排, 与状态/结束时间无关(取消、完成、重新生成都不换位)
  * 图片查看器: 本页查看(不开新标签页)、多图横向列表与切换、循环、下载文件名、
    关闭按钮与清理、单图隐藏列表、方向键
  * 标题栏 flex: 很长的提示词下标题省略、右侧按钮不被压扁(含布局数值模拟)

用法: venv\\Scripts\\python.exe scripts\\test_web_clicks.py     (需要 node)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:                                     # Windows 控制台默认 GBK, 标签里的 ✎ 会抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
HTML = BASE / "static" / "index.html"
# node 优先用 PATH 里的; 需要指定某个 node.exe 时设环境变量 NODE
NODE = Path(os.environ["NODE"]) if os.environ.get("NODE") else None

FAILED = []


def check(cond, label, extra=""):
    print(("  [ok] " if cond else "  [FAIL] ") + label + ("" if cond else f"  {extra}"))
    if not cond:
        FAILED.append(label)


# ============================================================ 从页面提取被测代码
def page_js():
    return re.search(r"<script>(.*?)</script>",
                     HTML.read_text(encoding="utf-8"), re.S).group(1)


def extract_js():
    js = page_js()
    # 渲染纯函数: paramsLine ... (含 statusBadge/rowActions/outputsCell/progressCell/rowHTML)
    fns = js[js.index("function paramsLine"):js.index("function renderList")]
    # 固定顺序(纯排序函数)
    order = js[js.index("function sortTasks"):js.index("function paramsLine")]
    # 分辨率解析(纯函数: 与服务端 resolve_size 同一套规则)
    res = js[js.index("const RES = {"):js.index("// 新建任务")]
    # 行内动作分发(taskAction 少一个 "edit" 分支曾是"铅笔按钮点了没反应"的根因)
    action = js[js.index("async function taskAction"):
                js.index('$("list").addEventListener("click"')]
    # 列表点击处理器
    handler = js[js.index('$("list").addEventListener("click"'):
                 js.index('$("selectAll").addEventListener')]
    # 图片查看器整段(状态 + 函数 + 事件绑定)
    viewer = js[js.index("const viewer = {"):js.index("// 编辑弹窗")]
    return fns + "\n" + order + "\n" + res + "\n" + action + "\n" + handler + "\n" + viewer


def state_init_js():
    """抄用页面自己的 `const state = {...}` 初始值, 只改测试相关的两项。

    再声明一个同名 state 会遮蔽页面读的那个对象, 出现"我改了 state 页面没变"的假象。
    """
    js = page_js()
    block = re.search(r"const state = \{(.*?)\n    \};", js, re.S)
    if not block:
        raise RuntimeError("找不到页面里的 state 初始化代码")
    body = re.sub(r"selected: new Set\(\)", "selected: called.selected", block.group(1))
    return "const state = {" + body + "\n    };\n"


# ============================================================ CSS 解析(标题栏 flex)
def css_rules(wanted):
    """按文件顺序取出命中的 CSS 规则: [(选择器, {声明}, 顺序)]。

    同一元素可能被多条规则命中, 必须全留并按顺序叠加才是真实层叠结果。
    """
    html = HTML.read_text(encoding="utf-8")
    styles = re.findall(r"<style>(.*?)</style>", html, re.S)
    if not styles:
        raise RuntimeError("页面里没有 <style> 块")
    css = re.sub(r"/\*.*?\*/", "", styles[0], flags=re.S)   # 注释必须先去掉
    rules = []
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = block.group(1).split("}")[-1].strip()
        decls = {}
        for d in block.group(2).split(";"):
            if ":" in d:
                key, value = d.split(":", 1)
                decls[key.strip()] = value.strip()
        for sel in (s.strip() for s in selectors.split(",")):
            if sel in wanted:
                rules.append((sel, decls, len(rules)))
    return rules


def computed(hit_selectors, rules):
    """把命中该元素的规则按顺序叠加成最终声明。"""
    out = {}
    for sel, decls, _order in rules:
        if sel in hit_selectors:
            out.update(decls)
    return out


def flex_of(decls):
    """解析 flex 简写 -> (grow, shrink, basis)。"""
    value = decls.get("flex", "0 1 auto").split()
    grow = float(value[0]) if len(value) > 0 else 0
    shrink = float(value[1]) if len(value) > 1 else 1
    basis = value[2] if len(value) > 2 else "auto"
    return grow, shrink, basis


def flex_layout(items, available):
    """简化版 flex 行布局: 按 grow/shrink 分配宽度, 返回每项最终宽度。

    items: [(名称, 基础宽度, 声明 dict)]; available: 容器内容宽度(px)。
    """
    bases = [base for _n, base, _d in items]
    free = available - sum(bases)
    specs = [flex_of(d) for _n, _b, d in items]
    if free >= 0:
        grow_sum = sum(s[0] for s in specs)
        return bases if grow_sum <= 0 else [b + free * s[0] / grow_sum
                                            for b, s in zip(bases, specs)]
    weights = [s[1] * b for s, b in zip(specs, bases)]
    total_weight = sum(weights)
    if total_weight <= 0:
        return bases
    out = []
    for (_name, base, decls), weight in zip(items, weights):
        floor = 0 if decls.get("min-width") == "0" else base
        out.append(max(floor, base + free * weight / total_weight))
    return out


VIEWER_HEAD_SELECTORS = {".viewer-head", ".viewer-title", ".viewer-info",
                         ".viewer-head .tool-btn", ".tool-btn"}


def check_viewer_title_css():
    """标题栏 CSS: 标题可省略且可伸缩, 计数与按钮不参与收缩。"""
    rules = css_rules(VIEWER_HEAD_SELECTORS)
    title = computed({".viewer-title"}, rules)
    info = computed({".viewer-info", ".viewer-info, .viewer-head .tool-btn"}, rules)
    button = computed({".tool-btn", ".viewer-head .tool-btn"}, rules)
    check(title.get("text-overflow") == "ellipsis" and title.get("overflow") == "hidden"
          and title.get("white-space") == "nowrap",
          "标题过长显示省略号 (overflow/text-overflow/white-space)")
    grow, shrink, _basis = flex_of(title)
    check(grow >= 1 and shrink >= 1, f"标题可伸缩并占满剩余宽度 (flex={title.get('flex')})")
    check(title.get("min-width") == "0",
          "标题设置了 min-width:0(否则 flex 项不收缩, 省略号不生效)")
    for name, decls in (("原图/下载/关闭按钮", button), ("张数计数", info)):
        check(flex_of(decls)[1] == 0, f"{name} 不参与收缩 (flex={decls.get('flex')})")


def check_viewer_title_layout():
    """用真实 CSS 值做 flex 布局数值模拟: 标题被压缩, 按钮宽度不变。"""
    rules = css_rules(VIEWER_HEAD_SELECTORS)
    title = computed({".viewer-title"}, rules)
    info = computed({".viewer-info", ".viewer-info, .viewer-head .tool-btn"}, rules)
    button = computed({".tool-btn", ".viewer-head .tool-btn"}, rules)
    buttons = [("原图", len("原图") * 11 + 26), ("下载", len("下载") * 11 + 26),
               ("关闭", len("关闭") * 11 + 26)]
    long_prompt = "雨夜霓虹招牌俯瞰街景" * 40
    title_base = len(f"#11 {long_prompt}") * 12
    gap = 10
    items = ([("标题", title_base, title), ("计数", 40, info)]
             + [(f"{name}按钮", width, button) for name, width in buttons])
    for width in (1100, 700, 420):
        widths = flex_layout(items, width - gap * (len(items) - 1))
        title_w, button_ws = widths[0], widths[-3:]
        squash = [name for (name, base), got in zip(buttons, button_ws) if got < base - 0.5]
        ok = title_w < title_base and not squash
        print(f"  [{'ok' if ok else 'FAIL'}] 容器 {width}px: 标题 {title_w:.0f}px"
              f"(需要 {title_base}px → 省略) 按钮 {[round(w) for w in button_ws]}"
              + (f" 被压扁: {squash}" if squash else " 未被压扁"))
        if not ok:
            FAILED.append(f"flex 布局在 {width}px 下压扁了按钮")


# ============================================================ Node 侧测试脚本
HARNESS = r"""
// ---------------------------------------------------------------- 极简 DOM
// 直接解析 rowHTML() 真正产出的 HTML, 避免"手搭 DOM 与页面结构不一致"的盲区。
// HTML 里没有自闭合标记的 void 元素(不能压栈, 否则其后的兄弟节点会变成它的子节点)
const VOID_TAGS = ["IMG", "INPUT", "BR", "HR", "META", "LINK", "SOURCE", "AREA", "BASE",
                   "COL", "EMBED", "PARAM", "TRACK", "WBR"];

function parseHTML(html) {
  const mk = (tagName) => {
    const el = {
      tagName, children: [], dataset: {}, attrs: {}, _classes: new Set(),
      _parent: null, _listeners: {}, checked: false, hidden: false, _text: "",
      addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    };
    el.classList = {
      add: (c) => el._classes.add(c),
      remove: (c) => el._classes.delete(c),
      contains: (c) => el._classes.has(c),
      toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
    };
    Object.defineProperty(el, "className", {
      get: () => [...el._classes].join(" "),
      set: (v) => { el._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    });
    Object.defineProperty(el, "innerHTML", {
      get: () => el._html || "",
      set: (v) => { el._html = String(v); el.children = []; },
    });
    return el;
  };
  const root = mk("#ROOT");
  const stack = [root];
  const re = /<(\/?)([a-zA-Z][\w-]*)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    if (m[5] !== undefined) {
      if (m[5].trim()) stack[stack.length - 1]._text += m[5];
      continue;
    }
    if (m[1]) { if (stack.length > 1) stack.pop(); continue; }
    const el = mk(m[2].toUpperCase());
    el._parent = stack[stack.length - 1];
    for (const a of m[3].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
      const name = a[1], value = a[2] === undefined ? "" : a[2];
      el.attrs[name] = value;
      if (name === "class") value.split(/\s+/).filter(Boolean).forEach((c) => el._classes.add(c));
      if (name.startsWith("data-")) {
        el.dataset[name.slice(5).replace(/-([a-z])/g, (s, c) => c.toUpperCase())] = value;
      }
      if (name === "checked") el.checked = true;
      if (name === "hidden") el.hidden = true;
    }
    stack[stack.length - 1].children.push(el);
    if (!m[4] && !["IMG", "INPUT", "BR", "HR"].includes(el.tagName)) stack.push(el);
  }
  return root;
}

function matchesSimple(el, sel) {
  if (sel.startsWith(".")) return el._classes.has(sel.slice(1));
  // 支持 tag / [attr] / [attr="value"] 组合
  const m = sel.match(/^([a-zA-Z][\w-]*)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
  if (!m) return false;
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  if (m[2] === undefined) return !!m[1];
  if (!Object.prototype.hasOwnProperty.call(el.attrs, m[2])) return false;
  return m[3] === undefined || el.attrs[m[2]] === m[3];
}

function matchesSelector(el, selector) {
  // 逗号 = 或; 空格 = 后代(行里大量用 ".outputs img" 这种选择器)
  for (const sel of selector.split(",").map((s) => s.trim())) {
    const parts = sel.split(/\s+/).filter(Boolean);
    if (!parts.length || !matchesSimple(el, parts[parts.length - 1])) continue;
    let node = el._parent;
    let ok = true;
    for (let i = parts.length - 2; i >= 0 && ok; i--) {
      while (node && node.tagName !== "#ROOT" && !matchesSimple(node, parts[i])) node = node._parent;
      if (!node || node.tagName === "#ROOT") ok = false;
      else node = node._parent;
    }
    if (ok) return true;
  }
  return false;
}

function qsa(root, selector, out) {
  out = out || [];
  for (const child of root.children || []) {
    if (matchesSelector(child, selector)) out.push(child);
    qsa(child, selector, out);
  }
  return out;
}

function attachDom(root) {
  const walk = (el) => {
    el.closest = (selector) => {
      let node = el;
      while (node && node.tagName !== "#ROOT") {
        if (matchesSelector(node, selector)) return node;
        node = node._parent;
      }
      return null;
    };
    el.querySelectorAll = (selector) => qsa(el, selector);
    el.querySelector = (selector) => qsa(el, selector)[0] || null;
    (el.children || []).forEach(walk);
  };
  root.querySelectorAll = (selector) => qsa(root, selector);
  root.querySelector = (selector) => qsa(root, selector)[0] || null;
  (root.children || []).forEach(walk);
  return root;
}

function parseFragment(html) { return attachDom(parseHTML(html)); }

// ---------------------------------------------------------------- 测试替身
// openEditor/renderList 用替身; taskAction 用页面里的真身(同名的 function 声明
// 在后面插入, 会覆盖这里的替身), 所以"点了按钮到底做了什么"看 postJSON/api 的调用记录。
const called = { openEditor: [], taskAction: [], renderList: 0, selected: new Set(),
                 post: [], api: [] };
__STATE_INIT__
function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function fmtTime(ts) { return ts ? "12-31 08:00" : ""; }
function fmtDur(sec) { return sec == null ? "" : sec.toFixed(1) + "s"; }
const STATUS_LABEL = { pending: "待开始", running: "生成中", canceling: "取消中",
  done: "已完成", failed: "失败", canceled: "已取消" };
function openEditor(id) { called.openEditor.push(id); }
function taskAction(id, act) { called.taskAction.push([id, act]); }
function renderList() { called.renderList++; }
function postJSON(path, payload) {
  called.post.push([path, payload]);
  return Promise.resolve({ seq: 99 });
}
function api(path, options) {
  called.api.push([path, options]);
  return Promise.resolve({});
}
function refreshTasks() { return Promise.resolve(); }
function setStatus() {}
function confirm() { return true; }

// ---------------------------------------------------------------- 被测代码
const listEl = { addEventListener(type, fn) { if (type === "click") handlers.push(fn); } };
const handlers = [];
function stubEl(id) {
  const el = {
    id, textContent: "", innerHTML: "", src: "", hidden: false, open: false,
    attrs: {}, children: [], _listeners: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(type, fn) { (el._listeners[type] ||= []).push(fn); },
    setAttribute(k, v) { el.attrs[k] = v; if (k === "src") el.src = v; },
    getAttribute(k) { return el.attrs[k]; },
    get title() { return el.attrs.title; },
    set title(v) { el.attrs.title = v; },
    showModal() { el.open = true; },
    // 真实浏览器 dialog.close() 会派发 close 事件(Esc / 按钮关闭同理), 页面靠它清理
    close() {
      el.open = false;
      (el._listeners.close || []).forEach((fn) => fn({ type: "close" }));
    },
    removeAttribute(k) { delete el.attrs[k]; if (k === "src") el.src = ""; },
    scrollIntoView() {},
  };
  return el;
}
const byId = {};
const $ = (id) => (id === "list" ? listEl : (byId[id] ||= stubEl(id)));
// 下载用的 <a download>: 记下每次"点了哪个文件", 用来断言下载行为
const created = [];
const document = {
  addEventListener(type, fn) { if (type === "keydown") keyHandlers.push(fn); },
  createElement(tag) {
    const el = {
      tagName: String(tag).toUpperCase(), attrs: {}, clicked: 0, removed: 0,
      set href(v) { el.attrs.href = v; },
      get href() { return el.attrs.href; },
      set download(v) { el.attrs.download = v; },
      get download() { return el.attrs.download; },
      click() { el.clicked++; },
      remove() { el.removed++; },
    };
    return el;
  },
  body: { appendChild(el) { created.push(el); } },
};
const keyHandlers = [];

__HANDLER__

// ---------------------------------------------------------------- 用例
const doneTask = {
  id: "T9", seq: 9, status: "done", staged: false, kind: "generation",
  prompt: "a red cube", negative_prompt: null, refs: [], origin: null,
  created_at: 1789000000, error: null, warnings: [],
  progress: { step: 4, total: 4 }, queue_position: null,
  params: { width: 512, height: 512, steps: 4, n: 1, seed: 7, true_cfg_scale: 1 },
  usage: { elapsed_sec: 11, queue_sec: 0, vram_peak_mb: 6400 },
  outputs: [{ url: "/outputs/a.png", width: 512, height: 512, seed: 7, steps: 4 }],
};
const multiTask = Object.assign({}, doneTask, {
  id: "T10", seq: 10,
  outputs: [
    { url: "/outputs/a.png", width: 512, height: 512, seed: 1, steps: 4 },
    { url: "/outputs/b.png", width: 512, height: 512, seed: 2, steps: 4 },
    { url: "/outputs/c.png", width: 512, height: 512, seed: 3, steps: 4 },
  ],
});
const pendingTask = Object.assign({}, doneTask, {
  id: "T8", seq: 8, status: "pending", staged: true, outputs: [], usage: null,
  queue_position: 1,
});
const runningTask = Object.assign({}, doneTask, {
  id: "T12", seq: 12, status: "running", outputs: [], usage: null,
  progress: { step: 2, total: 4 }, queue_position: null,
});

const row = (task) => parseFragment(rowHTML(task));
const one = (dom, selector) => dom.querySelector(selector);
const results = {};

function dispatch(target, extra) {
  called.openEditor.length = 0;
  called.taskAction.length = 0;
  called.post.length = 0;
  called.api.length = 0;
  // 假事件: 处理器里会用 preventDefault / stopPropagation, 必须给全
  const event = Object.assign({ target, preventDefault() {}, stopPropagation() {} }, extra || {});
  handlers.forEach((fn) => fn(event));
  return event;
}

// 这一次点击是不是"什么都没发生"
const quiet = () => called.openEditor.length === 0 && called.post.length === 0
  && called.api.length === 0;

// 点某个按钮并等它跑完, 返回它打过的接口(用真实 taskAction 的副作用断言)
async function press(task, act) {
  dispatch(one(row(task), `button[data-act="${act}"]`));
  await new Promise((r) => setTimeout(r, 0));
  return called.post.map((p) => p[0]).concat(called.api.map((p) => p[0])).join("|");
}

async function main() {
  const done = row(doneTask);
  const empty = row(pendingTask);

  // ---- 点击语义: 行内只有 复选框 / 操作按钮 / 结果缩览图 三处可点 ----
  dispatch(one(done, ".outputs img"));
  results.clickImage = called.openEditor.length === 0;
  dispatch(one(done, ".outputs a"));
  results.clickLink = called.openEditor.length === 0;
  dispatch(one(done, ".pick"));
  results.clickCheckbox = called.openEditor.length === 0 && called.renderList > 0;
  // 按钮之外的位置一律无操作(编辑不再挂在整行上)
  dispatch(one(done, ".clip-title"));
  results.clickTitleNoop = quiet();
  dispatch(one(done, ".clip-meta"));
  results.clickParamsNoop = quiet();
  dispatch(one(empty, ".out-placeholder"));
  results.clickPlaceholderNoop = quiet();
  dispatch(one(empty, ".out-placeholder").closest(".cell"));
  results.clickEmptyCellNoop = quiet();
  dispatch(one(done, ".idx"));
  results.clickIndexNoop = quiet();
  dispatch(one(done, ".task-row"));
  results.clickRowNoop = quiet();
  dispatch(one(done, ".clip-info"));
  results.clickInfoNoop = quiet();

  // 铅笔按钮 ✎ 必须打开编辑窗(曾经 taskAction 少了 "edit" 分支, 点了没反应)
  dispatch(one(done, 'button[data-act="edit"]'));
  await new Promise((r) => setTimeout(r, 0));
  results.editButtonOpensEditor = called.openEditor.length === 1
    && called.openEditor[0] === "T9";
  // 而且这是打开编辑窗的唯一入口
  results.editOnlyViaButton = (() => {
    called.openEditor.length = 0;
    dispatch(one(done, ".task-row"));
    dispatch(one(done, ".clip-title"));
    dispatch(one(done, ".clip-meta"));
    const viaRow = called.openEditor.length;
    dispatch(one(done, 'button[data-act="edit"]'));
    return viaRow === 0 && called.openEditor.length === 1 && called.openEditor[0] === "T9";
  })();

  // 行内按钮 -> 真实 taskAction, 各打各的接口(不弹编辑窗)
  results.clickRetryButton = (await press(doneTask, "retry")) === "/api/tasks/T9/retry"
    && called.openEditor.length === 0;
  results.clickReleaseButton = (await press(pendingTask, "release")) === "/api/tasks/T8/release";
  results.clickHoldButton = (await press(
    Object.assign({}, pendingTask, { staged: false, id: "T7", seq: 7 }), "hold"
  )) === "/api/tasks/T7/hold";
  results.clickCancelButton = (await press(runningTask, "cancel")) === "/api/tasks/T12/cancel";
  results.clickDeleteButton = (await press(doneTask, "delete")) === "/api/tasks/T9"
    && called.api[0][1].method === "DELETE";

  // ---- 渲染 ----
  const doneHTML = rowHTML(doneTask);
  const pendingHTML = rowHTML(pendingTask);
  results.renderDoneHasThumb = doneHTML.includes('href="/outputs/a.png"')
    && doneHTML.includes('data-view="T9"') && doneHTML.includes('draggable="false"');
  results.renderDoneNoNewTab = !doneHTML.includes('target="_blank"');
  results.renderPendingHasPlaceholder = pendingHTML.includes("out-placeholder")
    && !pendingHTML.includes("<img");
  results.renderColumnOrder =
    /clip-info">[\s\S]*clip-meta">[\s\S]*class="(?:outputs|out-placeholder)"[\s\S]*class="cell">[\s\S]*data-act="retry"/
      .test(doneHTML)
    && /class="(?:outputs|out-placeholder)"[\s\S]*class="cell">[\s\S]*data-act="retry"/
      .test(pendingHTML);
  // 生成中的任务 ✎ 置灰(点了也没反应最像"按钮坏了"), 其它状态可点
  const runningHTML = rowHTML(runningTask);
  results.runningEditDisabled =
    /data-act="edit"[^>]*\sdisabled/.test(runningHTML)
    && !/data-act="edit"[^>]*\sdisabled/.test(doneHTML);
  results.pendingEditEnabled = !/data-act="edit"[^>]*\sdisabled/.test(pendingHTML);

  // ---- 列表里的下载按钮: 有产物才可点, 一次把这条任务的图都下下来 ----
  const dlButton = (task) => one(row(task), 'button[data-act="download"]');
  const isDisabled = (el) => Object.prototype.hasOwnProperty.call(el.attrs, "disabled");
  const linkUrls = () => created.map((a) => a.href).join(",");
  results.rowHasDownloadButton = !!dlButton(doneTask) && !!dlButton(pendingTask)
    && !!dlButton(runningTask);
  results.downloadDisabledWithoutOutputs = isDisabled(dlButton(pendingTask))
    && isDisabled(dlButton(runningTask)) && !isDisabled(dlButton(doneTask));
  results.downloadTitleMentionsCount =
    /data-act="download"[^>]*title="下载这条任务的全部 3 张图片"/.test(rowHTML(multiTask));

  state.tasks = [doneTask];
  created.length = 0;
  dispatch(one(done, 'button[data-act="download"]'));
  results.clickDownloadSingle = linkUrls() === "/outputs/a.png" && created[0].clicked === 1
    && created[0].removed === 1 && created[0].download === "a.png"
    && called.openEditor.length === 0 && called.api.length === 0;

  state.tasks = [multiTask];
  created.length = 0;
  dispatch(one(row(multiTask), 'button[data-act="download"]'));
  results.downloadFirstIsImmediate = linkUrls() === "/outputs/a.png";   // 第一张在手势里直接下
  await new Promise((r) => setTimeout(r, 700));
  results.downloadAllOutputs = linkUrls() === "/outputs/a.png,/outputs/b.png,/outputs/c.png"
    && created.every((a) => a.clicked === 1 && a.removed === 1)
    && created.map((a) => a.download).join(",") === "a.png,b.png,c.png";

  results.downloadWithoutOutputsNoop = (() => {
    state.tasks = [pendingTask];
    created.length = 0;
    dispatch(one(row(pendingTask), 'button[data-act="download"]'));
    return created.length === 0 && called.api.length === 0 && called.openEditor.length === 0;
  })();

  // ---- 固定顺序: 按任务号降序, 与状态/结束时间无关 ----
  results.orderFixed = (() => {
    // 服务端真实顺序: 未结束在前(按 seq), 已结束在后(按结束时间倒序)
    const served = [
      { id: "a", seq: 3, status: "pending" },
      { id: "b", seq: 7, status: "running" },
      { id: "c", seq: 9, status: "done", finished_at: 300 },
      { id: "d", seq: 2, status: "done", finished_at: 900 },
      { id: "e", seq: 5, status: "canceled", finished_at: 100 },
    ];
    return sortTasks(served).map((t) => t.id).join(",") === "c,b,e,a,d";
  })();
  results.orderIgnoresStatus = (() => {
    // 同一批任务状态变了(取消/完成)顺序必须一模一样
    const before = sortTasks([
      { id: "a", seq: 1, status: "pending" },
      { id: "b", seq: 2, status: "pending" },
      { id: "c", seq: 3, status: "pending" },
    ]).map((t) => t.id).join(",");
    const after = sortTasks([
      { id: "a", seq: 1, status: "done", finished_at: 999 },
      { id: "b", seq: 2, status: "running" },
      { id: "c", seq: 3, status: "canceled", finished_at: 1 },
    ]).map((t) => t.id).join(",");
    return before === after && before === "c,b,a";
  })();
  results.orderDoesNotMutate = (() => {
    const served = [{ id: "a", seq: 1 }, { id: "b", seq: 2 }];
    sortTasks(served);
    return served[0].id === "a" && served[1].id === "b";
  })();

  // ---- 分辨率: 长边 / 自定义宽高(规则必须与服务端 resolve_size 一致) ----
  RES.max = 1536; RES.min = 256; RES.step = 16;
  RES.defaultLongSide = 1024; RES.softMax = 1024;
  const res = (v) => readResolution(Object.assign(
    { ratio: "", longSide: "", width: "", height: "" }, v));
  results.resCustomExact = (() => {
    const r = res({ width: "1280", height: "720" });
    return !r.error && r.size === "1280x720" && r.canvas === "1280×720";
  })();
  results.resCustomRounds = (() => {
    const r = res({ width: "1000", height: "700" });
    return r.size === "992x688" && /取整/.test(r.note);
  })();
  results.resCustomFloor = (() => res({ width: "100", height: "100" }).size === "256x256")();
  results.resCustomOverLimit = (() => {
    const r = res({ width: "2000", height: "1080" });
    return !!r.error && r.error.includes("1536") && r.error.includes("QWEN_MAX_SIDE");
  })();
  results.resLongSideOverLimit = (() => !!res({ longSide: "2048" }).error)();
  results.resCustomPartial = (() => !!res({ width: "1024" }).error)();
  results.resCustomBadNumber = (() => !!res({ width: "abc", height: "720" }).error)();
  results.resCustomWinsOverRatio = (() => {
    const r = res({ ratio: "16:9", longSide: "1024", width: "640", height: "896" });
    return r.size === "640x896" && !r.long_side;
  })();
  results.resRatioLongSide = (() => {
    const r = res({ ratio: "16:9", longSide: "1280" });
    return r.long_side === 1280 && r.canvas === "1280×720";
  })();
  results.resRatioDefaultLongSide = (() => {
    const r = res({ ratio: "4:3" });
    return r.long_side === 1024 && r.canvas === "1024×768";
  })();
  results.resSquareDefault = (() => {
    const r = res({});
    return r.long_side === 1024 && r.canvas === "1024×1024";
  })();
  results.resFollowRef = (() => {
    const r = res({ followRef: true });
    return !!r.long_side && /参考图/.test(r.note) && !r.error;
  })();
  results.resBigWarnsNotBlocked = (() => {
    const r = res({ width: "1536", height: "1536" });
    return !r.error && !!r.warn && r.warn.includes("1024");
  })();
  results.resPaintNote = (() => {
    const el = { className: "", textContent: "" };
    paintResNote(el, res({ width: "2000", height: "1080" }));
    const err = el.className === "res-note err" && el.textContent.indexOf("✕") === 0;
    paintResNote(el, res({ width: "640", height: "896" }));      // ≤1024: 正常, 无警告
    const ok = el.className === "res-note" && el.textContent.includes("640×896");
    paintResNote(el, res({ width: "1536", height: "1536" }));
    const warn = el.className === "res-note warn" && el.textContent.indexOf("⚠") === 0;
    return err && ok && warn;
  })();
  // 行里没有内联事件/内联编辑调用: 编辑只能由 #list 上的委托处理器按按钮分发
  results.renderRowHasNoInlineHandler = !/onclick|openEditor\(/.test(doneHTML);

  // ---- 图片查看器 ----
  results.thumbOpensViewer = (() => {
    state.tasks = [doneTask];
    called.openEditor.length = 0;
    let prevented = false;
    handlers.forEach((fn) => fn({
      target: one(done, ".outputs img"), preventDefault() { prevented = true; },
    }));
    return prevented && called.openEditor.length === 0
      && $("viewerImage").src === "/outputs/a.png" && $("viewer").open === true;
  })();
  results.thumbViewerTitle = $("viewerTitle").textContent.includes("#9")
    && $("viewerTitle").textContent.includes("a red cube")
    && !$("viewerTitle").textContent.includes("#10");
  results.thumbPicksClickedIndex = (() => {
    state.tasks = [multiTask];
    openViewer("T10", 1);
    const ok = $("viewerCount").textContent === "2 / 3"
      && $("viewerImage").src === "/outputs/b.png";
    closeViewer();
    return ok;
  })();
  state.tasks = [multiTask];
  openViewer("T10", 0);
  results.multiShowsStrip = $("viewerStrip").hidden === false
    && ($("viewerStrip").innerHTML.match(/<img /g) || []).length === 3
    && $("viewerPrev").hidden === false && $("viewerNext").hidden === false;
  results.multiCountText = $("viewerCount").textContent === "1 / 3"
    && $("viewerMeta").textContent.includes("seed 1");
  stepViewer(1);
  results.multiNext = $("viewerImage").src === "/outputs/b.png"
    && $("viewerCount").textContent === "2 / 3";
  stepViewer(-1); stepViewer(-1);
  results.multiWrap = $("viewerImage").src === "/outputs/c.png"
    && $("viewerCount").textContent === "3 / 3";
  results.multiDownloadName = $("viewerDownload").getAttribute("download") === "c.png";
  closeViewer();
  results.closeClears = $("viewer").open === false && !$("viewerImage").src
    && $("viewerStrip").hidden === true && $("viewerStrip").innerHTML === "";
  $("viewerClose")._listeners.click[0]();
  results.closeButtonWorks = $("viewer").open === false;

  state.tasks = [doneTask];
  openViewer("T9", 0);
  results.singleHidesStrip = $("viewerStrip").hidden === true
    && $("viewerPrev").hidden === true && $("viewerNext").hidden === true;
  results.singleCountText = $("viewerCount").textContent === "1 / 1";
  closeViewer();

  state.tasks = [multiTask];
  openViewer("T10", 0);
  keyHandlers.forEach((fn) => fn({ key: "ArrowRight", preventDefault() {} }));
  results.keyboardNext = $("viewerImage").src === "/outputs/b.png";
  closeViewer();
  results.keyboardIgnoredWhenClosed = (() => {
    const before = $("viewerImage").src;
    keyHandlers.forEach((fn) => fn({ key: "ArrowRight", preventDefault() {} }));
    return $("viewerImage").src === before;
  })();

  // 很长的提示词: 标题仍是一行, 完整内容挂在 title 属性上供悬停查看
  const longPrompt = "雨夜霓虹招牌俯瞰街景".repeat(40);
  state.tasks = [Object.assign({}, doneTask, { id: "T11", seq: 11, prompt: longPrompt })];
  openViewer("T11", 0);
  results.longPromptKeepsTitleTitle =
    $("viewerTitle").textContent === `#11 ${longPrompt}`
    && $("viewerTitle").getAttribute("title") === `#11 提示词：${longPrompt}`;
  closeViewer();

  // ---- 弹窗关闭: 点遮罩(背景) 与 Esc 都要能关(编辑窗曾经两条路都关不掉) ----
  const editDialog = $("editDialog");
  const openEditorStub = () => { state.editTaskId = "T9"; editDialog.open = true; };
  const clearEditor = () => { editDialog.open = false; state.editTaskId = null; };
  const pressKey = (extra) => keyHandlers.forEach((fn) => fn(Object.assign({ key: "Escape" }, extra)));

  results.editorMaskClickCloses = (() => {
    openEditorStub();
    editDialog._listeners.click[0]({ target: editDialog });
    const ok = editDialog.open === false && state.editTaskId === null;
    clearEditor();
    return ok;
  })();
  results.editorMaskClickInsideKeepsOpen = (() => {
    openEditorStub();
    editDialog._listeners.click[0]({ target: { tagName: "FORM" } });   // 点在弹窗里的表单上
    const ok = editDialog.open === true && state.editTaskId === "T9";
    clearEditor();
    return ok;
  })();
  results.editorEscCloses = (() => {
    openEditorStub();
    pressKey({});
    const ok = editDialog.open === false && state.editTaskId === null;
    clearEditor();
    return ok;
  })();
  results.editorEscIgnoredWhenClosed = (() => {
    editDialog.open = false;
    state.editTaskId = "T9";
    pressKey({});
    const ok = state.editTaskId === "T9";        // 没开窗就不该被 Esc 误清编辑态
    state.editTaskId = null;
    return ok;
  })();
  results.editorEscDuringImeKeepsOpen = (() => {
    openEditorStub();
    pressKey({ isComposing: true });              // 组词中的 Esc 属于输入法
    const ok = editDialog.open === true && state.editTaskId === "T9";
    clearEditor();
    return ok;
  })();
  results.editorOtherKeyKeepsOpen = (() => {
    openEditorStub();
    pressKey({ key: "ArrowLeft" });
    pressKey({ key: "a" });
    const ok = editDialog.open === true && state.editTaskId === "T9";
    clearEditor();
    return ok;
  })();
  // 任何关闭方式(含浏览器原生的 Esc 直接 close)都不该留下脏编辑态
  results.editorCloseEventClearsState = (() => {
    editDialog.open = true;
    state.editTaskId = "T9";
    editDialog.close();
    return state.editTaskId === null;
  })();
  results.viewerMaskClickCloses = (() => {
    state.tasks = [doneTask];
    openViewer("T9", 0);
    const dialog = $("viewer");
    const wasOpen = dialog.open === true;
    dialog._listeners.click[0]({ target: dialog });
    return wasOpen && dialog.open === false;
  })();
  results.viewerEscCloses = (() => {
    state.tasks = [doneTask];
    openViewer("T9", 0);
    const dialog = $("viewer");
    pressKey({});
    const ok = dialog.open === false;
    if (dialog.open) closeViewer();
    return ok;
  })();

  console.log(JSON.stringify(results));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""


def main():
    harness = (HARNESS.replace("__HANDLER__", extract_js())
               .replace("__STATE_INIT__", state_init_js()))
    tmp = Path(tempfile.mkdtemp(prefix="qwen-click-test-"))
    try:
        script = tmp / "click_test.js"
        script.write_text(harness, encoding="utf-8")
        node = str(NODE) if NODE and NODE.is_file() else shutil.which("node")
        if not node:
            print("[SKIP] 找不到 node, 无法执行交互测试")
            return 0
        proc = subprocess.run([node, str(script)], capture_output=True, text=True,
                              encoding="utf-8", timeout=90)
        if proc.returncode != 0:
            print("[FAIL] node 执行失败:\n" + (proc.stderr or "")[-2000:])
            return 1
        lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
        if not lines:
            print("[FAIL] 没有拿到测试结果输出")
            return 1
        res = json.loads(lines[-1])

        check(res["clickImage"], "点结果图片只在页内查看, 不弹编辑窗")
        check(res["clickLink"], "点结果链接不弹编辑窗")
        check(res["clickCheckbox"], "点复选框只切换选中")
        check(res["clickTitleNoop"], "点提示词区域无操作(不弹编辑窗)")
        check(res["clickParamsNoop"], "点参数列无操作")
        check(res["clickIndexNoop"], "点序号列无操作")
        check(res["clickInfoNoop"], "点任务信息区无操作")
        check(res["clickPlaceholderNoop"], "点结果列占位符无操作")
        check(res["clickEmptyCellNoop"], "点结果列空白处无操作")
        check(res["clickRowNoop"], "点整行空白无操作")
        check(res["editButtonOpensEditor"], "点铅笔按钮 ✎ 真的打开编辑窗(不再点了没反应)")
        check(res["editOnlyViaButton"], "编辑窗只能由 ✎ 按钮打开(点行不再打开)")
        check(res["clickRetryButton"], "点 ↻ 只打重新生成接口, 不弹编辑窗")
        check(res["clickReleaseButton"], "点 ▶ 只打开始暂存任务的接口")
        check(res["clickHoldButton"], "点 ‖ 只打退回暂存的接口")
        check(res["clickCancelButton"], "点 ■ 只打取消接口")
        check(res["clickDeleteButton"], "点 × 只打删除接口(DELETE)")
        check(res["renderDoneHasThumb"], "已出图任务在结果列渲染可点击的缩览图")
        check(res["renderDoneNoNewTab"], "缩览图不再 target=_blank(改为本页查看)")
        check(res["renderPendingHasPlaceholder"], "未出图任务在结果列渲染占位符")
        check(res["renderColumnOrder"], "结果列位于参数列与进度列之间")
        check(res["renderRowHasNoInlineHandler"], "行内没有内联 onclick/编辑调用(统一走事件委托)")
        check(res["runningEditDisabled"], "生成中的任务 ✎ 置灰提示先取消, 其它状态可点")
        check(res["pendingEditEnabled"], "待开始任务的 ✎ 可点")
        check(res["rowHasDownloadButton"], "每行都有下载按钮(↓)")
        check(res["downloadDisabledWithoutOutputs"], "还没出图的任务下载按钮置灰")
        check(res["downloadTitleMentionsCount"], "多图任务的下载按钮写明张数")
        check(res["clickDownloadSingle"], "单图任务点 ↓ 直接下载该图(带文件名)")
        check(res["downloadFirstIsImmediate"], "多图任务第一张在当前点击手势里立刻下载")
        check(res["downloadAllOutputs"], "多图任务点一次 ↓ 依次下完全部产物")
        check(res["downloadWithoutOutputsNoop"], "没有产物时点 ↓ 什么都不做")
        check(res["orderFixed"], "列表按任务号降序(最新在最上), 与服务端顺序无关")
        check(res["orderIgnoresStatus"], "任务状态变化(完成/取消)不改变行顺序")
        check(res["orderDoesNotMutate"], "排序不改动入参数组")
        check(res["resCustomExact"], "自定义宽高 = 该尺寸原样使用")
        check(res["resCustomRounds"], "自定义宽高按 16 的倍数取整并提示")
        check(res["resCustomFloor"], "过小的尺寸抬到最小值 256")
        check(res["resCustomOverLimit"], "自定义超过单边上限 -> 报错并提示 QWEN_MAX_SIDE")
        check(res["resLongSideOverLimit"], "长边超过单边上限 -> 报错")
        check(res["resCustomPartial"], "只填宽或只填高 -> 报错")
        check(res["resCustomBadNumber"], "非数字尺寸 -> 报错")
        check(res["resCustomWinsOverRatio"], "自定义宽高优先于长宽比")
        check(res["resRatioLongSide"], "长宽比 + 长边算出画布")
        check(res["resRatioDefaultLongSide"], "长边留空 = 服务端默认长边")
        check(res["resSquareDefault"], "无长宽比无自定义 = 正方形")
        check(res["resFollowRef"], "编辑跟随参考图时提示按参考图比例")
        check(res["resBigWarnsNotBlocked"], "超过 1024 只警告(可提交), 不拦")
        check(res["resPaintNote"], "提示行区分报错/正常/警告三种样式")
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
        check(res["longPromptKeepsTitleTitle"], "很长提示词时 title 属性仍带完整提示词")
        check(res["editorMaskClickCloses"], "编辑窗点遮罩(背景)能关闭")
        check(res["editorMaskClickInsideKeepsOpen"], "点编辑窗内部不误关")
        check(res["editorEscCloses"], "编辑窗按 Esc 能关闭并清掉编辑态")
        check(res["editorEscIgnoredWhenClosed"], "编辑窗没打开时按 Esc 不误清编辑态")
        check(res["editorEscDuringImeKeepsOpen"], "输入法组词中的 Esc 不关窗")
        check(res["editorOtherKeyKeepsOpen"], "非 Esc 按键不关窗")
        check(res["editorCloseEventClearsState"], "任何方式关闭编辑窗都清掉编辑态")
        check(res["viewerMaskClickCloses"], "查看器点遮罩(背景)能关闭")
        check(res["viewerEscCloses"], "查看器按 Esc 能关闭")

        # 排序函数必须真的被刷新流程用上, 否则只是摆设
        check("state.tasks = sortTasks(data.tasks || [])" in page_js(),
              "refreshTasks 用 sortTasks 重排任务(不直接用服务端顺序)")

        # 分辨率控件: 两个入口都要把结果发出去, 并且上限来自服务端
        page = page_js()
        check(all(f'id="{i}"' in HTML.read_text(encoding="utf-8")
                  for i in ("genLongSide", "genWidth", "genHeight",
                            "editLongSide", "editWidth", "editHeight")),
              "新建任务与编辑弹窗都有「长边 + 自定义宽高」输入框")
        check("if (res.size) body.size = res.size;" in page and
              "if (res.long_side) body.long_side = res.long_side;" in page,
              "新建任务提交时带上 size / long_side")
        check("if (res.size) payload.size = res.size;" in page,
              "编辑/重新生成提交时带上 size")
        check("if (data.max_side) RES.max = +data.max_side;" in page and
              "applyResLimits();" in page,
              "分辨率上限/默认长边取自服务端 /api/queue")
        check('setGenStatus("分辨率不合适：" + res.error, "err")' in page,
              "尺寸超限时在新建任务里直接报错(不提交)")

        # 两个弹窗都必须走同一套「点遮罩 / Esc 关闭」绑定(少一处就会出现关不掉的窗)
        binds = ['bindBackdropClose($("viewer"), closeViewer);',
                 'bindEscapeClose($("viewer"), closeViewer);',
                 'bindBackdropClose($("editDialog"), closeEditor);',
                 'bindEscapeClose($("editDialog"), closeEditor);']
        check(all(b in page for b in binds),
              "查看器与编辑窗都绑定了「点遮罩 / Esc 关闭」")
        check('<div class="hdr">下载</div>' in HTML.read_text(encoding="utf-8"),
              "列表表头新增「下载」列")

        check_viewer_title_css()
        check_viewer_title_layout()

        print()
        if FAILED:
            print(f"失败 {len(FAILED)} 项:")
            for item in FAILED:
                print("   - " + item)
            return 1
        print("全部通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
