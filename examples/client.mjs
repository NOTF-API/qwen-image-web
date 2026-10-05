// Qwen-Image-2.1 服务调用示例 (Node >= 18，或浏览器控制台直接用)
// 启动服务: start.bat  ->  http://127.0.0.1:8091
const BASE = "http://127.0.0.1:8091";

// 1) 文生图 (OpenAI 风格)
export async function generate(prompt, opts = {}) {
  const res = await fetch(`${BASE}/v1/images/generations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt,
      steps: 30,              // 1~60，默认 30
      response_format: "url", // "url" | "b64_json"
      // seed: 42,             // 不传则随机 (响应里会带回实际 seed)
      // transparent: true,    // 生成透明 PNG (RGBA)
      // n: 1,
      // size/aspect_ratio/long_side 三选一; 都没传时默认 1024x1024
      ...(opts.size || opts.aspect_ratio || opts.long_side
        ? {}
        : { size: "1024x1024" }),
      ...opts,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  const json = await res.json();
  console.log("耗时", json.usage, "图片", json.data[0]);
  return json.data[0]; // { url, seed, width, height, steps } 或 { b64_json, ... }
}

// 2) 图片编辑 / 多参考图 (base64 传入)
export async function edit(prompt, images, opts = {}) {
  const b64 = images.map(async (p) => {
    // 浏览器: 可直接传 dataURL 字符串; Node: 读文件转 base64
    if (p.startsWith("data:")) return p;
    const { readFileSync } = await import("node:fs");   // ESM 下不能用 require
    return "data:image/png;base64," + readFileSync(p).toString("base64");
  });
  const res = await fetch(`${BASE}/v1/images/edits`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, images: await Promise.all(b64), steps: 30, ...opts }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  const json = await res.json();
  return json.data[0];
}

// 3) 健康检查 (模型加载进度)
export async function health() {
  const res = await fetch(`${BASE}/health`);
  return res.json();
}

// ---- 直接运行: node examples/client.mjs ----
if (process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/\\/g, "/"))) {
  const h = await health();
  console.log("health:", h);
  const img = await generate("A neon shop sign that reads \"QWEN IMAGE 2.1\", rainy night, reflections on wet pavement", {
    aspect_ratio: "16:9",
  });
  console.log("生成完成:", img);
}
