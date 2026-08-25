# -*- coding: utf-8 -*-
"""
tools/make_example_config.py — h3studio.config.DEFAULTS から config.example.json を起こす。

  DEFAULTS にキーを足したら、これを走らせて雛形を更新する（手で書くと必ずずれる）。
  パス系だけ「よその PC でありそうな値」に差し替える。開発機の実値は書かない。

      python tools/make_example_config.py           # 書き出し
      python tools/make_example_config.py --check   # ずれていたら非 0 で終了（CI 用）
"""
from __future__ import annotations
import json, os, sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)
from h3studio import config as C  # noqa: E402

# 雛形に出す値。DEFAULTS が空文字（＝各自の環境で埋めるもの）のキーだけここで埋める
EXAMPLE_PATHS = {
    "comfy_input_dir": "C:/ComfyUI/input",
    "comfy_output_dir": "C:/ComfyUI/output",
    "workflow_json": "C:/ComfyUI/user/default/workflows/video_minimax_h3_i2v.json",
    "prompt_txt": "C:/ComfyUI/output/MiniMax-H3/prompt.txt",
    "archive_dir": "C:/ComfyUI/output/MiniMax-H3/archive",
    "raw_dir": "C:/art/reference",
}


def build() -> dict:
    cfg = json.loads(json.dumps(C.DEFAULTS))
    cfg.update(EXAMPLE_PATHS)
    return cfg


def main() -> int:
    want = json.dumps(build(), ensure_ascii=False, indent=2) + "\n"
    path = C.EXAMPLE_PATH
    have = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            have = f.read()
    if "--check" in sys.argv:
        if have != want:
            print("config.example.json が DEFAULTS とずれています。"
                  "`python tools/make_example_config.py` を実行してください。")
            return 1
        print("config.example.json は DEFAULTS と一致しています。")
        return 0
    # newline を明示する。既定だと Windows で CRLF になり、リポジトリ（.gitattributes で LF）と
    # 中身が同じなのに毎回ずれて見える（同期の取りこぼし検査が誤検出する）
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(want)
    print("書き出し: %s (%d キー)" % (path, len(build())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
