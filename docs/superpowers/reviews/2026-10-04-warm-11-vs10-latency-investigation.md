# Điều tra latency: vì sao 11 chậm hơn 10 và hướng loại bỏ warm graph

Ngày 2026-10-04. Mục tiêu người dùng: giảm latency thực tế; một cơ chế không đem lại lợi ích phải có hướng loại bỏ. Không lấy việc bật thêm cache làm bằng chứng cải tiến.

**Quyết định đề xuất: lấy compilation-only `10` làm cấu hình triển khai cho flow SCRFD/API v1 đang đo; đưa warm graph ra khỏi đường thực thi mặc định của workload này. Không tiếp tục đầu tư chỉ để khiến `11` thắng benchmark.** Việc xóa toàn bộ feature khỏi repository cần dựa vào phạm vi API còn phục vụ: v2 có một lợi ích đọc metadata riêng chưa được đo trong campaign v5. Lượt điều tra này tạo evidence và định hướng, chưa sửa runtime hoặc cấu hình triển khai.

## 1. `11` là tổ hợp hai cờ, không phải thứ hạng chất lượng

`10` bật compilation cache, `11` bật compilation cache và warm graph. Warm chỉ có lợi khi công việc tránh được lớn hơn lookup, validation, copying và tái tạo state riêng từng request. Hai cache có thể trùng phần công việc; lợi ích không cộng tuyến tính.

Đối với mục tiêu mean/p50/p95 overhead của flow hiện tại, số đo ủng hộ `10`:

| Campaign | Mean 10 | Mean 11 | 11−10 | p50 10→11 | p95 10→11 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Main v5 | 51,716 | 55,877 | +4,161 ms / +8,04% | 43,474→46,941 | 66,693→74,966 |
| Repeat v5 | 53,113 | 59,769 | +6,656 ms / +12,53% | 44,061→48,891 | 68,130→81,642 |

`10` có mean thấp hơn trong cả 8 primary blocks. Main block deltas: +6,644 / +5,373 / +0,665 / +3,961 ms; repeat: +4,332 / +15,253 / +4,055 / +2,982 ms. 50 request cuối mỗi slot vẫn cho pooled mean delta dương: +7,115 ms main và +4,079 ms repeat. D3 dùng 20 warmups/slot vẫn có mean `11−10` +4,397 ms. Các quan sát này không ủng hộ giải thích đơn giản “warm chưa đủ nóng”.

**Ngoại lệ phải giữ trong kết luận:** p99 overhead của `11` tốt hơn ở hai primary runs; ở diagnostic control D0, mean `11` còn thấp hơn `10` 0,394 ms (56,613 so với 57,007), dù median cao hơn 2,192 ms. D3 p99 lại xấu hơn. Vì vậy không tuyên bố `10` thắng mọi lần chạy/mọi percentile hoặc warm luôn chậm với mọi workload. Đây là kết quả descriptive với 4 independent blocks/campaign, không có CI/p-value.

## 2. Phần chậm thêm nằm trước khi component output methods bắt đầu

Đọc lại raw/cached decomposition ở **hai primary campaigns có diagnostic observer tắt**. Phân rã cộng đúng với overhead, không cộng chồng nested spans:

| Mean delta 11−10 (ms) | Main | Repeat |
| --- | ---: | ---: |
| Pre-method: middleware/auth/flow/setup/dispatch | +4,294 | +5,423 |
| Khoảng trống giữa methods | +0,064 | +0,214 |
| Sau method cuối | +0,161 | +0,485 |
| Chat Input/Output method bodies | −0,359 | +0,533 |
| **Tổng overhead delta** | **+4,161** | **+6,656** |

Ở main, pre-method tăng lớn hơn tổng gap vì các phần khác bù lại một ít; ở repeat, nó chiếm khoảng 81,5% tổng gap. Pre-method không đồng nghĩa graph-copy: còn bao gồm auth, fetch, tạo job và các bước request trước component. Nhưng đây là bằng chứng từ primary data định vị phần chậm thêm, không chỉ từ instrumentation nặng hơn của D1–D3.

## 3. Cache hit vẫn để lại phần lớn công việc dựng graph/component

Trong từng D1/D2/D3: `11` hit warm **1.000/1.000 measured requests**, zero cold fallback; cả hai arms có **6.000 compilation hits/arm**, zero misses/builds/bypasses/evictions. Cache hoạt động đúng theo cờ.

Compilation cache giữ source artifacts/AST/compiled class code. Mỗi request vẫn phục hồi AST, chuẩn bị globals, thực thi class code và tạo component instance. Xem [create_class](/Users/tranquangtrong/Desktop/langflow_CV/src/lfx/src/lfx/custom/validate.py:369), [class execution](/Users/tranquangtrong/Desktop/langflow_CV/src/lfx/src/lfx/custom/validate.py:787). Vì `10` đã có compilation cache, `11` không được nhận thêm toàn bộ lợi ích compilation một lần nữa.

Warm template là structural graph, constructor được hoãn để bind caller. Trên request hit:

