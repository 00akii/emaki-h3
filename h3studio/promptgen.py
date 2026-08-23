# -*- coding: utf-8 -*-
"""
promptgen.py — vendor/h3gen.py と vendor/h3lint.py のラッパ。
h3gen は LM Studio 直叩きなので、ここでは llm.chat を使う版を持ち、
自動修復ループ（エラー文を返して直させる）だけ h3gen の HINTS を流用する。
"""
from __future__ import annotations
import io, os, re, sys, time
from . import config, llm

sys.path.insert(0, config.VENDOR_DIR)
import h3lint  # noqa: E402
import h3gen   # noqa: E402  (HINTS / REPAIR / build_hints を流用)

SYSTEM_PATH = os.path.join(config.VENDOR_DIR, "system_h3.txt")
NOTES_PATH = os.path.join(config.VENDOR_DIR, "system_h3_notes.txt")


def _read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


_TEXT_LINE = re.compile(r'(?m)^TEXT:\s*"([^"]+)"')


def expected_text(brief: str):
    """ブリーフの TEXT 行（brief.build が出す `TEXT: "…"`）から、本文に出るべき原文を取る。無ければ None。"""
    m = _TEXT_LINE.search(brief or "")
    return m.group(1).strip() if m else None


def lint(text: str, mode=None, duration=None, expect_text=None) -> dict:
    rep = h3lint.check(text, mode, duration, expect_text=expect_text)
    return {"ok": rep.ok(),
            "errors": [{"code": c, "msg": m} for c, m in rep.errors],
            "warns": [{"code": c, "msg": m} for c, m in rep.warns],
            "info": list(rep.info),
            "words": next((int(s.split(":")[1]) for s in rep.info if s.startswith("本文語数:")), None)}


def generate(cfg: dict, brief: str, mode=None, duration=None, tries=3, seed=None,
             temperature=0.35, max_tokens=3000, model=None, progress=None) -> dict:
    """
    ブリーフ → プロンプト。h3lint が ERROR を返す間、エラー文＋修復ヒントを返して直させる。
    返り値: {prompt, lint, attempts:[{n, seconds, errors, warns}], ok}
    """
    system = _read(SYSTEM_PATH)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": brief}]
    attempts, best, best_rep, best_n = [], "", None, 10 ** 9
    et = expected_text(brief)   # 画面内の文字。本文に引用符つきで出ていなければ D13 で直させる
    for attempt in range(1, tries + 1):
        if progress:
            progress("プロンプト生成 試行%d/%d" % (attempt, tries))
        txt, dt, usage = llm.chat(cfg, messages, temperature=temperature, max_tokens=max_tokens,
                                  seed=(None if seed is None else seed + attempt - 1), model=model)
        rep = h3lint.check(txt, mode, duration, expect_text=et)
        attempts.append({"n": attempt, "seconds": round(dt, 1),
                         "errors": [c for c, _ in rep.errors], "warns": [c for c, _ in rep.warns],
                         "tokens": usage.get("completion_tokens")})
        if len(rep.errors) < best_n:
            best, best_rep, best_n = txt, rep, len(rep.errors)
        if rep.ok():
            break
        if attempt == tries:
            break
        errs = "\n".join("- [%s] %s" % (c, m) for c, m in rep.errors)
        messages = messages[:2] + [
            {"role": "assistant", "content": txt},
            {"role": "user", "content": h3gen.REPAIR.format(errors=errs, hints=h3gen.build_hints(rep.errors))}]
    return {"prompt": best.strip() + "\n", "lint": lint(best, mode, duration, expect_text=et),
            "attempts": attempts, "ok": bool(best_rep and best_rep.ok())}


def write_notes(cfg: dict, brief: str, prompt: str, model=None) -> str:
    """解説 .md を書かせる（納品規約: .txt と同名の .md）。"""
    system = _read(NOTES_PATH)
    user = "## ブリーフ\n\n%s\n\n## 生成された H3 構造化プロンプト\n\n%s\n" % (brief, prompt)
    txt, _, _ = llm.chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                         temperature=0.35, max_tokens=3000, model=model)
    return txt.strip() + "\n"


def frames_for(duration: int) -> int:
    return h3lint.frames_for(duration)


def actual_duration(duration: int) -> float:
    return h3lint.actual_duration(duration)
