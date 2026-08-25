# -*- coding: utf-8 -*-
"""
config.py — 環境依存をすべて config.json に外出しする。コードにパスを書かない。

  読み込み順: app/config.json → 無ければ config.example.json の値（存在チェックで赤く出る）
  起動時に check() を呼び、無いもの・届かないものを画面の「設定」に出す。
"""
from __future__ import annotations
import json, os, shutil, subprocess, urllib.request

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(APP_DIR, "vendor")
PROJECTS_DIR = os.path.join(APP_DIR, "projects")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
EXAMPLE_PATH = os.path.join(APP_DIR, "config.example.json")

DEFAULTS = {
    "comfy_url": "http://127.0.0.1:8189",
    "comfy_input_dir": "",
    "comfy_output_dir": "",
    "workflow_json": "",
    "prompt_txt": "",
    "archive_dir": "",
    "raw_dir": "",
    "lmstudio_url": "http://localhost:1234",
    "lmstudio_model": "",
    "lms_cli": "lms",
    "ffmpeg": "ffmpeg",
    "port": 8765,
    # LLM バックエンド。lmstudio（既定、GPU を H3 と取り合う）か openai_compat（クラウド、GPU 切替が不要になる）
    "llm": {
        "backend": "lmstudio",
        "openai_compat": {
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "H3STUDIO_LLM_KEY",
            "model": "",
        },
    },
    # 動画生成バックエンド。v1 は comfy のみ。minimax_api は v2 で検証してから
    "video": {"backend": "comfy"},
    # 実測で決めた生成既定値。環境が違えば変わりうるので設定に出す
    "gen": {
        # filename_prefix は ComfyUI の output からの相対（サブフォルダ可）。本番は手動運用と同じ連番に続ける
        "preview": {"width": 608, "height": 352, "filename_prefix": "MiniMax-H3/preview/MiniMax_H3"},
        "final": {"width": 1344, "height": 768, "filename_prefix": "MiniMax_H3"},
        "steps": 6,
        "sampler": "euler",
        "scheduler": "normal",
        "max_duration": 10,
        "default_duration": 8,
        "ref_video": {"width": 608, "height": 352, "frame_load_cap": 97, "skip_first_frames": 48},
        # 実測: 前回の UNET+VAE が VRAM に残ったままサンプラーに入るとブロック単位で重みを出し入れして 10 倍以上遅い。
        # 投入前に /free で全部降ろし、さらに参照エンコード後・サンプリング前に VRAM 解放ノードを挟む（無ければ /free のみ）
        "free_vram_before": True,
        # free_cache_before=True: 実行キャッシュも消して H3 ノード（参照エンコード）を毎回走らせる。CPU のテキストエンコード ~170s が
        # 毎回かかるが、サンプラー直前に purge → UNET 読み込みという「速いパターン」（step1 15s）を確実に再現できる。
        # False にすると seed 違いの再生成で参照エンコードは省けるが、動的 VRAM ローダーが空き 20GB を見て UNET を全部載せ、
        # step1（モデルの読み込み）は同じ設定でも 6 秒〜8 分とばらつく。原因は特定できていない（設計書 §9a 追記2・追記3）。
        # **`--reserve-vram 3` は効かなかった**（2026-08-25 実測15本。詰まりも発散も防がない）ので、False に落とす根拠にはならない
        "free_cache_before": True,
        "purge_node": "LayerUtility: PurgeVRAM V2",
        # 出来上がった動画の右下に、絵巻H3 のアイコンを薄く焼き込む（ffmpeg の後処理。ワークフローは触らない）。
        # **既定は off。**他人の動画に黙って印を付けない。使う人が自分で on にする
        "watermark": {
            "enabled": False,
            "image": "",        # 空なら static/watermark.png（灰色のアイコン＋「絵巻」）
            "opacity": 0.10,    # 0.0〜1.0。主張ではなく印なので薄い
            "scale": 0.10,      # 動画の幅に対する透かしの幅（横長なので高さは 1/4 程度）
            "margin": 0.02,     # 動画の幅に対する余白
            "crf": 12,          # 焼き込みは再エンコードになる。元の出力と同じ値
        },
        # プロンプト生成の直前に ComfyUI の VRAM を空ける。**空けてから LLM を載せる**のが重要で、
        # 逆順（ComfyUI が抱えたまま load）だと LM Studio は GPU に半分しか載せず残りを CPU に置く。
        # `lms ps` の SIZE は満額を表示するので気づけない。実測（2026-08-23）: プロンプト生成 45秒 → 218秒。
        # free_comfy_min_gb: ComfyUI の保持量がこれ以上なら降ろす。**既に正しく載っているなら何もしない**ので、
        # ここに来るのは LLM を実際に載せ直すときだけ（無用な解放にはならない）。
        # `/system_stats` の vram_free は ComfyUI 自身の torch 会計でデバイス全体の空きではないため判定に使えない（実測）
        "free_comfy_before_llm": True,
        "free_comfy_min_gb": 2,
        # vram_mode: VRAM の空け方をまとめて切り替える。上の free_vram_before / free_cache_before / purge_node は
        #   share のときだけ使われる。
        #   "share"    … 既定。/free で全部降ろし、purge ノードも挟む。LM Studio と GPU を取り合う前提の安全側。
        #                 1本ごとに UNET(20GB) を読み直すので、その分（15〜250秒）が毎回かかる。
        #   "resident" … /free も purge も一切しない。UNET を載せっぱなしにして読み直しを丸ごと省く。
        #                 同日のカメラ検証（15ノード構成の t2va・解放も purge も無し）は 3分40秒/本 で 61本を安定して回した。
        #                 「常駐そのものが遅い」のではなく、最悪手は「全部解放 → 空いた GPU へ再読込」だった。
        #   "auto"     … 直前のジョブが同じモデル構成で成功し、かつ今回 LM Studio を降ろしていない（＝間に GPU を
        #                 取られていない）ときだけ resident、それ以外は share。seed 違いの連投で効く。
        # ⚠ resident / auto はまだ実測していない（引き継ぎ書 §7 の【要検討】）。既定は検証済みの share のまま。
        "vram_mode": "share",
    },
    "lmstudio_load": {"context_length": 16384, "parallel": 1, "speculative_mtp": True, "ttl": 3600},
    # 切り抜き（段6b）。checkpoint は ComfyUI の models/checkpoints からの相対名。
    # 空なら一覧から sam3.1_multiplex_fp16 に近いものを自動で選ぶ（検証したのはこの重みだけ）。
    # refine は 1 固定（2 以上でマスクが劣化する・実測）。text の `:N` は個数の上限で、`:1` は本体のバグで使えない
    "sam3": {"checkpoint": "", "text": "person:5", "threshold": 0.5, "refine": 1, "crop_margin": 40},
    # 出来上がった動画を Eagle に送る。auto: off / final（本番のみ）/ all。
    # ComfyUI の SendToEagleVideo は使わない（無音版と音付き版の2本が送られるため。eagle.py の冒頭を参照）
    "eagle": {"enabled": False, "url": "http://localhost:41595", "token": "",
              "folder_id": "", "folder_name": "", "auto": "off",
              "send_contact_sheet": False, "extra_tags": []},
}


