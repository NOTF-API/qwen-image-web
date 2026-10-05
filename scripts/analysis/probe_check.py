# -*- coding: utf-8 -*-
"""探针偏差验证: 梯度剖面实际峰位 + 红圆 bbox + 与采样带的关系."""
import sys
import numpy as np
from PIL import Image


def analyze(path):
    im = Image.open(path)
    a = np.asarray(im.convert("L"), dtype=np.float32)
    rgb = np.asarray(im.convert("RGB"), dtype=np.int16)
    H, W = a.shape

    dx = np.abs(np.diff(a, axis=1))
    dy = np.abs(np.diff(a, axis=0))
    col = dx.mean(axis=0)
    row = dy.mean(axis=1)

    # 红圆 bbox
    red = (rgb[:, :, 0] > 150) & (rgb[:, :, 1] < 100) & (rgb[:, :, 2] < 100)
    if red.any():
        ys, xs = np.where(red)
        bbox = f"red x[{xs.min()}-{xs.max()}] y[{ys.min()}-{ys.max()}]"
    else:
        bbox = "no-red"

    top_c = np.argsort(col)[-12:][::-1]
    top_r = np.argsort(row)[-12:][::-1]
    print(path.replace("\\", "/").split("/")[-1][:32], "|", bbox)
    print("  top cols:", ", ".join(f"{i}:{col[i]:.2f}" for i in sorted(top_c)))
    print("  top rows:", ", ".join(f"{i}:{row[i]:.2f}" for i in sorted(top_r)))
    # 我的采样带
    band = [192, 384, 576, 768, 960]
    inb = np.concatenate([[p + d for d in range(-3, 4)] for p in band])
    inb = inb[(inb > 3) & (inb < W - 4)]
    print(f"  band mean col: {col[inb].mean():.3f}  all mean: {col[4:W-5].mean():.3f}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        analyze(p)
