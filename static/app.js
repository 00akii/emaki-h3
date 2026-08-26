/* 絵巻H3 — 段1〜3 のフロント。素の JS。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    project: null, projName: null, mode: "B",
    images: new Set(), videos: new Set(),
    prompt: "", brief: "", lint: null, h3mode: "ref2va", lastGen: null,
    models: null, cloud: false, cloudOk: false,
  };

  // ---------- 共通 ----------
  const api = async (path, opt = {}) => {
    const r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opt));
    let j = null; try { j = await r.json(); } catch (_) {}
    if ((r.status === 402 || r.status === 409) && j && j.need_confirm) { const e = new Error(j.reason || "need_confirm"); e.need_confirm = true; e.reason = j.reason; e.raw = j.raw; e.status = r.status; throw e; }
    if (!r.ok) { const e = new Error((j && (j.detail || j.error)) || r.statusText); e.status = r.status; throw e; }
    return j;
  };
  const toast = (msg, kind = "") => { const t = $("toast"); t.textContent = msg; t.className = "toast show " + kind; clearTimeout(t._t); t._t = setTimeout(() => t.className = "toast", 3200); };
  const open = (id) => $(id).classList.add("show");
  const close = (id) => $(id).classList.remove("show");
  document.querySelectorAll("[data-close]").forEach(b => b.onclick = () => close(b.dataset.close));
  document.querySelectorAll(".modalbg").forEach(m => m.addEventListener("click", e => { if (e.target === m) m.classList.remove("show"); }));
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // 進捗
  const STEP = { llm: 0, gen: 1, lint: 2, gpu: 3, comfy: 4 };
  const setStep = (name, st) => { const el = $("steps").children[STEP[name]]; el.className = "step " + st; };
  const resetSteps = () => { [...$("steps").children].forEach(c => c.className = "step"); };
  const log = (line, cls = "") => { const l = $("log"); if (l.textContent === "待機中") l.textContent = ""; const s = document.createElement("div"); s.className = cls; s.textContent = line; l.appendChild(s); l.scrollTop = l.scrollHeight; };
  let timer = null, t0 = 0;
  const startTimer = () => { t0 = Date.now(); clearInterval(timer); timer = setInterval(() => { const s = Math.floor((Date.now() - t0) / 1000); $("progTime").textContent = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }, 500); };
  const stopTimer = () => clearInterval(timer);

  // ---------- 設定・GPU 表示 ----------
  async function loadConfig() {
    const j = await api("/api/config");
    state.cloud = !!j.llm_cloud;
    $("pillCloud").style.display = state.cloud ? "inline-flex" : "none";
    if (!state.cloud) $("pillCloud").textContent = "☁ クラウド使用中 · 料金がかかります";
    if (state.cloud && j.usage) {
      const u = j.usage;
      $("pillCloud").textContent = "☁ クラウド使用中 · " + u.calls + "回 · " + ((u.prompt_tokens + u.completion_tokens) / 1000).toFixed(1) + "k tok" + (u.estimated_usd != null ? " · ≈$" + u.estimated_usd.toFixed(3) : "") + " · 料金がかかります";
    }
    $("cfgSource").textContent = j.source || "";
    const dl = $("cfgChecks"); dl.innerHTML = "";
    let bad = 0;
    j.checks.forEach(c => { if (!c.ok) bad++; dl.insertAdjacentHTML("beforeend", `<dt>${esc(c.key)}</dt><dd class="${c.ok ? "ok" : "bad"}">${c.ok ? "OK" : "NG"} · ${esc(c.detail)}</dd>`); });
    const comfy = j.checks.find(c => c.key === "comfy_url");
    const lms = j.checks.find(c => c.key === "lmstudio_url");
    $("pillComfy").innerHTML = `<span class="dot ${comfy && comfy.ok ? "on" : ""}"></span>ComfyUI · ${comfy && comfy.ok ? "起動中" : "未接続"}`;
    const cur = (j.config.llm && j.config.llm.backend === "openai_compat") ? (j.config.llm.openai_compat.model || "未選択") : (j.config.lmstudio_model || "未選択");
    $("pillLlm").innerHTML = `<span class="dot ${state.cloud ? "on" : (lms && lms.ok ? "on" : "")}"></span>${state.cloud ? "クラウド" : "LM Studio"} · ${esc(cur.length > 28 ? cur.slice(0, 28) + "…" : cur)}`;
    $("btnSettings").className = "btn sec sm" + (bad ? " " : "");
    if (bad) $("btnSettings").textContent = "設定 (" + bad + ")"; else $("btnSettings").textContent = "設定";
    return j;
  }
  async function loadPreflight(refresh) {
    const el = $("pfNodes"), msg = $("pfMsg");
    if (!el) return;
    el.innerHTML = "<dd class=\"sub\">確認中…</dd>";
    let j;
    try { j = await api("/api/preflight" + (refresh ? "?refresh=1" : "")); }
    catch (e) { msg.textContent = ""; el.innerHTML = `<dd class="bad">確認できません: ${esc(e.message)}</dd>`; return; }
    msg.textContent = j.message || "";
    msg.className = "sub " + (j.can_generate ? "" : "bad");
    if (!j.comfy) { el.innerHTML = `<dd class="bad">${esc(j.message)}</dd>`; return; }
    const label = { required: "必須", fallback: "代替あり", optional: "任意" };
    el.innerHTML = j.nodes.map(n => {
      const cls = n.ok ? "ok" : (n.need === "required" ? "bad" : "warn");
      const tail = n.ok ? esc(n.what) : esc(n.fallback || n.what) + "（" + esc(n.pack) + "）";
      return `<dt>${esc(n.cls)}</dt><dd class="${cls}">${n.ok ? "OK" : "無し"} · ${label[n.need]} · ${tail}</dd>`;
    }).join("");
    const me = $("pfModels");
    if (me) me.innerHTML = (j.models || []).map(m =>
      `<dt>${esc(m.what)}</dt><dd class="${m.ok ? "ok" : "bad"}">${m.ok ? "OK" : "無し"} · ${esc(m.name || "（不明）")} · ${esc(m.detail)}</dd>`
    ).join("") || `<dd class="sub">—</dd>`;
  }

  $("btnSettings").onclick = async () => { open("mdSettings"); await loadEagle(true); loadPreflight(false); };
  $("btnRecheck").onclick = async () => { await loadConfig(); await loadPreflight(true); toast("再チェックしました"); };

  // ---------- Eagle ----------
  state.eagle = { up: false, enabled: false, folders: null };
  async function loadEagle(withFolders) {
    let j; try { j = await api("/api/eagle/info"); } catch (_) { return; }
    state.eagle.up = j.up; state.eagle.enabled = j.enabled;
    $("eagleEnv").textContent = j.up ? ("Eagle " + (j.version || "") + (j.library ? " · " + j.library : "")) : (j.error || "未接続");
    $("eagleEnv").style.color = j.up ? "var(--ok)" : "var(--warn)";
    $("egEnabled").checked = !!j.enabled; $("egAuto").value = j.auto || "off"; $("egSheet").checked = !!j.send_contact_sheet;
    // 結果カードのボタンは「有効かつ Eagle が起動している」ときだけ
    const btn = $("btnEagle"); if (btn) btn.style.display = (j.enabled && j.up) ? "" : "none";
    if (withFolders && j.up && !state.eagle.folders) await loadEagleFolders(j.folder_id);
    else if (j.folder_id) $("egFolder").value = j.folder_id;
    return j;
  }
  async function loadEagleFolders(selectId) {
    const sel = $("egFolder");
    try {
      const j = await api("/api/eagle/folders"); state.eagle.folders = j.folders;
      sel.innerHTML = '<option value="">（ライブラリ直下）</option>';
      j.folders.forEach(f => { const o = document.createElement("option"); o.value = f.id; o.textContent = f.path; sel.appendChild(o); });
      if (selectId) sel.value = selectId;
    } catch (e) { toast("Eagle のフォルダが取れません: " + e.message, "warn"); }
  }
  $("btnEgReload").onclick = async () => { state.eagle.folders = null; const j = await api("/api/eagle/info"); await loadEagleFolders(j.folder_id); toast("フォルダを取り直しました"); };
  $("btnEgSave").onclick = async () => {
    const sel = $("egFolder");
    await api("/api/config", { method: "PUT", body: JSON.stringify({ eagle: {
      enabled: $("egEnabled").checked, auto: $("egAuto").value, send_contact_sheet: $("egSheet").checked,
      folder_id: sel.value, folder_name: sel.value ? sel.options[sel.selectedIndex].textContent : "" } }) });
    await loadEagle(false); toast("Eagle の設定を保存しました");
  };
  $("btnEagle").onclick = async (ev) => {
    const j = state.resultJob; if (!j) return;
    ev.target.disabled = true;
    try {
      const r = await api("/api/eagle/send", { method: "POST", body: JSON.stringify({ job_id: j.id }) });
      toast("Eagle に送りました（" + r.count + "件）");
      $("foot").innerHTML = r.sent.map(x => `<span>Eagle <code>${esc(x.name)}</code></span>`).join("");
    } catch (e) { toast("Eagle への送信に失敗: " + e.message, "bad"); }
    finally { ev.target.disabled = false; }
  };

  // ---------- プロジェクト ----------
  async function loadProjects(selectName) {
    const j = await api("/api/projects");
    const sel = $("projSel"); sel.innerHTML = "";
    j.projects.forEach(p => { const o = document.createElement("option"); o.value = p.name; o.textContent = p.name + (p.shots ? "（" + p.shots + "）" : ""); sel.appendChild(o); });
    if (!j.projects.length) { const o = document.createElement("option"); o.value = ""; o.textContent = "（作品を作成してください）"; sel.appendChild(o); }
    const name = selectName || localStorage.getItem("h3.proj") || (j.projects[0] && j.projects[0].name);
    if (name) { sel.value = name; await loadProject(name); }
  }
  async function loadProject(name) {
    if (!name) return;
    state.project = await api("/api/projects/" + encodeURIComponent(name));
    state.projName = name; localStorage.setItem("h3.proj", name);
    $("shotId").textContent = nextShotId(state.project);
    // 既定の選択: project.subjects[].images と ref_videos を全選択
    state.images = new Set(); state.videos = new Set();
    (state.project.subjects || []).forEach(s => (s.images || []).forEach(i => state.images.add(i)));
    (state.project.ref_videos || []).forEach(v => state.videos.add(v.file));
    if (state.project.defaults) { if (state.project.defaults.duration) $("duration").value = state.project.defaults.duration; if (state.project.defaults.ratio) $("ratio").value = state.project.defaults.ratio; }
    if (state.project.comfy && state.project.comfy.seed != null) $("seed").value = state.project.comfy.seed;
    await loadAssets();
    updateFrames();
  }
  const nextShotId = (p) => { const n = (p.shots || []).map(s => parseInt((s.id || "").replace(/\D/g, "")) || 0); return "S" + String((n.length ? Math.max(...n) : 0) + 1).padStart(2, "0"); };
  $("projSel").onchange = (e) => loadProject(e.target.value);
  $("projNew").onclick = () => { $("newProjName").value = ""; open("mdNewProj"); };

  // ---------------- 作品の設定（マニュアル担当の依頼 2026-08-24。mdNewProj の案内先が存在しなかった） ----------------
  // PUT /api/projects/{name} は**全置換**。編集した欄だけ差し込んだ完全なオブジェクトを送る（shots・image_roles を消さないため）
  let pEdit = null;   // 編集中のコピー。保存するまで state.project に触らない
  $("projEdit").onclick = async () => {
    if (!state.projName) return toast("先に作品を選んでください", "warn");
    pEdit = JSON.parse(JSON.stringify(await api("/api/projects/" + encodeURIComponent(state.projName))));
    $("projEditName").textContent = state.projName;
    $("pStyle").value = pEdit.style || "";
    $("pDuration").value = (pEdit.defaults && pEdit.defaults.duration) || 8;
    $("pRatio").value = (pEdit.defaults && pEdit.defaults.ratio) || "16:9";
    $("pMusic").value = (pEdit.defaults && pEdit.defaults.music) || "";
    $("pSeed").value = (pEdit.comfy && pEdit.comfy.seed != null) ? pEdit.comfy.seed : 1;
    $("pMsg").textContent = "";
    await renderPSubjects(); renderPVideos();
    open("mdProj");
  };

  async function pAssetLists() {
    const [im, vi] = await Promise.all([api("/api/assets/images?cut_only=false"), api("/api/assets/videos")]);
    return { images: im.items || [], videos: vi.items || [] };
  }

  async function renderPSubjects(filter) {
    // input\ には生画像が数百枚あるので、既定は切り抜き済み（_cut）だけを出す。
    // 検索で生画像も探せる（選択済みのものは絞り込みに関係なく常に出す）
    const { images } = await pAssetLists();
    const box = $("pSubjects"); box.innerHTML = "";
    const f = (filter || "").toLowerCase();
    (pEdit.subjects || []).forEach((sub, i) => {
      const d = document.createElement("div");
      d.className = "card"; d.style.cssText = "padding:8px;margin-bottom:8px";
      const picked = new Set(sub.images || []);
      d.innerHTML = `
        <div class="row" style="margin-bottom:6px">
          <input class="in mini" style="width:130px" data-k="label" value="${esc(sub.label || ("Subject " + (i + 1)))}" title="ラベル（Subject N）">
          <span class="sp"></span>
          <button class="btn sec sm" data-del="${i}">削除</button>
        </div>
        <textarea class="in" rows="2" data-k="description" placeholder="見た目の説明（髪・目・服。参照画像に写っているとおりに）">${esc(sub.description || "")}</textarea>
        <div class="row" data-imgs style="margin-top:6px;flex-wrap:wrap;gap:4px"></div>`;
      const imgRow = d.querySelector("[data-imgs]");
      const shown = images.filter(img => picked.has(img.name) || (f ? img.name.toLowerCase().includes(f) : img.cut));
      shown.slice(0, 60).forEach(img => {
        const name = img.name || img;
        const b = document.createElement("button");
        b.className = "chip" + (picked.has(name) ? " on" : "");
        b.title = name;
        b.innerHTML = `<img src="/api/file/input/${encodeURIComponent(name)}" style="width:34px;height:34px;object-fit:cover;border-radius:3px;vertical-align:middle"> ${esc(name.length > 22 ? name.slice(0, 20) + "…" : name)}${img.cut === false ? ' <span class="t">生</span>' : ""}`;
        b.onclick = () => { if (picked.has(name)) { picked.delete(name); b.classList.remove("on"); } else { picked.add(name); b.classList.add("on"); } sub.images = [...picked]; };
        imgRow.appendChild(b);
      });
      d.querySelector('[data-k="label"]').oninput = (e) => sub.label = e.target.value;
      d.querySelector('[data-k="description"]').oninput = (e) => sub.description = e.target.value;
      if (shown.length > 60) {
        const more = document.createElement("span"); more.className = "hint";
        more.textContent = "…ほか " + (shown.length - 60) + " 枚（検索で絞ってください）"; imgRow.appendChild(more);
      }
      d.querySelector("[data-del]").onclick = () => { pEdit.subjects.splice(i, 1); renderPSubjects($("pImgFilter").value); };
      box.appendChild(d);
    });
    if (!(pEdit.subjects || []).length) box.innerHTML = '<div class="hint">キャラ定義がありません。「＋ キャラを追加」から。</div>';
  }
  let pFilterT = null;
  $("pImgFilter").oninput = () => { clearTimeout(pFilterT); pFilterT = setTimeout(() => renderPSubjects($("pImgFilter").value), 300); };
  $("pAddSubj").onclick = () => { (pEdit.subjects = pEdit.subjects || []).push({ label: "Subject " + ((pEdit.subjects.length || 0) + 1), description: "", images: [], image_roles: {} }); renderPSubjects(); };

  async function renderPVideos() {
    const { videos } = await pAssetLists();
    const box = $("pVideos"); box.innerHTML = "";
    (pEdit.ref_videos || []).forEach((rv, i) => {
      const d = document.createElement("div");
      d.className = "row"; d.style.cssText = "margin-bottom:6px;gap:6px";
      const opts = videos.map(v => { const n = v.name || v; return `<option value="${esc(n)}"${n === rv.file ? " selected" : ""}>${esc(n)}</option>`; }).join("");
      d.innerHTML = `
        <select class="in mini" style="width:230px" data-k="file"><option value="">（ファイルを選ぶ）</option>${opts}</select>
        <input class="in mini" style="flex:1" data-k="description" placeholder="動きの説明（何がどう動くか）" value="${esc(rv.description || "")}">
        <button class="btn sec sm" data-del>削除</button>`;
      d.querySelector('[data-k="file"]').onchange = (e) => rv.file = e.target.value;
      d.querySelector('[data-k="description"]').oninput = (e) => rv.description = e.target.value;
      d.querySelector("[data-del]").onclick = () => { pEdit.ref_videos.splice(i, 1); renderPVideos(); };
      box.appendChild(d);
    });
    if (!(pEdit.ref_videos || []).length) box.innerHTML = '<div class="hint">参照動画はありません（無くても生成できます）。</div>';
  }
  $("pAddVid").onclick = () => { (pEdit.ref_videos = pEdit.ref_videos || []).push({ file: "", description: "" }); renderPVideos(); };

  $("pSave").onclick = async () => {
    pEdit.style = $("pStyle").value.trim();
    pEdit.defaults = Object.assign({}, pEdit.defaults, {
      duration: parseInt($("pDuration").value) || 8,
      ratio: $("pRatio").value,
      music: $("pMusic").value.trim() || "N/A",
    });
    pEdit.comfy = Object.assign({}, pEdit.comfy, { seed: parseInt($("pSeed").value) || 1 });
    pEdit.ref_videos = (pEdit.ref_videos || []).filter(v => v.file);   // ファイル未選択の行は保存しない
    await api("/api/projects/" + encodeURIComponent(state.projName), { method: "PUT", body: JSON.stringify(pEdit) });
    close("mdProj");
    await loadProject(state.projName);   // 参照素材の既定選択・seed 等を反映し直す
    toast("保存しました");
  };
  $("btnCreateProj").onclick = async () => { const n = $("newProjName").value.trim(); if (!n) return toast("作品名を入れてください", "warn"); await api("/api/projects", { method: "POST", body: JSON.stringify({ name: n }) }); close("mdNewProj"); await loadProjects(n); toast("作成しました: " + n); };

  // ---------- 参照素材 ----------
  // ---------- エクスプローラーからのドロップ ----------
  // 一覧の検索欄を自前で持たず、**探す仕事をエクスプローラーに任せる**ための入り口（ユーザーの案・2026-08-26）。
  // ブラウザはフルパスを渡さない（仕様）ので、中身ごとサーバーに送って input\ に置く。
  // 同名でサイズも同じなら既存を使う（同じものを2回落としただけ）。違えば連番で逃がす —— server.py の /api/upload 参照。
  const IMG_DROP = [".png", ".jpg", ".jpeg", ".webp"], VID_DROP = [".mp4", ".webm", ".mov"];
  function enableDrop(el, opts) {
    if (!el || el._dropOn) return;
    el._dropOn = true;
    const stop = e => { e.preventDefault(); e.stopPropagation(); };
    el.addEventListener("dragover", e => { stop(e); el.classList.add("dropping"); });
    el.addEventListener("dragleave", e => { stop(e); el.classList.remove("dropping"); });
    el.addEventListener("drop", async e => {
      stop(e); el.classList.remove("dropping");
      const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
      if (!files.length) return;
      // python-multipart が無い環境。**黙って何も起きないのが一番たちが悪い**ので理由を出す
      if (state.features && state.features.upload === false) {
        return toast(state.features.upload_hint || "この環境ではドロップ取り込みを使えません", "warn");
      }
      const exts = typeof opts.exts === "function" ? opts.exts() : opts.exts;
      const okFiles = [], bad = [];
      files.forEach(f => (exts.includes((String(f.name).match(/\.[a-z0-9]+$/i) || [""])[0].toLowerCase()) ? okFiles : bad).push(f));
      if (bad.length) toast(bad.length + " 件は扱えない種類です: " + bad.slice(0, 2).map(f => f.name).join(", "), "warn");
      if (!okFiles.length) return;
      const dest = typeof opts.dest === "function" ? opts.dest() : opts.dest;
      toast(okFiles.length + " 件を取り込んでいます…");
      const added = [];
      for (const f of okFiles) {
        const fd = new FormData(); fd.append("dest", dest); fd.append("file", f, f.name);
        try {
          const r = await fetch("/api/upload", { method: "POST", body: fd });   // FormData なので api() は使わない
          let j = null; try { j = await r.json(); } catch (_) {}
          if (!r.ok) { toast(f.name + ": " + ((j && j.detail) || r.statusText), "bad"); continue; }
          added.push(j.name);
        } catch (err) { toast(f.name + ": " + err.message, "bad"); }
      }
      if (added.length) await opts.onDone(added);
    });
  }

  async function loadAssets() {
    const cutOnly = $("cutOnly").checked;
    const [im, vi] = await Promise.all([api("/api/assets/images?cut_only=" + (cutOnly ? "true" : "false")), api("/api/assets/videos")]);
    // **選択済みは上限で切らずに必ず出す。** 並びは更新日時順なので、古い素材を選ぶと一覧から消え、
    // 「選ばれていて生成にも使われるのに、画面で確認も解除もできない」状態になっていた（2026-08-26 に実機で2本発見）
    const pinFirst = (items, isOn, cap) => {
      const on = items.filter(x => isOn(x.name));
      return on.concat(items.filter(x => !isOn(x.name)).slice(0, Math.max(0, cap - on.length)));
    };
    const box = $("imgRefs"); box.innerHTML = "";
    pinFirst(im.items, n => state.images.has(n), 40).forEach(it => {
      const b = document.createElement("button"); b.className = "ref" + (state.images.has(it.name) ? " on" : ""); b.title = it.name;
      b.innerHTML = `<span class="chk">✓</span><img loading="lazy" src="/api/file/input/${encodeURIComponent(it.name)}" alt="">${it.cut ? "" : '<span class="raw">生</span>'}<span>${esc(it.name.replace(/\.(png|jpg|jpeg|webp)$/i, ""))}</span>`;
      b.onclick = () => { if (state.images.has(it.name)) state.images.delete(it.name); else { if (state.images.size >= 9) return toast("画像は9枚まで", "warn"); state.images.add(it.name); } b.classList.toggle("on"); updateCounts(); };
      box.appendChild(b);
    });
    const add = document.createElement("button"); add.className = "ref add"; add.innerHTML = "＋<small>切り抜く</small>"; add.title = "SAM3 で人物を単色背景に抜く"; add.onclick = openCut; box.appendChild(add);
    const vb = $("vidRefs"); vb.innerHTML = "";
    pinFirst(vi.items, n => state.videos.has(n), 20).forEach(it => {
      const b = document.createElement("button"); b.className = "ref vid" + (state.videos.has(it.name) ? " on" : ""); b.title = it.name;
      // 静止時は先頭フレーム（= エクスプローラーと同じ絵）。ホバーしたときだけ実物を読んで再生する。
      // 全部を <video> にすると、Range 非対応のため 1 本 2.6MB × 20 本を読みに行く（h3studio/thumbs.py 参照）
      b.innerHTML = `<span class="chk">✓</span><img loading="lazy" src="/api/thumb/video/${encodeURIComponent(it.name)}" alt=""><span>${esc(it.name.replace(/\.(mp4|webm|mov)$/i, ""))}</span>`;
      b.onmouseenter = () => {
        if (b.querySelector("video")) return;
        const im = b.querySelector("img"); if (!im) return;
        const v = document.createElement("video");
        v.src = "/api/file/input/" + encodeURIComponent(it.name);
        v.muted = true; v.loop = true; v.playsInline = true; v.preload = "auto";
        im.style.display = "none"; im.insertAdjacentElement("afterend", v);
        v.play().catch(() => { v.remove(); im.style.display = ""; });   // 再生できなければ静止画に戻す
      };
      b.onmouseleave = () => {
        const v = b.querySelector("video"); if (v) { v.pause(); v.removeAttribute("src"); v.load(); v.remove(); }
        const im = b.querySelector("img"); if (im) im.style.display = "";
      };
      b.onclick = () => { if (state.videos.has(it.name)) state.videos.delete(it.name); else { if (state.videos.size >= 3) return toast("動画は3本まで", "warn"); state.videos.add(it.name); } b.classList.toggle("on"); updateCounts(); };
      vb.appendChild(b);
    });
    // エクスプローラーから直接落とせるようにする。落としたものは選択済みにして先頭に出す
    enableDrop(box, {
      dest: "input", exts: IMG_DROP,
      onDone: async names => {
        const room = 9 - state.images.size;
        names.slice(0, Math.max(0, room)).forEach(n => state.images.add(n));
        await loadAssets();
        toast(names.length > room ? ("画像は9枚まで。" + room + " 枚だけ選びました") : (names.length + " 枚を追加しました"),
              names.length > room ? "warn" : "");
      }
    });
    enableDrop(vb, {
      dest: "input", exts: VID_DROP,
      onDone: async names => {
        const room = 3 - state.videos.size;
        names.slice(0, Math.max(0, room)).forEach(n => state.videos.add(n));
        await loadAssets();
        toast(names.length > room ? ("動画は3本まで。" + room + " 本だけ選びました") : (names.length + " 本を追加しました"),
              names.length > room ? "warn" : "");
      }
    });
    updateCounts();
  }
  const updateCounts = () => { $("imgCount").textContent = state.images.size + "/9"; $("vidCount").textContent = state.videos.size + "/3"; updateFrames(); };
  $("cutOnly").onchange = loadAssets;

  // 尺 → フレーム（n%17==5 切り上げ、実測グリッド）
  const framesFor = (d) => { let n = Math.round(d * 24); n += (5 - (n % 17) + 17) % 17; return n; };
  const updateFrames = () => { const d = parseInt($("duration").value || "8"); const f = framesFor(d); $("frames").textContent = "→" + f + "f · " + (f / 24).toFixed(2) + "s"; const refs = state.images.size + state.videos.size; if ((state.images.size >= 3 || state.videos.size >= 2) && d > 8) $("frames").style.color = "var(--warn)"; else $("frames").style.color = ""; };
  $("duration").onchange = updateFrames;

  // ---------- モード・ヘルプ・カメラ ----------
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => { document.querySelectorAll(".tab").forEach(x => x.classList.remove("on")); t.classList.add("on"); state.mode = t.dataset.mode; ["A", "B", "C"].forEach(m => $("mode" + m).style.display = m === state.mode ? "" : "none"); });
  document.querySelectorAll(".q").forEach(q => q.onclick = () => { const h = $(q.dataset.help); h.classList.toggle("hidden"); q.classList.toggle("on", !h.classList.contains("hidden")); });
  function chipRow(boxId, inputId, items) {
    // items: 文字列 or {label, group, note}。クリックで入力欄に入れる。同じ値をもう一度押すと外す
    const box = $(boxId); box.innerHTML = "";
    let lastGroup = null;
    items.forEach(it => {
      const p = typeof it === "string" ? { label: it } : it;
      if (p.group && p.group !== lastGroup) { const g = document.createElement("span"); g.className = "chipgrp"; g.textContent = p.group; box.appendChild(g); lastGroup = p.group; }
      const c = document.createElement("button"); c.className = "chip"; c.textContent = p.label + (p.note ? " △" : ""); c.dataset.v = p.label;
      if (p.note) c.title = p.note;
      c.onclick = () => { const on = $(inputId).value === p.label; $(inputId).value = on ? "" : p.label; syncChips(boxId, inputId); framingCheck(); };
      box.appendChild(c);
    });
  }
  const syncChips = (boxId, inputId) => $(boxId).querySelectorAll(".chip").forEach(x => x.classList.toggle("on", x.dataset.v === $(inputId).value));
  async function loadCamera() {
    const j = await api("/api/camera/presets");
    chipRow("camChips", "bCamera", j.presets);
    chipRow("framChips", "bFraming", j.framing || []);
  }
  $("bCamera").oninput = () => { syncChips("camChips", "bCamera"); framingCheck(); };
  $("bFraming").oninput = () => { syncChips("framChips", "bFraming"); framingCheck(); };
  $("bText").oninput = () => textCheck();

  // 開始の構図 × 終端の組み合わせ判定（素通り / 修正 / 警告）。LLM は使わない。サーバーの brief.check_framing が唯一の判定点
  let fcT = null;
  function framingCheck() {
    clearTimeout(fcT);
    fcT = setTimeout(async () => {
      const el = $("framingCheck"); const f = $("bFraming").value.trim(), c = $("bCamera").value.trim();
      if (!f) { el.textContent = ""; el.className = "hint"; return; }
      try {
        const r = await api("/api/brief/framing-check", { method: "POST", body: JSON.stringify({ framing: f, camera: c }) });
        showFramingCheck(r);
      } catch (e) { el.textContent = ""; }
    }, 250);
  }
  function showFramingCheck(r) {
    const el = $("framingCheck"); if (!r) { el.textContent = ""; return; }
    const label = { pass: "そのまま通す", fix: "修正が入る", warn: "成立しにくい組み合わせ" }[r.action] || r.action;
    const cls = { pass: "ok", fix: "warn", warn: "bad" }[r.action] || "";
    el.innerHTML = `<span class="badge ${cls}">${label}</span> ${r.reason ? esc(r.reason) : ""}`;
    el.className = "hint";
  }

  // 「画面内の文字」欄の点検（JIS 水準と長さ）。LLM は使わない。サーバーの textcheck.check が唯一の判定点
  let tcT = null;
  function textCheck() {
    clearTimeout(tcT);
    tcT = setTimeout(async () => {
      const el = $("textCheck"); const t = $("bText").value.trim();
      if (!t) { el.textContent = ""; el.className = "hint"; return; }
      try {
        const r = await api("/api/brief/text-check", { method: "POST", body: JSON.stringify({ text: t }) });
        showTextCheck(r);
      } catch (e) { el.textContent = ""; }
    }, 250);
  }
  function showTextCheck(r) {
    const el = $("textCheck"); if (!r || r.action === "none") { el.textContent = ""; el.className = "hint"; return; }
    // 警告の理由でラベルを変える。字が化けるのと、長さが未検証なのは別の話
    const label = { ok: "出ます", substitution: "別の字になる恐れ", length: "長さが未検証" }[r.kind] || (r.action === "pass" ? "出ます" : "注意");
    const cls = r.action === "pass" ? "ok" : "warn";
    const what = `「${esc(r.text)}」${r.carrier ? "を" + esc(r.carrier) + "に" : ""}`;
    el.innerHTML = `<span class="badge ${cls}">${label}</span> ${what}${r.reason ? " — " + esc(r.reason) : ""}`;
    el.className = "hint";
  }

  // 「動き」欄をどう段階に分けたかをその場で見せる（区切りは何で書いてもよい）
  let stepsT = null;
  async function showMotionSteps() {
    const t = $("bMotion").value;
    const box = $("motionSteps");
    if (!t.trim()) { box.innerHTML = ""; return; }
    try {
      const j = await api("/api/brief/steps", { method: "POST", body: JSON.stringify({ text: t }) });
      const n = j.steps.length;
      box.innerHTML = j.steps.map((s, i) => `<span class="st2"><i>${i + 1}</i>${esc(s)}</span>`).join('<span class="ar">▸</span>')
        + `<span class="sep ${n < 2 ? "warn1" : ""}">${n}段階 · ${esc(j.separator)}</span>`;
    } catch (_) { box.innerHTML = ""; }
  }
  $("bMotion").oninput = () => { clearTimeout(stepsT); stepsT = setTimeout(showMotionSteps, 250); };

  // ---------- モデル ----------
  async function loadModels() {
    const j = await api("/api/models"); state.models = j;
    $("backendSel").value = j.backend; $("cloudFields").style.display = j.backend === "openai_compat" ? "" : "none";
    $("cloudWarnModels").style.display = j.cloud ? "" : "none";
    const cfg = (await api("/api/config")).config;
    if (cfg.llm && cfg.llm.openai_compat) { $("ocBase").value = cfg.llm.openai_compat.base_url || ""; $("ocKeyEnv").value = cfg.llm.openai_compat.api_key_env || ""; $("ocModel").value = cfg.llm.openai_compat.model || ""; }
    const list = $("modelList"); list.innerHTML = "";
    if (!j.models.length) list.innerHTML = `<div class="empty">モデル一覧が取れません。${j.cloud ? "base_url と API キーを確認" : "LM Studio が起動しているか確認"}してください。</div>`;
    j.models.forEach(m => {
      const row = document.createElement("div"); row.className = "mrow" + (m.id === j.current ? " on" : ""); row.dataset.id = m.id;
      const lab = m.measured ? `<div class="lab ok">${esc(m.measured.label)} · ${m.measured.avg_seconds}秒/回</div>` : `<div class="lab warn">未検証 — このモデルでの合格率は測っていません</div>`;
      row.innerHTML = `<div><div class="id">${esc(m.id)}${m.recommended ? '<span class="tag">推奨</span>' : ""}${m.loaded ? '<span class="tag" style="background:var(--ok-soft);color:var(--ok)">ロード済み</span>' : ""}</div>${lab}</div><div></div>`;
      row.onclick = () => { list.querySelectorAll(".mrow").forEach(x => x.classList.remove("on")); row.classList.add("on"); };
      list.appendChild(row);
    });
  }
  $("btnModels").onclick = async () => { open("mdModels"); await loadModels(); };
  $("backendSel").onchange = () => { $("cloudFields").style.display = $("backendSel").value === "openai_compat" ? "" : "none"; $("cloudWarnModels").style.display = $("backendSel").value === "openai_compat" ? "" : "none"; };
  $("btnSaveModel").onclick = async () => {
    const be = $("backendSel").value;
    const body = { llm: { backend: be, openai_compat: { base_url: $("ocBase").value.trim(), api_key_env: $("ocKeyEnv").value.trim() || "H3STUDIO_LLM_KEY", model: $("ocModel").value.trim() } } };
    const sel = $("modelList").querySelector(".mrow.on");
    if (be === "lmstudio" && sel) body.lmstudio_model = sel.dataset.id;
    if (be === "openai_compat" && sel && !body.llm.openai_compat.model) body.llm.openai_compat.model = sel.dataset.id;
    await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
    await loadConfig(); await loadModels(); toast("保存しました");
  };
  $("btnLoadModel").onclick = async () => { const sel = $("modelList").querySelector(".mrow.on"); if (!sel) return toast("モデルを選んでください", "warn"); toast("ロード中…（20〜60秒）"); const r = await api("/api/models/load", { method: "POST", body: JSON.stringify({ id: sel.dataset.id }) }); if (r.skipped) toast("クラウドではロード不要"); else if (r.ok) toast("ロード完了 " + (r.already ? "（既に載っていました）" : r.seconds + "秒")); else toast("ロード失敗: " + (r.error || ""), "bad"); await loadModels(); await loadConfig(); };
  $("btnUnload").onclick = async () => { await api("/api/models/unload", { method: "POST" }); toast("降ろしました"); await loadModels(); await loadConfig(); };

  // ---------- LM Studio 既定設定の修正 ----------
  function showPinnedFix() {
    if (document.getElementById("btnFixCtx")) return;
    const b = document.createElement("button"); b.id = "btnFixCtx"; b.className = "btn sec sm"; b.style.marginTop = "6px";
    b.textContent = "LM Studio の既定設定を直す（ctx → 検証済みの値）";
    b.onclick = async () => { b.disabled = true; toast("既定設定を書き換えて載せ直し中…"); const r = await api("/api/models/fix-context", { method: "POST", body: JSON.stringify({ reload: true }) }); if (r.ok) { log("既定設定を修正: " + r.before + " → " + r.after + "（バックアップ: " + r.backup + "）", "ok"); toast("直しました。次回から速くなります"); b.remove(); } else { toast("失敗: " + (r.error || ""), "bad"); b.disabled = false; } await loadGpu(); };
    $("log").appendChild(b);
  }
  async function loadGpu() {
    try {
      const g = await api("/api/gpu");
      const n = g.nvidia; const c = g.comfy;
      $("pillComfy").innerHTML = `<span class="dot ${c && c.up ? "on" : ""}"></span>ComfyUI · ${c && c.up ? (c.running ? "生成中" : "起動中") + (c.holding_gb > 1 ? " · " + c.holding_gb + "GB保持" : "") : "未接続"}`;
      let gpuPill = document.getElementById("pillGpu");
      if (!gpuPill) { gpuPill = document.createElement("span"); gpuPill.id = "pillGpu"; gpuPill.className = "pill"; $("pillComfy").after(gpuPill); }
      if (n) gpuPill.textContent = (n.used_mb / 1024).toFixed(1) + " / " + (n.total_mb / 1024).toFixed(1) + " GB";
      gpuPill.className = "pill" + (g.note ? " cloud" : ""); gpuPill.title = g.note || "";
      if (g.note && !state.gpuNoteShown) { log("⚠ " + g.note, "warn"); state.gpuNoteShown = true; }
    } catch (_) {}
  }

  // ---------- クラウド確認 ----------
  function confirmCloud(reason) {
    return new Promise(res => {
      if (!state.cloud || state.cloudOk) return res(true);
      $("cloudReason").textContent = reason || "";
      api("/api/config").then(j => { const u = j.usage || {}; $("cloudUsage").innerHTML = `<dt>これまで</dt><dd>${u.calls || 0}回 · ${(((u.prompt_tokens || 0) + (u.completion_tokens || 0)) / 1000).toFixed(1)}k tok${u.estimated_usd != null ? " · ≈$" + u.estimated_usd.toFixed(3) : ""}</dd><dt>1回の目安</dt><dd>約 8〜9k トークン</dd>`; });
      open("mdCloud");
      $("btnCloudGo").onclick = () => { if ($("cloudDontAsk").checked) state.cloudOk = true; close("mdCloud"); res(true); };
      $("mdCloud").querySelectorAll("[data-close]").forEach(b => b.onclick = () => { close("mdCloud"); res(false); });
    });
  }

  // ---------- ブリーフ → プロンプト ----------
  const fields = () => state.mode === "A" ? { text: $("aText").value } : state.mode === "C" ? { raw: $("cRaw").value } : { place: $("bPlace").value, motion: $("bMotion").value, framing: $("bFraming").value, camera: $("bCamera").value, text: $("bText").value, dialogue: $("bDialogue").value };
  // **seed は `parseInt(v) || null` で読んではいけない。** 0 が falsy で潰れて既定の 1 になり、
  // **ComfyUI の既定値である 0 が指定できなくなる**（2026-08-26 に発覚）。
  // 空欄は null（サーバー側で 1）、それ以外は数値として渡し、負の数や範囲外はサーバーが 400 で止める。
  const SEED_MAX = Number.MAX_SAFE_INTEGER;            // 2**53-1。ComfyUI の上限は 2**64-1 だが、
                                                       // ここを超えると Number が黙って値を化けさせる
  function readSeed() {
    const raw = String($("seed").value || "").trim();
    if (!raw) return null;                             // 空欄 → サーバーが 1 にする
    if (/^(random|rand|ランダム)$/i.test(raw)) return "random";
    const n = Number(raw);
    if (!Number.isInteger(n)) { toast("seed は整数で入れてください", "warn"); return null; }
    if (n < 0) { toast("seed に負の数は使えません。毎回変えたいなら「ランダム」を押してください", "warn"); return null; }
    if (n > SEED_MAX) { toast("seed が大きすぎます（上限 " + SEED_MAX + "）", "warn"); return null; }
    return n;
  }
  $("btnSeedRand").onclick = () => {
    // 具体的な数字を入れる。**ComfyUI に -1 を渡すのではない**（noise_seed は min=0 で、-1 は 400 になる）。
    // 数字にしておけば結果カードとジョブ記録に残り、良かった回をあとで再現できる
    const n = Math.floor(Math.random() * (SEED_MAX + 1));
    $("seed").value = n;
    toast("seed を " + n + " にしました");
  };

  const genBody = (extra = {}) => Object.assign({ project: state.projName, mode: state.mode, fields: fields(), images: [...state.images], videos: [...state.videos], duration: parseInt($("duration").value), ratio: $("ratio").value, seed: readSeed(), tries: 3, confirm_cloud: state.cloud && state.cloudOk }, extra);

  $("btnExpand").onclick = async () => {
    if (!$("aText").value.trim()) return toast("1行を入れてください", "warn");
    if (!(await confirmCloud("モードAの展開に LLM を1回使います"))) return;
    toast("展開中…");
    try {
      const j = await api("/api/brief/expand", { method: "POST", body: JSON.stringify({ text: $("aText").value, confirm_cloud: true }) });
      $("bPlace").value = j.fields.place || ""; $("bMotion").value = j.fields.motion || ""; $("bFraming").value = j.fields.framing || ""; $("bCamera").value = j.fields.camera || ""; $("bText").value = j.fields.text || ""; $("bDialogue").value = (j.fields.dialogue || "").replace(/^なし$/, ""); syncChips("framChips", "bFraming"); syncChips("camChips", "bCamera"); framingCheck(); textCheck();
      document.querySelector('.tab[data-mode="B"]').click(); toast("4欄に展開しました。確認して生成へ");
    } catch (e) { toast("展開に失敗: " + e.message, "bad"); }
  };

  async function generate(seedOverride) {
    if (!state.projName) return toast("作品を選んでください", "warn");
    if (state.mode === "B" && !$("bPlace").value.trim() && !$("bMotion").value.trim()) return toast("場所と動きのどちらかは書いてください", "warn");
    if (!(await confirmCloud("プロンプト生成に LLM を使います（自動修復で最大3回）"))) return;
    resetSteps(); $("log").textContent = "待機中"; startTimer(); $("btnGen").disabled = true;
    setStep("llm", "now"); log(state.cloud ? "クラウド LLM へ接続" : "LM Studio を確認…"); await loadGpu();
    try {
      const body = genBody({ confirm_cloud: true }); if (seedOverride != null) body.seed = seedOverride;
      setStep("llm", "done"); setStep("gen", "now"); log("生成中 … 自動修復つき（最大3回）");
      const j = await api("/api/prompt/generate", { method: "POST", body: JSON.stringify(body) });
      state.lastGen = j; state.prompt = j.prompt; state.brief = j.brief; state.h3mode = j.h3_mode; state.lint = j.lint;
      if (j.load && j.load.seconds) log("LM Studio ロード " + j.load.seconds + "秒" + (j.load.effective_context ? "（ctx " + j.load.effective_context + "）" : ""), "ok");
      if (j.load && j.load.warning) { log("⚠ " + j.load.warning, "warn"); state.pinnedWarn = j.load; showPinnedFix(); }
      j.attempts.forEach(a => log(`試行${a.n}  ${a.seconds}s  ERROR ${a.errors.length} / WARN ${a.warns.length}${a.errors.length ? "  [" + a.errors.join(",") + "]" : ""}`, a.errors.length ? "warn" : "ok"));
      setStep("gen", "done"); setStep("lint", j.ok ? "done" : "bad");
      $("promptBox").value = j.prompt; $("briefBox").textContent = j.brief; renderLint(j.lint); if (j.framing_check) showFramingCheck(j.framing_check); if (j.text_check) showTextCheck(j.text_check);
      log((j.ok ? "合格" : "不合格のまま（最良の試行を表示）") + " · " + j.frames + "f / " + j.actual_seconds + "s", j.ok ? "ok" : "bad");
      if (j.usage) await loadConfig();
      toast(j.ok ? "プロンプトができました" : "ERROR が残っています。内容を確認してください", j.ok ? "" : "warn");
    } catch (e) { setStep("gen", "bad"); log("失敗: " + e.message, "bad"); toast("生成に失敗: " + e.message, "bad"); }
    finally { stopTimer(); $("btnGen").disabled = false; }
  }
  $("btnGen").onclick = () => generate();
  $("btnRetry").onclick = () => { const c = readSeed(); const s = (typeof c === "number" ? c : 0) + 1; $("seed").value = s; generate(s); };

  function renderLint(l) {
    const b = $("lintBadges"); const list = $("lintList");
    if (!l) { b.innerHTML = '<span class="badge">未生成</span>'; list.style.display = "none"; return; }
    b.innerHTML = `<span class="badge ${l.errors.length ? "bad" : "ok"}">ERROR ${l.errors.length}</span><span class="badge ${l.warns.length ? "warn" : ""}">WARN ${l.warns.length}</span>${l.words ? `<span class="badge">${l.words}語</span>` : ""}`;
    const rows = [...l.errors.map(e => `<div class="e"><code>ERROR ${esc(e.code)}</code>${esc(e.msg)}</div>`), ...l.warns.map(w => `<div class="w"><code>WARN ${esc(w.code)}</code>${esc(w.msg)}</div>`)];
    list.innerHTML = rows.join(""); list.style.display = rows.length ? "" : "none";
  }
  $("btnLint").onclick = async () => { const p = $("promptBox").value; if (!p.trim()) return toast("プロンプトが空です", "warn"); const j = await api("/api/prompt/lint", { method: "POST", body: JSON.stringify({ prompt: p, mode: state.h3mode, duration: parseInt($("duration").value) }) }); state.lint = j; state.prompt = p; renderLint(j); toast(j.ok ? "合格" : "ERROR あり", j.ok ? "" : "warn"); };
  $("btnCopy").onclick = async () => { const p = $("promptBox").value; if (!p.trim()) return toast("プロンプトが空です", "warn"); try { await navigator.clipboard.writeText(p); toast("コピーしました"); } catch (_) { $("promptBox").select(); document.execCommand("copy"); toast("コピーしました"); } };
  $("btnWrite").onclick = async () => {
    const p = $("promptBox").value; if (!p.trim()) return toast("プロンプトが空です", "warn");
    const d = new Date(); const date = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    const name = `${date}_${state.projName}_${$("shotId").textContent}_${state.h3mode}_${framesFor(parseInt($("duration").value))}f_${$("ratio").value.replace(":", "-")}`;
    const j = await api("/api/prompt/write", { method: "POST", body: JSON.stringify({ prompt: p, archive_name: name }) });
    $("foot").innerHTML = j.written.map(w => `<span>書き出し <code>${esc(w)}</code></span>`).join("");
    // project.shots に追記
    const shot = { id: $("shotId").textContent, archive_name: name, brief: state.mode === "B" ? fields() : { mode: state.mode, ...fields() }, mode: state.h3mode, duration: parseInt($("duration").value), ratio: $("ratio").value, seed: readSeed(), images: [...state.images], videos: [...state.videos], lint: state.lint ? { errors: state.lint.errors.length, warns: state.lint.warns.length, words: state.lint.words } : null };
    state.project.shots = state.project.shots || []; state.project.shots.push(shot);
    await api("/api/projects/" + encodeURIComponent(state.projName), { method: "PUT", body: JSON.stringify(state.project) });
    $("shotId").textContent = nextShotId(state.project);
    toast("書き出しました: プロンプト.txt / アーカイブ");
  };

  // ---------- 履歴 ----------
  async function loadHistory() {
    const q = $("histQ").value.trim(); const thisOnly = $("histThisOnly").checked;
    const j = await api("/api/history?project_name=" + encodeURIComponent(state.projName || "") + (q ? "&q=" + encodeURIComponent(q) : ""));
    const list = $("histList"); list.innerHTML = "";
    const items = j.items.filter(it => !thisOnly || !it.other_project);
    if (!items.length) list.innerHTML = '<div class="empty">見つかりません</div>';
    items.forEach(it => {
      const br = it.brief ? `<div class="br">${it.brief.place ? "<b>場所</b> " + esc(it.brief.place) + "<br>" : ""}${it.brief.motion ? "<b>動き</b> " + esc(it.brief.motion) + "<br>" : ""}${it.brief.framing ? "<b>開始</b> " + esc(it.brief.framing) + "<br>" : ""}${it.brief.camera ? "<b>カメラ</b> " + esc(it.brief.camera) + "<br>" : ""}${it.brief.text ? "<b>文字</b> " + esc(it.brief.text) : ""}</div>` : `<div class="na">ブリーフは保存されていない（アプリ以前のアーカイブ）— プロンプトだけ呼び出せます</div>`;
      const row = document.createElement("div"); row.className = "drow";
      row.innerHTML = `<div><div class="id">${esc(it.shot)} <small>${esc(it.date)} · ${esc(it.work)} · ${esc(it.mode)} · ${it.frames || ""}f${it.other_project ? ' · <span style="color:var(--warn)">別作品</span>' : ""}</small></div>${br}</div><div class="ops"><button class="btn sec sm" data-a="brief" ${it.brief ? "" : "disabled"}>ブリーフへ</button><button class="btn sec sm" data-a="prompt">プロンプトへ</button><button class="btn sm" data-a="both" ${it.brief ? "" : "disabled"}>両方</button></div>`;
      row.querySelectorAll("button").forEach(b => b.onclick = async () => {
        const a = b.dataset.a;
        if (a === "brief" || a === "both") { const f = it.brief; $("bPlace").value = f.place || ""; $("bMotion").value = f.motion || ""; $("bFraming").value = f.framing || ""; $("bCamera").value = f.camera || ""; $("bText").value = f.text || ""; $("bDialogue").value = f.dialogue || ""; syncChips("framChips", "bFraming"); syncChips("camChips", "bCamera"); framingCheck(); textCheck(); document.querySelector('.tab[data-mode="B"]').click(); }
        if (a === "prompt" || a === "both") { const d = await api("/api/history/" + encodeURIComponent(it.archive_name)); $("promptBox").value = d.prompt; state.prompt = d.prompt; state.h3mode = it.mode || state.h3mode; renderLint(null); $("briefBox").textContent = "（履歴から呼び出し: " + it.archive_name + "）"; }
        close("mdHistory"); toast("呼び出しました: " + it.shot);
      });
      list.appendChild(row);
    });
  }
  $("btnHistory").onclick = async () => { open("mdHistory"); await loadHistory(); };
  $("histSearch").onclick = loadHistory; $("histThisOnly").onchange = loadHistory; $("histQ").onkeydown = e => { if (e.key === "Enter") loadHistory(); };

  // ---------- 段4: 生成投入・進捗・結果 ----------
  state.job = null; state.jobData = null; state.resultJob = null; state.lastGenParams = null;
  let jobTimer = null, logShown = 0;
  const fmtMin = (s) => s == null ? "—" : (s >= 60 ? Math.floor(s / 60) + "分" + String(Math.round(s % 60)).padStart(2, "0") + "秒" : Math.round(s) + "秒");
  const setBusy = (on) => { ["btnPreview", "btnFinal", "btnGen", "btnAgain", "btnAgainSeed", "btnToFinal"].forEach(id => { if ($(id)) $(id).disabled = on; }); $("btnCancel").style.display = on ? "" : "none"; };

  async function startGeneration(mode, extra = {}) {
    // extra に prompt/images/… があれば（「もう一度」「本番へ」）それを使う。無ければ画面の値
    const p = extra.prompt != null ? extra.prompt : $("promptBox").value; if (!p.trim()) return toast("プロンプトが空です。先に生成するか、履歴から呼び出してください", "warn");
    if (!state.projName && !extra.project) return toast("作品を選んでください", "warn");
    if (extra.prompt == null && state.lint && state.lint.errors && state.lint.errors.length && !extra.force) { if (!confirm("リンターの ERROR が " + state.lint.errors.length + " 件残っています。このまま生成しますか？")) return; }
    const body = Object.assign({ project: state.projName, prompt: p, mode, seed: readSeed(), duration: parseInt($("duration").value), ratio: $("ratio").value,
      images: [...state.images], videos: [...state.videos], shot_id: $("shotId").textContent, h3_mode: state.h3mode, brief: state.mode === "B" ? fields() : { mode: state.mode, ...fields() } }, extra);
    delete body.force;
    resetSteps(); $("log").textContent = "待機中"; logShown = 0; $("progStep").textContent = ""; startTimer(); setBusy(true);
    setStep("gpu", "now"); log(mode === "preview" ? "プレビュー生成を投入（608×352）" : "本番生成を投入（1344×768）");
    try {
      const r = await api("/api/generate", { method: "POST", body: JSON.stringify(body) });
      state.job = r.job_id; state.lastGenParams = body;
      log("ジョブ " + r.job_id + " · " + r.params.width + "×" + r.params.height + " · " + r.params.length + "f · seed " + r.params.seed + " · 目安 " + r.eta_minutes + "分", "ok");
      $("resSub").textContent = (mode === "preview" ? "プレビュー" : "本番") + " 生成中…";
      pollJob();
    } catch (e) {
      stopTimer(); setBusy(false); setStep("gpu", "bad");
      if (e.need_confirm) {
        log("確認待ち: " + e.reason, "warn");
        if (confirm(e.reason + "\n（" + ((e.raw || []).join(", ")) + "）\n\nそれでも本番を投入しますか？")) return startGeneration(mode, Object.assign({}, extra, { allow_raw: true }));
        return;
      }
      log("投入できない: " + e.message, "bad"); toast("投入できない: " + e.message, "bad");
    }
  }
  const _api = api;
  async function pollJob() {
    clearInterval(jobTimer);
    const tick = async () => {
      if (!state.job) return;
      let j; try { j = await _api("/api/jobs/" + state.job); } catch (e) { return; }
      state.jobData = j;
      (j.log || []).slice(logShown).forEach(l => { if (!/^step \d+\/\d+$/.test(l.msg)) log("[" + String(Math.floor(l.t / 60)).padStart(2, "0") + ":" + String(Math.floor(l.t % 60)).padStart(2, "0") + "] " + l.msg, l.kind || ""); });
      logShown = (j.log || []).length;
      const pr = j.progress || {};
      if (pr.max) $("progStep").textContent = "step " + pr.value + "/" + pr.max + (pr.node ? " · " + pr.node : ""); else if (pr.node) $("progStep").textContent = pr.node;
      if (["submitted", "running", "inspecting"].includes(j.state)) { setStep("gpu", "done"); setStep("comfy", "now"); }
      if (j.state === "done") { clearInterval(jobTimer); stopTimer(); setBusy(false); setStep("comfy", "done"); $("progStep").textContent = "完了"; renderResult(j); toast("生成が終わりました（" + fmtMin(j.result && j.result.gen_seconds) + "）"); loadGpu(); }
      else if (j.state === "error") { clearInterval(jobTimer); stopTimer(); setBusy(false); setStep("comfy", "bad"); $("progStep").textContent = "失敗"; toast("生成に失敗: " + (j.error || ""), "bad"); $("resSub").textContent = "失敗: " + (j.error || "").slice(0, 80); loadGpu(); }
      else if (j.state === "cancelled") { clearInterval(jobTimer); stopTimer(); setBusy(false); setStep("comfy", "bad"); $("progStep").textContent = "中止"; toast("中止しました", "warn"); $("resSub").textContent = "中止"; loadGpu(); }
    };
    await tick(); jobTimer = setInterval(tick, 2000);
  }
  $("btnCancel").onclick = async () => { if (!state.job) return; if (!confirm("生成を中止しますか？")) return; try { await api("/api/jobs/" + state.job + "/cancel", { method: "POST" }); log("中止を要求", "warn"); } catch (e) { toast("中止できない: " + e.message, "bad"); } };
  $("btnPreview").onclick = () => startGeneration("preview");
  $("btnFinal").onclick = () => startGeneration("final");

  function renderResult(j) {
    state.resultJob = j;
    const p = j.params || {}; const res = j.result || {}; const ins = res.inspect || {}; const vid = res.video || {};
    $("resEmpty").style.display = "none"; $("resBody").style.display = "";
    $("resSub").textContent = (p.mode === "final" ? "本番" : "プレビュー") + " · " + (p.shot_id || "") + " · " + (p.project || "") + " · " + j.id + (j.cached ? " · ⚠ 同一入力のためキャッシュ（再生成されていない）" : "");
    if (ins.contact) { $("resContact").src = "/api/jobs/" + j.id + "/contact?t=" + Date.now(); $("resContact").style.display = ""; $("resContactCap").textContent = "フレーム " + (ins.contact_frames || []).join(" / "); } else { $("resContact").style.display = "none"; $("resContactCap").textContent = ins.contact_error ? "コンタクトシート失敗: " + ins.contact_error : ""; }
    if (vid.rel) { $("resVideo").src = "/api/video?rel=" + encodeURIComponent(vid.rel); $("resVideoCap").innerHTML = "<code>" + esc(vid.rel) + "</code>"; }
    const au = ins.audio; const fdm = ins.frame_diff;
    const rows = [
      ["サイズ", (p.width || "?") + "×" + (p.height || "?")], ["フレーム", ins.frames != null ? ins.frames + "f" + (p.length && ins.frames !== p.length ? " (指定 " + p.length + ")" : "") : "—"],
      ["実尺", ins.duration != null ? ins.duration + "s" : "—"], ["bit_rate", ins.mbps != null ? ins.mbps + " Mbps" : "—", ins.mbps != null && ins.mbps < 5 ? "warn" : ""],
      ["音量", au ? (au.mean_db + " dB mean / " + au.max_db + " dB max") : "音声なし", au ? "" : "warn"], ["フレーム間差分", fdm != null ? fdm + (ins.frame_diff_detail ? " (f" + ins.frame_diff_detail.range.join("–") + ")" : "") : "—"],
      ["seed", p.seed], ["生成時間", fmtMin(res.gen_seconds)], ["参照", (p.images || []).length + "枚 / " + (p.videos || []).length + "本"],
    ];
    $("resMetrics").innerHTML = rows.map(r => `<dt>${esc(r[0])}</dt><dd class="${r[2] || ""}">${esc(r[1])}</dd>`).join("");
    const notes = ins.notes || []; $("resNotes").style.display = notes.length ? "" : "none"; $("resNotes").innerHTML = notes.map(n => "⚠ " + esc(n)).join("<br>");
    $("btnToFinal").style.display = p.mode === "final" ? "none" : "";
    $("resultCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  // ジョブの設定（プロンプト・素材・尺・比率・seed）をそのまま使って投げ直す
  const fromJob = (p, over = {}) => Object.assign({ project: p.project, prompt: p.prompt, images: p.images || [], videos: p.videos || [], duration: p.duration, ratio: p.ratio, seed: p.seed, shot_id: p.shot_id, h3_mode: p.h3_mode, brief: p.brief }, over);
  $("btnAgainSeed").onclick = () => { const p = state.resultJob && state.resultJob.params; if (!p) return; const s = (p.seed || 0) + 1; $("seed").value = s; startGeneration(p.mode, fromJob(p, { seed: s })); };
  $("btnToFinal").onclick = () => { const p = state.resultJob && state.resultJob.params; if (!p) return; $("seed").value = p.seed; startGeneration("final", fromJob(p)); };
  $("btnAdopt").onclick = async () => {
    const j = state.resultJob; if (!j) return; const p = j.params || {};
    const prompt = (p.prompt && p.prompt.trim()) ? p.prompt : $("promptBox").value; if (!prompt.trim()) return toast("プロンプトが空です", "warn");
    const d = new Date(); const date = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    const shotId = p.shot_id || $("shotId").textContent; const fr = p.length || framesFor(parseInt($("duration").value));
    const name = `${date}_${p.project || state.projName}_${shotId}_${p.h3_mode || state.h3mode}_${fr}f_${(p.ratio || $("ratio").value).replace(":", "-")}`;
    $("btnAdopt").disabled = true;
    try {
      const r = await api("/api/shots/archive", { method: "POST", body: JSON.stringify({ project: p.project || state.projName, shot_id: shotId, prompt, archive_name: name, brief: p.brief || (state.mode === "B" ? fields() : { mode: state.mode, ...fields() }), mode: p.h3_mode || state.h3mode, duration: p.duration, ratio: p.ratio, seed: p.seed, images: p.images, videos_ref: p.videos, lint: state.lint ? { errors: state.lint.errors.length, warns: state.lint.warns.length, words: state.lint.words } : null, job_id: j.id }) });
      $("foot").innerHTML = r.written.map(w => `<span>書き出し <code>${esc(w)}</code></span>`).join("") + `<span>作品に記録 <code>${esc(r.shot.id)}</code> → <code>${esc((r.shot.videos || []).join(", "))}</code></span>`;
      if ((p.project || state.projName) === state.projName) { state.project = await api("/api/projects/" + encodeURIComponent(state.projName)); $("shotId").textContent = r.next_shot_id; }
      toast("採用しました: " + r.shot.id + "（" + (p.project || state.projName) + "）");
    } catch (e) { toast("採用に失敗: " + e.message, "bad"); }
    finally { $("btnAdopt").disabled = false; }
  };
  async function loadJobs() {
    const j = await api("/api/jobs"); const list = $("jobList"); list.innerHTML = "";
    if (!j.jobs.length) list.innerHTML = '<div class="empty">まだありません</div>';
    j.jobs.forEach(jb => {
      const p = jb.params || {}; const ins = (jb.result && jb.result.inspect) || {};
      const row = document.createElement("div"); row.className = "jrow";
      row.innerHTML = `<div><div class="id">${esc(jb.id)}<small>${esc(p.project || "")} · ${esc(p.shot_id || "")} · ${p.mode === "final" ? "本番" : "プレビュー"} · ${p.width || ""}×${p.height || ""} · ${p.length || ""}f · seed ${p.seed ?? ""}</small></div><div class="hint" style="margin:2px 0 0">${jb.state === "done" ? esc((ins.mbps != null ? ins.mbps + " Mbps · " : "") + (ins.audio ? ins.audio.mean_db + " dB · " : "") + fmtMin(jb.result && jb.result.gen_seconds)) : esc(jb.error || "")}</div></div><span class="st ${esc(jb.state)}">${esc(jb.state)}</span>`;
      row.onclick = async () => { if (jb.state === "done") { const full = await api("/api/jobs/" + jb.id); renderResult(full); close("mdJobs"); } else if (["running", "submitted", "inspecting", "unloading", "queued"].includes(jb.state)) { state.job = jb.id; logShown = 0; $("log").textContent = "待機中"; startTimer(); setBusy(true); pollJob(); close("mdJobs"); } else toast("この状態のジョブには結果がありません", "warn"); };
      list.appendChild(row);
    });
  }
  $("btnJobs").onclick = async () => { open("mdJobs"); await loadJobs(); };
  // 起動時に実行中のジョブがあれば追従する
  async function resumeActiveJob() { try { const j = await api("/api/jobs"); if (j.active) { state.job = j.active; logShown = 0; startTimer(); setBusy(true); log("実行中のジョブに再接続: " + j.active); pollJob(); } } catch (_) {} }

  // ---------- 段6b: 切り抜き（SAM3） ----------
  state.cut = { session: null, dets: [], sel: new Set(), image: null, source: "raw" };

  async function openCut() {
    open("mdCut");
    $("cutDets").innerHTML = ""; $("cutOrderNote").style.display = "none";
    $("cutPreview").style.display = "none"; $("cutPrevEmpty").style.display = "";
    $("cutMetrics").innerHTML = ""; $("cutNotes").style.display = "none"; $("cutCheckInfo").textContent = "";
    state.cut = { session: null, dets: [], sel: new Set(), image: null, source: document.querySelector('input[name=cutSrc]:checked').value };
    $("cutPicked").textContent = ""; $("cutName").value = "";
    const env = await api("/api/cut/available");
    $("cutEnv").className = "badge " + (env.reason ? "bad" : "ok");
    $("cutEnv").textContent = env.reason ? env.reason : ("SAM3 " + (env.checkpoint || "").split(/[\\/]/).pop());
    $("btnCutDetect").disabled = !!env.reason;
    await loadCutSources();
  }
  async function loadCutSources(pin) {
    const src = document.querySelector('input[name=cutSrc]:checked').value;
    state.cut.source = src;
    const box = $("cutSrcList"); box.innerHTML = '<div class="empty">読み込み中…</div>';
    // 落とし先は「いま見ているソース」。元イラスト側なら raw_dir、input 側なら input に置く
    enableDrop(box, {
      dest: () => document.querySelector('input[name=cutSrc]:checked').value,
      exts: IMG_DROP,
      onDone: async names => { await loadCutSources(names[0]); toast(names.length + " 枚を取り込みました"); }
    });
    const j = await api(src === "raw" ? "/api/assets/raw" : "/api/assets/images?cut_only=false");
    let items = j.items || [];
    if (src === "input") items = items.filter(i => !i.cut);           // 切り抜き済みは元画像にしない
    box.innerHTML = "";
    if (!items.length) {
      // 空でもドロップは受けたいので return しない（ここで return すると落とす場所が無くなる）
      box.innerHTML = `<div class="empty">${src === "raw" ? "元イラストのフォルダ（config.raw_dir）が空です" : "input に未切り抜きの画像がありません"}<br><small>エクスプローラーから画像をここに落とせます</small></div>`;
      return;
    }
    // 落としたばかりのものは上限で切られないよう先頭に固定する（既存ファイルだと更新日時が古く、一覧に出ないことがある）
    if (pin) items = items.filter(x => x.name === pin).concat(items.filter(x => x.name !== pin));
    items.slice(0, 40).forEach(it => {
      const b = document.createElement("button"); b.className = "ref"; b.title = it.name;
      b.innerHTML = `<span class="chk">✓</span><img loading="lazy" src="/api/file/${src === "raw" ? "raw" : "input"}/${encodeURIComponent(it.name)}" alt=""><span>${esc(it.name.replace(/\.[a-z]+$/i, ""))}</span>`;
      b.onclick = () => {
        box.querySelectorAll(".ref").forEach(x => x.classList.remove("on")); b.classList.add("on");
        state.cut.image = it.name; $("cutPicked").textContent = it.name;
        $("cutName").value = suggestCutName(it.name);
      };
      box.appendChild(b);
      if (it.name === pin) b.onclick();      // 落としたものはそのまま選んでおく（続けて「検出する」に進める）
    });
  }
  const suggestCutName = (src) => {
    const shot = $("shotId").textContent || "S01";
    const m = /(front|back|side|profile|turn\d*)/i.exec(src);
    return `h3ref_${shot}_${m ? m[1].toLowerCase() : "front"}_cut.png`;
  };
  document.querySelectorAll('input[name=cutSrc]').forEach(r => r.onchange = () => { state.cut.image = null; $("cutPicked").textContent = ""; loadCutSources(); });

  $("btnCutSweep").onclick = async () => {
    if (!state.cut.image) return toast("元画像を選んでください", "warn");
    const btn = $("btnCutSweep"); btn.disabled = true;
    $("cutSweep").style.display = ""; $("cutSweep").innerHTML = '<div class="hint" style="margin:0">threshold を振っています…（6点・約10秒）</div>';
    try {
      const j = await api("/api/cut/sweep", { method: "POST", body: JSON.stringify({ image: state.cut.image, source: state.cut.source, text: $("cutText").value }) });
      $("cutSweep").innerHTML = j.rows.map(r => `<button class="sw${r.n ? "" : " zero"}" data-th="${r.threshold}"><b>${r.n == null ? "0" : r.n}</b>th ${r.threshold}</button>`).join("")
        + `<div class="hint" style="margin:0;align-self:center">検出数。<b>欲しい人数になる threshold</b> を押すとその値で検出します（${j.seconds}秒）</div>`;
      $("cutSweep").querySelectorAll(".sw").forEach(b => b.onclick = () => {
        $("cutTh").value = b.dataset.th;
        $("cutSweep").querySelectorAll(".sw").forEach(x => x.classList.remove("on")); b.classList.add("on");
        $("btnCutDetect").click();
      });
    } catch (e) { $("cutSweep").innerHTML = `<div class="hint" style="margin:0;color:var(--bad)">スイープに失敗: ${esc(e.message)}</div>`; }
    finally { btn.disabled = false; }
  };

  $("btnCutDetect").onclick = async () => {
    if (!state.cut.image) return toast("元画像を選んでください", "warn");
    const btn = $("btnCutDetect"); btn.disabled = true; $("cutDets").innerHTML = '<div class="empty">SAM3 で検出中…</div>';
    try {
      const d = await api("/api/cut/detect", { method: "POST", body: JSON.stringify({ image: state.cut.image, source: state.cut.source, text: $("cutText").value, threshold: parseFloat($("cutTh").value) || 0.5, refine: 1 }) });
      state.cut.session = d.session; state.cut.dets = d.detections; state.cut.sel = new Set();
      renderCutDets(d);
      if (d.freed) log("切り抜きのため " + d.freed, "warn");
      toast(d.detections.length + " 人を検出しました（" + d.seconds + "秒）");
    } catch (e) { $("cutDets").innerHTML = ""; toast("検出に失敗: " + e.message, "bad"); }
    finally { btn.disabled = false; }
  };
  function renderCutDets(d) {
    const box = $("cutDets"); box.innerHTML = "";
    if (!d.detections.length) { box.innerHTML = '<div class="empty">検出ゼロ。threshold を下げるか検出テキストを見直してください</div>'; return; }
    $("cutOrderNote").style.display = "";
    d.detections.forEach(r => {
      const b = document.createElement("button"); b.className = "cutdet"; b.dataset.i = r.index;
      b.innerHTML = `<span class="n">#${r.index}</span><span class="chk">✓</span><img loading="lazy" src="/api/cut/file/${encodeURIComponent(d.session)}/${encodeURIComponent(r.thumb)}" alt="">`
        + `<div class="meta">左から<b>${r.from_left}番目</b> · 髪<b>${esc(r.hair)}</b><br>面積 ${r.area_pct}% · score ${r.score}</div>`;
      b.onclick = () => { if (state.cut.sel.has(r.index)) state.cut.sel.delete(r.index); else state.cut.sel.add(r.index); b.classList.toggle("on"); cutPreview(); };
      box.appendChild(b);
    });
  }
  async function cutPreview() {
    if (!state.cut.session || !state.cut.sel.size) { $("cutPreview").style.display = "none"; $("cutPrevEmpty").style.display = ""; $("cutMetrics").innerHTML = ""; $("cutNotes").style.display = "none"; $("cutCheckInfo").textContent = ""; return; }
    try {
      const p = await api("/api/cut/preview", { method: "POST", body: JSON.stringify({ session: state.cut.session, select: [...state.cut.sel], crop: $("cutCrop").checked, crop_margin: parseInt($("cutMargin").value) || 40 }) });
      $("cutPreview").src = `/api/cut/file/${encodeURIComponent(state.cut.session)}/preview.png?t=` + Date.now();
      $("cutPreview").style.display = ""; $("cutPrevEmpty").style.display = "none";
      renderCutCheck(p);
    } catch (e) { toast("プレビューに失敗: " + e.message, "bad"); }
  }
  function renderCutCheck(p) {
    const c = p.check || {};
    const rows = [["残す人", p.selected.join(", ") + "（" + p.selected.length + "人）"], ["サイズ", p.size.join("×") + (p.cropped ? "（クロップ）" : "")],
      ["被写体の占有", p.subject_pct + "%", p.subject_pct < 12 ? "warn" : ""], ["検査", `ERROR ${(c.errors || []).length} / WARN ${(c.warns || []).length}`, (c.errors || []).length ? "bad" : ""]];
    $("cutMetrics").innerHTML = rows.map(r => `<dt>${esc(r[0])}</dt><dd class="${r[2] || ""}">${esc(r[1])}</dd>`).join("");
    const msgs = [...(c.errors || []).map(m => "⚠ " + m), ...(c.warns || []).map(m => "・" + m)];
    $("cutNotes").style.display = msgs.length ? "" : "none"; $("cutNotes").innerHTML = msgs.map(esc).join("<br>");
    $("cutCheckInfo").innerHTML = (c.info || []).map(esc).join("<br>");
  }
  $("btnCutPreview").onclick = cutPreview;
  $("cutCrop").onchange = cutPreview; $("cutMargin").onchange = cutPreview;
  $("btnCutSave").onclick = async (ev) => {
    if (!state.cut.session || !state.cut.sel.size) return toast("残す人を選んでください", "warn");
    const name = $("cutName").value.trim(); if (!name) return toast("保存名を入れてください", "warn");
    const body = { session: state.cut.session, select: [...state.cut.sel], crop: $("cutCrop").checked, crop_margin: parseInt($("cutMargin").value) || 40, save_as: name };
    const go = async (overwrite) => {
      const r = await api("/api/cut/save", { method: "POST", body: JSON.stringify(Object.assign({}, body, { overwrite })) });
      renderCutCheck(r);
      const c = r.check || {};
      if ((c.errors || []).length) toast("保存しましたが検査に ERROR があります: " + c.errors[0], "warn");
      else toast("保存しました: " + r.saved);
      $("cutOnly").checked = true; state.images.add(r.saved); await loadAssets(); close("mdCut");
      $("foot").innerHTML = `<span>切り抜き <code>${esc(r.path)}</code>（${r.selected.length}人 · ${r.size.join("×")} · 検査 ERROR ${(c.errors || []).length}/WARN ${(c.warns || []).length}）</span>`;
    };
    ev.target.disabled = true;
    try { await go(false); }
    catch (e) {
      if (e.need_confirm) { if (confirm(e.reason)) { try { await go(true); } catch (e2) { toast("保存に失敗: " + e2.message, "bad"); } } }
      else toast("保存に失敗: " + e.message, "bad");
    }
    finally { ev.target.disabled = false; }
  };

  // ---------- 起動 ----------
  (async () => {
    // 使える機能を先に聞く。python-multipart が無い環境ではドロップ取り込みだけが落ちる（起動はする）
    try { state.features = await api("/api/features"); } catch (_) { state.features = { upload: true }; }
    try { await loadConfig(); } catch (e) { toast("設定の読み込みに失敗: " + e.message, "bad"); }
    await loadCamera();
    await loadProjects();
    await loadGpu(); setInterval(loadGpu, 20000);
    // モードBの「?」ヘルプは初期表示、セリフだけ畳む（index.html 側）
    updateFrames();
    await loadEagle(false);
    await resumeActiveJob();
  })();
})();
