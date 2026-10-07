# Reviewer: readiness cho lượt full pipeline tiếp theo

Ngày đánh giá: 2026-10-04. Phạm vi: source hiện tại, bằng chứng kiểm chứng đã có, preflight đầu vào/môi trường và đường chạy tiếp theo. Lượt review này không khởi động Langflow, chạy inference hoặc thu campaign mới.

## Kết luận

**GO có điều kiện để bắt đầu pipeline mới từ smoke. Campaign dài chỉ được chạy sau khi smoke mới cùng source/profile/fixture đạt `overall_valid=true`.**

Không phát hiện blocker mới trong phần code benchmark đã remediation. Có một vấn đề ở cách khởi chạy: hai helper v3 là script lịch sử, không thể chạy lại nguyên trạng. Worker cần dùng output và nhật ký mới trước khi bắt đầu. Chưa có bằng chứng live cho source sau remediation; 332 tests và reanalysis không thay thế smoke thật.

## Bằng chứng kiểm tra

| Hạng mục | Kết quả |
| --- | --- |
| Source hiện tại | `b5a5f3383ecb84b8c0d87b25c607a403872d1d2c5acdc9b8e97ea6a673505f06`, khớp receipt kiểm chứng cuối. |
| Tests | Receipt trước đó ghi 332 passed, 8 warnings, exit 0; lượt này xác minh hash log khớp receipt, không chạy lại suite. |
| Feedback R1–R9 | Đã đóng theo review ledger và source kiểm chứng không thay đổi. |
| Frozen fixture | `LocalFixture.open(.../fixture-v3)` xác minh toàn bộ content hash thành công; SQLite khoảng 487,99 MiB. |
| Workload | Flow trong DB khớp export; đúng sáu node được cấu hình; model binding hợp lệ. Hash flow/ảnh/model khớp primary v3. |
| Output reference | Pixel reference và số face kỳ vọng trong config khớp manifest primary v3. Output thực tế vẫn phải được kiểm chứng bằng smoke. |
| Credentials | Có file credentials, API key được cấu hình và encryption key của fixture. Chưa kiểm tra live authentication trong lượt review này. |
| Runner dependencies | Cả năm package khớp exact pins trong `benchmark_analyst/requirements.txt`. |
| Runtime packages | ONNX Runtime 1.23.2, NumPy 2.4.6, Uvicorn 0.51.0, SQLAlchemy 2.0.51 có trong môi trường. Startup/inference thật thuộc smoke. |
| Cổng 7867 | `ensure_port_free(7867)` bind rồi đóng thành công; không khởi động server. |
| Namespace mới | Các thư mục `smoke-v4`, `diagnostic-smoke-v4`, `diagnostic-v4`, `main-v4`, `main-v4-repeat`, `implementation-v4` chưa tồn tại khi kiểm tra. |

Máy có 16 GiB RAM, khoảng 3,62 GiB available và 35,40 GiB disk trống tại lần đọc. CPU tức thời khoảng 10,7% trên 8 logical CPUs. Swap đang được sử dụng khoảng 6,17 GiB; con số tồn đọng này không chứng minh máy đang swap liên tục hoặc tự nó làm run INVALID. Cần giữ máy ổn định, tránh workload nặng/test/build đồng thời; quan sát tình trạng bộ nhớ trong smoke. RSS lớn nhất tại checkpoint của hai primary cũ khoảng 1,376/1,309 GiB, **không phải peak RSS** và không bảo đảm nhu cầu của lượt mới.

## R10 — [P2] Helper lịch sử có thể ghi đè nhật ký trước khi gặp output đã tồn tại

- `benchmark_analyst/runs/implementation-v3/run_live.py:13` ghi `preflight.json`; dòng 30 ghi `orchestration.json` trước khi gọi stage đầu tiên. Các stage tại dòng 22–27 hardcode output v3 đã tồn tại.
- `benchmark_analyst/runs/implementation-v3/run_primary.py` có cùng vấn đề với `primary_preflight.json` / `primary_orchestration.json`, và chỉ điều phối primary smoke/main/repeat, không gồm diagnostic suite.
- Core CLI có gate từ chối output đã tồn tại, nên không ghi đè raw campaign theo đường này. Tuy nhiên, nhật ký điều phối lịch sử đã có thể bị thay trước khi CLI từ chối.
- Yêu cầu worker: tạo helper/nhật ký riêng trong `runs/implementation-v4` hoặc dùng CLI tuần tự với các output mới. Kiểm tra tất cả output và ledger chưa tồn tại **trước lần ghi đầu tiên**. Giữ nguyên helper và dữ liệu v3.
- Trạng thái: **OPEN cho bước chuẩn bị khởi chạy; không phải lỗi trong core collector/analyzer.** Lượt reviewer này chỉ ghi nhận, chưa sửa helper hoặc chạy pipeline.

## Smoke cũ không cấp phép cho campaign mới

Đã gọi trực tiếp `check_smoke` với source hiện tại và các thuộc tính còn lại giữ như manifest smoke cũ:

- Primary `smoke-v3-reviewed`: source `69d151c5…`; bị từ chối với `source changed since smoke; freeze source and rerun smoke`.
- Diagnostic `diagnostic-smoke-v3/D1`: source `0f62d35e…`; bị từ chối cùng lý do.

Đây là hành vi đúng của gate. Không đổi hash trong manifest cũ để vượt gate. Có thể tái sử dụng baseline `fixture-v3` đã xác minh; không cần tạo lại fixture chỉ vì source benchmark thay đổi.

## Trình tự giao worker

