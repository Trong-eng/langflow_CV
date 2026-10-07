# Kế hoạch bổ sung trade-off RAM và điều tra arm 11 so với 10

> For agentic workers: triển khai từng task bằng superpowers:executing-plans hoặc superpowers:subagent-driven-development; điều tra theo superpowers:systematic-debugging, kiểm chứng trước khi kết luận. Tài liệu này là kế hoạch, không xác nhận mã đã được sửa hoặc campaign mới đã chạy.

**Goal:** làm báo cáo SCRFD giải thích được lợi ích latency, chi phí RAM và nguyên nhân warm + compilation có thể chậm hơn compilation đơn lẻ; sửa các thiếu sót đo lường đã chứng minh, tối ưu runtime chỉ sau khi đo được bottleneck.

**Architecture:** giữ phép đo ASGI production và workload đóng băng. Bổ sung collector RAM ngoài vùng timing, observer chẩn đoán có thể bật riêng, môi trường đo có provenance và analyzer offline. Tách dữ liệu diagnostic khỏi campaign dùng để kết luận hiệu năng.

**Tech Stack:** Python 3.13 hiện có, psutil 7.2.2, requests, uvicorn, pytest, matplotlib, SQLite fixture và notebook offline.

**Spec:** benchmark_analyst/DESIGN.md, kế hoạch RAM đã thống nhất trong chat, và yêu cầu mở rộng điều tra ngày 2026-10-02. Phạm vi được chọn là flow SCRFD hiện tại, chưa mở rộng cardinality, concurrency hoặc nhiều worker. Người dùng chưa có ngân sách RAM/SLA.

## Trạng thái triển khai ngày 2026-10-03

Task 0/A/B/C/E đã triển khai phần benchmark và hoàn tất các dataset bắt buộc: diagnostic suite 8.000 mẫu, hai primary campaign riêng 4.000 mẫu/lượt và128 RSS checkpoints tổng primary. 251 benchmark tests pass; focused cache correctness35 tests. Raw main/main-v2 và diagnostic trước correction không đổi hash. Các checkbox bên dưới giữ làm spec gốc; trạng thái thực thi và bằng chứng xem [findings](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/implementation-v3/FINDINGS.md), [verification](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/implementation-v3/FINAL_VERIFICATION.json) và [ledger](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/implementation-v3/PROGRESS.md).

Task D vẫn deferred. Không chạy thêm targeted nhẹ hơn: observer contrast không ổn định theo block, setup detail hiện có thêm hooks; reviewer khuyến nghị giao E và giữ nguyên nhân chi tiết unresolved. Đây là deviation được ghi rõ, không phải item đã hoàn thành. DB/native trace-finalization/telemetry/background timing chưa hỗ trợ giữ capability unavailable; USS AccessDenied là optional unavailable, không là zero.

Kết quả mới:11 chậm hơn10 trên cả mean/median/p95/p99 overhead ở hai primary; median penalty khoảng3,9ms, phần lớn trước method đầu tiên. RAM11−10 sau idle đổi dấu giữa block/campaign; payload bytes tách riêng whole-worker RSS. Hai GC context-parent false positives được sửa bằng analyzer semantics v2 với raw giữ nguyên; source collector và analyzer ghi riêng.

Review/remediation tiếp ngày 2026-10-04 đã đóng 9 findings về evidence gates, RAM/phase/suite validation, smoke contract và worker cleanup. Whole benchmark **332 tests pass**, Ruff 24 Python files sạch; reanalysis 14 dataset và 2 suite vẫn VALID, các thống kê đo và raw hashes giữ nguyên. [Review ledger](/Users/tranquangtrong/Desktop/langflow_CV/docs/superpowers/reviews/2026-10-04-benchmark-tradeoffs-review-and-remediation.md) ghi findings trước sửa, regression evidence và closure. [Verification mới](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/implementation-v3/REVIEW_V4_VERIFICATION.json) tách current source khỏi collector source của dữ liệu cũ; FINAL_VERIFICATION ngày trước giữ nguyên như bằng chứng lịch sử. Task D và targeted probe nhẹ hơn vẫn deferred như trạng thái đã ghi, không được đánh dấu hoàn tất bởi đợt review này.

## Handoff sau review

Phạm vi triển khai đợt này là sửa benchmark, đo RAM, bổ sung HTML và điều tra nguyên nhân. Task D là danh sách hướng tối ưu runtime cho một đợt tiếp theo; worker đợt này bàn giao bằng chứng và đề xuất, không tự đổi semantics graph/cache/auth/tracing production để làm arm11 thắng. Public read-only cache accounting và hook chỉ bật trong benchmark được phép trong Task B.

Thứ tự thực hiện: **Task 0 → A/B và phần hạ tầng C → smoke → C diagnostic → E campaign/report**. Phần dựng report ở A có thể dùng fixture tổng hợp theo contract Task 0 trước khi có collector. Hoàn tất E dù diagnostic chưa giải thích được nguyên nhân hoặc không cần tối ưu runtime. Mỗi task kết thúc bằng focused tests và review diff; chỉ chạy campaign khi toàn bộ execution source đã cố định.

