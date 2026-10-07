# Overhead điều phối Langflow / SCRFD

**VALID · CAMPAIGN — 1.000 request đo/arm**

Experiment: main-v2

langflow_overhead_ms = server_total_ms - scrfd_processing_ms. server_total_ms là wall-clock trong worker từ điểm vào middleware benchmark của /run đến khi gửi xong response body. scrfd_processing_ms là độ dài hợp các khoảng output method của bốn node SCRFD, không cộng hai lần phần chồng lấn. Upload, tải ảnh kết quả và toàn bộ xử lý SCRFD nằm ngoài chỉ số chính. Chat Input/Output, tạo graph/component, validation, auth bên trong middleware và serialize response vẫn được tính. Đây không phải CPU time hay latency end-user.

Ranh giới ghi trong manifest: Outer application ASGI entry through final response body send, including all Langflow middleware and HTTP telemetry, excluding post-response background work, minus union of SCRFD output method bodies

## Tính hợp lệ

Đủ số mẫu theo manifest; output, timing, cờ và counter trong worker đều đạt kiểm tra.

Latency/cache/output: VALID; RAM: unavailable; diagnostic observer: unavailable; overall: VALID.

## Thiết kế và cách đọc

Arm có bit thứ nhất = compilation cache, bit thứ hai = warm graph registry; 1 bật, 0 tắt. Legacy HTTP-reuse flag tắt; requests.Session vẫn reuse TCP. Mỗi slot dùng một worker độc lập, concurrency 1. Thiết kế khai báo 1000 mẫu đo/arm, bốn block, 5 warmup/slot. Warmup không đi vào thống kê độ trễ.

p50/p95/p99 dùng nội suy tuyến tính (Hyndman-Fan type 7). Mỗi request có trọng số bằng nhau; các block cân bằng nên mean tổng hợp cũng bằng mean của các mean block. Các percentile mô tả phân phối mẫu, không phải khoảng tin cậy.

## Chỉ số chính: Langflow overhead (ms)

| Arm | Đã thử | Output hợp lệ | n timing | Mean | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 00 | 1000 | 1000 | 1000 | 63.528 | 55.177 | 86.800 | 156.122 |
| 01 | 1000 | 1000 | 1000 | 63.828 | 57.180 | 77.953 | 193.858 |
| 10 | 1000 | 1000 | 1000 | 54.065 | 44.675 | 70.277 | 423.070 |
| 11 | 1000 | 1000 | 1000 | 57.437 | 48.979 | 68.610 | 370.681 |

![Phân phối, block và chuỗi request của overhead](charts.png)

## Trade-off latency và RAM worker

Phạm vi một flow SCRFD, một serving worker, concurrency 1. RSS/USS là RAM toàn worker; chưa đo peak RAM. before_warmup là sau readiness và workload verification, không phải process hoàn toàn cold. after_idle là sau ít nhất thời gian idle khai báo; background tasks vẫn chạy và checkpoint không chứng minh quiescence.

Không có SLA hoặc ngân sách RAM để chọn winner chung. Mean/median và p95/p99 là các mục tiêu khác nhau; đọc chênh lệch theo block trước khi xếp hạng. Payload cache không đo deep heap và không quy bytes RAM cho từng cache.

RAM chưa đo (legacy / measurement disabled); RSS và USS unavailable, không chuyển thành zero.

## Quan sát 11 so với 10 và phần chưa giải thích

Phân rã method spans: measured; 4000 request có đủ sáu span tuần tự. pre + gaps + post + Chat Input/Output = overhead. Missing/overlap chỉ làm phân rã unavailable; metric union SCRFD vẫn được kiểm tra riêng. Đoạn pre gồm middleware/auth/flow/setup/dispatch, chưa phải bằng chứng quy cho deepcopy.

| Block | Δmean | Δp50 | Δp95 | Δp99 | Δpre | Δgaps | Δpost | Δchat |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | -1.236 | 1.536 | -9.337 | -181.854 | 0.965 | -1.191 | -0.307 | -0.704 |
| 2 | 3.070 | 2.907 | 4.642 | -1.762 | 3.492 | -0.088 | -0.065 | -0.269 |
| 3 | 12.798 | 10.375 | 13.668 | 23.695 | 9.837 | 1.169 | 0.956 | 0.836 |
| 4 | -1.145 | 0.052 | -12.188 | 18.397 | 1.052 | -1.262 | -0.590 | -0.346 |

