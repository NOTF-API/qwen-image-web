# Qwen-Image-2.1 本地服务（非 ComfyUI）

像 Ollama 一样一条命令启动 HTTP 服务，JS 直接调接口生成图片。
针对 **RTX 5060 Ti 8GB** 做了量化 + offload 适配，全部走国内镜像下载。

## 架构

| 组件 | 方案 | 显存占用 |
|---|---|---|
| DiT（7B 视觉生成） | unsloth GGUF **Q4_K_M**（diffusers 原生 GGUF 加载） | ~4.2 GB |
| 文本编码器 Qwen3-VL 8B（BF16 17.6GB） | bitsandbytes **NF4** 量化 | ~5 GB |
| VAE（RGBA 64ch） | BF16 | 1.35 GB |
| 调度 | `enable_model_cpu_offload()` 组件轮流上卡 | 峰值约 6.5~7 GB |

- 推理框架：**Diffusers** `QwenImage21Pipeline`（官方 Day 0 支持），不依赖 ComfyUI
- 服务：FastAPI，OpenAI 风格接口，端口 **8091**（与官方 vLLM 示例一致）
- 依赖镜像：pypi=清华、torch=上交 SJTU pytorch-wheels、模型=ModelScope（32MB/s）

## 目录结构

```
qwen-image/
├─ server.py              服务端 (FastAPI)
├─ start.bat              启动入口
├─ static/index.html      Web 使用页面 (GET / 直接返回)
├─ model/                 模型权重 (scripts/download_model.ps1 下载)
├─ outputs/               生成的图片
├─ examples/client.mjs    JS 调用示例
├─ scripts/download_model.ps1   模型下载脚本 (可断点续传)
├─ wheels/                torch/torchvision 本地 wheel (cu128)
└─ venv/                  Python 3.11 虚拟环境
```

## 启动

```bat
start.bat
```

启动时会显示**量化模型选择菜单**（`model/transformer/` 下的 .gguf 都会出现，回车 = 默认 Q4_K_M）：

| 量化 | 文件大小 | 说明 |
|---|---|---|
| Q4_K_M | 3.9 GB | 默认，速度/质量平衡 |
| Q5_K_S | 4.2 GB | 质量略高，速度接近 Q4 |
| Q5_K_M | 5.0 GB | 质量最高，1024²@30步约 104 秒 |

跳过菜单直接指定：`start.bat Q5_K_S`（等价 `powershell -File start.ps1 Q5_K_S`，支持部分匹配如 `Q4`）。
也可以手动设 `QWEN_GGUF` 环境变量后 `python -u server.py`。

启动后浏览器打开 **http://127.0.0.1:8091/** 即为可视化使用页面（提示词/长宽比/步数/
seed/张数/透明背景/引导强度/参考图上传，带加载状态、耗时统计与会话画廊）；纯 API 调用见下文。

首次启动会在后台预加载模型（**实测冷启动 110~120 秒**），`/health` 显示
`load: loading -> ready`。之后每次生成（RTX 5060 Ti 8GB 实测）：

| 请求 | 耗时 | 显存峰值 |
|---|---|---|
| 1024×1024 / 30 步（auto，分块关） | **98 秒** | 7752 MB |
| 1024×1024 / 30 步（恒开分块，对照） | 111 秒 | 6421 MB |
| 1024×576（16:9）/ 30 步 | **49 秒** | 6431 MB |
| 1536×1536 / 8 步（auto，分块开） | 68 秒 | 6421 MB |
| 1024×1024 / 2 步（仅测速） | 12~20 秒 | 6423 MB |

> ⚠️ **步数不要低于 12**：本模型非少步蒸馏版（官方建议 40 步），步数太少去噪不收敛，
> 出图会是模糊色块+规则网格纹（"格子图"）。上表 2 步行仅用于速度测量，画质不可用；
> 正式出图建议 20~40 步（默认 30）。

## API

### 1. 文生图 `POST /v1/images/generations`

