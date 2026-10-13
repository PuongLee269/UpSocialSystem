// Wizard 8 bước. Vanilla ES module, không build, không CDN. Mọi chuỗi dữ liệu vào DOM qua textContent.
const STEPS = ['Nguồn', 'Frame', 'Nội dung', 'Thumbnail', 'VidIQ', 'Kiểm tra', 'Upload riêng tư', 'Công khai'];
const LIMITS = { title: 100, descBytes: 5000, tag: 100, tagsTotal: 500 };
const CHECKS = [
  ['rights', 'Tôi có quyền dùng toàn bộ hình ảnh, âm thanh, nhạc và tư liệu trong video.'],
  ['policy', 'Tôi đã rà nội dung theo chính sách cộng đồng/quảng cáo của YouTube.'],
  ['metadata', 'Tiêu đề, mô tả, tags và thumbnail phản ánh đúng nội dung video.'],
  ['disclosure', 'Tôi đã xử lý nhãn tài trợ / nội dung tổng hợp hoặc chỉnh sửa (nếu có).'],
  ['kids', 'Tôi đã xác định đối tượng (có/không dành cho trẻ em).'],
];
const AI_FLAGS = [['ai_content', 'Nội dung có AI hỗ trợ'], ['ai_thumbnail', 'Thumbnail có AI hỗ trợ'], ['ai_text', 'Tiêu đề/mô tả có AI hỗ trợ']];
const LS_JOB = 'agent.currentJob';

const S = { videos: [], filter: '', job: null, jobs: [], step: 0, integ: null, frames: [], templates: {}, thumbSel: null, busy: 0, loadErr: null };
const $ = (s) => document.querySelector(s);

// ---------- helpers ----------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'value' || k === 'checked' || k === 'disabled') el[k] = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
const utf8Len = (s) => new TextEncoder().encode(s).length;
const fmtDate = (s) => { try { return s ? new Date(s).toLocaleString('vi-VN') : '—'; } catch { return String(s); } };
const fmtDur = (n) => (n ? `${Math.floor(n / 60)}:${String(Math.round(n % 60)).padStart(2, '0')}` : '—');
const fmtSize = (n) => (n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const fileUrl = (rel) => `/api/file?path=${encodeURIComponent(`jobs/${S.job.id}/${rel}`)}`;

class ApiError extends Error {
  constructor(status, body) { super((body && body.error) || `Lỗi máy chủ (${status})`); this.status = status; this.body = body || {}; }
}
async function api(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) }, body: opts.body === undefined ? undefined : JSON.stringify(opts.body) });
  } catch {
    throw new ApiError(0, { error: 'Không kết nối được máy chủ local. Hãy kiểm tra app.py còn chạy rồi thử lại.' });
  }
  let body = null;
  try { body = await r.json(); } catch { /* không phải JSON */ }
  if (!r.ok) throw new ApiError(r.status, body);
  return body;
}

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 3500);
}
function showError(e, retry) {
  const box = $('#alert'); box.replaceChildren();
  const conflict = e.status === 409 && (e.body.code === 'conflict');
  box.append(h('strong', {}, conflict ? 'Dữ liệu đã thay đổi ở nơi khác. ' : 'Có lỗi. '), e.message);
  const reasons = e.body.reasons || [];
  if (reasons.length) box.append(h('ul', {}, reasons.map((x) => h('li', {}, x))));
  const row = h('div', { class: 'row' });
  if (conflict) row.append(h('button', { class: 'btn secondary', onclick: async () => { await reloadJob(); clearError(); toast('Đã tải bản mới nhất.'); } }, 'Tải bản mới nhất'));
  else if (retry) row.append(h('button', { class: 'btn secondary', onclick: () => { clearError(); retry(); } }, 'Thử lại'));
  row.append(h('button', { class: 'btn secondary', onclick: clearError }, 'Đóng'));
  box.append(row); box.classList.remove('hidden'); box.focus?.();
  box.scrollIntoView({ block: 'nearest' });
}
function clearError() { $('#alert').classList.add('hidden'); }

// Chạy action có loading + xử lý lỗi. Trả về true nếu thành công.
async function run(btn, fn, retryable = true) {
  if (btn) { btn.disabled = true; btn.classList.add('busy'); btn.dataset.label = btn.textContent; btn.textContent = 'Đang xử lý…'; }
  $('#stage').setAttribute('aria-busy', 'true');
  try { clearError(); await fn(); return true; }
  catch (e) { showError(e, retryable ? () => run(btn, fn, retryable) : null); return false; }
  finally {
    $('#stage').setAttribute('aria-busy', 'false');
    if (btn && btn.isConnected) { btn.disabled = false; btn.classList.remove('busy'); btn.textContent = btn.dataset.label; }
  }
}

