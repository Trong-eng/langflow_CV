# Benchmark bộ nhớ đệm cho dữ liệu trung gian khi biên dịch component

## Phạm vi và khả năng chạy lại

- Repository: `/Users/tranquangtrong/Desktop/langflow_CV`
- Nhánh: `codex/cache-component-compilation-artifacts`
- SHA baseline: `c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d`
- SHA của phần triển khai dùng để đo OFF/ON: `b133c5035`
- Môi trường: Python 3.13.14, macOS 26.6.2 arm64, một worker
- Mẫu đo độ trễ: 40 lần evaluate component và 15 lần chạy graph, sau 5 vòng warm-up
- Workload đồng thời: 32 request trên 8 thread; 15 batch để tính p50/p95 cho trường hợp source khác nhau
- Workload bộ nhớ: 128 source khác nhau, đo trong một process riêng
- Component benchmark: pass-through component chạy local; không gọi model, network hoặc dịch vụ bên ngoài

Cùng một harness `scripts/benchmarks/benchmark_component_compilation_cache.py` đã tạo ra ba file `baseline.json`, `off.json` và `on.json` được commit trong repository. Mỗi file JSON ghi `metadata.source_revision`. Harness không nhận revision do người chạy tự gắn nhãn: nó đọc Git HEAD hoặc revision manifest do baseline wrapper tạo ra. Chạy `scripts/benchmarks/run_component_compilation_cache_baseline.sh` để tạo một `git archive` tạm thời từ SHA baseline cố định và chạy bản sao của harness trong đó. Chế độ baseline sẽ từ chối mọi revision khác. Hai cấu hình OFF/ON được đo tại implementation SHA `b133c5035`. Cách này không làm thay đổi checkout hiện tại và ngăn việc gắn nhầm code hiện tại thành baseline.

## Đường thực thi

Trước khi có cache:

```text
resolve trusted source -> parse/validate/compile -> namespace/exec mới -> class mới -> instance mới
```

Sau khi bật tính năng:

```text
resolve trusted source -> tạo key + tra cache
                         | miss: parse/validate/compile -> lưu source artifact
                         | hit:  nạp source artifact
                       -> AST mới -> namespace/exec mới -> class mới -> instance mới
```

Việc resolve trusted source và các policy check cho phép thực thi code vẫn diễn ra trước bước evaluate. Cache hit không tái sử dụng execution namespace, class object, component instance, parameters, user/session binding, runtime output hoặc tracing state. Imports, helper được định nghĩa trong source, class body, decorator, annotation registration và constructor vẫn chạy lại ở mọi lần evaluate.

## Thiết kế cache

Artifact được cache chỉ gồm:

- tên class component được trích xuất từ source;
- AST template bất biến đã được serialize; mỗi cache hit sẽ deserialize thành một AST mới;
- compiled code object của class đích;
- metadata của trusted vector-store decorator đã được chứng minh bằng phân tích tĩnh.

Cache key là `(artifact generation, SHA-256(full resolved source), class-selection variant)`. Cache entry và artifact đều giữ và so sánh exact source; lúc dùng artifact cũng kiểm tra generation. Vì vậy, kể cả khi digest bị collision hoặc code nội bộ ghép nhầm artifact với source khác, artifact sai cũng không được thực thi. Source hoặc generation thay đổi sẽ tự tạo cache miss.

Cache chỉ tồn tại trong process hiện tại, được bảo vệ bằng `RLock` và dùng LRU tối đa 128 entry. Source lớn hơn 262.144 byte UTF-8 sẽ bỏ qua cache. Bước chuẩn bị khi miss được tuần tự hóa để tránh nhiều thread cùng compile một source; các bước import lúc runtime, `exec`, tạo class và tạo component nằm ngoài lock. Lỗi validation/compilation không được đưa vào cache. `clear_component_compilation_cache()` xóa artifact, counter và cả snapshot của feature setting trong process.

Tính năng mặc định tắt. Bật bằng:

```bash
LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED=true make backend
```

Tắt hoặc rollback bằng `LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED=false`, cũng là giá trị mặc định. Worker đang chạy chỉ đọc setting một lần; sau khi đổi biến môi trường, cần restart worker hoặc gọi clear hook nội bộ trong test/lifecycle code.

## Kết quả

Phần trăm được tính so với baseline. Số âm nghĩa là nhanh hơn hoặc dùng ít tài nguyên hơn. Nếu không ghi chú khác, đơn vị là mili giây.

