# -*- coding: utf-8 -*-
"""キャラ表画像を VLM に読ませて <Subject N> 用の記述を書かせる実験。"""
import base64, io, json, sys, time, urllib.request

SYS = """You describe a character reference sheet for a video-generation prompt.
Output ONE English sentence, nothing else. No preamble, no markdown.
Name only what fixes this character's identity across shots, in this order:
hair length and colour, any coloured highlight or streak and where it sits, any stray strand and where,
eye shape and iris colour and where the highlight sits in the iris, blush, then each garment with its colour as a flat area and its trim.
Use plain concrete words. Never write a mood word, never describe the background, never mention the image or the camera."""

def b64(p):
    return base64.b64encode(open(p, "rb").read()).decode()

def ask(model, path):
    payload = {"model": model, "messages": [
        {"role": "system", "content": SYS},
        {"role": "user", "content": [
            {"type": "text", "text": "Describe this character."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(path)}}]}],
        "temperature": 0.2, "max_tokens": 400, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}
    req = urllib.request.Request("http://localhost:1234/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read().decode())
    return d["choices"][0]["message"].get("content", ""), time.time() - t0

if __name__ == "__main__":
    model, path = sys.argv[1], sys.argv[2]
    txt, dt = ask(model, path)
    print("[%.1fs] %s" % (dt, txt.strip()))
