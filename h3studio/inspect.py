# -*- coding: utf-8 -*-
"""
inspect.py — 生成結果の機械検査（設計書 §8）。S14/S15 で手でやった手順をコード化。

  analyze(video_path, out_dir, ffmpeg="ffmpeg") → {
      frames, fps, width, height, duration, bit_rate, mbps, size_bytes,
      audio: {mean_db, max_db} | None,
      frame_diff: float | None       # 60〜120 フレームの隣接フレーム平均絶対差（ちらつきの目安。S14=46.09 / S15=47.43）
      contact: "contact.jpg",        # 3×3 コンタクトシート（0 … 最終フレームを等間隔）
      notes: [..]                    # 正常域から外れた項目の説明（判定はしない。人が決める）
  }

正常域の目安（実測）: bit_rate 11〜14Mbps（極端に低いと破損疑い）、音量 mean -45dB 前後（無音なら異常）。
"""
from __future__ import annotations
import json, os, re, subprocess, shutil


def _run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _ffprobe(ffprobe: str, path: str) -> dict:
    r = _run([ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
              "-show_entries", "stream=nb_read_frames,nb_frames,r_frame_rate,avg_frame_rate,width,height,bit_rate,codec_name",
              "-show_entries", "format=duration,bit_rate,size",
              "-of", "json", path], timeout=120)
    if r.returncode != 0:
        raise RuntimeError("ffprobe: " + (r.stderr or "")[-300:])
    j = json.loads(r.stdout or "{}")
    st = (j.get("streams") or [{}])[0]
    fm = j.get("format") or {}
    fr = st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1"
    try:
        a, b = fr.split("/"); fps = float(a) / float(b) if float(b) else 0.0
    except Exception:
        fps = 0.0
    frames = st.get("nb_read_frames") or st.get("nb_frames")
    out = {"frames": int(frames) if frames and str(frames).isdigit() else None,
           "fps": round(fps, 3), "width": st.get("width"), "height": st.get("height"), "codec": st.get("codec_name"),
           "duration": round(float(fm.get("duration", 0) or 0), 3),
           "bit_rate": int(fm.get("bit_rate") or st.get("bit_rate") or 0), "size_bytes": int(fm.get("size") or 0)}
    out["mbps"] = round(out["bit_rate"] / 1e6, 2) if out["bit_rate"] else None
    return out


def _has_audio(ffprobe: str, path: str) -> bool:
    r = _run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path], timeout=60)
    return "audio" in (r.stdout or "")


def _volume(ffmpeg: str, path: str) -> dict | None:
    r = _run([ffmpeg, "-hide_banner", "-nostats", "-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"], timeout=300)
    txt = r.stderr or ""
    m1 = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", txt); m2 = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", txt)
    if not (m1 or m2):
        return None
    return {"mean_db": float(m1.group(1)) if m1 else None, "max_db": float(m2.group(1)) if m2 else None}


def _contact(ffmpeg: str, path: str, frames: int | None, out_png: str, cols=3, rows=3, tile_w=320) -> list[int]:
    n = frames or 0
    k = cols * rows
    if n >= k:
        idx = [round(i * (n - 1) / (k - 1)) for i in range(k)]
    else:
        idx = list(range(max(n, 1)))
    sel = "+".join("eq(n\\,%d)" % i for i in idx)
    vf = "select='%s',scale=%d:-2,tile=%dx%d:padding=2:margin=2" % (sel, tile_w, cols, rows)
    r = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-vf", vf, "-fps_mode", "vfr", "-frames:v", "1", "-q:v", "3", out_png], timeout=300)
    if r.returncode != 0 or not os.path.isfile(out_png):
        # 古い ffmpeg は -fps_mode が無い
        r = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-vf", vf, "-vsync", "vfr", "-frames:v", "1", "-q:v", "3", out_png], timeout=300)
        if r.returncode != 0:
            raise RuntimeError("contact sheet: " + (r.stderr or "")[-300:])
    return idx


