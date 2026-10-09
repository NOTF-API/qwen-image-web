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
├─ server.py              服务端 (FastAPI) ＋ 队列 API
├─ taskqueue.py           持久化任务队列 (tasks.json + 单工作线程 + 采样步级取消)
├─ start.bat              启动入口
├─ static/index.html      Web 生图工作台 (GET / 直接返回)
├─ model/                 模型权重 (scripts/download_model.ps1 下载)
│  ├─ transformer/        DiT 量化权重 (*.gguf, 启动菜单里选)
│  └─ noctq/              Noct-Q 的原始 int8 单文件 (转成 GGUF 后可删, 约 7GB)
├─ outputs/               生成的图片
│  └─ tasks/              任务队列落盘目录: tasks.json + refs/<任务ID>/参考图
├─ examples/client.mjs    JS 调用示例
├─ scripts/download_model.ps1   模型下载脚本 (可断点续传)
├─ scripts/download_noctq.ps1  Noct-Q 无审查 DiT 下载 (可断点续传)
├─ scripts/noctq_to_gguf.py    Noct-Q int8 (ComfyUI 单文件) -> GGUF 转换
├─ scripts/check_noctq_gguf.py 校验 Noct-Q GGUF 能加载且权重与源一致 (不占 GPU)
├─ scripts/test_taskqueue.py    队列离线自测 (假生成, 秒级)
├─ scripts/test_server_queue.py 队列 HTTP 冒烟测试 (临时输出目录, 不加载模型)
├─ scripts/test_queue_gpu.py    队列 + 真模型集成测试 (需要 GPU)
├─ scripts/test_queue_loading.py 模型加载期入队测试 (需要 GPU)
├─ scripts/check_web.py   Web 页面内联脚本静态检查 (id 引用/接口路径)
├─ scripts/test_web_clicks.py  页面交互测试 (点击语义/固定行序/分辨率校验/图片查看器)
├─ scripts/test_api_live.py   对运行中的服务做接口完备性 + 网页一致性测试
├─ scripts/check_api_doc.py   核对 docs/API_FOR_AI.md 与真实实现是否一致
├─ scripts/test_oom_recovery_unit.py  OOM 后自动恢复的控制流测试（不占 GPU, 秒级）
├─ docs/API_QUICK.md          **给 AI / Agent 读的精简接口说明**（可直接贴进系统提示词）
├─ docs/API_FOR_AI.md         **给 AI / Agent 读的完整接口文档**
├─ wheels/                torch/torchvision 本地 wheel (cu128)
└─ venv/                  Python 3.11 虚拟环境
```

## 快速开始

> ⚠️ **首次使用请先看本节**；已经装好的直接跳到「[启动](#启动)」。

**环境要求**：Windows 10/11 + Python 3.11 + NVIDIA 显卡（显存 ≥8GB，内存 ≥32GB 更稳）。
仓库里的 `model/`、`venv/`、`wheels/` 都已在 `.gitignore` 中，**clone 下来是空的**，需要按下面 5 步准备。

```powershell
# 1) 拉代码
git clone https://github.com/NOTF-API/qwen-image-web.git
cd qwen-image-web

# 2) 建虚拟环境 (必须 3.11)
python -m venv venv
venv\Scripts\python.exe -m pip install -U pip

# 3) 装依赖 —— torch / diffusers 见下面第 4 步, 其余走 requirements.txt
venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 4) torch 必须用 cu128 本地 wheel, diffusers 必须用 main (PyPI 还没有 QwenImage21Pipeline)
venv\Scripts\python.exe -m pip install --force-reinstall --no-deps `
  "wheels\torch-2.11.0+cu128-cp311-cp311-win_amd64.whl" `
  "wheels\torchvision-0.26.0+cu128-cp311-cp311-win_amd64.whl"
venv\Scripts\python.exe -m pip install "https://codeload.github.com/huggingface/diffusers/tar.gz/refs/heads/main"

# 5) 拉模型权重 (~23GB, ModelScope 32MB/s, 断点续传)
powershell -ExecutionPolicy Bypass -File scripts\download_model.ps1
```

`wheels/` 里的 torch wheel 不在仓库中（`wheels/` 已 gitignore），从
`https://mirror.sjtu.edu.cn/pytorch-wheels/cu128/` 下载后放进 `wheels/`。

