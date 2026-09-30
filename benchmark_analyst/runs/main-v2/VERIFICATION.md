# Xác nhận sửa hai lỗi review

Campaign main-v2 hoàn tất **VALID** lúc 13:57:28 ngày 2026-09-29 (Asia/Ho_Chi_Minh).

## Hai lỗi đã sửa

1. **Ranh giới đo**: factory `langflow.benchmark_worker_identity:create_benchmark_app` bọc toàn bộ ứng dụng production sau khi đã cấu hình middleware và HTTP telemetry. Runner dùng factory này; `langflow.main:create_app` và server thông thường giữ nguyên hành vi. Đồng hồ kết thúc tại final ASGI response body, trước background work sau response.
2. **Warm cache miss với file thuộc user**: chỉ bắt `LocalFileAccessError` trong bước dựng template trung lập về user và trả về cold fallback. Cold/request-local graph vẫn kiểm tra namespace với identity và file tweaks của người gọi; ảnh ngoài quyền không được phép chạy.

## Test và kiểm tra

- Red: ba test mới thất bại trước sửa (factory mới chưa có; hai biến thể cache miss lỗi StorageNamespaceError).
- Green: cả ba test mới qua sau sửa.
- Bộ benchmark + warm graph: **122 passed**, 9 warnings.
- Bộ reporting chạy lại sau cập nhật hiển thị ranh giới: **39 passed** (đã nằm trong nhóm kiểm tra nêu trên, không cộng thành 161 test độc lập).
- Test ranh giới chạy qua middleware stack thực của `create_app`, với đồng hồ giả xác định: 7 ms middleware + 11 ms endpoint + 3 ms final send = 21 ms. Không dựa vào sleep/timing có thể dao động.
- Test cache miss dùng Graph, process_tweaks, WarmGraphRegistry thật; chỉ thay DB fetch bằng flow in-memory. Kiểm tra cả request đầu và lần sau, cả upload đúng namespace lẫn upload trái phép.
- Ruff các file liên quan và git diff --check: đạt.
- Notebook kết quả chạy đủ 9 code cell offline, không output lỗi. Biểu đồ đã kiểm tra trực quan.
- Source sau xuất notebook vẫn khớp fingerprint trong manifest.
- Không chạy lại toàn bộ monorepo/unit suite hay mypy. Hai lỗi catalog LFX tồn tại từ trước được ghi ở ../main/VERIFICATION.md; không tuyên bố toàn repository xanh.

## Bằng chứng campaign

- Smoke-v2 VALID: 32 mẫu đo + 80 warm-up.
- Main-v2 VALID: 4.000 mẫu đo, đúng 1.000/arm; thêm 80 warm-up. Tất cả 4.080 attempt thành công, đủ timing và output khớp reference.
- 16 worker riêng, bốn block cân bằng, concurrency 1.
- 01/11: mỗi arm đúng 1.000 warm hit, không cold fallback trong mẫu đo.
- 10/11: mỗi arm 6.000 compilation hit, không bypass; 00/01 có 6.000 bypass/arm.
- Đã dọn 4.080 upload input, tất cả cleanup thành công.
- Độc lập tính lại mean/p95/p99 từ requests.jsonl; số liệu khớp báo cáo.

| Arm (compilation, warm) | Mean ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: |
| 00 | 63.528 | 86.800 | 156.122 |
| 01 | 63.828 | 77.953 | 193.858 |
| 10 | 54.065 | 70.277 | 423.070 |
| 11 | 57.437 | 68.610 | 370.681 |

Arm 10 có mean thấp nhất, giảm 14,90% so với 00. Arm 11 có p95 thấp nhất. p99 của 10 và 11 vẫn cao hơn 00; không kết luận một cấu hình thắng trên mọi chỉ số. Warm hit là thật, nhưng warm không làm mean tốt hơn compilation-only trong campaign này. Chỉ có bốn block độc lập, không đưa CI/p-value và không suy rộng tốc độ toàn hệ thống.

Chỉ số chính loại upload/download và hợp thời gian các output method SCRFD; vẫn là wall-clock có instrumentation, không phải CPU time hoặc latency end-user.

## Dữ liệu cũ và lượt bị ngắt

- `../main`: giữ nguyên campaign với ranh giới middleware cũ và báo cáo review. Không dùng để thay thế kết quả main-v2, không so delta giữa hai campaign như tác động thuần của bản sửa vì điều kiện máy/thời điểm khác nhau.
- `../main-v2-interrupted`: lượt trước dừng ở 2.500 mẫu; đã đánh dấu INCOMPLETE sau khi xác nhận không còn process. Không resume và không gộp mẫu.
- Main-v2 hiện tại chạy mới từ đầu bằng tiến trình độc lập với terminal. Không đổi source kể từ smoke-v2.

Bàn giao: report.html, report.md, ket_qua_benchmark.ipynb, analysis.json, summary.csv, charts.png và toàn bộ raw dưới thư mục này. Không tạo commit.