```jsonc
{
  "prompt": "A neon shop sign that reads \"QWEN IMAGE 2.1\", rainy night",
  "size": "1024x1024",          // 或 "aspect_ratio": "16:9"，或 "long_side": 1280
  "steps": 30,                   // 1~60 (官方默认 40，这里默认 30)
  "seed": 42,                    // 可选，不传随机；响应带回实际 seed
  "true_cfg_scale": 1.0,         // 可选，引导强度。1.0=官方默认(无引导)，>1 才启用 CFG
  "negative_prompt": "blurry",   // 可选，但只有 true_cfg_scale>1 时才会生效(见下)
  "transparent": false,          // true = 生成透明 PNG (RGBA)
  "n": 1,                        // 1~4 张
  "response_format": "url"       // "url" | "b64_json"
}
```

> ⚠️ **关于 CFG 与负提示词**：Qwen-Image-2.1 是**按「无引导」采样**训练的，管线
> `true_cfg_scale` 默认 `1.0`，此时 `negative_prompt` **会被忽略**（diffusers 内部只打一条
> warning）。要让负提示词真正参与采样，必须**同时**给 `true_cfg_scale > 1`。代价是每步多一次
> 前向，明显更慢；官方默认不用引导，所以除非确有必要，建议保持 `1.0`。
> 本服务会在这种情况下于响应 `usage.warnings` 里明确告知，不再静默失败。
> （早期版本暴露的 `guidance_scale` **不是本管线的参数**，会被静默丢弃，现已移除。）

响应：

```jsonc
{
  "created": 1790000000,
  "model": "Qwen-Image-2.1",
  "data": [
    { "url": "http://127.0.0.1:8091/outputs/20260926-xxxx-ab12cd34.png",
      "seed": 42, "width": 1024, "height": 1024, "steps": 30 }
  ],
  "usage": { "elapsed_sec": 41.2, "vram_peak_mb": 6890, "queue_sec": 0.1,
            "vae_tiling": false,          // 本次是否用了 VAE 分块解码 (auto 策略结果)
            "true_cfg_scale": 1.0,        // 本次实际用的引导强度 (1.0 = 无引导)
            "output_resolution": 1024,    // 本次参考图缩放基准
            "mem_avail_gb": 2.4 }   // 生成前的系统可用内存(观测/归因用)
}
```

### 2. 图片编辑 / 多参考图 `POST /v1/images/edits`

最多 10 张参考图，base64（普通 base64 或 dataURL 均可）：

```jsonc
{
  "prompt": "把背景换成日落海滩",
  "images": ["data:image/png;base64,iVBORw0KGgo..."],
  "steps": 30,
  "true_cfg_scale": 1.0,          // 可选，同文生图；>1 时 negative_prompt 才生效
  "output_resolution": 1024,      // 可选，参考图缩放基准；不传=跟随出图长边
  "ref_index": 0                  // 可选，按第几张参考图定画布长宽比；-1 = 最后一张
}
```

不传 `size` / `aspect_ratio` 时，画布长宽比跟随第 `ref_index` 张参考图（默认第 1 张）。

#### 多参考图怎么"指哪张"

模型是**按位置**引用参考图的：上传的第 N 张在提示词里就是 `<imageN>`。也就是说多张参考图
不是"混在一起"，而是可以显式指定角色 —— 这正是官方「角色 / 产品 / 背景 / 风格各一张」的用法：

```jsonc
{
  "prompt": "把 <image2> 的配色和画面风格应用到 <image1> 的产品上，保留 <image1> 的材质细节",
  "images": ["<产品图 base64>", "<风格参考图 base64>"],
  "steps": 30
}
```

Web 页面上的参考图缩略图会标出 `image1`、`image2`… 的序号，方便直接对照书写。

> **参考图比例建议保持一致**：画布长宽比取自 `ref_index` 指定的那张，而 diffusers 管线内部
> 是用**最后一张**推导条件图缩放比例的。两者比例差得多时条件图会被缩成与画布不同的形状，
> 容易变形；此时响应 `usage.warnings` 会给出提示。统一参考图比例，或显式传 `ref_index: -1`
> 对齐管线内部语义即可。

#### output_resolution 为什么重要

diffusers 管线是按 `output_resolution`（默认 1024）把每张参考图等比缩到长边上限的，
**它和输出画布是两件事**。不显式传的话，`size: 1536x1536` 的编辑请求也会把参考图压到 1024²，
白白丢掉细节。本服务默认让 `output_resolution` 跟随出图长边（1024/1536…），
必要时也可用 `output_resolution` 显式压小以省显存。

