# -*- coding: utf-8 -*-
"""VAE tiling 接缝直接探针.

预测接缝位置(1024图, stride192): x/y = 192,384,576,768,960 (blend ±32px)
指标: 接缝列的平均 |dI/dx| / 全图列平均 |dI/dx|  (ratio>1 = 接缝可见)
对行同样计算。白底图上任何亮度台阶都会显现。
"""
import sys
import numpy as np
from PIL import Image

SEAMS = [192, 384, 576, 768, 960]


def probe(path):
    a = np.asarray(Image.open(path).convert("L"), dtype=np.float32)
    H, W = a.shape
    dx = np.abs(np.diff(a, axis=1))       # H x (W-1): 列梯度
    dy = np.abs(np.diff(a, axis=0))       # (H-1) x W: 行梯度
    col_g = dx.mean(axis=0)               # 每列平均梯度
    row_g = dy.mean(axis=1)

    # 接缝带: 接缝位置±3列 (排除边缘)
    def band(prof, pos_list, n, span=3):
        idx = []
        for p in pos_list:
            for d in range(-span, span + 1):
                k = p + d - 1  # diff 后的索引偏移
                if span <= k < n - span:
                    idx.append(k)
        return prof[idx].mean()

    span = 3
    base_c = col_g[span: W - 4].mean()
    base_r = row_g[span: H - 4].mean()
    seam_c = band(col_g, SEAMS, W - 1)
    seam_r = band(row_g, SEAMS, H - 1)

    return {
        "file": path.replace("\\", "/").split("/")[-1][:30],
        "ratio_x": round(float(seam_c / (base_c + 1e-9)), 3),
        "ratio_y": round(float(seam_r / (base_r + 1e-9)), 3),
        "seam_abs_x": round(float(seam_c), 3),
        "base_abs_x": round(float(base_c), 3),
    }


if __name__ == "__main__":
    rows = [probe(p) for p in sys.argv[1:]]
    hdr = ["file", "ratio_x", "ratio_y", "seam_abs_x", "base_abs_x"]
    print(" | ".join(hdr))
    for r in rows:
        print(" | ".join(str(r[h]) for h in hdr))
