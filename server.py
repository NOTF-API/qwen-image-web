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
import time
import uuid
import base64
import inspect
import logging
import threading
import subprocess
from pathlib import Path

# ---- 环境变量必须在 import torch 之前设置 ----
# 注: expandable_segments 在 Windows 上不受支持(torch 会警告并忽略), 故不设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# 原生层崩溃(access violation)时在 stderr 打印 C 栈, 便于定位
import faulthandler
faulthandler.enable()

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = Path(os.environ.get("QWEN_MODEL_DIR", BASE_DIR / "model"))
OUTPUT_DIR = Path(os.environ.get("QWEN_OUTPUT_DIR", BASE_DIR / "outputs"))
HOST = os.environ.get("QWEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("QWEN_PORT", "8091"))
MAX_SIDE = int(os.environ.get("QWEN_MAX_SIDE", "1536"))          # 单边上限(8GB 显存)
DEFAULT_LONG_SIDE = int(os.environ.get("QWEN_LONG_SIDE", "1024"))  # 默认出图长边
DEFAULT_STEPS = int(os.environ.get("QWEN_STEPS", "30"))          # 默认采样步数(官方 40)
MAX_STEPS = int(os.environ.get("QWEN_MAX_STEPS", "60"))
# 真 CFG 开关: Qwen-Image-2.1 官方按「无引导」采样, 管线默认 true_cfg_scale=1.0(关闭)。
# >1 时才启用 CFG, 且必须同时给 negative_prompt, 两者缺一则负提示词被忽略(管线只发警告)。
# 注意: 本项目此前暴露的 guidance_scale 并非本管线参数, 会被静默丢弃(已删除, 见 _generate)。
DEFAULT_TRUE_CFG_SCALE = float(os.environ.get("QWEN_TRUE_CFG_SCALE", "1.0"))
MAX_TRUE_CFG_SCALE = float(os.environ.get("QWEN_MAX_TRUE_CFG_SCALE", "20"))
# 参考图缩放基准(output_resolution): 管线按此值把每张参考图等比缩到长边上限,
# 并与输出画布分开。不传时管线默认 1024 —— 于是 1536 的编辑请求也把参考图压到 1024²,
# 白白丢细节。这里默认跟随出图长边, 可用 QWEN_OUTPUT_RESOLUTION 固定。
OUTPUT_RESOLUTION = int(os.environ.get("QWEN_OUTPUT_RESOLUTION", "0")) or None
# 多参考图时按第几张定画布长宽比(管线内部用最后一张, 本案默认第一张, 即内容/主体图)
REF_IMAGE_INDEX = int(os.environ.get("QWEN_REF_INDEX", "0"))
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

# 官方推荐的长宽比 -> 尺寸表 (在长边缩放后按 16 的倍数取整)
ASPECT_RATIOS = {
    "1:1": (1, 1), "4:3": (4, 3), "3:4": (3, 4), "3:2": (3, 2),
    "2:3": (2, 3), "16:9": (16, 9), "9:16": (9, 16),
}

# ---------------------------------------------------------------- pipeline 加载
PIPE = None
LOAD_LOCK = threading.Lock()
GEN_LOCK = threading.Lock()          # GPU 串行化: 并发请求排队
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
                 ref_wh=None) -> tuple:
    """返回 (width, height)，16 的倍数，长边不超过 MAX_SIDE"""
    long_side = long_side or DEFAULT_LONG_SIDE
    capped = False
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


def _save_or_encode(image, response_format: str, base_url: str, meta: dict) -> dict:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    raw = buf.getvalue()
    if response_format == "b64_json":
        item = {"b64_json": base64.b64encode(raw).decode()}
    else:
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
        (OUTPUT_DIR / name).write_bytes(raw)
        item = {"url": f"{base_url}outputs/{name}"}
    item.update(meta)
    return item


def _oom_reset():
    import torch
    torch.cuda.empty_cache()
    gc.collect()


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
            "POST /v1/images/generations": "文生图 (OpenAI 风格)",
            "POST /v1/images/edits": "图片编辑 / 多参考图 (JSON + base64, 最多 10 张)",
            "GET /v1/models": "模型列表",
            "GET /health": "健康检查 / 显存 / 加载状态",
            "GET /": "Web 使用页面",
        },
        "notes": {
            "cfg": "Qwen-Image-2.1 按无引导采样, true_cfg_scale 默认 1.0 时 negative_prompt 被忽略; >1 才启用 CFG",
            "multi_ref": "参考图按位置引用, 提示词里写 <image1> <image2> ... 最多 10 张",
            "output_resolution": "参考图缩放基准, 不传则跟随出图长边",
            "ref_index": "多参考图时按第几张定画布长宽比, 默认 0, -1 为最后一张",
        },
        "docs": "/docs",
    }


