# Worker prompt: benchmark MAIN với COMPILE-ONLY end-to-end

## Vai trò và mục tiêu

Bạn đóng vai **WORKER**, thực hiện end-to-end phép benchmark trong repository:

```text
/Users/tranquangtrong/Desktop/langflow_CV
```

Mục tiêu: xác định **compilation cache có thực sự giảm latency so với code nguyên bản của nhánh main đã warm up hay không**, đồng thời đo trade-off bộ nhớ.

Hoàn thành việc chuẩn bị code, sửa harness nếu cần, kiểm thử, chạy benchmark thật, kiểm tra kết quả và xuất báo cáo. Không dừng ở kế hoạch hoặc chỉ chạy tests.

## 1. Chuẩn bị hai phiên bản độc lập

- Đọc `AGENTS.md` và các hướng dẫn liên quan.
- Kiểm tra working tree; bảo toàn mọi thay đổi đang có.
- Resolve và ghim một commit của nhánh `main` **LOCAL** làm baseline. Ghi rõ commit; không tự thay bằng `origin/main` hoặc cập nhật main giữa quá trình.
- Tạo hai checkout/worktree riêng:

| Nhóm | Source | Compilation cache | Warm graph |
| --- | --- | --- | --- |
| **MAIN** | Đúng source của commit main đã ghim | Luồng gốc của main | Tắt |
| **COMPILE** | Chính commit đó + thay đổi cần thiết cho compilation cache | Bật | Tắt |

- Dùng prefix `codex/` cho branch mới.
- Không cherry-pick toàn bộ feature branch nếu nó kéo theo thay đổi warm graph không liên quan.
- Warm graph phải **OFF ở cả hai nhóm**. Không đưa các cải tiến warm graph của feature branch vào COMPILE. Nếu main vốn có code warm graph thì giữ baseline nguyên bản và tắt tính năng bằng cấu hình; không xóa riêng ở một phía làm phát sinh biến số khác.
- Review diff MAIN→COMPILE để xác nhận phép so sánh chỉ khác cơ chế compilation cache.

> **Lưu ý quan trọng:** `00` hiện tại không mặc nhiên tương đương main. Cache OFF vẫn đi qua compiler refactor, tạo artifact và `pickle.dumps(AST)`, trong khi main dùng luồng cũ. Không dùng `00` rồi đổi tên thành MAIN.

## 2. Chuẩn bị bộ đo công bằng

- Harness hiện tại chủ yếu so các cờ trên cùng source. Điều chỉnh để mỗi nhóm thật sự chạy đúng checkout và ghi source identity riêng.
- Instrumentation phải có cùng ranh giới đo và chi phí tương đương ở cả hai nhóm; không mang compiler refactor/artifact serializer vào MAIN để làm harness chạy được.
- Kiểm tra đường dẫn module Python thực sự được import, interpreter và dependency versions, tránh worker MAIN vô tình import source từ feature checkout.
- Cùng flow, ảnh, model, dependencies, cấu hình runtime, **một worker và concurrency = 1**.
- Mỗi slot dùng worker mới và bản sao DB/storage từ cùng fixture đã đóng băng.
- Có thể tái sử dụng fixture/reference đã kiểm chứng nếu còn nguyên hash; không sửa dữ liệu gốc hoặc làm lộ credentials.
- Upload/download, kiểm tra output, snapshot counters và thu thập RSS phải nằm ngoài khoảng timing tương ứng.
- Chứng minh COMPILE có cache hits sau warmup và warm graph tắt ở cả hai. Nếu main không có compilation counters, ghi rõ `unavailable`/`not applicable`; không giả lập zero hoặc đưa implementation cache vào main.
- Ghim workload/source/profile trước đo. Smoke gate phải khớp source của từng nhóm; không yêu cầu source MAIN và COMPILE giống nhau.

## 3. Kiểm thử và smoke

- Chạy format/lint/tests phù hợp cho compilation cache và harness đã thay đổi.
- Kiểm tra source identity, output correctness, timing boundary, worker cleanup, chống ghi đè output và smoke gate.
- Chạy smoke thật cho cả MAIN và COMPILE.
- Chỉ chạy campaign dài khi cả hai smoke hợp lệ.
- Nếu phát hiện lỗi collector/harness, giữ nguyên attempt lỗi, sửa và chạy smoke mới trên source mới. Không sửa raw, bỏ mẫu xấu hoặc nới validator để làm kết quả hợp lệ.