Giữ mọi outlier. Spike là overhead >200 ms; không retry hoặc loại mẫu. Early/late là 50 request đầu/cuối mỗi slot; với slot dưới 100 request hai cửa sổ overlap. Leave-one-block-out chỉ là sensitivity, không thay official results.

| Block | Arm | Early n | Early mean | Late n | Late mean | Overlap |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 00 | 50 | 71.240 | 50 | 61.220 | False |
| 1 | 01 | 50 | 58.052 | 50 | 70.669 | False |
| 1 | 10 | 50 | 49.002 | 50 | 60.139 | False |
| 1 | 11 | 50 | 51.653 | 50 | 51.376 | False |
| 2 | 00 | 50 | 66.573 | 50 | 51.548 | False |
| 2 | 01 | 50 | 59.169 | 50 | 60.198 | False |
| 2 | 10 | 50 | 53.067 | 50 | 56.253 | False |
| 2 | 11 | 50 | 57.212 | 50 | 58.796 | False |
| 3 | 00 | 50 | 84.601 | 50 | 67.056 | False |
| 3 | 01 | 50 | 55.212 | 50 | 57.192 | False |
| 3 | 10 | 50 | 46.030 | 50 | 55.122 | False |
| 3 | 11 | 50 | 60.979 | 50 | 69.253 | False |
| 4 | 00 | 50 | 53.939 | 50 | 57.942 | False |
| 4 | 01 | 50 | 65.569 | 50 | 57.365 | False |
| 4 | 10 | 50 | 45.335 | 50 | 60.793 | False |
| 4 | 11 | 50 | 49.604 | 50 | 64.589 | False |

