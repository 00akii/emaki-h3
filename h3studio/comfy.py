# -*- coding: utf-8 -*-
"""
comfy.py — ComfyUI への生成投入（段4）。

  設計書 §6: 既存ワークフロー JSON（video_minimax_h3_i2v.json）から「固定で持つ値」
  （UNET / LoRA / CLIP / VAE / サンプラー / 参照動画の読み方 / 出力形式）だけ読み、
  API 形式の prompt JSON は自前で組む。SAM3 サブグラフは通さず LoadImage(_cut.png) を直結。
  プロンプトは MiniMaxH3ReferenceToVideo.prompt に直接文字列（TextFileLoaderMC は使わない）。

  Autogrow 入力の API キーは平たい "ref_images.ref_image_0" 形式（comfy_api/latest/_io.py の
  finalize_prefix が "." で連結する。GUI の保存 JSON もこの名前）。

  ジョブは Thread で回し、進捗は /ws?clientId=… の progress イベント（step/max）、
  完了は executing(node=None) か /history で見る。WS が使えなければ /history ポーリングに落ちる。
"""
from __future__ import annotations
import json, math, os, threading, time, uuid, urllib.request, urllib.error, urllib.parse
from . import config, llm

JOBS: dict[str, "Job"] = {}
# 直前に走ったジョブの「VRAM に載るモデルの組み合わせ」と、それが成功したか。vram_mode="auto" の判断に使う。
# プロセスを再起動したら消えてよい（分からなければ安全側の share に倒れる）。
_LAST_RUN: dict = {"sig": None, "ok": False}
JOBS_DIR = os.path.join(config.APP_DIR, "jobs")

# ワークフロー JSON に該当ノードが無いときの最後の手段（2026-08 の検証環境の値）
FALLBACK = {
    "unet_name": "minimax_h3_ref2va_pruned_fp8_scaled.safetensors",
    "weight_dtype": "default",
    "lora_name": "MiniMax-H3\\minimax_h3_ref2v_turbo_4step_v0.1_comfyui_resized_avg_rank_21_bf16.safetensors",
    "lora_strength": 1.0,
    "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "clip_type": "minimax",
    "clip_device": "cpu",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "sampler": "euler",
    "scheduler": "normal",
    "steps": 6,
    "denoise": 1.0,
    "ref_video_force_rate": 24,
    "ref_video_format": "Cosmos",
    "out_format": "video/h264-mp4",
    "out_pix_fmt": "yuv420p",
    "out_crf": 12,
    "fps": 24,
}


# ---------------- 必要なノード（他の PC で足りるかの確認） ----------------
# ComfyUI 本体（0.33 以降）に入っているものは pack="本体"。それ以外はカスタムノードの導入が要る。
# need: "required" が無いと生成できない / "fallback" は代替に落ちる / "optional" は無くても動く
NODE_REQS = [
    {"cls": "MiniMaxH3ReferenceToVideo", "need": "required", "pack": "本体",
     "what": "H3 の参照エンコード本体", "fallback": ""},
    {"cls": "UNETLoader", "need": "required", "pack": "本体", "what": "UNET 読み込み", "fallback": ""},
    {"cls": "CLIPLoader", "need": "required", "pack": "本体", "what": "テキストエンコーダ", "fallback": ""},
    {"cls": "SamplerCustomAdvanced", "need": "required", "pack": "本体", "what": "サンプラー", "fallback": ""},
    {"cls": "VHS_VideoCombine", "need": "fallback", "pack": "ComfyUI-VideoHelperSuite",
     "what": "映像＋音声を mp4 にまとめて保存",
     "fallback": "本体の CreateVideo + SaveVideo に落ちる（crf / pix_fmt は指定できない）"},
    {"cls": "VHS_LoadVideo", "need": "optional", "pack": "ComfyUI-VideoHelperSuite",
     "what": "参照動画の読み込み（fps 変換・フレーム数制限つき）",
     "fallback": "無い場合は参照動画を使えない。参照画像だけなら生成できる"},
    {"cls": "SAM3_Detect", "need": "optional", "pack": "本体",
     "what": "切り抜き（段6b）",
     "fallback": "無い場合は切り抜き画面が使えない。切り抜き済み画像を input に置けば生成はできる"},
    {"cls": "DisplayAny", "need": "optional", "pack": "ComfyUI-Easy-Use など",
     "what": "切り抜きの検出枠を取り出す", "fallback": "本体の PreviewAny に落ちる"},
    {"cls": "LayerUtility: PurgeVRAM V2", "need": "optional", "pack": "ComfyUI-LayerStyle",
     "what": "サンプリング前後の VRAM 解放",
     "fallback": "無い場合は /free のみ。2 本目以降が大きく遅くなることがある（設計書 §9a）"},
]


def clear_node_cache() -> None:
    """ComfyUI にカスタムノードを入れて再起動したあと、確認をやり直すため。"""
    _NODE_CACHE.clear()


def preflight(cfg: dict) -> dict:
    """この ComfyUI で 絵巻H3 が動くかを見る。画面の「設定」に出す。"""
    if not comfy_up(cfg):
        return {"comfy": False, "can_generate": False, "nodes": [],
                "message": "ComfyUI に届きません（%s）" % cfg.get("comfy_url")}
    nodes, can = [], True
    for r in NODE_REQS:
        ok = node_available(cfg, r["cls"])
        if not ok and r["need"] == "required":
            can = False
        nodes.append(dict(r, ok=ok))
    miss = [n for n in nodes if not n["ok"]]
    if not miss:
        msg = "必要なノードは揃っています"
    elif not can:
        msg = "生成に必要なノードがありません: " + "、".join(n["cls"] for n in miss if n["need"] == "required")
    else:
        msg = "無くても動きますが機能が落ちます: " + "、".join(n["cls"] for n in miss)
    models = model_files(cfg)
    bad_models = [m for m in models if not m["ok"]]
    if bad_models:
        can = False
        msg = ((msg + " / ") if miss else "") + "重みが見つかりません: " + "、".join(m["what"] for m in bad_models)
    vram = vram_check(cfg)
    if vram.get("level") in ("warn", "tight"):
        msg = msg + " / " + vram["message"]
    return {"comfy": True, "can_generate": can, "nodes": nodes, "models": models,
            "vram": vram, "message": msg}