装完自检：

```powershell
venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

输出应类似 `2.11.0+cu128 True`。若 `False`，说明装成了 CPU 版，回到第 4 步。

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
| `noctq-v4-Q4` | 3.9 GB | 无审查版 DiT（Noct-Q），见下文「[无审查版 DiT](#无审查版-ditnoct-q)」 |

跳过菜单直接指定：`start.bat Q5_K_S`（等价 `powershell -File start.ps1 Q5_K_S`，支持部分匹配如 `Q4`）。
也可以手动设 `QWEN_GGUF` 环境变量后 `python -u server.py`。

启动后浏览器打开 **http://127.0.0.1:8091/** 即为**生图工作台**（左侧建任务：提示词/长宽比/
分辨率（长边或自定义宽×高）/步数/seed/张数/透明背景/引导强度/参考图上传；右侧任务队列：
暂存、开始、取消、编辑提示词、重新生成、下载、删除，以及每条的进度、耗时与出图缩览）。任务会落盘，
**重启服务后仍在**，可继续编辑、重新生成或删除。纯 API 调用见下文。

> 行内 **↓** = 一次下完这条任务的全部产物（多张时按顺序逐张下载，浏览器可能询问是否允许
> 下载多个文件）；还没出图的任务该按钮置灰。编辑窗与图片查看器都可以**点窗口外的遮罩**或
> 按 **Esc** 关闭（输入法组词中的 Esc 会先让输入法取消候选，不关窗）。

> 分辨率输入框实时显示最终画布：超过 1024 会黄字提示耗时/显存风险，超过单边硬上限
> `QWEN_MAX_SIDE`（默认 1536，**启动时读环境变量**）直接红字报错并拒绝提交；服务端对
> 显式 `size` 的缩放是兜底，网页不会再让你"提交了才发现被缩小"。

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

## 无审查版 DiT（Noct-Q）

除了官方的 Qwen-Image-2.1，本项目还可以挂 [Noctaluna/Noct-Q-Uncensored-Qwen-Image-2.1](https://huggingface.co/Noctaluna/Noct-Q-Uncensored-Qwen-Image-2.1)：
只改了 DiT 权重的**无审查微调版**（写实人体、成人场景不过滤，无需 LoRA），文本编码器 / VAE /
网页 / API / 任务队列全都复用官方那套，**服务端代码零改动**。

### 装

```powershell
# 1) 下载作者的 int8 单文件 (7.26GB, 走 hf-mirror, 断点续传)
powershell -ExecutionPolicy Bypass -File scripts\download_noctq.ps1

# 2) 转成本项目能加载的 GGUF, 落进 model\transformer\ (约 1 分钟)
venv\Scripts\python.exe scripts\noctq_to_gguf.py `
  -i model\noctq\NoctQ_V4_int8_convrot.safetensors `
  -o model\transformer\noctq-v4-Q4.gguf

