# -*- coding: utf-8 -*-
"""直接验证「offload 状态损坏 → 自动重建 → 重试成功」这条控制流。

为什么不用真 OOM: 触发一次真 OOM 要跑 1536² 的编辑路径, 动辄十几分钟, 而且
不一定每次都触发(1536² 文生图在 Q4 档位其实能跑完)。而这里要验的是**恢复逻辑
本身**, 与 OOM 怎么来的无关 —— 所以直接把「中毒」异常注入管线, 快且确定。

做法: 把 load_pipeline 换成返回假管线, 让它第一次抛 offload 中毒异常、
之后正常返回; 跑 run_generation, 看它是否自动重建并拿到图。
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("QWEN_MIN_MEM_GB", "0")

PASS, FAIL = [], []


def chk(cond, label, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{extra}]" if extra else ""))


print("=== 0. 单元级: 异常识别 ===")
import server  # noqa: E402

chk(server._is_offload_poison(RuntimeError(
    "Expected all tensors to be on the same device, but got mat2 is on cpu, "
    "different from other tensors on cuda:0")), "识别真实的中毒报错")
chk(server._is_offload_poison(RuntimeError(
    "input tensor is on cuda:0, but expected on cpu")), "识别反向形态")
for msg in ("CUDA out of memory. Tried to allocate 1.16 GiB",
            "size 格式应为 '1024x1024'", "PIL.Image truncated"):
    chk(not server._is_offload_poison(RuntimeError(msg)), f"不误判: {msg[:40]}")
chk(issubclass(server.OffloadPoison, RuntimeError),
    "OffloadPoison 继承 RuntimeError（能被既有 except 捕获）")

print("\n=== 1. 控制流: 注入中毒异常, 看是否自动重建并重试 ===")

# 假管线: 第一次调用抛中毒异常, 之后返回一张 1x1 图
calls = {"n": 0, "rebuilt": 0}


class FakeImage:
    def save(self, fp, format=None):
        fp.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)


class FakeVAE:
    use_tiling = False

    def enable_tiling(self):
        self.use_tiling = True


class FakePipe:
    def __init__(self):
        self.vae = FakeVAE()

    # 显式列出真实管线会用到的关键字: _pipeline_kwargs 是按签名裁剪参数的,
    # 全用 **kw 会被裁成空 dict, 而且 _make_step_callback 会认为不支持
    # callback_on_step_end 而直接抛错。
    def __call__(self, prompt=None, width=None, height=None, num_inference_steps=None,
                 generator=None, true_cfg_scale=None, output_resolution=None,
                 callback_on_step_end=None, negative_prompt=None, image=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(
                "Expected all tensors to be on the same device, but got mat2 is on cpu, "
                "different from other tensors on cuda:0 (when checking argument in "
                "method wrapper_CUDA_mm)")
        return types.SimpleNamespace(images=[FakeImage()])

    def unload(self):
        pass


def fake_load():
    calls["rebuilt"] += 1
    return FakePipe()


# 接管重建路径: 真的 _recover_pipeline 会去 load_pipeline 加载真模型(要 GPU),
# 这里替换成假的, 只验控制流。
server.load_pipeline = fake_load
server.PIPE = FakePipe()
server._PIPE_CALL_PARAMS = None      # 参数签名有缓存, 换假管线必须清掉
server.STATE["load"] = "ready"

plan = {"prompt": "[unit] 注入中毒", "width": 256, "height": 256, "steps": 2,
        "n": 1, "seed": 1, "true_cfg_scale": 1.0, "output_resolution": 1024,
        "negative_prompt": None, "transparent": False, "response_format": "url",
        "ref_index": None, "warnings": [], "capped": False, "base_url": "http://x/"}

items, usage, warns = server.run_generation(plan, images=None)

chk(len(items) == 1, "重建后拿到了结果图", str(items)[:120])
chk(calls["n"] == 2, "管线被调用两次（第一次中毒 -> 重建后重试）", f"调用 {calls['n']} 次")
chk(calls["rebuilt"] == 1, "确实重建了一次管线", f"重建 {calls['rebuilt']} 次")
chk(any("重建" in w for w in warns), "响应里带自愈说明（用户可感知）", str(warns))
chk(server.STATE["load"] == "ready", "重建后 load 回到 ready", server.STATE["load"])
chk(server.OFFLOAD_POISONED["at"] > 0, "记录了自愈时间戳（/health 可观测）")

print("\n=== 2. 控制流: 中毒后重建失败 -> 状态变 error 而不是崩 ===")
def bad_load():
    calls["rebuilt"] += 1
    raise OSError("模拟: 权重文件读不到")

server.load_pipeline = bad_load
server.PIPE = FakePipe()
server._PIPE_CALL_PARAMS = None
calls["n"] = 0
server.STATE["load"] = "ready"
try:
    server.run_generation(dict(plan), images=None)
    chk(False, "重建失败时应当抛错", "居然成功了")
except Exception as e:
    chk(server.STATE["load"] == "error", "重建失败 -> load=error（/health 能看出问题）",
        server.STATE["load"])
    chk("重建" in str(server.STATE["error"] or ""), "error 里说明了是重建失败",
        str(server.STATE["error"])[:80])
    print(f"        抛出: {type(e).__name__}: {str(e)[:70]}")

print("\n=== 3. /health 已暴露自愈时间戳 ===")
src = (BASE_DIR / "server.py").read_text(encoding="utf-8")
chk('"offload_recovered_at"' in src, "health 响应含 offload_recovered_at")
chk("OFFLOAD_POISONED" in src, "自愈状态有全局记录")

print(f"\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败:")
    for f in FAIL:
        print("  - " + f)
sys.exit(1 if FAIL else 0)