# 実測（RTX 4090 24GB）に基づく目安。**これ以外の容量では検証していない。**
# UNET は Q5_K_M GGUF で約 20GB、LLM（27B Q4_K_S）が約 17GB。アプリは両者を**時分割**で載せる
# （同時に載せるわけではない）ので、必要なのは「大きい方＋作業領域」であって合計ではない。
VRAM_UNET_GB = 20.0
VRAM_WORK_GB = 3.0      # 活性化メモリと VAE。24GB 機で UNET 常駐時の空きが 1〜4GB だった実測から


def vram_check(cfg: dict) -> dict:
    """GPU の容量が足りそうかを**先に言う**。落とさないし止めない（断定できる実測が無いため）。

    実測は 24GB の1環境しか無い。よって「動かない」とは書かず、**何が起きるかを具体的に伝える**。
    判定に使うのは `/system_stats` の総容量だけ（空き容量は使わない — ComfyUI 自身の torch 会計で
    デバイス全体の空きを表さないことが実測で分かっている。`gpu.residency()` の docstring 参照）。
    """
    try:
        with urllib.request.urlopen(cfg["comfy_url"].rstrip("/") + "/system_stats", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        dev = (d.get("devices") or [{}])[0]
        total_gb = round((dev.get("vram_total") or 0) / 1e9, 1)
    except Exception as e:
        return {"level": "unknown", "message": "", "error": str(e)[:80]}
    if not total_gb:
        return {"level": "unknown", "message": ""}
    need = VRAM_UNET_GB + VRAM_WORK_GB
    out = {"level": "ok", "total_gb": total_gb, "need_gb": need, "message": "",
           "verified_on_gb": 24, "note": "実測は 24GB の1環境のみ。それ以外は未検証"}
    if total_gb >= need:
        return out
    if total_gb >= VRAM_UNET_GB:
        out["level"] = "tight"
        out["message"] = ("GPU の VRAM が %.1fGB です。UNET だけで約 %.0fGB を使うため、作業領域がほとんど残りません。"
                          "生成は通るかもしれませんが、step 1 に数分かかる・VAE デコードが極端に遅い、といった形で出ます。"
                          "本アプリの実測は 24GB の環境のみで、この容量では検証していません。"
                          % (total_gb, VRAM_UNET_GB))
        return out
    out["level"] = "warn"
    out["message"] = ("GPU の VRAM が %.1fGB です。UNET だけで約 %.0fGB 必要なので、"
                      "**このままでは生成が VRAM 不足で失敗する可能性が高い**です（ComfyUI 側のエラーとして出ます）。"
                      "解像度を下げる・より小さい量子化の重みを使う等の調整が要ります。"
                      "本アプリの実測は 24GB の環境のみで、少ない容量では検証していません。"
                      % (total_gb, VRAM_UNET_GB))
    return out


def _combo_options(cfg: dict, cls: str, field: str) -> list[str] | None:
    """/object_info からその入力の選択肢（ファイル名の一覧）を取る。取れなければ None。"""
    try:
        d = _get(cfg, "/object_info/" + urllib.parse.quote(cls), timeout=10).get(cls) or {}
    except Exception:
        return None
    for kind in ("required", "optional"):
        spec = (d.get("input", {}) or {}).get(kind, {}) or {}
        if field in spec:
            v = spec[field]
            opts = v[0] if isinstance(v, list) and v else None
            if isinstance(opts, list):
                return [str(x) for x in opts]
            if isinstance(v, list) and len(v) > 1 and isinstance(v[1], dict):
                o = v[1].get("options")
                if isinstance(o, list):
                    return [str(x) for x in o]
    return None


def model_files(cfg: dict, values: dict | None = None) -> list[dict]:
    """ワークフローから読んだ重みが、この ComfyUI に実在するか。
    よその PC ではファイル名が違うのが普通なので、生成前に名前で照合して教える。"""
    v = values or workflow_values(cfg)["values"]
    want = [
        ("UNETLoader", "unet_name", v.get("unet_name"), "H3 の UNET"),
        ("LoraLoaderModelOnly", "lora_name", v.get("lora_name"), "Turbo LoRA（4/6 step 用）"),
        ("CLIPLoader", "clip_name", v.get("clip_name"), "テキストエンコーダ"),
        ("VAELoader", "vae_name", v.get("video_vae"), "映像 VAE"),
        ("VAELoader", "vae_name", v.get("audio_vae"), "音声 VAE"),
    ]
    out = []
    for cls, field, name, what in want:
        opts = _combo_options(cfg, cls, field)
        if not name:
            out.append({"what": what, "name": "", "ok": False, "detail": "ワークフローから読めていません"})
            continue
        if opts is None:
            out.append({"what": what, "name": name, "ok": True, "detail": "確認できず（一覧が取れない）"})
            continue
        # ComfyUI は区切りに \ を返す環境と / を返す環境がある
        norm = lambda x: str(x).replace("\\", "/").lower()
        ok = norm(name) in {norm(o) for o in opts}
        near = [o for o in opts if os.path.basename(norm(o)) == os.path.basename(norm(name))]
        detail = "あり" if ok else ("名前が違います。候補: " + (near[0] if near else "、".join(opts[:3]) or "（0 件）"))
        out.append({"what": what, "name": name, "ok": ok, "detail": detail})
    return out


def output_node(cfg: dict) -> str:
    """出力ノードを選ぶ。VideoHelperSuite が無ければ本体の CreateVideo + SaveVideo。"""
    return "VHS_VideoCombine" if node_available(cfg, "VHS_VideoCombine") else "SaveVideo"

# ---------------- ワークフロー JSON から固定値を読む ----------------

def _load_workflow(cfg: dict) -> dict | None:
    p = cfg.get("workflow_json") or ""
    if not p or not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _nodes_of(wf: dict, typ: str) -> list[dict]:
    return [n for n in (wf or {}).get("nodes", []) if n.get("type") == typ]


def workflow_values(cfg: dict) -> dict:
    """
    ワークフロー JSON から「固定で持つ値」を拾う。見つからない項目は FALLBACK。
    返り値には source（どこから取ったか）も入れて画面に出せるようにする。
    """
    v = dict(FALLBACK)
    src = {}
    wf = _load_workflow(cfg)
    if not wf:
        return {"values": v, "source": {"_": "workflow_json が読めないので FALLBACK"}}

    def pick(typ, fn):
        for n in _nodes_of(wf, typ):
            w = n.get("widgets_values")
            if w is None:
                continue
            try:
                if fn(w):
                    src[typ] = n.get("id")
                    return True
            except Exception:
                continue
        return False

    # UNET: ref2va を含むものを優先
    def _unet(w):
        if isinstance(w, list) and w and "ref2va" in str(w[0]).lower():
            v["unet_name"], v["weight_dtype"] = w[0], (w[1] if len(w) > 1 else "default"); return True
        return False
    pick("UNETLoader", _unet)

    def _lora(w):
        if isinstance(w, list) and w and "ref2v" in str(w[0]).lower():
            v["lora_name"] = w[0]; v["lora_strength"] = float(w[1]) if len(w) > 1 else 1.0; return True
        return False
    pick("LoraLoaderModelOnly", _lora)

    def _clip(w):
        if isinstance(w, list) and w and "minimax" in str(w[0]).lower():
            v["clip_name"] = w[0]; v["clip_type"] = w[1] if len(w) > 1 else "minimax"; v["clip_device"] = w[2] if len(w) > 2 else "cpu"; return True
        return False
    pick("CLIPLoader", _clip)

    for n in _nodes_of(wf, "VAELoader"):
        w = n.get("widgets_values") or []
        if w and "audio" in str(w[0]).lower():
            v["audio_vae"] = w[0]; src["VAELoader(audio)"] = n.get("id")
        elif w and "video" in str(w[0]).lower():
            v["video_vae"] = w[0]; src["VAELoader(video)"] = n.get("id")

    pick("KSamplerSelect", lambda w: (v.__setitem__("sampler", w[0]), True)[1])
    pick("BasicScheduler", lambda w: (v.__setitem__("scheduler", w[0]), v.__setitem__("steps", int(w[1])), v.__setitem__("denoise", float(w[2]) if len(w) > 2 else 1.0), True)[3])

    def _vhs_load(w):
        if isinstance(w, dict):
            v["ref_video_force_rate"] = w.get("force_rate", v["ref_video_force_rate"])
            v["ref_video_format"] = w.get("format", v["ref_video_format"]); return True
        return False
    pick("VHS_LoadVideo", _vhs_load)

    def _vhs_comb(w):
        if isinstance(w, dict):
            v["out_format"] = w.get("format", v["out_format"]); v["out_pix_fmt"] = w.get("pix_fmt", v["out_pix_fmt"])
            v["out_crf"] = w.get("crf", v["out_crf"]); v["fps"] = w.get("frame_rate", v["fps"]); return True
        return False
    pick("VHS_VideoCombine", _vhs_comb)

    # config.gen の値が最優先（画面から変えられる側）
    g = cfg.get("gen") or {}
    for k_cfg, k_v in (("sampler", "sampler"), ("scheduler", "scheduler"), ("steps", "steps")):
        if g.get(k_cfg) is not None:
            v[k_v] = g[k_cfg]; src[k_v] = "config.gen"
    return {"values": v, "source": src}


def workflow_snapshot(cfg: dict) -> dict:
    """GUI 手動運用との食い違い確認用: いまのワークフローの duration / 参照画像の配線 / 参照動画。"""
    wf = _load_workflow(cfg)
    out = {"path": cfg.get("workflow_json"), "duration": None, "ref_images": [], "ref_videos": [], "via_sam3": False}
    if not wf:
        return out
    for n in _nodes_of(wf, "PrimitiveFloat"):
        if "duration" in str(n.get("title", "")).lower():
            out["duration"] = (n.get("widgets_values") or [None])[0]
    h3 = next(iter(_nodes_of(wf, "MiniMaxH3ReferenceToVideo")), None)
    if h3:
        by_id = {n["id"]: n for n in wf.get("nodes", [])}
        links = {l[0]: l for l in wf.get("links", [])}
        for inp in h3.get("inputs", []):
            nm, lk = inp.get("name", ""), inp.get("link")
            if lk is None or lk not in links:
                continue
            up = by_id.get(links[lk][1], {})
            if nm.startswith("ref_images."):
                t = up.get("type", "")
                if len(t) > 30 and "-" in t:  # サブグラフ（UUID 型名）
                    out["via_sam3"] = True; out["ref_images"].append({"input": nm, "from": "subgraph"})
                else:
                    out["ref_images"].append({"input": nm, "from": t, "file": (up.get("widgets_values") or [None])[0]})
            if nm.startswith("ref_videos."):
                w = up.get("widgets_values") or {}
                out["ref_videos"].append({"input": nm, "file": w.get("video") if isinstance(w, dict) else None,
                                          "frame_load_cap": w.get("frame_load_cap") if isinstance(w, dict) else None})
    return out


# ---------------- サイズ・尺 ----------------

def size_for(cfg: dict, mode: str, ratio: str) -> tuple[int, int]:
    """config.gen.preview/final の画素面積を保ったまま比率を変える（32 の倍数）。16:9 は設定値そのまま。"""
    g = (cfg.get("gen") or {}).get(mode) or {}
    w0, h0 = int(g.get("width", 608)), int(g.get("height", 352))
    try:
        rw, rh = [float(x) for x in ratio.replace("：", ":").split(":")]
    except Exception:
        rw, rh = 16.0, 9.0
    if abs(rw / rh - 16 / 9) < 1e-6:
        return w0, h0
    area = w0 * h0
    w = int(round(math.sqrt(area * rw / rh) / 32)) * 32
    h = int(round(math.sqrt(area * rh / rw) / 32)) * 32
    return max(32, w), max(32, h)


# ---------------- API 形式の組み立て ----------------

def build_prompt(cfg: dict, prompt_text: str, images: list[str], videos: list[str],
                 width: int, height: int, length: int, seed: int, filename_prefix: str,
                 values: dict | None = None, purge_node: str | None = None,
                 out_node: str = "VHS_VideoCombine") -> dict:
    """purge_node: 参照エンコード後・サンプリング前に VRAM を空けるノード（手動ワークフローの LayerUtility: PurgeVRAM V2）。
    実測: これ無しで 2 本目を回すと前回の UNET+VAE が残ったままサンプラーに入り、ブロック単位で重みを出し入れして 10 倍以上遅くなる。"""
    v = values or workflow_values(cfg)["values"]
    rv = (cfg.get("gen") or {}).get("ref_video") or {}
    g = {}
    g["unet"] = {"class_type": "UNETLoader", "inputs": {"unet_name": v["unet_name"], "weight_dtype": v["weight_dtype"]}}
    g["lora"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["unet", 0], "lora_name": v["lora_name"], "strength_model": float(v["lora_strength"])}}
    g["clip"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": v["clip_name"], "type": v["clip_type"], "device": v["clip_device"]}}
    g["vae"] = {"class_type": "VAELoader", "inputs": {"vae_name": v["video_vae"]}}
    g["avae"] = {"class_type": "VAELoader", "inputs": {"vae_name": v["audio_vae"]}}
    h3_in = {"clip": ["clip", 0], "vae": ["vae", 0], "audio_vae": ["avae", 0],
             "prompt": prompt_text, "width": int(width), "height": int(height), "length": int(length),
             "ref_image_size": "match"}
    for i, img in enumerate(images[:9]):
        nid = "img%d" % i
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": img}}
        h3_in["ref_images.ref_image_%d" % i] = [nid, 0]
    for i, vid in enumerate(videos[:3]):
        nid = "vid%d" % i
        g[nid] = {"class_type": "VHS_LoadVideo", "inputs": {
            "video": vid, "force_rate": v["ref_video_force_rate"],
            "custom_width": int(rv.get("width", 608)), "custom_height": int(rv.get("height", 352)),
            "frame_load_cap": int(rv.get("frame_load_cap", 97)), "skip_first_frames": int(rv.get("skip_first_frames", 48)),
            "select_every_nth": 1, "format": v["ref_video_format"]}}
        h3_in["ref_videos.ref_video_%d" % i] = [nid, 0]
    g["h3"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": h3_in}
    g["noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}}
    g["guider"] = {"class_type": "BasicGuider", "inputs": {"model": ["lora", 0], "conditioning": ["h3", 0]}}
    g["sampler"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": v["sampler"]}}
    g["sigmas"] = {"class_type": "BasicScheduler", "inputs": {"model": ["lora", 0], "scheduler": v["scheduler"], "steps": int(v["steps"]), "denoise": float(v["denoise"])}}
    latent_src = ["h3", 1]
    if purge_node:
        # purge1: 参照エンコードの VAE を退避してから UNET(20GB) を載せる（無いと step1 が 185s、有ると 15s）
        g["purge"] = {"class_type": purge_node, "inputs": {"anything": ["h3", 1], "purge_cache": True, "purge_models": True}}
        latent_src = ["purge", 0]
    g["sample"] = {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0], "sigmas": ["sigmas", 0], "latent_image": latent_src}}
    dec_src = ["sample", 0]
    if purge_node:
        # purge2: UNET を退避してから VAE デコード（無いと UNET 常駐のままデコードがスラッシングして 5 分以上）
        g["purge2"] = {"class_type": purge_node, "inputs": {"anything": ["sample", 0], "purge_cache": True, "purge_models": True}}
        dec_src = ["purge2", 0]
    g["dec"] = {"class_type": "VAEDecode", "inputs": {"samples": dec_src, "vae": ["vae", 0]}}
    g["adec"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": dec_src, "vae": ["avae", 0]}}
    if out_node == "VHS_VideoCombine":
        g["out"] = {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["dec", 0], "audio": ["adec", 0], "frame_rate": int(v["fps"]), "loop_count": 0,
            "filename_prefix": filename_prefix, "format": v["out_format"], "pingpong": False, "save_output": True,
            "pix_fmt": v["out_pix_fmt"], "crf": int(v["out_crf"]), "save_metadata": True, "trim_to_audio": False}}
    else:
        # VideoHelperSuite が無い環境向け。本体だけで映像＋音声を書く（crf / pix_fmt は本体任せ）
        g["mkvid"] = {"class_type": "CreateVideo", "inputs": {
            "images": ["dec", 0], "audio": ["adec", 0], "fps": float(v["fps"])}}
        g["out"] = {"class_type": "SaveVideo", "inputs": {
            "video": ["mkvid", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}}
    return g


# ---------------- ComfyUI HTTP ----------------

def _base(cfg: dict) -> str:
    return cfg["comfy_url"].rstrip("/")


def _get(cfg: dict, path: str, timeout=10):
    with urllib.request.urlopen(_base(cfg) + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(cfg: dict, path: str, body: dict, timeout=30):
    req = urllib.request.Request(_base(cfg) + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def comfy_up(cfg: dict) -> bool:
    try:
        _get(cfg, "/system_stats", timeout=3); return True
    except Exception:
        return False


def queue_state(cfg: dict) -> dict:
    q = _get(cfg, "/queue", timeout=5)
    return {"running": len(q.get("queue_running", [])), "pending": len(q.get("queue_pending", [])),
            "running_ids": [x[1] for x in q.get("queue_running", []) if len(x) > 1],
            "pending_ids": [x[1] for x in q.get("queue_pending", []) if len(x) > 1]}


def submit(cfg: dict, graph: dict, client_id: str) -> str:
    try:
        r = _post(cfg, "/prompt", {"prompt": graph, "client_id": client_id})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
            msg = j.get("error", {}).get("message", "") + " " + json.dumps(j.get("node_errors", {}), ensure_ascii=False)[:800]
        except Exception:
            msg = body[:800]
        raise RuntimeError("ComfyUI /prompt が %s: %s" % (e.code, msg))
    if "prompt_id" not in r:
        raise RuntimeError("prompt_id が返らない: %s" % json.dumps(r, ensure_ascii=False)[:400])
    return r["prompt_id"]


def history_item(cfg: dict, prompt_id: str) -> dict | None:
    try:
        h = _get(cfg, "/history/" + prompt_id, timeout=10)
        return h.get(prompt_id)
    except Exception:
        return None


def cancel_prompt(cfg: dict, prompt_id: str, running: bool):
    if running:
        _post(cfg, "/interrupt", {})
    else:
        _post(cfg, "/queue", {"delete": [prompt_id]})


_NODE_CACHE: dict[str, bool] = {}


def node_available(cfg: dict, class_name: str) -> bool:
    """/object_info/<class> で存在確認（結果はプロセス内でキャッシュ）。"""
    if not class_name:
        return False
    if class_name in _NODE_CACHE:
        return _NODE_CACHE[class_name]
    try:
        d = _get(cfg, "/object_info/" + urllib.parse.quote(class_name), timeout=10)
        ok = class_name in d
    except Exception:
        ok = False
    _NODE_CACHE[class_name] = ok
    return ok


def model_sig(values: dict) -> str:
    """VRAM を占めるモデルの組み合わせ。これが前回と同じなら、載っているものを使い回せる。
    サイズ・尺・seed は含めない（重みの出入りには関係しないため）。"""
    return "|".join(str(values.get(k, "")) for k in
                    ("unet_name", "weight_dtype", "lora_name", "lora_strength", "clip_name", "vae_name", "audio_vae_name"))


def decide_vram_mode(cfg: dict, values: dict, llm_was_unloaded: bool) -> tuple[str, str]:
    """"share" か "resident" のどちらで走らせるかを決める。返り値は (モード, 理由の一行)。"""
    g = cfg.get("gen") or {}
    mode = str(g.get("vram_mode", "share") or "share").lower()
    if mode == "resident":
        return "resident", "設定が resident: /free も purge もしない"
    if mode != "auto":
        return "share", "設定が share: /free で降ろして purge を挟む"
    sig = model_sig(values)
    if llm_was_unloaded:
        return "share", "auto: 直前まで LM Studio が GPU を持っていたので、載せ直しから始める"
    if not _LAST_RUN.get("ok"):
        return "share", "auto: 直前に成功したジョブが無い（初回か、前回が失敗）"
    if _LAST_RUN.get("sig") != sig:
        return "share", "auto: 前のジョブとモデル構成が違う"
    return "resident", "auto: 前のジョブと同じモデル構成で、間に GPU を取られていない → 載せたまま使う"


def free_vram(cfg: dict, unload_models: bool = True, free_memory: bool = False) -> dict:
    """ComfyUI に常駐モデルを降ろさせる。free_memory=True は実行キャッシュも消す（次の同一プロンプトで
    CPU のテキストエンコード ~170s をやり直すことになる）ので、生成前は unload_models だけにする。"""
    try:
        _post(cfg, "/free", {"unload_models": bool(unload_models), "free_memory": bool(free_memory)}); return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


# ---------------- ジョブ ----------------

class Job:
    def __init__(self, params: dict):
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.params = params            # project, shot_id, prompt, mode, seed, duration, ratio, images, videos, length, width, height
        self.state = "queued"           # queued → unloading → submitted → running → inspecting → done | error | cancelled
        self.created = time.time()
        self.started = None
        self.finished = None
        self.prompt_id = None
        self.client_id = uuid.uuid4().hex
        self.progress = {"value": 0, "max": 0, "node": None}
        self.log: list[dict] = []
        self.error = None
        self.result: dict | None = None
        self.graph: dict | None = None
        self.cached = False             # 同一入力で ComfyUI のキャッシュが返った（再生成されていない）
        self.vram_mode = "share"        # share: /free + purge / resident: 載せたまま（config.gen.vram_mode）
        self._cancel = False
        self._lock = threading.Lock()

    def add(self, msg: str, kind: str = ""):
        with self._lock:
            self.log.append({"t": round(time.time() - self.created, 1), "msg": msg, "kind": kind})

    def to_dict(self, full=False) -> dict:
        d = {"id": self.id, "state": self.state, "created": self.created, "started": self.started, "finished": self.finished,
             "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1),
             "prompt_id": self.prompt_id, "progress": self.progress, "error": self.error, "cached": self.cached,
             "vram_mode": self.vram_mode,
             "params": {k: v for k, v in self.params.items() if k != "prompt"},
             "log": self.log[-80:], "result": self.result}
        if full:
            d["params"] = self.params
        return d

    def save(self):
        try:
            os.makedirs(os.path.join(JOBS_DIR, self.id), exist_ok=True)
            with open(os.path.join(JOBS_DIR, self.id, "job.json"), "w", encoding="utf-8") as f:
                json.dump(self.to_dict(full=True), f, ensure_ascii=False, indent=1)
        except Exception:
            pass


def _ws_listen(cfg: dict, job: Job, on_done, after_connect=None):
    """WS で progress / executing を受ける。after_connect は最初の接続直後（投入前）に呼ぶ＝イベントを取りこぼさない。
    切れたら同じ clientId で繋ぎ直す（実測: 重い処理中に ConnectionClosed が起きる）。
    戻り値: True=完了を WS で観測、False=WS が使えない（呼び出し側がポーリングに落とす）。"""
    try:
        import asyncio, websockets
    except Exception:
        return False
    u = urllib.parse.urlparse(_base(cfg))
    ws_url = "ws://%s%s/ws?clientId=%s" % (u.netloc, u.path.rstrip("/"), job.client_id)
    first = {"done": False}

    def _hist_state():
        h = history_item(cfg, job.prompt_id) if job.prompt_id else None
        if not h:
            return None
        st = h.get("status", {})
        if st.get("status_str") == "error":
            msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
            if msgs:
                job.error = (msgs[-1][1].get("exception_message") or "execution_error")[:800]
            return "error"
        if h.get("outputs") or st.get("completed"):
            return "done"
        return None

    async def session():
        async with websockets.connect(ws_url, max_size=None, ping_interval=None, open_timeout=10) as ws:
            if not first["done"]:
                job.add("WS 接続 (進捗を受信)", "")
                if after_connect:
                    after_connect()
                first["done"] = True
            else:
                job.add("WS 再接続", "")
                hs = _hist_state()
                if hs:
                    return hs
            while True:
                if job._cancel:
                    return "cancel"
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=45)
                except asyncio.TimeoutError:
                    hs = _hist_state()
                    if hs:
                        return hs
                    continue
                if isinstance(msg, (bytes, bytearray)):
                    continue  # プレビュー画像
                try:
                    ev = json.loads(msg)
                except Exception:
                    continue
                t, d = ev.get("type"), ev.get("data", {})
                if d.get("prompt_id") not in (None, job.prompt_id):
                    continue
                if t == "execution_start":
                    job.state = "running"; job.started = job.started or time.time(); job.add("ComfyUI 実行開始", "now")
                elif t == "executing":
                    node = d.get("node")
                    if node is None and d.get("prompt_id") == job.prompt_id:
                        return "done"
                    if node:
                        job.progress["node"] = node
                        cls = (job.graph or {}).get(node, {}).get("class_type", node)
                        job.add("ノード: %s" % cls, "")
                        if node == "sample":
                            job.progress["sampler_t0"] = time.time()
                elif t == "progress":
                    job.progress["value"] = d.get("value", 0); job.progress["max"] = d.get("max", 0); job.progress["node"] = d.get("node")
                    if d.get("max"):
                        job.add("step %s/%s" % (d.get("value"), d.get("max")), "now")
                        t0 = job.progress.get("sampler_t0")
                        if d.get("node") == "sample" and d.get("value") == 1 and t0 and time.time() - t0 > 300 and not job.progress.get("slow_warned"):
                            job.progress["slow_warned"] = True
                            # 2026-08-25 の実測（15本）: 同じ空き VRAM から step1 が 6 秒のことも 494 秒のこともある。
                            # 原因は特定できていないので、原因も対処も断定しない。ユーザーに要るのは「待っていいのか」だけ。
                            job.add("⚠ step 1 に %d 秒かかっています。モデルの読み込みで時間がかかることがあります"
                                    "（実測で 6 秒〜8 分。原因は分かっていません）。**そのまま待ってください。**"
                                    "ごくまれに完了も中止もできなくなります。その場合だけ ComfyUI の再起動が要ります" % int(time.time() - t0), "warn")
                elif t == "execution_error":
                    job.error = (d.get("exception_message") or "execution_error")[:800]
                    job.add("ComfyUI エラー: " + job.error, "bad")
                    return "error"
                elif t == "execution_interrupted":
                    return "cancel"
                elif t == "execution_cached":
                    nodes = d.get("nodes") or []
                    if "out" in nodes:
                        job.cached = True
                        job.add("同一入力のため ComfyUI のキャッシュが返ります（再生成されない）。変えるなら seed かプロンプト", "warn")
                    elif "h3" in nodes:
                        job.add("参照エンコードはキャッシュ（同じ素材・同じプロンプト）→ サンプリングから", "")

    attempts = 0
    while True:
        try:
            res = asyncio.run(session())
            on_done(res)
            return True
        except RuntimeError as e:
            if "ComfyUI /prompt" in str(e) or "prompt_id" in str(e):
                raise  # 投入そのものの失敗は上に上げる
            err = e
        except Exception as e:
            err = e
        if not first["done"]:
            job.add("WS が開けない (%s) → ポーリングに切替" % err.__class__.__name__, "warn")
            return False
        attempts += 1
        if attempts > 200:
            job.add("WS の再接続を諦めた → ポーリングに切替", "warn")
            return False
        if attempts <= 3 or attempts % 20 == 0:
            job.add("WS が切れた (%s) → %d 回目の再接続" % (err.__class__.__name__, attempts), "warn")
        time.sleep(3.0)
        hs = _hist_state()
        if hs:
            on_done(hs); return True
        if job._cancel:
            on_done("cancel"); return True


def _poll_until_done(cfg: dict, job: Job, on_done):
    # ComfyUI が「履歴にも無い・キューにも無い」状態が続いたら、向こうはこのジョブを見失っている。
    # ここで終わらせないと state が running のまま残り、active_job() が塞がって**新規投入が永久にできなくなる**
    # （2026-08-25、ComfyUI が落ちた後・こちらから kill した後の両方で実際に起きた。アプリ再起動でしか回復しなかった）。
    # 投入直後は一瞬どちらにも居ないことがあるので、連続で見失ったときだけ確定する。
    lost = 0
    LOST_LIMIT = 10          # 2 秒間隔 → 約 20 秒
    while True:
        if job._cancel:
            on_done("cancel"); return
        h = history_item(cfg, job.prompt_id)
        if h:
            st = h.get("status", {})
            if st.get("status_str") == "error":
                job.error = "ComfyUI の実行エラー（/history 参照）"
                msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                if msgs:
                    job.error = (msgs[-1][1].get("exception_message") or job.error)[:800]
                on_done("error"); return
            if st.get("completed") or h.get("outputs"):
                on_done("done"); return
        else:
            try:
                q = queue_state(cfg)
                if job.prompt_id in q["running_ids"]:
                    lost = 0
                    if job.state != "running":
                        job.state = "running"; job.started = job.started or time.time(); job.add("ComfyUI 実行中", "now")
                elif job.prompt_id in q["pending_ids"]:
                    lost = 0
                    job.state = "submitted"
                else:
                    # ComfyUI には届いたのに、履歴にもキューにも居ない。落ちたか、外から止められた。
                    lost += 1
                    if lost >= LOST_LIMIT:
                        job.error = ("ComfyUI がこのジョブを見失いました（履歴にもキューにも無い）。"
                                     "ComfyUI が落ちたか、外から停止された可能性があります")
                        job.add("⚠ " + job.error, "bad")
                        on_done("error"); return
            except Exception:
                # ComfyUI に届かないだけかもしれないので、見失った回数には数えない
                pass
        time.sleep(2.0)


def _find_video(cfg: dict, hist: dict) -> dict | None:
    outs = hist.get("outputs", {}) or {}
    for nid, o in outs.items():
        for key in ("gifs", "videos", "video"):
            items = o.get(key) or []
            for it in items:
                if isinstance(it, dict) and it.get("filename"):
                    sub = it.get("subfolder", "") or ""
                    rel = os.path.join(sub, it["filename"]) if sub else it["filename"]
                    path = os.path.normpath(os.path.join(cfg["comfy_output_dir"], rel)) if it.get("type", "output") == "output" else None
                    return {"node": nid, "filename": it["filename"], "subfolder": sub, "rel": rel.replace("\\", "/"),
                            "path": path, "format": it.get("format")}
    # 本体の SaveVideo は "images" キーに入れて返すことがある。拡張子で拾う
    for nid, o in outs.items():
        for it in (o.get("images") or []):
            fn = it.get("filename", "") if isinstance(it, dict) else ""
            if fn.lower().endswith((".mp4", ".webm", ".mkv", ".mov", ".m4v")):
                sub = it.get("subfolder", "") or ""
                rel = os.path.join(sub, fn) if sub else fn
                path = os.path.normpath(os.path.join(cfg["comfy_output_dir"], rel)) if it.get("type", "output") == "output" else None
                return {"node": nid, "filename": fn, "subfolder": sub, "rel": rel.replace("\\", "/"),
                        "path": path, "format": it.get("format")}
    return None


def run_job(cfg: dict, job: Job, inspect_fn=None, on_finish=None):
    """スレッド本体。"""
    JOBS[job.id] = job
    p = job.params
    try:
        if not comfy_up(cfg):
            raise RuntimeError("ComfyUI に届きません（%s）。起動してください" % cfg["comfy_url"])
        q = queue_state(cfg)
        if q["running"] or q["pending"]:
            raise RuntimeError("ComfyUI のキューに他のジョブがあります（実行中 %d / 待機 %d）。終わるまで投入しません" % (q["running"], q["pending"]))
        # GPU 切替: LM Studio を降ろす
        job.state = "unloading"
        llm_was_unloaded = False
        if llm._backend(cfg) == "lmstudio" and llm.loaded_models(cfg):
            job.add("LM Studio のモデルを降ろす（ComfyUI に GPU を渡す）", "now")
            t0 = time.time(); llm.unload_all(cfg); job.add("降ろした %.1fs" % (time.time() - t0), "ok")
            llm_was_unloaded = True
        else:
            job.add("LM Studio: 載っていない（切替不要）", "")
        # VRAM の空け方を決める。
        #   share    … /free で全部降ろし purge も挟む（実測: 前回の UNET+VAE が残ったままサンプラーに入ると 10 倍以上遅い）
        #   resident … 何もしない。UNET を載せたままにして読み直し（15〜250秒）を省く
        # ⚠ 解放と purge は必ず組で切り替える。purge2 は purge_models=True なので、
        #    /free だけ止めても各ジョブの終わりに UNET が降ろされ、常駐にはならない
        g = cfg.get("gen") or {}
        vals = workflow_values(cfg)["values"]
        vram_mode, why = decide_vram_mode(cfg, vals, llm_was_unloaded)
        job.vram_mode = vram_mode
        job.add("VRAM: %s — %s" % (vram_mode, why), "")
        purge = None
        if vram_mode == "share":
            if g.get("free_vram_before", True):
                fr = free_vram(cfg, unload_models=True, free_memory=bool(g.get("free_cache_before", False)))
                job.add("ComfyUI の常駐モデルを降ろす (/free) → %s" % ("ok" if fr.get("ok") else fr.get("error")), "" if fr.get("ok") else "warn")
            purge = g.get("purge_node") or None
            if purge and not node_available(cfg, purge):
                job.add("VRAM 解放ノード %s が無いので挿入しない（/free のみ）" % purge, "warn"); purge = None
        # 足りないノードの確認（他の PC で動かしたとき、原因の分かる止まり方をさせる）
        if p.get("videos") and not node_available(cfg, "VHS_LoadVideo"):
            raise RuntimeError("参照動画を使うには ComfyUI-VideoHelperSuite（VHS_LoadVideo）が要ります。"
                               "導入するか、参照動画の選択を外して参照画像だけで生成してください")
        out_node = output_node(cfg)
        if out_node != "VHS_VideoCombine":
            job.add("VHS_VideoCombine が無いので本体の CreateVideo + SaveVideo で書き出す", "warn")
        # 組み立て
        job.graph = build_prompt(cfg, p["prompt"], p.get("images", []), p.get("videos", []),
                                 p["width"], p["height"], p["length"], p["seed"], p["filename_prefix"],
                                 values=vals, purge_node=purge, out_node=out_node)
        job.add("API prompt 組み立て: %dx%d / %df / seed %s / 画像%d 動画%d / %s %s %dstep" % (
            p["width"], p["height"], p["length"], p["seed"], len(p.get("images", [])), len(p.get("videos", [])),
            vals["sampler"], vals["scheduler"], int(vals["steps"])), "")
        job.save()

        def do_submit():
            if job.prompt_id:
                return
            job.prompt_id = submit(cfg, job.graph, job.client_id)
            job.state = "submitted"; job.started = time.time()
            job.add("投入 prompt_id=%s" % job.prompt_id, "ok")
            job.save()

        outcome = {"res": None}

        def on_done(res):
            outcome["res"] = res
        # WS が開けたら接続後に投入（イベント取りこぼし防止）。開けなければ投入してポーリング
        ok = _ws_listen(cfg, job, on_done, after_connect=do_submit)
        if not ok:
            do_submit()
            _poll_until_done(cfg, job, on_done)
        res = outcome["res"]
        if res in ("cancel", "error"):
            # 途中で落ちた・止めた場合、VRAM に何が載っているか分からない。次は必ず share から始める
            _LAST_RUN.update({"sig": None, "ok": False})
        if res == "cancel":
            job.state = "cancelled"; job.finished = time.time(); job.add("中止", "warn"); job.save()
            return
        if res == "error":
            job.state = "error"; job.finished = time.time(); job.save()
            return
        # 完了: history から出力を取る（書き込み完了まで少し待つ）
        hist = None
        for _ in range(30):
            hist = history_item(cfg, job.prompt_id)
            if hist and hist.get("outputs"):
                break
            time.sleep(1.0)
        if not hist:
            raise RuntimeError("完了したが /history に出てこない")
        if hist.get("status", {}).get("status_str") == "error":
            job.error = job.error or "ComfyUI の実行エラー"; job.state = "error"; job.finished = time.time(); job.save(); return
        vid = _find_video(cfg, hist)
        if not vid or not vid.get("path"):
            raise RuntimeError("出力に動画が見つからない: %s" % json.dumps(hist.get("outputs", {}), ensure_ascii=False)[:300])
        # VHS は <name>.mp4 → <name>-audio.mp4 の順に書く。音声つきを優先
        path = vid["path"]
        for _ in range(20):
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                break
            time.sleep(0.5)
        gen_sec = round(time.time() - job.started, 1)
        # ここまで来たら、このモデル構成が VRAM に載った状態で正常に終わっている。
        # 次のジョブが同じ構成なら vram_mode="auto" が resident を選べる
        _LAST_RUN.update({"sig": model_sig(vals), "ok": True})
        job.add("生成完了 %s（%.1f分）" % (vid["filename"], gen_sec / 60), "ok")
        job.result = {"video": vid, "gen_seconds": gen_sec}
        job.state = "inspecting"
        if inspect_fn:
            job.add("結果を検査中（ffmpeg）…", "now")
            try:
                job.result["inspect"] = inspect_fn(path, os.path.join(JOBS_DIR, job.id))
                ins = job.result["inspect"]
                job.add("検査: %sf / %ss / %s Mbps / 音量 mean %s dB / 差分 %s" % (
                    ins.get("frames"), ins.get("duration"), ins.get("mbps"), (ins.get("audio") or {}).get("mean_db"), ins.get("frame_diff")), "ok")
            except Exception as e:
                job.add("検査に失敗: %r" % e, "warn"); job.result["inspect"] = {"error": repr(e)}
        job.state = "done"; job.finished = time.time(); job.save()
    except Exception as e:
        job.error = str(e)[:1000]; job.state = "error"; job.finished = time.time()
        job.add("失敗: " + job.error, "bad"); job.save()
    finally:
        if on_finish:
            try:
                on_finish(job)
            except Exception:
                pass


def start_job(cfg: dict, params: dict, inspect_fn=None, on_finish=None) -> Job:
    job = Job(params)
    JOBS[job.id] = job
    th = threading.Thread(target=run_job, args=(cfg, job, inspect_fn, on_finish), daemon=True, name="h3job-" + job.id)
    th.start()
    return job


def active_job() -> Job | None:
    for j in JOBS.values():
        if j.state in ("queued", "unloading", "submitted", "running", "inspecting"):
            return j
    return None


def cancel_job(cfg: dict, job: Job) -> dict:
    """中止を要求する。**ComfyUI がこのジョブを知らなくなっていたら、その場で終了させる。**

    状態を進めるのは実行スレッドだけなので、ComfyUI が落ちた・再起動した後は
    誰も `running` を終わらせられず、`active_job()` が塞がって次を投入できなくなる
    （2026-08-25 に実際に起きた。アプリの再起動でしか直らなかった）。
    """
    job._cancel = True
    known = None                      # None = ComfyUI に問い合わせられなかった
    if job.prompt_id:
        try:
            q = queue_state(cfg)
            known = job.prompt_id in q["running_ids"] or job.prompt_id in q.get("pending_ids", [])
            cancel_prompt(cfg, job.prompt_id, running=job.prompt_id in q["running_ids"])
        except Exception as e:
            return {"ok": False, "error": repr(e)}
    job.add("中止を要求", "warn")

    # ComfyUI 側に居ないなら、実行スレッドはもう状態を進められない。ここで畳む。
    # 投入前（prompt_id 無し）で止めた場合も同じ。
    if known is False or not job.prompt_id:
        if job.state in ("queued", "unloading", "submitted", "running", "inspecting"):
            job.state = "cancelled"; job.finished = time.time()
            job.add("ComfyUI 側にこのジョブが無いため、中止として確定した", "warn")
            job.save()
    return {"ok": True, "state": job.state}


def load_saved_jobs(limit=30) -> list[dict]:
    out = []
    if not os.path.isdir(JOBS_DIR):
        return out
    for d in sorted(os.listdir(JOBS_DIR), reverse=True)[:limit]:
        p = os.path.join(JOBS_DIR, d, "job.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    out.append(json.load(f))
            except Exception:
                pass
    return out
