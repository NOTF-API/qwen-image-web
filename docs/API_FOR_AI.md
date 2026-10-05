# Qwen-Image-2.1 生图服务 · API 文档（供 AI 调用）

> 这份文档写给**正在调用本服务的 AI / Agent**。人类开发者也可以看，但重点在「怎么调、
> 哪些坑会踩」。所有示例都按本机 `http://127.0.0.1:8091` 写的，换地址只需改 `BASE`。

---

## 0. 30 秒上手

```bash
curl -X POST http://127.0.0.1:8091/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一只戴毛线帽的柴犬，水彩风格","size":"1024x1024","steps":30}'
```

```js
// 浏览器 / Node 通用（CORS 已全开，任意端口可调）
const r = await fetch("http://127.0.0.1:8091/v1/images/generations", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ prompt: "一只戴毛线帽的柴犬", size: "1024x1024", steps: 30 }),
});
const { data, usage } = await r.json();
// data[0].url 是可直接 <img src> 的地址；data[0].b64_json 仅在 response_format="b64_json" 时有
```

**一条请求约 98 秒**（1024²、30 步、RTX 5060 Ti 8GB）。同步接口会一直阻塞到出图为止。

---

## 1. 先看这三条，能避免绝大多数事故

### ① 一步 30 步起；低于 12 步会出「格子图」

本模型**非少步蒸馏版**（官方建议 40 步）。步数太少去噪不收敛，出图是模糊色块 + 规则网格纹。
**正式出图用 20~40 步**（默认 30）。<12 步只用于测速，画质不可用。

### ② `negative_prompt` 默认无效，必须同时给 `true_cfg_scale > 1`

模型按「无引导」采样训练，`true_cfg_scale` 默认 `1.0`，此时负提示词**被忽略**。
要生效就传 `true_cfg_scale > 1`，代价是每步多一次前向、明显更慢。
不生效时响应的 `usage.warnings` 会明确告知（不会静默失败）。

```jsonc
// 想要负提示词生效
{ "prompt": "清晰的柴犬照片", "negative_prompt": "blurry, low quality", "true_cfg_scale": 4.0 }
```

> 历史遗留：更早版本暴露过一个 `guidance_scale` 参数，**它不是本管线的参数，会被静默丢弃**。
> 已经删除，不要再用。只有 `true_cfg_scale` 是真的。

### ③ GPU 只有一张，所有任务串行排队

同时来 N 个请求 = 排 N 次队，**总耗时是 N 倍**。批量出图请用异步队列接口
（`queue: true`），而不是并发开 N 个同步请求。

---

## 2. 两种调用方式

| | 同步（默认） | 异步队列 |
|---|---|---|
| 触发 | 不传 `queue` | 传 `queue: true` |
| 返回时机 | 出图后才返回 | **立即**返回任务 ID（毫秒级） |
| 取结果 | 响应里就有 | 轮询 `GET /api/tasks/{id}` |
| 适合 | 一次一两张、脚本 | 批量、网页、要能取消 |

```js
// 异步: 提交后轮询
const q = await (await fetch(`${BASE}/v1/images/generations`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ prompt: "雨夜霓虹招牌", steps: 30, queue: true }),
})).json();
const id = q.data[0].id;   // 202/200 + {queued:1, data:[{id,status,queue_position}]}

for (;;) {
  const t = await (await fetch(`${BASE}/api/tasks/${id}`)).json();
  if (["done", "failed", "canceled"].includes(t.status)) break;
  await new Promise(r => setTimeout(r, 3000));
}
if (t.status === "done") console.log(t.outputs[0].url);
```

---

## 3. 文生图 `POST /v1/images/generations`

