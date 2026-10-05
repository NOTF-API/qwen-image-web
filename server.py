# -*- coding: utf-8 -*-
"""
Qwen-Image-2.1 本地推理服务（独立运行，不依赖 ComfyUI）

- OpenAI 风格接口: POST /v1/images/generations  (文生图)
                    POST /v1/images/edits        (图生图 / 图片编辑 / 多参考图)
- 8GB 显存适配: DiT 用 GGUF Q4_K_M，文本编码器(Qwen3-VL 8B)用 bitsandbytes NF4，
  组件级 CPU offload 轮流上卡，峰值显存约 6.5~7GB

启动:  start.bat      默认 http://127.0.0.1:8091
"""
import os
import sys
import gc
import io
import json
import time
import uuid
import base64
import inspect
import logging
import threading
import subprocess
from pathlib import Path
from typing import Optional

# ---- 环境变量必须在 import torch 之前设置 ----
# 注: expandable_segments 在 Windows 上不受支持(torch 会警告并忽略), 故不设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# 原生层崩溃(access violation)时在 stderr 打印 C 栈, 便于定位
import faulthandler
faulthandler.enable()

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from taskqueue import (                                    # noqa: E402
    Canceled, QueueWorker, TaskConflict, TaskNotFound, TaskStore,
    cleanup_orphan_outputs,
)

