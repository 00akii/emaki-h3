# -*- coding: utf-8 -*-
"""
tools/sync_vendor.py — 正本（../ローカルLLM/ ../SAM3/）から app/vendor/ へ部品をコピーする。

  vendor/ は「配布用のコピー」で、正本ではない。正本を直したらこれを走らせる。
  公開物（vendor/ を同梱した zip や clone）には正本が無いので、その場合は何もせず正常終了する。

      python tools/sync_vendor.py            # 差分だけコピー
      python tools/sync_vendor.py --check    # 差分があれば非 0（コピーしない）
      python tools/sync_vendor.py --all      # 同じ内容でも上書き

  MAP に無いものは同期しない。vendor に何が要るかはここが唯一の定義。
"""
from __future__ import annotations
import hashlib, os, shutil, sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(APP_DIR, "vendor")
ROOT = os.path.dirname(APP_DIR)          # …/MiniMax-H3
LLM = os.path.join(ROOT, "ローカルLLM")
SAM3 = os.path.join(ROOT, "SAM3")

# vendor 内の相対パス → 正本の絶対パス
MAP = {
    "h3gen.py":            os.path.join(LLM, "h3gen.py"),
    "h3lint.py":           os.path.join(LLM, "h3lint.py"),
    "system_h3.txt":       os.path.join(LLM, "system_h3.txt"),
    "system_h3_notes.txt": os.path.join(LLM, "system_h3_notes.txt"),
    "sweep.json":          os.path.join(LLM, "sweep.json"),
    "vlm.py":              os.path.join(LLM, "vlm_test.py"),      # 名前が違う（正本は vlm_test.py）
    "modes/A_おまかせ.txt":  os.path.join(LLM, "modes", "A_おまかせ.txt"),
    "modes/B_推奨.txt":      os.path.join(LLM, "modes", "B_推奨.txt"),
    "modes/C_詳細.txt":      os.path.join(LLM, "modes", "C_詳細.txt"),
    "modes/カメラ選択肢.txt": os.path.join(LLM, "modes", "カメラ選択肢.txt"),
    # sam3_run.py / sam3_pick.py は同梱しない。どこからも import されない単体 CLI で、
    # INPUT_DIR に開発機のパスを持っているため。SAM3 の作業では ../SAM3/ の正本を直接使う
    "check_cut.py":        os.path.join(SAM3, "check_cut.py"),
}


def _sha(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    check = "--check" in sys.argv
    force = "--all" in sys.argv
    if not os.path.isdir(LLM) and not os.path.isdir(SAM3):
        print("正本（%s / %s）が無いので同期しません。配布物ではこれが正常です。" % (LLM, SAM3))
        return 0

    copied, same, missing_src, missing_dst = [], [], [], []
    for rel, src in sorted(MAP.items()):
        dst = os.path.join(VENDOR, rel.replace("/", os.sep))
        hs, hd = _sha(src), _sha(dst)
        if hs is None:
            missing_src.append((rel, src))
            if hd is None:
                missing_dst.append(rel)
            continue
        if hd == hs and not force:
            same.append(rel)
            continue
        if check:
            copied.append(rel)          # --check では「ずれている」印として集めるだけ
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    for rel, src in missing_src:
        print("!! 正本が見つからない: %s ← %s" % (rel, src))
    for rel in missing_dst:
        print("!! vendor にも正本にも無い: %s" % rel)
    print("一致 %d / %s %d 件" % (len(same), "ずれ" if check else "コピー", len(copied)))
    for rel in copied:
        print("   %s %s" % ("≠" if check else "→", rel))

    if missing_dst:
        return 2
    if check and copied:
        print("`python tools/sync_vendor.py` で更新してください。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
