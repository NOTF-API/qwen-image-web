# -*- coding: utf-8 -*-
"""逐条核对 docs/API_FOR_AI.md 里的说法与真实服务是否一致。

文档给 AI 看, 一旦与实现不符, AI 就会照着错的调 —— 所以每个数字和结论都实测一遍。
只做入队/参数层面的验证, 不做真出图(除非 --gpu)。
"""
from __future__ import annotations

import base64
import io
import json
import sys

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091"
S = requests.Session()
OK, BAD = [], []


def chk(cond, label, extra=""):
    (OK if cond else BAD).append(label + (f"  [{extra}]" if extra else ""))
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{extra}]" if extra else ""))


def png_b64(w=64, h=48):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), (200, 40, 40)).save(b, format="PNG")
    return base64.b64encode(b.getvalue()).decode()


def mk(**body):
    body.setdefault("queue", True)
    return call("POST", "/v1/images/generations", body)


def plan_of(tid):
    return S.get(BASE + f"/api/tasks/{tid}", timeout=20).json()["params"]


def drop(*ids):
    for i in ids:
        S.post(BASE + f"/api/tasks/{i}/cancel", timeout=20)
    S.post(BASE + "/api/queue/delete", json={"ids": [i for i in ids if i]}, timeout=30)


def call(method, path, body=None, timeout=30):
    """统一返回 (status, json), 免得 requests 的单对象返回和本脚本的约定不一致。"""
    r = S.request(method, BASE + path, json=body, timeout=timeout)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"_text": r.text[:200]}


print("=== §3 尺寸优先级 ===")
cases = [
    ({"size": "640x480"}, 640, 480, "size"),
    ({"width": 640, "height": 480}, 640, 480, "width/height（文档新写法）"),
    ({"size": "640x480", "width": 100, "height": 100}, 640, 480, "size 优先于 width/height"),
    ({"aspect_ratio": "3:2", "long_side": 1280}, 1280, 848, "aspect_ratio+long_side"),
    ({"aspect_ratio": "9:16", "long_side": 1024}, 576, 1024, "9:16 竖图"),
    ({}, 1024, 1024, "都不给 -> 1024x1024"),
    ({"size": "4000x4000"}, 1536, 1536, "超上限自动缩到 1536"),
]
made = []
for extra, ew, eh, label in cases:
    st, r = mk(prompts=["[docchk] a"], **extra)
    if st != 200:
        chk(False, label, f"{st} {r}")
        continue
    tid = r["data"][0]["id"]
    made.append(tid)
    p = plan_of(tid)
    chk((p["width"], p["height"]) == (ew, eh), f"{label} -> {ew}x{eh}",
        f"实际 {p['width']}x{p['height']}")
    chk(p["width"] % 16 == 0 and p["height"] % 16 == 0, f"  {label} 对齐 16 倍数")
drop(*made)

print("\n=== §3 官方比例表 ===")
q = S.get(BASE + "/api/queue", timeout=20).json()
doc_ratios = ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16"]
chk(q.get("aspect_ratios") == doc_ratios, "§7 aspect_ratios 与文档一致",
    str(q.get("aspect_ratios")))

print("\n=== §7 /api/queue 下发的字段（文档 §7 列的都要有）===")
for k in ("counts", "current", "auto_start", "worker", "model_load", "max_side",
          "default_long_side", "min_side", "side_step", "default_steps", "max_steps",
          "default_true_cfg_scale", "max_true_cfg_scale", "max_ref_images",
          "max_queue_batch", "edit_output_resolution", "aspect_ratios"):
    chk(k in q, f"/api/queue 含 {k}")
chk(q.get("max_side") == 1536, "max_side=1536 与文档一致", str(q.get("max_side")))
chk(q.get("max_ref_images") == 10, "max_ref_images=10 与文档一致", str(q.get("max_ref_images")))
chk(q.get("max_queue_batch") == 16, "max_queue_batch=16 与文档一致", str(q.get("max_queue_batch")))
chk(q.get("default_steps") == 30, "default_steps=30 与文档一致", str(q.get("default_steps")))
chk(q.get("default_true_cfg_scale") == 1.0, "default_true_cfg_scale=1.0 与文档一致")

print("\n=== §1② 负提示词 + cfg ===")
st, r = mk(prompts=["[docchk] b"], negative_prompt="blurry", true_cfg_scale=1.0)
if st == 200:
    tid = r["data"][0]["id"]
    p = plan_of(tid)
    chk(p["true_cfg_scale"] == 1.0 and p["negative_prompt"] == "blurry",
        "cfg<=1 时负提示词被记录但不生效（文档说法）")
    drop(tid)
r = S.post(BASE + "/v1/images/generations",
           json={"prompt": "x", "negative_prompt": "b", "true_cfg_scale": 0.5}, timeout=30)
chk(r.status_code == 400, "true_cfg_scale<1 被拒 400（文档 §6）", r.status_code)

print("\n=== §1① 步数边界 ===")
r = S.post(BASE + "/v1/images/generations",
           json={"prompt": "x", "queue": True, "steps": 61}, timeout=30)
