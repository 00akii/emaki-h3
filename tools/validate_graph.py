# -*- coding: utf-8 -*-
"""
tools/validate_graph.py — 組み立てた API グラフを ComfyUI の /object_info と突き合わせる。

  GPU を使わずに「投げたら通るか」だけ見る自己テスト。ノードの存在・必須入力の充足・
  リンク先ノードの存在・出力インデックスの範囲を検査する。
  VideoHelperSuite がある環境と無い環境（本体の CreateVideo + SaveVideo）の両方を組む。

      python tools/validate_graph.py

  ComfyUI が起動していないと何も確認できないので、その場合はスキップして 0 で終わる。
"""
from __future__ import annotations
import json, os, sys, urllib.parse, urllib.request

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)
from h3studio import config, comfy  # noqa: E402

_INFO: dict[str, dict | None] = {}

# /object_info に出ないが実際には受け付ける入力。
# VHS_VideoCombine は選んだ format の定義（video_formats/*.json）に応じて widget を生やすので、
# pix_fmt / crf は宣言に現れないが **kwargs で届いて効く（手動ワークフローと同じ値を渡している）
DYNAMIC_EXTRA = {
    "VHS_VideoCombine": {"pix_fmt", "crf", "save_metadata", "trim_to_audio"},
}


def info(cfg: dict, cls: str) -> dict | None:
    if cls not in _INFO:
        try:
            url = cfg["comfy_url"].rstrip("/") + "/object_info/" + urllib.parse.quote(cls)
            with urllib.request.urlopen(url, timeout=10) as r:
                _INFO[cls] = json.loads(r.read().decode("utf-8")).get(cls)
        except Exception:
            _INFO[cls] = None
    return _INFO[cls]


def validate(cfg: dict, graph: dict, label: str) -> list[str]:
    errs = []
    for nid, node in graph.items():
        cls = node["class_type"]
        d = info(cfg, cls)
        if not d:
            errs.append("%s: ノード %s（id %s）が ComfyUI にありません" % (label, cls, nid))
            continue
        spec = d.get("input", {}) or {}
        required = spec.get("required", {}) or {}
        known = set(required) | set(spec.get("optional", {}) or {})
        given = node.get("inputs", {}) or {}
        # Autogrow 入力（ref_images.ref_image_0 など）は接頭辞で照合する
        for k in required:
            if k in given:
                continue
            if any(g == k or g.startswith(k + ".") for g in given):
                continue
            errs.append("%s: %s(id %s) の必須入力 %s が無い" % (label, cls, nid, k))
        for k, v in given.items():
            base = k.split(".")[0]
            if k not in known and base not in known and k not in DYNAMIC_EXTRA.get(cls, ()):
                errs.append("%s: %s(id %s) に知らない入力 %s" % (label, cls, nid, k))
            if isinstance(v, list) and len(v) == 2 and isinstance(v[1], int):
                up = graph.get(v[0])
                if up is None:
                    errs.append("%s: %s(id %s).%s のリンク先 %s が無い" % (label, cls, nid, k, v[0]))
                else:
                    ud = info(cfg, up["class_type"])
                    n_out = len(ud.get("output", []) or []) if ud else 0
                    if ud and v[1] >= n_out:
                        errs.append("%s: %s(id %s).%s が %s の出力 %d 番を指すが出力は %d 個"
                                    % (label, cls, nid, k, up["class_type"], v[1], n_out))
    return errs


def main() -> int:
    cfg = config.load()
    if not comfy.comfy_up(cfg):
        print("ComfyUI に届かないので検査をスキップします（%s）" % cfg["comfy_url"])
        return 0

    vals = comfy.workflow_values(cfg)["values"]
    purge = (cfg.get("gen") or {}).get("purge_node") or None
    if purge and not comfy.node_available(cfg, purge):
        purge = None

    cases = [
        ("VHS あり・画像2枚", "VHS_VideoCombine", ["a.png", "b.png"], []),
        ("VHS あり・画像1枚＋動画1本", "VHS_VideoCombine", ["a.png"], ["c.mp4"]),
        ("VHS なし（本体のみ）・画像2枚", "SaveVideo", ["a.png", "b.png"], []),
    ]
    total = 0
    for label, out_node, imgs, vids in cases:
        if out_node == "VHS_VideoCombine" and not comfy.node_available(cfg, "VHS_VideoCombine"):
            print("--", label, ": VHS が無い環境なのでスキップ"); continue
        if vids and not comfy.node_available(cfg, "VHS_LoadVideo"):
            print("--", label, ": VHS_LoadVideo が無いのでスキップ"); continue
        g = comfy.build_prompt(cfg, "test prompt", imgs, vids, 608, 352, 192, 1,
                               "MiniMax-H3/_validate/x", values=vals, purge_node=purge, out_node=out_node)
        errs = validate(cfg, g, label)
        total += len(errs)
        print("-- %-28s ノード %2d / %s" % (label, len(g), "OK" if not errs else "%d 件の問題" % len(errs)))
        for e in errs:
            print("     ", e)
    print("合計 %d 件" % total)
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
