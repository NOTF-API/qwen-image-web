# -*- coding: utf-8 -*-
"""把 Noct-Q (ComfyUI int8 convrot 单文件) 转成 diffusers 可直接加载的 GGUF。

背景
----
Noctaluna/Noct-Q-Uncensored-Qwen-Image-2.1 只有一个 ComfyUI 用的 int8 单文件
(NoctQ_V4_int8_convrot.safetensors, 7.26GB)。它的 265 个逻辑张量名与本项目
model/transformer/qwen-image-2.1-Q4_K_M.gguf **完全一致**(纯 DiT 换权重), 所以
只要重新量化写成 GGUF, 就能直接落进 model/transformer/, 出现在 start.ps1 的量化
菜单里, server.py 一行都不用改。

为什么必须重量化: 7GB int8 权重在 8GB 卡上放不下(现有 Q5_K_M 5.0GB 已经
峰值 ~6.4GB)。gguf-python 只能写 legacy 量化(Q4_0/Q5_0/Q8_0), 写不了 K-quant,
所以同体积下质量比现有 Q4_K_M 略低一档。

两个必须注意的坑
----------------
1. **ConvRot**: 这不是普通的 int8, 是 ComfyUI `int8_tensorwise` 格式, 权重在量化
   之前先按 in_features 每 256 一组做了 regular Hadamard 旋转
   (W_rot = (W.view(out, in//256, 256) @ H.T))。不把 H 逆回去, 权重的范数完全正常
   但与原权重零相关 —— 加载不报错、跑得动、出图是纯噪点。见 read_weight()。
2. **张量名**: 融合权重 img_mlp.gate_up / attn.to_out.0 的辅助张量挂在**模块名**上
   (to_out.0.weight_scale 而不是 to_out.0.weight.weight_scale)。

量化方案
--------
直接**照抄参考 GGUF 的逐张量类型**, 只把 gguf-python 写不出的 K-quant 映射到
位宽相同的 legacy 类型:

    Q4_K -> Q4_0    (都是 4.5 bit/权重)
    Q5_K -> Q5_0    (都是 5.5 bit/权重)
    Q6_K -> Q8_0    (6.5 -> 8.5 bit/权重, 略大但更准)

F32 / BF16 / Q8_0 原样保留。于是产物体积和参考 Q4_K_M 基本一致(~3.9GB)。

GGUF 张量排布(已用现有 Q4_K_M 文件实测确认, 不要改)
----------------------------------------------------
torch 权重 W 的 shape 是 (out, in); GGUF 的 ne = [out, in], 分块量化沿 **out**
方向走。所以喂给 quantize 的数组是 **W.T**, shape (in, out)。

用法
----
    venv\\Scripts\\python.exe scripts\\noctq_to_gguf.py ^
        -i model\\noctq\\NoctQ_V4_int8_convrot.safetensors ^
        -o model\\transformer\\noctq-v4-Q4.gguf
"""
import argparse
import functools
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
from gguf import GGMLQuantizationType as QT, GGUFReader, GGUFWriter, quants
from safetensors import safe_open

PREFIX = "model.diffusion_model."
# int8 伴随张量: 量化用的辅助数据, 不是权重本身
HELPER_SUFFIXES = ("weight_scale", "comfy_quant")

# 参考 GGUF 里的 K-quant -> gguf-python 能写的 legacy 类型(位宽尽量对齐)
KMAP = {QT.Q4_K: QT.Q4_0, QT.Q5_K: QT.Q5_0, QT.Q6_K: QT.Q8_0}