### 请求参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prompt` | string | **必填** | 提示词 |
| `prompts` | string[] | — | 批量：一次拆成多条任务（上限 16）。与 `prompt` 二选一 |
| `size` | string | — | `"1024x1024"`，也接受 `"1024*1024"`。优先级最高 |
| `width` + `height` | int | — | OpenAI 风格写法，等价于 `size`。`size` 同时存在时以 `size` 为准 |
| `aspect_ratio` | string | — | `1:1` `4:3` `3:4` `3:2` `2:3` `16:9` `9:16`（官方 2K 比例表） |
| `long_side` | int | 1024 | 长边像素。与 `aspect_ratio` 配合使用 |
| `steps` | int | 30 | 1~60。**建议 20~40** |
| `n` | int | 1 | 1~4 张 |
| `seed` | int | 随机 | 不传则随机，响应回带实际值。`n>1` 时第 i 张用 `seed+i` |
| `true_cfg_scale` | float | 1.0 | 1.0~20。**>1 才启用 CFG**（见 §1②） |
| `negative_prompt` | string | — | 需 `true_cfg_scale>1` 才生效 |
| `transparent` | bool | false | true = 生成透明 PNG（RGBA） |
| `output_resolution` | int | 见下 | 参考图缩放基准，与画布是两件事 |
| `response_format` | string | `url` | `url` \| `b64_json` |
| `queue` | bool | false | true = 异步入队 |
| `ref_index` | int | 0 | 仅编辑路径：按第几张参考图定画布比例（`-1` = 最后一张） |
| `title` | string | — | 任务名（仅队列） |
| `released` | bool | — | 仅队列：false = 暂存不执行 |

**尺寸优先级**：`size` > `width`/`height` > `aspect_ratio`+`long_side` > 参考图比例 > 1024²。
所有尺寸会对齐到 16 的倍数，长边超过 `1536` 会被自动缩小（`usage.note` 会说明）。

### 响应

```jsonc
{
  "created": 1790000000,
  "model": "Qwen-Image-2.1",
  "data": [
    { "url": "http://127.0.0.1:8091/outputs/20260926-xxxx-ab12cd34.png",
      "seed": 42, "width": 1024, "height": 1024, "steps": 30 }
  ],
  "usage": {
    "elapsed_sec": 41.2,        // 纯生成耗时
    "queue_sec": 0.1,           // 排队等 GPU 的时间
    "vram_peak_mb": 6890,       // 显存峰值
    "vae_tiling": false,        // 本次是否用了 VAE 分块解码
    "true_cfg_scale": 1.0,      // 实际用的引导强度
    "output_resolution": 1024,  // 参考图缩放基准
    "mem_avail_gb": 2.4,        // 生成前系统可用内存（观测用）
    "warnings": ["..."]         // 仅在有需要注意的事时出现
  }
}
```

传 `response_format: "b64_json"` 时，`data[i]` 额外含 `b64_json`（纯 base64，不带 dataURL 前缀）。

---

## 4. 图片编辑 / 多参考图 `POST /v1/images/edits/json`

```jsonc
{
  "prompt": "把背景换成日落海滩",
  "images": ["data:image/png;base64,iVBORw0KGgo..."],  // 最多 10 张
  "size": "1024x1024",        // 不传则跟随 ref_index 那张参考图的比例
  "steps": 30,
  "output_resolution": 1024,  // 参考图缩放基准，默认 min(长边, 1024)
  "ref_index": 0
}
```

- `images` 接受三种形式：**base64**、**data URL**、**本站 `/outputs/xxx.png` 路径或 URL**。
  小尺寸图片请用 base64 或 dataURL，不要用文件名。
- 同步用法：直接调，阻塞到出图。
- 异步用法：加 `queue: true`（或用 `prompts: [...]` 批量），走队列。

> 另有一个 `POST /v1/images/edits`，只接受 base64、**不支持** `prompts` 批量。
> 除非你在严格对齐 OpenAI 规范，否则用 `/edits/json` 就行，它功能更全。

### 多参考图是「按位置」引用的

上传的第 N 张，在提示词里就是 `<imageN>`。这不是「混在一起」，可以显式指定角色：

