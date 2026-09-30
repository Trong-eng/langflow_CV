# Đo overhead điều phối Langflow với flow SCRFD

Mục tiêu là đo phần thời gian Langflow còn lại sau khi loại **toàn bộ xử lý SCRFD**, so sánh compilation cache và warm graph registry. Flow vẫn chạy thật qua API production, với input/output `chat`, `stream=false`, đúng ảnh và model đã đóng băng.

```text
langflow_overhead_ms = server_total_ms - scrfd_processing_ms
```

`server_total_ms` dùng đồng hồ monotonic bên trong worker, từ điểm vào ASGI ngoài cùng của ứng dụng cho `/run` đến khi gửi xong response body. Factory `langflow.benchmark_worker_identity:create_benchmark_app` bọc ứng dụng production sau khi đã cấu hình đầy đủ middleware và HTTP telemetry; server thông thường vẫn dùng factory gốc. `scrfd_processing_ms` là độ dài **hợp** các khoảng output method của bốn node SCRFD: đọc/preprocess ảnh, inference, vẽ và lưu ảnh. Khoảng chồng lấn chỉ trừ một lần. Observer có thể ghi cả Chat Input/Output; chúng vẫn thuộc overhead Langflow và không bị trừ.

Upload, download ảnh kết quả, mạng client và background work sau khi gửi response nằm ngoài chỉ số chính. Chat Input/Output, chuẩn bị graph/component, validation, auth, toàn bộ middleware ứng dụng và serialize response vẫn được tính. Đây là overhead wall-clock theo ranh giới đã chọn, không phải CPU time hay latency end-user. `flow_api_ms` và `upload_ms` chỉ là chỉ số phụ.

Campaign sau review dùng `runs/main-v2` và smoke `runs/smoke-v2`. `runs/main` giữ nguyên dữ liệu theo ranh giới middleware cũ để đối chiếu; không gộp mẫu giữa hai ranh giới. Review và script tái hiện nằm ở `runs/main/REVIEW.md`.

## Thiết kế bốn cấu hình

| Arm | Compilation cache | Warm graph registry |
| --- | --- | --- |
| 00 | OFF | OFF |
| 01 | OFF | ON |
| 10 | ON | OFF |
| 11 | ON | ON |

Mỗi arm có **1.000 request đo**, chia thành bốn block × 250 request. Tổng cộng 4.000 mẫu đo và 80 warmup (5/slot × 16 slot). Một slot là một cặp block/arm; runner khởi động worker mới cho từng slot, một process, concurrency 1, không reload. HTTP connection reuse tắt, các cờ khác giữ cố định. Thứ tự cân bằng:

| Block | Vị trí 1 | Vị trí 2 | Vị trí 3 | Vị trí 4 |
| --- | --- | --- | --- | --- |
| 1 | 00 | 01 | 11 | 10 |
| 2 | 01 | 10 | 00 | 11 |
| 3 | 10 | 11 | 01 | 00 |
| 4 | 11 | 00 | 10 | 01 |

Mỗi request upload ảnh, chạy `/api/v1/run/{flow_id}?stream=false`, kiểm tra số mặt trong Chat Output, tải ảnh kết quả và đối chiếu hash pixels với reference. Không dùng debug output, không sửa flow để tránh điều kiện warm, không thay model/input giữa các arm, không retry để bù mẫu lỗi.

### Phạm vi đường warm đã điều chỉnh

Kết quả áp dụng cho **đường warm đã sửa trong checkout này**, không đại diện cho việc chỉ bật cờ trên upstream nguyên bản. Đường warm trước đó từ chối file tweaks, có thể từ chối auto-global và fallback khi migration phát sự kiện lỗi với bốn custom node SCRFD đã lưu. Bản sửa cho phép file tweaks an toàn trên bản graph riêng cho từng request, xác minh auto-global thực tế không đổi binding, và phát lại các sự kiện migration lỗi khi không có sửa kiểu node, đồng thời giữ nguyên namespace của người dùng. Trường hợp migration sửa kiểu, metadata chưa biết hoặc tweaks không được hỗ trợ vẫn đi cold.

Compilation cache lưu artifact biên dịch source; benchmark không thêm cache cho class instance, ONNX runtime session hay kết quả inference. Warm hit không có nghĩa là bỏ qua SCRFD: cả bốn output method vẫn phải chạy và được quan sát trong mỗi mẫu.