BASE = Path(__file__).resolve().parent.parent
DEFAULT_REF = BASE / "model" / "transformer" / "qwen-image-2.1-Q4_K_M.gguf"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 量化方案
def build_plan(ref_gguf: Path):
    """照抄参考 GGUF 的张量名与逐张量量化类型。

    返回 [(gguf_name, torch_key, quant_type), ...]
    """
    if not ref_gguf.is_file():
        raise FileNotFoundError(
            f"找不到参考 GGUF: {ref_gguf}\n"
            "它决定每个张量用什么量化类型, 必须先跑 scripts\\download_model.ps1"
        )
    plan = []
    for t in GGUFReader(str(ref_gguf)).tensors:
        qt = QT(int(t.tensor_type))
        qt = KMAP.get(qt, qt)
        key = t.name[len(PREFIX):] if t.name.startswith(PREFIX) else t.name
        plan.append((t.name, key, qt))
    return plan


def source_keys(fh):
    return {k for k in fh.keys() if not k.endswith(HELPER_SUFFIXES)}


# ---------------------------------------------------------------- 读源张量
@functools.lru_cache(maxsize=8)
def _regular_hadamard(n: int, dtype=torch.float32) -> torch.Tensor:
    """ConvRot 用的 regular Hadamard (Theorem 3.3 的 H4 基矩阵做 Kronecker 扩展再归一化)。

    注意不是 Sylvester 矩阵 —— Sylvester 的全 1 列会放大 diffusion 模型的行向离群值,
    comfy-quants 的 int8_tensorwise 规范明确用 regular 版本。H 是对称且正交的
    (H @ H.T == I), 所以正/逆旋转都是同一个 H。
    """
    if n < 4 or n & (n - 1) or round(n ** 0.5) ** 2 != n:
        raise ValueError(f"regular Hadamard 的 size 必须是 4 的幂, 得到 {n}")
    h4 = torch.tensor([[1, 1, 1, -1], [1, 1, -1, 1],
                       [1, -1, 1, 1], [-1, 1, 1, 1]], dtype=dtype)
    h = h4
    while h.shape[0] < n:
        h = torch.kron(h, h4)
    return h / (n ** 0.5)


