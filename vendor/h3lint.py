# -*- coding: utf-8 -*-
"""
h3lint.py - MiniMax-H3 構造化プロンプトの機械検査。

使い方:
    python h3lint.py プロンプト.txt --mode ref2va --duration 8
    python h3lint.py プロンプト.txt            # モードは中身から自動判定

終了コード: 0 = 合格 / 1 = ERROR あり
"""
from __future__ import annotations
import argparse
import re
import sys
import unicodedata

FIELDS_A = ["integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"]
FIELDS_B = ["subject_definitions", "summary", "retention_analysis",
            "detailed_description", "overall_soundscape", "non_diegetic_music"]

TASK_TYPES = {"keyframe completion", "reference generation", "video editing",
              "video continuation", "audio reuse", "audio reference"}
VIS_MARKERS = {"fully_preserved", "partially_preserved", "attribute_transfer", "weak_reference"}
AUD_MARKERS = {"fully_copy", "partially_copy", "reference", "weak_reference"}

MOTION_TYPES = [
    # 公式12種の語形変化。単独の "tracks"(線路) を誤検出しないよう方向語まで含める。
    "zoom in", "zoom out", "zooms in", "zooms out", "zooming in", "zooming out",
    "push in", "pushes in", "pushing in", "push-in",
    "pull out", "pulls out", "pulling out", "pull-out",
    "pan left", "pan right", "pans left", "pans right", "panning left", "panning right",
    "truck left", "truck right", "trucks left", "trucks right",
    "trucking left", "trucking right",
    "tilt up", "tilt down", "tilts up", "tilts down", "tilting up", "tilting down",
    "pedestal up", "pedestal down", "pedestals up", "pedestals down",
    "arc shot", "arcs around", "arcing around",
    "tracking shot", "tracks with", "tracks alongside", "tracks backward",
    "tracks forward", "tracks left", "tracks right", "tracking backward",
    "tracking forward", "tracking alongside",
    "static shot", "holds a static", "holds static", "holds still on",
    "shake slightly", "shake strongly", "shakes slightly", "shakes strongly",
    "shaking slightly", "shaking strongly",
    "pov", "point of view",
    "roll clockwise", "roll counterclockwise", "rolls clockwise", "rolls counterclockwise",
]

BANNED = [
    "beautiful", "stunning", "gorgeous", "breathtaking", "epic", "magical", "dreamy",
    "ethereal", "atmospheric", "moody", "melancholy", "melancholic", "nostalgic",
    "emotional", "poignant", "mysterious", "haunting", "evocative", "vibe",
    "feeling of", "sense of", "atmosphere of", "mood of", "masterpiece",
    "best quality", "high quality", "4k", "8k", "ultra detailed", "ultra-detailed",
    "award-winning", "award winning", "trending", "cinematic lighting", "hyperrealistic",
]

# R4: 中途半端な振幅・速度は実測で無視される
WEAK_CAMERA = [
    r"with (moderate|medium|mild|gentle|slight|modest) amplitude",
    r"at (moderate|medium|mild|gentle|normal|slight|measured) speed",
]

NEGATIVE_PATTERNS = [
    r"\bdo not\b", r"\bdon't\b", r"\bavoid\b", r"\bwithout any\b", r"\bnever show\b",
    r"\bno visible\b", r"\bnot showing\b", r"\bshould not\b", r"\bmust not\b",
    r"\bnegative prompt\b", r"\bexclude\b", r"\bnothing else appears\b",
]

# D14: 上のリストに無い形の否定。実測 17/224 本で素通りしていた（2026-08-24）。warn 止まり
MISSED_NEGATIONS = [
    (r"\bno [a-z ]{0,28}(present|visible|audible|remains?|left)\b", "no 〜 present/visible/audible"),
    (r"\b(is|are|remains?) (completely )?absent\b", "〜 is/are absent"),
    (r"\b(free of|devoid of|empty of)\b", "free of / devoid of"),
    (r"\bwith no\b", "with no 〜"),
    (r"\b(no other|nothing (but|else))\b", "no other / nothing but"),
]

# <d> の中に紛れ込みやすい演技指示・話者名
D_TAG_CONTAMINANTS = [
    r"\(S\d", r"\bsays\b", r"\bwhispers\b", r"\bshouts\b", r"\bangrily\b", r"\bsoftly\b",
    r"\bvoice\b", r"\bnervously\b", r"\bquietly\b", r"\btone\b",
]


