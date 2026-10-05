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


def viewer_head_rules():
    """按文件顺序取出查看器标题栏相关规则, 用于模拟 flex 收缩。

    返回 [(选择器, 声明 dict, 顺序)], 保留全部命中规则 —— 同一个元素可能被多条
    规则命中(例如 .viewer-info 与 .viewer-info, .viewer-head .tool-btn), 必须按
    顺序叠加才是真实层叠结果, 不能只留最后一条。
    """
    html = HTML.read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", re.search(r"<style>(.*?)</style>", html, re.S).group(1),
                 flags=re.S)
    wanted = {".viewer-head", ".viewer-head .spacer", ".viewer-title", ".viewer-info",
              ".viewer-head .tool-btn", ".tool-btn"}
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


def computed(element_selectors, rules):
    """把命中该元素的规则按顺序叠加成最终声明。"""
    out = {}
    for sel, decls, _order in rules:
        if sel in element_selectors:
            out.update(decls)
    return out


def flex_of(decls):
    """解析 flex 简写, 返回 (grow, shrink, basis)。"""
    value = decls.get("flex", "0 1 auto").split()
    grow = float(value[0]) if len(value) > 0 else 0
    shrink = float(value[1]) if len(value) > 1 else 1
    basis = value[2] if len(value) > 2 else "auto"
    return grow, shrink, basis


def flex_layout(items, available):
    """简化版 flex 行布局: 按 flex-grow/shrink 分配宽度, 返回每项最终宽度。

    items: [(名称, 基础宽度, flex 声明 dict)]; available: 容器内容宽度(px)。
    够用则按 grow 分配剩余空间, 不够则按 shrink 比例收缩(受 min-width 限制)。
    """
    bases = [base for _n, base, _d in items]
    total = sum(bases)
    free = available - total
    specs = [flex_of(d) for _n, _b, d in items]
    if free >= 0:
        grow_sum = sum(s[0] for s in specs)
        if grow_sum <= 0:
            return bases
        return [b + free * s[0] / grow_sum for b, s in zip(bases, specs)]
    # 收缩: 权重 = shrink * basis; min-width:0 时下限为 0
    weights = [s[1] * b for s, b in zip(specs, bases)]
    total_weight = sum(weights)
    if total_weight <= 0:
        return bases
    out = []
    for (name, base, decls), weight in zip(items, weights):
        floor = 0 if decls.get("min-width") == "0" else base
        out.append(max(floor, base + free * weight / total_weight))
    return out


def measure_css_layout():
    """用真实 CSS 值模拟: 很长的提示词下, 标题被省略、按钮不被压扁。"""
    rules = viewer_head_rules()
    title = computed({".viewer-title"}, rules)
    info = computed({".viewer-info", ".viewer-info, .viewer-head .tool-btn"}, rules)
    button = computed({".tool-btn", ".viewer-head .tool-btn"}, rules)
    # 按钮基础宽度 = 文本字数 * 11px + 内边距 24px + 边框 2px(粗略但足够判断是否被压扁)
    def btn_width(text):
        return len(text) * 11 + 24 + 2
    buttons = [("原图", btn_width("原图")), ("下载", btn_width("下载")),
               ("关闭", btn_width("关闭"))]
    long_prompt = "雨夜霓虹招牌俯瞰街景" * 40
    title_base = len(f"#11 {long_prompt}") * 12          # 标题字号 12px
    gap = 10
    items = ([("标题", title_base, title)]
             + [("计数", 40, info)]
             + [(f"{name}按钮", width, button) for name, width in buttons])
    for width in (1100, 700, 420):                        # 大屏 / 窄窗 / 很窄
        widths = flex_layout(items, width - gap * (len(items) - 1))
        title_w = widths[0]
        button_ws = widths[-3:]
        # 按钮最终宽度必须不小于各自基础宽度(即没被压扁)
        squash = [name for (name, base), got in zip(buttons, button_ws) if got < base - 0.5]
        ok = title_w < title_base and not squash
        print(f"  [{'ok' if ok else 'FAIL'}] 容器 {width}px: 标题 {title_w:.0f}px"
              f"(需要 {title_base}px, 会省略) 按钮 {[round(w) for w in button_ws]}"
              + (f" 被压扁: {squash}" if squash else " 未被压扁"))
        if not ok:
            FAILED.append(f"flex 布局在 {width}px 下压扁了按钮")
    return not FAILED


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
    id, textContent: "", innerHTML: "", src: "", hidden: false, open: false,
    children: [], attrs: {}, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(type, fn) { (el._listeners[type] ||= []).push(fn); },
    _listeners: {},
    setAttribute(k, v) { el.attrs[k] = v; if (k === "src") el.src = v; },
    getAttribute(k) { return el.attrs[k]; },
    // 真实 DOM 里 el.title 与 title 属性是同一份数据, 这里同步, 免得测试看不一致
    get title() { return el.attrs.title; },
    set title(v) { el.attrs.title = v; },
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

// 很长的提示词: 标题仍是一行, 完整内容放在 title 属性上供悬停查看
const longPrompt = "雨夜霓虹招牌俯瞰街景" .repeat(40);
const longTask = Object.assign({}, doneTask, { id: "T11", seq: 11, prompt: longPrompt });
state.tasks = [longTask];
openViewer("T11", 0);
results.longPromptKeepsTitleTitle = $("viewerTitle").textContent.includes(longPrompt)
  && $("viewerTitle").getAttribute("title").includes(longPrompt)
  && $("viewerTitle").getAttribute("title").startsWith("#11");
closeViewer();

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

        # ---- 查看器标题栏的 flex 布局(提示词很长时不能把按钮压扁) ----
        rules = viewer_head_rules()
        title = computed({".viewer-title"}, rules)
        info = computed({".viewer-info", ".viewer-info, .viewer-head .tool-btn"}, rules)
        button = computed({".tool-btn", ".viewer-head .tool-btn"}, rules)
        check(title.get("text-overflow") == "ellipsis"
              and title.get("overflow") == "hidden"
              and title.get("white-space") == "nowrap",
              "标题过长显示省略号 (overflow/text-overflow/white-space)")
        _, title_shrink, _ = flex_of(title)
        check(flex_of(title)[0] >= 1 and title_shrink >= 1,
              f"标题可伸缩并占满剩余宽度 (flex={title.get('flex')})")
        check(title.get("min-width") == "0",
              "标题设置了 min-width:0(否则 flex 项不会收缩, 省略号不生效)")
        for name, decls in (("原图/下载/关闭按钮", button), ("张数计数", info)):
            _, shrink, _ = flex_of(decls)
            check(shrink == 0, f"{name} 不参与收缩 (flex={decls.get('flex')})")
        check(button.get("padding") and info.get("white-space") == "nowrap",
              "按钮保留自身内边距/计数不换行")
        check(res["longPromptKeepsTitleTitle"], "标题 title 属性带完整提示词(悬停显示)")

        print("  -- 标题栏 flex 布局模拟(长提示词) --")
        measure_css_layout()
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