def _convrot_meta(fh, module: str) -> dict:
    """解析 comfy_quant 标记 (uint8 JSON)。"""
    raw = bytes(fh.get_tensor(module + ".comfy_quant").to(torch.uint8).tolist())
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def read_weight(fh, key: str) -> torch.Tensor:
    """读一个逻辑权重(已按 torch shape 还原), dtype=torch.float32。

    int8 是 ComfyUI 的 int8_tensorwise 格式, 分两层:

      1. 逐行对称量化   W_rot = q * scale[out]
      2. ConvRot 旋转   W_rot = (W.view(out, in//gs, gs) @ H.T).reshape(out, in)
         —— gs = convrot_groupsize (本模型 256), H 是 regular Hadamard。

    **第 2 步不还原就是错的**: 权重范数对但与原权重完全不相关, 实测相对误差 1.37
    (≈√2, 即两个无关张量), 出图是纯噪点。这里把 H 逆着乘回去还原到原基底。
    """
    t = fh.get_tensor(key)
    if t.dtype != torch.int8:
        return t.to(torch.float32)

    mod = key[: -len(".weight")] if key.endswith(".weight") else key
    scale = fh.get_tensor(mod + ".weight_scale").to(torch.float32)
    if scale.ndim != 2 or scale.shape[1] != 1:
        raise ValueError(f"{mod}.weight_scale 形状异常: {tuple(scale.shape)}, 期望 (out, 1)")
    if scale.shape[0] != t.shape[0]:
        raise ValueError(
            f"{key} 量化轴不匹配: weight{tuple(t.shape)} vs scale{tuple(scale.shape)}"
        )
    w = t.to(torch.float32) * scale                # (out,1) 沿 in 广播

    meta = _convrot_meta(fh, mod)
    gs = int(meta.get("convrot_groupsize", 0) or 0)
    if not meta.get("convrot"):
        return w
    if w.ndim != 2 or w.shape[1] % gs:
        # 规范: in_features 不能被 gs 整除的层会跳过旋转, 此时标记里不带 convrot
        raise ValueError(f"{key}: convrot 标记存在但 in={w.shape[1]} 不能被 {gs} 整除")
    out_f, in_f = w.shape
    return (w.view(out_f, in_f // gs, gs) @ _regular_hadamard(gs)).reshape(out_f, in_f)


# ---------------------------------------------------------------- 写 GGUF
def add_tensor(writer: GGUFWriter, name: str, w: torch.Tensor, qt: QT):
    """把 torch 权重写进 GGUF。

    GGUF 的 ne 就是 torch 的 shape（(out, in) 就写 (out, in)），分块量化沿最后一维
    (in) 走。diffusers 的 GGUFParameter 反量化后形状必须正好等于模块的
    nn.Linear.weight.shape，所以这里**不做转置**。
    """
    a = w.contiguous().numpy()
    if a.ndim not in (1, 2):
        raise ValueError(f"{name} 维度异常: {a.shape}")

    if qt == QT.F32:
        writer.add_tensor(name, a.astype(np.float32))
        return
    if qt == QT.F16:
        writer.add_tensor(name, a.astype(np.float16))
        return
    if qt == QT.BF16:
        # 存成原始字节(小端, 每元素 2 字节): 形状写 (out, in*2)
        b = torch.from_numpy(a).to(torch.bfloat16).view(torch.uint8).numpy()
        writer.add_tensor(name, b, raw_shape=b.shape, raw_dtype=QT.BF16)
        return

    q = quants.quantize(a, qt)                   # (out, in//block*type_size)
    writer.add_tensor(name, q, raw_shape=q.shape, raw_dtype=qt)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Noct-Q int8 (ComfyUI) -> GGUF")
    ap.add_argument("-i", "--input", required=True, help="Noct-Q .safetensors 路径")
    ap.add_argument("-o", "--output", required=True, help="输出 .gguf 路径")
    ap.add_argument("--ref", default=str(DEFAULT_REF), help="参考 GGUF (决定逐张量量化类型)")
    args = ap.parse_args()

    src = Path(args.input).resolve()
    dst = Path(args.output).resolve()
    if not src.is_file():
        raise SystemExit(f"找不到源文件: {src}\n先跑 scripts\\download_noctq.ps1")
    dst.parent.mkdir(parents=True, exist_ok=True)

    plan = build_plan(Path(args.ref).resolve())

    log(f"源文件 : {src}  ({src.stat().st_size / 2**30:.2f} GB)")
    log(f"输出   : {dst}")
    log(f"张量数 : {len(plan)}")

    t0 = time.time()
    with safe_open(str(src), framework="pt") as fh:
        keys = source_keys(fh)
        missing = [k for _, k, _ in plan if k not in keys]
        if missing:
            raise SystemExit(
                f"源文件缺 {len(missing)} 个张量(该 Noct-Q 版本可能与参考架构不同), 例如: {missing[:5]}"
            )
        extra = keys - {k for _, k, _ in plan}
        if extra:
            raise SystemExit(f"源文件多出 {len(extra)} 个未知张量, 例如: {sorted(extra)[:5]}")
        log("张量名与参考完全对齐 OK")

        # use_temp_file=True: 张量数据边转边落盘, 峰值内存约 1GB 而不是整个模型
        writer = GGUFWriter(str(dst), "qwen-image2.1", use_temp_file=True)
        for i, (name, key, qt) in enumerate(plan, 1):
            add_tensor(writer, name, read_weight(fh, key), qt)
            if i % 40 == 0 or i == len(plan):
                log(f"  {i}/{len(plan)}  {key}")
        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_tensors_to_file()
        writer.close()

    size = dst.stat().st_size
    log(f"完成: {dst}  {size / 2**30:.2f} GB  用时 {time.time() - t0:.0f}s")
    log(f"现在可以: powershell -ExecutionPolicy Bypass -File start.ps1 {dst.stem}")


if __name__ == "__main__":
    sys.exit(main())
