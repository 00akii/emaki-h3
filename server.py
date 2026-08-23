# -*- coding: utf-8 -*-
"""
H3 Studio — FastAPI サーバー。

  python server.py                → config.json の port（既定 8765）
  python server.py --port 8799    → ポートを上書き（別フォルダで試すときなど）

  設定・プロジェクト・参照素材・切り抜き（SAM3）・モデル選択・
  ブリーフ → プロンプト → 機械検査 → プロンプト.txt 書き出し →
  ComfyUI へ投入 → 進捗 → 結果検査（ffmpeg）→ 採用・アーカイブ・Eagle 送信。

  環境依存は config.json のみ。コードにパスは書かない（h3studio/config.py）。
"""
from __future__ import annotations
import io, json, os, re, sys, time, glob, mimetypes
from typing import Optional

from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
from h3studio import config, project, llm, brief as briefmod, promptgen, comfy, gpu, cut, eagle, textcheck  # noqa: E402
from h3studio import inspect as inspectmod  # noqa: E402

app = FastAPI(title="H3 Studio", version="0.1.0")
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")

CFG = config.load()


def cfg() -> dict:
    return CFG


# ---------------- 画面 ----------------

@app.get("/", response_class=HTMLResponse)
def index():
    """index.html を返す。**app.js / style.css の URL にファイルの更新時刻を足す。**

    足さないと、コードを直してサーバーを再起動しても**ブラウザが古い app.js を握ったまま**になる
    （実測: 表示ラベルの修正が反映されず、`location.reload(true)` でも直らなかった。
    強制再読込の引数は今のブラウザでは無視される）。開発中も配布後も同じ問題を踏むので、URL 側で解決する。
    """
    sdir = os.path.join(APP_DIR, "static")
    with io.open(os.path.join(sdir, "index.html"), encoding="utf-8") as f:
        html = f.read()
    for name in ("app.js", "style.css"):
        try:
            v = int(os.path.getmtime(os.path.join(sdir, name)))
        except OSError:
            continue
        html = html.replace("/static/%s" % name, "/static/%s?v=%d" % (name, v))
    return html


# ---------------- 設定 ----------------

@app.get("/api/config")
def get_config():
    c = cfg()
    pub = {k: v for k, v in c.items() if not k.startswith("_")}
    return {"config": pub, "checks": config.check(c), "source": c.get("_source"),
            "llm_cloud": llm.is_cloud(c), "usage": llm.usage_summary(c)}


@app.put("/api/config")
def put_config(body: dict = Body(...)):
    global CFG
    allowed = set(config.DEFAULTS.keys())
    for k, v in body.items():
        if k in allowed:
            if isinstance(v, dict) and isinstance(CFG.get(k), dict):
                config._deep_update(CFG[k], v)
            else:
                CFG[k] = v
    config.save(CFG)
    CFG = config.load()
    return {"ok": True, "checks": config.check(CFG), "llm_cloud": llm.is_cloud(CFG)}


@app.get("/api/preflight")
def get_preflight(refresh: int = 0):
    """この ComfyUI に必要なノードが揃っているか。よその PC で動かすときの最初の確認。"""
    if refresh:
        comfy.clear_node_cache()
    return comfy.preflight(cfg())

# ---------------- プロジェクト ----------------

@app.get("/api/projects")
def projects():
    return {"projects": project.list_projects()}