| Block | Arm | Index | Request ID | Overhead ms |
| --- | --- | --- | --- | --- |
| 1 | 00 | 43 | 60a78667-bf71-45ac-bf1a-c96cc5216024 | 623.8921249999999 |
| 1 | 00 | 154 | 67d802ad-e453-48bb-b23d-981d2f49ec42 | 574.47775 |
| 1 | 01 | 242 | 2a5bcb39-bec7-455f-a513-79e8651b1995 | 593.984208 |
| 1 | 11 | 67 | 0bf0ce5a-c8af-4209-b137-fb90432a7d46 | 974.9792079999999 |
| 1 | 11 | 157 | 9998a31e-f42e-4575-9080-be3e4c0ff55e | 555.8747520000002 |
| 1 | 10 | 41 | 25edab7d-2871-41ee-9484-2017e170a469 | 317.7766659999999 |
| 1 | 10 | 118 | 420a02b8-edfd-4f20-869e-7703f2209b04 | 284.03029000000004 |
| 1 | 10 | 129 | 464fbdff-4d64-493a-97a5-da07647fc77e | 846.9791239999997 |
| 1 | 10 | 226 | 1bf9f40e-e1e0-4356-a0c7-1b082f04780f | 590.9876240000001 |
| 2 | 01 | 164 | 10310c82-4d53-43ef-98a2-f398414500e5 | 600.240626 |
| 2 | 10 | 43 | a9832bef-487a-44f0-8fd0-842a30569bae | 423.0641250000001 |
| 2 | 10 | 136 | de8488b9-0cc0-467a-a381-0ec2435b9d42 | 525.6560839999999 |
| 2 | 10 | 231 | 76f5ea9a-82a7-4911-b2b3-5ac9add4ece4 | 576.7705420000002 |
| 2 | 00 | 48 | 13cf96cd-a91d-4868-b091-e72fa6a2006a | 460.82454099999995 |
| 2 | 00 | 178 | d0890446-a12c-4ec0-8f2b-e85765d6226c | 473.38854200000003 |
| 2 | 11 | 40 | 4c00b1ac-6197-452c-8dd7-0ddca91b2d12 | 451.212291 |
| 2 | 11 | 125 | 17fabb75-c686-4eed-9a51-eddad1e18402 | 558.6080829999999 |
| 2 | 11 | 218 | 62a330a4-2c68-4f14-8bcf-7e82b264eadc | 569.44825 |
| 3 | 10 | 56 | 7f87463b-81a4-4e7e-a04e-b11e81147888 | 423.687167 |
| 3 | 10 | 149 | 6548aedd-a7ef-4991-810c-7949a6366614 | 593.40025 |
| 3 | 10 | 245 | 18413603-0c5b-4d1c-971a-40555f823274 | 530.9844589999999 |
| 3 | 11 | 41 | e38e9260-c593-46ef-8369-a5e105c9ab13 | 369.867125 |
| 3 | 11 | 127 | 82b1ea80-8981-48e5-9b8b-d2bfb2d8a655 | 958.361165 |
| 3 | 11 | 221 | aa4f49a8-da08-47ae-b9e7-32db5acb9db0 | 643.806168 |
| 3 | 01 | 57 | 8da7d676-8d29-405f-a49b-5929f5dc6e4e | 523.53979 |
| 3 | 01 | 86 | 5828c305-1c07-4c17-8154-2a93bf6bf186 | 318.29662500000006 |
| 3 | 01 | 142 | d369e491-2f28-4895-90b1-661f496eb80a | 259.999709 |
| 3 | 00 | 1 | 34978fbd-5b96-4250-a230-ce1f75971381 | 243.45266500000002 |
| 3 | 00 | 47 | c7141a73-ed3d-4250-befe-dbd57d013f36 | 813.9859169999999 |
| 3 | 00 | 157 | 9f534d75-e410-42c5-b36b-1763f08f08d8 | 742.2175 |
| 4 | 11 | 51 | a019ffd1-82c7-4340-9ce6-407665d14354 | 487.36729199999996 |
| 4 | 11 | 139 | f2790ca4-fd47-4b83-8a8d-d0be968c0932 | 520.7630839999999 |
| 4 | 11 | 232 | b0e93246-0527-4035-b9fc-6444391dc969 | 703.5487069999999 |
| 4 | 00 | 52 | a4cc94df-e770-4aad-8930-67e146aa69ed | 357.251543 |
| 4 | 10 | 53 | 3c1a3cfd-b9d9-4ef7-9b5c-f7a03a4cfa1e | 434.19504199999994 |
| 4 | 10 | 149 | 12d01ef6-8a66-46e1-aa8d-769b89145c71 | 729.4035009999999 |
| 4 | 10 | 245 | a5e818d3-fbea-48db-b3cd-913339a44a6a | 590.686499 |
| 4 | 01 | 45 | a9617a18-c505-470b-a89e-4b2ab1837653 | 401.63808299999994 |
| 4 | 01 | 110 | 09066904-6fa2-4e77-bd8c-948bb282d7a4 | 439.1927499999999 |
| 4 | 01 | 146 | 229ad1c9-7fce-4cf3-90fa-4c6f258c05ff | 247.74691699999994 |
| 4 | 01 | 150 | 4479ed38-92c7-4750-ab92-6c6c1f190e47 | 600.3802089999999 |
| 4 | 01 | 159 | 83f6f434-dcce-4e3d-9285-e52e0415f3a7 | 260.999333 |

Sensitivity 11-10 mean khi bỏ từng block (ms): {"1": 4.907659704000004, "2": 3.4721248493333405, "3": 0.2294577133333391, "4": 4.877255601333324}

Nguồn figure: run main-v2; profile legacy/unavailable; detail method-spans only; observer enabled=False. Các quan sát diagnostic không được gộp vào campaign khác hoặc trừ khỏi primary metric.

![Method envelope decomposition and spike timeline](diagnostics.png)

## Chi phí warmup

| Arm | Warmup attempts | Tổng server time warmup (ms) |
| --- | --- | --- |
| 00 | 20 | 4006.291 |
| 01 | 20 | 4330.956 |
| 10 | 20 | 4256.812 |
| 11 | 20 | 4254.105 |

Warmup được ghi riêng và không tham gia latency steady-state. Tổng server time warmup chưa gồm startup, readiness, upload/download hoặc verification; chưa đủ startup cost đối chứng để suy break-even request count.

## Hiệu ứng trên mean overhead

Δ = mean bật - mean đối chứng: âm nghĩa là overhead thấp hơn trong lần chạy này. Giảm (%) = (đối chứng - bật) / đối chứng x 100; âm nghĩa là chậm hơn. Không mặc định cache hoặc kết hợp hai cache luôn nhanh hơn.

