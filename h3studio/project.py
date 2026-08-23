# -*- coding: utf-8 -*-
"""
project.py — 作品単位の設定（スタイル宣言・キャラ・参照素材・既定値・ショット履歴）を
app/projects/<name>/project.json に読み書きする。
"""
from __future__ import annotations
import json, os, re, time
from . import config

SAFE = re.compile(r"[\\/:*?\"<>|]+")

EMPTY = {
    "name": "",
    "style": "2D-animated, hand-drawn 2D anime with soft thin inked outlines, flat two-tone cel shading, a muted palette, and watercolor-textured painted backgrounds",
    "subjects": [],
    "ref_videos": [],
    "defaults": {"duration": 8, "ratio": "16:9", "music": "N/A"},
    "comfy": {"seed": 1},
    "shots": [],
}


def _dir(name: str) -> str:
    return os.path.join(config.PROJECTS_DIR, SAFE.sub("_", name).strip())


def _path(name: str) -> str:
    return os.path.join(_dir(name), "project.json")


def list_projects() -> list[dict]:
    out = []
    if not os.path.isdir(config.PROJECTS_DIR):
        return out
    for d in sorted(os.listdir(config.PROJECTS_DIR)):
        p = os.path.join(config.PROJECTS_DIR, d, "project.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    j = json.load(f)
                out.append({"name": j.get("name") or d, "shots": len(j.get("shots", [])),
                            "updated": j.get("updated", "")})
            except Exception:
                out.append({"name": d, "shots": 0, "updated": "", "broken": True})
    return out


def load(name: str) -> dict:
    p = _path(name)
    if not os.path.isfile(p):
        raise FileNotFoundError(name)
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    base = json.loads(json.dumps(EMPTY))
    base.update(j)
    base["name"] = base.get("name") or name
    return base


def save(proj: dict) -> dict:
    name = proj.get("name") or "untitled"
    os.makedirs(_dir(name), exist_ok=True)
    proj["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(_path(name), "w", encoding="utf-8") as f:
        json.dump(proj, f, ensure_ascii=False, indent=2)
    return proj


def create(name: str) -> dict:
    proj = json.loads(json.dumps(EMPTY))
    proj["name"] = name
    return save(proj)


def add_shot(proj: dict, shot: dict) -> dict:
    """生成履歴を1件追加する。shot = {id, brief, prompt, lint, videos, mode, seed, note, ...}"""
    shot = dict(shot)
    shot.setdefault("created", time.strftime("%Y-%m-%dT%H:%M:%S"))
    proj.setdefault("shots", []).append(shot)
    return save(proj)


def next_shot_id(proj: dict) -> str:
    nums = []
    for s in proj.get("shots", []):
        m = re.match(r"S(\d+)", str(s.get("id", "")))
        if m:
            nums.append(int(m.group(1)))
    return "S%02d" % ((max(nums) + 1) if nums else 1)