Baseline hợp lệ cho một tối ưu runtime sau này là campaign ở E dưới profile mới. main-v2 chỉ dùng mô tả lịch sử; thay DB fixture/logging/profile khiến nó không phải đối chứng before cho một runtime fix. Đợt này không có runtime fix nên không tuyên bố đã cải thiện production latency.

## Ràng buộc và trọng tâm review

- Giữ flow/input/model, API v1, output chat, stream=false, kiểm tra pixels và số mặt; không chỉnh workload để một arm có kết quả đẹp hơn.
- Giữ primary metric: server_total_ms trừ hợp bốn output-method SCRFD. Không trừ thêm GC, DB, telemetry hoặc observer bằng một hằng số ước tính.
- Không sửa raw lịch sử, bỏ outlier, retry bù lỗi hoặc gộp main với main-v2; hai campaign có ranh giới timing khác nhau.
- RAM là toàn serving worker; payload bytes không phải heap bytes. USS unavailable không được chuyển thành zero. Peak RAM chưa nằm trong v1.
- Diagnostic phải bảo toàn auth, file namespace, globals, migration events, request isolation và phục hồi hook khi lỗi/cancellation. Không ghi source, credentials, giá trị globals hoặc SQL bind parameters.
- Môi trường clone không được trỏ ghi vào DB/storage đang dùng của người dùng. Snapshot phải nhất quán, các bản sao giữ dưới runs/ và không được commit.
- Source fingerprint phải ổn định khi tạo report; process identity, profile, checkpoint hoặc phase bị thiếu phải chặn kết luận tương ứng.
- Nested/overlapping durations không được cộng thẳng. Background task ngoài response vẫn nằm ngoài primary metric nhưng có thể ảnh hưởng request kế tiếp.

## 1. Bằng chứng hiện có

### 1.1. Arm 11 không thua trên mọi tiêu chí

Số liệu main-v2, đơn vị ms:

| Chỉ số | 10 | 11 | 11 - 10 |
| --- | ---: | ---: | ---: |
| Mean overhead | 54.065 | 57.437 | +3.372 |
| p50 overhead | 44.675 | 48.979 | +4.303 |
| p95 overhead | 70.277 | 68.610 | -1.668 |
| p99 overhead | 423.070 | 370.681 | -52.390 |
| Mean server total | 87.907 | 90.508 | +2.601 |
| Mean SCRFD | 33.842 | 33.071 | -0.771 |

11 chậm hơn ở mean/p50 và tốt hơn ở p95/p99 trong campaign này. Không có quy luật rằng thêm một cache bắt buộc làm mọi percentile tốt hơn.

### 1.2. Phần chênh lệch nằm chủ yếu trước component đầu tiên

Raw hiện có đủ sáu span, không overlap. Phân rã overhead theo ranh giới đã đo:

| Segment | Mean 10 | Mean 11 | 11 - 10 |
| --- | ---: | ---: | ---: |
| ASGI entry đến output method đầu tiên | 36.564 | 40.400 | +3.836 |
| Khoảng giữa các output method | 7.122 | 6.779 | -0.343 |
| Sau output method cuối đến response body cuối | 4.803 | 4.801 | -0.001 |
| Chat Input + Chat Output bodies | 5.577 | 5.457 | -0.120 |

Median đoạn trước method cũng tăng khoảng 4.049 ms. Đây chưa phải thời gian graph-copy: đoạn đó còn gồm middleware, auth, full-flow fetch, setup, constructor và dispatch wait.

Mean 11 - 10 theo bốn block: -1.236, +3.070, +12.798, -1.145 ms. Block 3 đóng góp khoảng 94.9% chênh lệch mean tổng hợp; tuy nhiên median 11 cao hơn 10 ở cả bốn block. Trong main cũ, median warm cũng cao hơn khoảng 4.087 ms; chỉ dùng điều này làm bằng chứng lặp lại, không gộp mẫu với main-v2.

### 1.3. Warm hit có thật nhưng phạm vi tiết kiệm nhỏ hơn giả định

- Mọi measured request của 11 là warm hit; có 6.000 compilation hit/arm, không cold fallback hay measured compilation miss. Không sửa warmup với lý do cache chưa hit.
- API v1 vẫn load đầy đủ Flow.data và tạo FlowRead trước khi quyết định warm. Metadata resolver tránh load full row hiện nằm ở v2, chưa được benchmark này dùng.
- Frozen flow có sáu field rỗng đủ điều kiện kiểm tra auto-globals. Warm dựng candidate bằng deepcopy, áp tweaks, lookup defaults và kiểm tra có thay đổi binding không.
- Migration dry-run của frozen flow cho bốn custom-node errors, không rewrite. Warm phải recheck migration trên một bản copy rồi replay events; cold cũng migrate và emit. Không mô tả nhầm warm chạy hai migration pass.
- copy_for_run deepcopy frontend payload rồi add_nodes_and_edges; process_flow deepcopy lần nữa và dựng lại vertices/edges/params. Cả 10 và 11 đều tạo lại sáu component class/instance.
- Warm có ít nhất ba full-payload deepcopy bổ sung: globals candidate, migration recheck và copy payload để chạy. process_flow copy là chi phí chung.
- Compilation hit vẫn restore AST, chuẩn bị imports/module definitions, exec class và kiểm tra annotations. Nó không có nghĩa bỏ mọi công việc compilation/runtime setup.
- Node ONNX có dictionary _session_cache ở class, nhưng class được tạo mới mỗi request. Hai cache đang so sánh không giữ/reuse ONNX session giữa request; không giả định native memory được thu hồi ngay.

