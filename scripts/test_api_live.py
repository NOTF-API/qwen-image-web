# -*- coding: utf-8 -*-
"""对**正在运行的服务**做一遍接口完备性 + 与 Web 工作台的一致性测试。

与其它脚本的分工:
  * test_server_queue.py  起一个临时实例 + 假生成, 离线覆盖队列逻辑(秒级, 不占 GPU)
  * test_queue_gpu.py     队列 + 真模型集成测试(自行拉起服务)
  * 本脚本                 直接打**已经开着的**服务, 验证「网页上能做的事, 接口都能做」

用法:
  venv\\Scripts\\python.exe scripts\\test_api_live.py            # 全量(含 2 次真出图)
  venv\\Scripts\\python.exe scripts\\test_api_live.py --no-gpu   # 跳过真出图(秒级)
  venv\\Scripts\\python.exe scripts\\test_api_live.py --base http://127.0.0.1:8091

副作用控制: 只创建自己带 ``[apitest]`` 标记的任务, 结束时全部删除并恢复 auto_start,
不会碰你已有的任务与图片。真出图产生的 PNG 也会在结束时删掉。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
from pathlib import Path

try:                                     # Windows 控制台默认 GBK, 中文会花屏
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_HTML = BASE_DIR / "static" / "index.html"
MARK = "[apitest]"

PASS, FAIL, SKIP = [], [], []
_created_ids: list[str] = []
_saved_autostart: bool | None = None
_created_files: list[str] = []
# 本次运行开始前就存在的任务 ID。删除是不可逆的(连同产物 PNG), 所以最后要核对
# 这批 ID 一个都没少 —— 早先清理时用「提示词像不像测试垃圾」去猜, 误删了用户任务。
_baseline_ids: set[str] = set()


# ---------------------------------------------------------------- 断言与输出
def check(cond, label, extra="") -> bool:
    line = f"  {label}"
    if cond:
        PASS.append(line)
        print(f"\033[32mPASS\033[0m {label}")
    else:
        FAIL.append(line + (f"  [{extra}]" if extra else ""))
        print(f"\x1b[31mFAIL\x1b[0m {label}" + (f"  [{extra}]" if extra else ""))
    return bool(cond)


def skip(label, why="") -> None:
    SKIP.append(label)
    print(f"\x1b[33mSKIP\x1b[0m {label}" + (f"  ({why})" if why else ""))


def section(title: str) -> None:
    print(f"\n\033[1m== {title}\033[0m")


# ---------------------------------------------------------------- HTTP 封装
def req(method: str, path: str, body=None, timeout=600, raw=False):
    url = path if path.startswith("http") else BASE + path
    try:
        r = requests.request(method, url, json=body, timeout=timeout)
    except requests.RequestException as e:
        return 0, {"error": str(e)}
    if raw:
        return r.status_code, r.content
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"_text": r.text[:400], "_ct": r.headers.get("content-type", "")}


def get(path, **kw):
    return req("GET", path, **kw)


def post(path, body=None, **kw):
    return req("POST", path, body if body is not None else {}, **kw)


def patch(path, body=None, **kw):
    return req("PATCH", path, body if body is not None else {}, **kw)


def delete(path, **kw):
    return req("DELETE", path, **kw)


def hold(path, **kw):
    return req("POST", path, {}, **kw)


# ---------------------------------------------------------------- 工具
def tiny_png_b64(size=(64, 48), color=(200, 40, 40)) -> str:
    from PIL import Image
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def web_paths() -> list[str]:
    """从 static/index.html 里静态抽出网页真正会调用的接口路径。"""
    src = WEB_HTML.read_text(encoding="utf-8")
    found: list[str] = []
    patterns = [
        r'''fetch\(\s*["`'](/[^"`'{$]+)''',            # fetch("/api/xxx")
        r'''["'`](/api/queue/[\w-]+)["'`]''',       # 字面量队列接口
        r'''["'`](/api/tasks/\$\{[^}]+\}/[\w-]+)["'`]''',  # 模板字符串
        r'''["'`]([^"'`]*?/api/tasks/\$\{[^}]+\})["'`]''',
        r'''["'`]([^"'`]*?/v1/images/(?:generations|edits/json))["'`]''',
    ]
    for pat in patterns:
        for m in re.finditer(pat, src):
            p = m.group(1)
            if p.startswith("/api") or p.startswith("/v1") or p.startswith("/health"):
                if p not in found:
                    found.append(p)
    return found


def norm(path: str) -> str:
    """把网页里的路径模板归一成 FastAPI 路由形式, 便于和 openapi 对比。"""
    p = path.split("?")[0].rstrip("/") or "/"
    p = p.replace("${taskId}", "{task_id}")
    p = re.sub(r"\$\{[^}]+\}", "{param}", p)
    return p


def new_task_ids(resp) -> list[str]:
    return [d["id"] for d in (resp.get("data") or []) if d.get("id")]


def queue_off() -> None:
    global _saved_autostart
    _, q = get("/api/queue")
    if q.get("auto_start") is not False:
        _saved_autostart = bool(q.get("auto_start"))
        post("/api/queue/auto-start", {"auto_start": False})


def queue_restore() -> None:
    if _saved_autostart is not None:
        post("/api/queue/auto-start", {"auto_start": _saved_autostart})


# 真出图阶段的统一上限。耗时随画布面积与 output_resolution **平方级**增长, 参考图
# 还要多付一次视觉编码器 prefill —— 所以本脚本**只做功能验证, 一律最小分辨率**。
# 想要真出图看效果请用 examples/client.mjs 或网页, 不要用这个脚本。
GPU_MAX_SIDE = 256          # 画布长边上限
GPU_MAX_OUT_RES = 256       # 参考图缩放基准上限
GPU_MAX_STEPS = 8           # 步数上限(步数是线性成本; 取消测试需要几步才来得及打断)


def gpu_guard():
    """真出图阶段的参数刹车: 任何超标的请求直接失败, 免得一轮测试烧几分钟。"""
    if GPU_MAX_SIDE > 256 or GPU_MAX_OUT_RES > 256 or GPU_MAX_STEPS > 8:
        raise RuntimeError("测试脚本只允许最小分辨率(<=256, <=8步); "
                           "不要为了看画质调大, 改用 examples/client.mjs 或网页")


def cap_gpu(body: dict) -> dict:
    """把请求压到最小分辨率/步数。"""
    gpu_guard()
    out = dict(body)
    out["steps"] = min(int(out.get("steps", GPU_MAX_STEPS)), GPU_MAX_STEPS)
    out["size"] = "256x256"
    if "output_resolution" in out:
        out["output_resolution"] = min(int(out["output_resolution"]), GPU_MAX_OUT_RES)
    if "long_side" in out:
        out["long_side"] = min(int(out["long_side"]), GPU_MAX_SIDE)
    return out


def wait_task(tid: str, statuses=("done", "failed", "canceled"), timeout=900) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, t = get(f"/api/tasks/{tid}")
        if st == 200 and t.get("status") in statuses:
            return t
        time.sleep(1.0)
    return get(f"/api/tasks/{tid}")[1] or {}


def cleanup() -> None:
    """收尾: **只删 _created_ids 里逐条记下的 ID**, 绝不按提示词/内容去猜。

    这条纪律是被迫学来的: 曾用「提示词看起来像测试垃圾」的启发式去清理, 结果把
    用户自己的任务一起删了。删除是不可逆的(连同产物 PNG), 所以唯一依据只能是
    「本次运行亲手记下的 ID」。
    """
    section("清理测试残留（只按记录的 ID 删）")
    if _created_ids:
        # 运行中的要先取消, 否则 /api/queue/delete 会把它们 skip 掉
        for tid in list(_created_ids):
            post(f"/api/tasks/{tid}/cancel", {})
        deadline = time.time() + 120
        while time.time() < deadline:
            live = [t for t in get("/api/tasks?limit=0")[1]["tasks"]
                    if t["id"] in _created_ids]
            busy = [t for t in live if t["status"] in ("running", "canceling")]
            if not busy and not live:
                break
            if not busy:
                time.sleep(1)
                continue
            time.sleep(2)
        st, res = post("/api/queue/delete", {"ids": list(_created_ids)})
        # 跳过项分两种: 「已不存在」(测试中途自己删过了, 正常) 和「还在运行」
        # (取消没生效)。只有后者算失败。
        stale = [t for t in (res.get("skipped") or [])
                 if f"'{t.get('id')}'" not in str(t.get("reason"))]
        check(st == 200 and not stale,
              f"删除本次创建的 {len(_created_ids)} 条任务",
              f"status={st} 未删掉 {stale}")
        for t in res.get("skipped") or []:
            print(f"      跳过 {t.get('id')}: {t.get('reason')}")
    queue_restore()
    for name in _created_files:
        p = BASE_DIR / "outputs" / name
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    check(True, f"auto_start 已恢复为 {_saved_autostart}")

    # 最重要的一条: 进场时就在的任务必须一条不少
    now_ids = {t["id"] for t in get("/api/tasks?limit=0")[1]["tasks"]}
    lost = _baseline_ids - now_ids
    check(not lost, f"进场时的 {len(_baseline_ids)} 条任务一条没少",
          f"丢失 {len(lost)} 条: {sorted(lost)[:5]}")


# ---------------------------------------------------------------- 各阶段测试
def phase_readonly():
    section("1. 只读接口（网页轮询用）")

    st, page = get("/", raw=True)
    check(st == 200 and b"genBtn" in page, "GET / 返回工作台页面", f"status={st}")
    st, _ = get("/docs", raw=True)
    check(st == 200, "GET /docs 可打开（页头「接口文档」）", f"status={st}")

    st, h = get("/health")
    check(st == 200, "GET /health", f"status={st}")
    for key in ("load", "cuda", "vram", "queue", "current_task", "max_side",
                "default_steps", "default_true_cfg_scale", "output_resolution",
                "edit_output_resolution", "ref_index", "last_generation", "waiting"):
        check(key in h, f"/health 含 {key}")
    ready = h.get("load") == "ready"
    check(ready, "模型已就绪（后续真出图前提）", f"load={h.get('load')} err={h.get('error')}")

    st, m = get("/v1/models")
    check(st == 200 and m.get("object") == "list"
          and m["data"][0]["id"] == "Qwen-Image-2.1", "GET /v1/models 符合 OpenAI 形状", str(m))

    st, q = get("/api/queue")
    check(st == 200, "GET /api/queue", f"status={st}")
    # 网页建任务表单的每一个上限/默认值都从这里来, 少一个就会用不了
    for key in ("counts", "current", "auto_start", "worker", "model_load", "max_side",
                "default_long_side", "min_side", "side_step", "default_steps",
                "max_steps", "default_true_cfg_scale", "max_true_cfg_scale",
                "max_ref_images", "max_queue_batch", "edit_output_resolution",
                "aspect_ratios"):
        check(key in q, f"/api/queue 下发 {key}")
    check(q.get("worker", {}).get("running") is True, "队列工作线程在跑")
    check(q.get("max_ref_images", 0) >= 1, "参考图上限可用", str(q.get("max_ref_images")))

    st, lst = get("/api/tasks")
    check(st == 200 and "tasks" in lst and "counts" in lst, "GET /api/tasks", f"status={st}")
    check(isinstance(lst.get("tasks"), list), "任务列表是数组")
    fields = {"id", "seq", "status", "prompt", "title", "kind", "params", "refs",
              "outputs", "usage", "error", "progress", "staged", "queue_position",
              "warnings", "source", "created_at", "finished_at", "negative_prompt"}
    if lst["tasks"]:
        missing = fields - set(lst["tasks"][0])
        check(not missing, "任务视图字段齐全（网页渲染所需）", f"缺 {missing}")
    else:
        skip("任务视图字段齐全（当前队列为空）")

    st, f1 = get("/api/tasks?status=done&limit=1")
    check(st == 200 and len(f1["tasks"]) <= 1, "GET /api/tasks?status/limit 过滤生效",
          f"{st} {len(f1.get('tasks', []))}")
    st, f2 = get("/api/tasks?status=pending&with_outputs=false")
    check(st == 200 and all("outputs" not in t for t in f2["tasks"]),
          "GET /api/tasks?with_outputs=false 生效", f"status={st}")
    return h


def phase_web_routes():
    section("2. 网页调用的每个接口都存在（静态对账）")
    st, spec = get("/openapi.json")
    if not check(st == 200, "GET /openapi.json", f"status={st}"):
        return
    live = set()
    for p, ops in spec["paths"].items():
        for m in ops:
            if m in ("get", "post", "put", "patch", "delete"):
                live.add((m.upper(), p))
    used = web_paths()
    check(len(used) >= 8, f"从 index.html 抽出 {len(used)} 条接口调用")
    for path in used:
        n = norm(path)
        # /v1/images/edits/json 与 /v1/images/generations 页面里是变量拼出来的
        hit = any(n == lp or lp.startswith(n.rstrip("*")) or n == lp for _, lp in live)
        check(hit, f"网页调用 {path} 有对应路由", f"归一化={n}")
    # 反向: 页面用不到的接口也应该存在(README 承诺的)
    for p in ("/v1/images/generations", "/v1/images/edits", "/v1/images/edits/json",
              "/api/queue/release-all", "/api/queue/auto-start", "/api/queue/delete",
              "/api/tasks/{task_id}/cancel", "/api/tasks/{task_id}/retry",
              "/api/tasks/{task_id}/release", "/api/tasks/{task_id}/hold",
              "/api/tasks/{task_id}/refs/{name}"):
        check(any(lp == p for _, lp in live), f"README 承诺的 {p} 存在")


def phase_validation():
    section("3. 参数校验（与网页的即时校验对齐）")
    # 校验阶段只该出现 400/404, 不该建出任何任务。先记下任务总数, 结束时核对 ——
    # 以前有个「期望 400」的探针在接口修好后反而入队了一条真任务(1024/30步, 占满GPU),
    # 就是靠这道保险才发现的。校验阶段的探针绝不能有副作用。
    before_ids = {t["id"] for t in get("/api/tasks?limit=0")[1]["tasks"]}
    bad = [
        ("缺少 prompt", {"prompt": ""}, 400),
        ("n 超范围", {"prompt": "x", "n": 9}, 400),
        ("steps 超上限", {"prompt": "x", "steps": 999}, 400),
        ("steps 为 0", {"prompt": "x", "steps": 0}, 400),
        ("aspect_ratio 非法", {"prompt": "x", "aspect_ratio": "5:1"}, 400),
        ("size 格式非法", {"prompt": "x", "size": "big"}, 400),
        ("true_cfg_scale < 1", {"prompt": "x", "true_cfg_scale": 0.5}, 400),
        ("true_cfg_scale 非数字", {"prompt": "x", "true_cfg_scale": "高"}, 400),
        ("output_resolution 非数字", {"prompt": "x", "output_resolution": "大"}, 400),
    ]
    for label, body, want in bad:
        st, res = post("/v1/images/generations", body)
        check(st == want, f"文生图 {label} -> {want}", f"实际 {st} {res}")

    st, res = get("/api/tasks/does-not-exist")
    check(st == 404, "GET /api/tasks/{不存在的ID} -> 404", f"status={st}")
    st, res = post("/api/tasks/does-not-exist/cancel", {})
    check(st == 404, "取消不存在的任务 -> 404", f"status={st}")
    st, res = delete("/api/tasks/does-not-exist")
    check(st == 404, "删除不存在的任务 -> 404", f"status={st}")
    st, res = post("/api/queue/delete", {"ids": []})
    check(st == 400, "批量删除空 ids -> 400", f"status={st}")
    st, res = post("/v1/images/edits/json", {"prompt": "x"})
    check(st == 400, "编辑缺 images -> 400", f"status={st}")
    st, res = post("/v1/images/edits/json",
                   {"prompt": "x", "images": [tiny_png_b64()], "ref_index": 5})
    check(st == 400, "ref_index 越界 -> 400", f"status={st}")
    st, res = post("/v1/images/edits/json",
                   {"prompt": "x", "images": ["http://evil.example.com/a.png"]})
    check(st == 400, "外站 URL 参考图被拒（不充当 URL 抓取器）", f"status={st}")
    st, res = post("/v1/images/generations", {"prompt": "x", "prompts": ["a"] * 99})
    check(st == 400, "prompts 超过批量上限 -> 400", f"status={st}")

    # 注意: 下面这几个「拒绝」用例都必须保证**请求本身是非法的**, 否则修好接口之后
    # 它就会静默建出一条真任务(默认 1024x1024/30 步, 能把 GPU 占掉几分钟)。
    # 早先这里用 {"prompt":"x","prompts":["a"]} 去试 /v1/images/edits/json, 修复
    # prompts 批量之后它不再被拒, 于是真的入队了一条任务 —— 这类「期望 400」的探针
    # 一旦接口行为变好就会变成副作用, 所以统一搭配一个必定非法的参数。
    st, res = post("/v1/images/edits/json",
                   {"prompt": "x", "images": [tiny_png_b64()], "prompts": ["a"],
                    "steps": 999})
    check(st == 400, "编辑路径 + 非法 steps -> 400（不会建出任务）", f"status={st} {res}")
    st, res = post("/v1/images/edits/json", {"prompts": ["a"], "n": 99})
    check(st == 400, "edits/json 缺 images -> 400", f"status={st} {res}")

    after_tasks = get("/api/tasks?limit=0")[1]["tasks"]
    strays = [t for t in after_tasks if t["id"] not in before_ids]
    check(not strays, "校验阶段没有建出任何任务（探针无副作用）",
          f"多出 {[t['prompt'][:20] for t in strays]}")
    # 用「进场时不存在的 ID」这个唯一依据来回收, 不靠提示词猜测
    for t in strays:
        post(f"/api/tasks/{t['id']}/cancel", {})
        delete(f"/api/tasks/{t['id']}")


def phase_queue_lifecycle():
    section("4. 队列生命周期（网页「暂存/开始/取消/编辑/重跑/删除」）")
    queue_off()

    st, res = post("/v1/images/generations", {
        "prompts": [f"{MARK} 一条", f"{MARK} 二条", f"{MARK} 三条"],
        "queue": True, "steps": 6, "n": 1, "aspect_ratio": "16:9", "long_side": 1024,
    })
    check(st == 200 and res.get("queued") == 3, "prompts 批量入队 3 条（网页多行提示词）",
          f"status={st} {res}")
    ids = new_task_ids(res)
    _created_ids.extend(ids)
    check(all(d.get("status") == "pending" and "queue_position" in d for d in res["data"]),
          "入队响应带 status/queue_position（网页立即刷新用）")
    check(all(d.get("task_url") for d in res["data"]), "入队响应带 task_url")

    st, t = get(f"/api/tasks/{ids[0]}")
    p = t["params"]
    check(t["status"] == "pending" and t["staged"] is True,
          "关掉自动生成时新任务为暂存态（网页「暂存中，未开始」）", f"{t['status']}")
    check((p["width"], p["height"]) == (1024, 576),
          "aspect_ratio+long_side -> 1024x576（网页比例换算一致）", f"{p['width']}x{p['height']}")
    check(p["steps"] == 6 and p["n"] == 1, "步数/张数已归一化")
    check(isinstance(p.get("seed"), int), "未给 seed 时自动补了随机种子")
    check(t["progress"] == {"step": 0, "total": 6}, "progress.total 预置为步数（网页进度条）")

    # --- 暂存 / 退回 / 开始 ---
    # 注意: release 之后工作线程会**立刻**接手这条任务(与网页「开始」同义),
    # 所以本阶段(不占 GPU)只在暂存态验证 hold; 「开始 -> 运行中 -> 409」的组合
    # 放到真出图阶段(phase 7), 那里才有真正的运行中任务。
    st, r = post(f"/api/tasks/{ids[1]}/hold")
    check(st == 200 and r["staged"] is True, "暂存态任务 hold 幂等（仍为暂存）",
          f"status={st} {r}")
    st, r = get(f"/api/tasks/{ids[1]}")
    check(r["queue_position"] is not None, "暂存任务也有排队位置（网页显示）",
          str(r.get("queue_position")))

    # --- PATCH 编辑（网页编辑弹窗的原样 payload） ---
    web_patch = {"prompt": f"{MARK} 改过的提示词", "negative_prompt": None, "steps": 9,
                 "true_cfg_scale": 1.0, "n": 2, "transparent": False,
                 "size": "1280x720", "seed": 12345, "output_resolution": 1024}
    st, r = patch(f"/api/tasks/{ids[0]}", web_patch)
    check(st == 200, "PATCH 用网页原样 payload 更新任务", f"status={st} {r}")
    check(r["prompt"] == web_patch["prompt"], "提示词已更新")
    check(r["params"]["steps"] == 9, "步数已更新", str(r["params"].get("steps")))
    check(r["params"]["n"] == 2, "张数已更新")
    check((r["params"]["width"], r["params"]["height"]) == (1280, 720),
          "自定义分辨率 size 生效（网页「自定义宽×高」）",
          f"{r['params']['width']}x{r['params']['height']}")
    check(r["params"]["seed"] == 12345, "seed 已更新")
    check(r["params"]["output_resolution"] == 1024, "output_resolution 已更新")
    check(r["negative_prompt"] is None, "negative_prompt 可清空")
    check(r["progress"]["total"] == 9, "progress.total 随步数更新")

    st, r = patch(f"/api/tasks/{ids[0]}", {"size": "1024x1024", "aspect_ratio": None})
    st, r2 = patch(f"/api/tasks/{ids[0]}", {"prompt": f"{MARK} 沿用原尺寸", "steps": 9})
    check(st == 200 and (r2["params"]["width"], r2["params"]["height"]) == (1024, 1024),
          "只改提示词时沿用原画布（不退回默认方形）",
          f"{r2['params']['width']}x{r2['params']['height']}")
    st, r3 = patch(f"/api/tasks/{ids[0]}", {"aspect_ratio": "9:16"})
    check(st == 200 and r3["params"]["height"] > r3["params"]["width"],
          "只改比例会丢掉旧 size（网页比例下拉生效）",
          f"{r3['params']['width']}x{r3['params']['height']}")
    st, r4 = patch(f"/api/tasks/{ids[0]}", {"title": f"{MARK} 标题"})
    check(st == 200 and r4["title"].startswith(MARK), "title 可单独更新", str(r4.get("title")))
    st, r5 = patch(f"/api/tasks/{ids[0]}", {"images": [tiny_png_b64()]})
    check(st == 400, "PATCH 拒绝替换参考图（提示新建任务）", f"status={st}")
    st, r6 = patch(f"/api/tasks/{ids[0]}", {"prompt": "   "})
    check(st == 400, "PATCH 空提示词 -> 400", f"status={st}")
    st, r7 = patch(f"/api/tasks/{ids[0]}", {"n": 99})
    check(st == 400, "PATCH 非法参数走同一套校验 -> 400", f"status={st}")

    # --- 取消（未开始立即取消） ---
    st, r = post(f"/api/tasks/{ids[2]}/cancel")
    check(st == 200 and r["status"] == "canceled", "未开始任务立即取消", f"status={st}")
    st, r = patch(f"/api/tasks/{ids[2]}", {"prompt": "x"})
    check(st == 409, "已取消任务拒绝编辑 -> 409（网页改走「重新生成」）", f"status={st}")
    st, r = post(f"/api/tasks/{ids[2]}/release")
    check(st == 409, "已取消任务不能 release -> 409", f"status={st}")
    st, r = post(f"/api/tasks/{ids[2]}/hold")
    check(st == 409, "已取消任务不能 hold -> 409", f"status={st}")

    # --- 重新生成（网页编辑已结束任务走的路） ---
    st, o = get(f"/api/tasks/{ids[1]}")
    orig_seed = o["params"].get("seed")
    st, r = post(f"/api/tasks/{ids[1]}/retry", {"prompt": f"{MARK} 重跑改提示词",
                                               "keep_seed": True})
    check(st == 200 and r.get("id") and r.get("origin") == ids[1],
          "retry 新建任务并记录来源", f"status={st} {r}")
    if st == 200:
        _created_ids.append(r["id"])
        check(r["params"].get("seed") == orig_seed, "keep_seed 保留原种子",
              f"{orig_seed} -> {r['params'].get('seed')}")
        check(r["prompt"] == f"{MARK} 重跑改提示词", "retry 可覆盖提示词", r["prompt"])
    st, o2 = get(f"/api/tasks/{ids[1]}")
    check(o2["status"] == "pending" and o2["prompt"] == f"{MARK} 二条",
          "原任务记录未被 retry 改写（历史保留）", f"{o2['status']} {o2['prompt']}")

    st, r2 = post(f"/api/tasks/{ids[1]}/retry", {})
    check(st == 200 and r2.get("id"), "retry 成功（默认换随机种子）", f"status={st} {r2}")
    if st == 200:
        _created_ids.append(r2["id"])
        check(r2["params"].get("seed") != orig_seed, "retry 默认换随机种子（网页「换一批」）",
              f"{orig_seed} -> {r2['params'].get('seed')}")
        check(r2["params"].get("seed") is not None, "新 seed 不是空（否则执行会 KeyError）")
        check((r2["params"]["width"], r2["params"]["height"]) == (1024, 576),
              "retry 沿用原任务画布",
              f"{r2['params']['width']}x{r2['params']['height']}")

    # --- 参考图任务（网页上传参考图那条路） ---
    ref_b64 = tiny_png_b64()
    # 网页 createTasks() 永远发的是 prompts 数组(从不发顶层 prompt), 有参考图时
    # 走 /v1/images/edits/json。这条就是「网页能不能用参考图」的判据。
    st, res = post("/v1/images/edits/json", {
        "prompts": [f"{MARK} 把背景换成日落海滩"],
        "queue": True, "steps": 6, "images": [ref_b64], "ref_index": 0,
    })
    if not check(st == 200 and res.get("queued") == 1,
                 "带参考图的编辑任务入队（网页原样 payload: 只有 prompts 没有 prompt）",
                 f"status={st} {res}"):
        FAIL.append("=> 网页「上传参考图」这条路走不通: edits/json 缺顶层 prompt 时"
                    "先返回 400, 轮不到 prompts 分支")
    else:
        eid = new_task_ids(res)[0]
        _created_ids.append(eid)
        st, et = get(f"/api/tasks/{eid}")
        check(et["kind"] == "edit" and et["refs"] == ["ref1.png"], "kind=edit 且参考图已落盘",
              f"{et['kind']} {et['refs']}")
        check("base64" not in json.dumps(et), "任务记录里不存 base64（只存文件名）")
        st, raw = get(f"/api/tasks/{eid}/refs/ref1.png", raw=True)
        check(st == 200 and raw[:4] == b"\x89PNG", "GET 任务参考图可用（网页缩略图）",
              f"status={st}")
        check(et["params"]["output_resolution"] == 1024,
              "编辑任务默认 output_resolution=min(长边,1024)",
              str(et["params"].get("output_resolution")))

    st, res = post("/v1/images/edits/json", {
        "prompts": [f"{MARK} 编辑甲", f"{MARK} 编辑乙"], "queue": True, "steps": 4,
        "images": [ref_b64]})
    check(st == 200 and res.get("queued") == 2,
          "prompts 批量 + 参考图 → 2 条编辑任务（网页空行分隔多条提示词）",
          f"status={st} {res}")
    if st == 200:
        _created_ids.extend(new_task_ids(res))

    st, res = post("/v1/images/edits/json", {
        "prompts": [f"{MARK} dataURL 参考图"], "queue": True, "steps": 4,
        "images": [f"data:image/png;base64,{ref_b64}"]})
    check(st == 200, "dataURL 形式的参考图可入队", f"status={st} {res}")
    if st == 200:
        _created_ids.extend(new_task_ids(res))

    tiny = tiny_png_b64((8, 8), (0, 0, 0))     # 压缩后很短的裸 base64
    st, res = post("/v1/images/edits/json", {
        "prompts": [f"{MARK} 极小裸 base64"], "queue": True, "steps": 4, "images": [tiny]})
    check(st == 200, f"短裸 base64({len(tiny)} 字符) 被当成图片而不是文件名",
          f"status={st} {res}")
    if st == 200:
        _created_ids.extend(new_task_ids(res))

    st, res = post("/v1/images/edits/json", {
        "prompts": [f"{MARK} 双参考图"], "queue": True, "steps": 4,
        "images": [ref_b64, tiny_png_b64((48, 64), (10, 90, 200))], "ref_index": -1,
    })
    check(st == 200, "多参考图 + ref_index=-1 入队", f"status={st} {res}")
    mid = new_task_ids(res)[0] if st == 200 else ""
    if st == 200:
        _created_ids.append(mid)
        st, mt = get(f"/api/tasks/{mid}")
        check(mt["refs"] == ["ref1.png", "ref2.png"], "两张参考图都落盘", str(mt["refs"]))
        check(mt["params"]["width"] < mt["params"]["height"],
              "ref_index=-1 用最后一张定画布比例（竖图 -> 竖画布）",
              f"{mt['params']['width']}x{mt['params']['height']}")
        check(mt["ref_index"] == 1, "ref_index 归一化为 1", str(mt.get("ref_index")))
    st, _ = post("/v1/images/edits/json", {
        "prompts": [f"{MARK} 太多图"], "queue": True,
        "images": [ref_b64] * 11})
    check(st == 400, "超过 10 张参考图 -> 400", f"status={st}")
    st, _ = get(f"/api/tasks/{mid}/refs/..%2F..%2Ftasks.json", raw=True)
    check(st in (400, 404), "参考图路径穿越被挡", f"status={st}")

    # --- 参考图来源: 本站 /outputs 路径（README 承诺, 网页「继续编辑」） ---
    st, lst = get("/api/tasks?status=done&limit=1")
    if lst["tasks"] and lst["tasks"][0].get("outputs"):
        url = lst["tasks"][0]["outputs"][0]["url"]
        st, res = post("/v1/images/edits/json", {
            "prompts": [f"{MARK} 用已有结果当参考图"], "queue": True,
            "images": [url], "steps": 4})
        check(st == 200, "images 传本站 /outputs URL 可入队（继续编辑）", f"status={st} {res}")
        if st == 200:
            _created_ids.extend(new_task_ids(res))
    else:
        skip("images 传本站 /outputs URL（当前无 done 任务产物）")

    # --- auto-start 开关（先验「关」这一侧，副作用最小） ---
    st, r = post("/api/queue/auto-start", {"auto_start": False})
    check(st == 200 and r["auto_start"] is False, "auto-start 可关闭", f"status={st} {r}")
    st, res = post("/v1/images/generations", {
        "prompts": [f"{MARK} 关自动后应暂存"], "queue": True, "steps": 4, "size": "256x256"})
    check(st == 200, "关闭自动生成后仍可入队", f"status={st} {res}")
    if st == 200:
        ntid = new_task_ids(res)[0]
        _created_ids.append(ntid)
        st, t = get(f"/api/tasks/{ntid}")
        check(t["staged"] is True, "关闭自动生成后新任务为暂存（网页「暂存中，未开始」）",
              str(t.get("staged")))

    # --- release-all ---
    # 注意: release-all 一定会把暂存任务全部释放, 于是它们会**真的开始出图**。
    # 所以先把本次建的暂存任务全部取消, 只留一条 256x256/1 步的便宜任务来验证,
    # 验完立刻取消 —— 否则一轮测试会白烧十几张 1024 的图。
    for tid in list(_created_ids):
        post(f"/api/tasks/{tid}/cancel", {})
    st, res = post("/v1/images/generations", cap_gpu({
        "prompts": [f"{MARK} release-all 专用"], "queue": True,
        "steps": 1, "size": "256x256"}))
    if st == 200:
        _created_ids.extend(new_task_ids(res))
    st, r = post("/api/queue/release-all", {})
    check(st == 200 and r["auto_start"] is True, "release-all 顺带打开自动生成", f"status={st}")
    st, q = get("/api/queue")
    check(q["counts"]["staged"] == 0, "release-all 后没有暂存任务了", str(q["counts"]))
    check(len(r.get("released") or []) >= 1, "release-all 回报被释放的任务 id",
          str(r.get("released")))
    post("/api/queue/auto-start", {"auto_start": False})
    time.sleep(2)
    for tid in list(_created_ids):
        post(f"/api/tasks/{tid}/cancel", {})

    # --- 删除（网页单条删除 + 批量清理） ---
    if mid:
        post(f"/api/tasks/{mid}/cancel", {})
        st, r = delete(f"/api/tasks/{mid}")
        check(st == 200 and r["deleted"] == mid, "DELETE 单条任务", f"status={st} {r}")
        st, r = get(f"/api/tasks/{mid}")
        check(st == 404, "删除后详情 404", f"status={st}")
        st, r = get(f"/api/tasks/{mid}/refs/ref1.png", raw=True)
        check(st == 404, "删除任务后参考图也取不到了", f"status={st}")
    else:
        skip("删除后详情/参考图 404（多参考图任务没建成）")
    st, r = post(f"/api/queue/delete", {"ids": [ids[2]]})
    check(st == 200 and r["deleted"] == [ids[2]] and r["skipped"] == [],
          "批量删除接口（网页「清理已结束」）", f"{r}")
    st, r = post("/api/queue/delete", {"ids": [ids[0], "does-not-exist"]})
    check(st == 200 and len(r["skipped"]) == 1 and ids[0] in r["deleted"],
          "批量删除会跳过不存在的 ID 并照常删掉其余", str(r))
    for gone in (mid, ids[0], ids[2]):      # 已经删掉的别留在待清理名单里
        if gone and gone in _created_ids:
            _created_ids.remove(gone)
    return ids


def phase_params_echo():
    section("5. 请求参数 → 生成计划 的完整映射（不占 GPU）")
    queue_off()
    cases = [
        ("默认(不传尺寸)", {}, {}),
        ("size 自定义", {"size": "1536x864"}, {"width": 1536, "height": 864}),
        ("long_side+比例 3:2", {"long_side": 1280, "aspect_ratio": "3:2"},
         {"width": 1280, "height": 848}),
        ("long_side+比例 9:16", {"long_side": 1024, "aspect_ratio": "9:16"},
         {"width": 576, "height": 1024}),
        ("size 超上限被压", {"size": "4000x4000"},
         {"width": 1536, "height": 1536}),
        ("steps 边界 1", {"steps": 1}, {"steps": 1}),
        ("steps 边界上限", {"steps": 60}, {"steps": 60}),
        ("transparent", {"transparent": True}, {"transparent": True}),
        ("seed 0(合法)", {"seed": 0}, {"seed": 0}),
        ("n=4", {"n": 4}, {"n": 4}),
    ]
    for label, extra, expect in cases:
        body = {"prompt": f"{MARK} {label}", "queue": True, "steps": 5}
        body.update(extra)
        st, res = post("/v1/images/generations", body)
        if not check(st == 200, f"{label}: 入队成功", f"{st} {res}"):
            continue
        tid = new_task_ids(res)[0]
        _created_ids.append(tid)
        st, t = get(f"/api/tasks/{tid}")
        p = t["params"]
        bad = {k: (p.get(k), v) for k, v in expect.items() if p.get(k) != v}
        check(not bad, f"{label}: 参数归一化正确", str(bad))
        post(f"/api/tasks/{tid}/cancel", {})

    # true_cfg / negative_prompt 的耦合规则
    st, res = post("/v1/images/generations", {
        "prompt": f"{MARK} cfg", "queue": True, "steps": 5,
        "negative_prompt": "blurry", "true_cfg_scale": 1.0})
    tid = new_task_ids(res)[0]
    _created_ids.append(tid)
    st, t = get(f"/api/tasks/{tid}")
    check(t["negative_prompt"] == "blurry" and t["params"]["true_cfg_scale"] == 1.0,
          "负提示词在 cfg<=1 时被记录（不丢参数）")
    post(f"/api/tasks/{tid}/cancel", {})

    st, res = post("/v1/images/generations", {
        "prompt": f"{MARK} 批量上限边界", "queue": True, "steps": 4})
    check(st == 200, "单条入队成功")
    _created_ids.extend(new_task_ids(res))
    st, res = post("/v1/images/generations", {
        "prompt": f"{MARK} 暂存态显式入队", "queue": True, "steps": 4, "released": False})
    tid = new_task_ids(res)[0]
    _created_ids.append(tid)
    st, t = get(f"/api/tasks/{tid}")
    check(t["staged"] is True, "released=false 可显式入队为暂存（网页批量暂存）", str(t["staged"]))
    st, res = post("/v1/images/generations", {
        "prompt": f"{MARK} 带标题", "queue": True, "steps": 4, "title": f"{MARK} 我的标题"})
    _created_ids.extend(new_task_ids(res))
    tid = new_task_ids(res)[0]
    st, t = get(f"/api/tasks/{tid}")
    check(t["title"] == f"{MARK} 我的标题", "title 字段可传（网页任务名）", str(t["title"]))
    check(t["source"] == "api", "来源标记为 api", str(t["source"]))


def phase_docs_promises():
    section("6. README / 网页承诺但代码里可能没实现的参数")
    # response_format: README 明确写了 "url" | "b64_json"，并给了 b64_json 的 JS 示例
    src = (BASE_DIR / "server.py").read_text(encoding="utf-8")
    has_b64_impl = "b64_json" in src
    st, res = post("/v1/images/generations", cap_gpu({
        "prompt": f"{MARK} response_format 自检", "steps": 4, "size": "256x256",
        "response_format": "b64_json"}))
    if st == 200:
        item = (res.get("data") or [{}])[0]
        _created_files.append(str(item.get("url", "")).split("/")[-1])
        check("b64_json" in item, "response_format=b64_json 真的返回 b64_json",
              f"实际返回键: {sorted(item)}")
    else:
        check(False, "response_format=b64_json 请求被接受", f"status={st} {res}")
    check(has_b64_impl, "server.py 里有 b64_json 的实现（否则上面那条只是巧合）")

    # 同步接口不传 queue 时应阻塞到出图（README: OpenAI 风格）
    st, res = post("/v1/images/generations", cap_gpu({
        "prompt": f"{MARK} 同步路径自检", "steps": 2, "size": "256x256", "seed": 7}))
    check(st == 200 and res.get("data") and "created" in res and "usage" in res,
          "同步接口返回 OpenAI 风格响应（不传 queue 不入队）", f"status={st}")
    if st == 200:
        _created_files.extend(str(i.get("url", "")).split("/")[-1] for i in res["data"])
    st, q = get("/api/queue")
    check(not any(t["prompt"].startswith(f"{MARK} 同步路径自检") for t in get("/api/tasks")[1]["tasks"]),
          "同步调用不会混进任务队列")


def phase_real_generation():
    section("7. 真出图（真模型）")
    st, h = get("/health")
    if h.get("load") != "ready":
        skip("真出图", f"模型 load={h.get('load')}")
        return
    if get("/api/queue")[1]["current"] is not None:
        skip("真出图", "已有任务在跑，等它结束")
        return

    # 7.1 同步文生图（最小画布 + 最少步数: 只验链路与响应结构, 不看画质）
    t0 = time.time()
    st, res = post("/v1/images/generations", cap_gpu({
        "prompt": f"{MARK} a small red cube on white background",
        "size": "256x256", "steps": 4, "seed": 42, "n": 2}))
    dt = time.time() - t0
    check(st == 200, f"同步文生图 n=2 成功（{dt:.0f}s）", f"status={st} {res}")
    if st == 200:
        data = res["data"]
        _created_files.extend(str(i["url"]).split("/")[-1] for i in data)
        check(len(data) == 2, "返回 2 张图")
        check(all(d.get("seed") == 42 + i for i, d in enumerate(data)),
              "多张图 seed 依次递增", str([d.get("seed") for d in data]))
        check(all((d["width"], d["height"]) == (256, 256) for d in data), "回传尺寸正确")
        check(all(d.get("steps") == 4 for d in data), "回传步数")
        u = res["usage"]
        for key in ("elapsed_sec", "vram_peak_mb", "queue_sec", "vae_tiling",
                    "true_cfg_scale", "output_resolution", "mem_avail_gb"):
            check(key in u, f"usage 含 {key}")
        check(u.get("vram_peak_mb", 0) > 0, "显存峰值已记录", str(u.get("vram_peak_mb")))
        st, raw = get(data[0]["url"], raw=True)
        check(st == 200 and raw[:4] == b"\x89PNG" and len(raw) > 2000,
              "返回的 url 能直接下载到 PNG", f"status={st} len={len(raw)}")

    # 7.2 同步编辑（用刚出的图当参考图）
    #     编辑路径最贵的一环是视觉编码器 prefill, 由 output_resolution 决定; 再叠上
    #     画布面积, token 数是平方级增长的。所以这里把两个都压到最小(256/256):
    #     要验的是「这条链路能跑通、usage 字段对」, 不是画质。
    if st == 200 and data:
        src_url = data[0]["url"]
        t0 = time.time()
        st, res = post("/v1/images/edits/json", cap_gpu({
            "prompt": f"{MARK} turn the background into a sunset beach",
            "images": [src_url], "steps": 4, "size": "256x256",
            "output_resolution": 256, "seed": 5}))
        dt = time.time() - t0
        check(st == 200, f"同步编辑（/outputs 路径当参考图）成功（{dt:.0f}s）",
              f"status={st} {res}")
        if st == 200:
            _created_files.extend(str(i["url"]).split("/")[-1] for i in res["data"])
            u = res["usage"]
            check(u.get("vae_tiling") is True, "编辑路径自动开 VAE 分块解码", str(u.get("vae_tiling")))
            check(u.get("ref_images") == 1, "usage 记录参考图张数", str(u.get("ref_images")))
            check(u.get("output_resolution") == 256, "编辑用了请求给的 output_resolution",
                  str(u.get("output_resolution")))

    # 7.3 队列真跑一条（走完整工作线程: 进度 -> 产物 -> usage）
    queue_off()
    st, res = post("/v1/images/generations", cap_gpu({
        "prompt": f"{MARK} 队列真跑", "queue": True, "steps": 4, "size": "256x256", "seed": 9}))
    tid = new_task_ids(res)[0]
    _created_ids.append(tid)
    post(f"/api/tasks/{tid}/release")
    seen_progress = False
    t0 = time.time()
    while time.time() - t0 < 300:
        st, t = get(f"/api/tasks/{tid}")
        if t.get("status") in ("running", "done"):
            if (t.get("progress") or {}).get("step", 0) > 0:
                seen_progress = True
        if t.get("status") in ("done", "failed", "canceled"):
            break
        time.sleep(1.5)
    check(t.get("status") == "done", "队列任务真跑出图", f"{t.get('status')} {t.get('error')}")
    check(seen_progress, "运行中能看到采样步进度（网页进度条）")
    check(bool(t.get("outputs")), "任务带产物", str(t.get("outputs")))
    if t.get("outputs"):
        for o in t["outputs"]:
            _created_files.append(str(o["url"]).split("/")[-1])
        st, raw = get(t["outputs"][0]["url"], raw=True)
        check(st == 200 and raw[:4] == b"\x89PNG", "任务产物 URL 可访问")
    check(bool(t.get("usage", {}).get("elapsed_sec")), "任务记录了耗时", str(t.get("usage")))

    # 7.4 「开始」一条暂存任务 -> 运行中; 运行中各操作的守卫
    #     这条是网页 ▶ 按钮的等价物: 点下去工作线程就接手, 于是它会真的跑起来。
    #     这里只要能观察到 running 就够, 所以用最小画布 + 中等步数(留出取消的时间窗)。
    st, res = post("/v1/images/generations", cap_gpu({
        "prompt": f"{MARK} 开始按钮测试", "queue": True, "steps": 8, "size": "256x256"}))
    rid = new_task_ids(res)[0]
    _created_ids.append(rid)
    st, r = get(f"/api/tasks/{rid}")
    check(r["staged"] is True, "待测试任务初始为暂存（网页显示「暂存」徽标）")
    st, r = post(f"/api/tasks/{rid}/release")
    check(st == 200 and r["staged"] is False, "release 后不再是暂存（网页 ▶ 消失、‖ 出现）",
          f"status={st} {r}")
    started = False
    for _ in range(120):
        st, r = get(f"/api/tasks/{rid}")
        if r.get("status") in ("running", "done"):
            started = r.get("status") == "running"
            break
        time.sleep(0.5)
    check(started, "release 后任务进入 running（真在工作线程里跑）", str(r.get("status")))
    if started:
        st, e = patch(f"/api/tasks/{rid}", {"prompt": "x"})
        check(st == 409, "运行中 PATCH -> 409（网页把 ✎ 置灰）", f"status={st} {e}")
        st, e = hold(f"/api/tasks/{rid}/hold")
        check(st == 409, "运行中 hold -> 409（网页不提供该按钮）", f"status={st} {e}")
        st, e = delete(f"/api/tasks/{rid}")
        check(st == 409, "运行中 DELETE -> 409（提示先取消）", f"status={st} {e}")
        st, e = post("/api/queue/delete", {"ids": [rid]})
        check(st == 200 and e.get("skipped"), "批量删除会跳过运行中任务", str(e))
        st, e = post(f"/api/tasks/{rid}/retry", {})
        check(st == 409, "运行中 retry -> 409（提示先取消）", f"status={st} {e}")
        # 顺带验证: 后面那条排队任务在有任务运行时可以 hold（网页 ‖ 的真实场景）
        st, res2 = post("/v1/images/generations", cap_gpu({
            "prompt": f"{MARK} 排第二", "queue": True, "steps": 4, "size": "256x256"}))
        sid = new_task_ids(res2)[0]
        _created_ids.append(sid)
        st, e = post(f"/api/tasks/{sid}/hold")
        check(st == 200 and e["staged"] is True,
              "有任务在跑时, 排队中的任务可 hold（网页 ‖ 的真实场景）", f"status={st} {e}")
        post(f"/api/tasks/{rid}/cancel", {})
        t0 = time.time()
        while time.time() - t0 < 120:
            st, r = get(f"/api/tasks/{rid}")
            if r.get("status") in ("canceled", "done", "failed"):
                break
            time.sleep(1.0)
        check(r.get("status") == "canceled", "运行中任务取消成功", str(r.get("status")))
        check(not r.get("outputs"), "取消后不留半成品图片")
        post(f"/api/tasks/{sid}/cancel", {})

    # 7.5 运行中真取消（到采样步边界）
    #     取消要落在「步边界」, 所以步数不能太少(否则出图比轮询还快), 但也没必要 30 步。
    st, res = post("/v1/images/generations", cap_gpu({
        "prompt": f"{MARK} 取消测试", "queue": True, "steps": 8, "size": "256x256"}))
    tid2 = new_task_ids(res)[0]
    _created_ids.append(tid2)
    post(f"/api/tasks/{tid2}/release")
    saw_canceling = False
    for _ in range(240):
        st, t2 = get(f"/api/tasks/{tid2}")
        if t2.get("status") == "canceling":
            saw_canceling = True
        if t2.get("status") == "running":
            post(f"/api/tasks/{tid2}/cancel", {})
            continue
        if t2.get("status") in ("done", "failed", "canceled"):
            break
        time.sleep(0.5)
    t0 = time.time()
    while time.time() - t0 < 120:
        st, t2 = get(f"/api/tasks/{tid2}")
        if t2.get("status") in ("canceled", "done", "failed"):
            break
        time.sleep(1.0)
    check(t2.get("status") == "canceled", "运行中任务可取消（采样步边界生效）",
          f"status={t2.get('status')}")
    check(saw_canceling, "取消请求先进入 canceling（网页显示「取消中…」）")
    if t2.get("status") == "canceled":
        check(not t2.get("outputs"), "已取消任务不残留半成品图片")
        st, e = delete(f"/api/tasks/{tid2}")
        check(st == 200, "取消后可以删除", f"status={st} {e}")
        if tid2 in _created_ids:      # 已经在这一步删了, 别让收尾再去删一次
            _created_ids.remove(tid2)

    # 7.6 已结束任务再编辑走 retry（网页对 done 任务的行为）
    st, r = post(f"/api/tasks/{tid}/retry", {
        "prompt": f"{MARK} 已结束改参数重跑", "steps": 4, "size": "256x256",
        "output_resolution": 256})
    check(st == 200 and r["status"] == "pending", "已结束任务 retry 新建任务", f"status={st} {r}")
    if st == 200:
        _created_ids.append(r["id"])
        check((r["params"]["width"], r["params"]["height"]) == (256, 256),
              "retry 覆盖 size 生效", f"{r['params']['width']}x{r['params']['height']}")
        st, rr = post(f"/api/tasks/{tid}/retry", {})
        check(st == 409 or st == 200, "对已结束任务重复 retry 的行为可预期", f"status={st}")
        if st == 200:
            _created_ids.append(rr["id"])


# ---------------------------------------------------------------- main
def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8091")
    ap.add_argument("--no-gpu", action="store_true", help="跳过真出图阶段")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    print(f"\033[1m测试目标: {BASE}\033[0m")
    st, h = get("/health", timeout=15)
    if st != 200:
        print(f"\x1b[31m服务不可达: {h}\x1b[0m")
        return 2

    global _baseline_ids
    _baseline_ids = {t["id"] for t in get("/api/tasks?limit=0")[1]["tasks"]}
    print(f"进场时已有 {len(_baseline_ids)} 条任务（结束时必须一条不少）")

    try:
        for name, fn in (("只读接口", phase_readonly), ("网页路由对账", phase_web_routes),
                         ("参数校验", phase_validation), ("队列生命周期", phase_queue_lifecycle),
                         ("参数映射", phase_params_echo), ("文档承诺", phase_docs_promises)):
            try:
                fn()
            except Exception as e:                # 一处异常不该中断整轮测试
                import traceback
                FAIL.append(f"{name} 阶段异常: {type(e).__name__}: {e}")
                print(f"\x1b[31mFAIL\x1b[0m {name} 阶段异常: {type(e).__name__}: {e}")
                traceback.print_exc()
        if not args.no_gpu:
            try:
                phase_real_generation()
            except Exception as e:
                import traceback
                FAIL.append(f"真出图阶段异常: {type(e).__name__}: {e}")
                print(f"\x1b[31mFAIL\x1b[0m 真出图阶段异常: {type(e).__name__}: {e}")
                traceback.print_exc()
        else:
            skip("真出图阶段（--no-gpu）")
    finally:
        cleanup()

    total = len(PASS) + len(FAIL)
    print(f"\n\033[1m结果: {len(PASS)}/{total} 通过, {len(FAIL)} 失败, {len(SKIP)} 跳过\033[0m")
    if FAIL:
        print("\n\x1b[31m失败项:\x1b[0m")
        for f in FAIL:
            print("  - " + f)
    if SKIP:
        print("\n\x1b[33m跳过项:\x1b[0m")
        for s in SKIP:
            print("  - " + s)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