1. [copy_for_run / _copy_graph](/Users/tranquangtrong/Desktop/langflow_CV/src/lfx/src/lfx/graph/graph/base.py:1673) deep-copy raw frontend payload.
2. [add_nodes_and_edges](/Users/tranquangtrong/Desktop/langflow_CV/src/lfx/src/lfx/graph/graph/base.py:368) gọi lại `process_flow`, initialize vertices/edges/params. [process_flow](/Users/tranquangtrong/Desktop/langflow_CV/src/lfx/src/lfx/graph/graph/utils.py:88) cũng deep-copy payload; bước này có trên cả cold và warm reconstruction.
3. [copy_for_run](/Users/tranquangtrong/Desktop/langflow_CV/src/lfx/src/lfx/graph/graph/base.py:1719) vẫn instantiate toàn bộ request-local components.

Do đó bỏ lời gọi có tên `Graph.from_payload` chưa đồng nghĩa bỏ hết parsing, graph reconstruction hoặc component construction. Median class preparation vẫn khoảng 11,5–11,9 ms ở cả hai arms.

| Diagnostic, mean ms | Cold setup 10 | Warm setup 11 | Warm copy 11 | Δ pre-component |
| --- | ---: | ---: | ---: | ---: |
| D1 | 26,446 | 29,264 | 22,608 | +4,162 |
| D2 | 25,554 | 28,224 | 21,526 | +3,410 |
| D3 | 25,174 | 28,533 | 21,956 | +4,788 |

Warm-setup-minus-cold-setup mean dương ở cả 12 diagnostic blocks. Warm copy là parent span có nested constructors; không coi 22 ms là chi phí deepcopy thuần hay cộng 22 ms thêm vào warm setup 29 ms.

## 4. Workload này kích hoạt thêm copy và migration replay

Probe chạy đúng frozen flow payload cho kết quả: ChatInput/ChatOutput là `known_current_component`; **SCRFDPreprocess, SCRFDInference, SCRFDDrawDetections, DetectionImageOutput là `unmapped`**, tổng 4 migration errors, zero rewrites. Đây là kết quả phân loại migration; các custom components vẫn thực thi và output campaign đã được kiểm chứng.

Graph template có errors nhưng không rewrite phải bảo toàn diagnostic semantics. [Warm replay branch](/Users/tranquangtrong/Desktop/langflow_CV/src/backend/base/langflow/api/warm_graph.py:221) chạy `migrate_flow_payload(deepcopy(raw_data))` lại trên mỗi hit, rồi report migration. D1–D3 thực tế có migration_validation/event_replay trên mọi warm request. Warm không tránh được khoản migration khoảng 4 ms/request vốn cũng tồn tại ở cold.

Flow còn có empty global-eligible fields: probe xác nhận `flow_needs_auto_globals=True`; file tweak hợp lệ. [v1 compatibility gate](/Users/tranquangtrong/Desktop/langflow_CV/src/backend/base/langflow/api/warm_graph.py:320) deep-copy flow để apply tweaks và kiểm tra global defaults. Sau đó warm copy lại apply request tweaks lên graph riêng.

Ba điểm full-payload copy trên warm path được xác định trong source: candidate để kiểm tra globals; migration revalidation; `_copy_graph` trước dựng graph. Ngoài ra có copy chung trong `process_flow`. Cold bắt đầu bằng shallow dict copy và migration in-place; global-default helper chỉ copy sâu khi có bindings cần xử lý. Số lần copy toàn bộ runtime còn tùy payload/policy; không gọi ba điểm trên là tổng số mọi deepcopy.

Probe primitives riêng, 5 blocks × 100 randomized/interleaved iterations mỗi operation, GC giữ nguyên:

| Operation | Mean ms | Median ms |
| --- | ---: | ---: |
| Một deepcopy payload | 0,554 | 0,554 |
| Ba deepcopy payload độc lập | 1,644 | 1,643 |
| Migration trên private payload không rewrite | 3,369 | 3,366 |

Đây là chi phí primitives trong process riêng. Nó xác nhận copying có chi phí hữu hình và migration không miễn phí; **không chứng minh bỏ ba copies sẽ giảm đúng 1,644 ms trên API**, cũng không giải thích trọn gap 4–6,7 ms. Probe không chạy DB/server/inference, không thay đổi GC, không patch runtime.

Để không gán toàn bộ warm-copy parent span cho cloning, đọc raw setup spans và trừ **union đã clip trong parent** của class preparation, migration validation và event replay:

| Setup residual, mean ms | Cold 10 | Warm 11 | Delta |
| --- | ---: | ---: | ---: |
| D1 | 2,832 | 6,909 | +4,077 |
| D2 | 2,717 | 5,949 | +3,232 |
| D3 | 2,803 | 5,941 | +3,138 |

