# Prompt thiết kế app YouTube Agent MVP

Bạn là senior product designer và full-stack engineer. Hãy phát triển tiếp ứng dụng local hiện có trong thư mục `Agent` của repository GitHub hiện tại. Trước khi sửa, đọc `AGENTS.md` nếu có, `Agent/WORKFLOW.md`, `Agent/README.md`, kiểm tra cấu trúc repo và `Agent/app.py`. Giữ tương thích với dữ liệu/job hiện có; không xóa hoặc ghi đè video, credential, config người dùng.

## Mục tiêu sản phẩm

Tạo một giao diện web local, tối ưu desktop và dùng được trên điện thoại, giúp chuẩn bị một video để đăng YouTube trước (MVP), với khả năng mở rộng TikTok, Instagram và Facebook. Luồng cốt lõi: chọn video local/Drive sync → chọn frame → chuẩn bị nội dung → tra cứu/chấm qua VidIQ trong phiên đăng nhập người dùng → làm thumbnail từ frame thật → duyệt → tải lên YouTube ở chế độ riêng tư → công khai chỉ sau xác nhận rõ ràng.

App phải giảm thao tác lặp và token bằng cách lưu một hồ sơ/job cho mỗi video: metadata, kết quả VidIQ, keywords/tags, frame đã chọn, thumbnail, checklist và trạng thái. Không gửi lại nội dung đã có cho AI; follow-up chỉ truyền phần thay đổi.

## Giao diện

Thiết kế gọn, trực quan, responsive, tiếng Việt, trạng thái từng bước nhìn thấy rõ. Tạo wizard hoặc stepper:

1. **Nguồn video**: quét/chọn file từ các root cấu hình; tìm kiếm; hiển thị tên, thời lượng, đường dẫn và preview.
2. **Chọn frame**: contact sheet có timestamp; chọn frame chủ đạo; preview crop 16:9; ưu tiên có nhân vật/mặt rõ và đúng cảnh. Không tự thay mặt hoặc bịa cảnh.
3. **Nội dung YouTube**: title, description, tags; cho phép sửa tay; bộ tags đầy đủ theo giới hạn YouTube; ghi nguồn/điểm keyword VidIQ và ngày tra cứu. Không tạo hay hiển thị điểm giả.
4. **Thumbnail**: preview lớn ở kích thước desktop và thumbnail nhỏ giả lập màn hình điện thoại; dùng frame thật, chữ tiếng Việt render rõ, safe area; hỗ trợ prompt/brief và quản lý file ảnh. Template có thể lưu lại và tái sử dụng. Hiển thị phiên bản và lịch sử chỉnh sửa.
5. **VidIQ gate**: vì tích hợp API chính thức có thể không sẵn có, mặc định dùng người dùng thao tác trên VidIQ đã đăng nhập. App mở VidIQ thuận tiện, cung cấp nút copy brief và các trường nhập score/keyword/tag notes sau khi người dùng tra cứu. Chỉ đánh dấu hoàn tất khi người dùng nhập xác nhận và lưu. Không tự động hóa đăng nhập, không cào dữ liệu trái phép, không giả vờ có kết nối.
6. **Kiểm tra trước upload**: checklist nội dung/bản quyền/chính sách/metadata; AI chỉ gắn cờ hỗ trợ, không cam kết video chắc chắn tuân thủ.
7. **Upload riêng tư**: dùng YouTube Data API chính thức nếu repo có OAuth hợp lệ hoặc triển khai tích hợp an toàn; ưu tiên upload private/unlisted draft. Không lưu client secret/token trong frontend, log, repo hoặc JSON công khai. Nếu credentials chưa sẵn sàng thì hiển thị rõ bước manual Studio, không giả lập upload.
8. **Công khai**: nút riêng biệt, mặc định khóa đến khi tất cả checklist/metadata/VidIQ gate đạt. Trước khi chuyển video sang Public, hiện xác nhận nêu đúng video và hành động. Không tự công khai theo schedule trong MVP.

## Nguyên tắc triển khai

- Tái sử dụng framework, backend, endpoints và quy ước repo hiện có; không thay toàn bộ stack nếu không cần.
- Giữ app local-first; thư mục output, source roots và config tiếp tục lấy từ `config.json`.
- Không upload video/frame/thumbnail lên dịch vụ AI bên ngoài nếu chưa có hành động rõ ràng của người dùng. Ghi nhãn rõ khi có truyền dữ liệu ra ngoài.
- VidIQ score chỉ ghi khi được người dùng đọc từ VidIQ hoặc lấy qua integration được cho phép; ghi rõ score là cho keyword hay thumbnail, không lẫn lộn.
- Lưu job JSON bền vững, versioned, có thể tiếp tục sau khi reload app. Có trạng thái `source_selected`, `content_ready`, `vidiq_reviewed`, `thumbnail_approved`, `private_uploaded`, `public` và lỗi tương ứng.
- Thêm xử lý lỗi dễ hiểu, trạng thái loading, khả năng retry an toàn và idempotency để tránh upload trùng.
- Kiểm tra đường dẫn nguồn/output để ngăn path traversal; không cho phép truy cập file tùy ý ngoài các root đã cấu hình.
- Accessibility: keyboard navigation, label đầy đủ, tương phản, focus states, alt text.
- Đừng thêm telemetry hoặc analytics bên ngoài.

## Tiêu chí nghiệm thu

- Người dùng hoàn tất được luồng từ video local đến bản nháp riêng tư; bước VidIQ và thumbnail có trạng thái rõ và được lưu theo job.
- Reload trang không làm mất job, tags, điểm/ghi chú VidIQ hoặc frame/thumbnail đã chọn.
- Công khai không thể vô tình xảy ra từ nút upload draft; cần bước xác nhận riêng.
- Mọi trạng thái thể hiện trung thực: nếu chưa kết nối VidIQ/API/YouTube thì báo chưa kết nối, không bịa thành công.
- Dùng được trên desktop lẫn viewport điện thoại; nội dung/form không tràn ngang.
- Có README hướng dẫn chạy local, cấu hình OAuth an toàn (nếu triển khai), quy trình thử nghiệm và giới hạn hiện tại.
- Tạo hoặc cập nhật các kiểm tra phù hợp repo cho trạng thái job, validation tags/path, và gate riêng tư/công khai; chạy các kiểm tra liên quan và báo kết quả.

## Phạm vi MVP

Ưu tiên YouTube end-to-end trước. Chỉ tạo data model/abstraction để mở rộng các nền tảng khác; chưa tích hợp đăng đa nền tảng. Không thay đổi title/description/tags hiện có trên video thật, không upload hoặc công khai video thật trong lúc phát triển. Làm trên branch riêng, giữ diff nhỏ, trình bày file thay đổi, cách chạy, kiểm tra đã chạy và hạn chế còn lại trong pull request.