// ---------- job ----------
const adopt = (r) => { S.job = r && r.job ? r.job : r; try { localStorage.setItem(LS_JOB, S.job.id); } catch { /* bỏ qua */ } };
async function saveSection(section, data) {
  const r = await api(`/api/jobs/${S.job.id}/${section}`, { method: 'PUT', body: { expected_revision: S.job.revision, data } });
  adopt(r); renderAll();
}
async function reloadJob() {
  if (!S.job) return;
  adopt(await api(`/api/jobs/${S.job.id}`)); renderAll();
}
async function openJob(id) {
  adopt(await api(`/api/jobs/${encodeURIComponent(id)}`));
  S.frames = []; S.thumbSel = null;
  S.step = firstOpenStep(); renderAll();
}
async function createJob(videoId) {
  adopt(await api('/api/jobs', { method: 'POST', body: { video_id: videoId } }));
  S.frames = []; S.step = firstOpenStep(); renderAll();
}

// ---------- trạng thái bước ----------
function validate(c) {
  const errs = [];
  const title = c.title || '';
  if (!title.trim()) errs.push('Cần có tiêu đề.');
  if (title.length > LIMITS.title) errs.push(`Tiêu đề tối đa ${LIMITS.title} ký tự.`);
  if (/[<>]/.test(title)) errs.push('Tiêu đề không được chứa < hoặc >.');
  if (utf8Len(c.description || '') > LIMITS.descBytes) errs.push(`Mô tả tối đa ${LIMITS.descBytes} byte.`);
  const tags = c.tags || [], seen = new Set();
  for (const t of tags) {
    if (!t.trim()) errs.push('Có tag rỗng.');
    if (t.length > LIMITS.tag) errs.push(`Tag "${t.slice(0, 20)}…" dài quá ${LIMITS.tag} ký tự.`);
    if (seen.has(t.toLowerCase())) errs.push(`Tag trùng: ${t}`);
    seen.add(t.toLowerCase());
  }
  if (tagChars(tags) > LIMITS.tagsTotal) errs.push(`Tổng tags vượt ${LIMITS.tagsTotal} ký tự.`);
  return errs;
}
const tagChars = (tags) => tags.reduce((n, t) => n + t.length + (/\s/.test(t) ? 2 : 0), 0);
const checklistDone = (j) => CHECKS.every(([k]) => j.checklist && j.checklist[k] === true);
function stepState(i) {
  const j = S.job; if (!j) return i === 0 ? 'doing' : 'todo';
  const stages = ['source', 'frame', 'content', 'thumbnail', 'vidiq', 'checklist', 'upload', 'public'];
  if (j.error && j.error.stage === stages[i]) return 'error';
  const done = [true, j.frame.selected_index != null, j.content.title && validate(j.content).length === 0,
    j.thumbnail.approved_version != null, j.vidiq.confirmed_by_user, checklistDone(j),
    j.upload.state === 'done', j.public.state === 'done'][i];
  return done ? 'done' : (i === S.step ? 'doing' : 'todo');
}
function firstOpenStep() { for (let i = 0; i < 8; i++) if (stepState(i) !== 'done') return i; return 7; }
function privateGate(j) {
  const r = [];
  if (!j.content.title || validate(j.content).length) r.push('Nội dung (title/mô tả/tags) chưa hợp lệ.');
  if (j.frame.selected_index == null) r.push('Chưa chọn frame.');
  if (!j.vidiq.confirmed_by_user) r.push('Chưa xác nhận bước VidIQ.');
  if (j.thumbnail.approved_version == null) r.push('Chưa duyệt thumbnail.');
  if (!checklistDone(j)) r.push('Checklist trước upload chưa đủ 5 mục.');
  return r;
}
function publicGate(j) {
  const r = privateGate(j);
  if (j.upload.state !== 'done' || !j.upload.youtube_video_id) r.push('Chưa upload riêng tư thành công.');
  if (j.status !== 'private_uploaded' && j.status !== 'public') r.push('Job chưa ở trạng thái đã upload riêng tư.');
  return r;
}

