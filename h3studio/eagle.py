# -*- coding: utf-8 -*-
"""
eagle.py — 出来上がった動画を Eagle に送る。

  info(cfg)                       Eagle が起動しているか（バージョン・ライブラリ）
  folders(cfg)                    フォルダ一覧（階層をパス表記で平たくして返す）
  send_file(cfg, path, ...)       1ファイルを追加
  send_job(cfg, job, project)     ジョブの結果（動画＋任意でコンタクトシート）をまとめて追加

**ComfyUI の `SendToEagleVideo` ノードは使わない。** 理由:

1. **無音版と音付き版の2本が送られる。** `VHS_VideoCombine` の `Filenames` 出力には
   `MiniMax_H3_00061.mp4`（無音）と `MiniMax_H3_00061-audio.mp4`（音付き）の両方が入るため。
   こちらは結果取得の時点で音付きの1本を特定しているので、それだけを送れる
2. 生成時にしか送れない。あとから「やっぱり送る」「採用したものだけ送る」ができない
3. カスタムノード依存が増える（公開時に「入れてください」が1つ増える）
4. アプリが持っている情報（ブリーフ・検査結果・作品名・ショットID）を注釈に書ける

Eagle 側は既定でトークン不要（4.0.0 で確認）。必要な環境向けに `config.eagle.token` を用意してある。
"""
from __future__ import annotations
import json, os, time, urllib.error, urllib.parse, urllib.request

DEFAULT_URL = "http://localhost:41595"


def _cfg(cfg: dict) -> dict:
    return cfg.get("eagle") or {}


def enabled(cfg: dict) -> bool:
    return bool(_cfg(cfg).get("enabled"))


def _url(cfg: dict, path: str) -> str:
    base = (_cfg(cfg).get("url") or DEFAULT_URL).rstrip("/")
    token = (_cfg(cfg).get("token") or "").strip()
    if token:
        path += ("&" if "?" in path else "?") + urllib.parse.urlencode({"token": token})
    return base + path


