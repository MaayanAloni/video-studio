'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const api = async (path, opt = {}) => {
  if (opt.body && !(opt.body instanceof FormData)) { opt.body = JSON.stringify(opt.body); opt.headers = { 'Content-Type': 'application/json' }; }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || j.detail || r.statusText);
  return j;
};
const fmtT = s => s == null ? '' : s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;

const S = { refs: [], exRefs: [], compute: 'local', mode: 't2v', quality: 'standard', images: {}, status: null, jobs: [], videos: [], cur: null, sel: null, loop: false, seenDone: new Set(), firstPoll: true };

/* ------------------------------------------------------------ navigation */
function show(view) {
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
  $$('.navbtn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  if (view !== 'editor') $('#vid').pause();
  if (view === 'library') renderLibrary();
  if (view === 'logs') refreshLogs(true);
  if (view === 'settings') { loadSettings(); rpLoadCatalog(); rpPoll(); }
}
$$('.navbtn').forEach(b => b.onclick = () => show(b.dataset.view));

/* ------------------------------------------------------------ create form */
function setMode(m) {
  S.mode = m;
  $$('#modeTabs .tab').forEach(t => t.classList.toggle('active', t.dataset.mode === m));
  $('#framesRow').hidden = m !== 'i2v';
  $('#singlePrompt').hidden = m === 'story';
  $('#storyBox').hidden = m !== 'story';
  $('#seconds').disabled = m === 'story';
  if (m === 'story' && !$('#shots').children.length) { addShot(); addShot(); }
  syncDrops(); updateHints();
}
$$('#modeTabs .tab').forEach(t => t.onclick = () => setMode(t.dataset.mode));
$$('#computeTabs .tab').forEach(t => t.onclick = () => {
  S.compute = t.dataset.compute;
  $$('#computeTabs .tab').forEach(x => x.classList.toggle('active', x === t));
  updateHints();
});
$$('#qualityCards .card').forEach(c => c.onclick = () => { S.quality = c.dataset.q; $$('#qualityCards .card').forEach(x => x.classList.toggle('active', x === c)); updateHints(); });

function addShot(data = {}) {
  const d = document.createElement('div');
  d.className = 'shot';
  d.innerHTML = `<div class="hd"><b class="n"></b><input type="number" class="sec" min="1" value="${data.seconds || 5}"> s
    <select class="cam">${cameraOptions()}</select><span class="spacer"></span>
    <button class="up" title="Move up">↑</button><button class="dn" title="Move down">↓</button><button class="rm" title="Remove">✕</button></div>
    <textarea rows="2" placeholder="What happens in this shot?">${esc(data.prompt || '')}</textarea>`;
  $('.rm', d).onclick = () => { d.remove(); renumber(); };
  $('.up', d).onclick = () => { d.previousElementSibling && d.parentNode.insertBefore(d, d.previousElementSibling); renumber(); };
  $('.dn', d).onclick = () => { d.nextElementSibling && d.parentNode.insertBefore(d.nextElementSibling, d); renumber(); };
  $('.sec', d).oninput = updateHints;
  $('#shots').appendChild(d); renumber();
}
function renumber() { $$('#shots .shot').forEach((s, i) => $('.n', s).textContent = `Shot ${i + 1}`); updateHints(); }
$('#addShot').onclick = () => addShot();
const cameraOptions = () => (S.status?.cameras || ['']).map(c => `<option value="${c}">${c ? c.replace(/_/g, ' ') : 'auto / from prompt'}</option>`).join('');

function totalSeconds() {
  return S.mode === 'story' ? $$('#shots .sec').reduce((a, i) => a + (+i.value || 0), 0) : (+$('#seconds').value || 5);
}
function estimate(quality, seconds, resolution) {
  const p = S.status?.perf?.[quality], q = S.status?.quality?.[quality];
  if (!p || !q) return null;
  const px = { '360p': 640 * 368, '480p': 832 * 480, '580p': 1024 * 576, '720p': 1280 * 704 }[resolution] || 832 * 480;
  const perFrame = p.sec / Math.max(1, p.frames) * Math.pow(px / p.px, 1.3);
  return perFrame * seconds * q.fps;
}
function updateHints() {
  const s = totalSeconds(), n = Math.max(1, Math.ceil(s / 5));
  $('#durHint').textContent = (n > 1 ? `${s}s = ${n} chained passes of ≤5s, each continuing from the previous last frame. ` : '') +
    (S.images.end_image && S.quality === 'draft' ? 'End frame needs Standard or higher. ' : '');
  $$('#qualityCards .card').forEach(c => {
    const e = estimate(c.dataset.q, s, $('#resolution').value);
    $('i', c).textContent = e ? '≈ ' + fmtT(e) : '';
    if (['ultra', 'h3', 'h3_turbo'].includes(c.dataset.q)) {   // rented GPU only
      const rm = S.status?.remote || {}, rmodels = S.status?.remote_models || {};
      const isH3 = c.dataset.q.startsWith('h3');
      c.hidden = !rm.enabled || (isH3 && rmodels.h3 === false);
      c.classList.toggle('off', S.compute !== 'remote');
      return;
    }
    const hq = c.dataset.q !== 'draft';
    c.classList.toggle('off', S.status ? !(hq ? S.status.models.hq : S.status.models.draft) : false);
  });
  const c = +$('#cfg').value; $('#cfgVal').textContent = c > 1 ? c : '(auto)';
  const isH3 = S.quality.startsWith('h3');
  const rm = S.status?.remote || {}, rmod = S.status?.remote_models || {};
  const canRef = isH3 && S.compute === 'remote' && rmod.h3_refs !== false;
  $('#refsBox').hidden = !canRef;
  if (!canRef && S.refs.length) { S.refs = []; drawRefs(); }

  // say WHY the better tiers are missing, instead of just hiding them
  let why = '';
  if (!rm.enabled) {
    why = 'Ultra and MiniMax H3 (and reference images) need a rented GPU — add your RunPod API key in Settings.';
  } else if (S.compute !== 'remote') {
    why = 'Switch “Render on” to the rented GPU to use Ultra, H3 and reference images.';
  } else if (isH3 && rmod.h3 === false) {
    why = 'H3 is not installed on the rented GPU yet — Settings → rent/connect will install it.';
  } else if (isH3 && rmod.h3_refs === false) {
    why = 'Reference images need the H3 reference model, which is not on this GPU yet.';
  } else if (canRef) {
    why = 'Reference images work alongside start/end frames — you can use both together.';
  }
  $('#tierHint').textContent = why;
}
['seconds', 'resolution', 'cfg'].forEach(id => $('#' + id).addEventListener('input', updateHints));

/* image drops */
let dropTarget = null;
function syncDrops() {
  $$('.drop').forEach(d => {
    const im = S.images[d.dataset.key];
    d.classList.toggle('has', !!im);
    d.style.backgroundImage = im ? `url(${im.url})` : '';
    $('.x', d)?.remove();
    if (im) { const x = document.createElement('div'); x.className = 'x'; x.textContent = '✕'; x.onclick = e => { e.stopPropagation(); delete S.images[d.dataset.key]; syncDrops(); updateHints(); }; d.appendChild(x); }
  });
}
async function uploadTo(key, file) {
  const fd = new FormData(); fd.append('file', file);
  try { S.images[key] = await api('/api/upload', { method: 'POST', body: fd }); syncDrops(); updateHints(); }
  catch (e) { $('#createErr').textContent = 'Upload failed: ' + e.message; }
}
$$('.drop').forEach(d => {
  d.onclick = () => { dropTarget = d.dataset.key; $('#file').click(); };
  d.ondragover = e => { e.preventDefault(); d.classList.add('over'); };
  d.ondragleave = () => d.classList.remove('over');
  d.ondrop = e => { e.preventDefault(); d.classList.remove('over'); const f = e.dataTransfer.files[0]; if (f) uploadTo(d.dataset.key, f); };
});
$('#file').onchange = e => {
  const f = e.target.files[0];
  if (f) {
    if (dropTarget === '__ref__') uploadRef(f);
    else if (dropTarget === '__exref__') uploadExRef(f);
    else uploadTo(dropTarget, f);
  }
  e.target.value = '';
};
async function uploadRef(file) {
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await api('/api/upload', { method: 'POST', body: fd });
    if (S.refs.length < 9) S.refs.push(r);
    drawRefs();
  } catch (e) { $('#createErr').textContent = 'Upload failed: ' + e.message; }
}
function drawRefs() {
  $('#refStrip').innerHTML = S.refs.map((r, i) =>
    `<div class="ref" style="background-image:url(${r.url})"><span class="x" data-ref="${i}">✕</span><span class="n">${i + 1}</span></div>`).join('')
    || '<div class="hint">none yet — the clip will be generated from the prompt alone</div>';
  $('#addRef').disabled = S.refs.length >= 9;
}
$('#refStrip').addEventListener('click', e => {
  const x = e.target.closest('[data-ref]');
  if (x) { S.refs.splice(+x.dataset.ref, 1); drawRefs(); }
});
$('#addRef').onclick = () => { dropTarget = '__ref__'; $('#file').click(); };