@app.get("/health")
def health():
    import torch
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
        "ref_index": REF_IMAGE_INDEX,
    }


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [
        {"id": "Qwen-Image-2.1", "object": "model", "owned_by": "Qwen",
         "created": 1789000000}
    ]}


@app.post("/v1/images/generations")
def generations(request: Request, payload: dict):
    prompt = payload.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise HTTPException(400, "缺少 prompt 字段")
    return _generate(request, payload, images=None)


@app.post("/v1/images/edits")
def edits(request: Request, payload: dict):
    prompt = payload.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise HTTPException(400, "缺少 prompt 字段")
    imgs = payload.get("images", payload.get("image"))
    if imgs is None:
        raise HTTPException(400, "缺少 images 字段 (base64 字符串或数组)")
    if isinstance(imgs, str):
        imgs = [imgs]
    if not isinstance(imgs, list) or not imgs:
        raise HTTPException(400, "images 需为 base64 字符串或数组")
    if len(imgs) > 10:
        raise HTTPException(400, "最多支持 10 张参考图")
    try:
        pil_images = [_b64_to_pil(s) for s in imgs]
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _generate(request, payload, images=pil_images)


def _generate(request: Request, payload: dict, images) -> dict:
    import torch
    prompt = payload["prompt"]
    warnings = []                     # 非致命提示, 随响应返回(如负提示词被忽略)
    n = int(payload.get("n", 1))
    if n < 1 or n > 4:
        raise HTTPException(400, "n 需在 1~4 之间")
    steps = int(payload.get("steps", payload.get("num_inference_steps", DEFAULT_STEPS)))
    if steps < 1 or steps > MAX_STEPS:
        raise HTTPException(400, f"steps 需在 1~{MAX_STEPS} 之间")
    response_format = payload.get("response_format", "url")
    if response_format not in ("url", "b64_json"):
        raise HTTPException(400, "response_format 只支持 url / b64_json")
    transparent = bool(payload.get("transparent", False))
    seed = payload.get("seed")
    seed = int(seed) if seed is not None else int.from_bytes(os.urandom(4), "big")
    negative = payload.get("negative_prompt")

    # 真 CFG (true_cfg_scale) 才是本管线的引导开关; 只有它 >1 且同时给了
    # negative_prompt, 负提示词才真正参与采样。官方按无引导采样, 故默认 1.0。
    true_cfg = payload.get("true_cfg_scale")
    if true_cfg is None:
        true_cfg = DEFAULT_TRUE_CFG_SCALE
    try:
        true_cfg = float(true_cfg)
    except (TypeError, ValueError):
        raise HTTPException(400, f"true_cfg_scale 需为数字，收到: {true_cfg!r}")
    if not 1.0 <= true_cfg <= MAX_TRUE_CFG_SCALE:
        raise HTTPException(400, f"true_cfg_scale 需在 1.0~{MAX_TRUE_CFG_SCALE:g} 之间，"
                                 f"收到: {true_cfg}")
    if negative and true_cfg <= 1.0:
        warnings.append(
            f"已给出 negative_prompt，但 true_cfg_scale={true_cfg:g} ≤ 1 未启用 CFG，"
            f"负提示词被忽略；如需生效请传 true_cfg_scale>1。"
            f"(Qwen-Image-2.1 官方按无引导采样)")

    try:
        w, h, capped = resolve_size(payload.get("size"), payload.get("aspect_ratio"),
                                    payload.get("long_side"))
    except ValueError as e:
        raise HTTPException(400, str(e))

    # output_resolution = 管线缩放参考图的基准(与输出画布解耦)。不传时默认跟随
    # 输出长边, 否则管线会用它的默认 1024 把 1536 编辑请求的参考图压到 1024²。
    out_res_raw = payload.get("output_resolution")
    if out_res_raw is None:
        out_res_raw = OUTPUT_RESOLUTION or max(w, h)
    try:
        output_resolution = _clamp_output_resolution(out_res_raw)
    except (TypeError, ValueError):
        raise HTTPException(400, f"output_resolution 需为正整数，收到: {out_res_raw!r}")

    ref_index_used = None
    if images:
        try:
            ref_index_used, ref_img = pick_ref_image(images, payload.get("ref_index"))
            w, h, capped = resolve_size(payload.get("size"), payload.get("aspect_ratio"),
                                        payload.get("long_side"),
                                        ref_wh=(ref_img.width, ref_img.height))
        except ValueError as e:
            raise HTTPException(400, str(e))
        # 画布比例取自 ref_index 那张, 而管线内部按最后一张推导。比例差得多时
        # 条件图会被缩成与画布不同的形状, 这里显式提示。
        last = images[-1]
        if len(images) > 1 and ref_index_used != len(images) - 1 and last.height:
            ra, rb = images[ref_index_used].width * last.height, images[ref_index_used].height * last.width
            if max(ra, rb) / max(1, min(ra, rb)) > 1.15:
                warnings.append(
                    f"参考图比例不一致：画布按第 {ref_index_used + 1} 张，管线内部按最后一张"
                    f"(第 {len(images)} 张) 推导；差异较大时条件图可能变形，"
                    f"建议统一参考图比例或用 ref_index=-1 对齐")

    real_prompt = _wrap_transparency(prompt, transparent)
    base_url = str(request.base_url)

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
            # VAE 分块策略: auto=编辑或长边>1024开(否则OOM), 文生图<=1024关(消除接缝网格)
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
                              output_resolution=output_resolution)
                if negative:
                    kwargs["negative_prompt"] = negative
                if images is not None:
                    kwargs["image"] = images[0] if len(images) == 1 else images
                kwargs = _filter_kwargs(pipe.__call__, kwargs)
                try:
                    image = pipe(**kwargs).images[0]
                except RuntimeError as e:
                    # OutOfMemoryError 是 RuntimeError 子类; 权重搬移等路径的 OOM
                    # 以泛 RuntimeError 抛出(仅按消息识别, 其他 RuntimeError 照常上抛)
                    if "out of memory" not in str(e).lower():
                        raise
                    _oom_reset()
                    # 长边 > 1024 时自动降级重试一次
                    if max(w, h) > 1024:
                        scale = 1024 / max(w, h)
                        w2, h2 = _round16(w * scale), _round16(h * scale)
                        log.warning("OOM，自动降级重试 %dx%d", w2, h2)
                        kwargs["width"], kwargs["height"] = w2, h2
                        # 参考图缩放基准同步降级, 否则条件图仍按原尺寸上卡
                        new_res = _clamp_output_resolution(min(output_resolution, max(w2, h2)))
                        if new_res != output_resolution:
                            output_resolution = new_res
                            kwargs["output_resolution"] = new_res
                            warnings.append(
                                f"OOM 降级重试：output_resolution 同步降至 {new_res}")
                        if TILING_MODE not in ("1", "on", "true", "always",
                                                "0", "off", "false", "never"):
                            pipe.vae.use_tiling = bool(images) or max(w2, h2) > 1024
                        image = pipe(**kwargs).images[0]
                        w, h = w2, h2
                        capped = True
                    else:
                        raise HTTPException(
                            413, "显存不足(OOM)。请降低 size/steps，或设置 "
                                 "QWEN_MAX_SIDE=1024、QWEN_OFFLOAD=sequential 后重启")
                meta = {"seed": seed + i, "width": w, "height": h, "steps": steps}
                items.append(_save_or_encode(image, response_format, base_url, meta))

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
        info["ref_index"] = ref_index_used
    if capped:
        info["note"] = f"尺寸已按 QWEN_MAX_SIDE={MAX_SIDE} 缩放"
    if warnings:
        info["warnings"] = warnings
    STATE["last_gen"] = {**info, "size": f"{w}x{h}", "steps": steps}
    log.info("生成完成 %dx%d steps=%d 用时%.1fs 峰值显存%dMB 等待%.1fs",
             w, h, steps, elapsed, peak, info["queue_sec"])
    for m in warnings:
        log.warning("请求提示: %s", m)
    return {"created": int(time.time()), "model": "Qwen-Image-2.1",
            "data": items, "usage": info}


if __name__ == "__main__":
    import uvicorn
    log.info("启动服务 http://%s:%d  (模型目录: %s)", HOST, PORT, MODEL_DIR)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
