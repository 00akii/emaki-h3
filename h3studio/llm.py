# -*- coding: utf-8 -*-
"""
llm.py — LLM バックエンド。lmstudio（ローカル）と openai_compat（クラウド）を同じ関数で叩く。

  chat(cfg, messages, ...)      → (text, seconds, usage)
  list_models(cfg)              → 選べるモデル一覧。手元の実測（sweep.json）のラベル付き
  ensure_loaded(cfg, model)     → LM Studio のときだけ lms load（検証済み設定で）
  unload_all(cfg)               → LM Studio のモデルを降ろす（ComfyUI に GPU を渡す前）

思考を切るのは reasoning_effort="none"。chat_template_kwargs だけでは効かない（実測）。
"""
from __future__ import annotations
import json, os, subprocess, time, urllib.request, urllib.error
from . import config

SWEEP_PATH_CANDIDATES = [
    os.path.join(config.VENDOR_DIR, "sweep.json"),
    os.path.join(config.APP_DIR, "..", "ローカルLLM", "sweep.json"),
]


USAGE_PATH = os.path.join(config.APP_DIR, "usage.json")


def _backend(cfg: dict) -> str:
    return (cfg.get("llm") or {}).get("backend", "lmstudio")


def is_cloud(cfg: dict) -> bool:
    """課金が発生しうるバックエンドか。画面の常時警告と、生成前の確認に使う。"""
    return _backend(cfg) == "openai_compat"


def _load_usage() -> dict:
    if os.path.isfile(USAGE_PATH):
        try:
            with open(USAGE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "by_model": {}, "since": time.strftime("%Y-%m-%d")}