/* extend-panel references (separate set from the Create page) */
async function uploadExRef(file) {
  const fd = new FormData(); fd.append('file', file);
  try { const r = await api('/api/upload', { method: 'POST', body: fd });
        if (S.exRefs.length < 9) S.exRefs.push(r); drawExRefs(); }
  catch (e) { $('#edErr').textContent = 'Upload failed: ' + e.message; }
}
function drawExRefs() {
  $('#exRefStrip').innerHTML = S.exRefs.map((r, i) =>
    `<div class="ref" style="background-image:url(${r.url})"><span class="x" data-exref="${i}">✕</span><span class="n">${i + 1}</span></div>`).join('')
    || '<div class="hint">none — the new part continues from the last frame alone</div>';
  $('#exAddRef').disabled = S.exRefs.length >= 9;
}
$('#exRefStrip').addEventListener('click', e => {
  const x = e.target.closest('[data-exref]');
  if (x) { S.exRefs.splice(+x.dataset.exref, 1); drawExRefs(); }
});
$('#exAddRef').onclick = () => { dropTarget = '__exref__'; $('#file').click(); };
function syncExtendPanel() {
  const q = $('#exQuality').value || '';
  const rm = S.status?.remote || {}, rmod = S.status?.remote_models || {};
  const isH3 = q.startsWith('h3');
  const canRef = isH3 && rm.enabled && rmod.h3_refs !== false;
  $('#exRefsBox').hidden = !canRef;
  if (!canRef && S.exRefs.length) { S.exRefs = []; drawExRefs(); }
  const src = S.cur && S.cur.quality;
  let hint = '';
  if (isH3 && !rm.enabled) hint = 'H3 needs the rented GPU — set one up in Settings.';
  else if (src && src.startsWith('h3') && !isH3) hint = 'This clip is H3 (24fps, with audio). Extending with a Wan tier re-times the new part and adds no sound.';
  else if (canRef) hint = 'References apply to the new footage only, on top of the last frame.';
  $('#exHint').textContent = hint;
}
$('#exQuality').onchange = syncExtendPanel;
document.addEventListener('paste', e => {
  if (!$('#view-create').classList.contains('active') || S.mode === 't2v') return;
  const f = [...e.clipboardData.files].find(f => f.type.startsWith('image/'));
  if (f) uploadTo(S.images.start_image ? 'end_image' : 'start_image', f);
});