（实测：首次编辑含视觉编码器预热约 130~190 秒，显存峰值 **8124~8378 MB**——贴近 8GB 上限，建议编辑时不要同时开占显存的程序。编辑路径**始终使用 VAE 分块解码**（auto 策略下不分块会 OOM，2026-09-27 实测 500）。）

### 3. 其它

- `GET /health` — 加载状态 / 显存 / 排队数
- `GET /v1/models` — OpenAI 兼容模型列表
- `GET /docs` — Swagger 交互式文档

## JS 调用（浏览器 / Node 通用）

```js
const res = await fetch("http://127.0.0.1:8091/v1/images/generations", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    prompt: "一只戴毛线帽的柴犬，水彩风格",
    aspect_ratio: "16:9",
    steps: 30,
    response_format: "b64_json",   // 浏览器可直接 <img src="data:image/png;base64,...">
  }),
});
const { data, usage } = await res.json();
console.log(usage.elapsed_sec, "秒");
// data[0].b64_json -> 存文件/显示;  或 response_format:"url" -> data[0].url 直接 <img src>
```

CORS 已全开（`*`），浏览器任意端口可直接调用。完整示例见 `examples/client.mjs`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `QWEN_PORT` | 8091 | 端口 |
| `QWEN_HOST` | 127.0.0.1 | 改 `0.0.0.0` 可局域网访问 |
| `QWEN_LONG_SIDE` | 1024 | 默认出图长边（8GB 卡建议 1024~1280） |
| `QWEN_MAX_SIDE` | 1536 | 长边硬上限（超了自动缩放并提示） |
| `QWEN_STEPS` | 30 | 默认步数（官方 40 更精细但更慢） |
| `QWEN_TRUE_CFG_SCALE` | 1.0 | 默认引导强度。**1.0 = 官方无引导采样**，此时 `negative_prompt` 被忽略；设 >1 才启用 CFG（更慢）。单次请求可用 `true_cfg_scale` 覆盖 |
| `QWEN_OUTPUT_RESOLUTION` | 跟随出图长边 | 参考图缩放基准（管线 `output_resolution`）。设 0/不设 = 自动跟随长边；显式固定可省显存。单次请求可用 `output_resolution` 覆盖 |
| `QWEN_REF_INDEX` | 0 | 多参考图时按第几张定画布长宽比（0 = 第一张；-1 = 最后一张，与管线内部语义一致）。单次请求可用 `ref_index` 覆盖 |
| `QWEN_OFFLOAD` | model | `sequential`=更省显存更慢；`none`=显存全上卡 |
| `QWEN_GGUF` | 自动选 Q4_K_M | 换量化档位，如 `qwen-image-2.1-Q5_K_M.gguf` |
| `QWEN_VAE_TILING` | auto | VAE 分块解码：`auto`=编辑/长边>1024 自动开，文生图≤1024 关（消除 192px 分块接缝网格，实测省 13s）；`1`=恒开（有接缝）；`0`=恒关（编辑/大图会 OOM） |
| `QWEN_MODEL_DIR` | `./model` | 模型目录 |
| `QWEN_MIN_MEM_GB` | 0（关闭） | 可选：按**提交余量**（Windows GlobalMemoryStatusEx）拦截，余量低于此 GB 数时 503，防 4.4GB 权重搬移触发原生崩溃。正常运行余量约 6~7GB，建议 `5`；`0`=关闭 |

**尺寸换算（实测）**：8GB 卡下 1024×1024@30步 ≈ 98s（auto 分块关）；16:9(1024×576)@30步 ≈ 49s；
1536×1536@8步 ≈ 68s（auto 分块开，6421MB；**该尺寸解码不分块会 OOM**，编辑路径同理，2026-09-27 实测）。
收到 OOM 时服务会自动降到 1024 重试一次。

**提示词技巧**：
- 透明图：`transparent: true` 自动套官方推荐句式（或在 prompt 里写
  "This is an RGBA image with transparency. ..."）
- 长宽比：官方 2K 比例表 1:1 / 4:3 / 3:4 / 3:2 / 2:3 / 16:9 / 9:16 都支持

## 模型与依赖重装（全国内镜像）