### Evaluate lặp lại cùng một source

| Chỉ số | Baseline | Cache OFF | OFF so với baseline | Cache ON | ON so với baseline |
|---|---:|---:|---:|---:|---:|
| Hoạt động parse/compile p50 | 0.439 | 0.395 | -10.0% | 0.282 | -35.8% |
| Hoạt động parse/compile p95 | 0.452 | 0.410 | -9.2% | 0.291 | -35.5% |
| Tạo namespace/class p50 | 1.247 | 1.257 | +0.8% | 0.979 | -21.5% |
| Tạo namespace/class p95 | 1.335 | 1.357 | +1.7% | 1.068 | -20.0% |
| Constructor p50 | 0.092 | 0.090 | -1.8% | 0.087 | -5.4% |
| Constructor p95 | 0.099 | 0.099 | +0.7% | 0.094 | -4.9% |

Probe parse/compile được gọi là “hoạt động tổng quát” vì nó bao gồm cả phần xử lý annotation lúc runtime, nhưng không bao gồm hashing, duyệt validation, serialize và chi phí LRU. Probe riêng cho implementation đo được:

- Cache OFF: build artifact mất 0.324 ms ở p50 và 0.343 ms ở p95, tổng cộng 41 lần build.
- Cache ON: lần build đầu tiên mất 0.323 ms.
- Cache hit: khôi phục AST mất 0.077 ms ở p50 và 0.091 ms ở p95, tổng cộng 40 lần khôi phục.

Baseline chưa có một phase artifact tách riêng, nên các field phase-probe của baseline được ghi là 0 thay vì đưa ra một số ước lượng dễ gây hiểu nhầm.

Constructor không bao giờ bị bỏ qua; chênh lệch nhỏ của chỉ số constructor chỉ là nhiễu giữa các lượt chạy, không phải công việc được cache. Ở cache miss đầu tiên khi cache bật, hoạt động parse/compile tổng quát mất 0.433 ms, tạo class mất 1.378 ms và tạo instance mất 0.122 ms. Workload steady ghi nhận 1 build, 1 miss và 45 hit, có tính cả warm-up/probe. Cả 40 lần đo đều tạo class identity khác nhau.

Với các source khác nhau, tạo class khi cache ON có p50 là 1.205 ms. Cập nhật source trong cùng worker mất 1.116 ms và tạo đúng một miss/artifact mới.

### Chuẩn bị graph

| Tải kiểm thử | Phân vị | Baseline | Cache OFF | OFF so với baseline | Cache ON | ON so với baseline |
|---|---|---:|---:|---:|---:|---:|
| Chuẩn bị cold graph 10 node | p50 | 52.196 | 56.290 | +7.8% | 49.971 | -4.3% |
| Chuẩn bị cold graph 10 node | p95 | 52.729 | 63.339 | +20.1% | 51.256 | -2.8% |
| Chuẩn bị warm graph 10 node | p50 | 13.644 | 13.866 | +1.6% | 11.509 | -15.7% |
| Chuẩn bị warm graph 10 node | p95 | 13.748 | 14.617 | +6.3% | 11.724 | -14.7% |
| Chuẩn bị cold graph 100 node | p50 | 526.278 | 538.516 | +2.3% | 497.054 | -5.6% |
| Chuẩn bị cold graph 100 node | p95 | 536.193 | 551.765 | +2.9% | 503.536 | -6.1% |
| Chuẩn bị warm graph 100 node | p50 | 134.596 | 140.156 | +4.1% | 113.938 | -15.3% |
| Chuẩn bị warm graph 100 node | p95 | 143.670 | 154.756 | +7.7% | 115.072 | -19.9% |

### Tổng thời gian chạy flow

| Tải kiểm thử | Phân vị | Baseline | Cache OFF | OFF so với baseline | Cache ON | ON so với baseline |
|---|---|---:|---:|---:|---:|---:|
| Cold flow 10 node | p50 | 54.928 | 59.769 | +8.8% | 52.653 | -4.1% |
| Cold flow 10 node | p95 | 55.631 | 66.751 | +20.0% | 54.006 | -2.9% |
| Warm flow 10 node | p50 | 16.359 | 16.750 | +2.4% | 14.135 | -13.6% |
| Warm flow 10 node | p95 | 16.586 | 17.473 | +5.3% | 14.477 | -12.7% |
| Cold flow 100 node | p50 | 580.677 | 595.027 | +2.5% | 551.222 | -5.1% |
| Cold flow 100 node | p95 | 592.103 | 609.806 | +3.0% | 557.627 | -5.8% |
| Warm flow 100 node | p50 | 188.281 | 194.940 | +3.5% | 168.124 | -10.7% |
| Warm flow 100 node | p95 | 198.989 | 213.679 | +7.4% | 172.828 | -13.1% |

