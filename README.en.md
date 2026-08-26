<img src="static/icon.svg" width="56" alt="">

# Emaki H3

**New here? Start with [docs/00_はじめに.md](docs/00_はじめに.md)** — a nine-chapter walkthrough of the actual screens (setup → your first clip → how to fill each field → troubleshooting). Japanese only for now.

A local web app that takes you from a rough idea to a finished MiniMax-H3 video in one screen: write a structured prompt with a local LLM, cut out your characters with SAM3, submit to ComfyUI, and inspect the result.

**Source art → cutout → reference images → brief → prompt → preview render → final render → accept & record.**

Japanese documentation: [README.md](README.md) — it is the primary document and is kept more detailed than this one.

---

## What you need

| | |
|---|---|
| **ComfyUI** | 0.33 or later (the version that ships `MiniMaxH3ReferenceToVideo` and `SAM3_Detect` in core). Default `http://127.0.0.1:8189` |
| **MiniMax-H3 weights** | UNET / Turbo LoRA / text encoder / video VAE / audio VAE. **Not bundled** — see License below |
| **An H3 workflow JSON** | **`workflows/emaki_h3_ref2va.json` is included** (see "Getting a workflow" below). The app reads only model names, sampler and output format from it, and **builds the graph it actually runs itself** |
| **Python** | 3.10+, with `fastapi` and `uvicorn` (`pip install -r requirements.txt`). `python-multipart` is **optional** — install it to drag reference material in from the file manager. **Without it the app still starts and everything else works** |
| **ffmpeg** | On PATH (used to inspect results, and to make thumbnails for reference videos) |
| **LM Studio** (optional) | For writing prompts locally. Default `http://localhost:1234`; the `lms` CLI is also used. Not needed if you use a cloud LLM |
| **Eagle** (optional) | To file finished videos |

### ComfyUI nodes

**Settings → section N** tells you what is missing in your ComfyUI. Check it once before your first render.

| Node | Needed? | If missing |
|---|---|---|
| `MiniMaxH3ReferenceToVideo` `UNETLoader` `CLIPLoader` `SamplerCustomAdvanced` | **Required** | Cannot render (all are in ComfyUI core 0.33+) |
| `VHS_VideoCombine` (ComfyUI-VideoHelperSuite) | Has a fallback | Falls back to core `CreateVideo` + `SaveVideo`. You lose control over `crf` / `pix_fmt` |
| `VHS_LoadVideo` (same pack) | Optional | **Reference videos are unavailable.** The app stops with a clear message if you select one. Reference images still work |
| `SAM3_Detect` (core) | Optional | The cutout screen is unavailable. You can still drop pre-cut images into `input\` |
| `DisplayAny` (ComfyUI-Easy-Use etc.) | Optional | Falls back to core `PreviewAny` |
| `LayerUtility: PurgeVRAM V2` (ComfyUI-LayerStyle) | Optional | Only `/free` is used. **Second and later renders can get much slower** — see Gotchas |

**Settings → section W** checks that the weight filenames read from your workflow JSON actually exist in your ComfyUI. Filenames differ per install; if you see red, re-save the H3 workflow in ComfyUI or point `config.json`'s `workflow_json` at your own.

---

## Run

**Double-click `start.bat`** (Windows). It finds a Python for you, and if it cannot, it tells you what to do instead of closing.

From a shell:

```bash
pip install -r requirements.txt
python server.py
```

Then open `http://127.0.0.1:8765`. To use a different port:

```bash
python server.py --port 8799
```

The UI opens even when LM Studio and ComfyUI are down — missing pieces show up in red under Settings.

## Getting a workflow

The app **does not run your workflow.** It reads three things from it, and `comfy.py` builds the graph it executes from scratch every time:

- the five weight filenames (UNET / Turbo LoRA / text encoder / video VAE / audio VAE)
- sampler, scheduler and step count
- how reference videos are loaded (fps, format) and the output format

**Point it at the included `workflows/emaki_h3_ref2va.json` and you are done.**

```json
"workflow_json": "workflows/emaki_h3_ref2va.json"
```

That file is ComfyUI's own template `video_minimax_h3_r2v.json` (MIT) with **only the sampler changed** to the values this app measured:

| | Official template | Included sample | Why |
|---|---|---|---|
| Sampler | `res_multistep` | **`euler`** | measured, with the Turbo LoRA |
| Scheduler | `simple` | **`normal`** | same |
| Steps | `20` | **`6`** | the Turbo LoRA is built for 4–6 steps |

**The weight filenames are the official ones.** If you use a different quantisation (`fp8_scaled`, say), section W under Settings turns red — point at your own workflow, or edit the names in the included JSON.

### Using your own workflow

