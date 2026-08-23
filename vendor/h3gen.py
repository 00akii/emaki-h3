# -*- coding: utf-8 -*-
"""
h3gen.py - ローカル LLM に MiniMax-H3 プロンプトを書かせ、機械検査に通るまで自動で直させる。

  python h3gen.py --brief brief.txt --mode ref2va --duration 8 --out プロンプト.txt

検査(h3lint)が ERROR を返す間、そのエラー本文をモデルに突き返して書き直させる。
既定で最大3回。全部落ちたら最も ERROR が少なかった版を書き出し、終了コード1を返す。
"""
from __future__ import annotations
import argparse, io, json, os, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import h3lint

DEFAULT_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_SYS = os.path.join(HERE, "system_h3.txt")

REPAIR = """Your previous output failed these mechanical checks:

{errors}
{hints}
Re-emit the COMPLETE prompt body with exactly those problems fixed. Change nothing else.
Same rules as before: the body only, no fence, no heading, no explanation, no note about what you changed."""

# エラー種別ごとの直し方。「直せ」だけでは動かない項目があるので具体化する。
HINTS = {
    "D14": ("You wrote what is NOT there. This model has no negative prompt, so an absence can be rendered as a presence. Delete the phrase and name what IS in that place instead: for a soundscape, the sounds that are actually audible; for a shot, the objects and surfaces actually in frame."),
    "D13": ("The brief's TEXT must appear in the body exactly once, inside English double quotes, character for "
            "character as given (do not translate or reword it), as writing on an object that is inside the frame: "
            "the object the brief names, or a sign, board or screen you place large and facing the camera."),
    "W1": ("The text is too short. Keep every existing sentence and ADD four or five new ones inside the "
           "same shot: name three more physical objects with their materials, state the direction and "
           "colour of the light and what surface it lands on, extend the reflection with what is visible "
           "inside it, and lengthen the action into more body mechanics (rotation, which limb leads, "
           "weight shift, where it stops). Do not delete anything."),
    "D1": ("Delete the banned word and replace it with what is physically on screen: the colour and "
           "direction of the light, the material of a surface, or the movement that produced that impression."),
    "D2": ("Replace the amplitude and speed with one of the two legal combinations: "
           "'with small amplitude at slow speed' or 'with large amplitude at fast speed'."),
    "D3": ("Add one camera clause to that shot, for example 'The camera holds a static shot on ...' or "
           "'The camera pushes in with small amplitude at slow speed toward ...'."),
    "D4": "Delete the negative instruction and describe what IS on screen in that place instead.",
    "D10": "Delete that clause entirely. It explains a choice instead of describing the picture.",
    "V3": ("Put a speaker phrase ending in (S1) immediately before the <d> block, for example "
           "'The young woman with a quiet, breathy voice (S1) says: <d>...'"),
    "V10": ("End that sentence after the volume change. Delete every clause about what the music does "
            "for the scene or the viewer."),
    "R3": ("Fix the task-type prefix. Reference images, motion reference videos and camera-movement "
           "reference videos are all [reference generation]."),
    "R5": "Add one retention_analysis line for that label, with a legal relationship marker.",
    "T4": "Move that cut time earlier so it sits at least 1.5 s before the end of the clip.",
}


def build_hints(errors):
    seen, out = set(), []
    for code, _ in errors:
        if code in HINTS and code not in seen:
            seen.add(code)
            out.append("- %s: %s" % (code, HINTS[code]))
    return ("\nHow to fix:\n" + "\n".join(out) + "\n") if out else ""


def chat(url, model, messages, temperature, max_tokens, seed, think, timeout):
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "top_p": 0.9, "max_tokens": max_tokens, "presence_penalty": 0.0,
               "frequency_penalty": 0.0, "stream": False}
    if not think:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload["reasoning_effort"] = "none"
    if seed is not None:
        payload["seed"] = seed
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"].get("content") or "", time.time() - t0


def generate(brief, model, mode=None, duration=None, system_path=DEFAULT_SYS,
             url=DEFAULT_URL, tries=3, temperature=0.35, max_tokens=3000,
             seed=None, think=False, timeout=900, verbose=True):
    system = io.open(system_path, encoding="utf-8").read()
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": brief}]
    best, best_rep, best_n = "", None, 10 ** 9
    history = []  # 試行ごとの [ERRORコード...] / WARNコード / 秒数。モデルのクセ集計用。

    for attempt in range(1, tries + 1):
        txt, dt = chat(url, model, messages, temperature, max_tokens,
                       (None if seed is None else seed + attempt - 1), think, timeout)
        rep = h3lint.check(txt, mode, duration)
        history.append({"attempt": attempt, "seconds": round(dt, 1), "chars": len(txt),
                        "errors": [c for c, _ in rep.errors],
                        "warns": [c for c, _ in rep.warns],
                        "notes": list(rep.info)})
        rep.history = history
        if verbose:
            print("  試行%d  %.1fs  ERROR %d / WARN %d" % (attempt, dt, len(rep.errors), len(rep.warns)))
            for c, m in rep.errors:
                print("      - [%s] %s" % (c, m))
        if len(rep.errors) < best_n:
            best, best_rep, best_n = txt, rep, len(rep.errors)
            best_rep.history = history
        if rep.ok():
            return txt, rep, attempt
        if attempt == tries:
            break
        errs = "\n".join("- [%s] %s" % (c, m) for c, m in rep.errors)
        messages = messages[:2] + [
            {"role": "assistant", "content": txt},
            {"role": "user", "content": REPAIR.format(errors=errs, hints=build_hints(rep.errors))}]
    return best, best_rep, tries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", required=True, help="ブリーフのテキストファイル")
    ap.add_argument("--out", required=True, help="書き出し先 (.txt)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--system", default=DEFAULT_SYS)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--mode", default=None,
                    choices=["t2va", "i2va", "fl2va", "l2va", "ref2va"])
    ap.add_argument("--duration", type=int, default=None)
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--temp", type=float, default=0.35)
    ap.add_argument("--max-tokens", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--think", action="store_true", help="思考モードを許可 (max-tokens を 8000 以上に)")
    a = ap.parse_args()

    brief = io.open(a.brief, encoding="utf-8").read()
    txt, rep, n = generate(brief, a.model, a.mode, a.duration, a.system, a.url,
                           a.tries, a.temp, a.max_tokens, a.seed, a.think)
    io.open(a.out, "w", encoding="utf-8", newline="\n").write(txt)
    ok = rep is not None and rep.ok()
    print("\n%s  (%d回目で確定)  -> %s" % ("PASS" if ok else "FAIL", n, a.out))
    if rep:
        for m in rep.info:
            print("[情報] %s" % m)
        for c, m in rep.warns:
            print("[WARN %s] %s" % (c, m))
        for c, m in rep.errors:
            print("[ERROR %s] %s" % (c, m))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