def _deep_update(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_PATH
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            _deep_update(cfg, json.load(f))
    cfg["_source"] = path
    return cfg


def save(cfg: dict) -> None:
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def _http_ok(url: str, timeout=3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def check(cfg: dict) -> list[dict]:
    """設定の存在・到達性を検査して [{key, ok, detail}] を返す。画面の「設定」に出す。"""
    out = []

    def add(key, ok, detail):
        out.append({"key": key, "ok": bool(ok), "detail": detail})

    for key in ("comfy_input_dir", "comfy_output_dir", "archive_dir", "raw_dir"):
        p = cfg.get(key) or ""
        add(key, p and os.path.isdir(p), p or "（未設定）")
    for key in ("workflow_json",):
        p = cfg.get(key) or ""
        add(key, p and os.path.isfile(p), p or "（未設定）")
    p = cfg.get("prompt_txt") or ""
    add("prompt_txt", p and os.path.isdir(os.path.dirname(p)), p or "（未設定）")

    add("comfy_url", _http_ok(cfg["comfy_url"].rstrip("/") + "/system_stats"), cfg["comfy_url"])
    add("lmstudio_url", _http_ok(cfg["lmstudio_url"].rstrip("/") + "/v1/models"), cfg["lmstudio_url"])
    add("lms_cli", shutil.which(cfg.get("lms_cli") or "lms") is not None, cfg.get("lms_cli"))
    add("ffmpeg", shutil.which(cfg.get("ffmpeg") or "ffmpeg") is not None, cfg.get("ffmpeg"))
    llm = cfg.get("llm", {})
    if llm.get("backend") == "openai_compat":
        oc = llm.get("openai_compat", {})
        key_env = oc.get("api_key_env") or "H3STUDIO_LLM_KEY"
        add("llm.openai_compat.api_key", bool(os.environ.get(key_env)), "環境変数 %s" % key_env)
        add("llm.openai_compat.model", bool(oc.get("model")), oc.get("model") or "（未選択）")
        add("llm.openai_compat.base_url", _http_ok((oc.get("base_url") or "").rstrip("/") + "/models", 5) or True,
            oc.get("base_url") or "（未設定）")
    else:
        add("lmstudio_model", bool(cfg.get("lmstudio_model")), cfg.get("lmstudio_model") or "（未選択）")
    add("vendor/system_h3.txt", os.path.isfile(os.path.join(VENDOR_DIR, "system_h3.txt")), VENDOR_DIR)
    return out


if __name__ == "__main__":
    # python -m h3studio.config で設定の自己診断。公開物にはここ以外にパスを書かない
    cfg = load()
    print("設定の読み込み元:", cfg.get("_source"))
    for r in check(cfg):
        print("%-26s %s %s" % (r["key"], "OK  " if r["ok"] else "NG  ", r["detail"]))
