# -*- coding: utf-8 -*-
"""
textcheck.py — 「画面内の文字」欄の入力を、生成前に機械的に点検する。LLM は使わない。

根拠は 2026-08-23 の実測（`../文字検証/結果.md`、12本・608×352・seed 固定）:
  - ASCII / ひらがな / カタカナ / JIS第1水準の常用漢字 … 9/9 が原文どおりに描画された（「こんにちは」「営業中」「コーヒー」）
  - JIS第2水準（鵺鰰）   … 偏は合うが旁が**別の実在漢字**になる
  - JIS X 0208 外の異体字（髙﨑） … 通常字体（高崎）に**黙って置き換わる**
  - 国字（峠辻）         … 辻○ 峠×。同じ国字で割れたので「中国語に在るか」は効かない。複雑で稀な字ほど崩れる
  - カメラが動いても・字種を本文で明示しても差なし。中国語簡体（营业中）と日本語（営業中）は同等
崩れ方が「読めない字」ではなく「別の実在漢字」なので、日本語が読めない人の確認では見逃す。
だから入力の時点で水準を見て、稀少字が混ざっていたら先に言う。

水準の判定は Shift_JIS のバイト列から機械的に行う（目視分類は 2/8 外した・実測）。
  第1水準 = 16〜47区（先頭バイト 0x88-0x98） / 第2水準 = 48〜84区（0x99-0xEA） / 非漢字 = 1〜8区（0x81-0x87）
"""
from __future__ import annotations
import re
import unicodedata

# 欄の書式: 「こんにちは」 または 看板に「こんにちは」 または "HELLO"。
# カギカッコ／ダブルクォートがあれば中が文字、外が「載せる物」。無ければ全体が文字。
_QUOTE = re.compile(r'[「"\u201c]([^」"\u201d]+)[」"\u201d]')


def parse(field: str) -> dict:
    """欄の文字列 → {text, carrier}。text は画面に出す原文、carrier は載せる物の説明（空あり）。"""
    f = (field or "").strip()
    if not f:
        return {"text": "", "carrier": ""}
    m = _QUOTE.search(f)
    if m:
        text = m.group(1).strip()
        carrier = (f[:m.start()] + f[m.end():]).strip(" 　、。にへのをで")
        return {"text": text, "carrier": carrier}
    return {"text": f, "carrier": ""}


def level(ch: str) -> str:
    """1文字の水準。"""
    try:
        b = ch.encode("shift_jis")
    except UnicodeEncodeError:
        return "0208外"
    if len(b) == 1:
        return "ASCII"
    lead = b[0]
    if 0x81 <= lead <= 0x87:
        return "非漢字"          # かな・記号・全角英数
    if 0x88 <= lead <= 0x98:
        return "JIS第1水準"
    if 0x99 <= lead <= 0xEA:
        return "JIS第2水準"
    return "その他"


_RISKY = ("JIS第2水準", "0208外", "その他")
_MEASURED_MAX = 5   # 実測した最長（「こんにちは」）。それより長い文字列は未検証


def check(field: str) -> dict:
    """
    返り値: {action: "pass"|"warn"|"none", text, carrier, reason, chars:[{ch, level}], risky:[ch...]}
    - none … 欄が空
    - pass … 実測で確実に出る範囲（ASCII・かな・第1水準）
    - warn … 第2水準・0208外（異体字）が混ざる。別の字になる恐れ。長さが未検証のときも warn
    """
    p = parse(field)
    text = p["text"]
    if not text:
        return {"action": "none", "text": "", "carrier": "", "reason": "", "chars": [], "risky": []}
    chars = [{"ch": c, "level": level(c)} for c in text if not c.isspace()]
    risky = [c["ch"] for c in chars if c["level"] in _RISKY]
    reasons = []
    # kind は警告の理由。画面のラベルを変えるために分ける（字が化けるのと、長さが未検証なのは別の話）
    kind = "ok"
    if risky:
        lv = sorted(set(c["level"] for c in chars if c["ch"] in risky))
        reasons.append("「%s」は%s。実測では偏や旁が別の実在漢字になる・通常字体に置き換わることがある（鵺鰰・髙﨑で確認）。"
                       "常用の字に言い換えられるなら、その方が確実" % ("".join(risky), "・".join(lv)))
        kind = "substitution"
    if len(chars) > _MEASURED_MAX:
        reasons.append("%d文字は未検証（実測は%d文字まで）。長いほど1文字あたりの大きさが落ちる" % (len(chars), _MEASURED_MAX))
        if kind == "ok":
            kind = "length"
    return {"action": "warn" if reasons else "pass", "kind": kind, "text": text, "carrier": p["carrier"],
            "reason": " ".join(reasons), "chars": chars, "risky": risky}
