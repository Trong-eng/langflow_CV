# SCRFD: overhead điều phối Langflow

Yêu cầu được chốt trong chat ngày 2026-09-28: chạy đúng flow SCRFD đã lưu qua API Langflow, so sánh compilation cache × warm graph registry; 1.000 request đo mỗi cấu hình 00/01/10/11. Người dùng đã cho phép triển khai end to end.

**Cập nhật ranh giới đo:** chỉ tối ưu overhead ở tầng Langflow. Loại thời gian upload/tải ảnh và toàn bộ xử lý SCRFD (preprocess, inference, vẽ/lưu ảnh). Mỗi lần đo vẫn chạy thật các bước đó, không mock hoặc lấy lại kết quả cũ.

Chỉ số chính: `langflow_overhead_ms = server_total_ms - scrfd_processing_ms`. `server_total_ms` đo bằng đồng hồ monotonic trong worker từ lúc request `/run` vào ASGI ngoài cùng của ứng dụng đến khi gửi xong body response. `scrfd_processing_ms` là độ dài hợp các khoảng thực thi output method của bốn node SCRFD; không cộng hai lần khoảng chồng nhau. Đây là overhead wall-clock theo ranh giới này, không phải CPU time, không phải latency end-user. Chat Input/Output, chuẩn bị graph/component, validation, auth, toàn bộ middleware ứng dụng và serialize response vẫn thuộc phần đo. Network client và background work sau khi gửi response không được tính; factory benchmark bọc toàn bộ ứng dụng production, kể cả HTTP telemetry. Upload và tải ảnh có thể thực hiện để kiểm tra tính đúng, ngoài vùng đo.

Giữ nguyên bytes flow/input/model. Request production: upload ảnh, `/api/v1/run/{id}?stream=false`, input chat, output chat (không ép debug để thu traces). Kiểm tra output có số mặt đúng và ảnh hợp lệ; đối chiếu hash pixels ảnh với reference chuẩn bị trước main. Không sửa flow để né gate warm. Mở rộng đường warm một cách bảo thủ cho ChatInput.files và chỉ cho phép auto-global có lookup xác nhận không thay đổi graph; trường hợp khác vẫn cold.

Mỗi worker độc lập một process, không reload, concurrency 1. Bốn block theo thứ tự cân bằng, 250 request/arm/block; restart mỗi slot, 5 warmup/slot tách khỏi 4.000 mẫu. Các cờ khác cố định, HTTP connection reuse tắt. Snapshot worker chứng minh cờ thực tế, compilation hit/bypass, warm hit/cold. Không coi bật cờ là bằng chứng hit.

Raw request JSONL giữ mọi lỗi; không retry để bù. Kết quả không đạt đủ số lượng, thiếu node/timing, output sai, sai flow/model/source hoặc warm ON không hit không được ghi VALID. Báo cáo mean/p50/p95/p99 theo arm và block; hiệu ứng bật riêng/kết hợp và tương tác, không mặc định nhanh hơn. CI theo 4 block chỉ mang tính thăm dò.

Code cũ được dọn khỏi benchmark_analyst; bản sao hồi phục nằm ngoài repository. Credentials và inputs cũ chỉ giữ dưới runs/ bị git-ignore. Không commit secret, không thay flow/database/model hoặc các thay đổi không liên quan trong checkout hiện tại.

Kiểm tra thực tế cần thêm tương thích warm cho lỗi migration của custom component không rewrite type: replay cùng event emitter theo user, kiểm tra migration hiện tại trên bản sao; có rewrite hoặc không rõ provenance vẫn cold. Vì vậy kết quả áp dụng cho đường warm đã bổ sung tương thích trong repository này. Không diễn giải compilation cache thành cache class, ONNX session hoặc kết quả inference.

Sau review: chuyển observer sang factory ASGI riêng bọc ứng dụng production; giữ factory server thông thường nguyên trạng. Lỗi quyền file khi dựng template trung lập về user trên cache miss chỉ kích hoạt cold fallback; không bỏ qua kiểm tra file trên graph của request. Campaign mới dùng runs/main-v2; runs/main là dữ liệu ranh giới cũ.