$('#generate').onclick = async () => {
  $('#createErr').textContent = '';
  const p = {
    quality: S.quality, aspect: $('#aspect').value, resolution: $('#resolution').value, camera: $('#camera').value,
    negative: $('#negative').value.trim() || null, style: $('#style').value.trim(), name: $('#name').value.trim() || null,
    seed: $('#seed').value === '' ? null : +$('#seed').value, variations: +$('#variations').value || 1,
    compute: S.compute,
    ref_images: S.refs.map(r => r.name), ref_size: $('#refSize').value,
    cfg: +$('#cfg').value > 1 ? +$('#cfg').value : null,
  };
  if (S.mode === 'story') {
    p.segments = $$('#shots .shot').map(s => ({ prompt: $('textarea', s).value.trim(), seconds: +$('.sec', s).value || 5, camera: $('.cam', s).value })).filter(s => s.prompt);
    if (!p.segments.length) return $('#createErr').textContent = 'Write at least one shot.';
    p.prompt = p.segments[0].prompt;
  } else { p.prompt = $('#prompt').value.trim(); p.seconds = +$('#seconds').value || 5; }
  if (S.mode !== 't2v') { p.start_image = S.images.start_image?.name || null; p.end_image = S.images.end_image?.name || null; }
  if (S.mode === 'i2v' && !p.start_image && !p.end_image) return $('#createErr').textContent = 'Add a start (or end) frame, or use Text → Video.';
  try { await api('/api/jobs', { method: 'POST', body: { type: 'generate', params: p } }); poll(); }
  catch (e) { $('#createErr').textContent = e.message; }
};

/* ------------------------------------------------------------ jobs */
function jobHTML(j) {
  const pct = Math.round((j.progress || 0) * 100);
  const run = j.status === 'running';
  return `<div class="job"><div class="top"><b>${esc(j.type.replace('_', ' '))}</b><span class="st ${j.status}">${j.status}</span>${j.params.compute === 'remote' ? '<span class="st remote">☁ rented GPU</span>' : ''}
    <span class="muted">${run ? esc(j.stage) + ` · ${pct}%` + (j.eta ? ` · ~${fmtT(j.eta)} left` : '') : new Date(j.created * 1000).toLocaleString()}</span><span class="spacer"></span>
    ${j.video ? `<button data-open="${j.video}">Open</button>` : ''}
    ${run || j.status === 'queued' ? `<button data-cancel="${j.id}">Cancel</button>` : `<button data-del="${j.id}">✕</button>`}</div>
    <div class="p">${esc(j.params.prompt || j.params.segments?.[0]?.prompt || '')}</div>
    ${run ? `<div class="bar"><div style="width:${pct}%"></div></div>` : ''}
    ${j.error ? `<div class="error">${esc(j.error)}</div>` : ''}
    ${run ? `<div class="prevwrap" id="pw_${j.id}" hidden><img class="jobprev" id="prev_${j.id}" alt="">
      <div class="prevcap">live preview — frames sampled across the whole clip, sharpening each step</div></div>
      <div class="framestrip" id="fs_${j.id}"></div>` : ''}</div>`;
}
document.addEventListener('click', async e => {
  const t = e.target.closest('[data-open],[data-cancel],[data-del]');
  if (!t) return;
  if (t.dataset.open) openEditor(t.dataset.open);
  if (t.dataset.cancel) { await api(`/api/jobs/${t.dataset.cancel}/cancel`, { method: 'POST' }); poll(); }
  if (t.dataset.del) { await api(`/api/jobs/${t.dataset.del}`, { method: 'DELETE' }); poll(); }
});