```jsonc
{
  "prompt": "把 <image2> 的配色和画面风格应用到 <image1> 的产品上，保留 <image1> 的材质细节",
  "images": ["<产品图 base64>", "<风格参考图 base64>"],
  "steps": 30
}
```

**画布比例取自 `ref_index` 指定的那张（默认第 1 张），而管线内部按最后一张缩放条件图。**
两者比例差得多时条件图会变形（响应 `usage.warnings` 会提示）。
**建议所有参考图保持相近比例**，或显式传 `ref_index: -1` 对齐管线内部语义。

### output_resolution：最容易踩的慢点

它决定每张参考图被缩到多大，**与输出画布是两件事**。默认值 = `min(出图长边, 1024)`。

耗时随**画布面积和 `output_resolution` 平方级增长**——因为参考图 token 和输出 token
进的是同一个注意力序列。8GB 卡的实测：

| 做法 | 效果 |
|---|---|
| `size` 保持 1024 或更小 | 与基准一致（1024²@30步 ≈ 98s） |
| 步数降到 20 | 线性省时，画质损失小于降分辨率 |
| 调高 `output_resolution` | 明确变慢；`usage.warnings` 会提示 |
| 编辑时开浏览器/游戏 | 编辑峰值显存已达 8124~8378 MB，**很危险** |

---

## 5. 任务队列（批量、取消、重跑）

| 接口 | 作用 |
|---|---|
| `POST /v1/images/generations` + `queue:true` | 入队文生图，立即返回 |
| `POST /v1/images/edits/json` + `queue:true` | 入队编辑任务 |
| `prompts: [...]` | 一次拆成多条任务（上限 16） |
| `GET /api/queue` | **队列概览 + 所有参数上限**（见 §7） |
| `GET /api/tasks?status=pending&limit=50` | 任务列表（未结束在前，已结束按完成时间倒序） |
| `GET /api/tasks/{id}` | 单条任务详情 |
| `PATCH /api/tasks/{id}` | 改**未开始**任务的提示词与参数（运行中/已结束 → 409） |
| `POST /api/tasks/{id}/cancel` | 取消（未开始立即；运行中在**采样步边界**停，秒级） |
| `POST /api/tasks/{id}/retry` | 按原参数**新建**一条任务（历史结果保留，便于对比） |
| `POST /api/tasks/{id}/release` / `/hold` | 单条「开始」/「退回暂存」 |
| `POST /api/queue/release-all` | 开始全部暂存任务 |
| `POST /api/queue/auto-start` `{auto_start}` | 开关「加入后自动生成」 |
| `DELETE /api/tasks/{id}` / `POST /api/queue/delete` `{ids:[...]}` | 删除任务及其图片（运行中 → 409） |
| `GET /api/tasks/{id}/refs/{name}` | 取回任务的参考图 |

**状态流转**：`pending`（待开始）→ `running` → `done` / `failed` / `canceled`，
运行中请求取消会短暂经过 `canceling`。

任务视图里的关键字段：

```jsonc
{
  "id": "a1b2c3d4e5f6", "seq": 34, "status": "done", "kind": "edit",
  "prompt": "...", "refs": ["ref1.png"],           // 参考图文件名
  "staged": false,                                   // true = 暂存未开始
  "queue_position": 2,                               // 待开始时的排位
  "progress": { "step": 17, "total": 30 },           // 采样进度
  "outputs": [ { "url": "...", "seed": 42, "width": 1024, "height": 1024 } ],
  "usage": { "elapsed_sec": 98.2, "vram_peak_mb": 7752 },
  "warnings": [], "error": null
}
```

**取消是真取消**：在每个采样步边界检查标记并中断，**不会留下半成品图片**。

**持久化**：队列落盘在 `outputs/tasks/tasks.json`，**重启服务后任务与产物仍在**；
上次退出时正在运行的任务会自动重新排队（上限 `QWEN_TASK_MAX_ATTEMPTS`，默认 2）。

---

## 6. 错误处理