```powershell
# 1) 模型 (~23GB, ModelScope 32MB/s, 断点续传)
powershell -ExecutionPolicy Bypass -File scripts\download_model.ps1

# 2) 依赖 (在 venv 内)
venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ^
  "transformers>=5.17" accelerate safetensors huggingface_hub bitsandbytes ^
  fastapi "uvicorn[standard]" pillow requests
# torch 必须用 cu128 本地 wheel (5060 Ti Blackwell sm_120 不支持 CPU 版/老 CUDA):
venv\Scripts\python.exe -m pip install --force-reinstall --no-deps ^
  "wheels\torch-2.11.0+cu128-cp311-cp311-win_amd64.whl" ^
  "wheels\torchvision-0.26.0+cu128-cp311-cp311-win_amd64.whl"
# 3) diffusers main (PyPI 0.40.0 还没有 QwenImage21Pipeline)
venv\Scripts\python.exe -m pip install "https://codeload.github.com/huggingface/diffusers/tar.gz/refs/heads/main"
```

torch wheel 来源（37MB/s）：`https://mirror.sjtu.edu.cn/pytorch-wheels/cu128/`

## 故障排查

| 现象 | 处理 |
|---|---|
| 负提示词"没用" | 正常现象：Qwen-Image-2.1 按无引导采样，`true_cfg_scale` 默认 1.0，此时 `negative_prompt` 被忽略（响应 `usage.warnings` 会告知）。要生效就传 `true_cfg_scale` > 1，代价是每步多一次前向、明显变慢 |
| 编辑时参考图细节被压掉 | 旧版没把 `output_resolution` 传给管线，管线按默认 1024 缩放参考图，`size: 1536` 也一样。现已默认跟随出图长边；如需进一步省显存可显式传小 |
| 多参考图出图变形 / 构图被裁 | 画布比例取自 `ref_index`（默认第 1 张），而管线内部按最后一张缩放条件图。统一参考图比例，或传 `ref_index: -1` 对齐；响应 `usage.warnings` 会提示比例不一致 |
| `/health` 一直 `loading` | 冷启动 1~3 分钟（NF4 量化 17.6GB 权重）；看服务控制台日志 |
| `torch.cuda.is_available()=False` | torch 装成了 CPU 版，重装上面第 2 步的 cu128 wheel |
| OOM (HTTP 413) | 降 `QWEN_MAX_SIDE=1024`，或 `QWEN_OFFLOAD=sequential`，关浏览器/游戏释放显存 |
| 生成时返回 503「提交余量不足」 | 开启了 `QWEN_MIN_MEM_GB` 提交余量预检（默认关闭）；关闭其他大内存程序，或把阈值调低/设 `0` |
| 服务进程崩溃(access violation / 闪退) | 多为内存·显存被其他进程挤占（2026-09-26 实测：外部进程占 4.5GB 显存 + 可用内存 1.7GB 时，TE 搬回 CPU 触发 0xc0000005）。保持 4GB+ 可用内存；`faulthandler` 会把崩溃时的 C 栈打到控制台 |
| 想要更高画质 | 启动时选 `Q5_K_M`/`Q5_K_S`（见上文「启动」），或设 `QWEN_GGUF=qwen-image-2.1-Q5_K_M.gguf`；两档显存峰值都在 ~6.4GB（以 `/health` 的 `vram_peak_mb` 为准）。**注意**：Q5_K_M 搬运需 5GB 提交内存，16GB 内存机器连续生成可能在第 2 张触发原生崩溃（2026-09-27 插桩实测：解码前提交余量仅剩 1.25GB < 需求 4.99GB）；加内存到 32GB 后余量充足即无此问题 |
| 出图有规则网格纹 | 分两类：①**低步数**（<12）的细网格是模型非蒸馏特性，加步数到 20~40；②**高步数残留的淡线**是 VAE 分块解码接缝（每 192px 一条，1024² 正好 5×5），默认 `QWEN_VAE_TILING=auto` 已在文生图 ≤1024 自动关闭。与量化档位无关（Q4/Q5 实测同样表现），同 seed 开关分块差值仅 ~0.5/255 灰阶。完整排查过程见 `docs/grid-artifact-fix.md` |

## 许可

模型权重遵循 **Qwen Research License**（非商用需另行申请）。