MODEL_DIR = Path(os.environ.get("QWEN_MODEL_DIR", BASE_DIR / "model"))
OUTPUT_DIR = Path(os.environ.get("QWEN_OUTPUT_DIR", BASE_DIR / "outputs"))
# 任务队列的持久化目录: <outputs>/tasks/{tasks.json, refs/<task_id>/*}
TASK_DIR = Path(os.environ.get("QWEN_TASK_DIR", OUTPUT_DIR / "tasks"))
HOST = os.environ.get("QWEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("QWEN_PORT", "8091"))
MAX_SIDE = int(os.environ.get("QWEN_MAX_SIDE", "1536"))          # 单边上限(8GB 显存)
DEFAULT_LONG_SIDE = int(os.environ.get("QWEN_LONG_SIDE", "1024"))  # 默认出图长边
DEFAULT_STEPS = int(os.environ.get("QWEN_STEPS", "30"))          # 默认采样步数(官方 40)
MAX_STEPS = int(os.environ.get("QWEN_MAX_STEPS", "60"))
# 真 CFG 开关: Qwen-Image-2.1 官方按「无引导」采样, 管线默认 true_cfg_scale=1.0(关闭)。
# >1 时才启用 CFG, 且必须同时给 negative_prompt, 两者缺一则负提示词被忽略(管线只发警告)。
# 注意: 本项目此前暴露的 guidance_scale 并非本管线参数, 会被静默丢弃(已删除, 见 _build_plan)。
DEFAULT_TRUE_CFG_SCALE = float(os.environ.get("QWEN_TRUE_CFG_SCALE", "1.0"))
MAX_TRUE_CFG_SCALE = float(os.environ.get("QWEN_MAX_TRUE_CFG_SCALE", "20"))
# 参考图缩放基准(output_resolution): 管线按此值把每张参考图等比缩到长边上限,
# 并与输出画布分开。不传时管线默认 1024 —— 于是 1536 的编辑请求也把参考图压到 1024²,
# 白白丢细节。这里默认取 min(出图长边, EDIT_DEFAULT_OUTPUT_RESOLUTION), 兼顾细节与速度;
# 显式设 QWEN_OUTPUT_RESOLUTION 可固定, 传 >1024 会显著变慢(见 _generate 注释)。
OUTPUT_RESOLUTION = int(os.environ.get("QWEN_OUTPUT_RESOLUTION", "0")) or None
# 走参考图(编辑)时, output_resolution 不显式指定下的默认上限。抬高会同时放大
# 视觉编码器 prefill 与条件 token 数, 8GB 卡上非常慢, 故默认与文档基准 1024 对齐。
EDIT_DEFAULT_OUTPUT_RESOLUTION = int(os.environ.get("QWEN_EDIT_OUTPUT_RESOLUTION", "1024"))
# 多参考图时按第几张定画布长宽比(管线内部用最后一张, 本案默认第一张, 即内容/主体图)
REF_IMAGE_INDEX = int(os.environ.get("QWEN_REF_INDEX", "0"))
MAX_REF_IMAGES = int(os.environ.get("QWEN_MAX_REF_IMAGES", "10"))   # 官方上限 10 张
MAX_QUEUE_BATCH = int(os.environ.get("QWEN_MAX_QUEUE_BATCH", "16"))  # 一次 prompts 批量上限
OFFLOAD = os.environ.get("QWEN_OFFLOAD", "model")                # model | sequential
GGUF_FILE = os.environ.get("QWEN_GGUF", "")                      # 指定 transformer/*.gguf
MIN_MEM_GB = float(os.environ.get("QWEN_MIN_MEM_GB", "0"))       # 可用内存下限(GB), 低于则503; 默认0=关闭(实测该指标无法可靠预判崩溃)
# VAE 分块解码: auto=按需(编辑/长边>1024开, 文生图<=1024关), 1=恒开, 0=恒关
# 文生图关分块可消除192px接缝网格(实测), 但峰值显存+1.3GB(编辑与>1024必须开)
TILING_MODE = os.environ.get("QWEN_VAE_TILING", "auto").lower()

try:
    import psutil
except Exception:
    psutil = None


def _commit_headroom_gb():
    """Windows 剩余可提交内存(GB)，非 Windows 返回 None。

    文本编码器搬回 CPU 时需一次性提交约 4.4GB，提交余量耗尽会在原生拷贝层
    以 access violation (0xc0000005) 崩掉进程（2026-09-26 实测）。物理可用
    内存不是有效指标（正常运行时也常 <1GB），故以提交余量为准。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return st.ullAvailPageFile / 2**30
    except Exception:
        return None

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("qwen-image")

# ---- 任务队列: 持久化存储(任务列表/参数/产物) + 单线程串行执行 ----
TASK_STORE = TaskStore(TASK_DIR)
QUEUE: "QueueWorker | None" = None
_PIPE_CALL_PARAMS: "set | None" = None      # pipeline.__call__ 的可用参数(懒加载缓存)

# 官方推荐的长宽比 -> 尺寸表 (在长边缩放后按 16 的倍数取整)
ASPECT_RATIOS = {
    "1:1": (1, 1), "4:3": (4, 3), "3:4": (3, 4), "3:2": (3, 2),
    "2:3": (2, 3), "16:9": (16, 9), "9:16": (9, 16),
}

# ---------------------------------------------------------------- pipeline 加载
PIPE = None
LOAD_LOCK = threading.Lock()
GEN_LOCK = threading.Lock()          # GPU 串行化: 并发请求排队
# waiting: 正在等待 GPU 的请求数(同步接口 + 队列工作线程共用)
STATE = {"load": "idle", "error": None, "loaded_at": None, "waiting": 0,
         "last_gen": None}


def _find_gguf() -> Path:
    tf_dir = MODEL_DIR / "transformer"
    if GGUF_FILE:
        p = Path(GGUF_FILE)
        return p if p.is_absolute() else tf_dir / p
    cands = sorted(tf_dir.glob("*.gguf"))
    if not cands:
        raise FileNotFoundError(f"{tf_dir} 下没有 .gguf 文件，请先运行 scripts/download_model.ps1")
    prefer = [c for c in cands if "Q4_K_M" in c.name]
    return prefer[0] if prefer else cands[0]


def load_pipeline():
    import torch
    from diffusers import QwenImage21Pipeline, QwenImage21Transformer2DModel
    try:
        from diffusers import GGUFQuantizationConfig
    except ImportError:               # 旧版 diffusers 的名字
        from diffusers import GgufQuantizationConfig as GGUFQuantizationConfig
    from transformers import BitsAndBytesConfig, Qwen3VLForConditionalGeneration

    gguf = _find_gguf()
    log.info("加载 DiT (GGUF): %s", gguf.name)

    # GGUF 必须走 from_single_file (diffusers 不支持 from_pretrained 加载 gguf)
    transformer = QwenImage21Transformer2DModel.from_single_file(
        str(gguf),
        config=str(MODEL_DIR), subfolder="transformer",
        quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
        dtype=torch.bfloat16,
    )
    # 修复 diffusers 对 GGUF 内 BF16 张量的误打包(否则 text_norm.weight.float()
    # 把字节当元素导致形状翻倍 4096->8192, 生成时抛 RuntimeError)
    sys.path.insert(0, str(BASE_DIR / "scripts"))
    from gguf_fix import fix_bf16_gguf_params
    fixed = fix_bf16_gguf_params(transformer, gguf, logger=log)

    log.info("加载文本编码器 Qwen3-VL 8B (bitsandbytes NF4, 首次需 1~3 分钟)...")
    te_quant = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    text_encoder = Qwen3VLForConditionalGeneration.from_pretrained(
        MODEL_DIR, subfolder="text_encoder", quantization_config=te_quant,
    )

    log.info("组装 QwenImage21Pipeline ...")
    pipe = QwenImage21Pipeline.from_pretrained(
        MODEL_DIR, transformer=transformer, text_encoder=text_encoder,
        torch_dtype=torch.bfloat16,
    )
    if OFFLOAD == "sequential":
        pipe.enable_sequential_cpu_offload()
    else:
        pipe.enable_model_cpu_offload()
    # VAE 分块解码: 恒开模式在此启用; auto 模式由 _generate 按请求动态开关
    if TILING_MODE in ("1", "on", "true", "always"):
        try:
            pipe.vae.enable_tiling()
        except Exception:
            pass
    pipe.set_progress_bar_config(leave=False)
    return pipe


def ensure_pipe():
    global PIPE
    with LOAD_LOCK:
        if PIPE is not None:
            return PIPE
        STATE["load"] = "loading"
        t0 = time.time()
        try:
            PIPE = load_pipeline()
            STATE["load"] = "ready"
            STATE["loaded_at"] = time.time()
            log.info("模型就绪，用时 %.1fs", time.time() - t0)
        except Exception as e:
            STATE["load"] = "error"
            STATE["error"] = f"{type(e).__name__}: {e}"
            log.exception("模型加载失败")
            raise
        return PIPE


def _preload_worker():
    try:
        ensure_pipe()
    except Exception:
        pass


# ---------------------------------------------------------------- 工具函数
def _gpu_mem():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        used, total = out.stdout.strip().splitlines()[0].split(",")
        return {"used_mb": int(used), "total_mb": int(total)}
    except Exception:
        return None


def _round16(v: int) -> int:
    return max(256, (int(v) // 16) * 16)


def resolve_size(size=None, aspect_ratio=None, long_side=None,
                 ref_wh=None, width=None, height=None) -> tuple:
    """返回 (width, height)，16 的倍数，长边不超过 MAX_SIDE

    优先级: ``size`` > ``width``/``height`` > ``aspect_ratio``+``long_side`` > 参考图比例 > 正方形。

    ``width``/``height`` 是 OpenAI 风格的写法, 以前这里不认, 传了会被静默忽略并退回
    默认 1024x1024(调用方看不出自己写错了)。现在显式支持。
    """
    long_side = long_side or DEFAULT_LONG_SIDE
    capped = False
    if not size and width and height:
        try:
            size = f"{int(width)}x{int(height)}"
        except (TypeError, ValueError):
            raise ValueError(f"width/height 需为正整数，收到: {width!r}x{height!r}")
    if size:
        s = str(size).lower().replace("*", "x").replace(" ", "")
        try:
            w, h = (int(x) for x in s.split("x"))
        except Exception:
            raise ValueError(f"size 格式应为 '1024x1024'，收到: {size!r}")
    elif aspect_ratio:
        key = str(aspect_ratio).strip()
        if key not in ASPECT_RATIOS:
            raise ValueError(f"aspect_ratio 需为 {list(ASPECT_RATIOS)}，收到: {aspect_ratio!r}")
        a, b = ASPECT_RATIOS[key]
        if a >= b:
            w, h = long_side, round(long_side * b / a)
        else:
            w, h = round(long_side * a / b), long_side
    elif ref_wh:                      # 编辑模式: 跟随参考图比例
        rw, rh = ref_wh
        if rw >= rh:
            w, h = long_side, round(long_side * rh / rw)
        else:
            w, h = round(long_side * rw / rh), long_side
    else:
        w = h = long_side

    w, h = _round16(w), _round16(h)
    m = max(w, h)
    if m > MAX_SIDE:
        scale = MAX_SIDE / m
        w, h = _round16(w * scale), _round16(h * scale)
        capped = True
    return w, h, capped


def pick_ref_image(images, ref_index=None):
    """多参考图时按第几张定画布比例 (默认第 0 张 = 内容/主体图)。

    管线内部用 image[-1] 推导比例, 而本服务用第 0 张: 若两者比例差得多,
    条件图会被缩放成与画布不同的形状。故这里返回所选图, 并交由调用方把同一张
    的比例传给 resolve_size, 同时校验差异。
    """
    n = len(images)
    idx = REF_IMAGE_INDEX if ref_index is None else int(ref_index)
    if idx < 0:                       # 兼容管线原生语义 image[-1]
        idx += n
    if not 0 <= idx < n:
        raise ValueError(f"ref_index 需在 {-n}~{n - 1} 之间，收到: {ref_index}")
    return idx, images[idx]


def _clamp_output_resolution(value):
    """参考图缩放基准: 取 16 的倍数, 限制在 256~MAX_SIDE。"""
    v = int(value)
    v = max(256, min(MAX_SIDE, v))
    return _round16(v)


def _b64_to_pil(data: str):
    from PIL import Image
    if "," in data and data.strip().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
    except Exception:
        raise ValueError("图片 base64 解码失败")
    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.mode or "P" in img.mode else "RGB")
    return img


def _looks_like_b64(s: str) -> bool:
    """是否像 base64 数据(而不是文件路径)。

    早先这里用「长度 > 256」来判断, 于是任何压得足够小的图(纯色小 PNG 只要几十个
    字符)都会被当成文件名, 报「找不到本站图片: <一串 base64>」。改用字符集判断:
    base64 字母表里没有 ``.`` / ``:`` / ``\\``, 所以带扩展名的路径一定不会命中。
    """
    if len(s) < 16:
        return False
    if any(ch in s for ch in ".:\\"):
        return False
    # 显式列 base64 字母表(不能用 str.isalnum(), 它对中文等也返回 True)
    return all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
               "0123456789+/=\r\n\t " for ch in s)


def _ref_source_to_pil(src: str):
    """参考图来源: base64 / data URL / 本站产物路径(/outputs/xxx.png)。"""
    if not isinstance(src, str) or not src.strip():
        raise ValueError("参考图需为 base64 字符串或本站图片路径")
    s = src.strip()
    if s.startswith("data:") or _looks_like_b64(s):
        return _b64_to_pil(s)
    if s.startswith(("http://", "https://")):
        # 只允许指回本站的产物, 避免服务被当成任意 URL 抓取器
        if "/outputs/" not in s:
            raise ValueError("只支持本站 /outputs/ 下的图片 URL")
        s = s.split("/outputs/", 1)[1]
    name = Path(s).name
    path = (OUTPUT_DIR / name).resolve()
    if path.parent != OUTPUT_DIR.resolve() or not path.is_file():
        raise ValueError(f"找不到本站图片: {name}")
    from PIL import Image
    img = Image.open(path)
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.mode or "P" in img.mode else "RGB")
    return img


def _wrap_transparency(prompt: str, transparent: bool) -> str:
    if not transparent:
        return prompt
    if "RGBA image" in prompt:
        return prompt
    return ("This is an RGBA image with transparency. "
            f"{prompt}. The image has alpha channel and the background is transparent.")


def _filter_kwargs(fn, kwargs: dict) -> dict:
    sig = inspect.signature(fn)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return kwargs
    allowed = set(sig.parameters)
    return {k: v for k, v in kwargs.items() if k in allowed}


def _save_output(image, base_url: str, meta: dict, response_format: str = "url") -> dict:
    """把结果写进 outputs/ 并返回 OpenAI 风格条目(带本地路径, 便于队列持久化)。

    ``response_format="b64_json"`` 时额外带上 base64(文件照旧落盘, 便于继续编辑/下载)。
    """
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    raw = buf.getvalue()
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
    path = OUTPUT_DIR / name
    path.write_bytes(raw)
    item = {"url": f"{base_url}outputs/{name}", "path": str(path)}
    if str(response_format).lower() == "b64_json":
        item["b64_json"] = base64.b64encode(raw).decode()
    item.update(meta)
    return item


def _oom_reset():
    import torch
    torch.cuda.empty_cache()
    gc.collect()


# offload 状态被 OOM 打断后会「中毒」: 之后每次都报
#   Expected all tensors to be on the same device ...
# 而且**不重载管线就永远好不了**(2026-10-01 实测 3/3 必现, 连不取消的正常请求也失败)。
# 根因是 enable_model_cpu_offload 的 hook 记录了「哪些模块在 GPU 上」, OOM 中断搬移后
# 这个记录和真实驻留不一致。empty_cache() 清不掉这种逻辑状态, 只能重建管线。
OFFLOAD_POISONED = {"at": 0, "err": ""}
POISON_SIGNATURES = ("tensors to be on the same device",
                     "expected all tensors to be on the same device",
                     "not on the same device",
                     "but expected on cpu",
                     "but expected on cuda")


def _is_offload_poison(err) -> bool:
    """这个异常是否说明 offload 状态已损坏(而非普通 OOM)。"""
    msg = str(err).lower()
    return any(sig in msg for sig in POISON_SIGNATURES)


def _recover_pipeline(reason: str) -> None:
    """重建管线以恢复 offload 状态。

    代价是一次冷加载(实测 ~37 秒), 但比重启服务轻得多 —— 模型文件、队列、
    已完成任务都不受影响, 这正是队列持久化存在的意义。
    """
    global PIPE
    log.error("模型 offload 状态已损坏(%s), 正在重建管线(约 30~60 秒)…", reason)
    t0 = time.time()
    STATE["load"] = "loading"
    with LOAD_LOCK:
        old = PIPE
        PIPE = None                     # 先摘掉, 避免并发请求用到半死的管线
        try:
            if old is not None:
                try:
                    old.unload()
                except Exception:
                    pass
                del old
            _oom_reset()
            PIPE = load_pipeline()
            STATE["load"] = "ready"
            STATE["loaded_at"] = time.time()
            # 注意: 这里**不要**清 OFFLOAD_POISONED["at"] —— 它是给 /health 看的
            # 「最近一次自愈时间」, 清掉了运维就看不出服务曾经坏过、是怎么恢复的。
            # 真正要留意的是它长时间不更新却一直有请求失败(见 _recover_pipeline 开头日志)。
            OFFLOAD_POISONED["err"] = ""
            log.info("管线重建完成, 用时 %.1fs", time.time() - t0)
        except Exception as e:
            PIPE = None
            STATE["load"] = "error"
            STATE["error"] = f"重建管线失败: {type(e).__name__}: {e}"
            log.exception("重建管线失败")
            raise


def _call_pipeline(pipe, kwargs: dict):
    """调管线, 并识别「offload 状态损坏」。

    这种错误看起来像普通的设备不匹配异常, 但含义完全不同: 管线已经不能用了,
    继续重试多少次都一样, 必须重建。识别出来就抛 OffloadPoison, 由调用方决定
    是重建后重试(同步接口)还是记为可重试的失败(队列)。
    """
    try:
        return pipe(**kwargs)
    except RuntimeError as e:
        if _is_offload_poison(e):
            raise OffloadPoison(str(e)[:200]) from e
        raise


class OffloadPoison(RuntimeError):
    """模型 offload 状态已损坏, 需重建管线(区别于普通 OOM)。"""


# ---------------------------------------------------------------- 请求归一化
class ParamError(ValueError):
    """请求参数不合法(HTTP 400 / 任务失败原因)。"""


def _has_prompt(payload: dict) -> bool:
    """请求里是否带了至少一条非空提示词(单条 ``prompt`` 或批量 ``prompts``)。

    网页的「新建任务」永远发的是 ``prompts`` 数组(splitPrompts), 顶层 ``prompt``
    只在直接调 API 时才有, 所以校验必须两个都认, 否则网页一上传参考图就 400。
    """
    p = payload.get("prompt")
    if isinstance(p, str) and p.strip():
        return True
    ps = payload.get("prompts")
    if isinstance(ps, str):
        return bool(ps.strip())
    if isinstance(ps, list):
        return any(str(x).strip() for x in ps)
    return False


def _check_response_format(value) -> str:
    """校验并归一化 response_format。入队请求也走这里, 免得非法值被静默吞掉。"""
    fmt = str(value or "url").lower()
    if fmt not in ("url", "b64_json"):
        raise ParamError(f"response_format 需为 'url' 或 'b64_json'，收到: {value!r}")
    return fmt


def _plan_size(payload: dict, images) -> tuple:
    """解析 (w, h, capped, ref_index_used)；编辑模式画布跟随 ref_index 那张。"""
    kw = dict(size=payload.get("size"), aspect_ratio=payload.get("aspect_ratio"),
              long_side=payload.get("long_side"), width=payload.get("width"),
              height=payload.get("height"))
    try:
        w, h, capped = resolve_size(**kw)
        ref_index_used = None
        if images:
            ref_index_used, ref_img = pick_ref_image(images, payload.get("ref_index"))
            w, h, capped = resolve_size(ref_wh=(ref_img.width, ref_img.height), **kw)
    except ValueError as e:
        raise ParamError(str(e))
    return w, h, capped, ref_index_used


def _speed_warnings(w: int, h: int, output_resolution: int, images) -> list:
    """尺寸相关的耗时提示(同步接口与队列任务共用)。"""
    warnings = []
    if max(w, h) > 1024:
        warnings.append(
            f"画布 {w}x{h} 超过 1024: 编辑路径下 token 数按面积增长, 耗时会明显高于文档基准"
            f"(1024²@30步约 98s), 8GB 卡上建议降到 1024 或减少步数")
    if images and output_resolution > 1024:
        warnings.append(
            f"output_resolution={output_resolution} 高于默认 1024: 参考图缩放基准越大, "
            f"视觉编码器 prefill 越慢; 如只是想更快可设 QWEN_EDIT_OUTPUT_RESOLUTION=1024")
    return warnings


def _ref_warnings(images, ref_index_used: int) -> list:
    """多参考图比例不一致的提示。"""
    if len(images) <= 1 or ref_index_used is None or ref_index_used == len(images) - 1:
        return []
    last = images[-1]
    if not last.height:
        return []
    ra = images[ref_index_used].width * last.height
    rb = images[ref_index_used].height * last.width
    if max(ra, rb) / max(1, min(ra, rb)) > 1.15:
        return [f"参考图比例不一致：画布按第 {ref_index_used + 1} 张，管线内部按最后一张"
                f"(第 {len(images)} 张) 推导；差异较大时条件图可能变形，"
                f"建议统一参考图比例或用 ref_index=-1 对齐"]
    return []


def _random_seed() -> int:
    """随机种子(未指定 seed 时用)。"""
    return int.from_bytes(os.urandom(4), "big")


def _build_plan(payload: dict, images, base_url: str = "") -> dict:
    """把请求 payload 归一化成一份可直接执行的生成计划。

    同步接口与队列任务走同一条路径: 所有校验只在这里做一遍,
    计划本身是可 JSON 序列化的(参考图以本地文件名表达), 因此可以落盘复用。
    """
    prompt = payload.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise ParamError("缺少 prompt 字段")

    n = int(payload.get("n", 1) or 1)
    if n < 1 or n > 4:
        raise ParamError("n 需在 1~4 之间")

    steps = int(payload.get("steps", payload.get("num_inference_steps", DEFAULT_STEPS)))
    if steps < 1 or steps > MAX_STEPS:
        raise ParamError(f"steps 需在 1~{MAX_STEPS} 之间")

    # 真 CFG (true_cfg_scale) 才是本管线的引导开关; 只有它 >1 且同时给了
    # negative_prompt, 负提示词才真正参与采样。官方按无引导采样, 故默认 1.0。
    true_cfg = payload.get("true_cfg_scale")
    if true_cfg is None:
        true_cfg = DEFAULT_TRUE_CFG_SCALE
    try:
        true_cfg = float(true_cfg)
    except (TypeError, ValueError):
        raise ParamError(f"true_cfg_scale 需为数字，收到: {true_cfg!r}")
    if not 1.0 <= true_cfg <= MAX_TRUE_CFG_SCALE:
        raise ParamError(f"true_cfg_scale 需在 1.0~{MAX_TRUE_CFG_SCALE:g} 之间，"
                         f"收到: {true_cfg}")

    negative = payload.get("negative_prompt") or None
    warnings = []
    if negative and true_cfg <= 1.0:
        warnings.append(
            f"已给出 negative_prompt，但 true_cfg_scale={true_cfg:g} ≤ 1 未启用 CFG，"
            f"负提示词被忽略；如需生效请传 true_cfg_scale>1。"
            f"(Qwen-Image-2.1 官方按无引导采样)")

    w, h, capped, ref_index_used = _plan_size(payload, images)

    # output_resolution = 管线缩放参考图的基准(与输出画布解耦)。
    # 关键: 管线的默认 1024 意味着 1536 编辑请求也把参考图压到 1024², 会白丢细节;
    # 但把它抬到出图长边又会让视觉编码器 + 条件 token 成平方级变慢(2026-09-30 实测
    # 30 步编辑 3 分钟仍未出图)。故默认取 min(出图长边, 1024) —— 与文档基准一致,
    # 只有显式传 output_resolution 或 QWEN_OUTPUT_RESOLUTION 才允许超过 1024。
    out_res_raw = payload.get("output_resolution")
    if out_res_raw is None:
        out_res_raw = OUTPUT_RESOLUTION or min(max(w, h), EDIT_DEFAULT_OUTPUT_RESOLUTION)
    try:
        output_resolution = _clamp_output_resolution(out_res_raw)
    except (TypeError, ValueError):
        raise ParamError(f"output_resolution 需为正整数，收到: {out_res_raw!r}")

    warnings += _speed_warnings(w, h, output_resolution, images)
    if images:
        warnings += _ref_warnings(images, ref_index_used)

    seed = payload.get("seed")
    seed = int(seed) if seed not in (None, "") else _random_seed()

    # 响应格式: OpenAI 风格的 "url"(默认) 或 "b64_json"。队列任务一律按 url 处理
    # (网页要的是可访问的链接, 也便于落盘后继续编辑), 所以入队时它会被剥掉 ——
    # 校验因此必须在这里之前做完, 否则入队请求会把这个非法值静默吞掉。
    response_format = _check_response_format(payload.get("response_format"))

    return {
        "prompt": prompt,
        "negative_prompt": negative,
        "n": n,
        "steps": steps,
        "true_cfg_scale": true_cfg,
        "seed": seed,
        "width": w,
        "height": h,
        # 把实际画布也记下来: PATCH/「重新生成」时才能原样复用(不然自定义尺寸会丢)
        "size": f"{w}x{h}",
        "capped": capped,
        "output_resolution": output_resolution,
        "transparent": bool(payload.get("transparent", False)),
        "response_format": response_format,
        "ref_index": ref_index_used,
        "base_url": base_url,
        "warnings": warnings,
    }


def _persist_refs(task_id: str, images) -> list:
    """把参考图写进任务目录, 返回文件名列表(任务记录里只存文件名, 不存 base64)。"""
    d = TASK_DIR / "refs" / task_id
    d.mkdir(parents=True, exist_ok=True)
    names = []
    for i, img in enumerate(images):
        name = f"ref{i + 1}.png"
        img.save(d / name, format="PNG")
        names.append(name)
    return names


def _load_ref_images(task: dict):
    """从任务目录读回参考图(PIL)。"""
    from PIL import Image
    d = TASK_DIR / "refs" / task["id"]
    out = []
    for name in task.get("refs") or []:
        p = d / name
        if not p.is_file():
            raise ParamError(f"参考图已丢失: {name}（请重新提交任务）")
        img = Image.open(p)
        img.load()
        out.append(img)
    return out


# ---------------------------------------------------------------- HTTP 应用
from fastapi import FastAPI, Request, HTTPException        # noqa: E402
from fastapi.middleware.cors import CORSMiddleware         # noqa: E402
from fastapi.staticfiles import StaticFiles                # noqa: E402

app = FastAPI(title="Qwen-Image-2.1", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")


@app.on_event("startup")
def _startup():
    threading.Thread(target=_preload_worker, daemon=True,
                     name="model-preload").start()


STATIC_DIR = BASE_DIR / "static"


@app.get("/")
def root():
    """Web 使用页面"""
    from fastapi.responses import FileResponse
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api")
def api_info():
    return {
        "service": "Qwen-Image-2.1",
        "endpoints": {
            "POST /v1/images/generations": "文生图 (OpenAI 风格); queue=true 或 prompts=[...] 时改为入队异步生成",
            "POST /v1/images/edits": "图片编辑 / 多参考图 (JSON + base64, 最多 10 张)",
            "POST /v1/images/edits/json": "同上, images 亦可为 data URL 或本站 /outputs/ 图片路径; 支持 queue=true / prompts=[...] 批量入队",
            "GET /v1/models": "模型列表",
            "GET /health": "健康检查 / 显存 / 加载状态 / 队列计数",
            "GET /api/queue": "任务队列概览与参数上限",
            "GET /api/tasks": "任务列表 (status / limit / with_outputs 过滤)",
            "GET /api/tasks/{id}": "单条任务详情",
            "GET /api/tasks/{id}/refs/{name}": "取回任务的参考图",
            "PATCH /api/tasks/{id}": "编辑未开始任务的提示词与参数",
            "POST /api/tasks/{id}/cancel": "取消任务(未开始立即取消, 运行中到采样步边界生效)",
            "POST /api/tasks/{id}/retry": "按原参数重新生成(新建一条任务)",
            "POST /api/tasks/{id}/release": "开始单条暂存任务",
            "POST /api/tasks/{id}/hold": "把尚未开始的任务退回暂存",
            "POST /api/queue/release-all": "开始全部暂存任务",
            "POST /api/queue/auto-start": "开关「加入后自动生成」",
            "POST /api/queue/delete": "批量删除任务及其产物",
            "DELETE /api/tasks/{id}": "删除任务及其产物",
            "GET /": "Web 使用页面",
        },
        "notes": {
            "cfg": "Qwen-Image-2.1 按无引导采样, true_cfg_scale 默认 1.0 时 negative_prompt 被忽略; >1 才启用 CFG",
            "multi_ref": "参考图按位置引用, 提示词里写 <image1> <image2> ... 最多 10 张",
            "output_resolution": "参考图缩放基准, 不传则跟随出图长边",
            "ref_index": "多参考图时按第几张定画布长宽比, 默认 0, -1 为最后一张",
            "queue": "任务落盘于 outputs/tasks/tasks.json, 重启后仍在; 未开始的任务可取消/编辑/删除",
            "response_format": "同步接口可传 'url'(默认) 或 'b64_json'; 入队任务一律返回 url",
        },
        "docs": "/docs",
    }


@app.get("/health")
def health():
    import torch
    with TASK_STORE.lock:
        current = TASK_STORE.state["worker"].get("current")
    return {
        "status": "ok",
        "model": "Qwen-Image-2.1",
        "load": STATE["load"],            # idle / loading / ready / error
        "error": STATE["error"],
        "cuda": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "vram": _gpu_mem(),
        "waiting": STATE["waiting"],
        "last_generation": STATE["last_gen"],
        "max_side": MAX_SIDE,
        "default_steps": DEFAULT_STEPS,
        "offload": OFFLOAD,
        "default_true_cfg_scale": DEFAULT_TRUE_CFG_SCALE,
        "output_resolution": OUTPUT_RESOLUTION,
        "edit_output_resolution": EDIT_DEFAULT_OUTPUT_RESOLUTION,
        "ref_index": REF_IMAGE_INDEX,
        "queue": TASK_STORE.counts(),
        "current_task": current,
        # offload 状态曾经损坏过(已自动重建); 非 0 表示重建发生的时间戳
        "offload_recovered_at": OFFLOAD_POISONED["at"] or None,
    }


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [
        {"id": "Qwen-Image-2.1", "object": "model", "owned_by": "Qwen",
         "created": 1789000000}
    ]}


@app.post("/v1/images/generations")
def generations(request: Request, payload: dict):
    return _generate(request, payload, images=None)


@app.post("/v1/images/edits")
def edits(request: Request, payload: dict):
    imgs = payload.get("images", payload.get("image"))
    if imgs is None:
        raise HTTPException(400, "缺少 images 字段 (base64 字符串或数组)")
    if isinstance(imgs, str):
        imgs = [imgs]
    if not isinstance(imgs, list) or not imgs:
        raise HTTPException(400, "images 需为 base64 字符串或数组")
    if len(imgs) > MAX_REF_IMAGES:
        raise HTTPException(400, f"最多支持 {MAX_REF_IMAGES} 张参考图")
    if not _has_prompt(payload):
        raise HTTPException(400, "缺少 prompt 字段")
    if payload.get("prompts"):
        raise HTTPException(400, "prompts 批量提交请用 /v1/images/edits/json")
    try:
        pil_images = [_b64_to_pil(s) for s in imgs]
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _generate(request, payload, images=pil_images)


@app.post("/v1/images/edits/json")
def edits_json(request: Request, payload: dict):
    """编辑任务的 JSON 变体: images 可为 base64、data URL 或 /outputs/... 路径。

    与 /v1/images/edits 等价, 但允许直接把已有结果图当参考图(Web 界面「继续编辑」用),
    也接受 ``prompts: [...]`` 批量入队 —— 网页「多条提示词 + 参考图」走的就是这条。
    """
    imgs = payload.get("images", payload.get("image"))
    if imgs is None:
        raise HTTPException(400, "缺少 images 字段")
    if isinstance(imgs, str):
        imgs = [imgs]
    if not isinstance(imgs, list) or not imgs:
        raise HTTPException(400, "images 需为字符串或数组")
    if len(imgs) > MAX_REF_IMAGES:
        raise HTTPException(400, f"最多支持 {MAX_REF_IMAGES} 张参考图")
    # 注意: 判空必须同时认 prompt 与 prompts —— 网页只发 prompts(数组), 从不发 prompt。
    if not _has_prompt(payload):
        raise HTTPException(400, "缺少 prompt 字段 (或 prompts 数组)")
    try:
        pil_images = [_ref_source_to_pil(s) for s in imgs]
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _generate(request, payload, images=pil_images)


def _pipeline_call_params() -> set:
    """本版 diffusers 的 pipeline 接受哪些关键字(用于裁剪 kwargs)。"""
    global _PIPE_CALL_PARAMS
    if _PIPE_CALL_PARAMS is None:
        _PIPE_CALL_PARAMS = set(inspect.signature(PIPE.__call__).parameters)
    return _PIPE_CALL_PARAMS


def _pipeline_kwargs(base: dict) -> dict:
    allowed = _pipeline_call_params()
    return {k: v for k, v in base.items() if k in allowed}


def _make_step_callback(progress):
    """采样步回调: 每步上报进度(顺带让队列检查取消标记)。

    取消就是在这里抛出 ``Canceled`` 的 —— 它顺着 pipeline 的采样栈一路上抛,
    被队列工作线程捕获后按「已取消」收尾, 不会留下半张图。
    """
    if PIPE is not None and "callback_on_step_end" not in _pipeline_call_params():
        raise RuntimeError("当前 diffusers 版本不支持 callback_on_step_end，无法安全取消任务")
    if progress is None:
        return None

    def _cb(pipe, step_index, timestep, callback_kwargs):
        progress(int(step_index) + 1)
        return callback_kwargs

    return _cb


def run_generation(plan: dict, images, progress=None) -> dict:
    """执行一份生成计划, 返回 (items, usage, warnings)。

    同步 HTTP 接口与队列工作线程共用这一条路径(队列任务额外带上 progress,
    于是可以被实时取消)。任何取消/失败都只抛异常, 由调用方决定如何记录。
    """
    import torch
    w, h = int(plan["width"]), int(plan["height"])
    steps = int(plan["steps"])
    n = int(plan["n"])
    seed = plan.get("seed")
    if seed in (None, ""):
        # 兜底: 老版本「重新生成」会把 seed 从计划里删掉(执行时 KeyError('seed'))。
        # 这类任务已经落在 tasks.json 里了, 这里补一颗随机种子让它还能正常跑完。
        seed = _random_seed()
        log.warning("任务计划缺少 seed, 已补随机种子 %d", seed)
    seed = int(seed)
    true_cfg = float(plan["true_cfg_scale"])
    output_resolution = int(plan["output_resolution"])
    negative = plan.get("negative_prompt")
    base_url = plan.get("base_url") or f"http://{HOST}:{PORT}/"
    real_prompt = _wrap_transparency(plan["prompt"], bool(plan.get("transparent")))
    warnings = list(plan.get("warnings") or [])
    capped = bool(plan.get("capped"))
    # 老任务(落盘时还没有这个键)也走默认值
    response_format = str(plan.get("response_format") or "url").lower()

    step_cb = _make_step_callback(progress)
    STATE["waiting"] += 1
    t_req = time.time()
    try:
        with GEN_LOCK:
            # 内存预检(可选): 按提交余量拦截, 防止 4.4GB 权重搬移把进程崩掉
            mem_avail = psutil.virtual_memory().available / 2**30 if psutil else None
            headroom = _commit_headroom_gb()
            if MIN_MEM_GB > 0:
                val = headroom if headroom is not None else mem_avail
                if val is not None and val < MIN_MEM_GB:
                    raise HTTPException(
                        503, f"系统提交余量不足 ({val:.1f}GB < {MIN_MEM_GB}GB)，"
                             f"为避免服务崩溃已拒绝本次生成。请关闭其他程序后重试，"
                             f"或调低/置 0 QWEN_MIN_MEM_GB。")
            pipe = ensure_pipe()
            if TILING_MODE in ("1", "on", "true", "always"):
                pipe.vae.use_tiling = True
            elif TILING_MODE in ("0", "off", "false", "never"):
                pipe.vae.use_tiling = False
            else:
                pipe.vae.use_tiling = bool(images) or max(w, h) > 1024
            torch.cuda.reset_peak_memory_stats()
            items, t0 = [], time.time()
            wait = t0 - t_req                # 排队等锁时间(无并发时 ≈ 0)
            for i in range(n):
                gen = torch.Generator("cpu").manual_seed(seed + i)
                kwargs = dict(prompt=real_prompt, width=w, height=h,
                              num_inference_steps=steps, generator=gen,
                              true_cfg_scale=true_cfg,
                              output_resolution=output_resolution,
                              callback_on_step_end=step_cb)
                if negative:
                    kwargs["negative_prompt"] = negative
                if images is not None:
                    kwargs["image"] = images[0] if len(images) == 1 else images
                kwargs = _pipeline_kwargs(kwargs)
                try:
                    image = _call_pipeline(pipe, kwargs).images[0]
                except OffloadPoison as poison:
                    # offload 状态被搞坏: 重建管线后原样重试一次。
                    # 用户不需要重启服务, 队列和历史结果都不受影响。
                    OFFLOAD_POISONED["at"] = time.time()
                    OFFLOAD_POISONED["err"] = str(poison)
                    _recover_pipeline(str(poison))
                    pipe = PIPE
                    warnings.append("检测到显存搬运状态异常, 已自动重建模型并重试本次生成")
                    image = _call_pipeline(pipe, kwargs).images[0]
                except RuntimeError as e:
                    # OutOfMemoryError 是 RuntimeError 子类; 权重搬移等路径的 OOM
                    # 以泛 RuntimeError 抛出(仅按消息识别, 其他 RuntimeError 照常上抛)
                    if "out of memory" not in str(e).lower():
                        raise
                    _oom_reset()
                    # OOM 也可能已经把 offload 状态搞坏 —— 先探一下, 别把中毒的
                    # 管线留着给后面的请求(那会导致「一次 OOM, 全服务报废」)。
                    recovered = False
                    try:
                        image = _call_pipeline(pipe, kwargs).images[0]
                    except OffloadPoison as poison:
                        OFFLOAD_POISONED["at"] = time.time()
                        OFFLOAD_POISONED["err"] = str(poison)
                        _recover_pipeline(f"OOM 之后 offload 状态异常: {poison}")
                        pipe = PIPE
                        warnings.append("OOM 后模型状态异常, 已自动重建模型并重试本次生成")
                        image = _call_pipeline(pipe, kwargs).images[0]
                        recovered = True
                    if not recovered:
                        if max(w, h) > 1024:
                            scale = 1024 / max(w, h)
                            w2, h2 = _round16(w * scale), _round16(h * scale)
                            log.warning("OOM，自动降级重试 %dx%d", w2, h2)
                            kwargs["width"], kwargs["height"] = w2, h2
                            # 参考图缩放基准同步降级, 否则条件图仍按原尺寸上卡
                            new_res = _clamp_output_resolution(
                                min(output_resolution, max(w2, h2)))
                            if new_res != output_resolution:
                                output_resolution = new_res
                                kwargs["output_resolution"] = new_res
                                warnings.append(
                                    f"OOM 降级重试：output_resolution 同步降至 {new_res}")
                            if TILING_MODE not in ("1", "on", "true", "always",
                                                    "0", "off", "false", "never"):
                                pipe.vae.use_tiling = bool(images) or max(w2, h2) > 1024
                            image = _call_pipeline(pipe, kwargs).images[0]
                            w, h = w2, h2
                            capped = True
                        else:
                            raise HTTPException(
                                413, "显存不足(OOM)。请降低 size/steps，或设置 "
                                     "QWEN_MAX_SIDE=1024、QWEN_OFFLOAD=sequential 后重启")
                meta = {"seed": seed + i, "width": w, "height": h, "steps": steps}
                items.append(_save_output(image, base_url, meta,
                                          response_format=response_format))

            elapsed = time.time() - t0
            peak = torch.cuda.max_memory_allocated() / 1e6
            tiling_used = bool(pipe.vae.use_tiling)
    finally:
        STATE["waiting"] -= 1

    info = {"elapsed_sec": round(elapsed, 1), "queue_sec": round(wait, 1),
            "vram_peak_mb": int(peak),
            "vae_tiling": tiling_used,
            "true_cfg_scale": true_cfg,
            "output_resolution": output_resolution,
            "mem_avail_gb": round(mem_avail, 1) if mem_avail is not None else None}
    if images:
        info["ref_images"] = len(images)
        info["ref_index"] = plan.get("ref_index")
    if capped:
        info["note"] = f"尺寸已按 QWEN_MAX_SIDE={MAX_SIDE} 缩放"
    if warnings:
        info["warnings"] = warnings
    STATE["last_gen"] = {**info, "size": f"{w}x{h}", "steps": steps}
    log.info("生成完成 %dx%d steps=%d 用时%.1fs 峰值显存%dMB 等待%.1fs",
             w, h, steps, elapsed, peak, info["queue_sec"])
    for m in warnings:
        log.warning("请求提示: %s", m)
    return items, info, warnings


def _run_plan(plan: dict, images) -> dict:
    """同步执行 + 组装 OpenAI 风格响应。"""
    items, info, _ = run_generation(plan, images)
    return {"created": int(time.time()), "model": "Qwen-Image-2.1",
            "data": items, "usage": info}


def _generate(request: Request, payload: dict, images) -> dict:
    """HTTP 生成入口(同步, 兼容既有 OpenAI 风格调用)。

    ``queue: true`` 时改为把任务交给持久化队列并立即返回(202)。
    """
    if _wants_queue(payload):
        return _submit_tasks(request, payload, images)
    try:
        plan = _build_plan(payload, images, base_url=str(request.base_url))
    except ParamError as e:
        raise HTTPException(400, str(e))
    try:
        return _run_plan(plan, images)
    except ParamError as e:
        raise HTTPException(400, str(e))
    except Canceled:
        raise HTTPException(409, "本次生成已被取消")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("生成失败")
        raise HTTPException(500, f"{type(e).__name__}: {e}")


# ================================================================ 任务队列
def _wants_queue(payload: dict) -> bool:
    """是否走异步队列: 显式 queue=true, 或一次提交多条提示词(prompts)。"""
    return bool(payload.get("queue")) or bool(payload.get("prompts"))


def _submit_tasks(request: Request, payload: dict, images) -> dict:
    """把一个请求拆成 N 条任务入队, 立即返回(不占用 HTTP 连接等 GPU)。"""
    base = {k: v for k, v in payload.items()
            if k not in ("prompt", "prompts", "images", "image", "queue",
                         "title", "released", "response_format")}
    prompts = payload.get("prompts")
    if prompts:
        if isinstance(prompts, str):
            prompts = [prompts]
        if not isinstance(prompts, list):
            raise HTTPException(400, "prompts 需为字符串数组")
        prompts = [str(p).strip() for p in prompts]
        prompts = [p for p in prompts if p]
        if not prompts:
            raise HTTPException(400, "prompts 为空")
        if len(prompts) > MAX_QUEUE_BATCH:
            raise HTTPException(400, f"一次最多提交 {MAX_QUEUE_BATCH} 条任务")
    else:
        prompts = [payload.get("prompt")]

    released = payload.get("released")
    released = None if released is None else bool(released)
    base_url = str(request.base_url)
    created = []
    for text in prompts:
        item = dict(base)
        item["prompt"] = text
        try:
            plan = _build_plan(item, images, base_url=base_url)
        except ParamError as e:
            raise HTTPException(400, str(e))
        plan.pop("warnings", None)
        plan.pop("capped", None)
        plan.pop("base_url", None)          # 执行时按当时的服务地址重新填
        kind = "edit" if images else "generation"
        task = TASK_STORE.create(
            kind=kind, prompt=str(text).strip(), params=plan,
            ref_index=plan.get("ref_index"),
            title=payload.get("title") or "",
            source="api", released=released)
        if images:
            names = _persist_refs(task["id"], images)
            with TASK_STORE.lock:
                task["refs"] = names
                TASK_STORE._touch(flush=True)
        created.append(TASK_STORE.view(task))
    if QUEUE:
        QUEUE.wake()
    log.info("已入队 %d 条任务: %s", len(created), ", ".join(t["id"] for t in created))
    return {
        "created": int(time.time()),
        "model": "Qwen-Image-2.1",
        "queued": len(created),
        "data": [{"id": t["id"], "seq": t["seq"], "status": t["status"],
                  "released": not t["staged"], "queue_position": t["queue_position"],
                  "task_url": f"{base_url}api/tasks/{t['id']}"} for t in created],
    }


def _worker_run(task: dict, cancel: threading.Event, progress) -> dict:
    """队列工作线程的执行体: 读回参考图 -> 生成 -> 返回产物。

    progress(step) 里会检查取消标记, 命中就抛 Canceled, 由队列记为「已取消」。
    """
    images = _load_ref_images(task) if task.get("refs") else None
    plan = dict(task["params"])
    plan["base_url"] = f"http://{HOST}:{PORT}/"
    plan.setdefault("warnings", [])
    total = int(plan.get("steps") or 0)

    def on_step(step: int) -> None:
        progress(step, total)

    items, usage, warnings = run_generation(plan, images, progress=on_step)
    return {"outputs": items, "usage": usage, "warnings": warnings}


def _model_usable() -> bool:
    """队列是否可以开工: 只有模型加载失败时才停摆。

    「加载中」刻意算可开工 —— 任务进入 run_generation 后会卡在 ensure_pipe()
    里等模型就绪(等待时间计入 usage.queue_sec), 这样既不失败, 也不依赖
    startup 的预加载线程真的在跑。加载失败时则原地等待, 免得排队任务
    一条条撞同一个错误而全部变成 failed。
    """
    return STATE["load"] != "error"


def _ensure_queue() -> QueueWorker:
    global QUEUE
    if QUEUE is None:
        recovered = TASK_STORE.recover_interrupted()
        if recovered:
            log.warning("有 %d 条任务在上次退出时仍在运行, 已重新排队", recovered)
        QUEUE = QueueWorker(TASK_STORE, _worker_run, can_run=_model_usable)
        QUEUE.start()
        threading.Thread(target=_cleanup_worker, daemon=True,
                         name="outputs-cleanup").start()
    return QUEUE


def _cleanup_worker() -> None:
    """清理无主产物(默认关闭)。

    outputs/ 里可能有用户自己留下的图片、或队列接管之前生成的老图, 自动删除会误伤,
    所以必须显式用 ``QWEN_TASK_CLEAN_ORPHANS=1`` 开启; 开启后也只清理
    「比队列都新」且没有任何任务引用的 PNG(即生成到一半被杀留下的半成品)。
    """
    if os.environ.get("QWEN_TASK_CLEAN_ORPHANS", "0") not in ("1", "true", "on", "yes"):
        return
    if STATE["load"] not in ("ready", "loading"):
        log.info("跳过无主产物清理（未加载模型）")
        return
    try:
        ensure_pipe()
    except Exception:
        return
    cleanup_orphan_outputs(TASK_STORE, OUTPUT_DIR, min_age_sec=300)


@app.get("/api/queue", summary="任务队列概览")
def queue_overview():
    q = _ensure_queue()
    with TASK_STORE.lock:
        current = TASK_STORE.state["worker"].get("current")
    counts = TASK_STORE.counts()
    return {"counts": counts, "current": current, "auto_start": counts["auto_start"],
            "worker": q.status(), "model_load": STATE["load"],
            "max_side": MAX_SIDE, "default_long_side": DEFAULT_LONG_SIDE,
            "min_side": 256, "side_step": 16,
            "default_steps": DEFAULT_STEPS,
            "max_steps": MAX_STEPS, "default_true_cfg_scale": DEFAULT_TRUE_CFG_SCALE,
            "max_true_cfg_scale": MAX_TRUE_CFG_SCALE,
            "max_ref_images": MAX_REF_IMAGES, "max_queue_batch": MAX_QUEUE_BATCH,
            "edit_output_resolution": EDIT_DEFAULT_OUTPUT_RESOLUTION,
            "aspect_ratios": list(ASPECT_RATIOS)}


@app.get("/api/tasks", summary="任务列表")
def tasks_list(status: str = "", limit: int = 0, with_outputs: bool = True):
    _ensure_queue()
    items = TASK_STORE.list(status=status or None, limit=limit or None)
    views = [TASK_STORE.view(t) for t in items]
    if not with_outputs:
        for v in views:
            v.pop("outputs", None)
    return {"counts": TASK_STORE.counts(), "tasks": views}


@app.get("/api/tasks/{task_id}", summary="单条任务详情")
def task_detail(task_id: str):
    try:
        return TASK_STORE.view(TASK_STORE.get(task_id))
    except TaskNotFound:
        raise HTTPException(404, "任务不存在")


@app.get("/api/tasks/{task_id}/refs/{name}", summary="任务的参考图")
def task_ref(task_id: str, name: str):
    from fastapi.responses import FileResponse
    d = (TASK_DIR / "refs" / task_id).resolve()
    path = (d / Path(name).name).resolve()
    if path.parent != d or not path.is_file():
        raise HTTPException(404, "参考图不存在")
    return FileResponse(path, media_type="image/png")


# 编辑任务时允许改动的字段(参考图不支持替换: 直接新建任务更清晰)
_EDITABLE_FIELDS = ("n", "steps", "true_cfg_scale", "seed", "size", "aspect_ratio",
                    "long_side", "output_resolution", "transparent")
# 影响画布尺寸的字段: 一个都没给就沿用原任务画布, 免得"只改提示词"把尺寸换回默认值
_SIZE_FIELDS = ("size", "aspect_ratio", "long_side", "width", "height")


def _replan(cur: dict, payload: dict, images) -> dict:
    """用「原参数 + 本次改动」重跑一遍归一化, 校验规则与新建任务完全一致。"""
    raw = dict(cur["params"])
    raw.pop("warnings", None)
    raw.pop("capped", None)
    raw.pop("width", None)
    raw.pop("height", None)
    raw.pop("base_url", None)
    raw["prompt"] = cur["prompt"]
    raw["negative_prompt"] = cur.get("negative_prompt")
    raw["ref_index"] = cur.get("ref_index")
    for k in _EDITABLE_FIELDS:
        if k in payload:
            raw[k] = payload[k]
    if "prompt" in payload:
        prompt = str(payload["prompt"] or "").strip()
        if not prompt:
            raise HTTPException(400, "提示词不能为空")
        raw["prompt"] = prompt
    if "negative_prompt" in payload:
        raw["negative_prompt"] = payload["negative_prompt"] or None
    # 调用方完全没提尺寸 -> 沿用原任务画布。
    # 老任务(params 里只有 width/height、没有 size)靠这一步才能原样重跑;
    # 否则只改一句提示词就会把画布换回默认的 1024 方形。
    if (not any(k in payload for k in _SIZE_FIELDS)
            and cur["params"].get("width") and cur["params"].get("height")):
        raw["size"] = f"{int(cur['params']['width'])}x{int(cur['params']['height'])}"
    # 尺寸: 显式传 size 就以它为准(与同步接口一致 —— resolve_size 里 size 优先);
    # 只改「比例类」字段时丢掉旧 size, 否则旧尺寸会一直压过新设置。
    # 注意别再写成 "size" in payload 也触发 pop: 那会把调用方刚传进来的 size 删掉,
    # 静默退回长宽比 —— 网页上的「自定义分辨率」就是这么失效的。
    if ("aspect_ratio" in payload or "long_side" in payload
            or "width" in payload or "height" in payload):
        raw.pop("size", None)
    if payload.get("size"):
        raw["size"] = str(payload["size"])
    elif payload.get("width") and payload.get("height"):
        raw["size"] = f"{int(payload['width'])}x{int(payload['height'])}"
    if "seed" in payload and payload["seed"] in (None, ""):
        raw.pop("seed", None)
    try:
        plan = _build_plan(raw, images, base_url="")
    except ParamError as e:
        raise HTTPException(400, str(e))
    plan.pop("warnings", None)
    plan.pop("capped", None)
    plan.pop("base_url", None)
    plan["n"] = int(plan["n"])
    return plan


@app.patch("/api/tasks/{task_id}", summary="编辑未开始的任务")
def task_update(task_id: str, payload: dict):
    _ensure_queue()
    if payload.get("images") is not None:
        raise HTTPException(400, "参考图不支持替换，请新建任务")
    try:
        cur = TASK_STORE.get(task_id)
    except TaskNotFound:
        raise HTTPException(404, "任务不存在")
    if cur["status"] != "pending":
        raise HTTPException(409, "只能编辑未开始的任务；其余任务请用「重新生成」")
    images = _load_ref_images(cur) if cur.get("refs") else None
    plan = _replan(cur, payload, images)
    try:
        task = TASK_STORE.update_params(
            task_id,
            {"prompt": plan["prompt"], "negative_prompt": plan.get("negative_prompt"),
             "title": (str(payload.get("title")).strip() if payload.get("title") else None)},
            ref_index=plan.get("ref_index"))
    except TaskConflict as e:
        raise HTTPException(409, str(e))
    with TASK_STORE.lock:
        task["params"] = plan
        task["progress"] = {"step": 0, "total": int(plan.get("steps") or 0)}
        TASK_STORE._touch(flush=True)
    if QUEUE:
        QUEUE.wake()
    return TASK_STORE.view(task)


@app.post("/api/tasks/{task_id}/cancel", summary="取消任务")
def task_cancel(task_id: str):
    _ensure_queue()
    try:
        t = TASK_STORE.cancel(task_id)
    except TaskNotFound:
        raise HTTPException(404, "任务不存在")
    if QUEUE:
        QUEUE.wake()
    return TASK_STORE.view(t)


@app.post("/api/tasks/{task_id}/release", summary="开始单条暂存任务")
def task_release(task_id: str):
    _ensure_queue()
    try:
        t = TASK_STORE.release(task_id)
    except TaskNotFound:
        raise HTTPException(404, "任务不存在")
    except TaskConflict as e:
        raise HTTPException(409, str(e))
    if QUEUE:
        QUEUE.wake()
    return TASK_STORE.view(t)


@app.post("/api/tasks/{task_id}/hold", summary="退回暂存(尚未开始)")
def task_hold(task_id: str):
    _ensure_queue()
    try:
        t = TASK_STORE.hold(task_id)
    except TaskNotFound:
        raise HTTPException(404, "任务不存在")
    except TaskConflict as e:
        raise HTTPException(409, str(e))
    return TASK_STORE.view(t)


@app.post("/api/tasks/{task_id}/retry", summary="重新生成(新建一条任务)")
def task_retry(task_id: str, payload: Optional[dict] = None):
    """重新生成: 复制原任务的参数与参考图, 新建一条待开始任务。

    刻意不覆盖原记录 —— 历史结果保留, 便于对比同一提示词的不同出图。
    """
    _ensure_queue()
    payload = payload or {}
    try:
        old = TASK_STORE.get(task_id)
    except TaskNotFound:
        raise HTTPException(404, "任务不存在")
    if old["status"] in ("running", "canceling"):
        raise HTTPException(409, "任务正在运行，请先取消再重新生成")

    override = {k: v for k, v in payload.items()
                if k in ("prompt", "negative_prompt", "steps", "true_cfg_scale",
                         "n", "output_resolution", "transparent", "seed", "size",
                         "aspect_ratio", "long_side")}
    if override.get("prompt") is not None and not str(override["prompt"]).strip():
        raise HTTPException(400, "提示词不能为空")
    images = _load_ref_images(old) if old.get("refs") else None
    plan = _replan(old, override, images)
    if not payload.get("keep_seed") and payload.get("seed") in (None, ""):
        # 「换一批随机种子」= 就地生成一颗新种子。注意不能把 seed 从计划里删掉:
        # run_generation 要读 plan["seed"], 删了会让任务一执行就 KeyError('seed')。
        plan["seed"] = _random_seed()
    new = TASK_STORE.create(kind=old["kind"], prompt=plan["prompt"], params=plan,
                            ref_index=plan.get("ref_index"), origin=task_id,
                            source="retry")
    if old.get("refs"):
        src = TASK_STORE.ref_dir(task_id)
        dst = TASK_STORE.ref_dir(new["id"], create=True)
        names = []
        for name in old["refs"]:
            if (src / name).is_file():
                (dst / name).write_bytes((src / name).read_bytes())
                names.append(name)
        with TASK_STORE.lock:
            new["refs"] = names
            TASK_STORE._touch(flush=True)
    if QUEUE:
        QUEUE.wake()
    return TASK_STORE.view(new)


@app.delete("/api/tasks/{task_id}", summary="删除任务及其产物")
def task_delete(task_id: str):
    _ensure_queue()
    try:
        t = TASK_STORE.delete(task_id)
    except TaskNotFound:
        raise HTTPException(404, "任务不存在")
    except TaskConflict as e:
        raise HTTPException(409, str(e))
    return {"deleted": task_id, "outputs_removed": len(t.get("outputs") or [])}


# 注意: 这两条必须挂在 /api/queue/ 下, 不能写成 /api/tasks/delete 或
# /api/tasks/release-all —— 那会被上面的 /api/tasks/{task_id} 参数化路由先匹配掉
# (FastAPI 按注册顺序匹配), 于是变成「任务 ID 叫 delete 的请求」而返回 404。
@app.post("/api/queue/delete", summary="批量删除任务")
def queue_delete_many(payload: dict):
    _ensure_queue()
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids 需为非空数组")
    return TASK_STORE.delete_many([str(i) for i in ids])


@app.post("/api/queue/auto-start", summary="开启/关闭「加入后自动生成」")
def queue_auto_start(payload: dict):
    _ensure_queue()
    res = TASK_STORE.set_auto_start(bool(payload.get("auto_start", True)))
    if QUEUE:
        QUEUE.wake()
    return res


@app.post("/api/queue/release-all", summary="开始所有暂存任务")
def queue_release_all():
    _ensure_queue()
    res = TASK_STORE.set_auto_start(True)
    if QUEUE:
        QUEUE.wake()
    return res


@app.on_event("startup")
def _startup_queue():
    _ensure_queue()
    c = TASK_STORE.counts()
    log.info("任务队列就绪: %s (共 %d 条, 待开始 %d, 暂存 %d)",
             TASK_DIR, c["total"], c["ready"], c["staged"])


if __name__ == "__main__":
    import uvicorn
    log.info("启动服务 http://%s:%d  (模型目录: %s)", HOST, PORT, MODEL_DIR)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
