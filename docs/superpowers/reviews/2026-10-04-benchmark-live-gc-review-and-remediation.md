# Live reviewer findings: GC callback reentry in diagnostic collector

Ngày 2026-10-04. User đã yêu cầu worker hoàn tất readiness conditions và chạy full pipeline thật.

## Lượt v4 dừng đúng gate

Source collector v4: `b5a5f3383ecb84b8c0d87b25c607a403872d1d2c5acdc9b8e97ea6a673505f06`. Primary smoke và diagnostic smoke mới đều COMPLETE/VALID. Diagnostic-v4 đã thu đủ 8.000 measured samples; D0/D2/D3 VALID, D1 có latency/output/RSS hợp lệ nhưng diagnostic evidence INVALID. Helper dừng với exit 2 từ suite; main/repeat chưa chạy.

Raw D1 rows 27775–27776 chứa cùng ID `31321:5528`, cùng request/parent/thread: GC generation0 trước, rồi setup `global_defaults`. Tổng 89.019 events, 89.018 unique IDs. Validator từ chối duplicate và drained-count mismatch. Không sửa raw, bỏ event, đổi manifest/source hoặc nới validator để làm lượt này VALID.

## R11 — ID reservation bị GC chen vào

`start_event` tăng shared `_next_id`, sau đó đọc lại shared counter để format ID. `RLock` cho phép GC callback cùng thread reenter: callback cấp ID tiếp theo và hoàn tất, outer event đọc counter đã tăng rồi dùng cùng ID. Trace có real `gc.collect(0)` giữa hai bước tái hiện đúng cặp GC/setup trùng ID.

Fix: `itertools.count(1)` cấp ticket vào local variable trước khi format. Callback có thể lấy ticket khác; outer event giữ ticket riêng.

Regression: `test_gc_reentry_during_id_formatting_keeps_both_events_unique` RED (hai events, một unique ID) → GREEN. Tracer chỉ kích hoạt ở setup `global_defaults` để GC tự động không lấy mất one-shot của test; tracing/callback state được phục hồi.

## R12 — Drain có thể mất event cùng cơ chế reentry

`drain` copy active deque rồi clear chính deque đó. Real GC giữa copy và clear append completed event vào deque; clear xóa event mới, trong khi counters không báo dropped/truncated. Repro có một setup event ban đầu và một real-GC event: trước fix, lần drain tiếp theo rỗng dù collector đã cấp hai ID.

Fix: detach deque trước khi materialize list; callbacks sau handoff append vào deque mới và được giữ cho drain tiếp theo. Callback trong lúc tạo deque mới vẫn append vào deque cũ đang được giữ để thu. Giữ lock và bounded collector hiện có.

Regression: `test_gc_reentry_after_drain_copy_keeps_event_for_next_drain` RED (GC event mất) → GREEN (GC event ở drain sau, total_drained=2).

**Không có bằng chứng silent drain loss xảy ra trong slot D1 thực tế.** Drained-count mismatch đã được giải thích bởi duplicate ID bị validator bỏ khỏi bảng unique events; R12 là lỗi độc lập được phát hiện và tái hiện khi điều tra cùng cơ chế.

## Kiểm chứng và pipeline mới

- Observer suite: 12 passed.
- Whole benchmark suite cuối: **334 passed, 8 existing deprecation warnings, 36,31s, exit 0** ngoài sandbox. Lượt trong sandbox có localhost/process PermissionErrors; giữ log đó và đã chạy lại với quyền phù hợp.
- Ruff hai file thay đổi pass; `git diff --check` sạch.
- Independent actual-code stress: 20.000 setup spans, **80.586 allocated IDs = 80.586 emitted events**, trong đó 60.586 GC events; zero duplicate/dropped/truncated/unfinished. GC thresholds/callbacks/trace/enabled state được phục hồi.
- Frozen execution source mới: `cd289b72c133fc493527b312ee16cba2bd9e1976f4f5ddcb51e282f3a323a1e4`.

R11/R12 CLOSED về code và regression evidence; live acceptance của source mới được kiểm tra bằng **pipeline v5 hoàn toàn mới**, bắt đầu từ cả hai smoke. Đây là experiment mới sau collector correction, không retry để thay bad samples trong v4. Giữ fixture/workload cũ đã hash; không đổi primary timing, runtime cache semantics hoặc validators.

Bằng chứng: [test receipt](../../../benchmark_analyst/runs/implementation-v5/TEST_VERIFICATION.json), [GC fix ledger](../../../benchmark_analyst/runs/implementation-v5/GC_COLLECTOR_FIX.md), [live orchestration](../../../benchmark_analyst/runs/implementation-v5/orchestration.json). Trạng thái tại thời điểm ghi: v5 đang chạy; tài liệu này chưa xác nhận campaign v5 hoàn tất.

## Closure sau full live pipeline v5

2026-10-04 09:50:49 UTC: **v5 đã COMPLETE/VALID 5/5 stage, orchestration exit 0; final acceptance PASS.** Hai smoke mới, full diagnostic D0–D3, full main và full repeat đều dùng frozen source `cd289b72…` đã qua 334 benchmark tests. Tổng 16.096 measured requests, 800 warmup requests, 448 RSS checkpoints. Sáu smoke gates fresh cùng source/profile/workload pass; raw hashes và 210 historical files giữ nguyên.

R11/R12 có live acceptance ở source mới; v4 vẫn INVALID và được bảo toàn. Post-run verifier sửa một giả định schema ở helper ignored: suite D0 ghi `diagnostic_events.jsonl:null` cho file absent, child analyzer bỏ absent file. Chỉ cho phép đúng extra key/null đó, kiểm tra path absent và mọi present/shared digest. Không sửa raw, cached analyses hay core validator.

Evidence: [final receipt](../../../benchmark_analyst/runs/implementation-v5/FINAL_VERIFICATION.json), [findings và report links](../../../benchmark_analyst/runs/implementation-v5/FINDINGS.md). `10` tốt hơn mean/p50/p95 overhead ở cả main và repeat; `11` có p99 overhead thấp hơn. RSS là whole worker, chưa có cache-owned heap/peak RAM hay causal claim. Browser layout chưa được render; HTML links/CSV/PNG kiểm tra tĩnh và hai primary tradeoff figures đã xem trực tiếp.
