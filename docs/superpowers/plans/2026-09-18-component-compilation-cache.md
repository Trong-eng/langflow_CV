# Kế hoạch triển khai bộ nhớ đệm cho dữ liệu trung gian khi biên dịch component

> **Dành cho agent thực thi:** Cần dùng `superpowers:subagent-driven-development` (khuyến nghị) hoặc `superpowers:executing-plans` để thực hiện lần lượt từng đầu việc. Các bước dùng cú pháp checkbox (`- [ ]`) để theo dõi tiến độ.

**Mục tiêu:** Thêm một cache (bộ nhớ đệm) nội bộ theo từng process, có giới hạn dung lượng và mặc định tắt. Cache chỉ tái sử dụng artifact, tức dữ liệu trung gian được tạo ra khi phân tích và biên dịch source code của custom component. Namespace, class, constructor context và instance vẫn phải được tạo mới ở mỗi lần chạy; các policy check cũng phải tiếp tục chạy đầy đủ.

**Kiến trúc:** Module `lfx.custom.component_compilation_cache` quản lý một LRU cache. Khóa cache gồm SHA-256 đầy đủ của source code và phiên bản artifact; source gốc cũng được so sánh lại để tránh dùng nhầm. `eval_custom_component_code` lấy artifact đã chuẩn bị, còn `create_class` dựng một AST riêng cho request rồi tiếp tục import và thực thi module/class ở mỗi lần gọi. Benchmark chạy qua đúng các đường public evaluation và graph instantiation ở ba cấu hình: baseline, cache tắt và cache bật.

**Công nghệ:** Python 3.13, `ast`, `hashlib`, `threading.RLock`, Pydantic settings, pytest, `resource`, `time.perf_counter_ns`.

**Đặc tả gốc:** `/Users/tranquangtrong/.codex/attachments/4e82310d-549f-4126-8e4d-eec70f49fe50/pasted-text.txt`

## Các ràng buộc chung

- Làm trực tiếp trong `/Users/tranquangtrong/Desktop/langflow_CV` trên nhánh `codex/cache-component-compilation-artifacts`; không tạo worktree hoặc clone repository khác.
- Commit production dùng làm baseline là `c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d`.
- Cache chỉ tồn tại trong process hiện tại, có giới hạn, mặc định tắt và bỏ qua source lớn hơn 262.144 byte UTF-8.
- Cache entry không được chứa credentials, parameters, object user/session, tham chiếu graph/vertex, runtime namespace, class, instance hoặc output.
- Việc resolve trusted source, import lúc runtime, thực thi module/class body, kiểm tra annotation, chạy constructor và tạo instance vẫn phải diễn ra ở mọi request.
- Dùng `uv run` cho các lệnh Python; không push, merge hoặc deploy.

---

### Đầu việc 1: Tạo benchmark baseline có thể chạy lại

**Các file:**

- Tạo: `scripts/benchmarks/benchmark_component_compilation_cache.py`
- Tạo: `scripts/benchmarks/run_component_compilation_cache_baseline.sh`
- Tạo: `benchmark_results/component_compilation_cache/baseline.json`

**Đầu vào/đầu ra:**

- Dùng các API: `lfx.custom.eval.eval_custom_component_code`, `lfx.interface.initialize.loading.instantiate_class` và `lfx.graph.graph.base.Graph._instantiate_components_in_vertices`.
- Cung cấp CLI `--mode baseline|off|on` và JSON chứa thông tin môi trường, số mẫu, p50/p95, số lần parse/compile, thống kê cache nếu có, CPU profile và chênh lệch RSS.

- [ ] **Bước 1: Thêm benchmark harness**

  Triển khai các workload có tính lặp lại: cùng source, nhiều source khác nhau, graph 10 node, graph 100 node, tuần tự, đồng thời, cập nhật source, cold/miss/hit, constructor, chuẩn bị graph và local pass-through component. Warm-up phải nằm ngoài mẫu đo. Ghi rõ số mẫu trong metadata và không gọi network, model hoặc dịch vụ bên ngoài.

- [ ] **Bước 2: Chạy harness trên production baseline**

  Chạy: `scripts/benchmarks/run_component_compilation_cache_baseline.sh`

  Kỳ vọng: exit code 0, JSON hợp lệ, revision đúng baseline và không có cache counter trong chế độ baseline.

- [ ] **Bước 3: Commit riêng phần benchmark**

  Chạy: `uv run git commit -m "perf: add component compilation benchmark harness"`