def _record_usage(model: str, usage: dict):
    u = _load_usage()
    pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    u["calls"] += 1
    u["prompt_tokens"] += pt
    u["completion_tokens"] += ct
    bm = u["by_model"].setdefault(model or "?", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    bm["calls"] += 1; bm["prompt_tokens"] += pt; bm["completion_tokens"] += ct
    with open(USAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(u, f, ensure_ascii=False, indent=1)


def usage_summary(cfg: dict) -> dict:
    """画面のヘッダーに出す。料金は単価表（config.llm.pricing）があれば概算、無ければトークン数だけ。"""
    u = _load_usage()
    pricing = ((cfg.get("llm") or {}).get("pricing") or {})  # {model: {"in": $/Mtok, "out": $/Mtok}}
    est = 0.0; priced = False
    for m, bm in u.get("by_model", {}).items():
        pr = pricing.get(m)
        if pr:
            priced = True
            est += bm["prompt_tokens"] / 1e6 * float(pr.get("in", 0)) + bm["completion_tokens"] / 1e6 * float(pr.get("out", 0))
    return {"cloud": is_cloud(cfg), "calls": u["calls"], "prompt_tokens": u["prompt_tokens"],
            "completion_tokens": u["completion_tokens"], "since": u.get("since"),
            "estimated_usd": round(est, 4) if priced else None}


def _endpoint(cfg: dict):
    """(base_url, headers, model) を返す"""
    if _backend(cfg) == "openai_compat":
        oc = (cfg.get("llm") or {}).get("openai_compat", {})
        key = os.environ.get(oc.get("api_key_env") or "H3STUDIO_LLM_KEY", "")
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = "Bearer " + key
        return (oc.get("base_url") or "").rstrip("/"), h, oc.get("model", "")
    return cfg["lmstudio_url"].rstrip("/") + "/v1", {"Content-Type": "application/json"}, cfg.get("lmstudio_model", "")


def chat(cfg: dict, messages: list[dict], temperature=0.35, max_tokens=3000,
         seed=None, think=False, timeout=900, model=None):
    base, headers, default_model = _endpoint(cfg)
    payload = {
        "model": model or default_model,
        "messages": messages,
        "temperature": temperature,
        "top_p": 0.9,
        "max_tokens": max_tokens,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "stream": False,
    }
    if not think:
        # 実測: reasoning_effort が効く本体。chat_template_kwargs は単独では無効
        payload["reasoning_effort"] = "none"
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if seed is not None and _backend(cfg) == "lmstudio":
        payload["seed"] = seed
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"), headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            pass
        raise RuntimeError("LLM HTTP %s from %s: %s" % (e.code, base, body or e.reason))
    msg = data["choices"][0]["message"]
    usage = data.get("usage", {}) or {}
    if is_cloud(cfg):
        try:
            _record_usage(payload["model"], usage)
        except Exception:
            pass
    return (msg.get("content") or ""), time.time() - t0, usage


def chat_vision(cfg: dict, system: str, text: str, image_paths: list[str],
                temperature=0.2, max_tokens=400, model=None):
    """画像つき。キャラ表の記述下書きに使う。"""
    import base64
    content = [{"type": "text", "text": text}]
    for p in image_paths:
        with open(p, "rb") as f:
            b = base64.b64encode(f.read()).decode()
        ext = os.path.splitext(p)[1].lower().lstrip(".") or "png"
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        content.append({"type": "image_url", "image_url": {"url": "data:image/%s;base64,%s" % (mime, b)}})
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    return chat(cfg, msgs, temperature=temperature, max_tokens=max_tokens, model=model)


# ---------------- モデル一覧と実測ラベル ----------------

def _load_sweep() -> dict:
    for p in SWEEP_PATH_CANDIDATES:
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def measured_labels() -> dict:
    """sweep.json → {model_id: {final, first, n, avg_tries, avg_seconds, label}}"""
    out = {}
    for m, r in _load_sweep().items():
        if not isinstance(r, dict) or r.get("load_failed") or not r.get("n"):
            continue
        n = r["n"]
        out[m] = {
            "final": r.get("final_pass", 0), "first": r.get("first_pass", 0), "n": n,
            "avg_tries": r.get("avg_tries", 0), "avg_seconds": r.get("avg_seconds", 0),
            "label": "検証済み %d/%d（うち最初の1回で合格 %d/%d・平均 %.1f 回）" % (r.get("final_pass", 0), n, r.get("first_pass", 0), n, r.get("avg_tries", 0)),
        }
    return out


RECOMMENDED = "qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved@q4_k_s"


def list_models(cfg: dict) -> list[dict]:
    """
    選べるモデル。lmstudio なら /v1/models の一覧、openai_compat なら設定値（＋ /models が取れれば一覧）。
    各項目: {id, backend, measured:{...}|None, recommended:bool, loaded:bool}
    """
    base, headers, _ = _endpoint(cfg)
    ids = []
    try:
        req = urllib.request.Request(base + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    except Exception:
        pass
    loaded = set()
    if _backend(cfg) == "lmstudio":
        loaded = set(loaded_models(cfg))
        ids = [i for i in ids if not i.startswith("text-embedding")]
    meas = measured_labels()
    out = []
    for i in ids:
        out.append({"id": i, "backend": _backend(cfg), "measured": meas.get(i),
                    "recommended": i == RECOMMENDED, "loaded": i in loaded})
    # 実測あり → 推奨 → 名前 の順
    out.sort(key=lambda m: (0 if m["recommended"] else 1,
                            -(m["measured"]["final"] / max(m["measured"]["n"], 1)) if m["measured"] else 1,
                            m["id"]))
    return out


# ---------------- LM Studio の載せ降ろし ----------------

def loaded_detail(cfg: dict) -> list[dict]:
    """lms ps --json → [{identifier, contextLength, parallel, status, sizeBytes}]"""
    try:
        r = subprocess.run([cfg.get("lms_cli") or "lms", "ps", "--json"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip().startswith(("[", "{")):
            data = json.loads(r.stdout)
            items = data if isinstance(data, list) else data.get("models", [])
            return [{"identifier": m.get("identifier") or m.get("modelKey") or "",
                     "contextLength": m.get("contextLength"), "parallel": m.get("parallel"),
                     "status": m.get("status"), "sizeBytes": m.get("sizeBytes")} for m in items]
    except Exception:
        pass
    # --json が無い版: テキストをなめる（設定の比較はできない）
    try:
        r = subprocess.run([cfg.get("lms_cli") or "lms", "ps"], capture_output=True, text=True, timeout=30)
        return [{"identifier": ln.split()[0]} for ln in r.stdout.splitlines()[1:] if ln.strip()]
    except Exception:
        return []


def loaded_models(cfg: dict) -> list[str]:
    return [m["identifier"] for m in loaded_detail(cfg)]


def _per_model_config_path(model_id: str) -> str | None:
    """LM Studio がモデルごとに保存する既定ロード設定（GUI で触ると出来る）。
    実測: ここに contextLength があると `lms load --context-length` より優先される。"""
    base = os.path.join(os.path.expanduser("~"), ".lmstudio", ".internal", "user-concrete-model-default-config")
    if not os.path.isdir(base):
        return None
    # 正確な対応は `lms ps --json` の path（"<publisher>/<repo>/<file>.gguf"）から取る。
    # 載っていないときだけ、最後の手段として名前の一致で探す（量子化の違いまで含めて一致させる）。
    try:
        r = subprocess.run(["lms", "ps", "--json"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip().startswith("["):
            for m in json.loads(r.stdout):
                if (m.get("identifier") or "") == model_id and m.get("path"):
                    cand = os.path.join(base, *m["path"].split("/")) + ".json"
                    if os.path.isfile(cand):
                        return cand
    except Exception:
        pass
    name, _, quant = model_id.partition("@")
    nkey = name.lower().replace("-", "").replace("_", "")
    qkey = quant.lower().replace("_", "")
    best = None
    for root, _, files in os.walk(base):
        for f in files:
            if not f.lower().endswith(".gguf.json"):
                continue
            fl = f.lower().replace("-", "").replace("_", "")
            if nkey[:28] in fl and (not qkey or qkey in fl):
                cand = os.path.join(root, f)
                if best is None or len(f) > len(os.path.basename(best)):
                    best = cand
    return best


def pinned_context(model_id: str):
    """(path, value) — 既定設定ファイルに contextLength があれば返す。"""
    p = _per_model_config_path(model_id)
    if not p:
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        for fld in (d.get("load") or {}).get("fields", []):
            if fld.get("key") == "llm.load.contextLength":
                return p, fld.get("value")
    except Exception:
        pass
    return p, None


def fix_pinned_context(model_id: str, value: int) -> dict:
    """既定設定ファイルの contextLength を value に書き換える（バックアップを残す）。"""
    p, cur = pinned_context(model_id)
    if not p:
        return {"ok": False, "error": "既定設定ファイルが見つからない"}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        bak = p + ".bak-h3studio"
        if not os.path.exists(bak):
            with open(bak, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
        done = False
        for fld in (d.get("load") or {}).get("fields", []):
            if fld.get("key") == "llm.load.contextLength":
                fld["value"] = int(value); done = True
        if not done:
            d.setdefault("load", {}).setdefault("fields", []).append({"key": "llm.load.contextLength", "value": int(value)})
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        return {"ok": True, "path": p, "before": cur, "after": int(value), "backup": bak}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def ensure_loaded(cfg: dict, model: str | None = None, progress=None) -> dict:
    """LM Studio のときだけ。検証済み設定（ctx16384 / parallel1 / MTP）で load する。"""
    if _backend(cfg) != "lmstudio":
        return {"skipped": True, "reason": "backend is not lmstudio"}
    model = model or cfg.get("lmstudio_model")
    if not model:
        return {"ok": False, "error": "モデルが未選択"}
    lm = cfg.get("lmstudio_load", {})
    want_ctx = int(lm.get("context_length", 16384)); want_par = int(lm.get("parallel", 1))
    for m in loaded_detail(cfg):
        if not m["identifier"].startswith(model):
            continue
        same = (m.get("contextLength") in (None, want_ctx)) and (m.get("parallel") in (None, want_par))
        if same:
            return {"ok": True, "already": True, "model": model, "context": m.get("contextLength"),
                    "effective_context": m.get("contextLength")}
        # 実測の罠: 既定ロード(ctx 64000 × parallel 4)だと VRAM が溢れて 10倍遅い。設定が違えば載せ直す
        if progress:
            progress("LM Studio: 設定が違う（ctx %s / parallel %s）ので載せ直します" % (m.get("contextLength"), m.get("parallel")))
        break
    cmd = [cfg.get("lms_cli") or "lms", "unload", "--all"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    cmd = [cfg.get("lms_cli") or "lms", "load", model,
           "--context-length", str(lm.get("context_length", 16384)),
           "--gpu", "max", "--parallel", str(lm.get("parallel", 1)),
           "--ttl", str(lm.get("ttl", 3600)), "-y"]
    if lm.get("speculative_mtp", True):
        cmd.append("--speculative-draft-mtp")
    if progress:
        progress("LM Studio: %s をロード中…" % model)
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    out = {"ok": r.returncode == 0, "model": model, "seconds": round(time.time() - t0, 1),
           "context": want_ctx, "parallel": want_par, "reloaded": True,
           "error": (r.stderr or r.stdout)[-400:] if r.returncode != 0 else ""}
    # 実測の罠: モデル個別の既定設定（GUI で保存される）に contextLength があると
    # --context-length より優先され、黙って 10 倍遅くなる。実効値を必ず確認する。
    eff = next((m.get("contextLength") for m in loaded_detail(cfg) if m["identifier"].startswith(model)), None)
    out["effective_context"] = eff
    if eff is not None and eff != want_ctx:
        path, pinned = pinned_context(model)
        out["warning"] = ("要求した context %d ではなく %s で載っています。LM Studio のモデル既定設定（%s）の "
                          "contextLength=%s が優先されています。「既定設定を直す」で %d に書き換えられます。"
                          % (want_ctx, eff, path or "不明", pinned, want_ctx))
        out["pinned_path"] = path; out["pinned_value"] = pinned
        if lm.get("auto_fix_pinned_context"):
            fx = fix_pinned_context(model, want_ctx)
            out["auto_fix"] = fx
            if fx.get("ok"):
                subprocess.run([cfg.get("lms_cli") or "lms", "unload", "--all"], capture_output=True, text=True, timeout=120)
                r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
                out["effective_context"] = next((m.get("contextLength") for m in loaded_detail(cfg) if m["identifier"].startswith(model)), None)
                out["reloaded_after_fix"] = r2.returncode == 0
    return out


def unload_all(cfg: dict) -> dict:
    if _backend(cfg) != "lmstudio":
        return {"skipped": True}
    r = subprocess.run([cfg.get("lms_cli") or "lms", "unload", "--all"], capture_output=True, text=True, timeout=120)
    return {"ok": r.returncode == 0}
