# Agent xuất bản YouTube (chạy local)

Ứng dụng local giúp chuẩn bị một video để đăng YouTube qua wizard 8 bước:
Nguồn → Frame → Nội dung → Thumbnail → VidIQ → Kiểm tra → Upload riêng tư → Công khai.
Thiết kế chi tiết và hợp đồng API: `DESIGN.md`.

## Chạy local

```powershell
cd D:\ANHCUABUM\Youtube\Agent
python app.py
```

Mở `http://127.0.0.1:8765`. Đổi cổng bằng biến `AGENT_PORT`. Server chỉ lắng nghe `127.0.0.1` (đổi bằng `AGENT_HOST`, không khuyến khích) và chỉ nhận thao tác ghi (POST/PUT) từ máy local.
Yêu cầu: Python 3.10+, `Pillow` và `opencv-python` (trích frame, vẽ thumbnail). Không cần build frontend, không dùng CDN.

Dữ liệu nằm trong `outbox/` (job ở `outbox/jobs/<id>/job.json`, frame ở `frames/`, thumbnail ở `thumbs/`, template ở `outbox/templates.json`). Gói duyệt cũ `outbox/<uuid>/review.json` vẫn được giữ nguyên.

## Kết nối YouTube (OAuth) an toàn

Nếu chưa cấu hình, app báo "chưa cấu hình" và hướng dẫn đăng thủ công; app **không bao giờ giả lập upload thành công**.

1. Vào Google Cloud Console, tạo project, bật **YouTube Data API v3**.
2. Tạo OAuth client loại **Desktop app**, tải JSON về và lưu thành `secrets\client_secret.json`.
3. Chạy luồng đăng nhập (loopback, mở trình duyệt trên máy này):
   ```powershell
   python -c "from ytagent import youtube; youtube.authorize('secrets')"
   ```
   Token lưu ở `secrets\token.json`.
4. Tải lại trang: banner phải báo YouTube đã cấu hình và cấp quyền.

Lưu ý bảo mật:
- `secrets\` nằm ngoài `outbox/`; `/api/file` chỉ phục vụ trong `outbox/` và từ chối mọi đường dẫn chứa `secrets`, `token`, `client_secret`.
- Không commit `secrets\` vào git (thêm vào `.gitignore`), không gửi file này cho ai.
- Trên Windows, quyền file của token chỉ được siết ở mức best-effort (không như `chmod 600` trên Linux). Hãy đặt dự án trong thư mục chỉ tài khoản của bạn đọc được và dùng tài khoản Google riêng cho kênh.
- Thu hồi quyền bất cứ lúc nào ở myaccount.google.com/permissions rồi xóa `secrets\token.json`.
- Dự án OAuth chưa qua kiểm định có thể khiến video upload bị YouTube khóa ở chế độ riêng tư; hãy kiểm tra trong YouTube Studio.

## VidIQ (thủ công)

VidIQ **chưa được kết nối API**. Bước VidIQ chỉ tạo "brief" (chuỗi văn bản từ job) để bạn copy sang app.vidiq.com, tự tra từ khóa/điểm tiêu đề, ghi lại số liệu thật rồi tick xác nhận. App không gọi mạng tới VidIQ và không tạo số liệu giả. Phải xác nhận mới qua được cổng upload.

## Quy trình thử nghiệm đề xuất

1. `python -m unittest discover -s tests` phải xanh (không gọi mạng ngoài; YouTube dùng client giả).
2. Chạy app, chọn một video ngắn, đi hết các bước đến "Kiểm tra".
3. Chưa cấu hình OAuth: bước 7 phải báo cần làm thủ công trong YouTube Studio.
4. Đã cấu hình: upload một video ngắn bằng tài khoản thử, xác nhận trong Studio rằng nó là **Riêng tư** và thumbnail đúng. Bấm upload lần hai phải trả lại kết quả cũ, không tạo video mới.
5. Chỉ thử bước Công khai trên video thử. Cần gõ đúng `CONG KHAI`; thao tác này không hoàn tác từ app.

## Cổng kiểm soát

- Upload riêng tư cần: tiêu đề/mô tả/tags hợp lệ, đã chọn frame, đã xác nhận VidIQ, đã duyệt thumbnail, checklist 5 mục đầy đủ. Upload luôn đặt `privacyStatus=private`.
- Công khai là endpoint riêng (`/go-public`), cần video đã upload riêng tư, mọi cổng trên vẫn đạt, `confirm_video_id` khớp và `confirm_text` = `CONG KHAI`.
- Lỗi upload được ghi vào `job.error`, không đổi trạng thái; có thể thử lại (idempotency key giữ nguyên, video đã tạo không bị tạo lại).

## Giới hạn hiện tại

- Chỉ YouTube; TikTok/Instagram/Facebook chưa có uploader (chỉ chừa chỗ trong schema `platforms`).
- Checklist chính sách/bản quyền chỉ là nhắc việc, không đảm bảo tuân thủ hay clear bản quyền.
- Không gen ảnh AI; thumbnail dựng từ frame thật của video (cắt 16:9, chữ, vùng an toàn).
- Không lên lịch đăng; không có tài khoản/đa người dùng; chỉ dùng trên máy local.
- Upload dùng một luồng đồng bộ trong request; video rất lớn có thể làm trình duyệt chờ lâu (tiến độ chưa hiển thị).
- Nguồn video lấy từ `video_roots` trong `config.json` (hiện chưa quét trực tiếp Google Drive cloud).
- Endpoint cũ (`/api/prepare`, `/api/job/<id>/draft`, `/api/frames`...) vẫn tồn tại để tương thích nhưng wizard mới không dùng.
