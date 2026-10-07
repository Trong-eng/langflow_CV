# Code Review Summary — benchmark trade-offs và warm diagnosis

Ngày review: 2026-10-04. Vai trò ban đầu: **reviewer**; chuyển sang **worker** sau khi ghi findings dưới đây, theo yêu cầu trực tiếp của người dùng. Review working tree của branch `add_worker_warm_graph`, HEAD `1a0d984db0cd04cd7291d1f1f6919caa7aec09b5`, gồm implementation chưa commit của kế hoạch [2026-10-02](/Users/tranquangtrong/Desktop/langflow_CV/docs/superpowers/plans/2026-10-02-benchmark-tradeoffs-and-warm-diagnosis.md).

Phạm vi: fixture/isolation, source/profile/smoke contracts, lifecycle worker, RAM accounting, diagnostic phase validation, suite pairing và các báo cáo HTML/Markdown. Review đã dùng reproductions trên bản sao tạm của raw v3 và process tạm; chưa sửa source hoặc raw chính thức ở giai đoạn reviewer. Các vị trí dòng bên dưới là vị trí **trước remediation**.

## Critical Issues

Không xác minh được P0/P1. Có **9 findings P2 phải sửa** vì ảnh hưởng độ tin cậy của benchmark hoặc bảo đảm dọn worker. Kết luận review ban đầu: cần remediation trước khi bàn giao implementation.

## Suggestions / Required Remediation

### R1 — [P2] RAM sidecar hỏng nhưng vẫn xuất số liệu so sánh RAM

- File: `benchmark_analyst/reporting.py:1728`.
- Repro: thêm một dòng `{invalid json}` vào `memory.jsonl` của bản sao main-v3. `overall_valid` và resource validity false, nhưng `resource_validity.contrasts` vẫn có số và Markdown vẫn in `11 - 10 | after_idle | rss`.
- Nguyên nhân: `validate_resources` tính contrasts trước khi lỗi đọc JSONL được ghép vào resource errors.
- Sửa: bằng chứng RAM hỏng phải bỏ resource contrasts và hình trade-off có kết luận; giữ descriptive diagnostics với trạng thái INVALID. Kiểm tra cả JSON và báo cáo được sinh.
- Regression: dữ liệu đủ checkpoint cộng một dòng JSONL hỏng không được giữ numeric RAM conclusions.

### R2 — [P2] Thiếu định danh/source/profile vẫn được xác nhận VALID

- Files: `benchmark_analyst/reporting.py:164`, `benchmark_analyst/memory_metrics.py:220`, `benchmark_analyst/campaign_contract.py:90`.
- Ba repro độc lập trên bản sao main-v3 đều vẫn `overall_valid=True`: source đổi thành `{"unverified":true}`; bỏ experiment ID ở cả manifest lẫn memory records; bỏ profile và profile_id.
- Nguyên nhân: source chỉ cần object không rỗng; hai ID thiếu so sánh `None == None`; thiếu profile được xem là legacy ngay cả khi manifest khai báo controlled fixture/RAM.
- Sửa: yêu cầu experiment ID không rỗng và source SHA256 đúng định dạng. Controlled run phải có profile/identity đầy đủ và effective common settings; giữ khả năng đọc main/main-v2 legacy thật. Không tự tạo provenance thay cho bằng chứng bị thiếu.
- Regression: mỗi mutation trên phải tạo INVALID và chặn kết luận tương ứng; raw legacy hợp lệ vẫn phân tích được.

### R3 — [P2] Phase chẩn đoán mâu thuẫn với method timing nhưng vẫn VALID

- File: `benchmark_analyst/reporting.py:734`.
- Repro: ở bản sao D1, đặt toàn bộ `setup/pre_component.end_ns = start_ns`. Diagnostic và overall vẫn VALID; setup mean 0 ms, trong khi method decomposition của arm11 vẫn pre mean 36,0977 ms.
- Nguyên nhân: validator kiểm tra envelope và node coverage, chưa ràng buộc pre-component hoặc từng component event với cùng boundary đã đo trong response.
- Sửa: đối chiếu pre-component với đầu envelope và method đầu tiên; đối chiếu multiplicity và start/end của component body với `component_intervals_ms`, dùng tolerance cho chuyển đổi ns/ms. Bằng chứng sai phải chặn phase conclusions.
- Regression: collapsed/moved setup, moved và duplicated body spans bị từ chối; overlapping hợp lệ vẫn dùng union, không cộng durations.

### R4 — [P2] Suite execution order không ràng buộc thời gian child thật

