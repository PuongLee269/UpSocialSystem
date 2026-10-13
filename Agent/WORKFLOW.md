# Quy trình MVP đăng video đa nền tảng

Quy trình đã được duyệt sau lần thử đăng video YouTube. Mục tiêu là giảm vòng lặp và tránh gửi lại dữ liệu không cần thiết cho AI.

## Luồng cho mỗi video

1. **Chọn nguồn một lần**: chọn video trong thư mục Drive đồng bộ/local. Lưu tên file, thời lượng và ghi chú nội dung vào một phiếu duy nhất.
2. **Tạo contact sheet**: trích một số frame đại diện từ video để chọn cảnh phù hợp. Ưu tiên khung hình có mặt/nhân vật rõ, đúng nội dung, biểu cảm tốt và bố cục còn chỗ cho chữ.
3. **VidIQ một lượt cho YouTube**: cung cấp tiêu đề hiện tại, mô tả ngắn, nội dung video và contact sheet/frame đã chọn. Yêu cầu một kết quả gộp gồm tiêu đề, mô tả, bộ tags phù hợp trong giới hạn YouTube, keyword có dữ liệu volume/competition/score nếu VidIQ có, cùng giờ đăng nếu khả dụng. Không điền số liệu ước lượng.
4. **Dựng một thumbnail theo template đã duyệt**: giữ ảnh/video frame thật và nhân vật thật; thống nhất font, màu, kích thước chữ, vùng safe area và cách đặt chữ phía sau nhân vật. Chỉ tạo một bản nháp đầu tiên.
5. **Duyệt thumbnail với người dùng**: chỉ sửa khi có góp ý cụ thể. Nếu yêu cầu điểm VidIQ cao, chấm đúng file thumbnail cuối. Nếu dưới ngưỡng đã thống nhất, dùng góp ý có tác động lớn nhất để sửa tối đa một vòng rồi chấm lại. Không dùng ảnh AI bị cảnh báo sai lệch nội dung.
6. **Kiểm tra chính sách và metadata**: rà soát tiêu đề, mô tả, tags, thumbnail, bản quyền/quyền sử dụng và checklist chính sách. AI chỉ hỗ trợ gắn cờ, không thay thế duyệt của người dùng.
7. **Upload ở trạng thái riêng tư/bản nháp**: lưu thumbnail và metadata, kiểm tra preview trong Studio. Với nền tảng không có draft riêng tư, giữ gói nội dung local cho đến lúc duyệt.
8. **Công khai chỉ sau khi người dùng xác nhận**: chuyển trạng thái và kiểm tra Studio xác nhận đã lưu/public.

## Quy tắc tiết kiệm token và thao tác

- Một lần phân tích VidIQ tổng hợp thay vì hỏi riêng title, tags, mô tả và keywords.
- Không đưa lại toàn bộ video/brief trong các lượt follow-up; chỉ gửi delta hoặc file ảnh cuối.
- Dùng một contact sheet để chọn frame; không tạo/chấm hàng loạt phương án chưa cần thiết.
- Chốt template thumbnail dùng lại giữa các video; thay ảnh và câu chữ theo nội dung.
- Tối đa một vòng sửa thumbnail sau góp ý VidIQ, trừ khi người dùng yêu cầu thêm.
- Không gọi VidIQ chấm concept bằng mô tả văn bản; chấm ảnh cụ thể cuối cùng.
- Tách rõ duyệt sáng tạo, lưu bản nháp và công khai. Không công khai tự động.
- Lưu kết quả VidIQ, tags, thumbnail và trạng thái theo job để không phân tích lại.

## Những gì MVP đã chứng minh

- Có thể tải video local lên YouTube Studio, lưu metadata/thẻ/thumbnail và giữ private.
- VidIQ đã được dùng trực tiếp cho keyword và chấm thumbnail; điểm ảnh phụ thuộc file ảnh thực tế.
- Thumbnail cuối cần được chấm riêng nếu yêu cầu ngưỡng điểm; điểm của ảnh cũ không áp dụng cho bản thiết kế mới.
- Thay đổi trạng thái Công khai là bước độc lập và đã yêu cầu xác nhận người dùng.

## Ngưỡng VidIQ

Ngưỡng cần cấu hình theo yêu cầu từng video/kênh; không tự mặc định một con số chung. Chỉ ghi điểm do VidIQ đo trên file ảnh cụ thể.

