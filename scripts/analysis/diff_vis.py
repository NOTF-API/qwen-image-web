# -*- coding: utf-8 -*-
"""同 seed tiling ON/OFF 差值隔离: 差值 = tiling 解码路径的全部影响.

用法: python diff_vis.py on.png off.png 输出前缀
"""
import sys
import numpy as np
from PIL import Image

a = np.asarray(Image.open(sys.argv[1]).convert("L"), dtype=np.float64)
b = np.asarray(Image.open(sys.argv[2]).convert("L"), dtype=np.float64)
d = a - b
H, W = d.shape
print(f"diff: rms={np.sqrt((d**2).mean()):.3f}  mean|d|={np.abs(d).mean():.3f}  max|d|={np.abs(d).max():.1f}")

# 列/行剖面: 若 tiling 在 192k 处产生接缝, 差值的列剖面应在此处有峰
col = np.abs(d).mean(axis=0)
row = np.abs(d).mean(axis=1)
top_c = np.argsort(col)[-14:]
top_r = np.argsort(row)[-14:]
print("top diff cols:", ", ".join(f"{i}:{col[i]:.2f}" for i in sorted(top_c)))
print("top diff rows:", ", ".join(f"{i}:{row[i]:.2f}" for i in sorted(top_r)))
band = [192, 384, 576, 768, 960]
inb = np.concatenate([[p + dd for dd in range(-4, 5)] for p in band])
inb = inb[(inb > 4) & (inb < W - 5)]
others = np.setdiff1d(np.arange(4, W - 5), inb)
print(f"tiling接缝带 |d|均值: {col[inb].mean():.3f}   其余列: {col[others].mean():.3f}   比值: {col[inb].mean()/col[others].mean():.3f}")

# 放大保存差值图
m = np.abs(d).max() + 1e-9
img = np.clip(d / m * 127 + 128, 0, 255).astype(np.uint8)
Image.fromarray(img).save(f"{sys.argv[3]}_diff.png")
print(f"saved {sys.argv[3]}_diff.png (x{m:.1f} 放大)")
