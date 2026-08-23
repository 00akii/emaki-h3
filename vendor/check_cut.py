# -*- coding: utf-8 -*-
"""
check_cut.py - SAM3 で切り抜いた参照画像が H3 に渡せる状態か検査する。

  python check_cut.py "<ComfyUI>/input/h3ref_S01_front_cut.png"
  python check_cut.py <画像> --expect-people 5 --out-mp 1.03

見るのは4点:
  1. 背景が完全な単色か（漏れの原因になる背景残りを検出）
  2. 前景に何個のかたまりがあるか（残すべき人物の数と合っているか）
  3. 主被写体が画面のどれくらいを占めるか（小さすぎると同一性が落ちる）
  4. 出力解像度に対して参照が過剰に大きくないか

終了コード: 0 = 問題なし / 1 = 要確認
"""
from __future__ import annotations
import argparse, os, sys

try:
    import numpy as np
    import cv2
    from PIL import Image
except ImportError as e:
    print("依存が足りない: %s  (pip install pillow numpy opencv-python)" % e)
    sys.exit(2)

GREY = 128  # Image Blank ノードの既定 (128,128,128)


def analyze(path, expect_people=None, out_mp=None, bg_tol=18):
    r = {"errors": [], "warns": [], "info": []}
    im = Image.open(path)
    a = np.array(im.convert("RGB")).astype(np.int16)
    H, W, _ = a.shape
    mp = W * H / 1e6
    r["info"].append("画像 %dx%d = %.2fMP  mode=%s" % (W, H, mp, im.mode))

    # 1. 背景の均一性
    # 四隅は被写体の髪や脚が届くと簡単に汚れるので、最頻色そのもので判定する。
    # （2026-08-23: 四隅judge は正しい切り抜きを ERROR にする誤検出を出した）
    flat = a.reshape(-1, 3).astype(np.int32)
    key = (flat[:, 0] << 16) | (flat[:, 1] << 8) | flat[:, 2]
    vals, counts = np.unique(key, return_counts=True)
    top = int(vals[counts.argmax()])
    bg = np.array([(top >> 16) & 255, (top >> 8) & 255, top & 255], dtype=np.int16)
    exact_mask = (key == top).reshape(H, W)
    exact_ratio = exact_mask.mean()
    ring = np.zeros((H, W), bool)
    ring[:8, :] = ring[-8:, :] = True
    ring[:, :8] = ring[:, -8:] = True
    ring_ratio = exact_mask[ring].mean()
    r["info"].append("最頻色 %s が全体の %.1f%% / 外周8px帯の %.1f%% を占める"
                     % (bg.tolist(), exact_ratio * 100, ring_ratio * 100))
    std = 0.0 if (exact_ratio >= 0.03 and ring_ratio >= 0.35) else 100.0  # 以降の判定用
    if exact_ratio < 0.03:
        r["errors"].append("最頻色が画面の %.1f%% しかない。単色背景に置換されていない疑い"
                           % (exact_ratio * 100))
    elif ring_ratio < 0.35:
        r["errors"].append("外周8px帯の %.1f%% しか背景色でない。元画像の背景が残っている可能性"
                           % (ring_ratio * 100))
    if abs(bg - GREY).max() > 6:
        r["warns"].append("背景色が %s で、既定のグレー(128,128,128)と違う" % bg.tolist())
    corner_std = np.concatenate([a[:40, :40].reshape(-1, 3), a[:40, -40:].reshape(-1, 3),
                                 a[-40:, :40].reshape(-1, 3), a[-40:, -40:].reshape(-1, 3)]).std(axis=0).max()
    if corner_std > 2.0:
        r["info"].append("（参考）四隅のばらつき %.2f — 被写体が隅まで届いているだけのことが多い" % corner_std)

    fgmask = (np.abs(a - bg).max(axis=2) > bg_tol).astype(np.uint8)
    fg_ratio = fgmask.mean()
    r["info"].append("前景の占有率 %.1f%%" % (fg_ratio * 100))

    # 1b. マスクのディザ/穴あき（negative_coords を使うと出る破綻）
    ex_fg = (~exact_mask).astype(np.uint8)
    k3 = np.ones((3, 3), np.uint8)
    loss = 1 - cv2.morphologyEx(ex_fg, cv2.MORPH_OPEN, k3).sum() / max(1, ex_fg.sum())
    r["info"].append("マスクのざらつき（3x3オープンでの欠損率） %.2f%%" % (loss * 100))
    if loss > 0.015:
        r["errors"].append("マスクがディザ状に破綻している（欠損率 %.2f%%）。負の点指定や refine 過多が原因" % (loss * 100))
    elif loss > 0.005:
        r["warns"].append("マスクの縁がざらついている（欠損率 %.2f%%）" % (loss * 100))
    if fg_ratio < 0.08:
        r["errors"].append("前景が %.1f%% しかない。抜けすぎ（被写体が消えた）疑い" % (fg_ratio * 100))
    if fg_ratio > 0.85:
        r["errors"].append("前景が %.1f%%。ほぼ抜けていない（マスクが効いていない）疑い" % (fg_ratio * 100))

    # 2. かたまりの数（小さなゴミは除外）
    clean = cv2.morphologyEx(fgmask, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(clean, connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    big = [(i + 1, int(areas[i])) for i in range(len(areas)) if areas[i] > W * H * 0.002]
    big.sort(key=lambda t: -t[1])
    r["info"].append("前景のかたまり %d 個（画面の0.2%%以上）" % len(big))
    for i, (idx, ar) in enumerate(big[:6]):
        x, y, w, h, _ = stats[idx]
        r["info"].append("  #%d px=%d  x[%d-%d] y[%d-%d]  %.2f%%"
                         % (i + 1, ar, x, x + w, y, y + h, ar / (W * H) * 100))
    if expect_people is not None and len(big) != expect_people:
        r["warns"].append("かたまりが %d 個。想定した人数 %d と違う（髪や小物が繋がると1個に見えることに注意）"
                          % (len(big), expect_people))

    # 3. 主被写体の大きさ
    if big:
        main = big[0][1] / (W * H)
        r["info"].append("最大のかたまりが画面の %.1f%%" % (main * 100))
        if main < 0.12:
            r["warns"].append("主被写体が小さい（%.1f%%）。H3 は小さい被写体で同一性が落ちる" % (main * 100))

    # 4. 出力解像度との関係
    if out_mp:
        fed = min(mp, out_mp)
        r["info"].append("H3 に渡る実効解像度 = min(参照 %.2fMP, 出力 %.2fMP) = %.2fMP" % (mp, out_mp, fed))
        if std <= 2.0:
            r["info"].append("背景は単色なので、この解像度でも背景漏れの心配はない")
        elif fed > 0.5:
            r["errors"].append("背景が残っている状態で実効 %.2fMP。実測では0.42MP超で漏れ始める" % fed)

    # 5. アルファ
    if im.mode in ("RGBA", "LA"):
        al = np.array(im.convert("RGBA"))[..., 3]
        if al.min() < 255:
            r["warns"].append("アルファチャンネルに透明部分がある。H3 は透明を扱わないので単色に焼き込むこと")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--expect-people", type=int, default=None, help="残すはずの人数")
    ap.add_argument("--out-mp", type=float, default=None, help="H3 の出力画素数(MP)。1344x768 なら 1.03")
    a = ap.parse_args()
    bad = 0
    for p in a.images:
        if not os.path.exists(p):
            print("[%s] ファイルが無い" % p); bad = 1; continue
        print("\n########## %s ##########" % os.path.basename(p))
        r = analyze(p, a.expect_people, a.out_mp)
        for m in r["info"]:
            print("  [情報] %s" % m)
        for m in r["warns"]:
            print("  [WARN] %s" % m)
        for m in r["errors"]:
            print("  [ERROR] %s" % m)
        print("  => ERROR %d / WARN %d : %s" % (len(r["errors"]), len(r["warns"]),
                                                "OK" if not r["errors"] else "要確認"))
        if r["errors"]:
            bad = 1
    sys.exit(bad)


if __name__ == "__main__":
    main()
