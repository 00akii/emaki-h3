# -*- coding: utf-8 -*-
"""
cut.py — SAM3 による参照画像の切り抜き（段6b）。

  detect(cfg, image, text, threshold, refine)  ComfyUI の /prompt に SAM3 を1回投げ、
                                               人物ごとの個別マスクと数値（score/面積/左から/髪色/bbox）を返す
  composite(cfg, session, select, crop)        選んだマスクだけを #808080 に合成（ローカル、GPU 不要）
  save_cut(cfg, session, select, save_as, ...) input\\ に保存して check_cut で検査

**SAM3 を回すのは detect の1回だけ。** マスクは原寸 PNG で `cutcache/<session>/` に落とし、
プレビューも保存もその合成で済ませる（毎回 GPU を触らない）。

SAM3 セッションの実測（記憶 h3-sam3-cutout / SAM3/引き継ぎ.md）に従う:
  - `refine_iterations` は 1（2 以上でマスクが劣化する）
  - `negative_coords` は使わない（マスクがディザ状に破綻する）
  - `person:1` とは書かない（本体のパースのバグで別物を拾う。Comfy-Org/ComfyUI#15811）
  - `:N` は個数の上限であって「その数だけ出す」ではない。先頭K枚は N に依存しない
  - **index は検出スコア順で、順位はほぼ「画面の水平中心からの距離」で決まる。**
    大きさ・向き・人物とは無関係。番号を固定値として扱わないこと
"""
from __future__ import annotations
import hashlib, io, json, os, re, shutil, sys, time, urllib.parse, urllib.request
from . import config, comfy, llm

CACHE_DIR = os.path.join(config.APP_DIR, "cutcache")
CKPT_DEFAULT = "sam3\\sam3.1_multiplex_fp16.safetensors"
TEXT_DEFAULT = "person:5"
GREY = (128, 128, 128)          # Image Blank ノードの既定。check_cut もこれを期待する

sys.path.insert(0, config.VENDOR_DIR)

HUES = [("赤", 0, 12), ("橙", 13, 22), ("金", 23, 38), ("黄緑", 39, 48), ("緑", 49, 85),
        ("水", 86, 99), ("青", 100, 130), ("紫", 131, 155), ("桃", 156, 167), ("赤", 168, 180)]


def _np():
    import numpy as np
    return np


def _cv2():
    import cv2
    return cv2


# ---------------- SAM3 のチェックポイント ----------------