// ---------- chrome: stepper, banner, side ----------
const STATE_LABEL = { todo: 'chưa làm', doing: 'đang làm', done: 'đạt', error: 'lỗi' };
function renderStepper() {
  const ol = $('#stepper'); ol.replaceChildren();
  STEPS.forEach((name, i) => {
    const st = stepState(i);
    const b = h('button', { type: 'button', disabled: !S.job && i > 0, 'aria-current': i === S.step ? 'step' : null,
      onclick: () => { S.step = i; renderAll(); } },
      h('span', { class: 'num', 'aria-hidden': 'true' }, st === 'done' ? '✓' : st === 'error' ? '!' : i + 1),
      h('span', {}, name, h('br'), h('span', { class: 'st' }, STATE_LABEL[st])));
    ol.append(h('li', { class: st }, b));
  });
}
function renderBanners() {
  const b = $('#banners'); b.replaceChildren();
  const g = S.integ;
  if (!g) { b.append(h('div', { class: 'badge warn' }, 'Đang kiểm tra tích hợp…')); return; }
  b.append(h('div', { class: 'badge warn' }, 'VidIQ: thủ công – chưa kết nối API'));
  const y = g.youtube || {};
  b.append(h('div', { class: y.configured && y.authorized ? 'badge' : 'badge warn' },
    y.configured ? (y.authorized ? 'YouTube: đã cấp quyền OAuth' : `YouTube: chưa cấp quyền${y.reason ? ' – ' + y.reason : ''}`) : 'YouTube: chưa cấu hình OAuth'));
}
function renderSide() {
  const s = $('#side'); s.replaceChildren(h('h2', {}, 'Job hiện tại'));
  const j = S.job;
  if (!j) s.append(h('p', { class: 'hint' }, 'Chưa chọn video. Chọn nguồn ở bước 1 để bắt đầu.'));
  else {
    s.append(h('dl', { class: 'kv' },
      h('dt', {}, 'Video'), h('dd', {}, j.source.name),
      h('dt', {}, 'Trạng thái'), h('dd', {}, j.status),
      h('dt', {}, 'Phiên bản'), h('dd', {}, `rev ${j.revision}`),
      h('dt', {}, 'Cập nhật'), h('dd', {}, fmtDate(j.updated)),
      h('dt', {}, 'YouTube ID'), h('dd', {}, j.upload.youtube_video_id || '—')));
    if (j.error) s.append(h('p', { class: 'status err' }, `Lỗi ở bước ${j.error.stage}: ${j.error.message}`));
    const g = publicGate(j);
    s.append(h('h3', {}, 'Điều kiện còn thiếu'), g.length ? h('ul', { class: 'gatelist' }, g.map((x) => h('li', {}, x))) : h('p', { class: 'hint' }, 'Đủ điều kiện.'));
  }
  if (S.jobs.length) {
    s.append(h('h3', {}, 'Job gần đây'), h('div', { class: 'jobs' }, S.jobs.slice(0, 8).map((x) =>
      h('button', { type: 'button', onclick: () => run(null, () => openJob(x.id)) }, `${x.title || '(chưa có tiêu đề)'} · ${x.status}`))));
  }
}
function renderAll() { renderStepper(); renderBanners(); renderSide(); renderStage(); }

// ---------- stage ----------
function renderStage() {
  const stage = $('#stage'); stage.replaceChildren();
  const fns = [stepSource, stepFrame, stepContent, stepThumb, stepVidiq, stepChecklist, stepUpload, stepPublic];
  if (S.step > 0 && !S.job) { S.step = 0; }
  fns[S.step](stage);
}
const head = (n, title, lead) => [h('h2', { tabindex: '-1', id: 'stepHead' }, `${n}. ${title}`), h('p', { class: 'lead' }, lead)];
function navRow(stage, nextOk = true) {
  stage.append(h('div', { class: 'row end', style: 'margin-top:18px' },
    S.step > 0 && h('button', { class: 'btn secondary', type: 'button', onclick: () => { S.step--; renderAll(); } }, '← Bước trước'),
    S.step < 7 && h('button', { class: 'btn secondary', type: 'button', disabled: !nextOk, onclick: () => { S.step++; renderAll(); } }, 'Bước tiếp →')));
}
function field(label, input, extra) { return h('label', { class: 'field' }, h('span', {}, label), input, extra); }

// 1 Nguồn
function stepSource(stage) {
  stage.append(...head(1, 'Chọn video nguồn', 'Tìm video đã xuất trong các thư mục nguồn. Chọn xong sẽ tạo (hoặc mở lại) job cho video đó.'));
  const info = h('div', { class: 'hint', role: 'status' }, S.videos.length ? `${S.videos.length} video.` : (S.loadErr ? '' : 'Đang tải danh sách…'));
  const list = h('div', { class: 'list' });
  const preview = h('div', { class: 'status', hidden: true });
  const draw = () => {
    const q = S.filter.toLowerCase();
    const arr = S.videos.filter((v) => `${v.name} ${v.folder}`.toLowerCase().includes(q));
    list.replaceChildren(...(arr.length ? arr.map((v) => h('button', { type: 'button', class: 'video', 'aria-pressed': S.job && S.job.source.video_id === v.id ? 'true' : 'false',
      onclick: () => { showPreview(v); } },
      h('span', { class: 'pill' }, (v.ext || '').slice(1).toUpperCase()),
      h('span', {}, h('div', { class: 'vname' }, v.name), h('div', { class: 'vmeta' }, `${v.folder} · ${fmtSize(v.size)} · ${v.modified}`)),
      h('span', { class: 'pill' }, v.width ? `${v.width}×${v.height}` : 'chưa đọc thông số'))) : [h('p', { class: 'hint' }, 'Không có video khớp. Kiểm tra video_roots trong config.json.')]));
  };
  const showPreview = (v) => {
    preview.hidden = false; preview.replaceChildren(
      h('strong', {}, v.name), h('div', { class: 'vmeta' }, `${v.folder} · ${fmtSize(v.size)} · ${fmtDur(v.duration)}${v.warning ? ' · ' + v.warning : ''}`),
      h('div', { class: 'row', style: 'margin-top:8px' }, h('button', { class: 'btn', type: 'button', onclick: (e) => run(e.currentTarget, () => createJob(v.id)) }, 'Dùng video này')));
  };
  const search = h('input', { type: 'search', class: 'grow', value: S.filter, 'aria-label': 'Lọc theo tên hoặc thư mục', placeholder: 'Lọc theo tên video hoặc thư mục', oninput: (e) => { S.filter = e.target.value; draw(); } });
  const scan = h('button', { class: 'btn secondary', type: 'button', onclick: (e) => run(e.currentTarget, loadVideos) }, 'Quét lại thư mục');
  stage.append(h('div', { class: 'row' }, search, scan), info, list, preview);
  if (S.loadErr) stage.append(h('p', { class: 'status err' }, S.loadErr));
  draw();
  if (S.job) stage.append(h('p', { class: 'status ok' }, `Đang làm việc với: ${S.job.source.name}`));
  navRow(stage, !!S.job);
}
async function loadVideos() {
  try { const j = await api('/api/videos'); S.videos = j.videos || []; S.loadErr = null; }
  catch (e) { S.loadErr = e.message; throw e; }
  finally { if (S.step === 0) renderStage(); }
}