@app.post("/api/projects")
def create_project(body: dict = Body(...)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name が空")
    return project.create(name)


@app.get("/api/projects/{name}")
def get_project(name: str):
    try:
        return project.load(name)
    except FileNotFoundError:
        raise HTTPException(404, "プロジェクトが無い: " + name)


@app.put("/api/projects/{name}")
def put_project(name: str, body: dict = Body(...)):
    body["name"] = name
    return project.save(body)


# ---------------- 参照素材 ----------------

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
VID_EXT = (".mp4", ".webm", ".mov")


def _listdir(d: str, exts):
    if not d or not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        if f.lower().endswith(exts):
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
                out.append({"name": f, "size": st.st_size, "mtime": int(st.st_mtime)})
            except OSError:
                pass
    return out


@app.get("/api/assets/images")
def assets_images(cut_only: bool = False):
    items = _listdir(cfg()["comfy_input_dir"], IMG_EXT)
    for it in items:
        it["cut"] = it["name"].lower().endswith("_cut.png") or "_cut" in it["name"].lower()
    if cut_only:
        items = [i for i in items if i["cut"]]
    # 新しい順
    items.sort(key=lambda x: -x["mtime"])
    return {"dir": cfg()["comfy_input_dir"], "items": items}


@app.get("/api/assets/videos")
def assets_videos():
    items = _listdir(cfg()["comfy_input_dir"], VID_EXT)
    items.sort(key=lambda x: -x["mtime"])
    return {"dir": cfg()["comfy_input_dir"], "items": items}


@app.get("/api/assets/raw")
def assets_raw():
    items = _listdir(cfg()["raw_dir"], IMG_EXT)
    items.sort(key=lambda x: -x["mtime"])
    return {"dir": cfg()["raw_dir"], "items": items}


def _safe_join(base: str, name: str) -> str:
    name = os.path.basename(name)
    p = os.path.abspath(os.path.join(base, name))
    if not p.startswith(os.path.abspath(base)):
        raise HTTPException(400, "不正なパス")
    if not os.path.isfile(p):
        raise HTTPException(404, name)
    return p


@app.get("/api/file/input/{name}")
def file_input(name: str):
    return FileResponse(_safe_join(cfg()["comfy_input_dir"], name))


@app.get("/api/file/raw/{name}")
def file_raw(name: str):
    return FileResponse(_safe_join(cfg()["raw_dir"], name))


@app.get("/api/file/output/{name}")
def file_output(name: str):
    return FileResponse(_safe_join(cfg()["comfy_output_dir"], name))


# ---------------- モデル ----------------

@app.get("/api/models")
def models():
    c = cfg()
    return {"backend": (c.get("llm") or {}).get("backend", "lmstudio"),
            "cloud": llm.is_cloud(c),
            "current": c.get("lmstudio_model") if not llm.is_cloud(c) else (c.get("llm", {}).get("openai_compat", {}).get("model")),
            "models": llm.list_models(c),
            "recommended": llm.RECOMMENDED}


@app.post("/api/models/select")
def select_model(body: dict = Body(...)):
    global CFG
    mid = (body.get("id") or "").strip()
    if not mid:
        raise HTTPException(400, "id が空")
    if llm.is_cloud(CFG):
        CFG.setdefault("llm", {}).setdefault("openai_compat", {})["model"] = mid
    else:
        CFG["lmstudio_model"] = mid
    config.save(CFG)
    CFG = config.load()
    return {"ok": True, "current": mid}


@app.post("/api/models/load")
def load_model(body: dict = Body(default={})):
    c = cfg()
    mid = body.get("id") or None
    prep = gpu.prepare_for_llm(c, mid)
    out = dict(prep["load"]); out["residency"] = prep.get("residency")
    if prep.get("freed"):
        out["freed_comfy"] = prep["freed"]
    if prep.get("residency", {}).get("warning") and not out.get("warning"):
        out["warning"] = prep["residency"]["warning"]
    return out


@app.post("/api/models/unload")
def unload_models():
    return llm.unload_all(cfg())


@app.post("/api/models/fix-context")
def fix_context(body: dict = Body(default={})):
    """LM Studio のモデル既定設定に固定された contextLength を、検証済みの値に書き換える（バックアップあり）。"""
    c = cfg()
    mid = body.get("id") or c.get("lmstudio_model")
    want = int((c.get("lmstudio_load") or {}).get("context_length", 16384))
    res = llm.fix_pinned_context(mid, want)
    if res.get("ok") and body.get("reload", True):
        llm.unload_all(c)
        res["reload"] = gpu.prepare_for_llm(c, mid)["load"]
    return res


@app.get("/api/gpu")
def gpu_state():
    """どちらが GPU を持っているか。ComfyUI が VRAM を抱えたままだと LLM が CPU にこぼれて遅くなる（実測 15s→35s）。"""
    return gpu.state(cfg())


@app.post("/api/gpu/free-comfy")
def gpu_free_comfy():
    """ComfyUI の常駐モデルを降ろして LM Studio に GPU を返す。生成中なら何もしない。"""
    return gpu.release_to_llm(cfg())


# ---------------- ブリーフ → プロンプト ----------------

class GenReq(BaseModel):
    project: str
    mode: str = "B"                      # A | B | C
    fields: dict = {}
    images: list[str] = []
    videos: list[str] = []
    duration: int = 8
    ratio: str = "16:9"
    seed: Optional[int] = None
    tries: int = 3
    model: Optional[str] = None
    confirm_cloud: bool = False          # クラウドのとき true が要る（課金の確認）


@app.post("/api/brief/build")
def brief_build(req: GenReq):
    proj = project.load(req.project)
    text = briefmod.build(req.mode, req.fields, proj, req.images, req.videos, req.duration, req.ratio)
    return {"brief": text, "h3_mode": "ref2va" if (req.images or req.videos) else "t2va",
            "motion_steps": briefmod.split_steps(req.fields.get("motion", "")),
            "framing_check": briefmod.check_framing(req.fields.get("framing", ""), req.fields.get("camera", "")),
            "text_check": textcheck.check(req.fields.get("text", ""))}


@app.post("/api/brief/steps")
def brief_steps(body: dict = Body(...)):
    """「動き」欄をどう段階に分けたか（画面のその場表示用）。LLM は使わない。"""
    t = body.get("text", "")
    return {"steps": briefmod.split_steps(t), "separator": briefmod.separator_used(t),
            "normalized": briefmod.normalize_steps(t)}


@app.post("/api/brief/expand")
def brief_expand(body: dict = Body(...)):
    """モードA の1行 → 4欄。クラウドのときは confirm_cloud が要る。"""
    c = cfg()
    if llm.is_cloud(c) and not body.get("confirm_cloud"):
        return JSONResponse({"need_confirm": True, "reason": "クラウド LLM を使うため料金がかかります"}, status_code=402)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text が空")
    msgs = briefmod.expand_prompt_for_mode_a(text)
    out, dt, usage = llm.chat(c, msgs, temperature=0.3, max_tokens=400, model=body.get("model"))
    m = re.search(r"\{.*\}", out, re.S)
    try:
        fields = json.loads(m.group(0)) if m else {}
    except Exception:
        fields = {}
    return {"fields": {k: str(fields.get(k, "")) for k in ("place", "motion", "framing", "camera", "dialogue")},
            "raw": out, "seconds": round(dt, 1)}


@app.post("/api/prompt/generate")
def prompt_generate(req: GenReq):
    c = cfg()
    if llm.is_cloud(c) and not req.confirm_cloud:
        return JSONResponse({"need_confirm": True, "reason": "クラウド LLM を使うため料金がかかります（1回あたり約8〜9k トークン）"}, status_code=402)
    proj = project.load(req.project)
    dur = max(4, min(int(req.duration), c["gen"]["max_duration"]))
    h3_mode = "ref2va" if (req.images or req.videos) else "t2va"
    text = briefmod.build(req.mode, req.fields, proj, req.images, req.videos, dur, req.ratio)
    # LM Studio なら載せる（載っていれば即返る）
    # ComfyUI の VRAM を空けてから LM Studio を載せる（逆順だと半分が CPU に載り、数倍遅くなる。gpu.prepare_for_llm 参照）
    prep = gpu.prepare_for_llm(c, req.model)
    load_info = dict(prep["load"]); load_info["residency"] = prep.get("residency")
    if prep.get("freed"):
        load_info["freed_comfy"] = prep["freed"]
    if prep.get("reloaded_for_residency"):
        load_info["reloaded_for_residency"] = True
    if prep.get("residency", {}).get("warning") and not load_info.get("warning"):
        load_info["warning"] = prep["residency"]["warning"]
    res = promptgen.generate(c, text, mode=h3_mode, duration=dur, tries=req.tries, seed=req.seed, model=req.model)
    res.update({"brief": text, "h3_mode": h3_mode, "duration": dur, "frames": promptgen.frames_for(dur),
                "framing_check": briefmod.check_framing(req.fields.get("framing", ""), req.fields.get("camera", "")),
                "text_check": textcheck.check(req.fields.get("text", "")),
                "actual_seconds": round(promptgen.actual_duration(dur), 2), "load": load_info,
                "usage": llm.usage_summary(c) if llm.is_cloud(c) else None})
    return res


@app.post("/api/prompt/lint")
def prompt_lint(body: dict = Body(...)):
    return promptgen.lint(body.get("prompt", ""), body.get("mode"), body.get("duration"))


def _write_prompt_files(c: dict, text: str, name: Optional[str], notes: Optional[str] = None) -> list[str]:
    """プロンプト.txt を上書きし、アーカイブに .txt（＋任意で .md）を書く。納品規約どおり本文のみ。"""
    text = (text or "").strip() + "\n"
    if not text.strip():
        raise HTTPException(400, "prompt が空")
    written = []
    with io.open(c["prompt_txt"], "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    written.append(c["prompt_txt"])
    if name:
        name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
        os.makedirs(c["archive_dir"], exist_ok=True)
        p = os.path.join(c["archive_dir"], name + ".txt")
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        written.append(p)
        if notes:
            pm = os.path.join(c["archive_dir"], name + ".md")
            with io.open(pm, "w", encoding="utf-8", newline="\n") as f:
                f.write(notes)
            written.append(pm)
    return written


@app.post("/api/prompt/write")
def prompt_write(body: dict = Body(...)):
    # archive_name 例: 2026-08-23_作品_S16_ref2va_192f_16-9
    written = _write_prompt_files(cfg(), body.get("prompt"), body.get("archive_name"), body.get("notes"))
    return {"ok": True, "written": written}


@app.post("/api/prompt/notes")
def prompt_notes(body: dict = Body(...)):
    c = cfg()
    if llm.is_cloud(c) and not body.get("confirm_cloud"):
        return JSONResponse({"need_confirm": True, "reason": "クラウド LLM を使うため料金がかかります"}, status_code=402)
    md = promptgen.write_notes(c, body.get("brief", ""), body.get("prompt", ""), model=body.get("model"))
    return {"notes": md}


@app.get("/api/camera/presets")
def camera_presets():
    return {"presets": briefmod.CAMERA_PRESETS, "framing": briefmod.FRAMING_PRESETS}


@app.post("/api/brief/framing-check")
def brief_framing_check(body: dict = Body(...)):
    """開始の構図 × 終端の指示の組み合わせ判定（素通り / 修正 / 警告）。LLM は使わない。"""
    return briefmod.check_framing(body.get("framing", ""), body.get("camera", ""))


@app.post("/api/brief/text-check")
def brief_text_check(body: dict = Body(...)):
    """「画面内の文字」欄の点検（JIS 水準と長さ）。LLM は使わない。実測の根拠は h3studio/textcheck.py の冒頭。"""
    return textcheck.check(body.get("text", ""))


# ---------------- 履歴（アーカイブ + プロジェクト shots） ----------------

@app.get("/api/history")
def history(project_name: Optional[str] = None, q: Optional[str] = None):
    """アーカイブの .txt/.md と project.shots を突き合わせて一覧にする。"""
    c = cfg()
    out = []
    shots_by_file = {}
    if project_name:
        try:
            pj = project.load(project_name)
            for s in pj.get("shots", []):
                if s.get("archive_name"):
                    shots_by_file[s["archive_name"]] = s
        except FileNotFoundError:
            pass
    for p in sorted(glob.glob(os.path.join(c["archive_dir"], "*.txt")), reverse=True):
        base = os.path.splitext(os.path.basename(p))[0]
        m = re.match(r"(\d{4}-\d{2}-\d{2})_(.+?)_(S\d+[a-z]?)_(\w+)_(\d+)f_([\d-]+)", base)
        item = {"archive_name": base, "date": m.group(1) if m else "", "work": m.group(2) if m else "",
                "shot": m.group(3) if m else base, "mode": m.group(4) if m else "", "frames": int(m.group(5)) if m else None,
                "ratio": m.group(6).replace("-", ":") if m else "", "has_md": os.path.isfile(p[:-4] + ".md"),
                "brief": None}
        s = shots_by_file.get(base)
        if s:
            item["brief"] = s.get("brief"); item["videos"] = s.get("videos"); item["adopted"] = s.get("adopted")
        if project_name and item["work"] and item["work"] != project_name:
            item["other_project"] = True
        if q:
            hay = json.dumps(item, ensure_ascii=False)
            try:
                hay += io.open(p, encoding="utf-8").read()
            except Exception:
                pass
            if q not in hay:
                continue
        out.append(item)
    return {"items": out}


@app.get("/api/history/{archive_name}")
def history_item(archive_name: str):
    c = cfg()
    base = os.path.basename(archive_name)
    p = os.path.join(c["archive_dir"], base + ".txt")
    if not os.path.isfile(p):
        raise HTTPException(404, base)
    txt = io.open(p, encoding="utf-8").read()
    md = ""
    pm = p[:-4] + ".md"
    if os.path.isfile(pm):
        md = io.open(pm, encoding="utf-8").read()
    return {"archive_name": base, "prompt": txt, "notes": md}


# ---------------- 段4: 生成投入・進捗・結果 ----------------

class GenerateReq(BaseModel):
    project: str
    prompt: str
    mode: str = "preview"                # preview | final
    seed: Optional[int] = None
    duration: int = 8
    ratio: str = "16:9"
    images: list[str] = []
    videos: list[str] = []
    shot_id: Optional[str] = None
    h3_mode: Optional[str] = None
    brief: Optional[dict] = None
    allow_raw: bool = False              # 本番で生画像（背景つき）を許す


@app.get("/api/workflow")
def workflow_info():
    """アプリが固定で持つ値（ワークフロー JSON から拾ったもの）と、GUI のワークフローの現状（手動運用との食い違い確認用）。"""
    c = cfg()
    return {"values": comfy.workflow_values(c), "snapshot": comfy.workflow_snapshot(c), "gen": c.get("gen")}


@app.post("/api/generate")
def generate(req: GenerateReq):
    c = cfg()
    if req.mode not in ("preview", "final"):
        raise HTTPException(400, "mode は preview か final")
    if not req.prompt.strip():
        raise HTTPException(400, "prompt が空")
    if comfy.active_job():
        raise HTTPException(409, "生成中のジョブがあります。終わるか中止してから投入してください")
    if not comfy.comfy_up(c):
        raise HTTPException(503, "ComfyUI に届きません（%s）。起動してください" % c["comfy_url"])
    q = comfy.queue_state(c)
    if q["running"] or q["pending"]:
        raise HTTPException(409, "ComfyUI のキューに他のジョブがあります（実行中 %d / 待機 %d）。他セッションの生成を壊さないため投入しません" % (q["running"], q["pending"]))
    in_dir = c["comfy_input_dir"]
    for f in req.images + req.videos:
        if not os.path.isfile(os.path.join(in_dir, os.path.basename(f))):
            raise HTTPException(400, "参照素材が input に無い: " + f)
    raw = [i for i in req.images if "_cut" not in i.lower()]
    if req.mode == "final" and raw and not req.allow_raw:
        return JSONResponse({"need_confirm": True, "raw": raw,
                             "reason": "生画像（背景つき）が含まれています。本番（1344×768）では背景が漏れます（実測）。切り抜いてから本番にするのを勧めます"}, status_code=409)
    dur = max(4, min(int(req.duration), int(c["gen"].get("max_duration", 10))))
    length = promptgen.frames_for(dur)
    w, h = comfy.size_for(c, req.mode, req.ratio)
    gmode = c["gen"].get(req.mode) or {}
    prefix = gmode.get("filename_prefix") or ("MiniMax_H3" if req.mode == "final" else "MiniMax-H3/preview/MiniMax_H3")
    seed = req.seed if req.seed is not None else 1
    params = {"project": req.project, "shot_id": req.shot_id, "prompt": req.prompt, "mode": req.mode, "seed": int(seed),
              "duration": dur, "length": length, "ratio": req.ratio, "width": w, "height": h,
              "images": req.images, "videos": req.videos, "h3_mode": req.h3_mode or ("ref2va" if (req.images or req.videos) else "t2va"),
              "brief": req.brief, "filename_prefix": prefix}

    def _inspect(path, out_dir):
        return inspectmod.analyze(path, out_dir, c.get("ffmpeg") or "ffmpeg", expected_frames=length)

    def _finish(job):
        """完了後に Eagle へ自動送信（config.eagle.auto）。失敗しても生成は成功のまま。"""
        if job.state != "done":
            return
        try:
            r = eagle.maybe_auto_send(cfg(), job.to_dict(full=True))
            if r:
                job.add("Eagle に送りました（%d件）" % r["count"], "ok")
                job.save()
        except Exception as e:
            job.add("Eagle への送信に失敗: %s" % e, "warn"); job.save()
    job = comfy.start_job(c, params, inspect_fn=_inspect, on_finish=_finish)
    return {"job_id": job.id, "params": {k: v for k, v in params.items() if k != "prompt"},
            "eta_minutes": 3 if req.mode == "preview" else 18}


@app.get("/api/jobs")
def jobs_list():
    live = [j.to_dict() for j in sorted(comfy.JOBS.values(), key=lambda j: -j.created)]
    live_ids = {j["id"] for j in live}
    saved = [j for j in comfy.load_saved_jobs() if j.get("id") not in live_ids]
    for j in saved:
        if isinstance(j.get("params"), dict):
            j["params"].pop("prompt", None)
    return {"jobs": live + saved, "active": (comfy.active_job().id if comfy.active_job() else None)}


def _job_or_404(job_id: str):
    j = comfy.JOBS.get(job_id)
    if j:
        return j.to_dict(full=True)
    p = os.path.join(comfy.JOBS_DIR, os.path.basename(job_id), "job.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    raise HTTPException(404, "job が無い: " + job_id)


@app.get("/api/jobs/{job_id}")
def job_get(job_id: str):
    return _job_or_404(job_id)


@app.get("/api/jobs/{job_id}/contact")
def job_contact(job_id: str):
    p = os.path.join(comfy.JOBS_DIR, os.path.basename(job_id), "contact.jpg")
    if not os.path.isfile(p):
        raise HTTPException(404, "コンタクトシートが無い")
    return FileResponse(p, media_type="image/jpeg")


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    j = comfy.JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "job が無い: " + job_id)
    return comfy.cancel_job(cfg(), j)


@app.get("/api/video")
def video_file(rel: str):
    """ComfyUI の output 配下の動画を相対パスで返す（サブフォルダ可）。"""
    base = os.path.abspath(cfg()["comfy_output_dir"])
    p = os.path.abspath(os.path.join(base, rel))
    if not p.startswith(base) or not os.path.isfile(p):
        raise HTTPException(404, rel)
    return FileResponse(p, media_type=mimetypes.guess_type(p)[0] or "video/mp4")


# ---------------- 段5: 採用 → アーカイブ ----------------

@app.post("/api/shots/archive")
def shots_archive(body: dict = Body(...)):
    """
    採用: プロンプト.txt 上書き + アーカイブ .txt(+.md) + project.shots に動画パス・計測値を記録。
    body: {project, shot_id, prompt, archive_name, notes?, brief?, mode?, duration?, ratio?, seed?, images?, videos?, lint?, job_id?, note?}
    同じ shot_id / archive_name の記録があれば上書き（採用し直し）。
    """
    c = cfg()
    name = body.get("project") or ""
    try:
        proj = project.load(name)
    except FileNotFoundError:
        raise HTTPException(404, "プロジェクトが無い: " + name)
    written = _write_prompt_files(c, body.get("prompt"), body.get("archive_name"), body.get("notes"))
    job = None
    if body.get("job_id"):
        try:
            job = _job_or_404(body["job_id"])
        except HTTPException:
            job = None
    shot = {k: body.get(k) for k in ("id", "archive_name", "brief", "mode", "duration", "ratio", "seed", "images", "videos_ref", "lint", "note") if body.get(k) is not None}
    shot["id"] = body.get("shot_id") or shot.get("id") or project.next_shot_id(proj)
    shot["adopted"] = True
    shot["adopted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if job:
        res = job.get("result") or {}
        vid = (res.get("video") or {})
        ins = res.get("inspect") or {}
        shot["job_id"] = job["id"]
        shot["gen_mode"] = (job.get("params") or {}).get("mode")
        shot["videos"] = [vid.get("rel")] if vid.get("rel") else []
        shot["video_path"] = vid.get("path")
        shot["gen_seconds"] = res.get("gen_seconds")
        shot["metrics"] = {k: ins.get(k) for k in ("frames", "duration", "mbps", "audio", "frame_diff", "width", "height") if k in ins}
        shot["size"] = [(job.get("params") or {}).get("width"), (job.get("params") or {}).get("height")]
    # 既存の同 id / 同 archive_name を更新、無ければ追加
    shots = proj.setdefault("shots", [])
    idx = next((i for i, s in enumerate(shots) if (shot.get("archive_name") and s.get("archive_name") == shot["archive_name"]) or (s.get("id") == shot["id"] and not s.get("adopted"))), None)
    if idx is None:
        shot.setdefault("created", time.strftime("%Y-%m-%dT%H:%M:%S"))
        shots.append(shot)
    else:
        shots[idx].update(shot)
    project.save(proj)
    return {"ok": True, "written": written, "shot": shot, "next_shot_id": project.next_shot_id(proj)}


# ---------------- Eagle ----------------

@app.get("/api/eagle/info")
def eagle_info():
    return eagle.info(cfg())


@app.get("/api/eagle/folders")
def eagle_folders():
    return {"folders": eagle.folders(cfg())}


@app.post("/api/eagle/send")
def eagle_send(body: dict = Body(...)):
    """
    ジョブの結果を Eagle に送る（音付きの1本だけ）。
    body: {job_id, folder_id?, contact_sheet?}  または {path, name?, tags?, annotation?, folder_id?}
    """
    c = cfg()
    if not eagle.enabled(c):
        raise HTTPException(400, "Eagle 連携が無効です。設定で有効にしてください")
    try:
        if body.get("job_id"):
            job = _job_or_404(body["job_id"])
            proj = None
            try:
                proj = project.load((job.get("params") or {}).get("project") or "")
            except Exception:
                pass
            return eagle.send_job(c, job, proj, body.get("folder_id"), body.get("contact_sheet"))
        p = body.get("path")
        if not p:
            raise HTTPException(400, "job_id か path が要ります")
        base = os.path.abspath(c["comfy_output_dir"])
        ap = os.path.abspath(p)
        if not ap.startswith(base):
            raise HTTPException(400, "出力フォルダの外は送れません")
        return {"ok": True, "sent": [eagle.send_file(c, ap, body.get("name"), body.get("tags"),
                                                     body.get("annotation", ""), body.get("folder_id"))], "count": 1}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


# ---------------- 段6b: 切り抜き（SAM3） ----------------

class CutDetectReq(BaseModel):
    image: str
    source: str = "input"                # input | raw
    text: str = "person:5"
    threshold: float = 0.5
    refine: int = 1


class CutSelectReq(BaseModel):
    session: str
    select: list[int] = []
    crop: bool = False
    crop_margin: int = 40


class CutSaveReq(CutSelectReq):
    save_as: str
    overwrite: bool = False


@app.get("/api/cut/available")
def cut_available():
    """切り抜きが使える環境か（ComfyUI / SAM3_Detect / チェックポイント）。"""
    return cut.available(cfg())


@app.post("/api/cut/detect")
def cut_detect(req: CutDetectReq):
    """SAM3 を1回投げて人物ごとの個別マスクを取り、数値の表とサムネイルを返す（約6秒）。"""
    try:
        return cut.detect(cfg(), req.image, req.text, req.threshold, req.refine, req.source)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/cut/sweep")
def cut_sweep(req: CutDetectReq):
    """threshold を振って検出数だけ数える。新しい絵は必ずこれを見てから threshold を決める。"""
    try:
        return cut.sweep(cfg(), req.image, req.text, req.source)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/cut/preview")
def cut_preview(req: CutSelectReq):
    """選んだ人だけを単色背景に合成（ローカル・GPU 不要）。input には保存しない。"""
    try:
        return cut.preview(cfg(), req.session, req.select, req.crop, req.crop_margin)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/cut/save")
def cut_save(req: CutSaveReq):
    """input\\ に保存して check_cut で検査する。保存後は参照素材に出る。"""
    try:
        return cut.save_cut(cfg(), req.session, req.select, req.save_as, req.crop, req.crop_margin, req.overwrite)
    except FileExistsError as e:
        return JSONResponse({"need_confirm": True, "reason": "同じ名前の画像が input にあります: %s。上書きしますか？" % e,
                             "exists": str(e)}, status_code=409)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/cut/file/{session}/{name}")
def cut_file(session: str, name: str):
    """cutcache の thumb_N.jpg / preview.png を返す。"""
    d = os.path.join(cut.CACHE_DIR, os.path.basename(session))
    p = _safe_join(d, name)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.post("/api/cut/check")
def cut_check(body: dict = Body(...)):
    """既存の参照画像を check_cut にかける（本番前の確認用）。"""
    name = os.path.basename(body.get("image") or "")
    p = _safe_join(cfg()["comfy_input_dir"], name)
    return cut.check(cfg(), p, body.get("expect_people"))


if __name__ == "__main__":
    import argparse, uvicorn
    ap = argparse.ArgumentParser(description="H3 Studio")
    ap.add_argument("--port", type=int, default=None, help="config.json の port を上書きする")
    ap.add_argument("--host", default="127.0.0.1", help="既定は 127.0.0.1（この PC からだけ）")
    a = ap.parse_args()
    port = a.port or int(cfg().get("port") or 8765)
    print("H3 Studio  http://%s:%d  (config: %s)" % (a.host, port, cfg().get("_source")))
    uvicorn.run(app, host=a.host, port=port, log_level="info")