## 4. Chạy benchmark mới từ đầu

### Thiết kế cố định

| Thông số | Giá trị |
| --- | --- |
| Nhóm so sánh | MAIN và COMPILE |
| Blocks mỗi lượt | 4 |
| Measured requests mỗi nhóm mỗi block | 250 |
| Measured requests mỗi nhóm mỗi lượt | 1.000 |
| Warmup requests mỗi worker | 5, không tính vào thống kê steady-state |
| Số lượt chính | 2: campaign chính và một lượt repeat hoàn toàn mới |
| Tổng measured requests của hai lượt chính | **4.000**, chưa tính smoke và warmup |

### Thứ tự cân bằng trong mỗi lượt

| Block | Thứ tự |
| --- | --- |
| 1 | MAIN → COMPILE |
| 2 | COMPILE → MAIN |
| 3 | COMPILE → MAIN |
| 4 | MAIN → COMPILE |

Lặp lại toàn bộ campaign một lần với các worker/fixture clones mới.

### Quy tắc thực thi

- Không chạy tests, analyzer nặng hoặc các probe cạnh tranh CPU trong lúc đo.
- Không chạy lại ma trận bốn cơ chế hoặc D0–D3: thí nghiệm này chỉ so MAIN và COMPILE.
- Dùng namespace output mới, kiểm tra tất cả destinations trước khi ghi. Bảo toàn toàn bộ raw/report v3/v4/v5.
- Dừng khi có lỗi validity; không tiếp tục campaign sau một stage thất bại.
- Chỉ dừng/cleanup các process và scratch do chính lượt này tạo.

## 5. Phân tích và báo cáo

Xuất báo cáo HTML so sánh rõ hai phiên bản, kèm Markdown, CSV, biểu đồ, manifests và verification receipt.

Báo cáo cần có:

- Mean, p50, p95, p99 của server latency và client API latency; định nghĩa rõ ranh giới.
- Langflow overhead để giải thích kết quả, không dùng thay thế latency thực tế.
- Chênh lệch tuyệt đối và phần trăm COMPILE−MAIN, theo từng block và từng lượt.
- Chi phí warmup.
- Whole-worker RSS tại `before_warmup`, `after_warmup`, `after_measurement` và `after_idle`.
- USS nếu thu được; unavailable phải giữ `null`/reason.
- Trade-off latency–RAM; không gọi RSS là dung lượng cache riêng hoặc peak RAM.
- Kiểm tra output, cache behavior, source/workload hashes và độ nhất quán của repeat.
- Nêu rõ bốn blocks là đơn vị lặp; không coi 1.000 requests là 1.000 thí nghiệm độc lập.
- Giữ mọi kết quả bất lợi. COMPILE không nhanh hơn vẫn là một kết quả hợp lệ; không đổi tiêu chí sau khi xem số liệu.

Kiểm tra links, CSV và PNG; render HTML nếu công cụ hỗ trợ. Ghi đúng phần đã và chưa kiểm chứng.

## 6. Nghiệm thu và bàn giao

- Tự review toàn bộ thay đổi và kết quả; sửa các lỗi thực sự trước khi bàn giao.
- Xác minh source không đổi trong campaign, raw hashes khớp, dữ liệu lịch sử nguyên vẹn và worker đã cleanup.
- Báo rõ commit/source của hai nhóm, tests đã chạy, số mẫu thực tế, trạng thái từng stage và hạn chế.
- Kết luận theo số liệu: compilation cache có đáng giữ cho mục tiêu latency hiện tại không, lợi ích bao nhiêu và đánh đổi RAM thế nào.
- Cung cấp link tới HTML chính, repeat, báo cáo tổng hợp và verification receipt.
- Không push, merge hoặc deploy.

## Quyền chủ động

Bạn được chủ động thực hiện các bước cần thiết trong phạm vi này. Cập nhật tiến độ ngắn gọn, không hỏi lại các quyết định đã chốt. Chỉ yêu cầu thông tin khi có blocker thực sự không thể giải quyết từ repository và dữ liệu hiện có.