let lastJobsKey = '';
async function poll() {
  try {
    S.jobs = await api('/api/jobs');
  } catch { $('#sys').innerHTML = '<span class="dot"></span>server offline'; return; }
  const active = S.jobs.filter(j => j.status === 'running' || j.status === 'queued');
  $('#queueBadge').hidden = !active.length; $('#queueBadge').textContent = active.length;
  const key = JSON.stringify(S.jobs.map(j => [j.id, j.status, j.progress, j.stage, j.eta]));
  if (key !== lastJobsKey) {
    lastJobsKey = key;
    $('#jobList').innerHTML = S.jobs.map(jobHTML).join('') || '<div class="empty">Nothing yet.</div>';
    $('#activeJob').innerHTML = active.filter(j => j.type === 'generate').map(jobHTML).join('');
    const mine = S.jobs.filter(j => j.params.video && S.cur && lineage(S.cur.id).has(j.params.video) && (j.status === 'running' || j.status === 'queued' || j.status === 'error')).slice(0, 4);
    $('#edJobs').innerHTML = mine.map(jobHTML).join('');
  }
  // refresh live previews (separate from the HTML render so the <img> never flickers)
  for (const j of S.jobs) {
    if (j.status !== 'running') continue;
    for (const el of $$(`#prev_${j.id}`)) {
      const stamp = j.preview_at || 0;
      if (stamp && el.dataset.at !== String(stamp)) {
        el.dataset.at = String(stamp);
        el.onload = () => { const w = el.closest('.prevwrap'); if (w) w.hidden = false; };
        el.src = `/api/jobs/${j.id}/preview?t=${stamp}`;
      }
    }
  }
  // finished frames of the running job (whole segments, real output)
  for (const j of S.jobs) {
    if (j.status !== 'running' || !j.frames_done) continue;
    for (const strip of $$(`#fs_${j.id}`)) {
      if (strip.dataset.n === String(j.frames_done)) continue;
      strip.dataset.n = String(j.frames_done);
      const n = Math.min(j.frames_done, 24);
      strip.innerHTML = Array.from({ length: n }, (_, i) =>
        `<img loading="lazy" src="/api/jobs/${j.id}/frames/${Math.round(i * (j.frames_done - 1) / Math.max(1, n - 1))}?w=160">`).join('');
    }
  }
  let newDone = null;
  for (const j of S.jobs) if (j.status === 'done' && !S.seenDone.has(j.id)) { S.seenDone.add(j.id); if (!S.firstPoll) newDone = newDone || j; }
  S.firstPoll = false;
  if (newDone) {
    await loadVideos();
    if (newDone.type === 'generate') showLatest(newDone.video);
    else if (S.cur && lineage(S.cur.id).has(newDone.params.video)) openEditor(newDone.video, true);
    if ($('#view-library').classList.contains('active')) renderLibrary();
  }
}
function lineage(id) { const s = new Set(); let v = S.videos.find(x => x.id === id); while (v) { s.add(v.id); v = S.videos.find(x => x.id === v.parent); } return s; }

function showLatest(id) {
  const v = S.videos.find(x => x.id === id); if (!v) return;
  $('#latest').innerHTML = `<video src="/videos/${v.id}/video.mp4" controls autoplay loop muted playsinline></video>
    <div class="meta"><b>${esc(v.name)}</b><span class="muted">${v.seconds}s · ${v.width}×${v.height} · seed ${v.seed ?? ''}</span><span class="spacer"></span>
    <button data-open="${v.id}">✂ Open in editor</button><a class="btn" href="/api/videos/${v.id}/download">⬇</a></div>`;
}

async function pollStatus() {
  try {
    S.status = await api('/api/status');
    const m = S.status.models;
    $('#sys').innerHTML = `<div><span class="dot ${S.status.engine ? 'ok' : 'warn'}"></span>engine ${S.status.engine ? 'ready' : 'starting…'}</div>
      <div><span class="dot ${m.draft ? 'ok' : ''}"></span>5B draft model</div><div><span class="dot ${m.hq ? 'ok' : ''}"></span>14B image&rarr;video</div>
      <div><span class="dot ${m.t2v_hq ? 'ok' : ''}"></span>14B text&rarr;video</div>
      <div>${S.status.disk_free_gb} GB free</div>`;
    const rm = S.status.remote || {};
    $('#computeRow').hidden = !rm.enabled;
    $('#edComputeWrap').hidden = !rm.enabled;
    $('#computeLabel').textContent = rm.label ? `· ${rm.label}` : '';
    if (!rm.enabled && S.compute === 'remote') { S.compute = 'local'; $$('#computeTabs .tab').forEach(x => x.classList.toggle('active', x.dataset.compute === 'local')); }
    if (rm.enabled && rm.default_for_new && !S.computeTouched) {
      S.computeTouched = true; S.compute = 'remote';
      $$('#computeTabs .tab').forEach(x => x.classList.toggle('active', x.dataset.compute === 'remote'));
      $('#edCompute').value = 'remote';
    }
    updateHints();
  } catch { /* poll() reports offline */ }
}

/* ------------------------------------------------------------ library */
async function loadVideos() { S.videos = await api('/api/videos'); }
function tileHTML(v) {
  return `<div class="tile" data-id="${v.id}"><div class="th" style="background-image:url(/videos/${v.id}/thumb.jpg)"></div>
    <div class="acts"><button class="fav" data-fav="${v.id}">${v.favorite ? '★' : '☆'}</button><button data-rm="${v.id}">🗑</button></div>
    <div class="inf"><b>${esc(v.name)}</b><small>${v.seconds}s · ${v.width}×${v.height} · ${esc(v.op || '')}${v.quality ? ' · ' + v.quality : ''}</small></div></div>`;
}
async function renderLibrary() {
  await loadVideos();
  const parents = new Set(S.videos.map(v => v.parent).filter(Boolean));
  let list = S.videos;
  if ($('#latestOnly').checked) list = list.filter(v => !parents.has(v.id));
  if ($('#favOnly').checked) list = list.filter(v => v.favorite);
  $('#grid').innerHTML = list.map(tileHTML).join('') || '<div class="empty">No videos yet.</div>';
}
$('#favOnly').onchange = $('#latestOnly').onchange = renderLibrary;
$('#grid').addEventListener('click', async e => {
  const fav = e.target.closest('[data-fav]'), rm = e.target.closest('[data-rm]'), tile = e.target.closest('.tile');
  if (fav) { const v = S.videos.find(x => x.id === fav.dataset.fav); await api('/api/videos/' + v.id, { method: 'PATCH', body: { favorite: !v.favorite } }); return renderLibrary(); }
  if (rm) { if (rm.dataset.armed) { await api('/api/videos/' + rm.dataset.rm, { method: 'DELETE' }); if (S.cur?.id === rm.dataset.rm) closeEditor(); return renderLibrary(); } rm.dataset.armed = 1; rm.textContent = 'Sure?'; setTimeout(() => { rm.textContent = '🗑'; delete rm.dataset.armed; }, 2500); return; }
  if (tile) openEditor(tile.dataset.id);
});
$('#grid').addEventListener('mouseover', e => {
  const th = e.target.closest('.th'); if (!th || $('video', th)) return;
  const v = document.createElement('video'); v.src = `/videos/${th.parentNode.dataset.id}/video.mp4`; v.muted = v.loop = v.autoplay = true; v.playsInline = true;
  th.appendChild(v); th.onmouseleave = () => v.remove();
});