// 2 Frame
async function ensureFrames() {
  if (S.frames.length || !S.job) return;
  const r = await api(`/api/jobs/${S.job.id}/contact-sheet`);
  S.frames = r.frames || []; renderStage();
}
function stepFrame(stage) {
  stage.append(...head(2, 'Chọn frame', 'Chọn khung hình làm nền thumbnail. Xem trước phần cắt 16:9 bên dưới.'));
  const j = S.job; const sel = j.frame.selected_index;
  if (!S.frames.length) {
    stage.append(h('p', { class: 'status' }, h('span', { class: 'spinner' }), 'Đang tạo contact sheet…'));
    run(null, ensureFrames); navRow(stage, false); return;
  }
  const chosen = S.frames.find((f) => f.index === sel);
  const cropState = { y: j.frame.crop169 ? (j.frame.crop169.pos ?? 0.5) : 0.5, hFrac: 1, yFrac: 0 };
  const box = h('div', { class: 'frame169' });
  const img = h('img', { alt: chosen ? `Frame ${chosen.index} tại ${chosen.time_sec}s, cắt 16:9` : '' });
  const slider = h('input', { type: 'range', min: 0, max: 1, step: 0.01, value: cropState.y, 'aria-label': 'Vị trí cắt dọc 16:9' });
  const applyCrop = () => {
    const r = img.naturalHeight / (img.naturalWidth || 1), box169 = 0.5625;
    if (!img.naturalWidth || r <= box169) { img.style.top = '0'; cropState.hFrac = 1; cropState.yFrac = 0; return; }
    img.style.top = `${-((r - box169) / box169) * cropState.y * 100}%`;
    cropState.hFrac = box169 / r; cropState.yFrac = cropState.y * (1 - cropState.hFrac);
  };
  slider.addEventListener('input', () => { cropState.y = Number(slider.value); applyCrop(); });
  img.addEventListener('load', applyCrop);
  if (chosen) { img.src = chosen.url; box.append(img); } else box.append(h('p', { class: 'hint', style: 'padding:12px' }, 'Chưa chọn frame.'));
  const ul = h('ul', { class: 'sheet' }, S.frames.map((f) => h('li', {}, h('button', { type: 'button', 'aria-pressed': f.index === sel ? 'true' : 'false',
    onclick: (e) => run(e.currentTarget, () => saveFrame(f, { y: 0.5, hFrac: 1, yFrac: 0 })) },
    h('img', { src: f.url, loading: 'lazy', alt: `Frame ${f.index} tại ${f.time_sec} giây` }), h('span', { class: 'cap' }, `#${f.index} · ${f.time_sec}s`)))));
  stage.append(ul, h('h3', {}, 'Xem trước cắt 16:9'), box,
    chosen && field('Vị trí cắt dọc', slider),
    chosen && h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn', type: 'button',
      onclick: (e) => run(e.currentTarget, () => saveFrame(chosen, cropState)) }, 'Lưu vùng cắt')));
  navRow(stage, sel != null);
}
async function saveFrame(f, c) {
  await saveSection('frame', { selected_index: f.index, time_sec: f.time_sec, file: f.file || S.job.frame.file,
    crop169: { x: 0, y: c.yFrac, w: 1, h: c.hFrac, pos: c.y } });
  toast('Đã lưu frame.');
}