- File: `benchmark_analyst/suite_reporting.py:165`, `:230`.
- Repro: dịch toàn bộ `suite_events.jsonl` của bản sao diagnostic-v3 thêm 365 ngày, giữ child raw. Suite vẫn VALID, execution order `measured`, paired comparisons vẫn tồn tại.
- Nguyên nhân: chỉ so thứ tự suite events với nhau và labels với schedule, không đối chiếu khoảng thời gian mỗi slot với requests/RAM checkpoints của child đó.
- Sửa: suite khai báo execution events phải chứng minh request warmup/measured và checkpoint collection nằm trong slot tương ứng, trên UTC clock của cùng runner. Thiếu/hỏng/outside thời gian phải chặn suite comparisons. Không suy ra worker monotonic cùng gốc giữa các process.
- Regression: shifted suite times, swapped child timing, thiếu timestamp hoặc slot interval quá hẹp bị từ chối; suite thật hợp lệ vẫn pass.

### R5 — [P2] Profile hỏng làm analyzer crash thay vì tạo INVALID report

- File: `benchmark_analyst/campaign_contract.py:110`.
- Repro: bỏ `profile.product_telemetry` ở bản sao main-v3; public `analyze` ném `KeyError`, không xuất INVALID report.
- Sửa: validate kiểu và các key bắt buộc trước khi truy cập; snapshot effective hỏng phải trở thành lỗi có ngữ cảnh, không exception ngoài validator.
- Regression: thiếu telemetry và malformed profile/effective object tạo JSON/HTML INVALID, CLI analyze trả failure như contract.

### R6 — [P2] Duration RAM quá lớn làm datetime overflow

- File: `benchmark_analyst/memory_metrics.py:253`, `:272`.
- Repro: `rss.duration_ms = 1e300` (JSON hợp lệ, finite và không âm) gây `OverflowError`, không sinh report.
- Sửa: kiểm tra duration trong miền timestamp/collection interval hoặc bắt overflow tại phép tính end time; RSS và accounting đều cần xử lý fail-closed.
- Regression: oversized finite duration của RSS và cache accounting đều tạo INVALID; không chuyển unavailable thành zero.

### R7 — [P2] Nhánh legacy nhận controlled smoke không đủ bằng chứng

- File: `benchmark_analyst/benchmark_langflow.py:102`.
- Repro: gọi legacy `run` không có `--fixture`, dùng controlled smoke với `valid=True`, `overall_valid=False` và reference/source khớp. Gate vẫn đi đến tạo Worker dù smoke mất mandatory RSS.
- Nguyên nhân: gate cũ dùng latency validity và bỏ contract fixture/profile/resources/diagnostics, raw integrity.
- Sửa: dùng smoke contract chung trước khi tạo worker/output. Controlled smoke không thể cấp phép cho target legacy không isolation; giữ đường chạy smoke legacy thật tương thích.
- Regression: invalid controlled smoke, profile mismatch và raw bị đổi đều bị chặn trước worker start; genuine legacy smoke phù hợp được nhận.
- Root review thêm cache contract: bản sao controlled smoke hợp lệ bị bỏ `analysis.overall_valid` vẫn được `check_smoke` nhận do fallback sang `valid`. Fallback chỉ được áp dụng cho manifest legacy thật; modern smoke phải có `overall_valid is True`. Bổ sung regression thiếu/malformed overall status, giữ fallback legacy tương thích.

### R8 — [P2] Launcher đã chết nhưng child server vẫn sống sau stop

- File: `benchmark_analyst/runtime.py:176`. Đây là lỗi có sẵn ở HEAD, được đưa vào scope vì controlled fixture mới chỉ được release sau khi worker dừng.
- Repro thực: tạo process group `uv run ... python`, kết thúc launcher trước rồi gọi `Worker.stop`; child sống sót vì `poll()` launcher đã kết thúc khiến code bỏ qua `killpg`.
- Sửa: giữ ownership của process group, dừng child ngay cả khi launcher đã exit, bounded wait/escalation; chỉ dọn scratch và cho phép release fixture sau khi worker group đã dừng. Không kill process không thuộc worker.
- Regression: child sống sau launcher exit; child không nhận SIGTERM; stop idempotent và cleanup không diễn ra khi còn process sống.
- Review vòng cuối tìm thêm lỗi trong remediation đầu tiên: `_live_group_members` dùng `process_iter(...).info["create_time"]`, nhưng psutil 7.2.2 cache Process objects/create_time. Reviewer tái hiện bằng process group không thuộc worker và cache generation cũ: fresh generation khác ownership, nhưng signal gate vẫn gọi killpg (spy, không gửi signal). Đây là lỗi stale-cache có thể sửa, khác với race POSIX giữa check và signal.
- Worker phải bổ sung regression cached birth không thể cấp quyền signal cho generation mới; đọc generation từ `psutil.Process(pid)` mới cho mỗi PID khi xác minh ownership. R8 được mở lại ở vòng này; closure dưới đây xác nhận kết quả kiểm tra sau sửa.

