# -*- coding: utf-8 -*-
"""
gpu.py — LM Studio ⇄ ComfyUI の GPU 排他（設計書 §7）。

  state(cfg)            どちらが GPU を持っているか（nvidia-smi / ComfyUI /system_stats / lms ps）
  release_to_comfy(cfg) 生成前: LM Studio のモデルを降ろす
  release_to_llm(cfg)   プロンプト生成前に ComfyUI の常駐モデルを降ろす（/free）。実測: 抱えたままだと LLM が 15s→35s
  prepare_for_llm(cfg)  ↑を自動でやってから LM Studio を載せ、**GPU に全部載ったかを検算**する（半載りは lms ps では見えない）

ComfyUI はアプリが起動しない（別プロセス前提）。LM Studio は lms load/unload を叩く。
"""
from __future__ import annotations
import json, subprocess, urllib.request
from . import llm, comfy


def nvidia() -> dict | None:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,power.draw",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            used, total, util, pw = [x.strip() for x in r.stdout.strip().split(",")[:4]]
            return {"used_mb": int(float(used)), "total_mb": int(float(total)), "util": int(float(util)), "power_w": float(pw)}
    except Exception:
        pass
    return None


def comfy_state(cfg: dict) -> dict:
    try:
        with urllib.request.urlopen(cfg["comfy_url"].rstrip("/") + "/system_stats", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        dev = (d.get("devices") or [{}])[0]
        argv = (d.get("system") or {}).get("argv") or []
        out = {"up": True, "version": (d.get("system") or {}).get("comfyui_version"),
               # 実測: 動的 VRAM ローダーが空き≥モデルサイズを見ると 20GB の UNET を全部載せて活性化メモリと出し入れ → 10 倍遅い。
               # --reserve-vram N で空きを少なく見せるのが筋の良い対処（推奨・未検証）
               "reserve_vram": any(str(a).startswith("--reserve-vram") for a in argv),
               "vram_free_gb": round(dev.get("vram_free", 0) / 1e9, 1),
               "vram_total_gb": round(dev.get("vram_total", 0) / 1e9, 1),
               "holding_gb": round((dev.get("vram_total", 0) - dev.get("vram_free", 0)) / 1e9, 1)}
        q = comfy.queue_state(cfg)
        out["running"] = q["running"]; out["pending"] = q["pending"]
        return out
    except Exception:
        return {"up": False}


def state(cfg: dict) -> dict:
    """/api/gpu の本体。"""
    out = {"nvidia": nvidia(), "comfy": comfy_state(cfg), "lmstudio": llm.loaded_detail(cfg), "note": "", "holder": "free"}
    c = out["comfy"]
    if out["lmstudio"]:
        out["holder"] = "llm"
    if c.get("up") and (c.get("running") or 0) > 0:
        out["holder"] = "comfy"
    if c.get("up") and c.get("holding_gb", 0) > 6 and out["lmstudio"]:
        out["note"] = ("ComfyUI が VRAM を %.1fGB 保持したまま LLM も載っています。プロンプト生成が 2倍程度遅くなります（実測）。"
                       "生成に入る前に ComfyUI を使わないなら一度 VRAM を空けてください。" % c["holding_gb"])
    # LLM が GPU に載りきっているかの検算（半載りは lms ps では見えない）
    out["residency"] = residency(cfg)
    if out["residency"].get("warning"):
        out["note"] = out["residency"]["warning"] + ("　" + out["note"] if out["note"] else "")
    job = comfy.active_job()
    out["job"] = {"id": job.id, "state": job.state, "progress": job.progress} if job else None
    return out


def release_to_comfy(cfg: dict) -> dict:
    """生成前。LM Studio を降ろす。"""
    if llm._backend(cfg) != "lmstudio":
        return {"skipped": True, "reason": "LLM はクラウド"}
    if not llm.loaded_models(cfg):
        return {"ok": True, "already": True}
    return llm.unload_all(cfg)


def release_to_llm(cfg: dict) -> dict:
    """プロンプト生成前。ComfyUI に常駐モデルを降ろさせる（生成中なら何もしない）。"""
    c = comfy_state(cfg)
    if not c.get("up"):
        return {"skipped": True, "reason": "ComfyUI が起動していない"}
    if c.get("running"):
        return {"skipped": True, "reason": "ComfyUI が生成中"}
    before = c.get("holding_gb")
    r = comfy.free_vram(cfg, unload_models=True, free_memory=True)
    after = comfy_state(cfg).get("holding_gb")
    r.update({"before_gb": before, "after_gb": after})
    return r


# ---------------- LLM を載せる前の準備（2026-08-23 夜の実測を受けて追加） ----------------
# 実測: ComfyUI が 6GB 抱えた状態で LM Studio を載せると、**GPU には 7GB しか載らず残りは CPU**。
# `lms ps` の SIZE は 17.16GB と出るので見た目では分からない。プロンプト生成が 45秒 → 218秒。
# 生成（ComfyUI）→ プロンプト生成（LLM）と往復する通常運用で踏む。`gen.vram_mode` を resident にすると必ず踏む。

# 直近の「載せたときに GPU がどれだけ増えたか」。/api/gpu で見せるために覚えておく。
# プロセスを再起動したら消えてよい（分からなければ「未計測」と出す）。
_LAST_RESIDENCY: dict = {"checked": False}


def residency(cfg: dict) -> dict:
    """直近に計測した「LLM が GPU に載りきっているか」を返す。

    **合計 VRAM からの推定は使わない。** ComfyUI も VRAM を持つので、合計値では
    「モデルより多いから全部載っている」とも「少ないから半載り」とも言えない
    （実測: ComfyUI 5.2GB 保持中に `--gpu 0.4` で半載りさせたら、合計 23.3GB > モデル 16.3GB で
    誤って「載りきっている」と判定した）。
    唯一正確なのは **ロードの前後で GPU 使用量がいくら増えたか**なので、`prepare_for_llm` が
    自分で載せたときにだけ測り、ここに残す。載せていないときは「未計測」と正直に返す。
    """
    if not llm.loaded_detail(cfg):
        return {"checked": False, "reason": "LLM が載っていない"}
    return dict(_LAST_RESIDENCY)


def _measure_load(cfg: dict, model, progress):
    """LM Studio を載せ、GPU 使用量の増分から「載りきったか」を測る。"""
    before = (nvidia() or {}).get("used_mb")
    out = llm.ensure_loaded(cfg, model, progress=progress)
    after = (nvidia() or {}).get("used_mb")
    models = llm.loaded_detail(cfg)
    size_mb = int((models[0].get("sizeBytes") or 0) / 1024 / 1024) if models else 0
    if before is None or after is None or not size_mb:
        _LAST_RESIDENCY.clear(); _LAST_RESIDENCY.update({"checked": False, "reason": "nvidia-smi かモデルサイズが取れない"})
        return out
    gained = after - before
    ok = gained >= size_mb * 0.85          # KV キャッシュぶん増える方向なので、下振れだけを見る
    res = {"checked": True, "model_mb": size_mb, "gpu_gained_mb": gained, "resident": ok}
    if not ok:
        res["warning"] = ("LM Studio が %.1fGB のモデルを載せたのに、GPU の使用量は %.1fGB しか増えていません。"
                          "**一部が CPU に載っています**（`lms ps` の SIZE は満額を表示するので気づけません）。"
                          "プロンプト生成が数倍遅くなります。" % (size_mb / 1024, gained / 1024))
    _LAST_RESIDENCY.clear(); _LAST_RESIDENCY.update(res)
    return out


def _already_good(cfg: dict, model: str | None) -> bool:
    """要求どおりの設定で載っていて、かつ GPU に載りきっていると**計測済み**か。真なら何もしなくてよい。
    未計測のときは False を返す（半載りのまま素通りさせないため）。"""
    model = model or cfg.get("lmstudio_model")
    if not model:
        return False
    lm = cfg.get("lmstudio_load", {})
    want_ctx = int(lm.get("context_length", 16384)); want_par = int(lm.get("parallel", 1))
    for m in llm.loaded_detail(cfg):
        if not m["identifier"].startswith(model):
            continue
        if m.get("contextLength") not in (None, want_ctx) or m.get("parallel") not in (None, want_par):
            return False
        r = residency(cfg)
        return bool(r.get("checked") and r.get("resident"))
    return False


def prepare_for_llm(cfg: dict, model: str | None = None, progress=None) -> dict:
    """プロンプト生成の直前。必要なときだけ ComfyUI の VRAM を空けてから LM Studio を載せ、載りきったかを検算する。

    **順番が重要: 空けてから載せる。** 逆だと半分が CPU に載り、あとから空けても移動しない（載せ直しが要る）。
    逆に、**既に正しく載っているなら ComfyUI を空けない**。空けると次の生成で UNET 読み直し（15〜250秒）が発生するため、
    無用な解放はそれ自体が損になる（設計書 §9a の「最悪手は全部解放 → 広い空きへ再読込」）。
    """
    out: dict = {}
    if _already_good(cfg, model):
        out["load"] = llm.ensure_loaded(cfg, model, progress=progress)   # already:True で即返る
        out["residency"] = residency(cfg)
        return out

    g = cfg.get("gen") or {}
    if g.get("free_comfy_before_llm", True):
        c = comfy_state(cfg)
        thr = float(g.get("free_comfy_min_gb", 2))
        # 判定に `vram_free_gb` は使えない。あれは ComfyUI 自身の torch 会計で、デバイス全体の空きではない
        # （実測: ComfyUI が 20.5GB 空きと報告している裏で、nvidia-smi の実際の空きは 4.1GB だった）。
        # ComfyUI 自身の保持量 `holding_gb` を見て、抱えているなら降ろす。
        # ここに来るのは「LLM を実際に載せ直す」ときだけなので、無用な解放にはならない。
        # 代償は次の生成での UNET 読み直し（15〜250秒・1回きり）で、半載りの遅さ（数倍・載せ直すまで永続）より軽い。
        if c.get("up") and not c.get("running") and not c.get("pending") and c.get("holding_gb", 0) >= thr:
            if progress:
                progress("ComfyUI が VRAM を %.1fGB 抱えているので先に降ろします（後から空けても CPU 側の重みは GPU に移らない）"
                         % c.get("holding_gb", 0))
            out["freed"] = release_to_llm(cfg)
    out["load"] = _measure_load(cfg, model, progress)
    out["residency"] = residency(cfg)
    # それでも半載りなら、一度だけ載せ直す（空けただけでは CPU 側の重みは GPU に移らない）
    if out["residency"].get("checked") and not out["residency"]["resident"]:
        if progress:
            progress("LLM が一部 CPU に載っているので載せ直します")
        llm.unload_all(cfg)
        out["load"] = _measure_load(cfg, model, progress)
        out["residency"] = residency(cfg)
        out["reloaded_for_residency"] = True
    return out
