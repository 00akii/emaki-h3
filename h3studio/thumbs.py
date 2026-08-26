# -*- coding: utf-8 -*-
"""参照動画のサムネイル。**先頭フレームを使う。**

**なぜ先頭なのか（中央に「直す」前に読んでほしい）**

2026-08-26 に実測した。**Windows エクスプローラーは動画の先頭フレームをサムネイルにしている。**
Shell API（`IShellItemImageFactory`）で実物のサムネイルを取り出し、全フレームと突き合わせた結果:

| 動画 | 長さ | 一致したフレーム |
|---|---|---|
| 8秒 / 192f | 8.00s | frame 3（0.08 秒・全体の 1.0%） |
| 10秒 / 240f | 10.00s | frame 2（0.04 秒・全体の 0.4%） |
| 8秒 / 192f | 8.00s | frame 2（0.04 秒・全体の 0.5%） |

長さを変えても比率は動かない。**よく言われる「1/3 地点」ではない。**

中央フレームのほうが中身の分かる動画はある（先頭が後ろ姿、暗転から始まる、など）。
それでも先頭にしているのは、**エクスプローラーと同じ絵にするため**。
違う絵にすると同じファイルが2つの見た目を持ち、**エクスプローラーで探したときに一致しない。**
「中身が分からない」はホバー再生が解決するので、静止画のほうで無理をしない。
—— ユーザーの指摘（2026-08-26）。

**方針**
- ffmpeg で1枚抜くだけ。**動画そのものには触らない**
- キャッシュは `thumbcache/`（`cutcache` と同じ流儀）。動画の mtime とサイズが変われば作り直す
- **失敗しても None を返すだけ。** サムネイルが無いのは致命ではないので、呼び出し側を止めない
"""
import hashlib
import os
import subprocess

from . import config

CACHE_DIR = os.path.join(config.APP_DIR, "thumbcache")
WIDTH = 156          # 画面では 78px 幅。Retina のために 2 倍で持つ
KEEP = 300           # 1枚 10KB 前後なので cutcache ほど切り詰めなくてよい


def _key(path: str) -> str:
    """同じ動画でも中身が変われば別キーになるように mtime とサイズを混ぜる。"""
    st = os.stat(path)
    raw = "%s|%d|%d" % (os.path.basename(path), st.st_mtime_ns, st.st_size)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _prune(keep: int = KEEP):
    """古いものから消す。溜めても小さいが、際限なく増やす理由もない。"""
    try:
        fs = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".jpg")]
        if len(fs) <= keep:
            return
        fs.sort(key=lambda p: os.path.getmtime(p))
        for p in fs[:len(fs) - keep]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def video_thumb(video_path: str) -> str | None:
    """先頭フレームの JPEG のパス。作れなければ None（呼び出し側は 404 を返せばよい）。"""
    if not os.path.isfile(video_path):
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = os.path.join(CACHE_DIR, _key(video_path) + ".jpg")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    tmp = out + ".part"
    try:
        # -frames:v 1 で最初のフレーム。-ss は付けない（付けるとキーフレーム送りで先頭からずれる）
        # **-f image2 は必須。** ffmpeg は拡張子から出力形式を決めるので、書き込み中の
        # `.jpg.part` のままだと «Unable to choose an output format» で落ちる（2026-08-26 に踏んだ）
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
                        "-frames:v", "1", "-vf", "scale=%d:-2" % WIDTH, "-q:v", "4",
                        "-f", "image2", tmp],
                       check=True, capture_output=True, timeout=60)
        if os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            _prune()
            return out
    except Exception:
        pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return None
