# -*- coding: utf-8 -*-
"""隔离测试: 不经 FastAPI, 在主线程直接调用管线生成 1 张图。

用于区分崩溃是"服务层(FastAPI/线程/uvicorn)"引起还是"管线本身"引起。
用法: venv\Scripts\python.exe scripts\test_direct.py [steps]
"""
import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import faulthandler
faulthandler.enable()

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(BASE, "model")
STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 3

import torch
from diffusers import QwenImage21Pipeline, QwenImage21Transformer2DModel
try:
    from diffusers import GGUFQuantizationConfig
except ImportError:
    from diffusers import GgufQuantizationConfig as GGUFQuantizationConfig
from transformers import BitsAndBytesConfig, Qwen3VLForConditionalGeneration

print("== load DiT (GGUF) ==", flush=True)
gguf = os.path.join(MODEL_DIR, "transformer", "qwen-image-2.1-Q4_K_M.gguf")
transformer = QwenImage21Transformer2DModel.from_single_file(
    gguf,
    config=MODEL_DIR, subfolder="transformer",
    quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
    dtype=torch.bfloat16,
)
# 修复 diffusers 对 GGUF 内 BF16 张量的误打包(详见 scripts/gguf_fix.py)
sys.path.insert(0, os.path.join(BASE, "scripts"))
from gguf_fix import fix_bf16_gguf_params
fix_bf16_gguf_params(transformer, gguf)

print("== load text encoder (bnb NF4) ==", flush=True)
te_quant = BitsAndBytesConfig(
    load_in_4bit=True, bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True,
)
text_encoder = Qwen3VLForConditionalGeneration.from_pretrained(
    MODEL_DIR, subfolder="text_encoder", quantization_config=te_quant,
)

print("== assemble pipeline ==", flush=True)
pipe = QwenImage21Pipeline.from_pretrained(
    MODEL_DIR, transformer=transformer, text_encoder=text_encoder,
    torch_dtype=torch.bfloat16,
)
pipe.enable_model_cpu_offload()

print(f"== generate (steps={STEPS}) ==", flush=True)
t0 = time.time()
image = pipe(
    prompt="a red apple on a wooden table, studio lighting",
    width=1024, height=1024, num_inference_steps=STEPS,
    generator=torch.Generator("cpu").manual_seed(42),
).images[0]
dt = time.time() - t0

out = os.path.join(BASE, "outputs", "direct_test.png")
image.save(out)
peak = torch.cuda.max_memory_allocated() / 1e6
print(f"OK: {dt:.1f}s | peak {peak:.0f} MB | saved {out}", flush=True)