Đây là cơ chế thêm công việc đã xác nhận bằng mã, chưa phải phép quy toàn bộ 3.372 ms cho deepcopy.

### 1.4. Những thiếu sót benchmark đã xác nhận

- Worker vẫn có 1.281 local access log/request lines mỗi slot dù runner dùng --no-access-log. Logger của ứng dụng thiết lập lại handler; cần xác minh effective logger sau startup.
- Outbound Scarf telemetry hoạt động trong lúc đo; số event hoàn tất khác giữa slot. Native tracing=true không mô tả đầy đủ outbound telemetry và logging.
- Native trace finalization được schedule thành async task; không có bảo đảm tất cả flush hoàn tất trước response. Pending task có thể tranh DB/CPU với request sau.
- Restart chỉ reset process/cache/temp, không reset DB/messages/jobs/traces/storage hoặc OS state. Chat Input/Output đều lưu message; không có Memory node nên chưa có bằng chứng quét history O(n).
- Manifest thiếu effective settings/versions, DB/storage identity, GC, CPU/RAM, thread/provider, logging, telemetry/exporters và context tải máy.
- source_identity chọn mọi benchmark_analyst/* tracked file, hiện bao gồm 12 artifact main-v2 mới được track. Toàn bộ file source ghi trong manifest cũ vẫn hash-match; khác fingerprint hiện tại không chứng minh execution code đã thay đổi.

### 1.5. Các spike còn cần xác định nguyên nhân

10 và 11 đều có tám request overhead >500 ms; phần lớn thời gian spike nằm trước Chat Input. Nhiều spike cách khoảng 15-18 giây hoặc khoảng 85-100 request. Không đủ bằng chứng quy cho GC, SQLite, reconcile hoặc telemetry.

Có ngoại lệ: B1/10 index118 có gap sau inference 144.264 ms; B3/11 index230 có post-method 81.437 ms. Observer phải bao phủ setup, giữa node và response tail.

### 1.6. Trade-off bộ nhớ hiện chưa thể định lượng

HTML hiện chỉ có latency và counter, không có RSS/USS hoặc số bytes đang lưu. Occupancy sau warmup đã xác nhận:

| Arm | Compilation entries | Warm graph entries | RAM worker |
| --- | ---: | ---: | --- |
| 00 | 0 | 0 | Chưa đo |
| 01 | 0 | 1 | Chưa đo |
| 10 | 6 | 0 | Chưa đo |
| 11 | 6 | 1 | Chưa đo |

- Compilation cache giữ source, AST pickle, CodeType và metadata trong RAM mỗi process; giới hạn128 entries, source đủ điều kiện tối đa262.144 bytes/entry, LRU, không TTL. Giới hạn source không phải giới hạn tổng heap cache.
- Warm registry giữ template trong RAM mỗi process; mặc định128 flows,2.000.000 bytes payload/flow và32.000.000 bytes payload tổng. Đây là JSON payload accounting, không phải cap32 MB cho RSS. Registry không có TTL/LRU thay thế vì đầy; từ chối thêm khi vượt capacity và request có thể fallback cold. Eviction do thay đổi/xóa/revalidation vẫn tồn tại.
- Vừa tăng retained objects vừa có temporary copies và native allocations; bốn checkpoints đo RAM ở các thời điểm, chưa đo peak trong lúc dựng graph. Không suy arm11 chắc chắn dùng nhiều RSS hơn10 trước khi đo.
- Cả hai cache process-local, không dùng dung lượng disk làm cache. Một worker với một flow không chứng minh footprint khi có128 flows hoặc nhiều workers.
- Thử đọc psutil trong môi trường này: RSS khả dụng, USS bị AccessDenied. Đây là lý do USS phải optional; không được thay bằng0 hay bỏ toàn bộ kết quả RAM.

## Task 0 — Chốt contract, scope và khả năng kiểm chứng

**Files:** benchmark_analyst/DESIGN.md, protocol.py, tests/test_protocol.py và các validator trong reporting.py. Ghi contracts dưới đây vào DESIGN.md trước khi nối collector/CLI.

- [ ] Manifest giữ schema_version=1 và thêm kind=`factorial` hoặc `diagnostic`; thiếu kind được hiểu là factorial cho dữ liệu cũ, kind khác bị từ chối. Diagnostic có validator/schedule riêng, chỉ hai arm10/11; không đi qua điều kiện16slot hay contrasts bốn arm. is_full_campaign chỉ true khi kind=factorial và đủ cả bốn arm,1.000request/arm,4blocks.
- [ ] Khai báo resource_measurement={version:1, enabled:true, required:["rss"], optional:["uss"], checkpoints:["before_warmup","after_warmup","after_measurement","after_idle"], idle_seconds:5} khi bật đo RAM. Diagnostic declaration có version, enabled, detail, interval/buffer limits và capability map. Thiếu declaration ở dữ liệu cũ nghĩa là chưa đo; declaration bật nhưng thiếu file/record là invalid.
- [ ] Mỗi memory record có schema_version, experiment_id, block, arm, checkpoint, pid, process_create_time, elapsed_since_last_sample_ms (null trước sample đầu tiên); mỗi metric có bytes hoặc null, status, reason, started_at_utc, duration_ms. started_at_utc và duration_ms của RSS/USS tách riêng; cache accounting snapshot có timestamp riêng. Khóa duy nhất là experiment_id+block+arm+checkpoint. Validate đủ bốn checkpoints/slot, đúng thứ tự, cùng process identity và đúng slot trong manifest.
- [ ] Mỗi diagnostic event có schema_version, experiment_id, block, arm, worker identity, event_id, request_id nullable, parent_id nullable, category, name, start_ns/end_ns theo đồng hồ worker, thread_id, outcome. Metadata collector ghi clock/unit, capabilities, dropped/truncated counts và số record drained. Nested intervals chỉ cho phép tính union/exclusive theo cây đã kiểm tra; không cộng parent và child.
- [ ] analysis.valid tiếp tục nghĩa là latency/cache/output validity như trước. Thêm resource_validity, diagnostic_validity và overall_valid; overall_valid là phép AND của latency với các evidence bắt buộc đã khai báo. Legacy không khai báo extension giữ overall_valid=valid. CLI run/diagnose/analyze trả0 khi overall_valid, trả2 khi invalid; report phải hiện riêng từng trạng thái, không chỉ một nhãn VALID khi RSS bắt buộc bị thiếu. USS unavailable với reason không làm RSS/overall invalid.
- [ ] Smoke gate mới phải khớp workload/source hash, profile_id, fixture hash, resource declaration và diagnostic mode với run được nó chứng thực. Test smoke latency hợp lệ nhưng thiếu RAM không được authorize main có memory enabled. Một smoke chung không chứng thực mọi diagnostic variant.
- [ ] Xác định các hook coarse có thể gắn đúng call site/alias trước khi viết collector. Bắt buộc: setup boundary, existing output-method spans, GC timestamps và event-loop lag. Capability chưa hỗ trợ của DB/telemetry/background spans ghi unavailable, không giả zero; claim nguyên nhân thuộc capability đó bị chặn nhưng không chặn toàn bộ RAM deliverable. Không coi tất cả instrumentation mong muốn bên dưới là điều kiện phải hoàn thành trước báo cáo đầu tiên.
- [ ] Preflight kiểm tra các dependency runner, đặc biệt psutil, trước khi tạo worker. README phải có cách tái lập dependency benchmark qua môi trường/lockfile được quản lý; ghi actual installed version. Không dùng --no-sync để che dependency chưa được khai báo và không thêm psutil vào runtime production chỉ để runner đọc RSS.

## 2. Task A — Phân tích offline và bổ sung nội dung report

**Files:** reporting.py, tests/test_reporting.py, benchmark_end_to_end.ipynb và README.md trong benchmark_analyst; chỉ ghi derived artifacts khi analyze được gọi rõ ràng.

**Interfaces:** analysis.json thêm setup_decomposition, tail_diagnostics, resource coverage và worker cache occupancy; summary.csv latency giữ nguyên, resource_summary.csv là artifact mới.

- [ ] Viết tests cho phân rã sáu span tuần tự, checksum pre+gaps+post+chat bằng overhead, missing/overlapping spans và giữ mọi outlier. Nếu span overlap khiến phân rã đơn giản không xác định được, chỉ chặn decomposition, không phá phép union SCRFD đang hợp lệ.
- [ ] Tạo bảng 11-10 theo block, mean/p50/p95/p99, pre/gaps/post/chat, early/late windows cố định 50 request và danh sách spike >200 ms. Phân tích bỏ một block chỉ là sensitivity được ghi nhãn, không thay official result.
- [ ] Đưa occupancy/hits/misses/bypasses/builds/evictions vào report; hit rate dùng compilation events, không suy mỗi hit là một request.
- [ ] Thêm mục trade-off chứa mọi arm, latency tails, RAM coverage, lifecycle/capacity và chi phí warmup tách riêng. Dữ liệu cũ ghi RAM chưa đo.
- [ ] Bảng trade-off ghi mean/p50/p95/p99, RSS/USS tại bốn checkpoints, signed delta với00, marginal delta11-10 và occupancy/payload riêng. Định nghĩa overhead saved = mean00 - mean_arm; RAM tăng = RSS_arm - RSS00 ở cùng checkpoint và block. Chỉ render ms tiết kiệm/MiB khi RAM tăng dương và đủ coverage; đây là tỷ số mô tả, không phải điểm tối ưu chung.
- [ ] Diễn giải điều kiện lựa chọn:10 hiện tốt hơn về mean/median;11 hiện tốt hơn về p95/p99; sau khi đo RAM mới đánh giá chi phí cho từng mục tiêu. Pareto chỉ có ý nghĩa khi chỉ rõ các metric và cùng profile, không chọn một winner chung khi chưa có SLA/RAM budget. Warmup cost đo riêng; break-even request count chỉ tính khi steady-state saving dương và có warmup cost đối chứng được đo, không dùng mean warmup thay tổng startup.
- [ ] Sửa image renderer: allowlist charts.png, tradeoffs.png và diagnostics.png; không biến mọi ảnh thành charts.png, không cho external/path traversal src. Diagnostic figure trong report chính phải ghi rõ nguồn run, profile và observer bật; không trộn diagnostic timings vào primary statistics.
- [ ] Tests xác nhận legacy latency VALID với RAM unavailable, invalid latency không tạo speedup conclusion, raw hashes không đổi và notebook vẫn offline mặc định.

## 3. Task B — Collector RAM và observer chẩn đoán

**Files:** tạo benchmark_analyst/memory_metrics.py và diagnostic_observer.py cùng tests tương ứng; nối vào worker_observer.py, runtime.py và src/backend/base/langflow/benchmark_worker_identity.py. Diagnostic hook chỉ hoạt động trong benchmark worker được opt-in; khi off không cài callback/timer/phase wrappers và không cấp phát event buffers.

**Interfaces:** --memory-checkpoints mặc định off; --diagnostic-phases mặc định off. manifest có các declaration version1 độc lập. memory.jsonl và diagnostic_events.jsonl là sidecar; primary request rows/timing giữ nguyên.

### RAM cho campaign chính

- [ ] Thu external RSS bằng psutil của runner tại before_warmup, after_warmup, after_measurement, after_idle trước cleanup. PID lấy từ authenticated worker snapshot, giữ Process/create_time cùng slot; không dùng PID launcher uv. Chốt t0 là lúc run_sample cuối cùng hoàn tất, gồm download/output verification. after_idle bắt đầu không sớm hơn t0+5s; ghi elapsed thực tế, không khẳng định scheduler chạy đúng5s tuyệt đối. Giữa hai checkpoint cuối không gửi workload, cleanup hay poll worker; background tasks tiếp tục bình thường.
- [ ] Thu RSS trước USS, timestamp/duration riêng; USS optional, probe capability một lần/worker và ghi null/reason khi AccessDenied hoặc unsupported. RSS failures cũng được lưu, không bỏ record.
- [ ] memory.jsonl có64 records cho full campaign16slot×4checkpoint: slot, phase, process identity, collector/version, start/end timestamp, rss_bytes, uss_bytes và status/reason. Raw bytes là integer không âm, render MiB=bytes/1048576.
- [ ] Thêm public accounting snapshot nhất quán: compilation entries/source UTF-8 bytes/AST pickle bytes/limits; warm resident JSON bytes/entries/reservations/limits. Không deep-size objects và không gọi payload total là RAM cache.
- [ ] Tại mỗi checkpoint cố định cùng thứ tự: authenticated identity/accounting snapshot → RSS → USS nếu khả dụng. Ghi duration của từng bước; before_warmup xảy ra sau readiness/workload verification, after_warmup xảy ra trước measured loop. Do đó before_warmup là mốc khởi đầu phép so sánh, không được gọi là RAM process hoàn toàn cold; phải hiển thị cả absolute RSS lẫn delta.
- [ ] Tính chênh lệch after_idle với00 và difference-in-differences từ before_warmup theo block; n RAM=4worker/arm. Negative differences hợp lệ. after_idle không khẳng định quiescence. Peak RAM chưa đo.
- [ ] Tách latency valid khỏi RSS/USS measured/unavailable/invalid. USS unavailable không chặn kết luận RSS; missing/wrong mandatory RSS chặn kết luận RAM và fail request đo memory. Legacy analyze giữ hành vi exit code cũ.

### Phase và nuisance diagnostics

Triển khai coarse diagnostics trước. Setup subphases và background collectors chi tiết dưới đây chỉ bổ sung nếu coarse evidence chưa phân biệt nguyên nhân; ghi detail level vào manifest. Không bắt buộc viết hàng chục hooks trong lượt đầu. Nếu thêm detail sau một run, tạo run mới với control có cùng source/detail settings; không ghép event schemas hay phase coverage khác nhau.

- [ ] Ghi interval có request_id, name, parent/span id, start/end monotonic, thread id và outcome; không đổi production return/exception semantics. Records bounded, có dropped/truncated counters; đầy buffer làm diagnostic coverage invalid.
- [ ] Không write JSONL hay network I/O trong timed hooks. Drain bounded event buffers từ runner sau run_sample, cùng cadence/control calls ở mọi diagnostic variant kể cảD0; background events drain trước worker.stop. Ghi flush/drain cost và unfinished events; event buffer bị đầy phải hiện invalid, không mất dữ liệu âm thầm. Kiểm thử diagnostic off không làm thay đổi primary timing path ngoài observer vốn có.
- [ ] Coarse phases: middleware/auth/flow resolution, caller policy, globals compatibility, warm or cold setup, graph execution, job-completion DB write, serialization/final send.
- [ ] Setup phases: candidate copy/tweaks/defaults query/comparison; migration copy/validation/event replay; policy revalidation; raw graph copy; process_flow; vertices/edges/params; artifact lookup/AST restoration; module preparation/class exec/annotation registration; constructors.
- [ ] Ghi dispatch-entry đến method-entry để thấy queue wait; retain existing method-body spans. Worker CPU delta là toàn process, không gọi request-exclusive CPU.
- [ ] GC callbacks chỉ giữ bounded timestamps/generation/counts, không log/collect/gọi psutil trong callback. Record event-loop lag diagnostic với cadence20ms; thống kê overlap với request/spike thay vì trừ khỏi metric.
- [ ] Ghi native trace-finalization start/end/pending, telemetry queue depth, DB checkout/query/commit durations (không SQL parameters), warm reconciliation intervals và worker uptime. Event ngoài request giữ identity riêng để tương quan với request sau.
- [ ] RSS/system available RAM/process thread count/GC thresholds ở checkpoints; diagnostic có thêm lightweight snapshot mỗi25request sau khi run_sample hoàn tất. Không giả định SessionOptions=0 đồng nghĩa một thread; record options/provider/version và process thread count.
- [ ] Tests bảo toàn output, exception/cancellation/context propagation; hook được phục hồi; không double-count nested/overlapping spans, không thấy source/secret/global values trong raw diagnostics.

## 4. Task C — Sửa kiểm soát môi trường và chạy thí nghiệm phân biệt nguyên nhân

**Files:** benchmark_langflow.py, runtime.py, protocol.py và tests; thêm diagnose subcommand để chạy paired campaign và analyze diagnostic mà không giả vờ đạt full factorial campaign.

**Interfaces:** manifest kind=diagnostic cho diagnose, lưu profile/variant/schedule/seed/effective settings. Không tính contrasts bốn arm nếu dataset chỉ có10/11. run/analyze hiện tại vẫn phân biệt SMOKE/CAMPAIGN.

- [ ] Suite diagnostic có suite.json lưu lịch interleaved và references tới bốn child run D0/D1/D2/D3. Mỗi child có experiment_id riêng, manifest/state/requests/worker_evidence/memory/diagnostic sidecars riêng và tám slots block×arm; cùng outer_block_id dùng cho đối chiếu suite. Không ghi bốn variants vào cùng khóa experiment_id+block+arm. Child chỉ COMPLETE sau slot cuối và integrity checks; suite hoàn tất khi tất cả child và lịch global hợp lệ. Analyzer child và suite phải kiểm tra chéo identifiers, profile và coverage.
- [ ] Explicitly exclude benchmark_analyst/runs/** khỏi source identity, kể cả tracked. Giữ execution source, shipped runtime config/migration/extension data, pyproject.toml và uv.lock. Hash workload riêng. Test regenerate report không đổi execution fingerprint; thay .py/migration data phải đổi.
- [ ] Worker snapshot ghi allowlisted effective settings sau startup: DB dialect/fixture hash/engine pool, storage backend/root identity, message/tracing/telemetry/exporter flags, access logger effective level, GC, registry limits/reconcile interval và actual installed critical versions. Không dump env/database URL/credential values.
- [ ] Ghi host CPU/RAM/platform, interpreter executable/version, monotonic clock resolution, power/load context nếu đọc được; unavailable có reason, không tự đoán cấu hình máy.
- [ ] Chuẩn bị consistent SQLite backup và storage fixture dùng riêng dưới runs/, cùng frozen flow/user/variable state và reference. Mỗi slot clone từ cùng baseline; worker chỉ trỏ bản sao. Đối chiếu realpath để từ chối original DB/storage hoặc symlink quay về đó; read-back effective paths trước khi gửi request ghi. Nếu DB/storage không phải local SQLite/filesystem như profile hỗ trợ, dừng preflight với lý do cụ thể; không fallback sang nguồn gốc. Giữ message/job/tracing behavior. Record SQLite journal_mode/synchronous/wal_autocheckpoint/page_size/busy_timeout và DB/WAL size; không ép checkpoint hoặc thay pragma trong primary run.
- [ ] Dùng SQLite backup API cho DB, không copy riêng file.db trong khi WAL đang hoạt động. Storage fixture phải chứa các file cần cho frozen flow trong đúng namespace; ghi manifest hash, xác minh reference từ DB tồn tại và hash không đổi trong lúc tạo baseline. Không dùng hardlink/symlink cho dữ liệu worker có thể ghi. Nếu không chứng minh snapshot DB/storage nhất quán, fail preflight và giữ chẩn đoán cụ thể; không tự dừng/xóa dữ liệu nguồn. Baseline fixture đóng băng trước smoke; mỗi slot dùng bản copy độc lập của cùng nội dung.
- [ ] Chạy primary controlled profile với native tracing bật, GC bình thường, production feature maintenance bật; pin DO_NOT_TRACK=false và LANGFLOW_DO_NOT_TRACK=false trong benchmark worker để đối chứng telemetry có ý nghĩa. Đặt uvicorn.access=ERROR qua per-logger override sau app logging setup; snapshot và smoke log phải chứng minh access records thực sự tắt. Record trạng thái network/exporter, không kết luận telemetry-on đồng nghĩa delivery thành công. Đừng gọi removed legacy HTTP-reuse flag là mọi connection reuse đều tắt: requests.Session hiện reuse TCP.
- [ ] Profile identity chứa effective settings chuẩn hóa và fixture content hash, không chứa PID/port/path clone ngẫu nhiên. So sánh các arm/variant chỉ cho khác những flags có trong thiết kế; settings drift khác phải chặn paired conclusions. Main-v2 và controlled profile báo cáo riêng.

### Protocol diagnostic cố định

Mỗi variant dùng bốn paired blocks10/11, fresh worker/fixture mỗi slot,250 measured request/slot và5warmup trừD3. Bốn variant là một suite với outer blocks chung; không chạy xong toàn bộD0 rồi mớiD1. Lịch variant theo block: [D0,D1,D2,D3], [D1,D2,D3,D0], [D2,D3,D0,D1], [D3,D0,D1,D2]. Trong mỗi variant, thứ tự arm theo block là10→11,11→10,11→10,10→11. Ghi toàn bộ schedule trước khi chạy; các worker chạy tuần tự. Tổng2.000 request/variant,8.000 request/suite. Giữ raw failures, không retry bù. Đây là đối chứng cân bằng theo thời gian ở mức block, không bảo đảm loại hết nhiễu máy.

Trước suite dài, smoke từng variant với2measured request/arm/block,4blocks, cùng warmup/profile/detail; xác nhận output, cache, memory và diagnostic coverage khai báo. Diagnostic smoke có kind=diagnostic và mode=smoke; không được gọi full campaign hay dùng để kết luận hiệu năng.

- [ ] D0: diagnostic phases off, RAM checkpoints on. D1: cùng mọi settings nhưng coarse diagnostic phases on, gồm GC/lag collectors. So sánh trong cùng outer block/arm và lưu phân phối ảnh hưởng của cả observer package, kể cả cadence drains; không trừ assumed constant, không dùng D1 làm campaign latency chính. D0 diagnostic-control vẫn có cùng drain cadence dù buffer rỗng; primary campaign ở E không có cadence này.
- [ ] Nếu instrumentation làm đảo hướng11-10 hoặc chênh D1-D0 cùng cỡ/lớn hơn hiệu ứng đang giải thích, giữ phase attribution ở mức giả thuyết và dùng lượt targeted nhẹ hơn; không tuyên bố nguyên nhân production đã được xác nhận chỉ từ run bị observer chi phối.
- [ ] D2: giống D1, chỉ đổi policy product telemetry bằng DO_NOT_TRACK=true và LANGFLOW_DO_NOT_TRACK=true (native tracing vẫn on). Kiểm tra effective telemetry flag/queue thay đổi thật. Không quy toàn bộ chênh lệch cho network RTT hoặc riêng Scarf khi policy ảnh hưởng nhiều event paths.
- [ ] D3: giống D1, chỉ warmup count20 thay5 ở cả hai arms. Cache-hit readiness và runtime stationarity là hai tiêu chí riêng; measured count giữ250. Đây là effect của warmup policy gồm thêm message/job/trace writes, không phải phép đo thuần túy CPU warmup.
- [ ] Phân tích từng variant theo paired block, không coi request là independent repetition; chưa báo CI/p-value. Ghi cả typical setup penalty, tail overlap và broad slot slowdown.
- [ ] Nếu spike overlap GC: xác nhận GC generation/duration và phase bị ảnh hưởng; chỉ sau đó mới thiết kế diagnostic allocation/native-session-lifetime probe riêng, không tắt GC để làm official latency đẹp.
- [ ] Nếu setup extra copies hoặc DB/flush/event-loop/reconcile giải thích chênh lệch: ghi phase evidence và đề xuất Task D cho đợt sau. Nếu chưa xác định, tối đa một lượt targeted instrumentation kèm control cùng source (hai variants × bốn paired blocks ×250request/slot =4.000 measured requests bổ sung); sau đó bàn giao phần chưa giải thích, vẫn hoàn tất E. Không mở vòng profiling vô hạn.

## 5. Task D — Hướng tối ưu runtime cho đợt tiếp theo

Không nằm trong phạm vi triển khai benchmark đợt này. Bàn giao findings/proposal tương ứng; chỉ đưa vào một đợt runtime riêng sau khi reviewer đánh giá bằng chứng và phạm vi thay đổi. Phải có baseline E trước thay đổi, giữ cùng workload/profile/boundary khi so before/after. Không lấy main-v2 làm before của controlled profile.

- [ ] Viết regression reproducer cho bottleneck và các invariant liên quan, ghi before phase timings. Chỉ sửa một mechanism mỗi lượt.
- [ ] Nếu globals-candidate copy là bottleneck: kiểm tra binding applicability bằng helper không mutation trước khi copy; vẫn giữ lookup/current user ordering và cold fallback khi binding thực sự thay đổi hoặc lookup lỗi.
- [ ] Nếu raw-copy + process_flow-copy là bottleneck: thử reuse một request-owned detached payload qua các bước xây dựng; không share mutable template, component instances, params hoặc globals giữa requests/users. Test nested/grouped flows và parallel requests.
- [ ] Nếu class/module preparation là bottleneck: reuse source-derived immutable preparation, giữ fresh namespace/classes, imports/side effects/annotations/decorators và constructor semantics. Không share _session_cache bằng cách cache runtime class.
- [ ] Nếu full Flow-row fetch là bottleneck: đề xuất riêng việc tái sử dụng metadata resolver cho v1 với owner/share/stale-version/privacy/policy fallback tests. Chuyển benchmark sang v2 là experiment khác, không sửa âm thầm endpoint hiện tại.
- [ ] Nếu tracing/DB/reconcile gây contention: sửa trong service sở hữu bottleneck, chứng minh flush/cancellation/deletion/revision/authorization behavior không đổi. Không loại maintenance khỏi primary arm11 chỉ để giảm số đo.
- [ ] Sau mỗi fix, chạy focused correctness tests rồi diagnostic pairs mới, source frozen riêng. So sánh before/after dưới cùng fixture/profile; không reuse smoke có khác hash.

## Task E — Campaign bốn arm và báo cáo bắt buộc

Task này độc lập với việc tìm ra nguyên nhân hoặc triển khai runtime optimization. Đợt benchmark hiện tại phải giao đủ latency/RAM/report trên code production chưa tối ưu thêm.

- [ ] Chạy smoke-v3 với8request/arm,4blocks,5warmup, memory on và diagnostic phases off, không diagnostic drains giữa requests. Chỉ chạy main-v3 khi latency/cache/output và mandatory RSS evidence đầy đủ, source/profile/fixture/collector declarations khớp smoke.
- [ ] Chạy main-v3 đủ1.000request/arm,4blocks cân bằng,16freshworkers. Rerun một campaign cùng profile/source vào output mới để kiểm tra độ lặp lại; báo cáo riêng từng campaign và đối chiếu hướng mean/median/tails, không ghép block1 của hai campaign thành paired data. Mỗi campaign phải có64memory records hợp lệ; tổng128records cho hai campaign.
- [ ] Thêm HTML: bảng latency–RAM, scatter ΔRSS so với mean overhead saved có block points; panel diagnostic riêng cho phase breakdown10/11 và spike timeline nếu coverage đủ. Mọi panel diagnostic ghi run/profile/detail/observer state và link nguồn; các mẫu diagnostic không tham gia primary latency hoặc RAM contrasts. Phân biệt observed, explained và unresolved.
- [ ] Ghi rõ phạm vi một flow/một worker, không peak RAM, không byte attribution cho từng cache. Khi chênh lệch RAM nhỏ/đổi dấu giữa blocks, hiển thị các block và ghi chưa đủ bằng chứng xếp hạng RAM, không dùng tỷ số ms/MiB để che biến động.
- [ ] Bàn giao code diff, test results, hai primary report paths, diagnostic suite paths, raw integrity hashes và findings/proposal Task D nếu có. Không sửa raw main/main-v2. Số measured requests mặc định:64diagnostic smoke +8.000diagnostic +32factorial smoke +8.000primary/repeat =16.096; targeted follow-up tối đa thêm4.000. Warmup ghi riêng.

## 6. Kiểm tra và tiêu chí hoàn thành

Chạy từ repository root:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --no-sync python -m pytest -c /dev/null -p no:cacheprovider benchmark_analyst/tests -q

Các tests mới phải bao phủ: wrong/reused PID, missing/duplicate checkpoint, optional USS denied, signed RAM differences, raw immutability, fingerprint excludes outputs, effective access logging, nested phase accounting, callbacks/hooks restored on cancellation, observer truncation, clone destination never original DB và diagnostic dataset không được gắn nhãn full campaign. Runtime fixes bổ sung focused cache/warm/auth/file/globals/migration correctness tests; không đặt performance thresholds trong unit tests.

Hoàn thành khi:

1. Báo cáo nói chính xác11 thua/tốt hơn ở metric nào, không mặc định cả hai cache phải thắng.
2. RAM được đo cùng campaign mới, cache payload được ghi nhãn riêng; dữ liệu cũ giữ unavailable.
3. Typical pre-component penalty và periodic spikes có phase evidence; nguyên nhân chưa biết được giữ rõ ràng.
4. Benchmark môi trường có thể tái lập, effective logging/telemetry/DB/thread context được ghi và raw/observer validity được kiểm chứng.
5. Mọi claim runtime cải thiện có reproducer, correctness tests và before/after campaign; nếu11 vẫn thua ở mean, báo cáo giữ kết quả đó.

## Nguồn kiểm chứng

- Raw: benchmark_analyst/runs/main-v2/requests.jsonl, worker_evidence.jsonl, manifest.json, report.md; main chỉ dùng đối chiếu riêng.
- v1 fetch/setup: src/backend/base/langflow/api/v1/endpoints.py:807, src/backend/base/langflow/helpers/flow.py:613, src/backend/base/langflow/api/warm_graph.py:221/238/271/320.
- Graph reconstruction: src/lfx/src/lfx/graph/graph/base.py:379/1673/1713; src/lfx/src/lfx/graph/graph/utils.py:88; runtime class setup: src/lfx/src/lfx/custom/validate.py:433/735/787.
- Trace lifecycle: src/lfx/src/lfx/graph/graph/base.py:1085/1246; src/backend/base/langflow/services/tracing/service.py:355.
- Fingerprint: benchmark_analyst/protocol.py:124. HTML images: benchmark_analyst/reporting.py:572. Effective logger reset: src/lfx/src/lfx/log/logger.py:1276.
- Cache capacity/lifecycle: src/lfx/src/lfx/custom/component_compilation_cache.py:16/27/108; src/backend/base/langflow/services/warm_registry/service.py:73/285.
- RSS/USS semantics và chi phí collector: https://psutil.readthedocs.io/stable/#psutil.Process.memory_full_info
- GC callbacks/timing: https://docs.python.org/3.13/library/gc.html#gc.callbacks
- Native thread pools: https://onnxruntime.ai/docs/performance/tune-performance/threading.html
- SQLite checkpoints: https://sqlite.org/wal.html — chỉ là cơ chế cần đối chiếu với effective pragmas và DB timings, chưa được xác nhận là nguyên nhân spike.