Residual gồm copying, graph structure, policy/tweaks/defaults và mọi thời gian chưa instrument trực tiếp; vẫn là wall-clock, có thể có GC/scheduling. Nó chứng minh phần extra setup ngoài ba phase đã đặt tên, chưa định lượng exclusive CPU cho từng function. GC/lag overlap xảy ra cả trên request bình thường nên không đủ để tuyên bố GC là nguyên nhân duy nhất.

## 5. API v1 không nhận lợi ích tránh đọc full Flow.data

Benchmark gọi [/api/v1/run](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runtime.py:374). Dependency [get_flow_for_api_key_user](/Users/tranquangtrong/Desktop/langflow_CV/src/backend/base/langflow/api/v1/endpoints.py:810) đọc flow trước khi `simple_run_flow` chọn cold/warm. [DB helper](/Users/tranquangtrong/Desktop/langflow_CV/src/backend/base/langflow/helpers/flow.py:583) dùng `session.get(Flow, ...)` và tạo FlowRead. Warm không bỏ được đọc full flow của đường này. Diagnostic D1 vẫn đo flow_fetch mean khoảng 1,054 ms ở 10 và 1,262 ms ở 11.

API v2 có [resolve_flow_for_execution](/Users/tranquangtrong/Desktop/langflow_CV/src/backend/base/langflow/api/v2/workflow.py:155) dùng warm resolver trước; [resolver](/Users/tranquangtrong/Desktop/langflow_CV/src/backend/base/langflow/services/warm_registry/resolver.py:97) chọn metadata không có Flow.data để validate snapshot. Đây là một cơ hội lợi ích khác, chưa có số đo trong v5. V5 không đủ để tuyên bố warm vô ích trên v2, nhưng cũng không tạo nghĩa vụ phải giữ feature nếu sản phẩm mục tiêu chỉ dùng v1.

## 6. Định hướng theo mục tiêu latency

1. **Chọn `10` cho workload hiện tại:** `LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED=true`, `LANGFLOW_WARM_REGISTRY_ENABLED=false`. Đây là đề xuất cấu hình; lượt điều tra chưa áp dụng vào môi trường triển khai.
2. **Đưa warm vào diện loại bỏ khỏi phạm vi sản phẩm hiện tại.** Nếu phạm vi chỉ có flow/API v1 này, có cơ sở bỏ warm branch/registry/reconcile cùng cấu hình và tests riêng khi làm task triển khai. Giữ compilation cache, request isolation, auth/policy/global/file semantics và historical benchmark evidence.
3. **Không đổi workload, tắt validation hoặc chọn percentile có lợi để cứu `11`.** Benchmark tiếp tục lưu cả kết quả bất lợi và favorable tails. Bản benchmark cũ vẫn là evidence cho quyết định loại bỏ; bản sau thay đổi dùng namespace/source mới.
4. **Chỉ giữ opt-in cho một use case đã xác định**, như v2 full-data fetch, nếu use case đó thuộc roadmap thực. Giữ feature vì “có thể một ngày sẽ nhanh hơn” không đủ. Cần tiêu chí latency mục tiêu đặt trước khi đo, trade-off RSS/complexity và kết quả lặp lại. Không mở thêm chiến dịch benchmark chỉ để bảo vệ warm.
5. Nếu KPI duy nhất là p99/SLO tail, quyết định cần đọc p99 **total latency** tương ứng: p99 overhead `11` thấp hơn ở hai primary runs nhưng p99 server total ở repeat là 425,719 ms (`11`) so với 425,198 ms (`10`), nên không thể đồng nhất hai mục tiêu. Đề xuất chọn `10` hiện dựa trên mean/p50/p95 nhất quán của primary runs.

RSS after-idle trung bình của `10` cao hơn `11` ở hai primary runs, nhưng độ lớn và dấu theo block không ổn định; đây là whole-worker RSS, chưa gán cho cache. Vì vậy `10` được đề xuất theo mục tiêu latency hiện tại, không được mô tả là tối ưu mọi chiều tài nguyên.

## Evidence và phạm vi kiểm chứng

- [Script điều tra](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/investigation-11-vs10-2026-10-04/investigate.py), [thiết kế probe trước khi đo](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/investigation-11-vs10-2026-10-04/PROBE_DESIGN.md), [evidence JSON với raw primitive samples và input hashes](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/investigation-11-vs10-2026-10-04/evidence.json), [log](/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/investigation-11-vs10-2026-10-04/probe.log).
- Source vẫn `cd289b72c133fc493527b312ee16cba2bd9e1976f4f5ddcb51e282f3a323a1e4`. Mọi consumed raw hash khớp cached analysis; script kiểm tra source/input bytes trước/sau. Exit 0. Không sửa runtime, raw v5, cached reports hoặc acceptance receipt.
- Chưa làm intervention end-to-end bỏ từng copy, chưa đo job/DB exclusive time, chưa benchmark v2. Những phần này không được trình bày là đã chứng minh. Định hướng loại warm khỏi workload hiện tại không cần chứng minh từng nanosecond: primary latency bất lợi lặp lại cộng với cơ chế extra work đã đủ để ngừng coi `11` là ứng viên mặc định tốt nhất.