### Đầu việc 2: Settings và hành vi LRU cache cốt lõi

**Các file:**

- Tạo: `src/lfx/src/lfx/custom/component_compilation_cache.py`
- Sửa: `src/lfx/src/lfx/services/settings/groups/cache.py`
- Sửa: `src/lfx/tests/unit/services/settings/test_settings_composition.py`
- Tạo: `src/lfx/tests/unit/custom/test_component_compilation_cache.py`

**Đầu vào/đầu ra:**

- Nhận `Settings.component_compilation_cache_enabled`, chuỗi source chính xác và một hàm builder không có tham số.
- Cung cấp `get_or_build_component_artifact(source, builder)`, `clear_component_compilation_cache()`, `component_compilation_cache_stats()`, `COMPONENT_COMPILATION_ARTIFACT_GENERATION` và `ComponentCompilationArtifact` bất biến.

- [ ] **Bước 1: Viết test lỗi trước cho settings và hành vi cache**

  Bao phủ các trường hợp: mặc định tắt/bật bằng biến môi trường, lần đầu miss/lần sau hit, source khác nhau, cùng tên class nhưng source khác, cập nhật source, LRU eviction, clear, source quá lớn, cache bị tắt, lỗi builder không được cache, thay đổi generation, kiểm tra exact source và nhiều luồng cùng source chỉ build một lần.

- [ ] **Bước 2: Xác nhận test đang fail vì chưa có API**

  Chạy: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/services/settings/test_settings_composition.py -q`

  Kỳ vọng: FAIL vì setting/module/API mới chưa tồn tại.

- [ ] **Bước 3: Triển khai cache có giới hạn trong process**

  Thêm `component_compilation_cache_enabled: bool = False`; dùng `OrderedDict` tối đa 128 entry, khóa gồm SHA-256 đầy đủ và generation, so sánh lại source gốc, giới hạn 262.144 byte, dùng `RLock`, build dưới lock để tránh nhiều luồng compile cùng source, không lưu lỗi và có counter cho hit/miss/bypass/eviction/build.

- [ ] **Bước 4: Xác nhận các test tập trung đã pass**

  Chạy: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/services/settings/test_settings_composition.py -q`

  Kỳ vọng: PASS.

### Đầu việc 3: Tích hợp artifact nhưng không tái sử dụng runtime state

**Các file:**

- Sửa: `src/lfx/src/lfx/custom/eval.py`
- Sửa: `src/lfx/src/lfx/custom/validate.py`
- Sửa: `src/lfx/tests/unit/custom/test_component_compilation_cache.py`
- Sửa: `src/lfx/tests/unit/custom/component/test_validate.py`

**Đầu vào/đầu ra:**

- Nhận `ComponentCompilationArtifact` chứa exact source, generation, tên class, AST template đã serialize, compiled code của class đích và alias của trusted vector-store decorator.
- Cung cấp `prepare_component_compilation_artifact(code, class_name=None)` và `create_class(code, class_name, *, artifact=None)` nhưng vẫn giữ tương thích với cách gọi public hiện có.

- [ ] **Bước 1: Viết test integration và isolation ở trạng thái fail**

  Bao phủ: giảm số lần parse/compile, class identity mới, function globals mới, tách biệt mutable class/global, side effect của decorator và helper ở mỗi lần gọi, imports, inheritance, constructor/user/parameter, inputs/outputs riêng biệt, chạy đồng thời không rò state, graph/request object được giải phóng, annotation nguy hiểm tiếp tục bị chặn và policy bị siết sau khi cache warm vẫn chặn trước eval.

- [ ] **Bước 2: Xác nhận test integration fail vì chưa tái sử dụng artifact**

  Chạy: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/custom/component/test_validate.py src/lfx/tests/unit/custom/test_annotation_validation.py src/lfx/tests/unit/utils/test_resolve_trusted_code_for_build.py -q`

  Kỳ vọng: các assertion mới về cache FAIL, còn test compatibility/security hiện có vẫn pass.

- [ ] **Bước 3: Triển khai bước chuẩn bị artifact và dựng runtime cho từng lần gọi**

  Chỉ đưa các phần thuần source vào artifact: biến đổi source, parse AST, validation annotation tĩnh, thêm future import, phân tích trusted decorator và compile class đích. Mỗi lần dùng cache hit phải deserialize thành AST mới; sau đó dựng lại `exec_globals`, helper class/function, component class, annotation sidecar và vector-store decoration. Không giữ cache lock khi chạy `prepare_global_scope`, `exec`, tạo class hoặc tạo instance.

- [ ] **Bước 4: Xác nhận các suite integration và security đã pass**

  Chạy: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/custom/component/test_validate.py src/lfx/tests/unit/custom/test_annotation_validation.py src/lfx/tests/unit/interface/test_loading_custom_component_code_param.py src/lfx/tests/unit/utils/test_resolve_trusted_code_for_build.py src/backend/tests/unit/api/test_warm_graph_execution.py src/backend/tests/unit/api/v1/test_custom_component_policy.py -q`

  Kỳ vọng: PASS.