# 3) 可选: 校验 (加载全部张量跟源逐个比对, 不占 GPU)
venv\Scripts\python.exe scripts\check_noctq_gguf.py
```

第 2 步完成后 `model\noctq\` 那 7.26GB 原始文件就可以删了（`Remove-Item -Recurse model\noctq`），
需要重新量化时再跑一次 `download_noctq.ps1` 即可。

### 用

菜单里会多出 `noctq-v4-Q4.gguf`；或者 `start.bat noctq`（部分匹配）。
`server.py` 不需要改，`QWEN_GGUF=noctq-v4-Q4.gguf` 效果相同。
作者建议 **25 步 + cfg 3 + 负面提示词**；cfg 1（默认）大约快一倍但忽略负面提示词。

实测（RTX 5060 Ti 8GB，1024×1024 / 30 步，与官方 Q4_K_M 同一提示词同一 seed 对照）：

| | 耗时 | 显存峰值 |
|---|---|---|
| `noctq-v4-Q4` | 86~89 秒 | 7769 MB |
| `qwen-image-2.1-Q4_K_M`（对照） | 86~88 秒 | 7769 MB |

两者速度/显存基本一致（区间是多次运行的波动，不是配置差异）；对比图见 `outputs/compare_*.png`。

### 为什么必须转换，不能直接用 int8 文件

作者的发布格式是 **ComfyUI 的 `int8_tensorwise` 单文件**，diffusers 不认识；而且 7.26GB 的权重
在 8GB 卡上根本放不下（本项目现有 Q5_K_M 5.0GB 已经峰值 ~6.4GB 显存）。

> ⚠️ **这个格式有个大坑：ConvRot。** 它不是普通的 int8 —— 权重在量化之前，先按 `in_features`
> 每 256 一组做了一次 **regular Hadamard 旋转**（`W_rot = (W.view(out, in//256, 256) @ H.T)`，
> H 是 regular Hadamard 而非 Sylvester）。如果只按 `q * scale` 反量化而**不把 H 逆回去**，
> 得到的权重**范数完全正常、与原权重却零相关**（相对误差 1.37 ≈ √2）：
> 加载不报错、能出图、耗时显存都正常，**但画出来是纯色噪点**。
> `scripts/check_noctq_gguf.py` 会顺带跟官方基线 GGUF 比对来兜住这种情况
> （正常 0.04~0.13，出错 ~1.37）。

转换脚本会照抄参考 GGUF 的逐张量量化类型，只把 K-quant 换成 gguf-python 能写的 legacy 类型：

| 参考文件里的类型 | 转成 | 位宽 |
|---|---|---|
| Q4_K | Q4_0 | 4.5 → 4.5 bit |
| Q5_K | Q5_0 | 5.5 → 5.5 bit |
| Q6_K | Q8_0 | 6.5 → 8.5 bit |

产物体积因此和参考 Q4_K_M 基本一致（3.91 GB）。
**代价**：同体积下 Q4_0 的质量比 Q4_K_M 略低一档（实测 Q4_0 自身的量化误差约 9%）。
想更精细只能换更贵的 legacy 类型（体积会涨），gguf-python 写不了 K-quant，llama.cpp 也还没有
Qwen-Image-2.1 的转换支持，所以暂时到不了 Q4_K_M 的水平。

### 其它

- 作者另有 V3（旧版）权重，`download_noctq.ps1 -Name NoctQ_V3_base_int8_convrot.safetensors` 同理；
  V4 是作者推荐版，成人场景命中率约为 V3 的三倍。
- 许可：Noct-Q 沿用 **Qwen RESEARCH LICENSE AGREEMENT**，**仅限非商业用途**。
  官方 Qwen-Image-2.1 本身的许可也请一并阅读。
- 该权重带 NSFW 标签（`not-for-all-audiences`），请自行确认使用场景符合当地法规与平台规则。

## API

> **要交给 AI / Agent 调用？**
> - 贴进系统提示词用精简版 → [`docs/API_QUICK.md`](docs/API_QUICK.md)（约 90 行，只留最常用接口）
> - 需要完整参考 → [`docs/API_FOR_AI.md`](docs/API_FOR_AI.md)（含多参考图角色写法、参数全表、队列接口全表）
>
> 两份都写给模型读：30 秒上手、最容易踩的坑、参数速查表。
> `scripts/check_api_doc.py` 会把文档的每条说法与真实服务实测对照，防止文档与实现漂移。
> 下面这几节是给人看的完整参考。

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

#### output_resolution 与速度（重要）

diffusers 管线按 `output_resolution`（默认 1024）把每张参考图等比缩到长边上限，
**它和输出画布是两件事**。本服务默认取 `min(出图长边, 1024)`：既不会像管线默认那样
把 1536 编辑的参考图压到 1024²（那才叫丢细节），也不会为了保细节把耗时推到不可接受。

**耗时为什么随尺寸平方级增长**：参考图的 token 和输出 token 进的是**同一个注意力序列**
（`img_shapes` 里条件图与目标图并列），画布与参考图各自翻倍 = token 数四倍，
视觉编码器 prefill 与每步注意力开销一起涨。所以 8GB 卡上编辑建议：

| 做法 | 效果 |
|---|---|
| `size` 保持 1024 或更小 | 与文档基准一致（1024²@30步 ≈ 98s） |
| 步数降到 20 | 线性省时，画质损失小于降分辨率 |
| 需要更细参考图细节才调高 `output_resolution` | 明确变慢；`usage.warnings` 会提示 |
| 别在编辑时开浏览器/游戏 | 编辑峰值显存已达 8124~8378 MB |

（实测：首次编辑含视觉编码器预热约 130~190 秒，显存峰值 **8124~8378 MB**——贴近 8GB 上限，建议编辑时不要同时开占显存的程序。编辑路径**始终使用 VAE 分块解码**（auto 策略下不分块会 OOM，2026-09-27 实测 500）。）

### 3. 任务队列（Web 工作台用）

GPU 只有一张，队列把生成串行化：**提交即返回**，出图在后台按提交顺序一条条跑，
进度、取消、编辑、重跑、删除都通过下列接口完成。

| 接口 | 作用 |
|---|---|
| `POST /v1/images/generations` + `queue: true` | 入队文生图任务，立即返回任务号（不占用 HTTP 连接等 GPU） |
| `POST /v1/images/generations` + `prompts: [...]` | 一次把多条提示词拆成多条任务（最多 `QWEN_MAX_QUEUE_BATCH`，默认 16） |
| `POST /v1/images/edits/json` | 图生图/编辑入队；`images` 可为 base64、data URL 或本站 `/outputs/xxx.png` 路径。同样支持 `queue: true` 与 `prompts: [...]`（多条提示词共用同一组参考图） |
| `GET /api/queue` | 队列计数、当前任务、参数上限（步数/参考图数/长宽比表/分辨率上限 `max_side` + `default_long_side`/`min_side`/`side_step`） |
| `GET /api/tasks?status=pending&limit=50` | 任务列表（未结束在前按提交顺序，已结束在后按完成时间倒序） |
| `GET /api/tasks/{id}` | 单条任务详情（参数、进度、产物、耗时、显存峰值） |
| `PATCH /api/tasks/{id}` | 编辑**未开始**任务的提示词与参数（运行中/已结束返回 409） |
| `POST /api/tasks/{id}/cancel` | 取消：未开始立即取消；运行中在当前**采样步边界**停止（秒级） |
| `POST /api/tasks/{id}/retry` | 按原参数**新建**一条任务（历史结果保留，便于对比同提示词不同出图） |
| `POST /api/tasks/{id}/release` / `/hold` | 单条任务「开始」/「退回暂存」 |
| `POST /api/queue/release-all` | 开始全部暂存任务 |
| `POST /api/queue/auto-start` `{auto_start}` | 开关「加入后自动生成」；开启时顺带释放已暂存任务 |
| `DELETE /api/tasks/{id}` / `POST /api/queue/delete` `{ids:[...]}` | 删除任务及其图片文件（运行中的拒绝删除，返回 409） |
| `GET /api/tasks/{id}/refs/{name}` | 取回任务落盘的参考图 |

任务状态：`pending`（待开始）→ `running` → `done` / `failed` / `canceled`，
运行中请求取消会短暂进入 `canceling`。

```jsonc
// 入队一次, 之后用任务号轮询
const q = await (await fetch("http://127.0.0.1:8091/v1/images/generations", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ prompt: "雨夜霓虹招牌", steps: 30, queue: true }),
})).json();
const id = q.data[0].id;                       // 202/200 + {queued:1, data:[{id,status,queue_position}]}

// 轮询: GET /api/tasks/{id} -> status/progress/outputs[].url
// 改提示词后重跑: POST /api/tasks/{id}/retry  {prompt: "改成清晨"}
// 不要了:        DELETE /api/tasks/{id}       （运行中先 POST /cancel）
```

行为与持久化细节：

- 队列状态落盘在 `outputs/tasks/tasks.json`（原子写入），**重启后任务列表与产物都在**；
  上次退出时正在运行的任务会自动重新排队（最多 `QWEN_TASK_MAX_ATTEMPTS` 次，默认 2）。
- 参考图会复制到 `outputs/tasks/refs/<任务ID>/`，所以重跑/重启后参数与参考图完整可复用。
- 取消是**真取消**：借助 diffusers 的 `callback_on_step_end` 在每个采样步检查标记并中断，
  不会留下半成品图片（30 步约 3 秒/步，取消通常数秒内生效）。
- 同步接口保持原样：不传 `queue` / `prompts` 时 `POST /v1/images/generations` 仍然阻塞到出图
  （OpenAI 风格兼容），两种用法可共存。
- 已结束任务的记录上限 `QWEN_TASK_MAX_KEEP`（默认 500），超出后从最旧的开始清理并删除其图片。
- `outputs/` 里**没有被任何任务引用**的 PNG 默认**不删**（队列接管之前的老图、
  手工放进去的图都保留）。想清理「生成到一半被强杀」留下的半成品，可设
  `QWEN_TASK_CLEAN_ORPHANS=1`：启动后只清理比队列都新、且无任务引用的 PNG。

### 4. 其它

- `GET /health` — 加载状态 / 显存 / 队列计数 / 当前任务
- `GET /v1/models` — OpenAI 兼容模型列表
- `GET /api` — 接口清单与能力说明
- `GET /docs` — Swagger 交互式文档

> `response_format` 只在**同步**接口上有意义：`"b64_json"` 会在 `data[i]` 里额外带上
> `b64_json` 字段（图片同时照旧落盘，仍可 `url` 下载）；入队任务一律返回 `url`。

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
| `QWEN_MAX_STEPS` | 60 | 单请求 `steps` 取值范围 1~此值，越界直接报错 |
| `QWEN_TRUE_CFG_SCALE` | 1.0 | 默认引导强度。**1.0 = 官方无引导采样**，此时 `negative_prompt` 被忽略；设 >1 才启用 CFG（更慢）。单次请求可用 `true_cfg_scale` 覆盖 |
| `QWEN_MAX_TRUE_CFG_SCALE` | 20 | 单请求 `true_cfg_scale` 取值范围 1.0~此值，越界直接报错 |
| `QWEN_OUTPUT_RESOLUTION` | 跟随出图长边 | 参考图缩放基准（管线 `output_resolution`）。0/不设 = 自动（编辑器路径取 `min(出图长边, QWEN_EDIT_OUTPUT_RESOLUTION)`）。单次请求可用 `output_resolution` 覆盖 |
| `QWEN_EDIT_OUTPUT_RESOLUTION` | 1024 | 带参考图时 `output_resolution` 的默认上限。**抬高会明显变慢**：参考图 token 与视觉编码器 prefill 都随之增长；1024 与文档耗时基准一致 |
| `QWEN_REF_INDEX` | 0 | 多参考图时按第几张定画布长宽比（0 = 第一张；-1 = 最后一张，与管线内部语义一致）。单次请求可用 `ref_index` 覆盖 |
| `QWEN_OFFLOAD` | model | `sequential`=更省显存更慢；`none`=显存全上卡 |
| `QWEN_GGUF` | 自动选 Q4_K_M | 换量化档位，如 `qwen-image-2.1-Q5_K_M.gguf`；也可选 `noctq-v4-Q4.gguf`（无审查版） |
| `QWEN_VAE_TILING` | auto | VAE 分块解码：`auto`=编辑/长边>1024 自动开，文生图≤1024 关（消除 192px 分块接缝网格，实测省 13s）；`1`=恒开（有接缝）；`0`=恒关（编辑/大图会 OOM） |
| `QWEN_MODEL_DIR` | `./model` | 模型目录 |
| `QWEN_OUTPUT_DIR` | `./outputs` | 出图目录（队列的 `tasks/` 也放在这里） |
| `QWEN_TASK_DIR` | `$QWEN_OUTPUT_DIR/tasks` | 任务队列落盘目录 |
| `QWEN_QUEUE_AUTOSTART` | 1 | 首次启动时「加入后自动生成」的默认开关（之后以界面上的勾选为准） |
| `QWEN_MAX_QUEUE_BATCH` | 16 | 一次 `prompts` 批量提交的任务数上限 |
| `QWEN_MAX_REF_IMAGES` | 10 | 参考图数量上限（官方上限 10 张） |
| `QWEN_TASK_MAX_ATTEMPTS` | 2 | 进程中断后自动重新排队的最大尝试次数，超过则标记失败 |
| `QWEN_TASK_MAX_KEEP` | 500 | 已结束任务的保留条数上限，超出清理最旧的（含其图片） |
| `QWEN_TASK_CLEAN_ORPHANS` | 0（关闭） | 设 `1` 时启动后清理「比队列都新且无任务引用」的 PNG（半成品）。默认关闭，避免误删老图 |
| `QWEN_MIN_MEM_GB` | 0（关闭） | 可选：按**提交余量**（Windows GlobalMemoryStatusEx）拦截，余量低于此 GB 数时 503，防 4.4GB 权重搬移触发原生崩溃。正常运行余量约 6~7GB，建议 `5`；`0`=关闭 |

**尺寸换算（实测）**：8GB 卡下 1024×1024@30步 ≈ 98s（auto 分块关）；16:9(1024×576)@30步 ≈ 49s；
1536×1536@8步 ≈ 68s（auto 分块开，6421MB；**该尺寸解码不分块会 OOM**，编辑路径同理，2026-09-27 实测）。
收到 OOM 时服务会自动降到 1024 重试一次。

**提示词技巧**：
- 透明图：`transparent: true` 自动套官方推荐句式（或在 prompt 里写
  "This is an RGBA image with transparency. ..."）
- 长宽比：官方 2K 比例表 1:1 / 4:3 / 3:4 / 3:2 / 2:3 / 16:9 / 9:16 都支持

## 测试

```powershell
# 队列逻辑离线自测（假生成，秒级；入队/取消/重跑/删除/重启恢复）
venv\Scripts\python.exe scripts\test_taskqueue.py

# 队列 HTTP 冒烟测试（起临时服务实例 + 临时输出目录，不加载模型）
venv\Scripts\python.exe scripts\test_server_queue.py

# 队列 + 真模型集成测试（需要 GPU/模型，约 3~6 分钟：真出图、真取消、重启后重跑）
venv\Scripts\python.exe scripts\test_queue_gpu.py

# 模型加载期间入队（需要 GPU，约 1 分钟：任务不失败，就绪后自动开始）
venv\Scripts\python.exe scripts\test_queue_loading.py

# 页面交互语义（需要 node）：点结果图只打开图片、不弹编辑窗；行内 ↓ 下载；弹窗遮罩/Esc 可关
venv\Scripts\python.exe scripts\test_web_clicks.py

# 对**已经开着的服务**做接口完备性 + 与网页一致性测试（接口齐全、参数归一化、
# 队列生命周期、参考图、真出图链路）。加 --no-gpu 跳过真出图，秒级完成
venv\Scripts\python.exe scripts\test_api_live.py --no-gpu
venv\Scripts\python.exe scripts\test_api_live.py

# 核对 docs/API_FOR_AI.md 的每条说法与真实服务一致（改完接口记得跑）
venv\Scripts\python.exe scripts\check_api_doc.py

# OOM 后自动恢复的控制流（不占 GPU，秒级：直接注入「状态损坏」异常验证恢复逻辑）
venv\Scripts\python.exe scripts\test_oom_recovery_unit.py

# 校验 Noct-Q 转出来的 GGUF（不占 GPU，约 1 分钟：走 server.py 的加载路径，再把全部
# 张量跟源 int8 和官方基线 GGUF 各比对一遍。需要已下载并转换过 Noct-Q）
venv\Scripts\python.exe scripts\check_noctq_gguf.py
```

前两个与倒数第二个脚本不碰 `outputs/` 与真实模型，可随时跑；中间两个会占用 GPU；
最后一个只读模型文件、不占显存，跑之前需要先装好 Noct-Q。

> `test_api_live.py` 只做**功能验证**，真出图一律压到最小分辨率（≤256px、≤8 步），
> 免得一轮测试把 GPU 占满。想看出图效果请用网页或 `examples/client.mjs`。
> 它只删自己逐条记录下的任务 ID，结束时还会核对「进场时的任务一条没少」。

## 模型与依赖重装（全国内镜像）

```powershell
# 1) 模型 (~23GB, ModelScope 32MB/s, 断点续传)
powershell -ExecutionPolicy Bypass -File scripts\download_model.ps1

# 2) 依赖 (在 venv 内) —— 依赖清单以 requirements.txt 为准, 不要在这里另抄一份
venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
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
| 出图报 `Expected all tensors to be on the same device` | 显存搬运状态被打断（通常由 OOM 引发）。**服务会自动重建模型并重试，无需重启**（约 40 秒，期间 `/health` 的 `load` 显示 `loading`）。`/health` 的 `offload_recovered_at` 会记下最近一次自愈时间；一直不恢复才需重启 |
| 生成时返回 503「提交余量不足」 | 开启了 `QWEN_MIN_MEM_GB` 提交余量预检（默认关闭）；关闭其他大内存程序，或把阈值调低/设 `0` |
| 服务进程崩溃(access violation / 闪退) | 多为内存·显存被其他进程挤占（2026-09-26 实测：外部进程占 4.5GB 显存 + 可用内存 1.7GB 时，TE 搬回 CPU 触发 0xc0000005）。保持 4GB+ 可用内存；`faulthandler` 会把崩溃时的 C 栈打到控制台 |
| 报 `numpy._ArrayMemoryError: Unable to allocate` | **系统内存耗尽**，不是显存问题。服务本身常驻约 24GB 系统内存，32GB 机器满载时只剩 1.3GB 可用、可用提交 0.1MB 级，连 44MB 都分配不出来（2026-10-02 实测）。关掉占内存的大程序后重启服务；这类失败重建模型也解决不了 |
| 出图报 `Expected all tensors to be on the same device` | 显存搬运状态被打断（通常由 OOM 或内存耗尽引发）。**服务会自动重建模型并重试，无需重启**（约 40 秒，期间 `/health` 的 `load` 显示 `loading`）。`/health` 的 `offload_recovered_at` 会记下最近一次自愈时间；若重建后仍失败，多半是上面的内存耗尽，先查系统内存 |
| 想要更高画质 | 启动时选 `Q5_K_M`/`Q5_K_S`（见上文「启动」），或设 `QWEN_GGUF=qwen-image-2.1-Q5_K_M.gguf`；两档显存峰值都在 ~6.4GB（以 `/health` 的 `vram_peak_mb` 为准）。**注意**：Q5_K_M 搬运需 5GB 提交内存，16GB 内存机器连续生成可能在第 2 张触发原生崩溃（2026-09-27 插桩实测：解码前提交余量仅剩 1.25GB < 需求 4.99GB）；加内存到 32GB 后余量充足即无此问题 |
| 出图有规则网格纹 | 分两类：①**低步数**（<12）的细网格是模型非蒸馏特性，加步数到 20~40；②**高步数残留的淡线**是 VAE 分块解码接缝（每 192px 一条，1024² 正好 5×5），默认 `QWEN_VAE_TILING=auto` 已在文生图 ≤1024 自动关闭。与量化档位无关（Q4/Q5 实测同样表现），同 seed 开关分块差值仅 ~0.5/255 灰阶。完整排查过程见 `docs/grid-artifact-fix.md` |
| 取消后还在跑 | 取消在**采样步边界**生效：当前步跑完才中断（30 步约 3 秒/步），界面会显示「取消中…」。进程被强杀除外——那属于中断，重启后任务会自动重新排队 |
| 任务一直「待开始」 | 看是否关掉了「加入后自动生成」（此时是暂存态，需点「开始队列」），以及是否已有任务在运行（单卡串行，一条接一条） |
| 重启后老任务变成失败 | 该任务在上次退出时正在运行，且已用满 `QWEN_TASK_MAX_ATTEMPTS`（默认 2）次自动重试；用「重新生成」即可再跑一条 |
| 队列出图想手工留存 | 直接下载 `outputs/xxx.png`；注意删除任务会连带删除它的图片文件（仍被其他任务引用的不会被删） |

## 许可

本项目代码以 **MIT** 协议开源，见 [LICENSE](LICENSE)。

模型权重遵循 **Qwen Research License**（非商用需另行申请）。`scripts/download_model.ps1` 只负责拉取上游权重，权重本身不在本仓库内。
