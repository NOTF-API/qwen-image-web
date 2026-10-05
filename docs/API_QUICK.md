# Qwen-Image-2.1 生图服务 · 精简接口说明（供 AI 调用）

服务地址 `http://127.0.0.1:8091`（换地址改 `BASE` 即可）。
完整文档（含多参考图角色写法、参数全表、队列接口全表）见 `docs/API_FOR_AI.md`。

---

## 最重要的一件事

**一张 GPU，一次只能跑一个任务。** 每个任务 1024²/30步约 **98 秒**。

- 要出 1~2 张图 → 用**同步**接口
- 要批量出图 / 需要能中断 → 用**队列**接口（`queue: true`），别并发开多个同步请求

实测参考（RTX 5060 Ti 8GB / Q4 量化）：

| 请求 | 耗时 | 峰值显存 |
|---|---|---|
| 1024×1024 / 30 步 | ~98s | 7752 MB |
| 1024×576（16:9）/ 30 步 | ~49s | 6431 MB |
| 256×256 / 2 步 | ~6s | 6422 MB |

耗时随**画布面积**和**步数**增长（面积近似平方级）；带参考图的编辑要额外付一次
视觉编码器开销，明显更慢。所以：**设 HTTP 超时时给足余量**（单张 1024² 至少 180 秒，
编辑至少 300 秒）。

---

## 出图（最常用）

```bash
curl -X POST http://127.0.0.1:8091/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一只戴毛线帽的柴犬，水彩风格","size":"1024x1024","steps":30}'
```

```js
const r = await fetch("http://127.0.0.1:8091/v1/images/generations", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ prompt: "一只戴毛线帽的柴犬", size: "1024x1024", steps: 30 }),
});
const { data, usage } = await r.json();
console.log(usage.elapsed_sec, "秒");
// data[0].url 可直接 <img src>；传 response_format:"b64_json" 则用 data[0].b64_json
```

### 参数（只列最常用的）

| 参数 | 默认 | 说明 |
|---|---|---|
| `prompt` | **必填** | 提示词 |
| `size` | `1024x1024` | 也可 `width`+`height`；或 `aspect_ratio`+`long_side` |
| `aspect_ratio` | — | `1:1` `4:3` `3:4` `3:2` `2:3` `16:9` `9:16` |
| `long_side` | 1024 | 长边像素 |
| `steps` | 30 | 1~60，**建议 20~40** |
| `n` | 1 | 1~4 张 |
| `seed` | 随机 | 不传则随机；`n>1` 时第 i 张用 `seed+i` |
| `negative_prompt` | — | **需同时给 `true_cfg_scale>1` 才生效** |
| `true_cfg_scale` | 1.0 | 1.0~20；**1.0 = 无引导（负提示词无效）** |
| `transparent` | false | true = 透明 PNG |
| `response_format` | `url` | `url` \| `b64_json` |
| `queue` | false | true = 异步入队（见下） |
| `prompts` | — | 批量：一次拆多条任务（上限 16） |

**尺寸优先级**：`size` > `width`/`height` > `aspect_ratio`+`long_side` > 参考图比例 > 1024²。
对齐 16 的倍数；长边超 1536 自动缩小。

### 三个必知的坑

1. **`steps` 低于 12 出的图不可用**（模糊色块+网格纹）。本模型非蒸馏版，正式出图用 20~40 步。
2. **`negative_prompt` 默认无效**。模型按无引导采样，`true_cfg_scale` 默认 1.0 会忽略它。
   要生效就传 `true_cfg_scale: 4.0` 之类，代价是明显变慢。
3. **尺寸越大越慢，且是平方级**。8GB 卡建议画布 ≤1024。

---

## 批量 / 需要能中断 → 用队列

