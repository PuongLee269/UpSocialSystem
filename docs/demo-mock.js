// BẢN DEMO: mô phỏng toàn bộ API local bằng state trong bộ nhớ. Không gọi mạng, không YouTube/VidIQ thật.
(function () {
  'use strict';
  const realFetch = window.fetch.bind(window);
  const LIM = { title: 100, descBytes: 5000, tag: 100, tagsTotal: 500 };
  const CHECK_KEYS = ['rights', 'policy', 'metadata', 'disclosure', 'kids'];
  const RANK = ['source_selected', 'content_ready', 'vidiq_reviewed', 'thumbnail_approved', 'private_uploaded', 'public'];
  const now = () => new Date().toISOString();
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rid = () => Math.random().toString(16).slice(2, 10);

  // ---------- dữ liệu giả ----------
  const VIDEOS = [
    ['Du lich Da Lat - ngay 1.mp4', 'D:/Videos/Export', 1840, 312.4], ['Review tai nghe khong day.mp4', 'D:/Videos/Export', 920, 487.0],
    ['Nau an cuoi tuan - bun bo.mp4', 'D:/Videos/Bep', 1310, 604.8], ['Vlog chay bo buoi sang.mp4', 'D:/Videos/Vlog', 650, 215.2],
    ['Huong dan lap rap PC 2026.mp4', 'D:/Videos/Tech', 2480, 1180.0], ['Phong van khach moi tap 12.mov', 'D:/Videos/Podcast', 3320, 2410.5],
  ].map(([name, folder, mb, duration], i) => ({ id: 'demo' + (1000 + i) + 'abcdef0', name, folder, ext: '.' + name.split('.').pop(), size: mb * 1048576,
    modified: '2026-0' + (3 + (i % 5)) + '-1' + i, width: i % 3 === 0 ? 1080 : 1920, height: i % 3 === 0 ? 1920 : 1080, duration, warning: i === 3 ? 'Thông số đọc bằng ước lượng (demo)' : null }));
  const jobs = new Map();
  const templates = { 'Mac dinh': { text: 'XEM NGAY', template: 'Mac dinh' }, 'Tieu de lon': { text: 'BAT NGO', template: 'Tieu de lon' } };

  // ---------- validation & gate (theo DESIGN.md) ----------
  const u8 = (s) => new TextEncoder().encode(s).length;
  const tagChars = (tags) => tags.reduce((n, t) => n + t.length + (/\s/.test(t) ? 2 : 0), 0);
  function validateContent(c) {
    const e = [], title = c.title || '', tags = c.tags || [], seen = new Set();
    if (!title.trim()) e.push('Cần có tiêu đề.');
    if (title.length > LIM.title) e.push(`Tiêu đề tối đa ${LIM.title} ký tự.`);
    if (/[<>]/.test(title)) e.push('Tiêu đề không được chứa < hoặc >.');
    if (u8(c.description || '') > LIM.descBytes) e.push(`Mô tả tối đa ${LIM.descBytes} byte.`);
    for (const t of tags) {
      if (!String(t).trim()) e.push('Có tag rỗng.');
      if (t.length > LIM.tag) e.push(`Tag "${t.slice(0, 20)}…" dài quá ${LIM.tag} ký tự.`);
      if (seen.has(t.toLowerCase())) e.push(`Tag trùng: ${t}`);
      seen.add(t.toLowerCase());
    }
    if (tagChars(tags) > LIM.tagsTotal) e.push(`Tổng tags vượt ${LIM.tagsTotal} ký tự.`);
    return e;
  }
  function canUploadPrivate(j) {
    const r = [];
    if (validateContent(j.content).length) r.push('Nội dung (title/mô tả/tags) chưa hợp lệ.');
    if (j.frame.selected_index == null) r.push('Chưa chọn frame.');
    if (!j.vidiq.confirmed_by_user) r.push('Chưa xác nhận bước VidIQ.');
    if (j.thumbnail.approved_version == null) r.push('Chưa duyệt thumbnail.');
    if (!CHECK_KEYS.every((k) => j.checklist[k] === true)) r.push('Checklist trước upload chưa đủ 5 mục.');
    return r;
  }
  function canGoPublic(j, body) {
    const r = canUploadPrivate(j);
    if (j.status !== 'private_uploaded') r.push('Job chưa ở trạng thái đã upload riêng tư.');
    if (j.upload.state !== 'done' || !j.upload.youtube_video_id) r.push('Chưa upload riêng tư thành công.');
    if (!body || body.confirm_video_id !== j.upload.youtube_video_id) r.push('Mã video xác nhận không khớp.');
    if (!body || body.confirm_text !== 'CONG KHAI') r.push('Cần gõ đúng CONG KHAI.');
    return r;
  }

  // ---------- job ----------
  function newJob(v) {
    return { schema_version: 2, id: 'demo-' + rid() + rid(), created: now(), updated: now(), revision: 1, status: 'source_selected', error: null,
      source: { video_id: v.id, name: v.name, duration: v.duration, rel_path: '0/' + v.name },
      frame: { selected_index: null, time_sec: null, file: null, crop169: null },
      content: { title: '', description: '', tags: [], tags_total_chars: 0 },
      vidiq: { reviewed: false, reviewed_at: null, lookup_date: null, keywords: [], thumbnail_score: { value: null, score_for: 'thumbnail', file: null, version: null }, notes: '', confirmed_by_user: false },
      thumbnail: { versions: [], approved_version: null, template: {} },
      checklist: { rights: false, policy: false, metadata: false, disclosure: false, kids: false, ai_flags: [] },
      upload: { state: 'none', idempotency_key: '', youtube_video_id: null, privacy: null, uploaded_at: null },
      public: { confirmed_at: null, state: 'locked' }, platforms: { youtube: {}, tiktok: null, instagram: null, facebook: null },
      history: [{ at: now(), event: 'created', detail: 'DEMO' }] };
  }
  function touch(j, event, detail) {
    j.revision += 1; j.updated = now(); j.history.push({ at: now(), event, detail: detail || '' });
    let st = 'source_selected';
    if (j.content.title && !validateContent(j.content).length) st = 'content_ready';
    if (st === 'content_ready' && j.vidiq.confirmed_by_user) st = 'vidiq_reviewed';
    if (st === 'vidiq_reviewed' && j.thumbnail.approved_version != null) st = 'thumbnail_approved';
    const keep = RANK.indexOf(j.status) >= RANK.indexOf('private_uploaded'); // chỉ tiến, không lùi
    if (!keep && RANK.indexOf(st) > RANK.indexOf(j.status)) j.status = st;
  }
  const reply = (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const err = (status, code, error, reasons) => reply(status, { error, code, reasons: reasons || [] });

  // ---------- ảnh canvas ----------
  function hue(n) { return (n * 47 + 200) % 360; }
  function drawBase(ctx, w, h, hu, label, sub) {
    const g = ctx.createLinearGradient(0, 0, w, h);
    g.addColorStop(0, `hsl(${hu},65%,38%)`); g.addColorStop(1, `hsl(${(hu + 60) % 360},70%,22%)`);
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = 'rgba(255,255,255,.12)'; ctx.beginPath(); ctx.arc(w * 0.72, h * 0.4, h * 0.28, 0, 7); ctx.fill();
    ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.font = `bold ${Math.round(h * 0.12)}px sans-serif`; ctx.fillText(label, w / 2, h * 0.52);
    if (sub) { ctx.font = `${Math.round(h * 0.06)}px sans-serif`; ctx.fillText(sub, w / 2, h * 0.62); }
  }
  function canvasUrl(w, h, fn) { const c = document.createElement('canvas'); c.width = w; c.height = h; fn(c.getContext('2d'), w, h); return c.toDataURL('image/jpeg', 0.82); }
  const frameCache = new Map();
  function frameUrl(index, time) {
    if (!frameCache.has(index)) frameCache.set(index, canvasUrl(360, 640, (x, w, h) => { drawBase(x, w, h, hue(index), `Frame #${index}`, `${time}s · DEMO`); }));
    return frameCache.get(index);
  }
  function thumbUrl(j, ver) {
    return canvasUrl(1280, 720, (x, w, h) => {
      drawBase(x, w, h, hue(ver.frame_index || 0), ver.text || '', `${ver.template || 'không template'} · v${ver.v}`);
      x.fillStyle = 'rgba(0,0,0,.55)'; x.fillRect(0, h - 70, w, 70); x.fillStyle = '#fff'; x.font = 'bold 34px sans-serif'; x.textAlign = 'left';
      x.fillText('DEMO – ảnh giả sinh bằng canvas', 24, h - 24);
    });
  }
  const FILE_RE = /^\/api\/file\?path=(.+)$/;
  function resolveFileSrc(src) {
    let m; try { const u = new URL(src, location.href); m = (u.pathname.replace(/^.*?(\/api\/file)/, '$1') + u.search).match(FILE_RE); } catch { return null; }
    if (!m) return null;
    const p = decodeURIComponent(m[1]).split('/'); // jobs/<id>/thumbs/vN.jpg
    const j = jobs.get(p[1]); const mm = (p[3] || '').match(/^v(\d+)\.jpg$/);
    const ver = j && mm && j.thumbnail.versions.find((x) => x.v === Number(mm[1]));
    return ver ? thumbUrl(j, ver) : null;
  }
  const isFileSrc = (s) => typeof s === 'string' && s.indexOf('/api/file') !== -1;
  const origSet = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (this instanceof HTMLImageElement && name === 'src' && isFileSrc(value)) { const d = resolveFileSrc(value); if (d) return origSet.call(this, name, d); }
    return origSet.call(this, name, value);
  };

  // ---------- router ----------
  async function route(method, path, query, body) {
    await sleep(120 + Math.random() * 200);
    let m;
    if (path === '/api/health') return reply(200, { ok: true, demo: true });
    if (path === '/api/videos') return reply(200, { videos: VIDEOS });
    if (path === '/api/integrations') return reply(200, { youtube: { configured: true, authorized: true, reason: 'DEMO – không kết nối thật' }, vidiq: { connected: false, mode: 'manual' }, ollama: { available: false } });
    if (path === '/api/templates' && method === 'GET') return reply(200, { templates: templates });
    if ((m = path.match(/^\/api\/templates\/(.+)$/)) && method === 'PUT') {
      const name = decodeURIComponent(m[1]); if (!name.trim()) return err(422, 'invalid', 'Tên template không hợp lệ.');
      templates[name] = clone((body && body.data) || {}); return reply(200, { name, template: templates[name] });
    }
    if (path === '/api/jobs' && method === 'GET') {
      return reply(200, { jobs: [...jobs.values()].sort((a, b) => b.updated.localeCompare(a.updated)).map((j) => ({ id: j.id, status: j.status, title: j.content.title, updated: j.updated })) });
    }
    if (path === '/api/jobs' && method === 'POST') {
      const v = VIDEOS.find((x) => x.id === (body && body.video_id)); if (!v) return err(404, 'not_found', 'Không tìm thấy video nguồn.');
      const open = [...jobs.values()].find((j) => j.source.video_id === v.id && j.status !== 'public');
      if (open) return reply(200, open);
      const j = newJob(v); jobs.set(j.id, j); return reply(201, clone(j));
    }
    if (path === '/api/vidiq/brief') {
      const j = jobs.get(query.get('job')); if (!j) return err(404, 'not_found', 'Không tìm thấy job.');
      return reply(200, { url: 'https://app.vidiq.com/', text: `[DEMO] Brief tra cứu VidIQ cho video "${j.content.title || j.source.name}".\nTừ khóa gợi ý: ${(j.content.tags || []).slice(0, 5).join(', ') || '(chưa có tags)'}.\nHãy tự tra cứu và nhập đúng số liệu bạn thấy; không có thì để trống.` });
    }
    if ((m = path.match(/^\/api\/jobs\/([^/]+)(?:\/(.+))?$/))) return jobRoute(method, decodeURIComponent(m[1]), m[2] || '', body);
    return err(404, 'not_found', 'Không tìm thấy API (bản demo).');
  }

  async function jobRoute(method, id, sub, body) {
    const j = jobs.get(id); if (!j) return err(404, 'not_found', 'Không tìm thấy job (bản demo mất dữ liệu khi tải lại trang).');
    body = body || {};
    if (sub === '' && method === 'GET') return reply(200, clone(j));
    if (sub === 'contact-sheet' && method === 'GET') {
      const frames = Array.from({ length: 10 }, (_, i) => { const t = Math.round((j.source.duration * (i + 0.5)) / 10 * 10) / 10; return { index: i + 1, time_sec: t, file: `frames/frame-${String(i + 1).padStart(2, '0')}.jpg`, url: frameUrl(i + 1, t) }; });
      return reply(200, { frames });
    }
    let m;
    if (method === 'PUT' && (m = sub.match(/^(frame|content|vidiq|checklist)$/))) {
      if (body.expected_revision !== j.revision) return err(409, 'conflict', `Phiên bản không khớp (client ${body.expected_revision}, máy chủ ${j.revision}).`);
      const d = body.data || {}, sec = m[1];
      if (sec === 'content') {
        const errs = validateContent(d); if (errs.length) return err(422, 'invalid', 'Nội dung chưa hợp lệ.', errs);
        j.content = { title: d.title, description: d.description || '', tags: d.tags || [], tags_total_chars: tagChars(d.tags || []) };
      } else if (sec === 'frame') {
        if (!Number.isInteger(d.selected_index) || d.selected_index < 1 || d.selected_index > 10) return err(422, 'invalid', 'Frame không hợp lệ.');
        j.frame = { selected_index: d.selected_index, time_sec: d.time_sec, file: d.file, crop169: d.crop169 || null };
      } else if (sec === 'vidiq') {
        j.vidiq = { ...j.vidiq, ...clone(d) };
        if (j.vidiq.confirmed_by_user && !j.vidiq.reviewed_at) j.vidiq.reviewed_at = now();
      } else {
        j.checklist = { ...j.checklist, ...clone(d) };
      }
      touch(j, 'update_' + sec); return reply(200, clone(j));
    }
    if (sub === 'thumbnail/render' && method === 'POST') {
      if (j.frame.selected_index == null) return err(422, 'invalid', 'Chưa chọn frame.');
      const v = { v: j.thumbnail.versions.length + 1, file: `thumbs/v${j.thumbnail.versions.length + 1}.jpg`, template: body.template || '', text: String(body.text || '').slice(0, 60), created: now(), note: body.note || '', frame_index: body.frame_index };
      j.thumbnail.versions.push(v); touch(j, 'thumbnail_render', 'v' + v.v); return reply(201, { version: v });
    }
    if (sub === 'thumbnail/approve' && method === 'POST') {
      if (!j.thumbnail.versions.some((x) => x.v === body.version)) return err(404, 'not_found', 'Không có phiên bản thumbnail này.');
      j.thumbnail.approved_version = body.version; touch(j, 'thumbnail_approve', 'v' + body.version); return reply(200, clone(j));
    }
    if (sub === 'upload-private' && method === 'POST') {
      if (j.upload.state === 'done') return reply(200, clone(j)); // idempotent
      const reasons = canUploadPrivate(j); if (reasons.length) return err(409, 'gate_failed', 'Chưa đủ điều kiện upload riêng tư.', reasons);
      await sleep(1200);
      j.upload = { state: 'done', idempotency_key: 'demo-' + rid(), youtube_video_id: 'DEMO-' + rid().slice(0, 6), privacy: 'private (DEMO – không upload thật)', uploaded_at: now() };
      j.status = 'private_uploaded'; j.error = null; touch(j, 'upload_private', 'DEMO'); return reply(200, clone(j));
    }
    if (sub === 'go-public' && method === 'POST') {
      if (j.public.state === 'done') return reply(200, clone(j));
      const reasons = canGoPublic(j, body);
      if (reasons.length) return err(body.confirm_text !== 'CONG KHAI' || !body.confirm_video_id ? 403 : 409, 'gate_failed', 'Chưa đủ điều kiện công khai.', reasons);
      j.public = { confirmed_at: now(), state: 'done' }; j.status = 'public'; touch(j, 'go_public', 'DEMO – không công khai thật'); return reply(200, clone(j));
    }
    return err(404, 'not_found', 'Không tìm thấy API (bản demo).');
  }

  window.fetch = async function (input, init) {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const idx = url.pathname.indexOf('/api/');
    if (idx === -1) return realFetch(input, init);
    const method = ((init && init.method) || 'GET').toUpperCase();
    let body = null; try { body = init && init.body ? JSON.parse(init.body) : null; } catch { /* bỏ qua */ }
    return route(method, url.pathname.slice(idx), url.searchParams, body);
  };

  // ---------- banner cố định ----------
  function banner() {
    const b = document.createElement('div'); b.id = 'demoBanner'; b.setAttribute('role', 'note');
    b.textContent = 'BẢN DEMO – dữ liệu giả, không kết nối YouTube/VidIQ, không upload thật';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:#b45309;color:#fff;font:600 14px/1.3 sans-serif;text-align:center;padding:8px 12px;box-shadow:0 2px 6px rgba(0,0,0,.3)';
    document.body.prepend(b); document.body.style.paddingTop = (b.offsetHeight + 4) + 'px';
    window.addEventListener('resize', () => { document.body.style.paddingTop = (b.offsetHeight + 4) + 'px'; });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', banner); else banner();
})();