// 3 Nội dung
function stepContent(stage) {
  const c = S.job.content;
  stage.append(...head(3, 'Nội dung', 'Tiêu đề, mô tả và tags theo giới hạn của YouTube.'));
  const cnt = (id) => h('div', { class: 'counter', id, 'aria-live': 'polite' });
  const title = h('input', { type: 'text', value: c.title, 'aria-describedby': 'cT' });
  const desc = h('textarea', { 'aria-describedby': 'cD', style: 'min-height:180px' }); desc.value = c.description;
  const tags = h('textarea', { 'aria-describedby': 'cG' }); tags.value = (c.tags || []).join(', ');
  const errBox = h('ul', { class: 'gatelist', role: 'status' });
  const parseTags = () => tags.value.split(/[,\n]/).map((t) => t.trim()).filter(Boolean);
  const upd = () => {
    const tg = parseTags();
    const set = (id, text, over) => { const e = $(id); e.textContent = text; e.classList.toggle('over', over); };
    set('#cT', `${title.value.length}/${LIMITS.title} ký tự`, title.value.length > LIMITS.title);
    set('#cD', `${utf8Len(desc.value)}/${LIMITS.descBytes} byte`, utf8Len(desc.value) > LIMITS.descBytes);
    set('#cG', `${tg.length} tag · ${tagChars(tg)}/${LIMITS.tagsTotal} ký tự (tag có dấu cách tính thêm 2)`, tagChars(tg) > LIMITS.tagsTotal);
    errBox.replaceChildren(...validate({ title: title.value, description: desc.value, tags: tg }).map((x) => h('li', {}, x)));
  };
  [title, desc, tags].forEach((el) => el.addEventListener('input', upd));
  const v = c.vidiq_source;
  stage.append(h('div', { class: 'stack' },
    field('Tiêu đề', title, cnt('cT')), field('Mô tả', desc, cnt('cD')),
    field('Tags (cách nhau bằng dấu phẩy)', tags, cnt('cG')), errBox,
    h('div', { class: 'status' }, 'Nguồn/điểm VidIQ: ', S.job.vidiq.lookup_date
      ? `tra cứu ngày ${S.job.vidiq.lookup_date}; ${(S.job.vidiq.keywords || []).filter((k) => k.keyword).map((k) => `${k.keyword}${k.score != null ? ' (' + k.score + ')' : ''}`).join(', ') || 'chưa có từ khóa'}.`
      : 'chưa có số liệu VidIQ xác thực – nhập ở bước 5. Không tự điền điểm.'),
    h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', onclick: (e) => {
      const tg = parseTags(); const errs = validate({ title: title.value, description: desc.value, tags: tg });
      if (errs.length) { showError(new ApiError(0, { error: 'Nội dung chưa hợp lệ.', reasons: errs })); return; }
      run(e.currentTarget, async () => { await saveSection('content', { title: title.value.trim(), description: desc.value, tags: tg, tags_total_chars: tagChars(tg) }); toast('Đã lưu nội dung.'); });
    } }, 'Lưu nội dung'))));
  void v; upd(); navRow(stage, validate(c).length === 0 && !!c.title);
}

// 4 Thumbnail
function stepThumb(stage) {
  const t = S.job.thumbnail; const vers = t.versions || [];
  stage.append(...head(4, 'Thumbnail', 'Tạo thumbnail từ frame thật đã chọn. Duyệt một phiên bản để đi tiếp.'));
  if (S.job.frame.selected_index == null) { stage.append(h('p', { class: 'status warn' }, 'Hãy chọn frame ở bước 2 trước.')); navRow(stage, false); return; }
  if (!S.templates._loaded) run(null, loadTemplates, false);
  const cur = vers.find((x) => x.v === (S.thumbSel ?? t.approved_version ?? (vers.length ? vers[vers.length - 1].v : null)));
  const big = h('img', { class: 'preview-big', alt: cur ? `Thumbnail phiên bản ${cur.v}` : '', src: cur ? fileUrl(cur.file) : null, hidden: !cur });
  const phone = h('div', { class: 'phone', 'aria-label': 'Mô phỏng hiển thị trên điện thoại' }, h('div', { class: 'notch' }),
    cur ? h('img', { src: fileUrl(cur.file), alt: '' }) : h('div', { class: 'hint' }, 'Chưa có thumbnail'), h('div', { class: 'pt' }, S.job.content.title || 'Tiêu đề video'));
  stage.append(h('div', { class: 'two' }, h('div', {}, big, !cur && h('p', { class: 'hint' }, 'Chưa có phiên bản nào. Bấm "Tạo thumbnail".')), phone));
  const text = h('input', { type: 'text', value: (cur && cur.text) || '', maxlength: 60 });
  const tplName = h('input', { type: 'text', value: (cur && cur.template) || '', list: 'tplList' });
  const dl = h('datalist', { id: 'tplList' }, Object.keys(S.templates).filter((k) => k !== '_loaded').map((k) => h('option', { value: k })));
  const note = h('input', { type: 'text', value: '' });
  const brief = `Thumbnail 16:9 cho video "${S.job.content.title || S.job.source.name}". Dùng frame thật #${S.job.frame.selected_index}; chữ ngắn, tương phản cao, giữ chủ thể trong vùng an toàn; không thêm chi tiết sai sự thật.`;
  stage.append(h('div', { class: 'stack', style: 'margin-top:14px' },
    field('Chữ trên thumbnail (ngắn)', text), field('Template (tên để lưu/tải lại)', tplName), dl,
    h('div', { class: 'row' },
      h('button', { class: 'btn', type: 'button', onclick: (e) => run(e.currentTarget, async () => {
        const r = await api(`/api/jobs/${S.job.id}/thumbnail/render`, { method: 'POST', body: { frame_index: S.job.frame.selected_index, template: tplName.value.trim(), text: text.value, note: note.value } });
        await reloadJob(); S.thumbSel = r.version && r.version.v != null ? r.version.v : r.version; renderStage(); toast('Đã tạo thumbnail.');
      }) }, 'Tạo thumbnail'),
      h('button', { class: 'btn secondary', type: 'button', disabled: !tplName.value && false, onclick: (e) => run(e.currentTarget, async () => {
        const name = tplName.value.trim(); if (!name) throw new ApiError(0, { error: 'Nhập tên template trước khi lưu.' });
        await api(`/api/templates/${encodeURIComponent(name)}`, { method: 'PUT', body: { data: { text: text.value, template: name } } });
        S.templates[name] = { text: text.value }; toast('Đã lưu template.'); renderStage();
      }) }, 'Lưu template'),
      cur && h('button', { class: 'btn', type: 'button', disabled: t.approved_version === cur.v, onclick: (e) => run(e.currentTarget, async () => {
        await api(`/api/jobs/${S.job.id}/thumbnail/approve`, { method: 'POST', body: { version: cur.v } }); await reloadJob(); toast(`Đã duyệt phiên bản ${cur.v}.`);
      }) }, t.approved_version === cur.v ? 'Đã duyệt' : `Duyệt phiên bản ${cur.v}`)),
    field('Ghi chú cho lần tạo này (tùy chọn)', note),
    h('h3', {}, 'Brief / prompt để dùng với công cụ ảnh'), h('pre', { class: 'brief', tabindex: '0' }, brief),
    h('button', { class: 'btn secondary', type: 'button', onclick: () => copyText(brief) }, 'Chép brief')));
  if (vers.length) stage.append(h('h3', {}, 'Lịch sử phiên bản'), h('ul', { class: 'versions' }, vers.map((x) => h('li', {}, h('button', { type: 'button', 'aria-pressed': cur && cur.v === x.v ? 'true' : 'false',
    onclick: () => { S.thumbSel = x.v; renderStage(); } }, `v${x.v}${t.approved_version === x.v ? ' ✓' : ''} · ${x.text || 'không chữ'}`)))));
  navRow(stage, t.approved_version != null);
}
async function loadTemplates() {
  const r = await api('/api/templates'); S.templates = { ...(r.templates || r), _loaded: true }; renderStage();
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('Đã chép.'); } catch { toast('Không chép được, hãy bôi đen và chép thủ công.'); }
}