Open **Workflow → Templates → Video → MiniMax H3 Reference to Video** in ComfyUI, **set the sampler to `euler` / `normal` / 6 steps**, save it, and point `workflow_json` at that file. ComfyUI defaults to 20 steps — **leave it and every clip takes three times longer.**

**Cutting out subjects (SAM3) has nothing to do with your workflow.** The app builds that graph itself. All it needs is the `SAM3_Detect` node in ComfyUI and a checkpoint under `models/checkpoints/sam3/`.

### If you use the ComfyUI portable build

`ComfyUI_windows_portable` ships **its own Python**, and that one is not on your PATH — typing `python` finds nothing, or finds a different install. Install into that Python:

```bat
..\python_embeded\python.exe -m pip install -r requirements.txt
..\python_embeded\python.exe server.py
```

To point `start.bat` at it:

```bat
set EMAKI_PYTHON=C:\...\ComfyUI_windows_portable\python_embeded\python.exe
start.bat
```

## Configure

Everything environment-specific lives in **`config.json`**; there are no paths in the code. If it does not exist, `config.example.json` is read instead — copy it and edit.

| Key | Meaning |
|---|---|
| `comfy_url` / `comfy_input_dir` / `comfy_output_dir` | Where ComfyUI is. Reference images are read from `input\` |
| `workflow_json` | Your H3 workflow — source of model names, sampler and output format |
| `prompt_txt` / `archive_dir` | Where prompts and archives are written |
| `raw_dir` | Un-cut source artwork for the cutout screen |
| `sam3` | Cutout defaults (`checkpoint` empty = auto-pick / `text` / `threshold` / `refine` fixed at 1 / `crop_margin`) |
| `lmstudio_url` / `lmstudio_model` / `lmstudio_load` | Local LLM. `lmstudio_load` holds the verified load settings (ctx 16384 / parallel 1 / MTP) |
| `llm.backend` | `lmstudio` (default, free) or `openai_compat` (cloud, billed) |
| `llm.openai_compat` | `base_url` / `model` / `api_key_env` — **you give the name of an environment variable, never the key itself** |
| `llm.pricing` | Optional `{model: {"in": $/Mtok, "out": $/Mtok}}` to show an estimated cost |
| `eagle` | Eagle integration (`enabled` / `url` / `token` / `folder_id` / `auto` = off\|final\|all / `send_contact_sheet` / `extra_tags`) |
| `gen` | Render defaults (preview 608×352 / final 1344×768 / 6 steps / euler+normal / 10 s cap). All measured, not guessed |

`config.json` holds your paths, model names and Eagle token. **It is already in `.gitignore`, so it will not be published with the repo.**

## Using it

1. Pick a **project** at the top. Style declaration, character definitions and reference assets are stored per project
2. Write a **brief**. The 4-field "recommended" mode is the default; each field has a `?` explaining how to write it
   - Place/time and motion are required. **Opening framing, camera (end), on-screen text and dialogue are optional**
   - **Camera means the END framing** — where the shot comes to rest (measured: direction correct 12/12). Empty means a static shot. **Prefer specifying push-in / pull-out here rather than in the opening framing**
   - **Opening framing** is frame 1. Height (eye level / high / low) and viewpoint (front / profile / behind) work by naming them.
     **Choosing a tight size also constrains the body text to what fits the frame** — without that constraint the prose wins and the size is silently ignored (measured: ×9 → ×0).
     The verdict for your combination (pass / adjusted / unlikely to work) appears right under the field. **Options that do not work at all — dutch angle, top-down, telephoto, wide-angle — are not offered.**
   - **On-screen text** is writing on a sign, card or screen. Write it as `看板に「こんにちは」` — whatever is inside the quotes is reproduced verbatim, and anything before it names what carries the text.
     **Everyday kanji, kana and ASCII come out correctly** (measured 9/9). **Rare kanji and old variant forms are silently replaced by a different real character**, so the app classifies the input and warns
   - **Separate motion beats however you like** — spaces, commas, hyphens, newlines or arrows all work; the app normalizes to `A → B → C` and shows you how it parsed the line right below the field
3. Pick **reference assets** (up to 9 images, 3 videos). Un-cut images are selectable but preview-only
4. **Generate prompt** → the prompt and a mechanical lint report (ERROR/WARN) appear on the right
5. Edit and **re-lint**. When happy, **write to `プロンプト.txt`** (an archive copy is written too, and it enters the project history)
6. **Recall from history** to restore a past brief and prompt
7. **Preview render** (608×352. **Timing varies a lot with the conditions — see the measured table under "Traps we hit"**) → a 3×3 contact sheet, the video, and metrics (frame count, real duration, bit rate, loudness, inter-frame difference). **Re-submitting the same prompt with the same seed returns ComfyUI's cache** rather than re-rendering — bump the seed
8. **Final render** (1344×768, **15.6 min measured — from a single run** on 2026-08-23 with two reference images. **The GPU power limit at the time was not recorded, so conditions may differ**). You get a warning if an un-cut image is selected — backgrounds leak at full resolution
9. **Accept this result** → overwrites `プロンプト.txt`, writes the archive, and records the video path, metrics and job id into the project's `shots[]`. **A human decides accept/reject; the app never judges**
10. **Past jobs** lists everything submitted from the app. Reloading the page reconnects to a render still in progress

While rendering, the LLM is unloaded so ComfyUI gets the GPU. **Going back to prompt generation frees ComfyUI's VRAM first, then reloads the LLM, then verifies it actually fits on the GPU** (the reverse order leaves part of the model on the CPU, which is several times slower and invisible in `lms ps`). If it is already loaded correctly, nothing happens. Nothing is submitted while ComfyUI's queue is busy. Step progress comes from ComfyUI's WebSocket — no core patch required.

### Cutout (SAM3)

Lifts a character out of source art onto a flat `#808080` background, saves it to `input\`, and selects it as a reference image.