## Phân tích offline trước

Mở `benchmark_end_to_end.ipynb`, đặt `RUN_DIR` đến thư mục kết quả. Notebook mặc định `RUN_LIVE = False`: chạy tất cả cell chỉ đọc raw và dựng lại báo cáo; không khởi động worker hay gửi request.

Từ repository root:

```bash
uv run --no-sync python -m benchmark_analyst.benchmark_langflow analyze \
  --output benchmark_analyst/runs/main-v2
```

Ba file raw bắt buộc là `manifest.json`, `requests.jsonl`, `worker_evidence.jsonl`. Nếu có `state.json`, trạng thái phải là `COMPLETE`; `RUNNING`/`INCOMPLETE` luôn làm run INVALID dù đã có đủ request. Lệnh trả exit code 0 khi hợp lệ, 2 khi không hợp lệ. Không cần worker đang chạy hoặc API key để phân tích offline.

Các artifact được ghi lại trong cùng thư mục:

| File | Nội dung |
| --- | --- |
| `analysis.json` | Kết quả kiểm chứng, thống kê arm/block, hiệu ứng hoặc `contrasts: null` |
| `report.md` / `report.html` | Báo cáo tiếng Việt, ranh giới đo và bằng chứng worker |
| `summary.csv` | Mean/p50/p95/p99 của từng metric theo arm và block |
| `charts.png` | Percentile, mean theo block và chuỗi request của overhead |

Raw không bị sửa khi phân tích. Kết quả nhỏ hơn 1.000 request/arm được gắn nhãn **SMOKE**, kể cả khi vượt qua mọi kiểm chứng.

## Chuẩn bị workload và chạy thật

Dùng môi trường Python của repository đã có dependency. Cấu hình và credentials đặt dưới `benchmark_analyst/runs/` đã git-ignore. Các đường dẫn workload tương đối được tính từ thư mục chứa config. Ví dụ cấu trúc config (thay bằng giá trị thật, không thay flow đã lưu):

```json
{
  "flow_id": "UUID-flow-da-luu",
  "input_node_id": "ChatInput-ID",
  "output_node_id": "ChatOutput-ID",
  "scrfd_node_ids": ["ImageLoader-ID", "SCRFD-ID", "Draw-ID", "Save-ID"],
  "image_path": "face_detection_example.jpg",
  "model_path": "/duong-dan/model.onnx",
  "flow_export_path": "scrfd-flow.json",
  "expected_faces": 1,
  "timeout_seconds": 60
}
```

`flow_export_path` phải chứa bản export của flow đang lưu. Runner kiểm tra hash dữ liệu flow và đúng sáu node (Chat Input/Output cộng bốn node xử lý). Credentials file dạng dotenv chỉ cần `LANGFLOW_API_KEY=...`, hoặc dùng biến môi trường tương ứng. `.env` chứa cấu hình server; không đưa secret vào config hay notebook. Các lệnh sau chỉ truyền đường dẫn credentials.

Chọn một port trống. Runner chỉ quản lý process do chính nó tạo; nếu port đã có server, lệnh sẽ dừng. Mỗi `--output` phải là thư mục mới dưới `benchmark_analyst/runs/`; runner không ghi đè hoặc tự resume campaign dang dở.

**1. Chuẩn bị reference**, chạy thật một request và lưu `reference.jpg`, `reference_request.json`, cùng `config.json` có `reference_pixel_sha256`:

```bash
uv run --no-sync python -m benchmark_analyst.benchmark_langflow prepare \
  --config benchmark_analyst/runs/inputs/config.json \
  --output benchmark_analyst/runs/reference \
  --env-file .env \
  --credentials-file benchmark_analyst/runs/inputs/credentials.env \
  --port 7860
```

Kiểm tra reference đúng ảnh/số mặt trước khi tiếp tục. Reference được tính từ pixels RGB cùng kích thước, không chỉ từ bytes JPEG.

**2. Chạy smoke** sau khi đã hoàn tất mọi thay đổi source. 8 request/arm cho đủ bốn block, 2 request/slot; vẫn giữ 5 warmup/slot:

```bash
uv run --no-sync python -m benchmark_analyst.benchmark_langflow run \
  --config benchmark_analyst/runs/reference/config.json \
  --output benchmark_analyst/runs/smoke-v2 \
  --requests-per-arm 8 --blocks 4 --warmups 5 \
  --env-file .env \
  --credentials-file benchmark_analyst/runs/inputs/credentials.env \
  --port 7860
```

