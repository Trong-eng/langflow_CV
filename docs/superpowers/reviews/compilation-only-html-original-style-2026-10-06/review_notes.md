# Verification — giữ bố cục bản cũ

- HTML: /Users/tranquangtrong/Desktop/langflow_CV/optimize_compilation_cache_langflow_CV_original_style.html
- Kết quả: PASS; 10 slides, giữ thứ tự các slide gốc 1, 2, 4, 5, 6, 7, 13, 14, 15, 16. Bỏ các slide warm graph 3, 8–12.
- CSS gốc được giữ byte-for-byte làm prefix; chỉ bổ sung điều chỉnh viewport nhỏ để chữ/code không tràn. Nội dung slide Feynman và các code blocks compilation giữ nguyên byte-for-byte. Các controls gốc được giữ.
- Phần benchmark cuối giữ bố cục cards/bảng; cập nhật hai lượt MAIN–COMPILE mean/p95 và thêm bảng RSS trong cùng slide. Bảng RSS nêu rõ mức tăng sau idle 87.102/206.668 MiB (8.10%/23.13%), checkpoint toàn worker, biến thiên giữa blocks, USS unavailable/AccessDenied; không diễn giải là peak hoặc dung lượng riêng cache.
- Mục ngắn loại warm graph dẫn v5/repeat và D1. copy_for_run vẫn sao chép dữ liệu graph, dựng nodes/edges và khởi tạo component; span có class preparation, không phải deepcopy thuần và không chứng minh nguyên nhân duy nhất.
- Đối chiếu 58 giá trị HTML với CSV đầy đủ và tính lại 60 dòng delta. Kiểm tra source pins, patch counts, cache counters, bất lợi block 4 và giới hạn workload.
- 29 links / 23 đích tồn tại và resolve từ vị trí file:// HTML; campaign receipt mở được qua thao tác click. Link source COMPILE trỏ tới worktree đã đo.
- Render 10 slides × 2 themes × 2 viewports = 40 ảnh. Root kiểm tra toàn bộ light; reviewer độc lập kiểm tra toàn bộ dark, zoom code/benchmark và cột phải bảng mobile. Không thấy lỗi cắt chữ, chồng nội dung hay tràn ngang toàn trang. Bảng mobile cần cuộn ngang trong container; slide dài cần cuộn dọc.
- Kiểm tra nút/phím trước-sau, Space, PageUp/Down, Home/End, progress/counts, xem tất cả/M, sáng/tối, fullscreen button/F và nút sửa/khóa số liệu (không đổi giá trị). Fullscreen được kiểm tra trong headless Chrome, chưa xác nhận macOS display chrome.
- Console/page errors: 0; network failures: 0.
- Hash trước/sau: 111 files được bảo vệ không đổi, gồm HTML gốc, Downloads, source/benchmark/receipt và bản HTML 14 slides trước đó. Original SHA256: f301f215b3253e7e8e77d185f5b84a49253fffe70936644b55ad9a1a0ec63107. Nhánh add_worker_warm_graph giữ nguyên; git status chỉ thêm HTML suffix mới.
- Không sửa source/dữ liệu benchmark, không chạy benchmark hoặc suite backend.
- Yêu cầu mở bản mới trong Codex đã trả về queued; không khẳng định panel đã hiển thị.