/* ------------------------------------------------------------ editor */
const vid = $('#vid'), tl = $('#timeline');
const frameOf = () => S.cur ? Math.min(S.cur.frames - 1, Math.max(0, Math.floor(vid.currentTime * S.cur.fps + 1e-3))) : 0;
const seekFrame = f => { f = Math.max(0, Math.min(S.cur.frames - 1, f)); vid.currentTime = (f + 0.5) / S.cur.fps; };
const xOf = f => f / Math.max(1, S.cur.frames - 1) * 100;

function closeEditor() { S.cur = null; vid.removeAttribute('src'); $('#editorMain').hidden = true; $('#editorEmpty').hidden = false; }
async function openEditor(id, keepSel = false) {
  await loadVideos();
  const v = S.videos.find(x => x.id === id); if (!v) return;
  const prevSel = keepSel ? S.sel : null;
  S.cur = v; S.sel = null;
  show('editor'); $('#editorMain').hidden = false; $('#editorEmpty').hidden = true; $('#edErr').textContent = '';
  vid.src = `/videos/${v.id}/video.mp4?${v.created}`; vid.loop = !S.loop;
  $('#edName').value = v.name; $('#edInfo').textContent = `${v.seconds}s · ${v.frames} frames @ ${v.fps}fps · ${v.width}×${v.height}`;
  $('#edDownload').href = `/api/videos/${v.id}/download`;
  const tiers = Object.keys(S.status?.quality || {});
  const rmEnabled = (S.status?.remote || {}).enabled;
  $('#exQuality').innerHTML = tiers.filter(t => rmEnabled || !(S.status.quality[t].remote_only))
    .map(t => `<option value="${t}" ${t === v.quality ? 'selected' : ''}>${t}</option>`).join('');
  S.exRefs = []; drawExRefs(); syncExtendPanel();
  // versions: whole family tree of this video
  const root = [...lineage(v.id)].pop(), fam = S.videos.filter(x => [...lineage(x.id)].pop() === root).sort((a, b) => a.created - b.created);
  $('#edVersions').innerHTML = fam.map((x, i) => `<option value="${x.id}" ${x.id === v.id ? 'selected' : ''}>v${i + 1} · ${esc(x.op)} · ${x.seconds}s</option>`).join('');
  const n = Math.min(14, v.frames);
  $('#strip').innerHTML = Array.from({ length: n }, (_, i) => `<img loading="lazy" src="/api/videos/${v.id}/frame/${Math.round(i * (v.frames - 1) / Math.max(1, n - 1))}?w=200">`).join('');
  $('#segs').innerHTML = (v.segments || []).map(s => `<div title="${esc(s.prompt)}" style="left:${xOf(s.start)}%;width:calc(${xOf(s.end) - xOf(s.start)}% - 2px)"></div>`).join('') +
    (v.edits || []).map(s => `<div class="edit" title="edit: ${esc(s.prompt)}" style="left:${xOf(s.start)}%;width:${xOf(s.end) - xOf(s.start)}%"></div>`).join('');
  if (prevSel && prevSel[1] < v.frames) S.sel = prevSel;
  drawSel(); drawHead(); lastJobsKey = ''; poll();
}
$('#edVersions').onchange = e => openEditor(e.target.value);
$('#edName').onchange = async e => { if (S.cur) await api('/api/videos/' + S.cur.id, { method: 'PATCH', body: { name: e.target.value } }); };

function drawHead() {
  if (!S.cur) return;
  const f = frameOf();
  $('#playhead').style.left = `calc(${xOf(f)}% - 1px)`;
  $('#tc').textContent = `frame ${f} / ${S.cur.frames - 1} · ${(f / S.cur.fps).toFixed(2)}s`;
}
function drawSel() {
  const el = $('#sel'), has = !!S.sel;
  el.hidden = !has;
  $$('.needsSel').forEach(b => b.disabled = !has);
  $('#btnRegen').disabled = !has || (S.status && !S.status.models.hq);
  if (!has) { $('#selInfo').textContent = S.status && !S.status.models.hq ? '14B model still downloading — range edits need it.' : 'No range selected — drag on the timeline.'; return; }
  const [a, b] = S.sel;
  el.style.left = xOf(a) + '%'; el.style.width = (xOf(b) - xOf(a)) + '%';
  const n = b - a + 1, passes = Math.max(1, Math.ceil((n - 1) / 80));
  $('#selInfo').innerHTML = `Frames <b>${a}–${b}</b> (${(n / S.cur.fps).toFixed(2)}s, ${n} frames${passes > 1 ? `, ${passes} passes` : ''}). Frames ${a} and ${b} stay untouched; everything between is regenerated. Length snaps to the model's 4k+1 grid.`;
}
(function loopTick() { if (S.cur) { drawHead(); if (S.loop && S.sel && !vid.paused) { const f = frameOf(); if (f >= S.sel[1] || f < S.sel[0]) seekFrame(S.sel[0]); } } requestAnimationFrame(loopTick); })();

