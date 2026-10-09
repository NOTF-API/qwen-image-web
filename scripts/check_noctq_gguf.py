# -*- coding: utf-8 -*-
"""校验 Noct-Q 转出来的 GGUF。

两步:
1) 走 server.py 里一模一样的加载路径 (from_single_file + GGUFQuantizationConfig +
   gguf_fix.fix_bf16_gguf_params), 确认能加载 —— 张量形状/排布如果错了,
   diffusers 的 check_quantized_param_shape 会直接抛错。
2) 用 diffusers 自己的解码器把 GGUF 全部 265 个张量解出来, 跟源 int8 反量化结果
   逐个比对相对误差。不经过模型结构, 所以 img_mlp.gate_up 这种融合权重也能覆盖。

不加载文本编码器/VAE, 不占显存。
"""
import sys
from pathlib import Path

import torch
from safetensors import safe_open

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE))

from diffusers.models.model_loading_utils import load_gguf_checkpoint     # noqa: E402
from diffusers.quantizers.gguf.utils import dequantize_gguf_tensor        # noqa: E402
from gguf_fix import fix_bf16_gguf_params                                # noqa: E402
from noctq_to_gguf import DEFAULT_REF, PREFIX, read_weight                 # noqa: E402

SRC = BASE / "model" / "noctq" / "NoctQ_V4_int8_convrot.safetensors"
GGUF = BASE / "model" / "transformer" / "noctq-v4-Q4.gguf"
MODEL_DIR = BASE / "model"

# 量化本身的相对误差下限: Q4_0 ~0.09, Q5_0 ~0.04, Q8_0 ~0.006, BF16 ~0.001
TOL = {"Q4_0": 0.12, "Q5_0": 0.07, "Q8_0": 0.02, "BF16": 0.005, "F32": 0.0}
# 与官方基线的相对差异上限。Noct-Q 是同架构的轻量微调, 实测 0.04~0.08;
# 若源权重没正确解量化(例如 ConvRot 没逆回来)会飙到 ~1.37, 这里正好兜住。
REF_MAX = 0.6


def main():
    from diffusers import GGUFQuantizationConfig, QwenImage21Transformer2DModel

    print(f"[1/3] 按 server.py 的路径加载 {GGUF.name} ...")
    t = QwenImage21Transformer2DModel.from_single_file(
        str(GGUF), config=str(MODEL_DIR), subfolder="transformer",
        quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
        dtype=torch.bfloat16,
    )
    fixed = fix_bf16_gguf_params(t, GGUF)
    print(f"      from_single_file OK; gguf_fix 还原 BF16 张量 {len(fixed)} 个: {fixed}")
    del t

    print(f"[2/3] 解码全部张量并与 {SRC.name} 比对 ...")
    sd = load_gguf_checkpoint(str(GGUF), return_tensors=True)
    ref = load_gguf_checkpoint(str(DEFAULT_REF), return_tensors=True)
    bad = 0
    worst_rel, worst_name, worst_ref = [], "", 0.0
    with safe_open(str(SRC), framework="pt") as fh:
        for name, p in sd.items():
            key = name[len(PREFIX):] if name.startswith(PREFIX) else name
            want = read_weight(fh, key)
            qt = getattr(p, "quant_type", None)
            got = dequantize_gguf_tensor(p) if qt is not None else p
            if tuple(got.shape) != tuple(want.shape):
                print(f"  BAD {key:52s} 形状 {tuple(got.shape)} != {tuple(want.shape)}")
                bad += 1
                continue
            # (a) GGUF 是否忠实反映了源权重(量化误差)
            rel = float((got.float() - want).norm() / want.norm())
            qn = qt.name if qt is not None else "F32"
            if qn == "F32":
                ok = rel == 0.0
            else:
                ok = rel <= TOL[qn]
            if not ok:
                bad += 1
                print(f"  BAD {key:52s} {qn:5s} rel_err={rel:.4f} > {TOL[qn]}")

            # (b) 源权重读对了吗 —— 跟官方基线比。同一架构的微调应该高度相关;
            #     ConvRot 没逆回来时这里是 ~1.37 (两个无关张量), 正好能兜住。
            r = ref.get(name)
            if r is not None:
                rr = dequantize_gguf_tensor(r) if getattr(r, "quant_type", None) is not None else r
                if tuple(rr.shape) == tuple(got.shape):
                    rel_ref = float((got.float() - rr.float()).norm() / rr.float().norm())
                    worst_rel.append((rel_ref, qn, key))
                    if rel_ref > worst_ref:
                        worst_ref, worst_name = rel_ref, key
                    if rel_ref > REF_MAX:
                        bad += 1
                        print(f"  BAD {key:52s} 与官方基线 rel={rel_ref:.3f} > {REF_MAX}"
                              f" (源权重可能没正确解量化)")

    worst_rel.sort(reverse=True)
    print(f"      [a] 与源比对: {len(sd)} 个张量全部在容差内")
    print(f"      [b] 与官方基线比对: 最大 {worst_ref:.4f} ({worst_name})")
    print("      差异最大的 3 个:")
    for rel, qn, key in worst_rel[:3]:
        print(f"        {rel:.4f}  {qn:5s} {key}")

    print("\n全部通过 OK" if not bad else f"\n{bad} 项异常")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