// 5 VidIQ
const kwRow = (k, i, list, redraw) => h('div', { class: 'kw' },
  field('Từ khóa', h('input', { type: 'text', value: k.keyword || '', oninput: (e) => { k.keyword = e.target.value; } })),
  field('Điểm', h('input', { type: 'number', min: 0, max: 100, value: k.score ?? '', oninput: (e) => { k.score = e.target.value === '' ? null : Number(e.target.value); } })),
  field('Điểm cho', h('select', { onchange: (e) => { k.score_for = e.target.value; } },
    h('option', { value: 'keyword', selected: (k.score_for || 'keyword') === 'keyword' }, 'keyword'), h('option', { value: 'thumbnail', selected: k.score_for === 'thumbnail' }, 'thumbnail'))),
  h('button', { class: 'btn secondary', type: 'button', 'aria-label': `Xóa dòng ${i + 1}`, onclick: () => { list.splice(i, 1); redraw(); } }, 'Xóa'));
function stepVidiq(stage) {
  const v = JSON.parse(JSON.stringify(S.job.vidiq));
  v.keywords = v.keywords && v.keywords.length ? v.keywords : [{ keyword: '', score: null, score_for: 'keyword', volume: null, competition: null }];
  stage.append(...head(5, 'Kiểm tra VidIQ (thủ công)', 'App không gọi VidIQ. Mở VidIQ, tra cứu, rồi nhập đúng số liệu bạn thấy. Không có số liệu thì để trống.'));
  const briefBox = h('pre', { class: 'brief', tabindex: '0' }, 'Chưa tải brief.');
  const kwBox = h('div', {});
  const redraw = () => kwBox.replaceChildren(...v.keywords.map((k, i) => kwRow(k, i, v.keywords, redraw)));
  redraw();
  const lookup = h('input', { type: 'date', value: v.lookup_date || '', onchange: (e) => { v.lookup_date = e.target.value || null; } });
  const thumbScore = h('input', { type: 'number', min: 0, max: 100, value: v.thumbnail_score && v.thumbnail_score.value != null ? v.thumbnail_score.value : '' });
  const notes = h('textarea', {}); notes.value = v.notes || '';
  const confirm = h('input', { type: 'checkbox', id: 'vConf', checked: !!v.confirmed_by_user });
  stage.append(h('div', { class: 'stack' },
    h('div', { class: 'row' },
      h('a', { class: 'btn secondary', href: 'https://app.vidiq.com/', target: '_blank', rel: 'noopener noreferrer' }, 'Mở VidIQ (tab mới)'),
      h('button', { class: 'btn secondary', type: 'button', onclick: (e) => run(e.currentTarget, async () => {
        const r = await api(`/api/vidiq/brief?job=${encodeURIComponent(S.job.id)}`); briefBox.textContent = r.text; await copyText(r.text);
      }) }, 'Lấy & chép brief')), briefBox,
    h('h3', {}, 'Số liệu tra cứu'), field('Ngày tra cứu', lookup), kwBox,
    h('button', { class: 'btn secondary', type: 'button', onclick: () => { v.keywords.push({ keyword: '', score: null, score_for: 'keyword', volume: null, competition: null }); redraw(); } }, '+ Thêm từ khóa'),
    field('Điểm thumbnail (nếu VidIQ cho)', thumbScore), field('Ghi chú', notes),
    h('label', { class: 'check' }, confirm, h('span', {}, 'Tôi xác nhận đã tự kiểm tra trong VidIQ và số liệu ở trên là thật (hoặc tôi chủ động bỏ qua bước này).')),
    h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', onclick: (e) => run(e.currentTarget, async () => {
      const keywords = v.keywords.filter((k) => (k.keyword || '').trim()).map((k) => ({ ...k, keyword: k.keyword.trim(), score_for: k.score_for || 'keyword' }));
      const ts = thumbScore.value === '' ? null : Number(thumbScore.value);
      const wasConfirmed = S.job.vidiq.confirmed_by_user;
      await saveSection('vidiq', { ...v, keywords, notes: notes.value, confirmed_by_user: confirm.checked,
        reviewed: confirm.checked, reviewed_at: confirm.checked ? (wasConfirmed ? v.reviewed_at : new Date().toISOString()) : null,
        thumbnail_score: { ...(v.thumbnail_score || {}), value: ts, score_for: 'thumbnail' } });
      toast('Đã lưu bước VidIQ.');
    }) }, 'Lưu'))));
  navRow(stage, S.job.vidiq.confirmed_by_user);
}