### R9 — [P2] Engine provenance unavailable vẫn có path tưởng như đã xác minh

- Files: `benchmark_analyst/worker_observer.py:78`, `:573`, `benchmark_analyst/fixtures.py:194`.
- Repro: settings trỏ clone tạm nhưng `get_db_service` ném RuntimeError. `snapshot_async` báo engine unavailable, vẫn giữ configured clone path; cả fixture gate và profile gate nhận snapshot.
- Nguyên nhân: actual-engine result không ghi đè path/backend khi lookup thất bại; fixture gate chỉ kiểm tra path/settings.
- Sửa: tách configured và observed provenance hoặc xóa observed path/backend khi chưa chứng minh; controlled fixture gate yêu cầu actual measured SQLite engine/path. Không mở connection thay thế để tạo bằng chứng.
- Regression: lookup lỗi, thiếu/non-AsyncEngine, unsupported engine và in-memory SQLite không thể chứng minh isolated fixture; registered SQLite thật vẫn được nhận.

## Worker remediation ledger — sau kiểm chứng

| Finding | Trạng thái | Thay đổi và bằng chứng |
| --- | --- | --- |
| R1 | CLOSED | JSONL read errors xóa RAM contrasts, chặn trade-off figure; test kiểm tra JSON/Markdown/HTML. |
| R2 | CLOSED | Experiment ID/source SHA256 bắt buộc; modern profile đủ descriptor và 15 common-setting keys, đối chiếu mọi checkpoint; legacy thật vẫn pass. |
| R3 | CLOSED | So multiplicity và endpoints theo node; pre-component khớp ASGI entry/method đầu tiên với tolerance 1 ns; invalid phase summaries bị bỏ. Raw D1 thật 2.040/2.040 request vẫn hợp lệ. |
| R4 | CLOSED | Mọi request warmup/measured và RAM collection interval ràng buộc đúng global slot theo runner UTC; 7 regression từng fail, full suite-reporting 39 pass. |
| R5 | CLOSED | Thiếu key hoặc malformed effective object tạo INVALID report; public analyzer regressions có trong 174 reporting/memory tests. |
| R6 | CLOSED | Duration overflow/huge integers được từ chối có ngữ cảnh; cả accounting, RSS và numeric primary đi qua INVALID thay vì crash. |
| R7 | CLOSED | Shared smoke gate trước Worker/output; raw bị sửa/xóa bị chặn, modern overall status phải là boolean true; legacy fallback giữ tương thích. 33 contract/CLI tests pass. |
| R8 | CLOSED | Đọc fresh process generation, stop cả orphan/TERM-ignoring child, bounded cleanup và giữ ownership/scratch khi chưa dừng. 11 runtime tests pass; independent stale-cache repro không gọi killpg, unrelated child vẫn sống. |
| R9 | CLOSED | Async observed path/backend null khi engine unavailable; fixture yêu cầu measured actual SQLite engine/path. 47 observer/fixture tests pass, gồm registered AsyncEngine thật. |

Quy trình đã thực hiện: reviewer ghi 9 findings trước source edits → worker đọc MD, regression red → fix và focused green → independent review → sửa hai biến thể R8/R7 được review bổ sung → whole-suite và raw reanalysis cuối. Source không đổi giữa lượt kiểm chứng cuối và closure.

## Bằng chứng kiểm chứng cuối ngày 2026-10-04

**332 tests pass, 8 deprecation warnings đã có, 35,09 giây; exit code 0.** Ruff kiểm tra toàn bộ 24 file Python thay đổi pass; `git diff --check` sạch. Independent reviewer kiểm tra reporting/memory, suite và runner; root kiểm tra observer/fixture. Các repro dùng dữ liệu/process tạm, không chạy campaign hiệu năng mới.

Lệnh whole-suite:

```bash
PYTHONPATH=.:src/backend/base:src/lfx/src:src/sdk/src \
UV_CACHE_DIR=/private/tmp/langflow-benchmark-plan-uv-cache \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
MPLCONFIGDIR=/private/tmp/langflow-benchmark-mpl \
uv run --no-sync python -m pytest -c /dev/null -p no:cacheprovider \
  -p pytest_asyncio.plugin --asyncio-mode=auto benchmark_analyst/tests -q
```