| 状态码 | 含义 | 怎么处理 |
|---|---|---|
| 400 | 参数不合法 | `detail` 里有中文原因，照着改参数 |
| 404 | 任务/图片不存在 | 检查 ID |
| 409 | 状态冲突 | 运行中的任务不能编辑/删除/重跑；先 `cancel` |
| 413 | 显存不足（OOM） | 降 `size`/`steps`；或设 `QWEN_MAX_SIDE=1024`、`QWEN_OFFLOAD=sequential` 后重启 |
| 500 | 内部错误 | `detail` 有异常类型和消息 |
| 503 | 系统提交内存不足 | 关闭其他大内存程序，或把 `QWEN_MIN_MEM_GB` 调低 |

**所有错误响应的结构是 `{"detail": "中文原因"}`。** 出错时先读 `detail`。

服务收到 OOM 会自动降到 1024 重试一次。任务失败时原因记在任务的 `error` 字段。

### OOM 之后服务会自愈（不用重启）

8GB 卡上 OOM 有时会顺带打断 diffusers 的 `enable_model_cpu_offload()`，让管线进入
**「中毒」状态**：此后每次生成都报

```
Expected all tensors to be on the same device, but got mat2 is on cpu, ...
```

修复前这会导致**整个服务永久卡死、只能重启进程**（2026-10-01 实测 3/3 必现，连参数正常的
请求也失败）。现在服务会识别这种状态、**自动重建模型并重试本次请求**：

- 重建期间 `GET /health` 的 `load` 短暂显示 `loading`，`waiting` 反映正在等待的请求
- 重建成功：本次请求继续返回结果，`usage.warnings` 里有说明
- 队列任务同样会自动重试，**不需要重新提交**
- 重建后 `GET /health` 的 `offload_recovered_at` 会是最近一次自愈的时间戳（`null` = 从未发生过）

如果重建本身也失败，`load` 会变成 `error`、`error` 字段有原因，这时才需要重启服务。

---

## 7. 运行时信息：先问服务要，别写死

`GET /api/queue` 会下发当前所有上限和默认值——**这是最权威的一份，脚本应当启动时读一次**：

```jsonc
{
  "counts": { "pending": 0, "running": 0, "done": 17, "staged": 0, "auto_start": true, ... },
  "current": { ... },              // 正在跑的任务（没有则为 null）
  "auto_start": true,
  "worker": { "running": true, "blocked": false },
  "model_load": "ready",           // idle / loading / ready / error
  "max_side": 1536,                // 长边硬上限
  "default_long_side": 1024, "min_side": 256, "side_step": 16,
  "default_steps": 30, "max_steps": 60,
  "default_true_cfg_scale": 1.0, "max_true_cfg_scale": 20.0,
  "max_ref_images": 10, "max_queue_batch": 16,
  "edit_output_resolution": 1024,
  "aspect_ratios": ["1:1","4:3","3:4","3:2","2:3","16:9","9:16"]
}
```

`GET /health` 给运行状态：`load`（`loading` → `ready`，冷启动 110~120 秒）、`cuda`、
`gpu`、`vram`、`queue` 计数、`current_task`、`offload_recovered_at`
（最近一次 OOM 自动重建模型的时间戳，`null` = 从未发生，见 §6）。

`GET /v1/models` 是 OpenAI 风格模型列表（`id: "Qwen-Image-2.1"`）。
`GET /docs` 是 Swagger 交互文档。

---

## 8. 一条稳妥的调用策略（推荐给 Agent）