Hoàn tất cấu hình điều phối mới, chốt source rồi chạy tuần tự; mỗi stage phải exit 0 và có overall validity hợp lệ trước khi sang stage kế tiếp.

| Thứ tự | Output dưới `benchmark_analyst/runs/` | Số measured requests | Gate |
| --- | --- | ---: | --- |
| 1 | `smoke-v4` | 32 | Bốn arm × 8; RAM checkpoints bật, diagnostic phases/drains của suite tắt. |
| 2 | `diagnostic-smoke-v4` | 64 | D0–D3 × hai arm × 8; tất cả child và suite hợp lệ. |
| 3 | `diagnostic-v4` | 8.000 | Dùng diagnostic smoke mới khớp từng variant. |
| 4 | `main-v4` | 4.000 | Dùng primary smoke mới; 1.000/arm, 4 blocks, 16 workers tuần tự. |
| 5 | `main-v4-repeat` | 4.000 | Cùng source/fixture/profile; output riêng và báo cáo riêng. |

Tổng 16.096 measured requests, không cộng warmup. Primary giữ 5 warmup/slot; diagnostic D3 dùng 20, các variant khác dùng 5. Primary và repeat mỗi lượt phải có 64 RAM checkpoints hợp lệ. Giữ cơ chế kiểm tra source/workload giữa các slot và stage; khi lỗi, dừng và giữ bằng chứng, không retry bù để tạo campaign tưởng như đầy đủ.

Smoke phải xác minh startup/authentication, output pixel/face count, cache readiness, actual SQLite clone, effective profile, mandatory RSS và worker cleanup. Đây là phần còn thiếu để cho phép campaign dài trên source hiện tại.

Sau đo, kiểm tra raw integrity và xuất HTML/CSV/PNG; hai primary report phải tách riêng, diagnostic ghi rõ observer mode và không trộn vào primary statistics. Không chỉnh source trong lúc chạy để sửa báo cáo; thay source thì cần smoke mới tương ứng.

## Giới hạn kết luận giữ nguyên

- Đủ điều kiện thu lại benchmark không đồng nghĩa đã chứng minh nguyên nhân arm 11 thua arm 10. GC overlap và phase attribution vẫn phải được đánh giá cùng observer effect.
- Task D tối ưu runtime và targeted probe nhẹ hơn vẫn deferred theo handoff. Chúng không là điều kiện chặn việc thu latency/RAM theo phạm vi đã chốt.
- RAM là whole-worker RSS tại checkpoints; USS optional và có thể unavailable. Payload cache không phải heap attribution; peak RAM chưa được đo.
- Browser rendering toàn trang chưa được kiểm chứng ở lượt trước; static artifacts/links và ảnh biểu đồ là phạm vi QA đã có.

Nguồn: [review/remediation ledger](2026-10-04-benchmark-tradeoffs-review-and-remediation.md), [worker verification receipt](../../../benchmark_analyst/runs/implementation-v3/WORKER_FEEDBACK_RECHECK.json), [kế hoạch đã chốt](../plans/2026-10-02-benchmark-tradeoffs-and-warm-diagnosis.md).

## Worker: đóng điều kiện khởi chạy theo yêu cầu tiếp theo

R10 **CLOSED cho v4**: helper mới `runs/implementation-v4/run_live.py` kiểm tra cả năm output, ba JSON ledger và năm stage logs trước lần ghi đầu tiên; log mở exclusive, không tự resume/retry. Regression RED 2 failures → GREEN 3/3. Reviewer độc lập `review_v4_orchestration` xác nhận không có blocker khởi chạy. Helper v3 và raw lịch sử giữ nguyên; 102 unique historical files được xác minh trước đo.

Source execution vẫn là `b5a5f338…`; helper và kiểm tra của lượt này nằm trong ignored runs nên không đổi fingerprint đã qua 332 tests. Worker được người dùng yêu cầu chạy live đầy đủ; trạng thái thực tế và kết quả từng stage ghi riêng trong `runs/implementation-v4/orchestration.json`. Smoke mới vẫn là gate bắt buộc trước campaign dài; closure R10 không có nghĩa smoke/campaign đã chạy xong.

## Worker: full live pipeline và acceptance đã hoàn thành

V4 dừng đúng gate tại D1 vì real-GC reentry tạo duplicate event ID. Hai collector defects được tái hiện và sửa với RED→GREEN regressions; xem [live GC review](2026-10-04-benchmark-live-gc-review-and-remediation.md). Giữ nguyên v4 INVALID; v5 là whole new pipeline, cùng fixture/workload, frozen source mới `cd289b72c133fc493527b312ee16cba2bd9e1976f4f5ddcb51e282f3a323a1e4`, 334 benchmark tests pass.

**2026-10-04: 5/5 stages v5 COMPLETE/VALID, driver exit 0**, gồm smoke, diagnostic smoke, diagnostic D0–D3 full (8.000 measured), main (4.000) và repeat (4.000). Tổng 16.096 measured requests, 800 warmups, 448 RSS checkpoints; six fresh smoke gates pass. Final read-only verification kiểm tra source/profile/fixture/workload, raw/artifact hashes, 210 historical files và required HTML/CSV/PNG. Không reanalysis hoặc raw mutation.

[Final receipt](../../../benchmark_analyst/runs/implementation-v5/FINAL_VERIFICATION.json) và [bàn giao kết quả](../../../benchmark_analyst/runs/implementation-v5/FINDINGS.md). Đủ điều kiện chạy đã được thực thi và nghiệm thu; không còn stage benchmark được yêu cầu đang chờ. Task tối ưu runtime để `11` thắng và đo heap/peak riêng chưa thuộc kết luận VALID của campaign này.
