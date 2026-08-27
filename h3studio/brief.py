# -*- coding: utf-8 -*-
"""
brief.py — 画面の入力（モード A/B/C）を system_h3.txt に渡すブリーフ文字列に組み立てる。

モードB（推奨）の4欄: place / motion / camera / dialogue
プロジェクト側の情報（style / subjects / ref_videos / defaults）を合流させ、
system_h3 が読む MODE/DURATION/RATIO/STYLE/REFS/SHOT/FRAMING/CAMERA/TEXT/DIALOGUE/SOUND/MUSIC を書く。
"""
from __future__ import annotations
import re
from . import textcheck

# 「動き」欄の段階の区切り。**打ちやすい記号を何でも受け付けて、LLM には " → " に正規化して渡す。**
# 「→」はキーボードに無くて面倒、という指摘（2026-08-23）。矢印に機能的な依存は無く
# （brief.py は分割せずそのまま埋めていた／system_h3.txt にも h3lint にも矢印の指定は無い）、
# 正規化しても LLM に届く形は今までと同じ。
#
# 上から順に試し、**最初に2つ以上へ分かれたものを採用する**。だから
# 「机に座る 3-5歩あるく」は空白で2段階に割れ、数値範囲のハイフンは巻き込まない。
# 「ー」（長音）は絶対に区切りにしない。語尾を壊すため。
_SEPARATORS = [
    (re.compile(r"\s*(?:→|⇒|=>|->)\s*"), "矢印"),
    (re.compile(r"[\r\n]+"), "改行"),
    (re.compile(r"\s*[、，,;；]\s*"), "読点・カンマ"),
    (re.compile(r"[\s　]+[-－][\s　]+"), "ハイフン"),
    (re.compile(r"　+"), "全角スペース"),
    (re.compile(r"[ \t]+"), "スペース"),
]


def split_steps(text: str) -> list[str]:
    """「動き」欄を段階のリストにする。区切りが無ければ1段階として返す。"""
    t = (text or "").strip()
    if not t:
        return []
    for pat, _name in _SEPARATORS:
        parts = [p.strip() for p in pat.split(t) if p.strip()]
        if len(parts) > 1:
            return parts
    return [t]


