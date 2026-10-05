# -*- coding: utf-8 -*-
"""频带分离可视化: 把指定频带的成分从图中提取出来放大保存.

用法: python band_vis.py 输入.png 输出前缀
生成: <prefix>_fine.png (2.5~10px) / <prefix>_mid.png (10~30px) / <prefix>_seam.png (150~240px)
每张都是该频带成分的对比度拉伸灰度图(亮=正峰 暗=负谷)。
"""
import sys
import numpy as np
from PIL import Image

BANDS = {
    "fine": (1.0 / 10.0, 1.0 / 2.5),
    "mid": (1.0 / 30.0, 1.0 / 10.0),
    "seam": (1.0 / 240.0, 1.0 / 150.0),
}


def extract(path, prefix):
    a = np.asarray(Image.open(path).convert("L"), dtype=np.float64)
    H, W = a.shape
    F = np.fft.fft2(a)
    fy = np.fft.fftfreq(H)[:, None] * np.ones((1, W))
    fx = np.ones((H, 1)) * np.fft.fftfreq(W)[None, :]
    r = np.hypot(fy, fx)

    for name, (lo, hi) in BANDS.items():
        mask = (r >= lo) & (r <= hi)
        comp = np.real(np.fft.ifft2(F * mask))
        m = np.abs(comp).max() + 1e-9
        img = np.clip(comp / m * 127 + 128, 0, 255).astype(np.uint8)
        out = f"{prefix}_{name}.png"
        Image.fromarray(img).save(out)
        rms = float(np.sqrt((comp ** 2).mean()))
        print(f"{out}  band={1/hi:.1f}~{1/lo:.0f}px  rms={rms:.3f}  max={m:.3f}")


if __name__ == "__main__":
    extract(sys.argv[1], sys.argv[2])