def find_checkpoint(cfg: dict) -> str:
    """
    config.sam3.checkpoint が一覧にあればそれ。無ければ検証済みの綴りに近いものを選ぶ。
    実測環境には sam3 が5個あり、検証したのは `sam3.1_multiplex_fp16` だけ。名前順で先頭を拾うと
    別の重み（`-sam3-fp16`）を掴むので、3.1 → multiplex → fp16 の順に加点して選ぶ。
    """
    want = ((cfg.get("sam3") or {}).get("checkpoint") or "").strip()
    try:
        oi = comfy._get(cfg, "/object_info/CheckpointLoaderSimple", timeout=10)
        names = [str(n) for n in oi["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]]
    except Exception:
        return want or CKPT_DEFAULT
    if want and want in names:
        return want
    cand = [n for n in names if "sam3" in n.lower()]
    if not cand:
        return want or CKPT_DEFAULT
    if CKPT_DEFAULT in cand:
        return CKPT_DEFAULT

    def score(n: str) -> tuple:
        s = n.lower()
        return (("3.1" in s) * 4 + ("multiplex" in s) * 2 + ("fp16" in s) * 1 + s.endswith(".safetensors"), -len(s))
    return max(cand, key=score)


def available(cfg: dict) -> dict:
    """切り抜きが使える環境か。画面で「＋切り抜く」を出す前に見る。"""
    out = {"comfy": comfy.comfy_up(cfg), "node": False, "checkpoint": None, "reason": ""}
    if not out["comfy"]:
        out["reason"] = "ComfyUI に届きません（%s）。起動してください" % cfg["comfy_url"]
        return out
    out["node"] = comfy.node_available(cfg, "SAM3_Detect")
    if not out["node"]:
        out["reason"] = "この ComfyUI に SAM3_Detect ノードがありません（本体 0.33 以降の comfy_extras.nodes_sam3）"
        return out
    ck = find_checkpoint(cfg)
    out["checkpoint"] = ck
    if not ck or "sam3" not in ck.lower():
        out["reason"] = "SAM3 のチェックポイントが見つかりません（models/checkpoints/sam3/ に置いてください）"
    return out


# ---------------- 検出 ----------------

def _guard_busy(cfg: dict, wait: float = 6.0):
    """生成ジョブ中は SAM3 を投げない（GPU を取り合って他セッションの生成を壊す）。
    直前の検出がキューから消えるまで一瞬ラグがあるので、少しだけ待ってから諦める。"""
    j = comfy.active_job()
    if j:
        raise RuntimeError("生成ジョブ %s が %s です。終わるか中止してから切り抜いてください" % (j.id, j.state))
    t0 = time.time()
    while True:
        q = comfy.queue_state(cfg)
        if not (q["running"] or q["pending"]):
            return
        if time.time() - t0 >= wait:
            raise RuntimeError("ComfyUI のキューに他のジョブがあります（実行中 %d / 待機 %d）" % (q["running"], q["pending"]))
        time.sleep(0.5)


def _wait_queue_clear(cfg: dict, prompt_id: str, timeout: float = 15.0):
    """自分の prompt がキューから消えるまで待つ（次の検出がすぐ 409 にならないように）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            q = comfy.queue_state(cfg)
        except Exception:
            return
        if prompt_id not in q["running_ids"] and prompt_id not in q["pending_ids"]:
            return
        time.sleep(0.3)


def _prune_cache(keep: int = 12):
    """cutcache は原寸マスクを持つので溜めない。更新が古いものから消す。"""
    try:
        ds = [os.path.join(CACHE_DIR, d) for d in os.listdir(CACHE_DIR)]
        ds = [d for d in ds if os.path.isdir(d)]
        ds.sort(key=lambda d: -os.path.getmtime(d))
        for d in ds[keep:]:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def _session_id(image: str, text: str, threshold: float, refine: int) -> str:
    key = "%s|%s|%.3f|%d" % (image, text, threshold, refine)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.splitext(image)[0])[:40] + "-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def _disp_node(cfg: dict) -> str:
    """検出枠を取り出すノード。DisplayAny（カスタム）が無ければ本体の PreviewAny。"""
    return "DisplayAny" if comfy.node_available(cfg, "DisplayAny") else "PreviewAny"


def _build_graph(image: str, text: str, threshold: float, refine: int, ckpt: str,
                 disp: str = "DisplayAny") -> dict:
    # 検出枠（boxes）を text として取り出すノード。DisplayAny はカスタム、PreviewAny は本体。
    # どちらも history の outputs["20"]["text"] に入るので、下流の読み方は同じ
    disp_in = ({"input": ["3", 1], "mode": "raw value"} if disp == "DisplayAny"
               else {"source": ["3", 1]})
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "LoadImage", "inputs": {"image": image}},
        "9": {"class_type": "CLIPTextEncode", "inputs": {"text": text, "clip": ["1", 1]}},
        "3": {"class_type": "SAM3_Detect",
              "inputs": {"model": ["1", 0], "image": ["2", 0], "conditioning": ["9", 0],
                         "threshold": float(threshold), "refine_iterations": int(refine),
                         "individual_masks": True}},
        "12": {"class_type": "MaskToImage", "inputs": {"mask": ["3", 0]}},
        # SaveImage（output に永続）ではなく PreviewImage（ComfyUI の temp。起動時に本体が掃除する）。
        # SaveImage だと毎回マスク PNG が output に溜まり、こちらで消すと
        # 同一パラメータの再検出でキャッシュされた history が消えたファイルを指して 404 になる
        "13": {"class_type": "PreviewImage", "inputs": {"images": ["12", 0]}},
        "20": {"class_type": disp, "inputs": disp_in},
    }


def _fetch(cfg: dict, item: dict) -> bytes:
    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                "type": item.get("type", "output")})
    with urllib.request.urlopen(comfy._base(cfg) + "/view?" + q, timeout=60) as r:
        return r.read()


def _hair(img_bgr, m):
    """マスク上端30%の彩度のある画素の最頻色相 → 髪色の名前。sam3_pick.py と同じ判定。"""
    np, cv2 = _np(), _cv2()
    ys, xs = np.where(m)
    if len(ys) == 0:
        return "?"
    top = m.copy()
    top[int(ys.min() + (ys.max() - ys.min()) * 0.30):, :] = False
    if top.sum() < 50:
        top = m
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    sel = top & (hsv[..., 1] > 60) & (hsv[..., 2] > 40)
    if sel.sum() < 30:
        return "?"
    peak = int(np.bincount(hsv[..., 0][sel], minlength=180).argmax())
    for nm, lo, hi in HUES:
        if lo <= peak <= hi:
            return nm
    return "h%d" % peak


def _thumb(np_img, mask, path, width=180):
    """その人だけを灰色背景に抜いた小さいプレビュー（誰が何番かを画面で確かめる用）。"""
    np, cv2 = _np(), _cv2()
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return None
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    pad = 12
    H, W = mask.shape
    y0, y1 = max(0, y0 - pad), min(H - 1, y1 + pad)
    x0, x1 = max(0, x0 - pad), min(W - 1, x1 + pad)
    sub = np_img[y0:y1 + 1, x0:x1 + 1]
    sm = mask[y0:y1 + 1, x0:x1 + 1]
    out = np.full(sub.shape, GREY, dtype=np.uint8)
    out[sm] = sub[sm]
    h, w = out.shape[:2]
    scale = width / max(w, 1)
    if scale < 1:
        out = cv2.resize(out, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    from PIL import Image
    Image.fromarray(out).save(path, quality=85)
    return os.path.basename(path)


def detect(cfg: dict, image: str, text: str = TEXT_DEFAULT, threshold: float = 0.5,
           refine: int = 1, source: str = "input") -> dict:
    """
    SAM3 を1回投げて個別マスクを取り、数値の表とサムネイルを作る。
    source: "input"（ComfyUI の input\\）か "raw"（config.raw_dir。input\\ に一時コピーしてから回す）
    """
    np = _np(); cv2 = _cv2()
    _guard_busy(cfg)
    av = available(cfg)
    if av.get("reason"):
        raise RuntimeError(av["reason"])
    if int(refine) != 1:
        # 実測: 2 以上でマスクが劣化する（点・枠・テキストのどの経路でも）
        refine = max(0, min(int(refine), 1))
    text = (text or TEXT_DEFAULT).strip()
    if re.search(r":\s*1\s*(,|$)", text):
        raise ValueError("`:1` は使えません（ComfyUI 本体のパースのバグで別の物体を拾う。Comfy-Org/ComfyUI#15811）。"
                         ":N を省くか :2 以上にしてください")

    in_dir = cfg["comfy_input_dir"]
    name = os.path.basename(image)
    tmp_copy = None
    if source == "raw":
        src = os.path.join(cfg["raw_dir"], name)
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        # ComfyUI は input\ しか読めないので一時名でコピーする
        name = "_h3studio_tmp_" + name
        tmp_copy = os.path.join(in_dir, name)
        shutil.copyfile(src, tmp_copy)
    path = os.path.join(in_dir, name)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    sid = _session_id(image, text, threshold, refine)
    sdir = os.path.join(CACHE_DIR, sid)
    if os.path.isdir(sdir):
        shutil.rmtree(sdir, ignore_errors=True)
    os.makedirs(sdir, exist_ok=True)
    _prune_cache(keep=12)

    # LM Studio が GPU を抱えていて空きが足りないときだけ降ろす（SAM3 は小さいので普段は不要）
    freed = None
    try:
        import subprocess
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        tot, used = [int(float(x)) for x in r.stdout.strip().split(",")[:2]]
        if (tot - used) < 3000 and llm.loaded_models(cfg):
            llm.unload_all(cfg); freed = "LM Studio を降ろしました（空き %dMB）" % (tot - used)
    except Exception:
        pass

    t0 = time.time()
    try:
        graph = _build_graph(name, text, threshold, refine, av["checkpoint"], _disp_node(cfg))
        pid = comfy.submit(cfg, graph, "h3studio-cut")
        hist = None
        while time.time() - t0 < 600:
            hist = comfy.history_item(cfg, pid)
            if hist:
                break
            time.sleep(0.4)
        if not hist:
            raise TimeoutError("SAM3 が 600 秒で返らない")
        if hist.get("status", {}).get("status_str") == "error":
            msg = ""
            for m in hist.get("status", {}).get("messages", []):
                if m and m[0] == "execution_error":
                    msg = (m[1].get("exception_message") or "")[:300]
            # 検出ゼロだと SAM3_Detect が落ちる（空のマスクを返さない）。設定の問題なので ValueError（400）
            raise ValueError("SAM3 が検出ゼロで止まりました（threshold %.2f / %r）。threshold を下げるか検出テキストを見直してください%s"
                             % (threshold, text, ("  [%s]" % msg) if msg else ""))
        outs = hist.get("outputs", {})
        items = outs.get("13", {}).get("images", [])
        masks = []
        from PIL import Image
        for i, it in enumerate(items):
            blob = _fetch(cfg, it)
            m = np.array(Image.open(io.BytesIO(blob)).convert("L")) > 127
            masks.append(m)
            np.save(os.path.join(sdir, "mask_%d.npy" % i), np.packbits(m, axis=-1))
        try:
            import ast
            boxes = ast.literal_eval("".join(outs.get("20", {}).get("text", [])))[0]
        except Exception:
            boxes = []
        _wait_queue_clear(cfg, pid)
    finally:
        if tmp_copy and os.path.isfile(tmp_copy):
            try:
                os.remove(tmp_copy)
            except OSError:
                pass
    seconds = round(time.time() - t0, 1)

    # cv2.imread は日本語パスを開けない。元画像は必ず PIL 経由で読む（一時コピーは消えている）
    img = _source_image(cfg, {"image": image, "source": source})
    H, W = img.shape[:2]
    if masks and masks[0].shape != (H, W):
        raise RuntimeError("マスク %s と元画像 %s のサイズが違います" % (masks[0].shape, (H, W)))
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    order_x = sorted(range(len(masks)), key=lambda i: (np.where(masks[i])[1].mean() if masks[i].any() else 1e9))
    rank_x = {i: k for k, i in enumerate(order_x)}
    rows = []
    for i, m in enumerate(masks):
        if not m.any():
            continue
        ys, xs = np.where(m)
        rows.append({
            "index": i,
            "score": round(float(boxes[i]["score"]), 5) if i < len(boxes) else None,
            "area_pct": round(float(m.mean()) * 100, 2),
            "cx": int(xs.mean()), "cy": int(ys.mean()),
            "x0": int(xs.min()), "x1": int(xs.max()), "y0": int(ys.min()), "y1": int(ys.max()),
            "from_left": rank_x[i] + 1,
            "hair": _hair(img, m),
            "thumb": _thumb(rgb, m, os.path.join(sdir, "thumb_%d.jpg" % i)),
        })
    meta = {"session": sid, "image": image, "source": source, "text": text, "threshold": threshold,
            "refine": refine, "checkpoint": av["checkpoint"], "width": W, "height": H,
            "n_masks": len(masks), "detections": rows, "seconds": seconds, "shape": [H, W],
            "freed": freed, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(os.path.join(sdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def sweep(cfg: dict, image: str, text: str = TEXT_DEFAULT, source: str = "input",
          thresholds: list[float] | None = None) -> dict:
    """
    threshold を振って検出数だけ数える（マスクは取り込まない）。1点あたり1〜2秒。

    SAM3 セッションの取り決め: **新しい絵では必ずスイープする。**既定の検出テキストのまま
    threshold を上げると検出数が落ちる（実測 `person:30,BODY:30,human:30` で 0.5→23個 / 0.9→1個）。
    点や枠を打つ前にこれを見る。
    """
    _guard_busy(cfg)
    av = available(cfg)
    if av.get("reason"):
        raise RuntimeError(av["reason"])
    ths = thresholds or [0.35, 0.5, 0.6, 0.7, 0.8, 0.9]
    in_dir = cfg["comfy_input_dir"]
    name = os.path.basename(image)
    tmp_copy = None
    if source == "raw":
        src = os.path.join(cfg["raw_dir"], name)
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        name = "_h3studio_tmp_" + name
        tmp_copy = os.path.join(in_dir, name)
        shutil.copyfile(src, tmp_copy)
    rows = []
    t0 = time.time()
    try:
        for th in ths:
            graph = _build_graph(name, text, float(th), 1, av["checkpoint"], _disp_node(cfg))
            pid = comfy.submit(cfg, graph, "h3studio-cut")
            hist = None
            t1 = time.time()
            while time.time() - t1 < 300:
                hist = comfy.history_item(cfg, pid)
                if hist:
                    break
                time.sleep(0.3)
            n = None
            if hist and hist.get("status", {}).get("status_str") != "error":
                n = len(hist.get("outputs", {}).get("13", {}).get("images", []))
            rows.append({"threshold": round(float(th), 2), "n": n})
            _wait_queue_clear(cfg, pid)
    finally:
        if tmp_copy and os.path.isfile(tmp_copy):
            try:
                os.remove(tmp_copy)
            except OSError:
                pass
    return {"image": image, "text": text, "rows": rows, "seconds": round(time.time() - t0, 1)}


# ---------------- 合成（ローカル・GPU 不要） ----------------

def load_session(sid: str) -> dict:
    p = os.path.join(CACHE_DIR, os.path.basename(sid), "meta.json")
    if not os.path.isfile(p):
        raise FileNotFoundError("検出結果が無い（もう一度検出してください）: " + sid)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _load_mask(sid: str, i: int, shape):
    np = _np()
    p = os.path.join(CACHE_DIR, os.path.basename(sid), "mask_%d.npy" % i)
    packed = np.load(p)
    return np.unpackbits(packed, axis=-1, count=shape[1]).astype(bool)


def _source_image(cfg: dict, meta: dict):
    np = _np(); cv2 = _cv2()
    from PIL import Image
    d = cfg["raw_dir"] if meta.get("source") == "raw" else cfg["comfy_input_dir"]
    p = os.path.join(d, os.path.basename(meta["image"]))
    if not os.path.isfile(p):
        raise FileNotFoundError(p)
    return cv2.cvtColor(np.array(Image.open(p).convert("RGB")), cv2.COLOR_RGB2BGR)


def composite(cfg: dict, sid: str, select: list[int], crop: bool = False,
              crop_margin: int = 40, bg=GREY):
    """選んだ検出だけを単色背景に合成する。返り値: (PIL.Image, 情報)"""
    np = _np(); cv2 = _cv2()
    from PIL import Image
    meta = load_session(sid)
    shape = tuple(meta["shape"])
    img = _source_image(cfg, meta)
    if img.shape[:2] != shape:
        raise RuntimeError("元画像のサイズが検出時と違います（%s → %s）。もう一度検出してください"
                           % (shape, img.shape[:2]))
    valid = {d["index"] for d in meta["detections"]}
    sel = [int(i) for i in (select or []) if int(i) in valid]
    if not sel:
        raise ValueError("残す人を1人以上選んでください")
    m = np.zeros(shape, bool)
    for i in sel:
        m |= _load_mask(sid, i, shape)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    out = np.full(rgb.shape, bg, dtype=np.uint8)
    out[m] = rgb[m]
    info = {"selected": sel, "cropped": False, "size": [shape[1], shape[0]]}
    if crop:
        ys, xs = np.where(m)
        y0 = max(0, int(ys.min()) - crop_margin); y1 = min(shape[0] - 1, int(ys.max()) + crop_margin)
        x0 = max(0, int(xs.min()) - crop_margin); x1 = min(shape[1] - 1, int(xs.max()) + crop_margin)
        out = out[y0:y1 + 1, x0:x1 + 1]
        info.update({"cropped": True, "crop_box": [x0, y0, x1, y1], "size": [x1 - x0 + 1, y1 - y0 + 1]})
    info["subject_pct"] = round(float(m.sum()) / (out.shape[0] * out.shape[1]) * 100, 2)
    return Image.fromarray(out), info


def preview(cfg: dict, sid: str, select: list[int], crop: bool = False, crop_margin: int = 40) -> dict:
    """プレビュー PNG を cutcache に書き、check_cut の検査結果を付けて返す。input\\ には置かない。"""
    im, info = composite(cfg, sid, select, crop, crop_margin)
    sdir = os.path.join(CACHE_DIR, os.path.basename(sid))
    p = os.path.join(sdir, "preview.png")
    im.save(p)
    info["preview"] = "preview.png"
    info["check"] = check(cfg, p)
    return info


def check(cfg: dict, path: str, expect_people: int | None = None, out_mp: float | None = None) -> dict:
    """vendor/check_cut.py の検査（背景の単色性・かたまり・大きさ・実効解像度）。"""
    if out_mp is None:
        f = (cfg.get("gen") or {}).get("final") or {}
        out_mp = round(int(f.get("width", 1344)) * int(f.get("height", 768)) / 1e6, 2)
    try:
        import check_cut
    except Exception as e:
        return {"error": "check_cut を読み込めない: %r" % e, "errors": [], "warns": [], "info": []}
    try:
        r = check_cut.analyze(path, expect_people, out_mp)
        r["ok"] = not r["errors"]
        r["out_mp"] = out_mp
        return r
    except Exception as e:
        return {"error": repr(e), "errors": [], "warns": [], "info": []}


def suggest_name(project: str, shot_id: str, kind: str = "front") -> str:
    """命名規約 h3ref_<ショットID>_<向き>_cut.png（SAM3 セッションの取り決め）。"""
    base = re.sub(r"[^A-Za-z0-9_-]+", "", shot_id or "S01") or "S01"
    kind = re.sub(r"[^A-Za-z0-9_-]+", "", kind or "front") or "front"
    return "h3ref_%s_%s_cut.png" % (base, kind)


def save_cut(cfg: dict, sid: str, select: list[int], save_as: str, crop: bool = False,
             crop_margin: int = 40, overwrite: bool = False) -> dict:
    """input\\ に保存して検査する。保存後は参照素材として選べるようになる。"""
    name = os.path.basename((save_as or "").strip())
    if not name:
        raise ValueError("保存名が空です")
    if not name.lower().endswith(".png"):
        name = os.path.splitext(name)[0] + ".png"
    if re.search(r"[\\/:*?\"<>|]", name):
        raise ValueError("保存名に使えない文字が含まれています")
    if "_cut" not in name.lower():
        # 画面の「切り抜き済みのみ」フィルタと本番前の警告がこの綴りを見ている
        name = os.path.splitext(name)[0] + "_cut.png"
    dst = os.path.join(cfg["comfy_input_dir"], name)
    if os.path.exists(dst) and not overwrite:
        raise FileExistsError(name)
    im, info = composite(cfg, sid, select, crop, crop_margin)
    im.save(dst)
    info["saved"] = name
    info["path"] = dst
    info["check"] = check(cfg, dst, expect_people=len(info["selected"]))
    return info
