# Run1 tables revision — PASS

- Updated the existing original-style HTML in place; original input and benchmark files remain untouched.
- Latency table retains only first-campaign Server/Client API, two rows and seven columns. Both result tables have no run column.
- RAM table replaced with one header/value: +87.102 MiB/worker (+8.10%). Note identifies after-idle checkpoint after five seconds, average four worker processes per group, whole-worker RSS, variation and unavailable USS.
- Counts/intro/conclusions align with displayed first campaign: 2000 measured, 40 warmup, eight worker generations, block4 server/client p95 +7.50/+7.59. Source evidence and original both-campaign retention criterion remain referenced.
- Original CSS and JavaScript unchanged byte-for-byte from html_before.html; compilation slides 2–7 unchanged.
- Eighteen rendered values validated against existing full-precision CSV; 60 source comparison rows independently recomputed. No benchmark or backend suites run.
- All ten slides rendered at desktop 1440×1000 and small 390×844, light/dark: 40 slide renders plus fullscreen/document/right-edge/reference images. Controls, keyboard, count/progress, edit/lock, theme, document view and fullscreen passed.
- All links resolve from delivery file:// path; campaign receipt clicked and opened in browser. Console/page errors and failed requests: zero.
- Root inspected all light slides via contact sheets plus benchmark/conclusion detail; independent reviewer inspected dark benchmark/mobile/rightmost columns/conclusion. PASS, no blockers.
- Fresh hash audit preserves 113 protected files, including source/data, original/Downloads, earlier HTML and previous QA receipt/snapshot. Git status and branch remain unchanged.
- Codex preview request returned queued; headless Chrome render verified independently.
- Limits: fullscreen tested in headless Chrome rather than macOS window chrome; RSS checkpoint is not peak or isolated cache footprint.