// 6 Checklist
function stepChecklist(stage) {
  const cl = S.job.checklist; const flags = new Set(cl.ai_flags || []);
  stage.append(...head(6, 'Kiểm tra trước upload', 'Bạn tự xác nhận từng mục. App không tự kiểm tra hay cam kết video tuân thủ chính sách YouTube.'));
  const boxes = {};
  const list = h('div', { class: 'checks' }, CHECKS.map(([k, label]) => {
    boxes[k] = h('input', { type: 'checkbox', checked: cl[k] === true });
    return h('label', { class: 'check' }, boxes[k], h('span', {}, label));
  }));
  const fl = {};
  const aiList = h('div', { class: 'checks' }, AI_FLAGS.map(([k, label]) => {
    fl[k] = h('input', { type: 'checkbox', checked: flags.has(k) });
    return h('label', { class: 'check' }, fl[k], h('span', {}, label));
  }));
  stage.append(h('div', { class: 'stack' }, list, h('h3', {}, 'Cờ AI hỗ trợ'), aiList,
    h('p', { class: 'status warn' }, 'Lưu ý: danh sách này chỉ là nhắc việc. Nó không đảm bảo video đáp ứng chính sách YouTube hay quy định pháp luật; trách nhiệm thuộc về bạn.'),
    h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', onclick: (e) => run(e.currentTarget, async () => {
      const data = { ...cl, ai_flags: AI_FLAGS.map(([k]) => k).filter((k) => fl[k].checked) };
      CHECKS.forEach(([k]) => { data[k] = boxes[k].checked; });
      await saveSection('checklist', data); toast('Đã lưu checklist.');
    }) }, 'Lưu checklist'))));
  navRow(stage, checklistDone(S.job));
}

