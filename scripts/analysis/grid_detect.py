# -*- coding: utf-8 -*-
"""客观网格检测 v2: 分轴 FFT.

输出:
  E_v  纵向线强度 (谱峰在水平频率轴, 对应竖线)
  E_h  横向线强度 (谱峰在垂直频率轴, 对应横线)
  P_v / P_h 对应周期(px)
网格 = E_v 和 E_h 同时显著且周期接近。
范围 2.5px ~ 半图宽, 覆盖 VAE 2px 棋盘格和 ~192px tiling 接缝。
"""
import sys
import numpy as np
from PIL import Image


def detect(path):
    img = Image.open(path).convert("L")
    a = np.asarray(img, dtype=np.float32)
    H, W = a.shape

    # 高通: 减 15px 盒均值 (保留 >=30px 的内容, 去掉大块光影)
    k = 15
    pad = k // 2
    ap = np.pad(a, pad, mode="reflect")
    csum = np.cumsum(np.cumsum(ap, axis=0), axis=1)
    csum = np.pad(csum, ((1, 0), (1, 0)))
    box = (csum[k:, k:] - csum[:-k, k:] - csum[k:, :-k] + csum[:-k, :-k]) / (k * k)
    hp = a - box[:H, :W]

    wy = np.hanning(H)[:, None]
    wx = np.hanning(W)[None, :]
    hp = hp * wy * wx

    F = np.abs(np.fft.fftshift(np.fft.fft2(hp)))
    fy = np.fft.fftshift(np.fft.fftfreq(H))[:, None] * np.ones((1, W))
    fx = np.ones((H, 1)) * np.fft.fftshift(np.fft.fftfreq(W))[None, :]
    r = np.hypot(fy, fx)

    cy, cx = H // 2, W // 2
    band = 2  # 轴附近 ±2 频点

    # 频率范围: 周期 2.5px ~ min(H,W)/2
    valid = (r >= 2.0 / max(H, W)) & (r <= 1.0 / 2.5)
    ref = float(np.median(F[valid])) + 1e-9

    def axis_peak(axis):
        # axis='v': 竖线 => 谱峰在 fx 轴 (fy≈0); axis='h': 横线 => fy 轴
        if axis == "v":
            mask = (np.abs(np.arange(H)[:, None] - cy) <= band) & valid
        else:
            mask = (np.abs(np.arange(W)[None, :] - cx) <= band) & valid
        Fm = np.where(mask, F, 0)
        idx = np.unravel_index(np.argmax(Fm), Fm.shape)
        return float(Fm[idx] / ref), float(1.0 / r[idx]) if r[idx] > 0 else 0, idx

    E_v, P_v, iv = axis_peak("v")
    E_h, P_h, ih = axis_peak("h")

    # tiling 接缝频带: 周期 150~240px (stride192), 双轴
    band_seam = (r >= 1.0 / 240) & (r <= 1.0 / 150)

    def seam_peak(axis):
        if axis == "v":
            m = (np.abs(np.arange(H)[:, None] - cy) <= band) & band_seam
        else:
            m = (np.abs(np.arange(W)[None, :] - cx) <= band) & band_seam
        Fm = np.where(m, F, 0)
        idx = np.unravel_index(np.argmax(Fm), Fm.shape)
        return float(Fm[idx] / ref), float(1.0 / r[idx]) if r[idx] > 0 else 0

    S_v, SP_v = seam_peak("v")
    S_h, SP_h = seam_peak("h")

    return {
        "file": path.replace("\\", "/").split("/")[-1][:30],
        "size": f"{W}x{H}",
        "E_v": round(E_v, 1), "P_v": round(P_v, 1),
        "E_h": round(E_h, 1), "P_h": round(P_h, 1),
        "grid": round(min(E_v, E_h), 1),
        "seam_v": round(S_v, 1), "seam_h": round(S_h, 1),
        "seam": round(min(S_v, S_h), 1),
    }


if __name__ == "__main__":
    rows = [detect(p) for p in sys.argv[1:]]
    hdr = ["file", "size", "E_v", "P_v", "E_h", "P_h", "grid", "seam_v", "seam_h", "seam"]
    print(" | ".join(hdr))
    for r in rows:
        print(" | ".join(str(r[h]) for h in hdr))