| So sánh | Δ ms | Giảm % |
| --- | --- | --- |
| 01 - 00 | 0.300 | -0.472 |
| 10 - 00 | -9.463 | 14.896 |
| 11 - 00 | -6.091 | 9.588 |
| Compilation khi warm OFF (10 - 00) | -9.463 | 14.896 |
| Compilation khi warm ON (11 - 01) | -6.391 | 10.013 |
| Warm khi compilation OFF (01 - 00) | 0.300 | -0.472 |
| Warm khi compilation ON (11 - 10) | 3.372 | -6.236 |

Tương tác μ11 - μ10 - μ01 + μ00 = **3.072 ms**. Giá trị âm cho thấy mức giảm kết hợp lớn hơn tổng hai mức giảm riêng theo thang ms.

Không báo CI hay p-value. Chỉ có bốn block độc lập; 4.000 request không phải 4.000 lần lặp độc lập của điều kiện máy. Kết quả là quan sát tại workload, source và máy đã ghi nhận.

## Theo block (chỉ số chính, ms)

| Block | Arm | n | Mean | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 00 | 250 | 67.703 | 56.329 | 107.735 | 157.396 |
| 1 | 01 | 250 | 61.602 | 56.415 | 74.083 | 140.657 |
| 1 | 10 | 250 | 56.663 | 45.632 | 73.358 | 301.241 |
| 1 | 11 | 250 | 55.427 | 47.169 | 64.021 | 119.387 |
| 2 | 00 | 250 | 58.246 | 52.471 | 69.210 | 101.311 |
| 2 | 01 | 250 | 63.808 | 59.068 | 77.227 | 118.130 |
| 2 | 10 | 250 | 51.738 | 43.448 | 63.670 | 282.362 |
| 2 | 11 | 250 | 54.808 | 46.355 | 68.313 | 280.600 |
| 3 | 00 | 250 | 72.135 | 61.673 | 99.083 | 214.210 |
| 3 | 01 | 250 | 62.858 | 56.296 | 79.128 | 217.981 |
| 3 | 10 | 250 | 50.495 | 42.935 | 58.358 | 254.449 |
| 3 | 11 | 250 | 63.293 | 53.310 | 72.026 | 278.144 |
| 4 | 00 | 250 | 56.029 | 53.221 | 65.428 | 79.209 |
| 4 | 01 | 250 | 67.043 | 57.259 | 86.742 | 332.725 |
| 4 | 10 | 250 | 57.365 | 47.206 | 76.477 | 277.533 |
| 4 | 11 | 250 | 56.220 | 47.259 | 64.289 | 295.930 |

## Chỉ số phụ (ms)

server_total và SCRFD dùng để kiểm tra ranh giới phép trừ. flow_api gồm thời gian phía client quanh API /run; upload được ghi riêng và không cộng vào overhead.

| Arm | Metric | n | Mean | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- | --- |
| 00 | server_total_ms | 1000 | 101.589 | 88.120 | 150.739 | 294.085 |
| 00 | scrfd_processing_ms | 1000 | 38.061 | 32.251 | 64.141 | 116.206 |
| 00 | flow_api_ms | 1000 | 101.665 | 88.228 | 150.906 | 294.088 |
| 00 | upload_ms | 1000 | 7.974 | 6.784 | 13.662 | 24.309 |
| 01 | server_total_ms | 1000 | 97.994 | 87.776 | 125.963 | 372.053 |
| 01 | scrfd_processing_ms | 1000 | 34.166 | 30.246 | 48.442 | 87.129 |
| 01 | flow_api_ms | 1000 | 98.067 | 87.892 | 126.282 | 372.352 |
| 01 | upload_ms | 1000 | 8.925 | 6.342 | 12.185 | 25.913 |
| 10 | server_total_ms | 1000 | 87.907 | 75.326 | 118.536 | 454.427 |
| 10 | scrfd_processing_ms | 1000 | 33.842 | 30.197 | 49.798 | 76.777 |
| 10 | flow_api_ms | 1000 | 88.003 | 75.428 | 118.713 | 454.374 |
| 10 | upload_ms | 1000 | 7.522 | 6.598 | 13.666 | 21.271 |
| 11 | server_total_ms | 1000 | 90.508 | 79.587 | 112.273 | 401.143 |
| 11 | scrfd_processing_ms | 1000 | 33.071 | 30.361 | 47.704 | 58.199 |
| 11 | flow_api_ms | 1000 | 90.594 | 79.686 | 112.559 | 401.160 |
| 11 | upload_ms | 1000 | 6.960 | 6.304 | 10.491 | 16.975 |