chk(r.status_code == 400, "steps>60 被拒（文档说上限 60）", r.status_code)

print("\n=== §3 n 范围 ===")
r = S.post(BASE + "/v1/images/generations",
           json={"prompt": "x", "queue": True, "n": 5}, timeout=30)
chk(r.status_code == 400, "n>4 被拒（文档说 1~4）", r.status_code)

print("\n=== §4 参考图上限与三种来源 ===")
r = S.post(BASE + "/v1/images/edits/json",
           json={"prompts": ["x"], "queue": True, "images": [png_b64()] * 11}, timeout=30)
chk(r.status_code == 400, "11 张参考图被拒（文档说最多 10）", r.status_code)

made = []
st, r = call("POST", "/v1/images/edits/json", {"prompts": ["[docchk] c"], "queue": True, "images": [png_b64()]}, timeout=30)
if st == 200:
    tid = r["data"][0]["id"]
    made.append(tid)
    t = S.get(BASE + f"/api/tasks/{tid}", timeout=20).json()
    chk(t["kind"] == "edit", "base64 参考图 -> kind=edit", t.get("kind"))
    chk(t["refs"] == ["ref1.png"], "参考图落盘为 ref1.png", str(t.get("refs")))
    chk(t["params"]["output_resolution"] == 1024,
        "output_resolution 默认 min(长边,1024)=1024（文档 §4）",
        str(t["params"]["output_resolution"]))
else:
    chk(False, "base64 参考图入队", f"{st} {r}")

st, r = call("POST", "/v1/images/edits/json", {"prompts": ["[docchk] d"], "queue": True,
                     "images": ["data:image/png;base64," + png_b64()]}, timeout=30)
chk(st == 200, "dataURL 参考图可入队（文档 §4）", f"{st} {r}")
if st == 200:
    made += [d["id"] for d in r["data"]]

st, r = call("POST", "/v1/images/edits/json", {"prompts": ["[docchk] e"], "queue": True, "images": [png_b64(8, 8)]}, timeout=30)
chk(st == 200, "小尺寸裸 base64 不被当文件名（文档 §4 的坑）", f"{st} {r}")
if st == 200:
    made += [d["id"] for d in r["data"]]
drop(*made)

# /outputs 路径
lst = S.get(BASE + "/api/tasks?status=done&limit=1", timeout=20).json()["tasks"]
if lst and lst[0].get("outputs"):
    url = lst[0]["outputs"][0]["url"]
    st, r = call("POST", "/v1/images/edits/json", {"prompts": ["[docchk] f"], "queue": True, "images": [url]}, timeout=30)
    chk(st == 200, "/outputs URL 作参考图可入队（文档 §4）", f"{st} {r}")
    if st == 200:
        drop(*[d["id"] for d in r["data"]])
else:
    print("  (跳过) 没有 done 任务产物可测 /outputs 路径")

print("\n=== §4 ref_index ===")
made = []
st, r = call("POST", "/v1/images/edits/json", {
    "prompts": ["[docchk] g"], "queue": True,
    "images": [png_b64(64, 48), png_b64(48, 64)]})
if st == 200:
    tid = r["data"][0]["id"]
    made.append(tid)
    p = plan_of(tid)
    chk(p["width"] > p["height"], "默认 ref_index=0 -> 跟随第 1 张(横向)",
        f"{p['width']}x{p['height']}")
st, r = call("POST", "/v1/images/edits/json", {
    "prompts": ["[docchk] h"], "queue": True, "ref_index": -1,
    "images": [png_b64(64, 48), png_b64(48, 64)]})
if st == 200:
    tid = r["data"][0]["id"]
    made.append(tid)
    p = plan_of(tid)
    chk(p["width"] < p["height"], "ref_index=-1 -> 跟随最后一张(纵向)",
        f"{p['width']}x{p['height']}")
drop(*made)

print("\n=== §5 队列接口与状态流转 ===")
st, r = mk(prompts=["[docchk] i"], steps=4, size="256x256")
tid = r["data"][0]["id"]
t = S.get(BASE + f"/api/tasks/{tid}", timeout=20).json()
chk(t["status"] == "pending", "入队后 status=pending（文档 §5）", t["status"])
chk("staged" in t and "queue_position" in t, "含 staged / queue_position（文档 §5）")
chk(t["progress"]["total"] == 4, "progress.total 预置为步数（文档 §5）")
chk(set(("id", "seq", "status", "kind", "prompt", "refs", "staged", "queue_position",
         "progress", "outputs", "usage", "warnings", "error")) <= set(t),
    "任务视图含文档列出的全部字段")
st, r = call("POST", f"/api/tasks/{tid}/cancel", timeout=20)
chk(r["status"] == "canceled", "未开始任务立即取消（文档 §5）", r.get("status"))
st, r = call("PATCH", f"/api/tasks/{tid}", {"prompt": "x"}, timeout=20)
chk(st == 409, "已结束任务 PATCH -> 409（文档 §5/§6）", f"{st}")
st, r = call("POST", f"/api/tasks/{tid}/retry", {}, timeout=30)
chk(st == 200, "retry 新建任务（文档 §5）", f"{st} {r}")
if st == 200:
    new = r
    chk(new.get("origin") == tid, "retry 记录来源 origin（文档 §5）", str(new.get("origin")))
    drop(new["id"])