# 実測グリッド: frames = round(d*24) を n%17==5 まで切り上げ
def frames_for(req_seconds: int) -> int:
    n = round(req_seconds * 24)
    return n + (5 - n % 17) % 17


def actual_duration(req_seconds: int) -> float:
    return frames_for(req_seconds) / 24.0


class Report:
    def __init__(self):
        self.errors = []
        self.warns = []
        self.info = []

    def error(self, code, msg):
        self.errors.append((code, msg))

    def warn(self, code, msg):
        self.warns.append((code, msg))

    def note(self, msg):
        self.info.append(msg)

    def ok(self):
        return not self.errors


def has_japanese(s: str) -> bool:
    for ch in s:
        name = unicodedata.name(ch, "")
        if name.startswith(("CJK UNIFIED", "HIRAGANA", "KATAKANA")):
            return True
    return False


def strip_protected(text: str) -> str:
    """<d>...</d> と "..." で囲まれた画面内テキストを除去した残りを返す。"""
    t = re.sub(r"<d>.*?</d>", " ", text, flags=re.S)
    t = re.sub(r'"[^"]*"', " ", t)
    return t


def detect_mode(text: str) -> str:
    head = text.lstrip()[:80]
    if head.startswith("subject_definitions:"):
        return "ref2va"
    if head.startswith("For the target video, at 0.00 seconds"):
        return "i2va"
    if head.startswith("How the reference pictures align"):
        return "fl2va_or_l2va"
    if head.startswith("integrated_multimodal_description:"):
        return "t2va"
    return "unknown"


def split_fields(text: str, names):
    """フィールド名: の位置で分割して dict を返す。"""
    idx = []
    for n in names:
        m = re.search(r"(?m)^\s*" + re.escape(n) + r"\s*:", text)
        idx.append((m.start() if m else -1, n))
    out, order = {}, []
    present = sorted([(p, n) for p, n in idx if p >= 0])
    for i, (pos, name) in enumerate(present):
        end = present[i + 1][0] if i + 1 < len(present) else len(text)
        chunk = text[pos:end]
        chunk = re.sub(r"(?m)^\s*" + re.escape(name) + r"\s*:", "", chunk, count=1).strip()
        out[name] = chunk
        order.append(name)
    return out, order


