# -*- coding: utf-8 -*-
"""修复 diffusers GGUF 加载器对 BF16 张量的处理缺陷。

diffusers 的 load_gguf_checkpoint 只把 F32/F16 视为"torch 原生 dtype",
GGUF 中 dtype=BF16 (type 30) 的张量被误包成 GGUFParameter(底层是 uint8
裸字节)。F.linear 等有专门分支的算子不受影响, 但普通算子(如 RMSNorm 中的
self.weight.float())会走 __torch_function__ 的 fallback, 直接把字节缓冲按
元素 cast —— 长度翻倍(4096 -> 8192), 触发 "size of tensor a must match
size of tensor b" 错误。

本模块在 from_single_file 加载完成后调用, 把 GGUF 中的 BF16 张量还原成
普通 torch tensor(bf16), 从根本上绕开该缺陷。

用法:
    from gguf_fix import fix_bf16_gguf_params   # scripts/ 内
    fixed = fix_bf16_gguf_params(transformer, gguf_path, logger=log)
"""
import torch
from gguf import GGUFReader

GGUF_BF16 = 30  # gguf.GGMLQuantizationType.BF16
DIFFUSERS_PREFIX = "model.diffusion_model."


def fix_bf16_gguf_params(model, gguf_path, logger=None):
    """把 GGUF 中 BF16 的权重张量替换为普通 tensor。

    Returns:
        list[str]: 被修复的参数名(相对模型根, 如 "txt_in.text_norm.weight")
    """
    reader = GGUFReader(str(gguf_path))
    fixed = []
    for t in reader.tensors:
        if int(t.tensor_type) != GGUF_BF16:
            continue
        name = t.name
        if name.startswith(DIFFUSERS_PREFIX):
            name = name[len(DIFFUSERS_PREFIX):]
        mod_path, _, param_name = name.rpartition(".")
        try:
            mod = model.get_submodule(mod_path)
        except AttributeError:
            continue
        cur = getattr(mod, param_name, None)
        if cur is None or type(cur).__name__ != "GGUFParameter":
            continue

        raw = bytes(t.data)                      # bf16 裸字节 (2 bytes/elem)
        tensor = torch.frombuffer(bytearray(raw), dtype=torch.uint16).view(torch.bfloat16)
        if isinstance(mod, torch.nn.Linear):
            shape = (mod.out_features, mod.in_features)
        else:
            shape = (tensor.numel(),)            # 1-D norm 权重
        tensor = tensor.reshape(shape).clone()   # clone: 脱离 frombuffer 缓冲
        mod._parameters[param_name] = torch.nn.Parameter(tensor, requires_grad=False)
        fixed.append(name)

    if logger is not None and fixed:
        logger.info("BF16 张量已还原为普通 tensor (%d): %s", len(fixed), ", ".join(fixed))
    return fixed