```js
const BASE = "http://127.0.0.1:8091";

// 0) 超时给足余量: 单张 1024² 约 98s, 编辑更慢。别用默认 30s 超时
const TIMEOUT_SYNC = 300_000;     // 同步出图
const TIMEOUT_QUEUE = 30_000;     // 队列提交/查询(很快)

// 1) 启动时读一次上限, 不要写死
const cfg = await (await fetch(`${BASE}/api/queue`)).json();

// 2) 提交前自查, 避免白等 98 秒
function validate(p) {
  if (!p.prompt?.trim()) throw new Error("缺少 prompt");
  if (p.steps != null && (p.steps < 1 || p.steps > cfg.max_steps))
    throw new Error(`steps 需在 1~${cfg.max_steps}`);
  if (p.steps != null && p.steps < 20)
    console.warn("步数 <20 可能是格子图, 正式出图建议 20~40");
  if (p.n != null && (p.n < 1 || p.n > 4)) throw new Error("n 需在 1~4");
  if (p.images && p.images.length > cfg.max_ref_images)
    throw new Error(`参考图最多 ${cfg.max_ref_images} 张`);
}

// 3) 批量出图一律走队列, 不要并发同步请求
const submit = async (prompts, opts = {}) => {
  validate({ ...opts, prompt: prompts[0] });
  return (await fetch(`${BASE}/v1/images/generations`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompts, queue: true, steps: 30, ...opts }),
  })).json();
};

const waitDone = async (id, timeoutMs = 10 * 60_000) => {
  const t0 = Date.now();
  for (;;) {
    const t = await (await fetch(`${BASE}/api/tasks/${id}`)).json();
    if (["done", "failed", "canceled"].includes(t.status)) return t;
    if (Date.now() - t0 > timeoutMs) {
      await fetch(`${BASE}/api/tasks/${id}/cancel`, { method: "POST" });
      throw new Error("超时, 已请求取消");
    }
    await new Promise(r => setTimeout(r, 3000));
  }
};
```

---

## 9. 已知限制

1. **同步接口无法主动取消**。只有队列任务能取消（`POST /api/tasks/{id}/cancel`）。
   要能中断就用 `queue: true`。
2. **必须给足 HTTP 超时**。单张 1024²/30步约 98 秒，编辑可能 130~190 秒，
   OOM 后的自动重建还要再加约 40 秒。默认 30 秒超时一定会失败。
3. **无鉴权**。CORS 全开（`*`），仅适合本机 / 可信内网，不要暴露到公网。
4. **8GB 显存是硬约束**。编辑路径峰值 8124~8378 MB，**生成期间别开浏览器或游戏**。
5. **步数 <12 出的图不可用**（§1①）。
6. **参考图比例不一致可能变形**（§4）。
7. 服务重启会中断正在跑的任务，但它们会自动重新排队（§5）。
8. 8GB 显存的代价：文本编码器 BF16 权重约 17.6GB 常驻**系统内存**（NF4 只省显存，不省内存），
   所以服务开着时内存会被长期占掉 20GB+。**32GB 机器实测只剩 1.3GB 可用、可用提交 0.1GB**，
   此时连 numpy 分配 44MB 都会失败，生成会以 `numpy._ArrayMemoryError: Unable to allocate`
   或 `Expected all tensors to be on the same device` 告终（2026-10-02 实测）。
   保持 4GB+ 可用内存，生成时别开占内存的大程序；出现上述报错且**重建模型也救不回来**时，
   先查系统内存是不是被别的程序吃光了（这类失败重建管线也解决不了）。

---

## 10. 参数速查

```
必填:  prompt
尺寸:  size "WxH"  |  width+height  |  aspect_ratio + long_side   （优先级从左到右）
质量:  steps 20~40（默认 30，低于 12 不可用）
数量:  n 1~4
引导:  true_cfg_scale 默认 1.0（=无引导）；>1 才让 negative_prompt 生效
参考图: images（base64 / dataURL / /outputs/ 路径），最多 10 张
        ref_index 默认 0（第一张），-1 = 最后一张
        output_resolution 默认 min(长边,1024)，调高明显变慢
异步:  queue: true + prompts: [...]
取图:  data[0].url（默认）| data[0].b64_json（response_format="b64_json"）
查限:  GET /api/queue  —— 启动时读一次
排错:  错误响应的 detail 字段；任务的 error 字段
```