const tlFrame = e => { const r = tl.getBoundingClientRect(); return Math.round(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (S.cur.frames - 1)); };
let drag = null;
tl.addEventListener('pointerdown', e => {
  if (!S.cur) return;
  tl.setPointerCapture(e.pointerId); vid.pause();
  const f = tlFrame(e);
  if (e.target.classList.contains('h')) drag = { kind: e.target.classList.contains('l') ? 'l' : 'r' };
  else drag = { kind: 'new', origin: f, moved: false };
  seekFrame(f);
});
tl.addEventListener('pointermove', e => {
  if (!drag) return;
  const f = tlFrame(e);
  if (drag.kind === 'new') { if (Math.abs(f - drag.origin) >= 2) { drag.moved = true; S.sel = [Math.min(f, drag.origin), Math.max(f, drag.origin)]; } }
  else if (drag.kind === 'l') S.sel = [Math.min(f, S.sel[1] - 4), S.sel[1]];
  else S.sel = [S.sel[0], Math.max(f, S.sel[0] + 4)];
  seekFrame(f); drawSel();
});
tl.addEventListener('pointerup', () => { if (drag?.kind === 'new' && drag.moved && S.sel[1] - S.sel[0] < 4) S.sel = null; drag = null; drawSel(); });

const setIn = () => { const f = frameOf(); S.sel = [Math.min(f, S.cur.frames - 5), Math.max(S.sel?.[1] ?? S.cur.frames - 1, f + 4)]; S.sel[1] = Math.min(S.sel[1], S.cur.frames - 1); drawSel(); };
const setOut = () => { const f = frameOf(); S.sel = [Math.min(S.sel?.[0] ?? 0, Math.max(0, f - 4)), Math.max(f, 4)]; drawSel(); };
$('#btnIn').onclick = setIn; $('#btnOut').onclick = setOut;
$('#btnClear').onclick = () => { S.sel = null; drawSel(); };
$('#btnPlay').onclick = () => vid.paused ? vid.play() : vid.pause();
vid.onplay = () => $('#btnPlay').textContent = '❚❚'; vid.onpause = () => $('#btnPlay').textContent = '▶';
$('#btnPrev').onclick = () => { vid.pause(); seekFrame(frameOf() - 1); };
$('#btnNext').onclick = () => { vid.pause(); seekFrame(frameOf() + 1); };
$('#btnLoop').onclick = () => { S.loop = !S.loop; $('#btnLoop').classList.toggle('on', S.loop); vid.loop = true; };
document.addEventListener('keydown', e => {
  if (!$('#view-editor').classList.contains('active') || !S.cur || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  const k = e.key.toLowerCase();
  if (k === ' ') { e.preventDefault(); $('#btnPlay').click(); }
  else if (k === 'arrowleft') { e.preventDefault(); vid.pause(); seekFrame(frameOf() - (e.shiftKey ? 10 : 1)); }
  else if (k === 'arrowright') { e.preventDefault(); vid.pause(); seekFrame(frameOf() + (e.shiftKey ? 10 : 1)); }
  else if (k === 'i') setIn(); else if (k === 'o') setOut(); else if (k === 'l') $('#btnLoop').click(); else if (k === 'escape') $('#btnClear').click();
});

async function edJob(type, params) {
  $('#edErr').textContent = '';
  try { await api('/api/jobs', { method: 'POST', body: { type, params: { video: S.cur.id, compute: $('#edCompute').value, ...params } } }); lastJobsKey = ''; poll(); }
  catch (e) { $('#edErr').textContent = e.message; }
}
$('#btnRegen').onclick = () => {
  const prompt = $('#edPrompt').value.trim();
  if (!prompt) return $('#edErr').textContent = 'Describe what should happen in the range.';
  edJob('edit_range', { f0: S.sel[0], f1: S.sel[1], prompt, quality: $('#edQuality').value, seed: $('#edSeed').value === '' ? null : +$('#edSeed').value });
};
$('#btnExtend').onclick = () => edJob('extend', {
  prompt: $('#exPrompt').value.trim(), seconds: +$('#exSeconds').value || 10,
  camera: $('#exCamera').value, quality: $('#exQuality').value,
  seed: $('#exSeed').value === '' ? null : +$('#exSeed').value,
  ref_images: S.exRefs.map(r => r.name), ref_size: 'match' });
$('#tKeep').onclick = () => edJob('trim', { f0: S.sel[0], f1: S.sel[1], mode: 'keep' });
$('#tCut').onclick = () => edJob('trim', { f0: S.sel[0], f1: S.sel[1], mode: 'cut' });
$('#tReverse').onclick = () => edJob('reverse', {});
$('#tInterp').onclick = () => edJob('interpolate', {});
$('#tUpscale').onclick = () => edJob('upscale', {});
$('#tJoin').onclick = () => {
  $('#pickGrid').innerHTML = S.videos.filter(v => v.id !== S.cur.id).map(tileHTML).join('') || '<div class="empty">No other clips.</div>';
  $('#pick').showModal();
};
$('#pickGrid').addEventListener('click', e => { const t = e.target.closest('.tile'); if (t) { $('#pick').close(); edJob('join', { other: t.dataset.id }); } });
$('#tFrameRef').onclick = async () => {
  const r = await api(`/api/videos/${S.cur.id}/frame/${frameOf()}/as_upload`, { method: 'POST' });
  S.images.start_image = r; setMode('i2v'); show('create');
};
$('#tFrameSave').onclick = () => { const a = document.createElement('a'); a.href = `/api/videos/${S.cur.id}/frame/${frameOf()}`; a.download = `${S.cur.name}_f${frameOf()}.png`; a.click(); };

$('#tReuse').onclick = () => {
  // walk up to the original generation and load its settings back into the Create form
  let v = S.cur; while (v && v.op !== 'generate' && v.parent) v = S.videos.find(x => x.id === v.parent);
  const p = v?.params || {};
  if (p.segments) { setMode('story'); $('#shots').innerHTML = ''; p.segments.forEach(addShot); }
  else { setMode(p.start_image || p.end_image ? 'i2v' : 't2v'); $('#prompt').value = p.prompt || ''; $('#seconds').value = p.seconds || 5; }
  for (const k of ['start_image', 'end_image']) if (p[k]) S.images[k] = { name: p[k], url: '/uploads/' + p[k] }; else delete S.images[k];
  $('#aspect').value = p.aspect || '16:9'; $('#resolution').value = p.resolution || '480p'; $('#camera').value = p.camera || '';
  $('#negative').value = p.negative || ''; $('#style').value = p.style || ''; $('#seed').value = v?.seed ?? '';
  if (p.quality) $(`[data-q=${p.quality}]`).click();
  syncDrops(); updateHints(); show('create');
};

/* ------------------------------------------------------------ logs */
let logBusy = false;
async function refreshLogs(force) {
  if (!force && !$('#view-logs').classList.contains('active')) return;
  if (logBusy) return;
  logBusy = true;
  try {
    const r = await api(`/api/logs/${$('#logPick').value}?lines=600`);
    const box = $('#logBox');
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    box.innerHTML = esc(r.text).replace(/^.*(Traceback|Error|error|FAIL|interrupted).*$/gm, m => `<span class="err">${m}</span>`);
    $('#logInfo').textContent = `${(r.size / 1024).toFixed(0)} KB on disk`;
    if (force || ($('#logFollow').checked && atBottom)) box.scrollTop = box.scrollHeight;
  } catch (e) { $('#logBox').textContent = 'could not read log: ' + e.message; }
  logBusy = false;
}
$('#logPick').onchange = () => refreshLogs(true);
$('#logCopy').onclick = () => navigator.clipboard.writeText($('#logBox').textContent);
setInterval(() => refreshLogs(false), 2000);

/* ------------------------------------------------------------ settings */
async function loadSettings() {
  const s = await api('/api/settings');
  const r = s.remote || {};
  $('#rEnabled').checked = !!r.enabled; $('#rUrl').value = r.url || '';
  $('#rToken').value = r.token || ''; $('#rLabel').value = r.label || '';
  $('#rDefault').checked = !!r.default_for_new;
  const p = s.runpod || {};
  $('#pKey').value = p.api_key === '********' ? '' : '';
  $('#pKey').placeholder = p.api_key ? 'key saved — paste a new one to replace it' : 'rpa_…';
  $('#pSetup').hidden = !p.api_key;
  await rpLoadCatalog();
  if (p.gpu) $('#pGpu').value = p.gpu;
  if (p.datacenter) $('#pDc').value = p.datacenter;
  rpCost();
}
$('#rSave').onclick = async () => {
  $('#rResult').textContent = 'saving…';
  try {
    await api('/api/settings', { method: 'PUT', body: { remote: {
      enabled: $('#rEnabled').checked, url: $('#rUrl').value.trim(), token: $('#rToken').value.trim(),
      label: $('#rLabel').value.trim(), default_for_new: $('#rDefault').checked } } });
    $('#rResult').textContent = 'saved.';
    await pollStatus();
  } catch (e) { $('#rResult').innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`; }
};
$('#rTest').onclick = async () => {
  $('#rResult').textContent = 'connecting…';
  try {
    const r = await api('/api/remote/test', { method: 'POST', body: { url: $('#rUrl').value.trim(), token: $('#rToken').value.trim() } });
    if (!r.ok) { $('#rResult').innerHTML = `<span style="color:var(--err)">${esc(r.error)}</span>`; return; }
    const m = r.models || {};
    $('#rResult').innerHTML = `<span style="color:var(--acc2)">connected</span> — ${esc(r.device || '?')} ·
      ${r.vram_gb} GB VRAM · ComfyUI ${esc(r.comfyui || '?')} · ${r.latency_ms} ms<br>
      models present: Wan image→video ${m.wan_i2v ? '✓' : '✗'} · Wan text→video ${m.wan_t2v ? '✓' : '✗'} ·
      ${m.count} diffusion files`;
  } catch (e) { $('#rResult').innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`; }
};

/* ------------------------------------------------------------ runpod */
let rpCat = null;
async function rpLoadCatalog() {
  if (rpCat) return rpCat;
  try { rpCat = await api('/api/runpod/catalog'); } catch { return null; }
  $('#pGpu').innerHTML = (rpCat.gpus || []).map(g =>
    `<option value="${esc(g.id)}" data-hr="${g.usd_hr}">${esc(g.name)} · ${g.vram_gb} GB · $${g.usd_hr}/hr</option>`).join('');
  $('#pDc').innerHTML = (rpCat.datacenters || []).map(d =>
    `<option value="${esc(d)}">${d.startsWith('CA') ? 'Canada' : d.startsWith('AP-JP') ? 'Japan' : d.startsWith('EUR-IS') ? 'Iceland' : d.startsWith('EUR-NO') ? 'Norway' : esc(d)} (${esc(d)})</option>`).join('');
  rpCost();
  return rpCat;
}
function rpCost() {
  const o = $('#pGpu').selectedOptions[0], hr = o && +o.dataset.hr;
  $('#pCost').textContent = hr ? `$${hr.toFixed(2)} per hour while it runs, billed by the second. Stopping it stops the charge.` : '';
}
$('#pGpu').onchange = rpCost;
const rpSay = (t, err) => $('#pStatus').innerHTML = err ? `<span style="color:var(--err)">${esc(t)}</span>` : esc(t);

$('#pKey').addEventListener('keydown', e => { if (e.key === 'Enter') $('#pSaveKey').click(); });
$('#pSaveKey').onclick = async () => {
  const k = $('#pKey').value.trim();
  if (!k) return rpSay('paste your key first', true);
  try {
    await api('/api/settings', { method: 'PUT', body: { runpod: { api_key: k } } });
    $('#pSetup').hidden = false; $('#pKey').value = '';
    $('#pKey').placeholder = '✓ key saved — paste a new one only to replace it';
    rpSay('✓ key saved. Now choose a GPU below and press Rent.'); rpLoadCatalog();
  } catch (e) { rpSay(e.message, true); }
};

// one button: rent -> wait for boot -> install models -> connect
$('#pLaunch').onclick = async () => {
  const o = $('#pGpu').selectedOptions[0];
  if (!confirm(`Rent ${o ? o.textContent : 'this GPU'}?\n\nBilling starts now and runs until you press Stop.`)) return;
  try {
    await api('/api/runpod/setup', { method: 'POST', body: {
      gpu: $('#pGpu').value, datacenter: $('#pDc').value, models: 'h3' } });
    rpSay('setting up — this keeps running even if you close this page.');
    rpPoll();
  } catch (e) { rpSay(e.message, true); }
};

async function rpStop(action) {
  const msg = action === 'terminate'
    ? 'Delete the rented machine completely? Your installed models are kept for next time.'
    : 'Stop the rented machine? Charges for computing stop immediately.';
  if (!confirm(msg)) return;
  try { await api('/api/runpod/disconnect', { method: 'POST', body: { action } }); rpSay('stopped — you are no longer being charged for computing.'); await pollStatus(); rpPoll(); }
  catch (e) { rpSay(e.message, true); }
}

function rpBadge(s) {
  const el = $('#podBadge');
  const p = s && s.pod;
  if (!s || !s.pod_id || !p) { el.hidden = true; return; }
  const running = p.status === 'RUNNING';
  el.hidden = false;
  el.classList.toggle('paying', running);
  const mins = p.started ? Math.max(0, Math.round((Date.now() - new Date(p.started).getTime()) / 60000)) : null;
  const hr = +p.cost_hr || 0;
  el.innerHTML = `<b>${running ? '● Rented GPU running' : '○ Rented GPU ' + esc(p.status || '')}</b>
    ${running ? `$${hr.toFixed(2)}/hr${mins != null ? ` · ${mins} min · ~$${(hr * mins / 60).toFixed(2)} so far` : ''}` : 'not being charged for computing'}
    <br>${s.tunnel && s.tunnel.up ? 'connected' : 'not connected'}
    ${running ? '<button id="podStop">Stop — end charges</button>' : ''}`;
  const b = $('#podStop'); if (b) b.onclick = () => rpStop('stop');
}

async function rpPoll() {
  try {
    const s = await api('/api/runpod/status');
    rpBadge(s);
    const su = s.setup || {};
    if (su.running) { rpSay(`setting up: ${su.step}…  (safe to close this page)`); setTimeout(rpPoll, 5000); }
    else if (su.error) rpSay('setup failed: ' + su.error, true);
    else if (su.done) rpSay('✓ ready — pick H3 in Create and set “Render on” to the rented GPU.');
    return s;
  } catch { return null; }
}

/* ------------------------------------------------------------ boot */
(async function boot() {
  await pollStatus();
  const st = S.status || { aspects: ['16:9'], resolutions: ['480p'], cameras: [''] };
  $('#aspect').innerHTML = st.aspects.map(a => `<option>${a}</option>`).join('');
  $('#resolution').innerHTML = st.resolutions.map(a => `<option ${a === '480p' ? 'selected' : ''}>${a}</option>`).join('');
  $('#camera').innerHTML = $('#exCamera').innerHTML = cameraOptions();
  if (st.models && !st.models.hq && st.models.draft) $('[data-q=draft]').click();
  await loadVideos(); await poll(); updateHints();
  setInterval(poll, 1500); setInterval(pollStatus, 8000);
  rpPoll(); setInterval(rpPoll, 20000);
})();
