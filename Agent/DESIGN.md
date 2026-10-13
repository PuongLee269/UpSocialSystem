# DESIGN – YouTube Agent MVP (hợp đồng giữa các module)

Nguồn yêu cầu: `APP_PROMPT.md`. Stack giữ nguyên: Python stdlib `http.server` + Pillow/cv2 tùy chọn, không thêm framework. Frontend là static files, không build step, không CDN/telemetry.

## Cấu trúc file (mỗi worker CHỈ sửa file mình sở hữu)

```
app.py                  # [Integrator] entry + router mỏng, giữ endpoint cũ (/api/videos,/api/health,/api/frames,/api/file,/api/prepare,/api/job/<id>/draft|json)
ytagent/__init__.py
ytagent/paths.py        # [A] safe_join(root, rel), resolve_in_roots(); chặn path traversal/symlink thoát root
ytagent/jobs.py         # [A] JobStore: job JSON versioned, state machine, idempotency, migration job cũ (review.json)
ytagent/validation.py   # [A] validate_tags/title/description theo giới hạn YouTube
ytagent/gates.py        # [A] can_upload_private(job), can_go_public(job) -> (ok, reasons[])
ytagent/thumbnail.py    # [B] render từ frame thật, template, safe area, version history
ytagent/youtube.py      # [C] OAuth (loopback, token ở thư mục secrets/ ngoài outbox), resumable upload private, set thumbnail, public
ytagent/routes.py       # [Integrator] gắn API mới vào Handler
static/index.html, static/app.js, static/style.css   # [D] wizard 8 bước
tests/test_*.py         # mỗi worker tự viết test cho module mình (unittest, stdlib)
secrets/                # gitignored; client_secret.json + token.json; KHÔNG bao giờ phục vụ qua /api/file
README.md               # [Integrator] cập nhật cuối
```

## Job schema (jobs/<job_id>/job.json trong OUTBOX; schema_version=2)

```json
{
 "schema_version": 2, "id": "uuid", "created": "iso", "updated": "iso", "revision": 1,
 "status": "source_selected|content_ready|vidiq_reviewed|thumbnail_approved|private_uploaded|public",
 "error": null,   // {"stage":"upload","message":"...","at":"iso","retryable":true}
 "source": {"video_id":"sha1[:16]","name":"","duration":0,"rel_path":"<root_index>/<rel>"},
 "frame": {"selected_index":null,"time_sec":null,"file":"frames/frame-03.jpg","crop169":null},
 "content": {"title":"","description":"","tags":[], "tags_total_chars":0},
 "vidiq": {"reviewed":false,"reviewed_at":null,"lookup_date":null,
           "keywords":[{"keyword":"","score":null,"score_for":"keyword","volume":null,"competition":null}],
           "thumbnail_score":{"value":null,"score_for":"thumbnail","file":null,"version":null},
           "notes":"","confirmed_by_user":false},
 "thumbnail": {"versions":[{"v":1,"file":"thumbs/v1.jpg","template":"","text":"","created":"iso","note":""}],
               "approved_version":null, "template":{}},
 "checklist": {"rights":false,"policy":false,"metadata":false,"disclosure":false,"kids":false,"ai_flags":[]},
 "upload": {"state":"none|in_progress|done|failed","idempotency_key":"","youtube_video_id":null,"privacy":null,"uploaded_at":null},
 "public": {"confirmed_at":null,"state":"locked|done"},
 "platforms": {"youtube":{}, "tiktok":null, "instagram":null, "facebook":null},   // mở rộng sau
 "history": [{"at":"iso","event":"","detail":""}]
}
```
- Ghi atomic (tmp + os.replace), lock theo job, `revision` tăng mỗi lần ghi; client gửi `expected_revision` → 409 nếu lệch.
- Job cũ (`outbox/<uuid>/review.json`) KHÔNG xóa/ghi đè; `JobStore.import_legacy(id)` đọc và tạo job v2 mới.
- Trạng thái chỉ tiến theo gate; lỗi lưu trong `error`, không đổi status lùi.

## Giới hạn / validation (validation.py)
title 1–100 ký tự, không `<` `>`; description ≤5000 byte UTF-8; mỗi tag ≤100 ký tự, tổng ≤500 ký tự (tag có dấu cách tính thêm 2 ký tự nháy), không trùng (không phân biệt hoa thường), không rỗng.

## Gates (gates.py) – trả `(ok: bool, reasons: list[str])`
- `can_upload_private`: title/description/tags hợp lệ + frame đã chọn + vidiq.confirmed_by_user + thumbnail.approved_version + checklist đủ 5 mục true. Upload luôn `privacyStatus=private`.
- `can_go_public`: status==private_uploaded + upload.state==done + youtube_video_id + toàn bộ gate trên vẫn đạt + request mang `confirm_video_id == youtube_video_id` và `confirm_text == "CONG KHAI"`.
- Không có code path nào đặt public từ endpoint upload.

## API mới (JSON, mọi POST chỉ chấp nhận client 127.0.0.1/::1, body ≤ 256KB)
```
GET  /api/jobs                          -> {jobs:[{id,status,title,updated}]}
POST /api/jobs {video_id}               -> 201 job (idempotent: trả job đang mở của video nếu có)
GET  /api/jobs/<id>                     -> job
PUT  /api/jobs/<id>/<section> {expected_revision, data}   section ∈ frame|content|vidiq|checklist
GET  /api/jobs/<id>/contact-sheet       -> {frames:[{index,time_sec,url}]}  (8–12 frame, timestamp)
POST /api/jobs/<id>/thumbnail/render {frame_index, template, text, safe_area?} -> {version}
POST /api/jobs/<id>/thumbnail/approve {version}
POST /api/jobs/<id>/upload-private      -> job (409 nếu gate fail / đã upload: idempotent)
POST /api/jobs/<id>/go-public {confirm_video_id, confirm_text}
GET  /api/vidiq/brief?job=<id>          -> {text, url:"https://app.vidiq.com/"}  (chỉ tạo chuỗi để copy)
GET  /api/integrations                  -> {youtube:{configured,authorized,reason}, vidiq:{connected:false,mode:"manual"}, ollama:{...}}
GET  /api/templates, PUT /api/templates/<name>   (thumbnail templates lưu OUTBOX/templates.json)
```
Lỗi: `{error:"thông điệp tiếng Việt dễ hiểu", code:"gate_failed|conflict|not_found|invalid|not_configured", reasons:[...]}`.
`/api/file` chỉ phục vụ trong OUTBOX và từ chối `secrets/`, `token`, `client_secret`.

## Frontend (static) – trạng thái trung thực
Stepper 8 bước: Nguồn → Frame → Nội dung → Thumbnail → VidIQ → Kiểm tra → Upload riêng tư → Công khai. Mỗi bước hiện: chưa làm / đang / đạt / lỗi. Banner "VidIQ: thủ công – chưa kết nối API", "YouTube: chưa cấu hình OAuth" lấy từ `/api/integrations`. Nút Công khai disabled tới khi `gates` ok, rồi mở dialog xác nhận nêu tên video + ID. Tiếng Việt, mobile-first responsive, focus ring, label, alt text, không tràn ngang.

## Tiêu chí kiểm thử (unittest)
tags/title validation; path traversal (`..`, absolute, symlink); job state transitions + revision conflict + legacy import; gate private/public (kể cả: upload endpoint không bao giờ public; thiếu confirm → 403/409); idempotent upload (gọi 2 lần chỉ 1 lần gọi API – dùng fake client).