def _frame_diff(ffmpeg: str, path: str, frames: int | None, start=60, end=120, w=160) -> dict | None:
    """隣接フレームの平均絶対差（グレースケール 0-255、幅 160 に縮小）。区間は 60〜120（S14/S15 と同じ）。"""
    n = frames or 0
    if n and n - 1 < end:
        # 短いときは中央付近
        span = min(60, max(n - 2, 1)); start = max(0, (n - span) // 2); end = start + span
    p = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path,
                        "-vf", "select='between(n\\,%d\\,%d)',scale=%d:-2,format=gray" % (start, end, w), "-fps_mode", "passthrough",
                        "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, timeout=300)
    data = p.stdout
    if not data:
        return None
    # 高さを知るために 1 フレームのバイト数が要る → 幅 w、高さは総量から推定できないので ffprobe の比率を使う
    try:
        import numpy as np
    except Exception:
        np = None
    # 高さは scale=-2 なので元の比率から: 呼び出し側が width/height を渡さないので、総バイト数を候補高さで割って整数になるものを探す
    cand = None
    for h in range(16, 1200):
        if h % 2:
            continue
        fsz = w * h
        if len(data) % fsz == 0 and (len(data) // fsz) >= 2 and abs((len(data) // fsz) - (end - start + 1)) <= 2:
            cand = h; break
    if cand is None:
        return None
    fsz = w * cand; nf = len(data) // fsz
    if np is not None:
        arr = np.frombuffer(data[:nf * fsz], dtype=np.uint8).reshape(nf, cand, w).astype(np.int16)
        d = np.abs(arr[1:] - arr[:-1]).mean(axis=(1, 2))
        return {"mean": round(float(d.mean()), 2), "max": round(float(d.max()), 2), "min": round(float(d.min()), 2),
                "range": [start, start + nf - 1], "frames": nf}
    tot, mx, mn = 0.0, 0.0, 1e9
    for i in range(1, nf):
        a = data[(i - 1) * fsz:i * fsz]; b = data[i * fsz:(i + 1) * fsz]
        s = sum(abs(x - y) for x, y in zip(a, b)) / fsz
        tot += s; mx = max(mx, s); mn = min(mn, s)
    return {"mean": round(tot / (nf - 1), 2), "max": round(mx, 2), "min": round(mn, 2), "range": [start, start + nf - 1], "frames": nf}


def analyze(video_path: str, out_dir: str, ffmpeg: str = "ffmpeg", expected_frames: int | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    ffprobe = "ffprobe"
    if ffmpeg and ffmpeg != "ffmpeg":
        cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if ffmpeg.lower().endswith(".exe") else ""))
        if os.path.isfile(cand):
            ffprobe = cand
    if shutil.which(ffprobe) is None and not os.path.isfile(ffprobe):
        raise RuntimeError("ffprobe が見つからない")
    out = _ffprobe(ffprobe, video_path)
    out["path"] = video_path
    out["audio"] = _volume(ffmpeg, video_path) if _has_audio(ffprobe, video_path) else None
    try:
        out["contact_frames"] = _contact(ffmpeg, video_path, out["frames"], os.path.join(out_dir, "contact.jpg"))
        out["contact"] = "contact.jpg"
    except Exception as e:
        out["contact"] = None; out["contact_error"] = repr(e)
    try:
        fd = _frame_diff(ffmpeg, video_path, out["frames"])
        out["frame_diff"] = fd["mean"] if fd else None
        out["frame_diff_detail"] = fd
    except Exception as e:
        out["frame_diff"] = None; out["frame_diff_error"] = repr(e)
    notes = []
    # 正常域の目安は 1344×768 で 11〜14 Mbps（実測）。解像度で按分する（608×352 なら 2.3 Mbps 前後）
    if out.get("mbps") is not None and out.get("width") and out.get("height"):
        expected = 11.0 * (out["width"] * out["height"]) / (1344 * 768)
        out["mbps_expected"] = round(expected, 2)
        if out["mbps"] < expected * 0.4:
            notes.append("bit_rate %.1f Mbps は同解像度の正常域（%.1f 前後）より大きく低い。破損・ベタ塗り化の疑い" % (out["mbps"], expected))
    if out["audio"] is None:
        notes.append("音声トラックが無い")
    elif out["audio"].get("mean_db") is not None and out["audio"]["mean_db"] < -70:
        notes.append("音量 mean %.1f dB はほぼ無音（正常域 -45 前後）" % out["audio"]["mean_db"])
    if expected_frames and out.get("frames") and abs(out["frames"] - expected_frames) > 1:
        notes.append("フレーム数 %d が指定 %d と違う" % (out["frames"], expected_frames))
    out["notes"] = notes
    with open(os.path.join(out_dir, "inspect.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(analyze(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "."), ensure_ascii=False, indent=1))