### Đầu việc 4: Benchmark cache OFF/ON và viết báo cáo

**Các file:**

- Sửa: `scripts/benchmarks/benchmark_component_compilation_cache.py`
- Tạo: `benchmark_results/component_compilation_cache/off.json`
- Tạo: `benchmark_results/component_compilation_cache/on.json`
- Tạo: `benchmark_results/component_compilation_cache/report.md`

**Đầu vào/đầu ra:**

- Dùng API clear/stats cuối cùng và `LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED`.
- Tạo số liệu tuyệt đối và phần trăm cho baseline/OFF/ON, gồm p50/p95, số lần parse/compile, RSS, chi phí cold miss, workload đồng thời và các kích thước graph.

- [ ] **Bước 1: Chạy code cuối với cache tắt**

  Chạy: `LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED=false uv run python scripts/benchmarks/benchmark_component_compilation_cache.py --mode off --output benchmark_results/component_compilation_cache/off.json`

  Kỳ vọng: exit code 0 và số cache entry vẫn bằng 0.

- [ ] **Bước 2: Chạy code cuối với cache bật**

  Chạy: `LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED=true uv run python scripts/benchmarks/benchmark_component_compilation_cache.py --mode on --output benchmark_results/component_compilation_cache/on.json`

  Kỳ vọng: exit code 0, cùng source tạo cache hit và source thay đổi tạo cache miss.

- [ ] **Bước 3: Viết báo cáo so sánh từ số liệu thực đo**

  Tạo bảng Markdown từ các file JSON với p50/p95 tuyệt đối, phần trăm chênh lệch, tóm tắt CPU/profile, số lần parse/compile, số hit/miss/bypass/eviction, chi phí RSS, phiên bản Python, số worker, số mẫu, giới hạn và khuyến nghị triển khai chỉ dựa trên dữ liệu đã đo.

### Đầu việc 5: Kiểm chứng cuối và review diff

**Các file:**

- Chỉ format các file đã được liệt kê trong đầu việc 1-4 nếu cần.

**Đầu vào/đầu ra:**

- Nhận implementation và artifact benchmark đã hoàn tất.
- Tạo một nhánh đã kiểm chứng, sẵn sàng để người dùng review nhưng chưa push/merge/deploy.

- [ ] **Bước 1: Format thay đổi backend**

  Chạy: `make format_backend`

  Kỳ vọng: exit code 0.

- [ ] **Bước 2: Chạy lại các test tập trung**

  Chạy: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/custom/component/test_validate.py src/lfx/tests/unit/custom/test_annotation_validation.py src/lfx/tests/unit/interface/test_loading_custom_component_code_param.py src/lfx/tests/unit/utils/test_resolve_trusted_code_for_build.py src/lfx/tests/unit/services/settings/test_settings_composition.py src/backend/tests/unit/api/test_warm_graph_execution.py src/backend/tests/unit/api/v1/test_custom_component_policy.py -q`

  Kỳ vọng: PASS.

- [ ] **Bước 3: Chạy lint/check cho các file Python đã thay đổi**

  Chạy: `uv run ruff check src/lfx/src/lfx/custom/component_compilation_cache.py src/lfx/src/lfx/custom/eval.py src/lfx/src/lfx/custom/validate.py src/lfx/src/lfx/services/settings/groups/cache.py src/lfx/tests/unit/custom/test_component_compilation_cache.py scripts/benchmarks/benchmark_component_compilation_cache.py`

  Kỳ vọng: PASS.

- [ ] **Bước 4: Review diff và trạng thái repository**

  Chạy: `git diff --check && git diff --stat c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d && git status --short --branch`

  Kỳ vọng: không có lỗi whitespace và không có file ngoài phạm vi kế hoạch.