def check(text: str, mode=None, duration=None, expect_text=None) -> Report:
    """expect_text: ブリーフの TEXT（画面内の文字）。与えられたら本文に引用符つきで出ているかを見る（D13）。
    省略時は何もしない。bench2.py / h3gen.py は渡さないので既存の数字は動かない。"""
    r = Report()
    text = text.replace("\r\n", "\n").replace("\ufeff", "")

    # ---------- C1/C2 混入物 ----------
    if "```" in text:
        r.error("C2", "コードフェンス ``` が混入している")
    if re.search(r"(?m)^\s*#{1,6}\s", text):
        r.error("C2", "Markdown 見出し (#) が混入している")
    if re.search(r"(?m)^\s*(---+|===+|\*\*\*)\s*$", text):
        r.error("C2", "水平線 (--- / ===) が混入している")
    if re.search(r"(?m)^\s*[-*]\s+\S", text):
        r.warn("C2", "行頭の箇条書き記号らしきものがある")
    if re.search(r"(?is)<think>|</think>|<reasoning>", text):
        r.error("C4", "<think> ブロックが混入している")
    if re.search(r"(?m)^\s*(Here (is|'s)|Sure[,!]|Note:|Assumptions?:|Output:|Prompt:)", text):
        r.error("C1", "前置き・注記らしき行がある")

    # ---------- C5 先頭 ----------
    detected = detect_mode(text)
    if detected == "unknown":
        r.error("C5", "先頭が既定の開始文字列のいずれでもない")
    if mode:
        want = {"t2va": "t2va", "i2va": "i2va", "fl2va": "fl2va_or_l2va",
                "l2va": "fl2va_or_l2va", "ref2va": "ref2va"}.get(mode, "?")
        if detected != want:
            r.error("C5", "MODE=%s なのに先頭が %s 形式" % (mode, detected))
    if text[:1] in (" ", "\n"):
        r.warn("C1", "先頭に空白・改行がある")

    is_ref = (detected == "ref2va") or (mode == "ref2va")
    names = FIELDS_B if is_ref else FIELDS_A
    fields, order = split_fields(text, names)

    # ---------- C7/C8 フィールド ----------
    for n in names:
        if n not in fields:
            r.error("C7", "必須フィールド %s が無い" % n)
        elif not fields[n].strip():
            r.error("C8", "%s が空" % n)
    if order and order != [n for n in names if n in fields]:
        r.error("C7", "フィールドの順序が違う: %s" % order)
    if not is_ref and re.search(r"(?m)^\s*detailed_description\s*:", text):
        r.error("C7", "FORMAT A なのに detailed_description: がある")
    if is_ref and re.search(r"(?m)^\s*integrated_multimodal_description\s*:", text):
        r.error("C7", "ref2va なのに integrated_multimodal_description: がある")

    # ---------- 整合文 ----------
    if detected in ("i2va", "fl2va_or_l2va"):
        stripped = text.strip()
        first_line = stripped.split("\n")[0]
        rest = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if not rest.startswith("\n"):
            r.error("A1", "整合文の直後に空行が1行無い")
        if detected == "fl2va_or_l2va":
            vals = re.findall(r"(\d+\.\d+)-second mark", first_line)
            if not vals:
                r.error("A2", "整合文に S.SS の -second mark が無い")
            for v in vals:
                if len(v.split(".")[1]) != 2:
                    r.error("A2", "S.SS が小数2桁でない: %s" % v)
            if duration and vals:
                want = "%.2f" % actual_duration(duration)
                if want not in vals:
                    r.warn("A2", "整合文の秒数 %s が実尺 %s と一致しない" % (vals, want))

    body_key = "detailed_description" if is_ref else "integrated_multimodal_description"
    body = fields.get(body_key, "")

    # ---------- ショットとタイムスタンプ ----------
    shots = re.findall(r"\[Shot (\d+)\]", body)
    if not shots:
        r.error("T1", "[Shot N] が本文に無い")
    else:
        if shots[0] != "1":
            r.error("T1", "最初のショットが [Shot 1] でない: [Shot %s]" % shots[0])
        nums = [int(s) for s in shots]
        if nums != sorted(nums) or len(set(nums)) != len(nums):
            r.error("T1", "ショット番号が連番でない: %s" % nums)

    m1 = re.search(r"\[Shot 1\][^\[]*", body)
    if m1 and re.search(r"At \d\d:\d\d\.\d\d\d", m1.group(0)[:160]):
        r.error("T2", "[Shot 1] にタイムスタンプが付いている")

    ts = re.findall(r"\[Shot \d+\]\s*At (\d\d):(\d\d)\.(\d\d\d)", body)
    secs = [int(h) * 60 + int(m) + int(ms) / 1000.0 for h, m, ms in ts]
    if secs != sorted(secs) or len(set(secs)) != len(secs):
        r.error("T3", "カット時刻が厳密増加でない: %s" % secs)
    if duration:
        act = actual_duration(duration)
        for s in secs:
            if s >= act:
                r.error("T4", "カット時刻 %.3fs が実尺 %.2fs を超えている" % (s, act))
            elif s > act - 1.5:
                r.warn("T4", "カット時刻 %.3fs が終端から1.5s以内 (実尺 %.2fs)" % (s, act))
    # 実務上限10秒(実尺10.13s)。--duration 未指定でも絶対上限として見る。
    for sec in secs:
        if sec >= 10.13:
            r.error("T7", "カット時刻 %.3fs が実務上限(10秒=実尺10.13s)を超えている" % sec)
    if len(re.findall(r"At \d\d:\d\d\.\d\d\d", body)) != len(ts):
        r.warn("T5", "[Shot N] に紐づかないタイムスタンプがある")
    if re.search(r"(?m)^\s*\d+\s*[-\u2013]\s*\d+\s*s\s*:", body) or re.search(r"\b\d+-\d+s:", body):
        r.error("T6", "0-2s: のような秒刻みラベルが混入している")

    # ---------- 記述 ----------
    low = (body + " " + fields.get("overall_soundscape", "") + " " +
           fields.get("non_diegetic_music", "")).lower()
    for w in BANNED:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
            r.error("D1", "禁止語: %s" % w)
    # Static Shot に振幅・速度は付かない（公式のカメラ表では別項目）
    for m in re.finditer(r"static shot[^.]{0,80}?(with (small|large) amplitude|at (slow|fast) speed)", body, re.I):
        r.error("D11", "Static Shot に振幅・速度が付いている: %s" % m.group(0)[:70])
    for pat in WEAK_CAMERA:
        m = re.search(pat, body, re.I)
        if m:
            r.error("D2", "実測で無視される中途半端なカメラ指定: %s" % m.group(0))
    # ショットごとにカメラ節が1つ以上あるか
    shot_chunks = re.split(r"(?=\[Shot \d+\])", body)
    shot_chunks = [c for c in shot_chunks if c.strip().startswith("[Shot")]
    if not shot_chunks and body.strip():
        shot_chunks = [body]
    for c in shot_chunks:
        sid = re.match(r"\[Shot (\d+)\]", c.strip())
        sid = sid.group(1) if sid else "?"
        has_motion = any(mt in c.lower() for mt in MOTION_TYPES)
        has_camera_word = "camera" in c.lower()
        if not has_motion and not has_camera_word:
            r.error("D3", "[Shot %s] にカメラ節が無い（静止なら holds a static shot と書く）" % sid)
        elif not has_motion:
            r.warn("D3", "[Shot %s] のカメラ節に公式のモーション種別が見当たらない" % sid)
        if not re.search(r"reflect|reflection|mirrored|specular", c.lower()):
            r.warn("D8", "[Shot %s] に反射の描写が無い" % sid)
        mats = re.findall(r"(?<![a-z])(wood|wooden|glass|steel|metal|metallic|concrete|paper|"
                          r"cotton|linen|plastic|ceramic|brick|tile|leather|fabric|cloth|"
                          r"asphalt|stone|enamel|chrome|vinyl|canvas|denim|wool)(?![a-z])", c.lower())
        if len(set(mats)) < 3:
            r.warn("D9", "[Shot %s] の素材名が %d 種類しか無い（3種類以上）" % (sid, len(set(mats))))
    # 実測(S14): カメラを動かすのは振幅タグではなく「終端の構図」の記述
    END_FRAMING = (r"until |ending on|ends on|leaving only|fills the frame|fills the upper|"
                   r"fills more of the frame|comes to rest on|settles on|tightening (from|until)|"
                   r"widening until|so that only|framing (tightens|widens)|keeping .* in frame")
    for c in shot_chunks:
        sid = re.match(r"\[Shot (\d+)\]", c.strip())
        sid = sid.group(1) if sid else "?"
        moving = any(mt in c.lower() for mt in MOTION_TYPES
                     if mt not in ("static shot", "holds a static", "holds static"))
        if moving and not re.search(END_FRAMING, c, re.I):
            r.warn("D12", "[Shot %s] のカメラ節に終端の構図が書かれていない（振幅タグだけでは効きにくい）" % sid)

    # D14 R8 の見落とし: 禁止語リスト（NEGATIVE_PATTERNS）に無い形の否定。
    # 実測（2026-08-24・過去の生成物 224本）: 17件が素通りしていた。ほとんどが音の記述で
    #   「footsteps are absent」「with no echo or distortion」「no other distinct sounds」「no audible sound」
    # このモデルに negative prompt は無いので、無いものを書くと**書いたものが出る**恐れがある。
    # **error ではなく warn。** error にすると修復ループが回り、56/56 の合格率に影響し得る（未計測）。
    # 昇格するなら bench_regression.py で測り直すこと（引き継ぎ §7）
    for pat, label in MISSED_NEGATIONS:
        m = re.search(pat, text, re.I)
        if m:
            around = text[max(0, m.start() - 24):m.end() + 16].replace("\n", " ")
            r.warn("D14", "無いものを書いている（%s）: …%s… → 代わりにその場所にあるものを書く" % (label, around))

    # D13 画面内の文字（TEXT）。実測（2026-08-23・12本）: 常用の範囲は原文どおり描画される。出ていなければ直させる
    if expect_text:
        import difflib
        et = expect_text.strip()
        src = body or text
        straight = [q for q in re.findall(r'"([^"]+)"', src)]
        curly = [q for q in re.findall(r'[“”]([^“”]+)[“”]', src)]
        hits = [q for q in straight if q.strip() == et]
        if hits and len(hits) > 1:
            r.warn("D13", '画面内の文字 "%s" が %d 回書かれている（1回でよい）' % (et, len(hits)))
        elif not hits:
            if any(q.strip() == et for q in curly):
                # 公式の書式は英語の直線ダブルクォート。曲がり引用符は D7（日本語の漏れ）にも引っかかる
                r.error("D13", '画面内の文字 "%s" が曲がり引用符 “ ” で書かれている。直線の " " にする' % et)
            else:
                near = [q for q in straight + curly
                        if difflib.SequenceMatcher(None, q.strip(), et).ratio() >= 0.6]
                r.error("D13", '画面内の文字 "%s" が本文に引用符つきで出ていない%s'
                        % (et, ("（似た文字列: %s）" % " / ".join('"%s"' % q for q in near[:3])) if near else ""))

    # ルールの漏出
    for pat in [r"in order to\b", r"as required\b", r"to ensure\b", r"for consistency\b",
                r"to keep the (style|look)", r"kept to one\b", r"per the (rules|guide)",
                r"as (specified|instructed)\b", r"to maintain (visual|temporal) stability"]:
        m = re.search(pat, body, re.I)
        if m:
            r.error("D10", "システムプロンプトのルールが本文に漏れている: %s" % m.group(0))
    for pat in NEGATIVE_PATTERNS:
        m = re.search(pat, body, re.I)
        if m:
            r.error("D4", "ネガティブ表現: %s" % m.group(0))
    for line in body.split("\n"):
        if line.count(",") >= 8 and not re.search(r"\.\s", line) and len(line) > 80:
            r.warn("D5", "カンマ区切りのタグ列らしき行がある")
            break
    for w in [r"\bresolution\b", r"\b1344\b", r"\b768\b", r"\bseed\b", r"\beuler\b",
              r"\bsampler\b", r"\bcfg\b", r"\bdenoise\b", r"step count",
              r"\.mp4", r"\.png", r"\.safetensors"]:
        m = re.search(w, body.lower())
        if m:
            r.warn("D6", "実行設定・ファイル名らしき語: %s" % m.group(0))

    leaked = strip_protected(body)
    if has_japanese(leaked):
        r.error("D7", "<d> と画面内テキスト以外の場所に日本語がある")
    for n in names:
        if n == body_key:
            continue
        if n in fields and has_japanese(strip_protected(fields[n])):
            r.error("D7", "%s に日本語がある" % n)

    words = len(re.findall(r"[A-Za-z][A-Za-z'\-]*", body))
    r.note("プロンプトの語数: %d" % words)
    # 公式: 350-500語は「生成タスク」のみ。動画編集/継続タスクは元動画の複雑さに従うので対象外。
    editing_task = bool(re.match(r"\[[^\]]*(video editing|video continuation)",
                                 fields.get("summary", "").strip()))
    if is_ref:
        if editing_task:
            r.note("編集/継続タスクのため 350-500語の下限は適用しない")
        elif words < 320:
            r.error("W1", "detailed_description が短すぎる (%d語 / 目安350-500)" % words)
        elif words > 560:
            r.warn("W1", "detailed_description が長い (%d語 / 目安350-500)" % words)
    else:
        # 公式サンプルは 10s2ショットで245語、8s1ショットで532語と幅が大きい。
        # 語数はベースモードでは目安（WARN）。明らかな未完成だけ ERROR。
        floor = {4: 150, 5: 150, 6: 200, 7: 230, 8: 250, 9: 280, 10: 300}.get(duration or 8, 250)
        if words < 120:
            r.error("W1", "本文が短すぎる (%d語)" % words)
        elif words < floor:
            r.warn("W1", "本文が目安より短い (%d語 / %ds の目安 %d語)" % (words, duration or 8, floor))

    # ---------- 音声 ----------
    d_blocks = re.findall(r"<d>(.*?)</d>", body, flags=re.S)
    for d in d_blocks:
        if not re.match(r"^\s*\[[A-Za-z]+\]", d):
            r.error("V1", "<d> の先頭に [Language] タグが無い: %r" % d[:40])
        inner = re.sub(r"^\s*\[[A-Za-z]+\]", "", d)
        for pat in D_TAG_CONTAMINANTS:
            if re.search(pat, inner, re.I):
                r.error("V2", "<d> の中に演技指示・話者情報が混入: %r" % inner[:40])
                break
    ids = re.findall(r"\(S\d[^)]*\)", body)
    if d_blocks and not ids:
        r.error("V3", "セリフがあるのに話者 ID (S1) が無い")
    if not d_blocks and ids:
        r.warn("V3", "セリフが無いのに話者 ID が振られている")
    # 公式が必須としているのはボイスオーバー時のみ。通常セリフは「最後の </d> の後」を実務目安として見る。
    last_d = None
    for m in re.finditer(r"</d>", body):
        last_d = m
    if last_d is not None:
        tail = body[last_d.end():last_d.end() + 240].lower()
        if not re.search(r"lips|mouth|jaw|closes|closed|ceases", tail):
            r.warn("V4", "最後の </d> の直後に口を閉じる描写が見当たらない")
    for m in re.finditer(r"off-screen voiceover", body):
        tail = body[m.end():m.end() + 400].lower()
        if "lips remain" not in tail:
            r.error("V5", "ボイスオーバーの後に lips remain ... closed の明記が無い")
            break
    sc = fields.get("overall_soundscape", "")
    if "<d>" in sc or re.search(r"\(S\d", sc):
        r.error("V6", "overall_soundscape にセリフ/話者IDが入っている")
    for w in ["dialogue", "says", "speaks", "singing", "sings", "lyrics"]:
        if re.search(r"(?<![a-z])" + w + r"(?![a-z])", sc.lower()):
            r.warn("V6", "overall_soundscape に発話系の語: %s" % w)
    nd = fields.get("non_diegetic_music", "")
    # 公式 4.7: 気分語と「音楽の感情的役割の説明」を書かない
    for pat in [r"creat\w+ a (sense|feeling|mood)", r"convey\w*", r"evok\w+", r"emphasis\w+",
                r"underscor\w+", r"reinforc\w+", r"enhanc\w+", r"support\w+ the",
                r"without", r"reflect\w+ (her|his|their) (inner|emotional)"]:
        m = re.search(pat, nd, re.I)
        if m:
            r.error("V10", "non_diegetic_music が音楽の役割・感情を説明している: %s" % m.group(0))
    for pat in [r"without"]:
        m = re.search(pat, sc, re.I)
        if m:
            r.warn("V10", "overall_soundscape に否定表現: %s" % m.group(0))
    for w in ["radio", "television", " tv ", "jukebox", "record player"]:
        if w in nd.lower():
            r.error("V7", "non_diegetic_music に劇中音源: %s" % w.strip())
    if nd.strip() and nd.strip().upper() != "N/A" and "<Audio" not in nd:
        if not re.search(r"piano|guitar|string|cello|violin|synth|drum|bass|horn|flute|koto|"
                         r"pad|percussion|orchestr|tone|note|chime|marimba|harp|organ", nd.lower()):
            r.warn("V8", "non_diegetic_music に楽器名が無い")
        if not re.search(r"tempo|slow|fast|beat|rhythm|pulse", nd.lower()):
            r.warn("V8", "non_diegetic_music にテンポ/リズムの記述が無い")

    if duration:
        jp = 0
        for d in d_blocks:
            if d.strip().lower().startswith("[japanese]"):
                s = re.sub(r"^\s*\[[A-Za-z]+\]", "", d)
                jp += len([c for c in s if has_japanese(c) or c in "、。！？ー"])
        if jp:
            act = actual_duration(duration)
            r.note("日本語セリフ %d文字 / 実尺 %.2fs" % (jp, act))
            if jp > act * 6:
                r.error("V9", "日本語セリフが尺に収まらない (%d文字 > %.0f文字)" % (jp, act * 6))
            elif jp > act * 4:
                r.warn("V9", "日本語セリフが多め (%d文字)" % jp)

    # ---------- ref2va 固有 ----------
    if is_ref:
        sd = fields.get("subject_definitions", "")
        standalone = set()
        for line in sd.split("\n"):
            lm = re.match(r"\s*<(Subject|Picture|Video|Audio) (\d+)>", line)
            if lm:
                standalone.add((lm.group(1), lm.group(2)))
        cited = set(re.findall(r"<(Subject|Picture|Video|Audio) (\d+)>", sd))
        defined = standalone | cited
        used = set(re.findall(
            r"<(Subject|Picture|Video|Audio) (\d+)>",
            fields.get("summary", "") + fields.get("retention_analysis", "") + body +
            fields.get("overall_soundscape", "") + fields.get("non_diegetic_music", "")))
        for u in sorted(used - defined):
            r.error("R1", "未定義のラベルを使用: <%s %s>" % u)
        for u in sorted(standalone - used):
            r.warn("R1", "独立定義したが他セクションで未使用: <%s %s>" % u)
        if not standalone:
            r.error("R2", "subject_definitions に行頭から始まるラベル定義が1つも無い")

        summ = fields.get("summary", "").strip()
        m = re.match(r"\[([^\]]+)\]", summ)
        if not m:
            r.error("R3", "summary がタスク種別プレフィックス [ ... ] で始まっていない")
        else:
            parts = [p.strip() for p in m.group(1).split("+")]
            for p in parts:
                if p not in TASK_TYPES:
                    r.error("R3", "不正なタスク種別: %s" % p)
            if len(parts) != len(set(parts)):
                r.error("R3", "タスク種別が重複している")
        if re.search(r"<(Subject|Picture|Video|Audio) (\d+)>", summ):
            new_in_summary = set(re.findall(r"<(Subject|Picture|Video|Audio) (\d+)>", summ)) - defined
            for u in sorted(new_in_summary):
                r.error("R3", "summary が新しいラベルを導入している: <%s %s>" % u)

        ra = fields.get("retention_analysis", "")
        if re.search(r"\(S\d", ra):
            r.error("R4", "retention_analysis に話者 ID (Sx) が書かれている")
        for l in [x for x in ra.split("\n") if x.strip()]:
            lm = re.match(r"\s*<(Subject|Picture|Video|Audio) (\d+)>", l)
            if not lm:
                r.error("R4", "retention_analysis の行がラベルで始まっていない: %r" % l[:50])
                continue
            marker = re.search(r":\s*([a-z_]+)\s*-", l)
            if not marker:
                r.error("R4", "retention_analysis の行にマーカーが無い: %r" % l[:50])
                continue
            mk = marker.group(1)
            allowed = AUD_MARKERS if lm.group(1) == "Audio" else VIS_MARKERS
            if mk not in allowed:
                r.error("R4", "<%s %s> に不正なマーカー: %s" % (lm.group(1), lm.group(2), mk))
        for lab in sorted(standalone):
            if not re.search(r"<%s %s>" % lab, ra):
                r.error("R5", "<%s %s> が retention_analysis に無い（独立定義したラベルは必須）" % lab)

        first = body.strip().split("\n")[0] if body.strip() else ""
        if first.startswith("[Shot"):
            r.error("R6", "スタイル宣言が [Shot 1] の前に無い")
        elif "[Shot 1]" in first:
            r.error("R6", "スタイル宣言と [Shot 1] が同じ行にある")
        if not re.search(r"<(Subject|Picture|Video|Audio) \d+>", body):
            r.error("R7", "detailed_description に参照ラベルが1つも無い")

    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--mode", default=None,
                    choices=["t2va", "i2va", "fl2va", "l2va", "ref2va"])
    ap.add_argument("--duration", type=int, default=None)
    a = ap.parse_args()

    with open(a.path, "r", encoding="utf-8") as f:
        text = f.read()

    r = check(text, a.mode, a.duration)
    if a.duration:
        print("[尺] 指定%ds -> %dフレーム / 実尺 %.2fs"
              % (a.duration, frames_for(a.duration), actual_duration(a.duration)))
    for m in r.info:
        print("[情報] %s" % m)
    for c, m in r.warns:
        print("[WARN %s] %s" % (c, m))
    for c, m in r.errors:
        print("[ERROR %s] %s" % (c, m))
    print("\n=> ERROR %d / WARN %d : %s" % (len(r.errors), len(r.warns), "PASS" if r.ok() else "FAIL"))
    sys.exit(0 if r.ok() else 1)


if __name__ == "__main__":
    main()
