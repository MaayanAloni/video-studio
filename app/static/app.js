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

const S = { mode: 't2v', quality: 'standard', images: {}, status: null, jobs: [], videos: [], cur: null, sel: null, loop: false, seenDone: new Set(), firstPoll: true };

/* ------------------------------------------------------------ navigation */
function show(view) {
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
  $$('.navbtn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  if (view !== 'editor') $('#vid').pause();
  if (view === 'library') renderLibrary();
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
    const hq = c.dataset.q !== 'draft';
    c.classList.toggle('off', S.status ? !(hq ? S.status.models.hq : S.status.models.draft) : false);
  });
  const c = +$('#cfg').value; $('#cfgVal').textContent = c > 1 ? c : '(auto)';
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
$('#file').onchange = e => { if (e.target.files[0]) uploadTo(dropTarget, e.target.files[0]); e.target.value = ''; };
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
  return `<div class="job"><div class="top"><b>${esc(j.type.replace('_', ' '))}</b><span class="st ${j.status}">${j.status}</span>
    <span class="muted">${run ? esc(j.stage) + ` · ${pct}%` + (j.eta ? ` · ~${fmtT(j.eta)} left` : '') : new Date(j.created * 1000).toLocaleString()}</span><span class="spacer"></span>
    ${j.video ? `<button data-open="${j.video}">Open</button>` : ''}
    ${run || j.status === 'queued' ? `<button data-cancel="${j.id}">Cancel</button>` : `<button data-del="${j.id}">✕</button>`}</div>
    <div class="p">${esc(j.params.prompt || j.params.segments?.[0]?.prompt || '')}</div>
    ${run ? `<div class="bar"><div style="width:${pct}%"></div></div>` : ''}
    ${j.error ? `<div class="error">${esc(j.error)}</div>` : ''}</div>`;
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
      <div><span class="dot ${m.draft ? 'ok' : ''}"></span>5B draft model</div><div><span class="dot ${m.hq ? 'ok' : ''}"></span>14B HQ model</div>
      <div>${S.status.disk_free_gb} GB free</div>`;
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
  try { await api('/api/jobs', { method: 'POST', body: { type, params: { video: S.cur.id, ...params } } }); lastJobsKey = ''; poll(); }
  catch (e) { $('#edErr').textContent = e.message; }
}
$('#btnRegen').onclick = () => {
  const prompt = $('#edPrompt').value.trim();
  if (!prompt) return $('#edErr').textContent = 'Describe what should happen in the range.';
  edJob('edit_range', { f0: S.sel[0], f1: S.sel[1], prompt, quality: $('#edQuality').value, seed: $('#edSeed').value === '' ? null : +$('#edSeed').value });
};
$('#btnExtend').onclick = () => edJob('extend', { prompt: $('#exPrompt').value.trim(), seconds: +$('#exSeconds').value || 5, camera: $('#exCamera').value, quality: $('#edQuality').value });
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
})();
