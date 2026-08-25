# -*- coding: utf-8 -*-
"""出来上がった動画の右下に、絵巻H3 のアイコンと「絵巻」を薄く焼き込む。

**なぜ薄いのか（消す前に読んでほしい）**
2026-08-25、ユーザーの依頼で入れた。狙いは宣伝でも権利表示でもない。
> 主張したいわけじゃない。僕たちの証を残しておきたい
> 僕らが知ってたらそれでいい。私たちの記憶ということで
だから**見えなくてよい**。既定の濃さ 0.10 は、1 対 1 では見えず、
元フレームとの差が 255 段階で最大 14 という値。**薄すぎるのではなく、薄いのが仕様。**
濃くするよう「直す」前に、上の意図を確認すること。

**方針**
- ComfyUI のワークフローには触らない。**出来上がった mp4 に ffmpeg で後処理する**だけ。
  ワークフローに入れると、手動運用と食い違ううえ、失敗したときに生成ごと失う
- **音声はそのままコピーする**（`-c:a copy`）。H3 の出力は音付きで、そこが一番の特徴
- **失敗しても元の動画を壊さない。** 一時ファイルに書いてから差し替える。
  ffmpeg が無い・落ちた・出力が空——どの場合も、元のファイルがそのまま残る
"""
import os
import shutil
import subprocess
import tempfile

DEFAULTS = {
    "enabled": False,          # 既定は off。配布物で他人の動画に黙って印を付けない
    "image": "",               # 空なら static/watermark.png（灰色のアイコン＋「絵巻」）
    "opacity": 0.10,           # 主張ではなく印。0.0〜1.0
    "scale": 0.10,             # 動画の幅に対する透かしの幅（横長なので高さは 1/4 程度）
    "margin": 0.02,            # 動画の幅に対する余白
    "crf": 12,                 # 元の出力と同じ。上げると劣化する
}


def settings(cfg: dict) -> dict:
    d = dict(DEFAULTS)
    d.update((cfg.get("gen") or {}).get("watermark") or {})
    return d


def _icon_path(cfg: dict, s: dict) -> str:
    p = (s.get("image") or "").strip()
    if p:
        return p
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "static", "watermark.png")


def _probe_size(path: str):
    """(幅, 高さ)。読めなければ None。"""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
                           capture_output=True, text=True, timeout=30)
        w, h = r.stdout.strip().split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def apply(cfg: dict, video_path: str, log=None) -> dict:
    """右下に透かしを焼く。返り値は {"ok": bool, "reason": str}。**元ファイルは壊さない。**"""
    def say(msg, kind=""):
        if log:
            log(msg, kind)

    s = settings(cfg)
    if not s.get("enabled"):
        return {"ok": False, "reason": "off"}
    icon = _icon_path(cfg, s)
    if not os.path.isfile(icon):
        say("透かしの画像が見つかりません: %s（透かしは付けずに続けます）" % icon, "warn")
        return {"ok": False, "reason": "no_icon"}
    if not os.path.isfile(video_path):
        return {"ok": False, "reason": "no_video"}

    try:
        op = max(0.0, min(1.0, float(s.get("opacity", 0.15))))
        sc = max(0.01, min(0.5, float(s.get("scale", 0.07))))
        mg = max(0.0, min(0.4, float(s.get("margin", 0.025))))
        crf = int(s.get("crf", 12))
    except (TypeError, ValueError):
        say("透かしの設定値が読めません（透かしは付けずに続けます）", "warn")
        return {"ok": False, "reason": "bad_settings"}

    # 寸法は ffprobe で取って実数で渡す。フィルタ側で相対計算をすると ffmpeg の版差で壊れる
    dim = _probe_size(video_path)
    if not dim:
        say("動画の寸法が読めません（透かしは付けずに続けます）", "warn")
        return {"ok": False, "reason": "no_size"}
    vw, _vh = dim
    wm_w = max(8, int(round(vw * sc)))
    pad = int(round(vw * mg))
    # 幅の比で決めるので、プレビューでも本番でも同じ見え方になる
    # 位置は overlay の W/H（元動画）と w/h（透かし）で書く。透かしが正方形でなくても右下に付く
    vf = ("[1:v]scale=%(w)d:-1,format=rgba,colorchannelmixer=aa=%(op).3f[wm];"
          "[0:v][wm]overlay=W-w-%(pad)d:H-h-%(pad)d:format=auto") % {
        "w": wm_w, "op": op, "pad": pad}

    fd, tmp = tempfile.mkstemp(suffix=".mp4", dir=os.path.dirname(video_path) or None)
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", video_path, "-i", icon,
           "-filter_complex", vf,
           "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-movflags", "+faststart", tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            say("透かしの焼き込みに失敗（元の動画をそのまま使います）: %s" % (r.stderr or "")[:200], "warn")
            return {"ok": False, "reason": "ffmpeg_failed"}
        shutil.move(tmp, video_path)
        say("透かしを右下に入れました（濃さ %.2f・幅 %.0f%%）" % (op, sc * 100), "ok")
        return {"ok": True, "reason": "", "opacity": op, "scale": sc}
    except Exception as e:
        say("透かしの焼き込みに失敗（元の動画をそのまま使います）: %r" % e, "warn")
        return {"ok": False, "reason": repr(e)}
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