## Bằng chứng worker

Counter bên dưới là after_measurement - after_warmup. Warm ON phải hit ở mọi request đo và không cold; OFF phải cold ở mọi request và không hit. Compilation ON phải có hit đo được; OFF phải có bypass và không hit. PID/cờ phải nhất quán trước warmup, sau warmup và sau đo.

| block | arm | pid | measured_attempts | compilation_hits | compilation_bypasses | warm_hits | warm_cold | compilation_misses | compilation_builds | compilation_evictions | compilation_hit_rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 00 | 19342 | 250 | 0 | 1500 | 0 | 250 | 0 | 0 | 0 | None |
| 1 | 01 | 20222 | 250 | 0 | 1500 | 250 | 0 | 0 | 0 | 0 | None |
| 1 | 11 | 21080 | 250 | 1500 | 0 | 250 | 0 | 0 | 0 | 0 | 1.0 |
| 1 | 10 | 21790 | 250 | 1500 | 0 | 0 | 250 | 0 | 0 | 0 | 1.0 |
| 2 | 01 | 22544 | 250 | 0 | 1500 | 250 | 0 | 0 | 0 | 0 | None |
| 2 | 10 | 23400 | 250 | 1500 | 0 | 0 | 250 | 0 | 0 | 0 | 1.0 |
| 2 | 00 | 24151 | 250 | 0 | 1500 | 0 | 250 | 0 | 0 | 0 | None |
| 2 | 11 | 24910 | 250 | 1500 | 0 | 250 | 0 | 0 | 0 | 0 | 1.0 |
| 3 | 10 | 25639 | 250 | 1500 | 0 | 0 | 250 | 0 | 0 | 0 | 1.0 |
| 3 | 11 | 26372 | 250 | 1500 | 0 | 250 | 0 | 0 | 0 | 0 | 1.0 |
| 3 | 01 | 27134 | 250 | 0 | 1500 | 250 | 0 | 0 | 0 | 0 | None |
| 3 | 00 | 27944 | 250 | 0 | 1500 | 0 | 250 | 0 | 0 | 0 | None |
| 4 | 11 | 28803 | 250 | 1500 | 0 | 250 | 0 | 0 | 0 | 0 | 1.0 |
| 4 | 00 | 29520 | 250 | 0 | 1500 | 0 | 250 | 0 | 0 | 0 | None |
| 4 | 10 | 30284 | 250 | 1500 | 0 | 0 | 250 | 0 | 0 | 0 | 1.0 |
| 4 | 01 | 31083 | 250 | 0 | 1500 | 250 | 0 | 0 | 0 | 0 | None |

Hit rate dùng compilation hits/(hits+misses), chỉ hiển thị khi có counter miss; compilation events không phải số request. Cache bị reset theo worker/slot; capacity/occupancy/payload được ghi riêng dưới đây. Payload bytes là source UTF-8, AST pickle hoặc resident JSON, không phải heap bytes hoặc RAM quy cho từng cache.