Log đầy đủ: [review_v4_tests.log](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/implementation-v3/review_v4_tests.log). Bằng chứng máy đọc được: [REVIEW_V4_VERIFICATION.json](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/implementation-v3/REVIEW_V4_VERIFICATION.json). Script reanalysis: [review_v4_reanalyze.py](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/implementation-v3/review_v4_reanalyze.py).

Reanalysis 14 dataset và 2 diagnostic suite đều VALID. Arm summaries, latency contrasts, RAM contrasts và diagnostic phase summaries khớp chính xác snapshot trước remediation. Mọi slot suite thật qua kiểm tra UTC mới. Hash 86 raw files của lượt review giữ nguyên; đối chiếu bổ sung 8 historical raw files và 72 original diagnostic/smoke raw files cũng giữ nguyên (các tập có giao nhau, không cộng thành tổng mới).

Source hiện đã kiểm chứng: `b5a5f3383ecb84b8c0d87b25c607a403872d1d2c5acdc9b8e97ea6a673505f06`, 1.742 source files. **Source collector của primary cũ vẫn là `69d151c5…`; diagnostic cũ vẫn là `0f62d35e…`.** Raw manifest/profile/source ID không được sửa thành source hiện tại. Derived diagnostic analyzer hash nhất quán với reporting.py hiện tại; đây là reanalysis, không phải phép đo latency mới.

Các báo cáo HTML được tái sinh và static local-link/artifact checks pass; trade-off PNG đã được xem để kiểm tra units, whole-worker RSS và cảnh báo peak chưa đo. Kết quả khoa học giữ nguyên: arm11 chậm hơn arm10 trong hai primary v3; chênh RAM sau idle chưa ổn định. Gate và lifecycle được sửa ngoài primary timed path, không dùng để tuyên bố tối ưu production latency.

Kết luận closure: 9/9 findings đã sửa trong scope benchmark. Campaign dài tiếp theo phải chạy smoke mới khớp source; không tái sử dụng smoke thuộc collector source cũ.

## Worker đối chiếu lại feedback theo yêu cầu tiếp theo

Worker đã đọc lại toàn bộ R1–R9, đối chiếu các fix và regression trong working tree; independent acceptance audit xác nhận PASS cho cả chín mục. Các sửa đã có đầy đủ ở source hiện tại, lượt này bổ sung bằng chứng kiểm chứng mới.

Fresh whole-suite: **332 tests pass, 8 warnings đã có, 34,39 giây, exit 0**. Ruff 24 file Python thay đổi pass; `git diff --check` sạch. Source vẫn là `b5a5f3383ecb84b8c0d87b25c607a403872d1d2c5acdc9b8e97ea6a673505f06`; hash 86 review raw, 8 historical raw và 72 original diagnostic raw tiếp tục khớp. Các tập raw có giao nhau.

Log mới: [worker_feedback_recheck_tests.log](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/implementation-v3/worker_feedback_recheck_tests.log). Receipt riêng: [WORKER_FEEDBACK_RECHECK.json](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/implementation-v3/WORKER_FEEDBACK_RECHECK.json). Verification cũ được giữ như snapshot của lượt trước; receipt mới ghi hash phiên bản MD có phần bổ sung này.

## Giới hạn giữ nguyên

- Không sửa raw/history/source ID của campaign đã chạy. Campaign cũ là bằng chứng cho collector source ghi trong manifest; reanalysis nếu có ghi rõ analyzer mới.
- Không tối ưu Task D hoặc sửa graph để làm arm11 thắng. Quan sát GC overlap chưa chứng minh nguyên nhân nhân quả; chưa chạy thêm targeted probe nhẹ hơn là deviation đã ghi trong kế hoạch trước.
- Lỗi wrapper khi gọi đồng thời hai output trên cùng component tái hiện được nhưng đã có ở HEAD, không thuộc SCRFD tuần tự hiện tại; không đưa vào 9 findings cần sửa đợt này.
- Ownership dùng PID/create_time mới trước signal và giữ resources khi chưa chứng minh được cleanup. Kiểm tra generation và `killpg` là hai thao tác POSIX riêng, nên không có bảo đảm signaling nguyên tử; stale psutil cache đã được loại khỏi quyết định signal.
- Full-page browser rendering chưa được kiểm chứng do URL safety block ở lượt trước. Không tìm đường vòng; static artifact/link checks và PNG inspection là phạm vi QA có thể thực hiện.