Tải kiểm thử graph 10 node khi bật cache ghi nhận 1 build, 1 miss và 319 hit. Tải kiểm thử 100 node ghi nhận 1 build, 1 miss và 3.199 hit. Bản sao warm graph vẫn tạo instance mới cho từng component ở mỗi lần chạy.

### Chạy đồng thời, probe compile và CPU profile

| Chỉ số | Baseline | Cache OFF | OFF so với baseline | Cache ON | ON so với baseline |
|---|---:|---:|---:|---:|---:|
| Tổng thời gian 32 request cùng source | 38.822 | 38.969 | +0.4% | 32.139 | -17.2% |
| 32 request có source khác nhau, p50 | 38.529 | 38.250 | -0.7% | 38.765 | +0.6% |
| 32 request có source khác nhau, p95 | 40.202 | 39.305 | -2.2% | 42.550 | +5.8% |
| Số lần probe parse/compile trong 40 steady evaluation | 4.880 | 4.840 | -0.8% | 4.760 | -2.5% |

Lượt chạy đồng thời cùng source với cache ON có 1 build, 1 miss, 31 hit và 32 class identity khác nhau. Mỗi batch source khác nhau có 32 build/miss; global miss lock làm p95 tăng 5.8%, còn p50 gần như không đổi. Số đếm probe tổng quát vẫn bao gồm bước resolve annotation lúc runtime vì phần này bắt buộc chạy ở mỗi request. Số lần build source artifact chỉ giảm còn một trong workload lặp lại cùng source. CPU profile khi cache ON cho thấy phần lớn thời gian còn lại nằm ở `prepare_global_scope` và snapshot annotation lúc runtime; vì vậy cache hit không thể loại bỏ phần lớn chi phí tạo component.

### Bộ nhớ

| Chỉ số với 128 source khác nhau | Baseline | Cache OFF | Cache ON | ON so với baseline |
|---|---:|---:|---:|---:|
| Chênh lệch RSS | 901.120 B | 868.352 B | 2.392.064 B | +165.5% |
| Chênh lệch traced allocation | 1.323.586 B | 987.930 B | 2.370.081 B | +79.1% |

Khi bật cache, traced allocation tăng khoảng 18,1 KiB cho mỗi entry nếu cache đầy 128 entry. RSS phụ thuộc vào allocator và trạng thái process, vì vậy đây chỉ là ước lượng cho workload benchmark có giới hạn, không phải cam kết sizing cho production.

## Đánh giá

Cache cải thiện các workload lặp lại cùng source trong lần đo này: warm flow 100 node nhanh hơn 10.7% ở p50 và 13.1% ở p95; workload đồng thời cùng source nhanh hơn 17.2%. Tuy nhiên, cache không cải thiện phần đuôi của workload nhiều cache miss: p95 khi các source đều khác nhau chậm hơn 5.8%, qua đó cho thấy rõ rủi ro của global miss lock. Cache OFF cũng dao động tới 20% ở kết quả p95 của flow 10 node rất ngắn. Vì vậy, các chênh lệch trong workload nhỏ nên được xem là tín hiệu cần theo dõi, không nên kết luận chỉ từ một lượt benchmark.

Khuyến nghị: tiếp tục để cache mặc định tắt và chỉ bật thử theo kiểu canary ở những deployment có nhiều request dùng source component giống nhau và có mức tái sử dụng warm graph đáng kể. Trước khi triển khai rộng, cần theo dõi RSS của worker, cache hit rate và tail latency của flow nhỏ. Các rủi ro còn lại gồm hit rate phụ thuộc workload, bước chuẩn bị cache miss bị tuần tự hóa khi nhiều source khác nhau đến đồng thời, bộ nhớ cache nhân theo số worker và yêu cầu tăng `COMPONENT_COMPILATION_ARTIFACT_GENERATION` mỗi khi logic compiler/validation thuần source thay đổi.