// 7 Upload riêng tư
function stepUpload(stage) {
  const j = S.job; const y = (S.integ && S.integ.youtube) || {}; const gate = privateGate(j);
  stage.append(...head(7, 'Upload riêng tư', 'Video luôn được tải lên ở chế độ riêng tư (private). Bước này không bao giờ làm video công khai.'));
  const ready = y.configured && y.authorized;
  stage.append(h('p', { class: ready ? 'status ok' : 'status warn' }, ready ? 'YouTube: đã cấu hình và cấp quyền.'
    : `YouTube: ${y.configured ? 'chưa cấp quyền OAuth' : 'chưa cấu hình OAuth'}${y.reason ? ' – ' + y.reason : ''}.`));
  if (!ready) {
    stage.append(h('div', { class: 'status' }, h('strong', {}, 'Làm thủ công trong YouTube Studio'),
      h('ol', {}, ['Mở studio.youtube.com và bấm "Tạo" → "Tải video lên".', 'Chọn file video gốc, dán tiêu đề, mô tả, tags đã soạn ở bước 3.',
        'Tải thumbnail đã duyệt ở bước 4.', 'Đặt chế độ hiển thị là "Riêng tư" rồi lưu.'].map((t) => h('li', {}, t))),
      h('a', { class: 'btn secondary', href: 'https://studio.youtube.com/', target: '_blank', rel: 'noopener noreferrer' }, 'Mở YouTube Studio')));
  }
  if (gate.length) stage.append(h('ul', { class: 'gatelist' }, gate.map((x) => h('li', {}, x))));
  const st = j.upload.state;
  stage.append(h('dl', { class: 'kv', style: 'margin:12px 0' }, h('dt', {}, 'Trạng thái upload'), h('dd', {}, { none: 'chưa upload', in_progress: 'đang upload', done: 'đã xong', failed: 'thất bại' }[st] || st),
    h('dt', {}, 'YouTube ID'), h('dd', {}, j.upload.youtube_video_id || '—'), h('dt', {}, 'Quyền riêng tư'), h('dd', {}, j.upload.privacy || '—'),
    h('dt', {}, 'Lúc'), h('dd', {}, fmtDate(j.upload.uploaded_at))));
  if (j.error && j.error.stage === 'upload') stage.append(h('p', { class: 'status err' }, `Lỗi upload: ${j.error.message}${j.error.retryable ? ' (có thể thử lại)' : ''}`));
  stage.append(h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', disabled: !ready || gate.length > 0 || st === 'done' || st === 'in_progress',
    onclick: (e) => run(e.currentTarget, async () => { adopt(await api(`/api/jobs/${S.job.id}/upload-private`, { method: 'POST', body: {} })); renderAll(); toast('Đã upload riêng tư.'); }) },
  st === 'failed' ? 'Thử upload lại (riêng tư)' : st === 'done' ? 'Đã upload' : 'Upload riêng tư')));
  navRow(stage, st === 'done');
}

// 8 Công khai
function stepPublic(stage) {
  const j = S.job; const gate = publicGate(j); const done = j.public.state === 'done';
  stage.append(...head(8, 'Công khai', 'Bước tách biệt và không thể hoàn tác từ app. Chỉ mở khóa khi mọi điều kiện đã đạt.'));
  if (done) { stage.append(h('p', { class: 'status ok' }, `Video đã công khai lúc ${fmtDate(j.public.confirmed_at)}.`)); navRow(stage); return; }
  if (gate.length) stage.append(h('p', { class: 'status warn' }, 'Đang khóa:'), h('ul', { class: 'gatelist' }, gate.map((x) => h('li', {}, x))));
  stage.append(h('div', { class: 'row', style: 'margin-top:12px' }, h('button', { class: 'btn danger', type: 'button', disabled: gate.length > 0, onclick: openPublicDialog }, 'Công khai video…')));
  navRow(stage);
}
function openPublicDialog() {
  const j = S.job; const d = $('#publicDialog'); d.replaceChildren();
  const input = h('input', { type: 'text', autocomplete: 'off', 'aria-describedby': 'pdHint' });
  const go = h('button', { class: 'btn danger', type: 'button', disabled: true, onclick: (e) => run(e.currentTarget, async () => {
    adopt(await api(`/api/jobs/${j.id}/go-public`, { method: 'POST', body: { confirm_video_id: j.upload.youtube_video_id, confirm_text: input.value.trim() } }));
    d.close(); renderAll(); toast('Đã công khai video.');
  }, false) }, 'Công khai ngay');
  input.addEventListener('input', () => { go.disabled = input.value.trim() !== 'CONG KHAI'; });
  d.append(h('h2', { id: 'pdTitle' }, 'Xác nhận công khai'),
    h('p', {}, 'Bạn sắp chuyển video sau sang công khai trên YouTube:'),
    h('dl', { class: 'kv' }, h('dt', {}, 'Tên'), h('dd', {}, j.content.title || j.source.name), h('dt', {}, 'YouTube ID'), h('dd', {}, j.upload.youtube_video_id)),
    h('label', { class: 'field', style: 'margin-top:12px' }, h('span', {}, 'Gõ CONG KHAI để xác nhận'), input, h('span', { class: 'hint', id: 'pdHint' }, 'Phải đúng chữ hoa, không dấu.')),
    h('div', { class: 'row end', style: 'margin-top:16px' }, h('button', { class: 'btn secondary', type: 'button', onclick: () => d.close() }, 'Hủy'), go));
  d.showModal(); input.focus();
}

// ---------- khởi động ----------
async function init() {
  renderAll();
  const tasks = [
    api('/api/integrations').then((r) => { S.integ = r; renderBanners(); if (S.step === 6) renderStage(); }).catch((e) => { S.integ = { youtube: { configured: false, reason: e.message } }; renderBanners(); }),
    api('/api/jobs').then((r) => { S.jobs = r.jobs || []; renderSide(); }).catch(() => {}),
    loadVideos().catch((e) => showError(e, () => run(null, loadVideos))),
  ];
  let id = null; try { id = localStorage.getItem(LS_JOB); } catch { /* bỏ qua */ }
  if (id) { try { await openJob(id); } catch (e) { if (e.status !== 404) showError(e, () => run(null, () => openJob(id))); } }
  await Promise.allSettled(tasks);
  renderAll();
}
init();