```json
[
  {
    "block": 1,
    "arm": "00",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 0
      },
      "after_measurement": {
        "registry_entries": 0
      }
    },
    "effective": {}
  },
  {
    "block": 1,
    "arm": "01",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 1
      },
      "after_measurement": {
        "registry_entries": 1
      }
    },
    "effective": {}
  },
  {
    "block": 1,
    "arm": "11",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 1
      },
      "after_measurement": {
        "registry_entries": 1
      }
    },
    "effective": {}
  },
  {
    "block": 1,
    "arm": "10",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 0
      },
      "after_measurement": {
        "registry_entries": 0
      }
    },
    "effective": {}
  },
  {
    "block": 2,
    "arm": "01",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 1
      },
      "after_measurement": {
        "registry_entries": 1
      }
    },
    "effective": {}
  },
  {
    "block": 2,
    "arm": "10",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 0
      },
      "after_measurement": {
        "registry_entries": 0
      }
    },
    "effective": {}
  },
  {
    "block": 2,
    "arm": "00",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 0
      },
      "after_measurement": {
        "registry_entries": 0
      }
    },
    "effective": {}
  },
  {
    "block": 2,
    "arm": "11",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 1
      },
      "after_measurement": {
        "registry_entries": 1
      }
    },
    "effective": {}
  },
  {
    "block": 3,
    "arm": "10",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 0
      },
      "after_measurement": {
        "registry_entries": 0
      }
    },
    "effective": {}
  },
  {
    "block": 3,
    "arm": "11",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 1
      },
      "after_measurement": {
        "registry_entries": 1
      }
    },
    "effective": {}
  },
  {
    "block": 3,
    "arm": "01",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 1
      },
      "after_measurement": {
        "registry_entries": 1
      }
    },
    "effective": {}
  },
  {
    "block": 3,
    "arm": "00",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 0
      },
      "after_measurement": {
        "registry_entries": 0
      }
    },
    "effective": {}
  },
  {
    "block": 4,
    "arm": "11",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 1
      },
      "after_measurement": {
        "registry_entries": 1
      }
    },
    "effective": {}
  },
  {
    "block": 4,
    "arm": "00",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 0
      },
      "after_measurement": {
        "registry_entries": 0
      }
    },
    "effective": {}
  },
  {
    "block": 4,
    "arm": "10",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 0
      },
      "after_measurement": {
        "registry_entries": 0
      }
    },
    "effective": {}
  },
  {
    "block": 4,
    "arm": "01",
    "cache_occupancy": {
      "before_warmup": {
        "registry_entries": 0
      },
      "after_warmup": {
        "registry_entries": 1
      },
      "after_measurement": {
        "registry_entries": 1
      }
    },
    "effective": {}
  }
]
```

## Nguồn và khả năng tái lập

manifest.json giữ thiết kế, hashes workload/source; requests.jsonl giữ mọi attempt; worker_evidence.jsonl giữ snapshot worker. Báo cáo đọc offline và không sửa ba file raw. Thiếu, sai số lượng, lỗi output/timing hoặc thiếu bằng chứng làm toàn bộ run INVALID.

```json
{
  "workload": {
    "flow_id": "d7b1b956-0181-472e-8d6f-ccf7e55a4066",
    "model_path": "/Users/tranquangtrong/Downloads/det_500m.onnx",
    "image_path": "/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/inputs/face_detection_example.jpg",
    "flow_export_path": "/Users/tranquangtrong/Desktop/langflow_CV/benchmark_analyst/runs/inputs/scrfd-flow.json",
    "input_node_id": "ChatInput-Q4OPx",
    "output_node_id": "ChatOutput-PNysN",
    "scrfd_node_ids": [
      "CustomComponent-68ngh",
      "CustomComponent-gHGu0",
      "CustomComponent-z55Zl",
      "CustomComponent-SSIb3"
    ],
    "expected_faces": 3,
    "timeout_seconds": 60,
    "reference_pixel_sha256": "69ad6123b68bddb4a1c0dab702bf2e153e4f38db2e527e07deee3eb1e44f86a1",
    "flow_sha256": "1567ae27b44e66d126f239f77b88fec8a090d5264bbc962c3d994885e792bfc0",
    "input_sha256": "c908a84bd0ce2e8140ea17e3afededcabd64b064b6bb2b9f9fd4aa624b05a3c2",
    "model_sha256": "5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a"
  },
  "source": {
    "git_revision": "43772e04ce51d3f643484f8d83135c206402d37b",
    "sha256": "9f0d0dc4fd8f830adc3979875bec77f794341c499dff9f6ce5fcddb9a65d1f84",
    "recorded_file_count": 1728
  },
  "profile_id": null,
  "profile": null,
  "fixture": null,
  "raw_sha256": {
    "manifest.json": "858ba27e2962f94ae9a713e67c90127c963d24df5daf7779bcda7abff1c1891a",
    "requests.jsonl": "f902cc60f05d3e575b63b41a35a13869836c7478a91b9776c0256b29c2b2f320",
    "worker_evidence.jsonl": "59e217512aa2cfc7e7b38d5a6e73e3001f01a2b71aa7584820b462b5658a7a97",
    "state.json": "1766be63817fa85d5aeeea066b217abdc6f6a5374d607c794a7b131ebb35b77d"
  }
}
```