drop(tid)

print("\n=== §5 批量 prompts ===")
st, r = mk(prompts=[f"[docchk] j{i}" for i in range(3)], size="256x256")
chk(st == 200 and r.get("queued") == 3, "prompts 3 条 -> queued=3（文档 §5）",
    f"{st} {r.get('queued')}")
if st == 200:
    drop(*[d["id"] for d in r["data"]])
st, r = mk(prompts=[f"[docchk] k{i}" for i in range(17)], size="256x256")
chk(st == 400, "prompts 超 16 条被拒（文档 §5）", f"{st}")

print("\n=== §5 暂存 / release / hold ===")
S.post(BASE + "/api/queue/auto-start", json={"auto_start": False}, timeout=20)
st, r = mk(prompts=["[docchk] l"], size="256x256")
tid = r["data"][0]["id"]
t = S.get(BASE + f"/api/tasks/{tid}", timeout=20).json()
chk(t["staged"] is True, "auto_start 关闭时新任务为暂存（文档 §5）", str(t["staged"]))
st, r = call("POST", f"/api/tasks/{tid}/hold", timeout=20)
chk(st == 200 and r["staged"] is True, "hold 退回暂存", f"{st}")
st, r = call("POST", f"/api/tasks/{tid}/release", timeout=20)
chk(st == 200 and r["staged"] is False, "release 开始任务", f"{st}")
S.post(BASE + f"/api/tasks/{tid}/cancel", timeout=20)
st, r = call("POST", f"/api/tasks/{tid}/release", timeout=20)
chk(st == 409, "已取消任务 release -> 409（文档 §6）", f"{st}")
drop(tid)
S.post(BASE + "/api/queue/auto-start", json={"auto_start": True}, timeout=20)

print("\n=== §6 错误响应结构 ===")
for label, path, method, body, want in [
    ("缺 prompt", "/v1/images/generations", "POST", {"queue": True}, 400),
    ("非法 aspect_ratio", "/v1/images/generations", "POST",
     {"prompt": "x", "queue": True, "aspect_ratio": "5:4"}, 400),
    ("size 格式错", "/v1/images/generations", "POST",
     {"prompt": "x", "queue": True, "size": "big"}, 400),
    ("任务不存在", "/api/tasks/nope", "GET", None, 404),
    ("取消不存在任务", "/api/tasks/nope/cancel", "POST", {}, 404),
    ("批量删除空 ids", "/api/queue/delete", "POST", {"ids": []}, 400),
    ("edits 缺 images", "/v1/images/edits/json", "POST", {"prompt": "x"}, 400),
    ("非法 response_format", "/v1/images/generations", "POST",
     {"prompt": "x", "response_format": "raw"}, 400),
]:
    if method == "GET":
        r = S.get(BASE + path, timeout=20)
    else:
        r = S.request(method, BASE + path, json=body, timeout=20)
    chk(r.status_code == want, f"{label} -> {want}（文档 §6）", f"实际 {r.status_code}")
    try:
        d = r.json()
        chk("detail" in d, f"  {label} 响应含 detail 字段（文档 §6）", str(d)[:60])
    except Exception:
        chk(False, f"  {label} 响应不是 JSON")

print("\n=== §7 其它只读接口 ===")
for path, must in [("/health", ("load", "cuda", "gpu", "vram", "queue", "current_task")),
                   ("/v1/models", ("data",)),
                   ("/api", ("endpoints", "notes"))]:
    d = S.get(BASE + path, timeout=20).json()
    chk(all(m in d for m in must), f"GET {path} 含文档所述字段")
h = S.get(BASE + "/health", timeout=20).json()
chk(h["load"] in ("idle", "loading", "ready", "error"), "load 取值与文档一致", h["load"])
m = S.get(BASE + "/v1/models", timeout=20).json()
chk(m["data"][0]["id"] == "Qwen-Image-2.1", "模型 id 与文档一致", m["data"][0]["id"])
r = S.get(BASE + "/docs", timeout=20)
chk(r.status_code == 200, "GET /docs 可用（文档 §7）", f"{r.status_code}")

print("\n=== §9 CORS ===")
r = S.options(BASE + "/v1/images/generations", timeout=20, headers={
    "Origin": "http://anywhere.example", "Access-Control-Request-Method": "POST"})
chk(r.headers.get("access-control-allow-origin") == "*",
    "CORS 全开（文档 §9 已知限制）", str(r.headers.get("access-control-allow-origin")))

print(f"\n结果: {len(OK)}/{len(OK) + len(BAD)} 一致")
if BAD:
    print("不一致:")
    for b in BAD:
        print("  - " + b)
sys.exit(1 if BAD else 0)