def separator_used(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    for pat, name in _SEPARATORS:
        if len([p for p in pat.split(t) if p.strip()]) > 1:
            return name
    return "区切りなし"


def normalize_steps(text: str) -> str:
    """LLM に渡す形（" → " 連結）に直す。"""
    steps = split_steps(text)
    return " → ".join(steps)


CAMERA_PRESETS = [
    "顔が画面の上半分を占めるまで寄る",
    "胸から上が入るまで寄る",
    "手元・小物に落ちるまで寄る",
    "目のアップになるまで寄る",
    "全身が入るまで引く",
    "場所全体が見えるまで引く",
    "遠景に人物が小さく残るまで引く",
    "横に流して画面の反対側の相手が入るまで",
    "足元から顔まで舐め上げる",
    "動かさない（構図を保つ）",
]

# 終端（CAMERA_PRESETS）の各項目を、判定用に「種別」と「到達する画角」に落とす。
# 画角の段階: 0=ドアップ 1=顔 2=胸から上 3=腰から上 6=全身 7=場所全体 8=遠景
CAMERA_END = {
    "顔が画面の上半分を占めるまで寄る":       ("tighten", 1),
    "胸から上が入るまで寄る":                 ("tighten", 2),
    "手元・小物に落ちるまで寄る":             ("tighten", 0),
    "目のアップになるまで寄る":               ("tighten", 0),
    "全身が入るまで引く":                     ("widen", 6),
    "場所全体が見えるまで引く":               ("widen", 7),
    "遠景に人物が小さく残るまで引く":         ("widen", 8),
    "横に流して画面の反対側の相手が入るまで": ("lateral", None),
    "足元から顔まで舐め上げる":               ("tilt", None),
    "動かさない（構図を保つ）":               ("static", None),
}

# 開始の構図（軸1・新設）。2026-08-23 のカメラ検証（61本・ブラインド判定）で
# 効いたものだけを載せる。size は画角の段階（上の CAMERA_END と同じ目盛り）。
#
# 2026-08-27 訂正: ダッチアングル・真俯瞰・広角は「効かない」としていたが誤りだった。
# 当時の検証は**カメラ節だけを詳しく書き、本文は水平・標準レンズの世界のまま**だったため負けていた
# （サイズで発見した ×9→×0 の罠と同型。本文が指定と矛盾すると指定が負ける）。
# 本文ごとその効果に矛盾しない世界として描写したところ、3件とも効いた（各2seed・対照実験つき）。
# → FRAMING_TILT_NOTE 以下で「本文を書き換える」形で入れてある。
#
# 載せていないもの: 望遠の圧縮（本文ごと書き換えた徹底検証でも 2/2 で通常の遠近。本物の×）。
# 選んでも変わらないものは置かない。
#
# ⚠ 傾き・広角・真俯瞰は「効く」が「量と向きは制御できない」。15度と書いても大きく傾き、
#   時計回りと書いても逆に回ることがある。だから度数・強さの選択肢は作らない（二値にとどめる）。
FRAMING_PRESETS = [
    # サイズ — プロンプトの描写が部屋全体を描写していると負ける。寄りで動かない画は build() が本文を絞る
    {"label": "手元や目のドアップから始まる",   "group": "サイズ", "size": 0},
    {"label": "顔のクローズアップから始まる",   "group": "サイズ", "size": 1},
    {"label": "胸から上から始まる",             "group": "サイズ", "size": 2},
    {"label": "腰から上から始まる",             "group": "サイズ", "size": 3},
    {"label": "全身が入る画から始まる",         "group": "サイズ", "size": 6},
    {"label": "人物が小さく見える引きから始まる", "group": "サイズ", "size": 8},
    # 高さ — アイレベル・ハイ・ローは堅い
    {"label": "目線の高さから始まる",           "group": "高さ"},
    {"label": "見下ろすハイアングルから始まる", "group": "高さ"},
    {"label": "見上げるローアングルから始まる", "group": "高さ"},
    # 視点 — 正面・真横・背面は ×0。斜め45度だけ4件中2件（正対か真横に寄る）
    {"label": "正面から始まる",                 "group": "視点"},
    {"label": "真横から始まる",                 "group": "視点"},
    {"label": "背中側から始まる",               "group": "視点"},
    {"label": "斜め45度から始まる",             "group": "視点", "note": "不安定（正対か真横に寄ることがある）"},
    # 質感
    {"label": "背景が大きくボケた画から始まる", "group": "質感"},
    {"label": "魚眼レンズの画から始まる",       "group": "特殊"},
    # 2026-08-27 追加。本文ごと書き換えないと効かない（下の NOTE が入力を差し替える）。
    # 量・向きは制御できないので「かける／かけない」の二値だけ。
    {"label": "画面が斜めに傾いた構図から始まる", "group": "特殊",
     "note": "傾く向きと角度は選べない（大きく傾くことがある）"},
    {"label": "広角レンズの誇張された遠近から始まる", "group": "特殊",
     "note": "誇張の強さは選べない（魚眼に近くなることがある）"},
    {"label": "真上から見下ろす構図から始まる", "group": "特殊",
     "note": "壁が写らない真俯瞰になる"},
]

# 上の3つを選んだときにブリーフへ足す制約。FRAMING_FIX_NOTE と同じ流儀で、
# 「その効果を書け」ではなく「その効果と矛盾しない世界を描写しろ」と、書く対象を差し替える。
# 実測（2026-08-27・各2seed・対照実験つき）: カメラ節だけ詳しくしても効かず、
# 本文の空間描写をこの形に変えて初めて効いた。
FRAMING_WORLD_NOTE = {
    "画面が斜めに傾いた構図から始まる":
        "場面全体を、傾いた画面ごしに見えるものとして描写する。水平線と床と壁の境目は斜めに走り、"
        "窓や柱の垂直の縁は画面の辺に対して傾き、窓から差す光は長方形ではなく平行四辺形として床に落ちる。"
        "人物は世界に対しては直立しているが、画面の辺に対しては斜めに立って見える。"
        "「傾いている」という言葉ではなく、傾いた結果そう見えるものを書く。",
    "広角レンズの誇張された遠近から始まる":
        "場面全体を、広角レンズごしに見えるものとして描写する。手前にあるもの（手・小物）は不釣り合いに大きく、"
        "奥の壁や人物は異常に遠く小さく見え、窓や柱の直線は画面の縁に近づくほど外側へ膨らむ。"
        "床と天井は手前から奥へ急激にすぼまる。「広角」という言葉ではなく、広角で見た結果そう見えるものを書く。",
    "真上から見下ろす構図から始まる":
        "場面全体を、真上から見下ろした床の平面として描写する。人物は頭頂と肩の丸い塊として見え、"
        "床が画面のほとんどを占め、壁面は一切見えない。家具や小物は上から見た輪郭で書く。"
        "「真俯瞰」という言葉ではなく、真上から見た結果そう見えるものを書く。",
}
_FRAMING_SIZE = {f["label"]: f["size"] for f in FRAMING_PRESETS if "size" in f}

# 自由記述のときの拾い方（プリセット外でも、寄り・引きの語があれば判定に使う）
_SIZE_WORDS = [
    (("ドアップ", "超クローズアップ", "目のアップ", "手元のアップ", "指先"), 0),
    (("クローズアップ", "顔のアップ", "顔が画面"), 1),
    (("胸から上", "バストアップ", "バストショット"), 2),
    (("腰から上", "ミディアム"), 3),
    (("全身",), 6),
    (("引き", "全景", "場所全体", "ワイド"), 7),
    (("遠景", "小さく"), 8),
]


def framing_size(text: str):
    """開始の構図から画角の段階を取る。分からなければ None（＝判定に使わない）。"""
    t = (text or "").strip()
    if not t:
        return None
    if t in _FRAMING_SIZE:
        return _FRAMING_SIZE[t]
    for words, size in _SIZE_WORDS:
        if any(w in t for w in words):
            return size
    return None


def camera_kind(text: str):
    """終端の指示を (種別, 到達する画角) に落とす。空欄は静止（system_h3 の既定と同じ）。"""
    t = (text or "").strip()
    if not t:
        return ("static", None)
    if t in CAMERA_END:
        return CAMERA_END[t]
    if any(w in t for w in ("動かさない", "固定", "静止", "構図を保つ")):
        return ("static", None)
    if "寄る" in t or "寄って" in t or "アップまで" in t:
        return ("tighten", framing_size(t))
    if "引く" in t or "引いて" in t:
        return ("widen", framing_size(t))
    if "舐め" in t or "ティルト" in t:
        return ("tilt", None)
    if "横に" in t or "パン" in t or "流して" in t:
        return ("lateral", None)
    return ("unknown", None)


def check_framing(framing: str, camera: str) -> dict:
    """
    開始の構図と終端の指示の組み合わせを判定する。実測に基づく分岐:
      - 開始が寄り（胸から上より寄る）でなければ素通り。高さ・視点は本文と衝突しない（viewpoint ×0 / height 中間値は堅い）
      - 開始が寄りで、終端が静止・さらに寄る・横移動 → 修正。プロンプトの描写を画角内の要素に絞らないと引きに負ける（shot_size ×9 → ×0）
      - 開始が寄りで、終端が引き・舐め上げ → 警告。プロンプトの描写を絞ると終端の描写が足りなくなる。逃げ場がない
      - 開始が引き（全身以上）で、終端が舐め上げ → 警告。全身が既に画面内なので動く意味が無く、静止か寄りに化ける（段D 0/2）
    返り値: {"action": "pass"|"fix"|"warn", "reason": str, "size": int|None, "camera": (kind, end)}
    """
    size = framing_size(framing)
    kind, end = camera_kind(camera)
    base = {"size": size, "camera": [kind, end]}
    if kind == "tilt" and size is not None and size >= 6:
        # 段D の実測: wide 開始からの「足元から顔まで舐め上げる」は 0/2 で静止か push in に化けた
        # （全身が既に画面内なので動く意味が無い）。開始が足元の寄りなら成立する
        return dict(base, action="warn",
                    reason="開始が引き（全身が画面内）だと「足元から顔まで舐め上げる」は成立しにくい（実測 0/2、静止か寄りに化ける）。開始を足元の寄りにするか、終端を寄り／引きに変えるのが安全")
    if size is None or size > 2:
        return dict(base, action="pass", reason="")
    if kind in ("static", "lateral") or (kind == "tighten" and (end is None or end <= size)):
        return dict(base, action="fix",
                    reason="開始が寄りで、カメラが引かない。プロンプトの描写を画角内の要素（表情・髪・布地・手元・肌に当たる光・背後の面の質感）に絞り、場所の記述は背景のヒントに下げる")
    if kind in ("widen", "tilt") or (kind == "tighten" and end is not None and end > size):
        return dict(base, action="warn",
                    reason="開始が寄りなのにカメラが引く（または足元から舐め上げる）。冒頭を寄りにするにはプロンプトの描写から部屋を消す必要があり、消すと終端に書くものが無くなる。開始を引きにするか、終端を寄りに変えるのが安全")
    return dict(base, action="pass", reason="終端の指示が読み取れないので手を入れない")


# 修正時にブリーフへ足す制約。除外の指示ではなく「書くべき対象の差し替え」として書く
# （ローカルLLM は除外指示を守りにくい。書く対象を入れ替える方が素直に乗る）。
FRAMING_FIX_NOTE = (
    "この画角に入るものだけを書く。環境の具体物（3つ以上・素材つき）は画角内から取る: "
    "髪の束、布地の織り目、手元の紙や小物、肌に当たる光と影の境、背後の一面の質感。"
    "背後の家具・別の人物・部屋の広がりは書かない。場所は光の色と背後の面の素材としてだけ現れる。"
)


def _refs_block(proj: dict, selected_images: list[str], selected_videos: list[str]) -> str:
    """
    REFS を組む。プロジェクトの subjects[].description をそのまま使う（人が確認済みの記述）。
    選択された画像がどの subject のものかは subjects[].images で引く。
    """
    lines = []
    n_img = 0
    for subj in proj.get("subjects", []):
        imgs = [i for i in subj.get("images", []) if i in selected_images]
        if not imgs:
            continue
        for i, img in enumerate(imgs):
            n_img += 1
            role = subj.get("image_roles", {}).get(img, "")
            desc = subj.get("description", "").strip()
            head = "  参照画像%d: " % n_img
            if i == 0:
                lines.append(head + (desc or "キャラ表") + (("（%s）" % role) if role else ""))
            else:
                lines.append(head + "同じ人物のキャラ表" + (("（%s）" % role) if role else ""))
    # プロジェクトに紐づかない画像（生画像など）
    known = {i for s in proj.get("subjects", []) for i in s.get("images", [])}
    for img in selected_images:
        if img not in known:
            n_img += 1
            lines.append("  参照画像%d: %s（説明なし・背景つきの可能性）" % (n_img, img))
    for k, v in enumerate(selected_videos, 1):
        meta = next((r for r in proj.get("ref_videos", []) if r.get("file") == v), {})
        desc = meta.get("description") or v
        lines.append("  参照動画%d: %s" % (k, desc))
    return "\n".join(lines)


def build(mode: str, fields: dict, proj: dict, selected_images: list[str],
          selected_videos: list[str], duration: int, ratio: str, seed_note: str = "") -> str:
    """
    mode: "A" | "B" | "C"
    fields:
      A: {text}
      B: {place, motion, framing, camera, text, dialogue}   text = 画面内の文字（textcheck.parse の書式）
      C: {raw}  … 全キーをそのまま（そのまま渡す）
    """
    defaults = proj.get("defaults", {})
    has_refs = bool(selected_images or selected_videos)
    h3_mode = "ref2va" if has_refs else "t2va"

    if mode == "C":
        return fields.get("raw", "").strip() + "\n"

    out = []
    out.append("MODE: %s" % h3_mode)
    out.append("DURATION: %d" % duration)
    out.append("RATIO: %s" % ratio)
    if proj.get("style"):
        out.append("STYLE: %s" % proj["style"])
    if has_refs:
        out.append("REFS:")
        out.append(_refs_block(proj, selected_images, selected_videos))

    if mode == "A":
        out.append("SHOT: %s" % fields.get("text", "").strip())
    else:
        place = fields.get("place", "").strip()
        # 区切りは何で書いてもよい（矢印・改行・読点・ハイフン・スペース）。LLM には " → " に揃えて渡す
        motion = normalize_steps(fields.get("motion", ""))
        framing = fields.get("framing", "").strip()
        cam = fields.get("camera", "").strip()
        chk = check_framing(framing, cam)
        if chk["action"] == "fix":
            # 寄りで動かない画。場所は「描写する対象」から「背景の一行ヒント」に降格させ、
            # 書く対象を画角内の要素に差し替える。LLM に除外を守らせるのではなく入力を変える
            shot = "被写体に寄った画。" + (("背景ヒント（画角には入らない・光と面の質感だけ）: %s。" % place) if place else "")
        else:
            shot = place
        if motion:
            shot = (shot + "。" if shot and not shot.endswith("。") else shot) + "動き: " + motion
        out.append("SHOT: %s" % shot)
        if framing:
            # 「効果に矛盾しない世界を描写しろ」の制約（傾き・広角・真俯瞰）。サイズの fix とは別系統で、
            # 両方付くこともある（例: 寄り＋傾き）。どちらも「書く対象の差し替え」なので併記して構わない
            note = FRAMING_FIX_NOTE if chk["action"] == "fix" else ""
            world = FRAMING_WORLD_NOTE.get(framing, "")
            extra = "。".join(x for x in (note, world) if x)
            out.append("FRAMING: %s%s" % (framing, ("。" + extra) if extra else ""))
        if cam:
            out.append("CAMERA: %s" % cam)
        # 画面内の文字。SHOT とは別行にする。場所欄の外にあるので、寄りの画角で場所が「背景ヒント（画角には
        # 入らない）」に降格されても巻き込まれない。文字を載せる物は定義上フレーム内（実測 CU_prop 3/3）
        tx = textcheck.parse(fields.get("text", ""))
        if tx["text"]:
            out.append('TEXT: "%s"%s' % (tx["text"], ("（%sに）" % tx["carrier"]) if tx["carrier"] else ""))
        dia = fields.get("dialogue", "").strip()
        out.append("DIALOGUE: %s" % (dia if dia else "なし"))
    music = fields.get("music") or defaults.get("music") or "N/A"
    out.append("MUSIC: %s" % music)
    return "\n".join(out) + "\n"


def expand_prompt_for_mode_a(text: str) -> list[dict]:
    """モードA の1行を4欄に展開させる LLM メッセージ（brief/expand API 用）。"""
    sys = ("You convert a one-line Japanese idea for a short anime shot into four Japanese fields. "
           "Output ONLY a JSON object with keys: place, motion, camera, dialogue. "
           "place: where, when, and what it is made of (name one material). "
           "motion: 3-5 steps joined by ' → ', ending in a resting state. "
           "camera: where the framing ends up (e.g. 顔が画面の上半分を占めるまで寄る). "
           "dialogue: the spoken line in 「」 with speaker hint, or なし. No other text.")
    return [{"role": "system", "content": sys}, {"role": "user", "content": text}]