Mở `smoke-v2/report.html` và xác nhận `VALID · SMOKE`. Nếu sai timing, output hoặc cache không hit, sửa nguyên nhân và chạy smoke mới. Không dùng smoke để kết luận tốc độ.

**3. Chạy campaign chính** với source/workload giống smoke đã VALID:

```bash
uv run --no-sync python -m benchmark_analyst.benchmark_langflow run \
  --config benchmark_analyst/runs/reference/config.json \
  --output benchmark_analyst/runs/main-v2 \
  --smoke benchmark_analyst/runs/smoke-v2 \
  --requests-per-arm 1000 --blocks 4 --warmups 5 \
  --env-file .env \
  --credentials-file benchmark_analyst/runs/inputs/credentials.env \
  --port 7860
```

Runner tự phân tích sau khi hoàn tất. Source (kể cả dirty/untracked source liên quan), flow, ảnh và model có hashes trong manifest. Không chỉnh code/notebook/reporting giữa smoke và campaign; cập nhật source làm kiểm tra tính nhất quán thất bại. Tránh chạy test/build hoặc tác vụ nặng cùng lúc với phép đo.

## Khi nào kết quả được coi là hợp lệ

- Đủ chính xác số request đo và warmup trong từng slot; không request ID trùng, không lỗi/timeout/output sai.
- Mọi timing hữu hạn và không âm; đủ bốn node SCRFD; intervals nằm trong request; phép hợp và phép trừ khớp. Output pixels khớp reference.
- Manifest đủ bốn block với thứ tự cân bằng, hashes workload và thông tin source.
- Snapshot thực tế trước warmup, sau warmup, sau đo có PID/cờ nhất quán với rows và arm. Counter không bị reset.
- Counter đo = `after_measurement - after_warmup`: warm ON có hit đúng bằng số attempt và cold = 0; warm OFF có cold đúng bằng số attempt và hit = 0. Mỗi row cũng phải ghi đúng warm/cold path.
- Compilation ON có measured hit > 0. OFF có measured hit = 0 và bypass > 0. Bật cờ đơn thuần không phải bằng chứng cache hoạt động.
- `state.json` nếu có phải xác nhận runner hoàn tất kiểm tra tính nhất quán cuối campaign.

Thiếu bất kỳ bằng chứng nào làm run **INVALID**. Báo cáo vẫn giữ số lượng attempt và thống kê có thể đọc được để chẩn đoán, nhưng không tính hiệu ứng tăng tốc và không âm thầm loại mẫu xấu để cứu kết luận.

## Đọc hiệu ứng

Chỉ số chính có mean/p50/p95/p99 theo arm và từng block. Percentile dùng nội suy tuyến tính type 7. Với `μab` là mean overhead của arm `ab`:

- So với baseline: `μ10 - μ00`, `μ01 - μ00`, `μ11 - μ00`.
- Compilation khi warm OFF/ON: `μ10 - μ00`, `μ11 - μ01`.
- Warm khi compilation OFF/ON: `μ01 - μ00`, `μ11 - μ10`.
- Tương tác: `μ11 - μ10 - μ01 + μ00`.

Δ âm nghĩa là overhead thấp hơn; mức giảm phần trăm dương nghĩa là nhanh hơn. Báo cáo không giả định kết quả có lợi cho cache. So sánh dùng mean cân bằng giữa block; p95/p99 mô tả phần đuôi và có thể khác xu hướng mean.

Chỉ có bốn block độc lập, nên không coi 4.000 request là 4.000 lần lặp điều kiện máy độc lập. Bản báo cáo này không đưa CI hoặc p-value. Kết luận chỉ áp dụng cho workload, phiên bản source, cấu hình và máy đã ghi nhận; không suy rộng thành tốc độ toàn hệ thống hay trải nghiệm end-user.

## Kiểm tra mã báo cáo

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --no-sync python -m pytest \
  -c /dev/null -p no:cacheprovider benchmark_analyst/tests/test_reporting.py -q
```

Tests tạo dữ liệu tổng hợp nhỏ để kiểm tra công thức, artifact và việc chặn kết luận khi thiếu/sai bằng chứng. Chúng không khởi động Langflow hay chạy model SCRFD.