```js
// 提交后立即返回, 不占用连接
const q = await (await fetch(`${BASE}/v1/images/generations`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ prompt: "雨夜霓虹招牌", steps: 30, queue: true }),
})).json();
const id = q.data[0].id;          // {queued:1, data:[{id,status,queue_position}]}

// 轮询
for (;;) {
  const t = await (await fetch(`${BASE}/api/tasks/${id}`)).json();
  if (["done", "failed", "canceled"].includes(t.status)) break;
  await new Promise(r => setTimeout(r, 3000));
}
if (t.status === "done") console.log(t.outputs[0].url);
```

| 接口 | 作用 |
|---|---|
| `GET /api/tasks/{id}` | 查任务（`status` `progress` `outputs` `usage` `error`） |
| `POST /api/tasks/{id}/cancel` | **取消**（采样步边界生效，秒级，不留半成品） |
| `POST /api/tasks/{id}/retry` | 按原参数新建一条重跑（历史保留） |
| `PATCH /api/tasks/{id}` | 改**未开始**任务的提示词与参数 |
| `GET /api/tasks?status=pending&limit=50` | 任务列表 |
| `DELETE /api/tasks/{id}` | 删任务及其图片 |

状态：`pending` → `running` → `done` / `failed` / `canceled`（取消中短暂 `canceling`）。

> 同步接口**无法取消**。要能中断就必须用队列。

---

## 参考图编辑

```jsonc
POST /v1/images/edits/json
{
  "prompt": "把背景换成日落海滩",
  "images": ["data:image/png;base64,iVBORw0KGgo..."],  // 最多 10 张
  "size": "1024x1024",
  "steps": 30
}
```

- `images` 支持 **base64 / dataURL / 本站 `/outputs/xxx.png` 路径**。
- 不传 `size` 则跟随 `ref_index` 那张（默认第 1 张）的比例；`ref_index: -1` = 最后一张。
- **多参考图按位置引用**：第 N 张在提示词里就是 `<imageN>`。
  例：`"把 <image2> 的配色应用到 <image1> 的产品上"`。
- 编辑**很慢**（比文生图贵得多，因为要走视觉编码器）。`size` 保持 ≤1024，
  `output_resolution` 保持默认（`min(长边,1024)`），别乱调高。
- **编辑时别开浏览器/游戏**，峰值显存已达 8GB 上限。

---

## 出错了怎么办

所有错误都是 `{"detail": "中文原因"}`，**先读 `detail`**。

| 码 | 原因 | 处理 |
|---|---|---|
| 400 | 参数不对 | 按 `detail` 改参数 |
| 404 | 任务/图片不存在 | 检查 ID |
| 409 | 状态冲突 | 运行中的任务不能编辑/删除，先 `cancel` |
| 413 | 显存不足 | 降 `size`/`steps` |
| 500 | 内部错误 | 看 `detail` 里的异常信息 |

**如果生成持续报 `tensors to be on the same device`**：这是显存搬运状态异常。
服务会自动重建模型（约 40 秒）并重试；`GET /health` 的 `load` 会短暂显示 `loading`。
若重建后仍失败，**多半是系统内存不够**（本服务常驻约 24GB 系统内存，32GB 机器满载时
会只剩 1.3GB，此时连 44MB 都分配不出来）—— 这种情况重建救不回来，
需要先关掉占内存的程序，实在不行就重启服务。

---

## 启动时先问服务要配置

```js
const cfg = await (await fetch("http://127.0.0.1:8091/api/queue")).json();
// max_side:1536  max_steps:60  max_ref_images:10  max_queue_batch:16
// default_long_side:1024  default_steps:30  aspect_ratios:[...]
```

**别把这些数字写死在代码里** —— 服务端的启动参数可以改（比如 `QWEN_MAX_SIDE`），
写死就会在某次重启后悄悄失效。启动时读一次即可。

`GET /health` 看运行状态：`load`（`loading`→`ready`，冷启动 110~120 秒）、`cuda`、`vram`、队列计数。
`GET /docs` 是 Swagger。`GET /api` 是接口清单。

---

## 无鉴权

CORS 全开（`*`），适合本机 / 可信内网。**不要暴露到公网。**