def _get(cfg: dict, path: str, timeout=8):
    with urllib.request.urlopen(_url(cfg, path), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(cfg: dict, path: str, body: dict, timeout=30):
    req = urllib.request.Request(_url(cfg, path), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def info(cfg: dict) -> dict:
    """疎通確認。画面の「設定」と Eagle の設定欄に出す。"""
    out = {"up": False, "enabled": enabled(cfg), "url": _cfg(cfg).get("url") or DEFAULT_URL,
           "version": None, "library": None, "folder_id": _cfg(cfg).get("folder_id") or "",
           "folder_name": _cfg(cfg).get("folder_name") or "", "auto": _cfg(cfg).get("auto", "off"),
           "send_contact_sheet": bool(_cfg(cfg).get("send_contact_sheet")), "error": ""}
    try:
        j = _get(cfg, "/api/application/info", timeout=5)
        out["up"] = j.get("status") == "success"
        out["version"] = (j.get("data") or {}).get("version")
    except Exception as e:
        out["error"] = "Eagle に届きません（%s）。Eagle を起動してください [%s]" % (out["url"], e.__class__.__name__)
        return out
    try:
        j = _get(cfg, "/api/library/info", timeout=5)
        lib = (j.get("data") or {}).get("library")
        out["library"] = lib.get("path") if isinstance(lib, dict) else None
    except Exception:
        pass
    return out


def folders(cfg: dict) -> list[dict]:
    """階層を "親/子" のパス表記にして平たく返す（選択用）。"""
    try:
        j = _get(cfg, "/api/folder/list", timeout=10)
    except Exception:
        return []
    out = []

    def walk(items, prefix=""):
        for f in items or []:
            name = f.get("name") or ""
            path = (prefix + "/" + name).strip("/")
            out.append({"id": f.get("id"), "name": name, "path": path})
            walk(f.get("children"), path)
    walk(j.get("data") or [])
    out.sort(key=lambda x: x["path"].lower())
    return out


def create_folder(cfg: dict, name: str, parent_id: str | None = None) -> dict:
    body = {"folderName": name}
    if parent_id:
        body["parent"] = parent_id
    j = _post(cfg, "/api/folder/create", body)
    d = j.get("data") or {}
    return {"id": d.get("id"), "name": d.get("name")}


def send_file(cfg: dict, path: str, name: str | None = None, tags: list[str] | None = None,
              annotation: str = "", folder_id: str | None = None, website: str = "") -> dict:
    """1ファイルを Eagle に追加する。path は絶対パス。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    body = {"path": os.path.abspath(path), "name": name or os.path.splitext(os.path.basename(path))[0]}
    if tags:
        body["tags"] = [t for t in tags if t]
    if annotation:
        body["annotation"] = annotation[:8000]
    if website:
        body["website"] = website
    fid = folder_id if folder_id is not None else (_cfg(cfg).get("folder_id") or "")
    if fid:
        body["folderId"] = fid
    try:
        j = _post(cfg, "/api/item/addFromPath", body)
    except urllib.error.HTTPError as e:
        raise RuntimeError("Eagle API %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
    if j.get("status") != "success":
        raise RuntimeError("Eagle が受け付けませんでした: %s" % json.dumps(j, ensure_ascii=False)[:300])
    return {"ok": True, "name": body["name"], "path": body["path"], "folder_id": fid,
            "tags": body.get("tags", []), "item": j.get("data")}


# ---------------- ジョブの結果を送る ----------------

def _annotation(job: dict, project: dict | None) -> str:
    p = job.get("params") or {}
    res = job.get("result") or {}
    ins = res.get("inspect") or {}
    au = ins.get("audio") or {}
    L = []
    L.append("%s / %s / %s" % (p.get("project") or "", p.get("shot_id") or "", "本番" if p.get("mode") == "final" else "プレビュー"))
    L.append("%sx%s · %sf · 実尺 %ss · seed %s · %s" % (p.get("width"), p.get("height"), ins.get("frames") or p.get("length"),
                                                        ins.get("duration"), p.get("seed"), p.get("ratio")))
    if ins.get("mbps") is not None:
        L.append("bit_rate %s Mbps / 音量 mean %s dB max %s dB / フレーム間差分 %s"
                 % (ins.get("mbps"), au.get("mean_db"), au.get("max_db"), ins.get("frame_diff")))
    if res.get("gen_seconds"):
        L.append("生成 %.1f 分（H3 Studio · job %s）" % (res["gen_seconds"] / 60, job.get("id")))
    refs = (p.get("images") or []) + (p.get("videos") or [])
    if refs:
        L.append("参照: " + ", ".join(refs))
    b = p.get("brief")
    if isinstance(b, dict):
        for k, label in (("place", "場所と時間"), ("motion", "動き"), ("camera", "カメラ"), ("dialogue", "セリフ")):
            if b.get(k):
                L.append("%s: %s" % (label, b[k]))
    if p.get("prompt"):
        L.append("")
        L.append(p["prompt"].strip())
    return "\n".join(L)


def _tags(cfg: dict, job: dict) -> list[str]:
    p = job.get("params") or {}
    t = ["MiniMax-H3", "H3 Studio"]
    if p.get("project"):
        t.append(p["project"])
    if p.get("shot_id"):
        t.append(p["shot_id"])
    t.append("本番" if p.get("mode") == "final" else "プレビュー")
    if p.get("ratio"):
        t.append(p["ratio"])
    if p.get("h3_mode"):
        t.append(p["h3_mode"])
    t += [x for x in (_cfg(cfg).get("extra_tags") or []) if x]
    seen, out = set(), []
    for x in t:
        if x not in seen:
            seen.add(x); out.append(x)
    return out


def _display_name(job: dict) -> str:
    p = job.get("params") or {}
    parts = [p.get("project") or "H3", p.get("shot_id") or "", "本番" if p.get("mode") == "final" else "プレビュー",
             "%sx%s" % (p.get("width"), p.get("height")), "seed%s" % p.get("seed")]
    return " ".join(x for x in parts if x)


def send_job(cfg: dict, job: dict, project: dict | None = None, folder_id: str | None = None,
             contact_sheet: bool | None = None) -> dict:
    """
    ジョブの結果を Eagle に送る。**音付きの1本だけ**（無音版は送らない）。
    contact_sheet が真ならコンタクトシートも同じフォルダに入れる。
    """
    res = job.get("result") or {}
    vid = res.get("video") or {}
    path = vid.get("path")
    if not path or not os.path.isfile(path):
        raise FileNotFoundError("動画が見つかりません: %s" % path)
    ann = _annotation(job, project)
    tags = _tags(cfg, job)
    name = _display_name(job)
    sent = [send_file(cfg, path, name=name, tags=tags, annotation=ann, folder_id=folder_id)]
    if contact_sheet is None:
        contact_sheet = bool(_cfg(cfg).get("send_contact_sheet"))
    if contact_sheet:
        from . import comfy
        cs = os.path.join(comfy.JOBS_DIR, job.get("id", ""), "contact.jpg")
        if os.path.isfile(cs):
            sent.append(send_file(cfg, cs, name=name + " コンタクトシート",
                                  tags=tags + ["コンタクトシート"], annotation=ann, folder_id=folder_id))
    return {"ok": True, "sent": sent, "count": len(sent)}


def maybe_auto_send(cfg: dict, job: dict) -> dict | None:
    """
    生成完了時の自動送信。config.eagle.auto が "final"（本番のみ）か "all"（全部）のとき送る。
    失敗しても生成は成功のままにする（呼び出し側でログに出す）。
    """
    if not enabled(cfg):
        return None
    auto = (_cfg(cfg).get("auto") or "off").lower()
    if auto == "off":
        return None
    mode = (job.get("params") or {}).get("mode")
    if auto == "final" and mode != "final":
        return None
    return send_job(cfg, job)