Sweep `threshold` across 6 points to see how many people are detected at each, pick the value that gives you the right count, then click the people you want to keep. A preview and a `check_cut` inspection (background flatness, blob count, subject coverage, effective resolution) appear immediately; "tighten margins" typically lifts a small figure in a crowd from 2% to 30%+ coverage.

**Detection numbers are disposable.** `#N` is ranked by score, and rank turns out to track distance from the horizontal center — not size, facing or identity. Pick people by their thumbnail and attributes (position from left, hair color), never by index.

Constraints (all measured): `refine_iterations` must stay at 1 (2+ degrades the mask); do not use `negative_coords` (it produces dithered garbage); `:N` in the detection text is a **max count**, and `:1` is unusable due to a core parsing bug (Comfy-Org/ComfyUI#15811).

## Gotchas (all measured on the machine below)

- **LM Studio's per-model default config overrides `lms load --context-length`.** A stale `contextLength: 98304` in `~/.lmstudio/.internal/user-concrete-model-default-config/.../<file>.gguf.json` made prompt generation go from 16 s to 215 s. The app reads the effective context after loading, warns, and offers a "fix the default config" button (with a backup; it only writes when you click)
- **If ComfyUI is holding ~9 GB of VRAM, the LLM takes 35 s per prompt** versus 15 s with ComfyUI stopped. The header GPU pill shows this
- **Worse: loading LM Studio while ComfyUI holds VRAM puts part of the model on the CPU.** `lms ps` still reports the full size (17.16 GB), so nothing looks wrong — but prompt generation went from 45 s to 218 s. **Freeing ComfyUI afterwards does not move those weights back to the GPU**; the model has to be reloaded. The app frees ComfyUI first (only when a reload is actually needed), then measures the **VRAM delta across the load** to verify residency, and reloads once if it falls short (`gpu.prepare_for_llm`). **Comparing against total VRAM cannot detect this** — ComfyUI holds VRAM too, so the total exceeds the model size even when half of it is on the CPU. `/system_stats`'s `vram_free` is ComfyUI's own accounting and is not device-wide either
- **To disable thinking, use `reasoning_effort: "none"`.** `chat_template_kwargs.enable_thinking:false` alone does not work
- **Render time varies a lot between runs. The same settings are sometimes fast and sometimes slow.** Measured over 15 runs on 2026-08-25 (RTX 4090, 360 W power limit, preview 608×352, 192f, 6 steps):

  | Situation | Total |
  |---|---|
  | One render at a time (the model is reloaded every time) | **126 / 128 / 133 / 142 s** — but one run took **336 s** |
  | Back-to-back renders with the same settings (model stays loaded) | **45 / 49 s** |

  On the slow runs it is **step 1 (loading the model)** that stretches — 213–494 s versus 6–10 s on the fast ones. **We do not know what makes the difference.**
  Free VRAM does not explain it: **the same 20638 MB of free VRAM produced step 1 times of 10.28 s and 212.93 s** (another pair behaved the same way).
  Every stall we observed happened on a run that **reloaded the model**, but reloading does not reliably cause one (**4 of the 8 runs that reloaded**; none of the 2 runs that did not).
  **Rarely, a run can neither finish nor be cancelled** (`/interrupt` is only checked at block boundaries). **Only restarting ComfyUI recovers from that.**
- **⚠ `--reserve-vram 3` did not help.** This README used to recommend it as the cleaner fix; **that was retracted after measuring it on 2026-08-25.**
  With the flag set, stalls still happened (step 1 of 494 s), and a run still ended up neither finishing nor cancellable.
  Note that **whether the flag is honoured cannot be observed from outside** — it changes ComfyUI's internal load accounting, not the free VRAM the driver reports —
  so the only available evidence is behavioural, and **the behaviour did not improve**
- **ComfyUI's HTTP stalls while thrashing** — `/interrupt` took 41 s to answer. Press cancel and wait
- On Windows, `pkill -f "python server.py"` does not work. Use `Get-NetTCPConnection -LocalPort 8765 -State Listen | % { Stop-Process -Id $_.OwningProcess -Force }`

## Known limitations

- **Keep clips at 10 s or under.** Longer output degrades, and the safe frame count drops as you add reference assets (Comfy-Org/ComfyUI#15738)
- **Cutout is text-prompt only.** Point (`positive_coords`) and box (`bboxes`) inputs are not exposed
- Cutouts are always saved to `input\`
- `check_cut`'s blob count is unreliable when hair or props connect (one person can read as three). It warns; **you decide**
- **On-screen text is reliable only for everyday characters.** Hiragana, katakana, common JIS level-1 kanji and ASCII came out correctly 9/9, but **JIS level-2 kanji keep the right radical and get the wrong phonetic component, and variant forms (髙, 﨑) are silently normalized to the common form**. The failure mode is *a different real character*, not garbled shapes, so a reader who cannot read Japanese will not catch it. The app warns on input, but **a human must read the result**. Strings longer than 5 characters, multiple lines and vertical text are untested. Camera motion does not degrade the text (measured)
- **An optional watermark can be burned into the bottom-right of the finished video** (`gen.watermark` in `config.json`; **off by default**).
  It composites a grey mark — the app icon plus the word 絵巻 — at low opacity. **This is an ffmpeg post-process**, so the ComfyUI workflow is untouched.
  **The audio stream is copied as-is.** A failure never damages the original (it writes to a temp file, then swaps). Opacity, size and margin are configurable, and `image` can point at your own file
- The "auto" brief expansion (`/api/brief/expand`) does **not** handle the on-screen-text field yet; it and explanation-`.md` generation (`/api/prompt/notes`) are **unverified**
- The past-jobs list reads every entry in `jobs/` and will slow down as it grows
- The UI assumes `127.0.0.1` and **has no authentication — do not expose it**

## Verified environment

| | |
|---|---|
| GPU | NVIDIA RTX 4090 24GB |
| OS | Windows 11 Pro |
| ComfyUI | 0.33.1 (no `--reserve-vram`) |
| Python | 3.10 |
| LLM | LM Studio + `qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved@q4_k_s` (ctx 16384 / parallel 1 / MTP) |
| Eagle | 4.0.0 |

**Every number in this document was measured on that machine.** Yours will differ.

## Layout

```
server.py            FastAPI (--port to override)
h3studio/            config / project / llm / brief / promptgen / comfy (submit & progress) /
                     gpu (exclusion & VRAM residency) / inspect (ffmpeg) / cut (SAM3) / eagle /
                     textcheck (on-screen text screening)
static/              index.html / app.js / style.css (plain JS, no build step)
vendor/              bundled parts (h3gen / h3lint / system_h3.txt / check_cut / vlm / sweep.json / modes)
tools/               dev scripts (below)
projects/<name>/project.json
jobs/<job_id>/       job.json / contact.jpg / inspect.json
cutcache/<session>/  cutout detection results (full-size masks, thumbnails, preview); pruned past 12
config.json          your local values (gitignored)
config.example.json  template, generated from DEFAULTS
```

### Dev scripts

```bash
python tools/make_example_config.py --check
```

```bash
python tools/sync_vendor.py --check
```

```bash
python tools/validate_graph.py
```

- `make_example_config.py` — regenerates `config.example.json` from `h3studio/config.py`'s `DEFAULTS`. `--check` exits non-zero on drift
- `sync_vendor.py` — copies the canonical parts into `vendor/`. In a distributed copy the sources are absent and it exits 0 without doing anything
- `validate_graph.py` — checks the assembled API graph against ComfyUI's `/object_info`: node existence, required inputs, link targets. **No GPU needed.** Builds both the VHS and the core-only variant

## License

- **The app is MIT** — see [LICENSE](LICENSE)
- **MiniMax-H3 weights are not bundled.** They are covered by the **MiniMax H3 Community License** (separate permission required in the US/EU/UK/South Korea). ComfyUI, LM Studio and Eagle are not bundled either. **See [NOTICE](NOTICE) for what is excluded and under which terms**
- **⚠ Before publishing generated videos, please read [docs/LICENSE-NOTES.en.md](docs/LICENSE-NOTES.en.md).** It covers three points we noticed only after publishing: the territory restriction appears to extend to **Outputs**, there is an obligation to **disclose machine-generated content**, and the scope of the safeguard clause is unclear (**inquiry sent to MiniMax, awaiting reply**)
