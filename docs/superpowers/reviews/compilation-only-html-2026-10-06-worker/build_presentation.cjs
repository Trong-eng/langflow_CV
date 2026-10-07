const fs = require('fs');
const path = require('path');
const root = process.cwd();
const qa = __dirname;
const aggregate = 'benchmark_analyst/runs/main-compile-v6-comparison-mean-p95/';
function csv(file) {
  const content=fs.readFileSync(file,'utf8'); let rows=[],row=[],cell='',quoted=false;
  for(let i=0;i<content.length;i++){const c=content[i];if(c==='"'){if(quoted&&content[i+1]==='"'){cell+='"';i++;}else quoted=!quoted;}else if(c===','&&!quoted){row.push(cell);cell='';}else if(c==='\n'&&!quoted){row.push(cell.replace(/\r$/,''));rows.push(row);row=[];cell='';}else cell+=c;}
  if(cell||row.length){row.push(cell);rows.push(row);}const keys=rows.shift();return rows.filter(r=>r.length===keys.length).map(r=>Object.fromEntries(keys.map((k,i)=>[k,r[i]])));
}
const escape=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const link=(p,label)=>`<a href="${escape(p)}" target="_blank" rel="noopener">${label}</a>`;
const sources=(...entries)=>`<p class="sources">Nguồn: ${entries.map(([p,label])=>link(p,label)).join(' · ')}</p>`;
const numberEvidence=[];
function num(value,file,row,key,digits=3,signed=false,suffix=''){
  const v=Number(value); if(!Number.isFinite(v))throw Error('invalid numeric value');
  const id='n'+(numberEvidence.length+1);numberEvidence.push({id,file,row,key,value:v,digits,signed,suffix});
  return `<span id="${id}" class="number ${signed?(v>0?'adverse':'favorable'):''}">${signed&&v>0?'+':''}${v.toFixed(digits)}${suffix}</span>`;
}
function table(headers,rows,extra='') {return `<div class="table-container ${extra}"><table class="data-table"><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map((cell,i)=>`<td data-label="${escape(headers[i].replace(/<[^>]+>/g,''))}"><span class="cell-value">${cell}</span></td>`).join('')}</tr>`).join('')}</tbody></table></div>`;}
const card=(title,text,cls='')=>`<div class="card ${cls}"><h3 class="card-title">${title}</h3><div class="card-desc">${text}</div></div>`;
const note=(text,cls='tech-card')=>`<div class="${cls} note">${text}</div>`;
const code=(label,text,cls='success-border')=>`<div class="code-frame ${cls}"><div class="code-header">${label}</div><pre class="code-body">${escape(text)}</pre></div>`;
const compilationRoot='../../.codex/worktrees/benchmark-compile-v6/langflow_CV/';
const mainRoot='../../.codex/worktrees/benchmark-main-v6/langflow_CV/';
const patch='benchmark_analyst/runs/setup-main-compile-v6/main-to-compile.patch';
const sourceReview='benchmark_analyst/runs/setup-main-compile-v6/source_review.json';
const receipt=aggregate+'final_verification_receipt.json';
const comparisons=csv(aggregate+'comparisons.csv');
const ram=csv(aggregate+'ram_comparisons.csv');
const warmup=csv(aggregate+'warmup_summary.csv');
const blocks=csv(aggregate+'summary.csv');
const v5=[csv('benchmark_analyst/runs/main-v5/summary.csv'),csv('benchmark_analyst/runs/main-v5-repeat/summary.csv')];
const slides=[];
function slide(title,eyebrow,subtitle,body,badge='',id=''){
  const n=slides.length+1;slides.push(`<section class="slide${n===1?' active':''}" data-slide="${n}" id="slide-${n}"${id?` data-topic="${id}"`:''}><div class="slide-header"><div><div class="slide-eyebrow">${eyebrow}</div><h${n===1?'1':'2'} class="slide-title">${title}</h${n===1?'1':'2'}><p class="slide-subtitle">${subtitle}</p></div>${badge?`<span class="chip chip-blue">${badge}</span>`:''}</div>${body}<div class="slide-footer">Compilation cache · MAIN → COMPILE · ${String(n).padStart(2,'0')}</div></section>`);
}
slide('Compilation Cache Cho Pipeline AI Cố Định Trên Langflow','BÁO CÁO BẢO VỆ · 06/10/2026','Tái sử dụng artifact chuẩn bị source Python; đánh giá mean, p95 và RAM trong hai lượt độc lập.',
`<div class="grid-3">${card('Mean server giảm','<strong class="hero-number">6,60–7,19%</strong><p>Giảm 5,924–6,719 ms khi gọi <code>/run</code>.</p>')}${card('p95 server giảm','<strong class="hero-number">7,91–8,87%</strong><p>Giảm 9,911–10,969 ms ở kết quả gộp từng lượt.</p>')}${card('RSS sau idle tăng','<strong class="hero-number adverse">87–207 MiB</strong><p>+8,10% và +23,13% bộ nhớ toàn worker.</p>')}</div>
${note('<strong>Đề xuất có điều kiện:</strong> Giữ compilation cache khi ưu tiên latency và ngân sách RAM cho mỗi worker cho phép. Lợi ích được quan sát trên một flow SCRFD, một model, một ảnh cố định và một máy.')}
<div class="grid-2 spaced">${card('Tối ưu phần chuẩn bị source','Cache AST template và code object đã compile, trong RAM của tiến trình. Runtime vẫn tạo globals, class và component riêng.')}${card('Bằng chứng để quyết định','Hai lượt VALID, 4.000 measured requests + 80 warmup. Có block bất lợi và mức RSS tăng khác nhau giữa hai lượt.')}</div>
${sources([aggregate+'report.html','Báo cáo tổng hợp'],[aggregate+'comparisons.csv','Latency CSV'],[aggregate+'ram_comparisons.csv','RAM CSV'])}`,'mean · p95 · RAM');
slide('Vì Sao Cache Parse & Compile?','CƠ CHẾ · SOURCE → ARTIFACT','Source component không đổi vẫn có phần chuẩn bị lặp lại trước khi chạy runtime.',
`<div class="grid-2">${card('Đường MAIN','<p><strong>AST</strong> là cây biểu diễn cấu trúc source Python. <code>ast.parse()</code> đọc source thành AST; các bước tiếp theo kiểm tra annotations và trích class. <code>compile_class_code()</code> tạo code object cho class.</p><div class="pipeline"><span>Source</span><b>→</b><span>Parse / validate</span><b>→</b><span>Compile</span><b>→</b><span>Runtime</span></div>')}${card('Đường COMPILE khi hit','<p>Tra artifact theo source; khôi phục AST riêng cho request và tái sử dụng <code>compiled_class</code>. Khi miss hoặc bypass, vẫn phải chuẩn bị source.</p><div class="pipeline"><span>Source / khóa</span><b>→</b><span>Artifact hit</span><b>→</b><span>AST riêng</span><b>→</b><span>Runtime</span></div>')}</div>
${note('<strong>Artifact chứa gì?</strong> Source gốc, generation, AST module đã serialize thành bytes, tên class, <code>compiled_class: CodeType</code> và trusted alias. Code object chứa bytecode Python; bytecode không phải mã máy.')}
${note('<strong>Phần còn lại:</strong> Imports, globals, <code>exec</code>, tạo class, constructor và graph runtime vẫn thực hiện. Một số định nghĩa cấp module vẫn qua <code>compile()</code>; cache hit không làm chi phí runtime bằng zero.','feynman-card')}
${sources([mainRoot+'src/lfx/src/lfx/custom/validate.py','MAIN validate.py'],[compilationRoot+'src/lfx/src/lfx/custom/validate.py','COMPILE validate.py'],[patch,'Diff nguồn đã đo'])}`,'Artifact ≠ runtime');
const files=[
 ['custom/component_compilation_cache.py','Tạo mới · +140','Artifact; SHA256; RLock; LRU và counters.'],
 ['custom/eval.py','Sửa · +1 / −2','Gọi create_class_from_code() để dùng chung đường artifact.'],
 ['custom/validate.py','Sửa · +150 / −44','Tách chuẩn bị source khỏi imports, globals và tạo class runtime.'],
 ['services/settings/groups/cache.py','Sửa · +2','Cờ component_compilation_cache_enabled mặc định False.'],
];
slide('Đúng Hai Source, Đúng Patch Được Đo','SOURCE IDENTITY · V6','COMPILE xuất phát từ MAIN local pin; diff gồm 4 runtime files và 2 test files.',
`<div class="grid-2">${card('MAIN · baseline nguyên bản','<code>c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d</code><p>Nhánh <code>codex/benchmark-main-baseline-v6</code></p><p class="file-path">/Users/tranquangtrong/.codex/worktrees/benchmark-main-v6/langflow_CV</p>')}${card('COMPILE · chỉ compilation cache','<code>c20b6591292e8c3e979db59f831987f3bc4cfbf0</code><p>Nhánh <code>codex/benchmark-compile-only-v6</code></p><p class="file-path">/Users/tranquangtrong/.codex/worktrees/benchmark-compile-v6/langflow_CV</p>')}</div>
${table(['Runtime file trong src/lfx/src/lfx/','Diff thực tế','Vai trò'],files.map(([f,stat,role])=>[link(compilationRoot+'src/lfx/src/lfx/'+f,`<span class="file-path">${f}</span>`),stat,role]))}
<p class="compact"><strong>6 files: +847 / −46.</strong> Runtime: +293 / −46; tests: +554. Hai test files: <code>test_component_compilation_cache.py</code> (+546), <code>test_settings_composition.py</code> (+8).</p>
${sources([sourceReview,'Source review'],[patch,'Patch 6 files'],['benchmark_analyst/runs/main-compile-v6/manifest.json','Manifest lượt 1'],['benchmark_analyst/runs/main-compile-v6-repeat/manifest.json','Manifest repeat'])}`,'4 runtime + 2 tests');
slide('SHA256, LRU Và Thread Safety','IMPLEMENTATION · CACHE TRONG TIẾN TRÌNH','Khóa gồm generation, digest của source gốc và variant; vẫn so sánh toàn bộ source với entry.',
`<div class="code-split">${code('Lookup khi hit · code rút gọn từ COMPILE',`key = (COMPONENT_COMPILATION_ARTIFACT_GENERATION,
       _source_digest(source), variant)
with _cache_lock:
    cached = _cache.get(key)
    if cached is not None and cached.source == source:
        _cache.move_to_end(key)
        _stats["hits"] += 1
        return ComponentArtifactLookup(
            artifact=cached.artifact, retained=True)`)}${code('Miss và giới hạn LRU · cùng lock',`    _stats["misses"] += 1
    artifact = builder()
    _stats["builds"] += 1
    _cache[key] = _CacheEntry(
        source=source, artifact=artifact)
    _cache.move_to_end(key)
    if len(_cache) > 128:
        _cache.popitem(last=False)
        _stats["evictions"] += 1`)}</div>
<div class="grid-3 spaced">${card('128 entries / worker','LRU loại entry ít được dùng gần đây khi vượt giới hạn. Đây là giới hạn số entry, không phải giới hạn tổng RSS.')}${card('RLock','Lock bao phần chuẩn bị khi miss, tránh nhiều thread cùng build một source; imports, exec và constructor nằm ngoài builder.')}${card('Opt-in & bypass','Mặc định <code>False</code>. Source lớn hơn 262.144 bytes hoặc cờ OFF đi bypass. Builder lỗi không giữ artifact.')}</div>
<p class="compact">Source đổi tạo khóa khác; entry cũ có thể còn đến eviction. AST bytes được tạo trong tiến trình từ source đã chuẩn bị, rồi khôi phục thành AST riêng cho request.</p>
${sources([compilationRoot+'src/lfx/src/lfx/custom/component_compilation_cache.py','Cache tại commit COMPILE'],[compilationRoot+'src/lfx/src/lfx/services/settings/groups/cache.py','Cờ opt-in'])}`,'LRU · RLock · opt-in');
slide('Đối Chiếu Luồng Tạo Class','IMPLEMENTATION · VALIDATE.PY','Minh họa rút gọn theo hai source đã đo; cache tái sử dụng phần chuẩn bị trước runtime.',
`<div class="code-split">${code('MAIN · chuẩn bị lại artifact cho source',`module = ast.parse(code)
validate_return_annotations(module)
# ... chuẩn bị runtime_module / future_imports
runtime_class_code = extract_class_code(
    runtime_module, class_name)
compiled_class = compile_class_code(
    runtime_class_code, future_imports)
# ... chuẩn bị globals và thực thi
component_class = build_class_constructor(
    compiled_class, exec_globals, class_name)`,'danger-border')}${code('COMPILE · lookup rồi chạy runtime',`lookup, prepared_module = (
    _get_component_compilation_artifact(
        code, class_name))
artifact = lookup.artifact
module = (prepared_module
    if prepared_module is not None else
    _load_component_module_template(
        artifact.module_template))
# ... imports / prepare_global_scope vẫn chạy
component_class = build_class_constructor(
    artifact.compiled_class,
    exec_globals, class_name)`)}</div>
${note('<strong>Hit:</strong> Tái sử dụng code object của class và AST template đã chuẩn bị. <strong>Miss:</strong> Parse / validate / compile, tạo artifact rồi lưu khi đủ điều kiện. Cả hai đường vẫn tạo globals và class cho lần thực thi hiện tại.')}
${note('<strong>Cách ly runtime:</strong> Không cache instance component, user/session hoặc globals đã thực thi. COMPILE kiểm tra source, generation và class name của artifact trước khi dùng. Code minh họa lược bỏ kiểm tra lỗi và decorators.','feynman-card')}
${sources([patch,'Patch chính xác'],[mainRoot+'src/lfx/src/lfx/custom/validate.py','MAIN create_class'],[compilationRoot+'src/lfx/src/lfx/custom/validate.py','COMPILE create_class'])}`,'Runtime vẫn diễn ra');
// The sole historical exclusion section. All historical arm references are confined here.
function oldOverhead(rows,arm){const r=rows.find(r=>r.arm===arm && String(r.block)==='all' && r.metric==='langflow_overhead_ms');if(!r)throw Error('v5 CSV row not found');return r;}
const oldRows=v5.map((rows,i)=>{const a=oldOverhead(rows,'10'),b=oldOverhead(rows,'11');const file='benchmark_analyst/runs/'+(i?'main-v5-repeat':'main-v5')+'/summary.csv';return [i?'v5 repeat':'v5',num(a.mean,file,{arm:'10',block:'all',metric:'langflow_overhead_ms'},'mean')+' → '+num(b.mean,file,{arm:'11',block:'all',metric:'langflow_overhead_ms'},'mean'),num(a.p95,file,{arm:'10',block:'all',metric:'langflow_overhead_ms'},'p95')+' → '+num(b.p95,file,{arm:'11',block:'all',metric:'langflow_overhead_ms'},'p95'),num(Number(b.mean)-Number(a.mean),file,{derived:'11-10',stat:'mean'},'derived',3,true)+' / '+num(Number(b.p95)-Number(a.p95),file,{derived:'11-10',stat:'p95'},'derived',3,true)];});
slide('Vì Sao Chỉ Giữ Compilation Cache?','QUYẾT ĐỊNH PHẠM VI · BẰNG CHỨNG CŨ','v5 so hai cờ trên cùng source feature: arm 10 compilation ON / warm OFF, arm 11 cả hai ON.',
`${table(['Lượt cũ','Overhead mean · 10 → 11 (ms)','Overhead p95 · 10 → 11 (ms)','Δ11−10 · mean / p95 (ms)'],oldRows)}
<p class="compact">Arm 11 có template hits ở toàn bộ 1.000 measured requests mỗi lượt, nhưng mean/p95 overhead xấu hơn; server/client API latency cũng tăng. Đây là phép đánh giá bật thêm warm graph, không phải MAIN nguyên bản so với COMPILE v6.</p>
${note('<strong>Có template vẫn có chi phí runtime:</strong> Mỗi request cần graph riêng để cô lập user/session/tweaks. <code>copy_for_run()</code> sao chép dữ liệu graph, dựng nodes/edges rồi khởi tạo component; lấy flow, kiểm tra và setup vẫn còn. Template hit không bỏ toàn bộ chi phí dựng runtime.')}
<p class="compact"><strong>D1 (diagnostic riêng):</strong> Span <code>setup/warm_graph_copy</code> mean <strong>22.608 ms</strong>, p95 <strong>23.446 ms</strong>; pre-component mean 10 → 11: <strong>38.033 → 42.196 ms</strong>. Span bọc cả <code>copy_for_run</code>, gồm component/class preparation, không phải thời gian deepcopy thuần. Span lồng/chồng nhau: không cộng chúng hoặc cộng vào campaign v6; không chứng minh deepcopy là nguyên nhân duy nhất, cũng không quy cho GC/CPU.</p>
<p class="compact"><strong>Quyết định:</strong> Với workload và implementation đã đo, bật thêm warm graph làm mean/p95 xấu hơn dù có hits. Vì vậy chỉ giữ compilation cache trong trình bày; không kết luận phương pháp này luôn vô ích, không xóa implementation trong repository.</p>
${sources(['benchmark_analyst/runs/main-v5/report.html','v5'],['benchmark_analyst/runs/main-v5-repeat/report.html','v5 repeat'],['benchmark_analyst/runs/diagnostic-v5/suite_report.html','Diagnostic suite'],['benchmark_analyst/runs/diagnostic-v5/D1/report.html','D1'],['benchmark_analyst/runs/diagnostic-v5/D1/analysis.json','D1 JSON'],['benchmark_analyst/runs/diagnostic-v5/D1/diagnostic_events.jsonl','Events'],['src/lfx/src/lfx/graph/graph/base.py','_copy_graph / copy_for_run'],['benchmark_analyst/diagnostic_observer.py','Span wrapper'])}`,'Bằng chứng v5 / D1','excluded-method');
slide('Thiết Kế Hai Lượt MAIN–COMPILE','BENCHMARK · WORKLOAD ĐƯỢC GHIM','Warmup là các request làm nóng trước đo, khác cơ chế template đã loại ở slide trước.',
`<div class="grid-3">${card('2 lượt VALID','Mỗi lượt: 4 blocks × 250 measured requests/nhóm = <strong>1.000/nhóm</strong>. Tổng hai lượt: <strong>4.000 measured + 80 warmup</strong>.')}${card('Worker và fixture mới','Mỗi slot dùng worker và clone DB/storage fixture mới; 5 warmup/worker. Worker là một tiến trình Python chạy backend Langflow.')}${card('Concurrency 1','Một worker chạy tại một thời điểm. Thứ tự cân bằng MAIN→COMPILE / COMPILE→MAIN theo blocks; không đánh giá tải đồng thời.')}</div>
<div class="grid-2 spaced">${card('Điều kiện chung','<ul><li><strong>warm graph OFF ở cả hai nhóm.</strong></li><li><code>requests.Session</code> có tái sử dụng TCP.</li><li>Native tracing và telemetry bật giống nhau.</li><li>Diagnostic chi tiết tắt; idle checkpoint sau 5 giây.</li></ul>')}${card('Workload cố định','<ul><li>Một flow có bốn node SCRFD.</li><li>Model <code>det_500m.onnx</code> và ảnh đầu vào cố định.</li><li>Đối chiếu số mặt và pixel hash của output.</li><li>Source / flow / model / ảnh / harness đều có identity trong manifests.</li></ul>')}</div>
${note('<strong>Đơn vị lặp là 4 blocks mỗi lượt.</strong> Các request trong block là mẫu lặp bên trong, không phải 1.000 thí nghiệm độc lập. p95 tính trên dữ liệu gộp trong từng lượt, không lấy trung bình percentile các blocks, không gộp hai lượt để che biến thiên.')}
${sources(['benchmark_analyst/runs/main-compile-v6/manifest.json','Manifest lượt 1'],['benchmark_analyst/runs/main-compile-v6-repeat/manifest.json','Manifest repeat'],[aggregate+'analysis.json','Phương pháp / kết quả'])}`,'4 blocks / lượt');
slide('Đo Độc Lập, Đúng Ranh Giới /run','INSTRUMENTATION · SERVER / CLIENT / RSS','Hai latency thực tế là thời gian của bước /run; không đại diện cả hành trình upload → download.',
`<div class="grid-2">${card('server_total_ms','Wall-clock trong worker: từ ASGI entry bên ngoài ứng dụng đến lúc gửi xong response body. Wrapper ngoài production app dùng cùng cách đo cho MAIN và COMPILE.')}${card('flow_api_ms','Wall-clock phía client: từ gọi <code>/run</code> bằng <code>requests.Session</code> đến khi đọc xong response bytes. Bao network/client overhead trong khoảng này.')}</div>
<div class="pipeline boundary"><span>Upload ảnh<br>ngoài clock</span><b>→</b><span class="timed">Gọi /run → đọc response<br>client API clock</span><b>→</b><span>Download / kiểm tra output<br>ngoài clock</span></div>
${note('<strong>Ở ngoài cả hai latency:</strong> Upload/download ảnh, kiểm tra output, snapshot counters và thu thập RSS. Khởi động worker và warmup cũng tách khỏi thống kê steady-state.')}
<div class="grid-2 spaced">${card('Overhead để giải thích','<code>langflow_overhead_ms</code> = server_total_ms trừ <strong>hợp</strong> các khoảng xử lý của bốn node SCRFD; không cộng các khoảng trùng nhau. Không thay server/client bằng overhead khi đánh giá latency thực tế.')}${card('RAM tại checkpoints','RSS toàn worker trước warmup, sau warmup, sau đo và sau idle. MiB = 1.048.576 bytes. Đo điểm lấy mẫu, không đo peak; USS giữ null và reason khi không truy cập được.')}</div>
<p class="compact">Harness đóng băng SHA256: <code>adfae1a939421b5c03aa53b8ee75e32e61e1dcd7cf24b73316e770f953ceacf3</code>; source worker đối chiếu bằng commit và hash trong manifests/receipt.</p>
${sources(['benchmark_analyst/revision_worker.py','Worker wrapper'],['benchmark_analyst/revision_campaign.py','Client campaign'],['benchmark_analyst/worker_observer.py','Node observer'],[receipt,'Receipt nguồn / harness'],[aggregate+'report.html','Ranh giới đo'])}`,'Wall-clock · MiB');
const pooled=comparisons.filter(r=>r.block==='all'&&['server_total_ms','flow_api_ms'].includes(r.metric)).sort((a,b)=>Number(a.run)-Number(b.run)||a.metric.localeCompare(b.metric)||a.statistic.localeCompare(b.statistic));
slide('Mean & p95 Của Hai Latency Thực Tế','KẾT QUẢ CHÍNH · HAI LƯỢT RIÊNG','Đơn vị ms. Δ = COMPILE − MAIN; tỷ lệ thay đổi dùng MAIN làm mẫu số. Số làm tròn từ CSV đầy đủ.',
`${table(['Lượt','Metric','Thống kê','MAIN','COMPILE','Δ ms','Δ %'],pooled.map(r=>[r.run,r.metric==='server_total_ms'?'Server':'Client API',r.statistic,num(r.main,aggregate+'comparisons.csv',{run:r.run,block:r.block,metric:r.metric,statistic:r.statistic},'main'),num(r.compile,aggregate+'comparisons.csv',{run:r.run,block:r.block,metric:r.metric,statistic:r.statistic},'compile'),num(r.delta_ms,aggregate+'comparisons.csv',{run:r.run,block:r.block,metric:r.metric,statistic:r.statistic},'delta_ms',3,true),num(r.delta_percent,aggregate+'comparisons.csv',{run:r.run,block:r.block,metric:r.metric,statistic:r.statistic},'delta_percent',2,true,'%')]))}
${note('<strong>Cả hai lượt:</strong> Mean và p95 gộp của server/client API đều giảm. Mean server giảm 5,924–6,719 ms; p95 giảm 9,911–10,969 ms. Kết quả mô tả workload này, chưa xác lập SLA hoặc ý nghĩa thống kê.')}
${sources(['benchmark_analyst/runs/main-compile-v6-mean-p95/report.html','Lượt 1'],['benchmark_analyst/runs/main-compile-v6-repeat-mean-p95/report.html','Repeat'],[aggregate+'summary.csv','Summary'],[aggregate+'comparisons.csv','Delta đầy đủ'])}`,'1.000 mẫu / nhóm / lượt');
const block4=comparisons.filter(r=>r.block==='4'&&r.statistic==='p95'&&['server_total_ms','flow_api_ms'].includes(r.metric)).sort((a,b)=>Number(a.run)-Number(b.run)||a.metric.localeCompare(b.metric));
slide('Giữ Lại Block Bất Lợi','BIẾN THIÊN · KHÔNG THẮNG MỌI BLOCK','Dù p95 gộp giảm ở cả hai lượt, p95 của block 4 tăng ở cả server và client API.',
`${table(['Lượt / block','Metric','MAIN p95 (ms)','COMPILE p95 (ms)','Δ ms','Δ %'],block4.map(r=>[r.run+' / 4',r.metric==='server_total_ms'?'Server':'Client API',num(r.main,aggregate+'comparisons.csv',{run:r.run,block:r.block,metric:r.metric,statistic:r.statistic},'main'),num(r.compile,aggregate+'comparisons.csv',{run:r.run,block:r.block,metric:r.metric,statistic:r.statistic},'compile'),num(r.delta_ms,aggregate+'comparisons.csv',{run:r.run,block:r.block,metric:r.metric,statistic:r.statistic},'delta_ms',3,true),num(r.delta_percent,aggregate+'comparisons.csv',{run:r.run,block:r.block,metric:r.metric,statistic:r.statistic},'delta_percent',2,true,'%')]))}
<div class="grid-2 spaced">${card('p95 server theo blocks','Lượt 1: blocks 1–3 giảm; block 4 <strong class="adverse">+7,50%</strong>.<br>Repeat: blocks 1–3 giảm; block 4 <strong class="adverse">+8,80%</strong>.')}${card('Mean và p95 khác nhau','Mean server/client giảm ở cả bốn blocks trong từng lượt. Điều đó không đảm bảo p95 từng block giảm, hay mọi request đều nhanh hơn.')}</div>
${note('<strong>Giới hạn:</strong> Bốn blocks là đơn vị lặp trong thiết kế. Giữ toàn bộ outliers; không có CI hoặc kết luận significance. Chưa có bằng chứng để quy biến thiên cho GC/CPU.','feynman-card')}
${sources([aggregate+'comparisons.csv','Từng block / delta'],[aggregate+'summary.csv','Mean / p95 đầy đủ'],[aggregate+'report.html','Báo cáo tổng hợp'])}`,'Block 4: server +7,50 / +8,80%');
const ramRows=ram.filter(r=>r.metric==='rss');
slide('RAM: RSS Trung Bình Toàn Worker','KẾT QUẢ RAM · BỐN CHECKPOINTS','Trung bình của bốn worker mỗi nhóm/lượt. Đơn vị MiB; after_idle lấy sau 5 giây idle.',
`${table(['Lượt','Checkpoint','MAIN MiB','COMPILE MiB','Δ MiB','Δ % sau idle'],ramRows.map(r=>[r.run,r.checkpoint,num(r.main_mib,aggregate+'ram_comparisons.csv',{run:r.run,checkpoint:r.checkpoint,metric:r.metric},'main_mib'),num(r.compile_mib,aggregate+'ram_comparisons.csv',{run:r.run,checkpoint:r.checkpoint,metric:r.metric},'compile_mib'),num(r.delta_mib,aggregate+'ram_comparisons.csv',{run:r.run,checkpoint:r.checkpoint,metric:r.metric},'delta_mib',3,true),r.checkpoint==='after_idle'?num(Number(r.delta_mib)/Number(r.main_mib)*100,aggregate+'ram_comparisons.csv',{run:r.run,checkpoint:r.checkpoint,metric:r.metric,derived:'delta/main*100'},'derived',2,true,'%'):'—']))}
${note('<strong>Sau idle:</strong> RSS COMPILE cao hơn MAIN <strong class="adverse">87,102 MiB (+8,10%)</strong> ở lượt 1 và <strong class="adverse">206,668 MiB (+23,13%)</strong> ở repeat. Mốc sau đo có delta −9,262 / +64,270 MiB; mức tăng không ổn định giữa các checkpoints hoặc hai lượt.','feynman-card')}
${sources([aggregate+'resource_summary.csv','RSS / USS summary'],[aggregate+'ram_comparisons.csv','RAM delta'],[aggregate+'analysis.json','Định nghĩa RAM'])}`,'MiB = 1.048.576 bytes');
slide('Đánh Đổi RAM Cần Cân Nhắc','DIỄN GIẢI · CHI PHÍ TRÊN MỖI WORKER','Để mean giảm khoảng 6–7 ms và p95 giảm khoảng 10–11 ms, RSS sau idle quan sát tăng 87–207 MiB.',
`<div class="grid-2">${card('Tăng 8–23% toàn worker','Đây là mức đáng cân nhắc theo ngân sách RAM của từng tiến trình Python chạy backend, đặc biệt khi triển khai nhiều worker. Kết quả hiện tại không đo tổng RAM dưới tải nhiều worker đồng thời.')}${card('RSS không riêng cache','RSS chứa mọi trang nhớ resident của worker. Không thể quy toàn bộ chênh lệch cho artifact cache, xem đó là dung lượng cố định của cache hoặc xem checkpoint là peak RAM.')}</div>
<div class="grid-2 spaced">${card('Biến động giữa blocks / lượt','RSS giữa blocks biến động lớn. Sau đo: delta −9 / +64 MiB; sau idle: +87 / +207 MiB. Không có một “RAM penalty” ổn định từ hai lượt này.')}${card('USS unavailable','Cả <strong>64 checkpoint chính</strong> đều không lấy được USS: <code>value: null</code>, <code>reason: AccessDenied</code>. Không thay bằng zero; không dùng USS để suy ra footprint riêng.')}</div>
${note('<strong>Giới hạn phép đo:</strong> Chỉ RSS tại bốn mốc lấy mẫu; không profile cấp phát hoặc đo peak. Không có kết luận tiết kiệm RAM, CPU, GC hay chi phí hạ tầng. Chưa có ngân sách RAM chấp nhận được hoặc mục tiêu latency tuyệt đối được chốt.','feynman-card')}
${sources([aggregate+'ram_comparisons.csv','Delta và reasons'],[aggregate+'resource_summary.csv','Unavailable USS'],[aggregate+'report.html','Đánh đổi / khuyến nghị'])}`,'Whole worker · nullable USS');
const wu=warmup.filter(r=>r.block==='all'&&r.metric==='server_total_ms');
slide('Output, Cache Hits Và Chi Phí Warmup','KIỂM CHỨNG · TÁCH WARMUP KHỎI STEADY-STATE','Output đúng ở mọi mẫu; warmup làm nóng runtime và cache trước giai đoạn đo chính.',
`<div class="grid-2">${card('COMPILE steady-state / block','<strong>1.500 hits</strong>; misses/builds/bypasses/evictions đều <strong>0</strong>. Sau warmup có 6 entries, 6 builds và 24 hits; không đưa các builds đó vào steady-state.')}${card('MAIN counters','<code>counters: null</code>, status unavailable, reason <code>absent_in_baseline</code>. Baseline không có cache nên không giả lập counters bằng zero. Mỗi lượt: 1.000/1.000 measured và 20/20 warmup thành công ở mỗi nhóm.')}</div>
${table(['Lượt','Nhóm','Warmup n','Server mean (ms)','Server p95 (ms)','Tổng /run warmup (ms)'],wu.map(r=>[r.run,r.arm,r.n,num(r.mean,aggregate+'warmup_summary.csv',{run:r.run,arm:r.arm,block:r.block,metric:r.metric},'mean'),num(r.p95,aggregate+'warmup_summary.csv',{run:r.run,arm:r.arm,block:r.block,metric:r.metric},'p95'),num(r.total_ms,aggregate+'warmup_summary.csv',{run:r.run,arm:r.arm,block:r.block,metric:r.metric},'total_ms')]))}
<p class="compact"><strong>Warmup biến thiên:</strong> COMPILE cao hơn MAIN ở lượt 1, thấp hơn ở repeat; không khẳng định cold-start nhanh hơn. Bảng chỉ là thời gian /run trong 20 warmup requests/nhóm/lượt, không gồm startup, upload/download hoặc toàn thời gian chuẩn bị worker.</p>
${sources([aggregate+'warmup_summary.csv','Warmup riêng'],['benchmark_analyst/runs/main-compile-v6-mean-p95/report.html','Output / cache lượt 1'],['benchmark_analyst/runs/main-compile-v6-repeat-mean-p95/report.html','Output / cache repeat'],[receipt,'Receipt chiến dịch'])}`,'4.000 measured + 80 warmup');
slide('Kết Luận: Giữ Cache Có Điều Kiện','KẾT LUẬN · WORKLOAD ĐÃ ĐO','Mean và p95 gộp của cả hai latency thực tế giảm ở cả hai lượt; cân nhắc cùng chi phí RAM.',
`<div class="grid-2">${card('Tiêu chí giữ cache không đổi','<strong>Mean và p95 server/client API giảm trong cả hai lượt đầy đủ.</strong> Compilation cache đạt tiêu chí quan sát này; đáng giữ khi ưu tiên latency và ngân sách RAM cho phép.')}${card('Giới hạn kết luận','Một flow SCRFD/model/ảnh cố định trên một máy; concurrency 1. Bốn blocks/lượt, kết quả mô tả. Chưa chứng minh SLA, tải production đồng thời hoặc lợi ích cho mọi flow.')}</div>
${note('<strong>Bất lợi vẫn được giữ:</strong> p95 block 4 tăng ở cả hai lượt. RSS sau idle tăng 8,10% và 23,13%, khác nhau rõ giữa hai lượt. Không kết luận cache thắng mọi block/chỉ số hoặc RAM penalty cố định.','feynman-card')}
${table(['Kiểm thử trong campaign receipt','Passed','Skipped','Xfailed'],[['MAIN','330','2','1'],['COMPILE','358','2','1'],['Harness / reporting','363','—','—']])}
<p class="compact">Các kết quả kiểm thử trên được trích từ receipt của chiến dịch đã có; nhiệm vụ cập nhật HTML này không chạy lại benchmark hoặc suites backend.</p>
${sources([aggregate+'report.html','Khuyến nghị gốc'],[receipt,'Campaign verification receipt'],[aggregate+'analysis.json','Dữ liệu / tiêu chí'],['docs/superpowers/reviews/compilation-only-html-2026-10-06-worker/verification_receipt.json','QA riêng của HTML'])}`,'Latency + ngân sách RAM');
let original=fs.readFileSync('optimize_final_langflow_CV.html','utf8');
let head=original.slice(0,original.indexOf('<body>'));
head=head.replace(/<title>[\s\S]*?<\/title>/,'<title>Compilation Cache Langflow · MAIN–COMPILE · Mean, p95 và RAM</title>');
const css=`
    /* Presentation-only refinements; original palette, fonts, cards and code frames retained. */
    html { color-scheme: dark; } html.light { color-scheme: light; }
    body { overflow-x: visible; }
    header.deck-header { flex-wrap: wrap; }
    .brand-group, .nav-controls { min-width: 0; flex-wrap: wrap; }
    .deck-title { max-width: 380px; }
    .slide-wrapper { min-height: 600px; }
    .slide { padding: 2rem 2.5rem; }
    .slide-header { gap: 1rem; }
    .slide-header > div { min-width: 0; }
    .slide-header > .chip { flex-shrink: 0; }
    .slide-footer { margin-top: 1.35rem; padding-top: .6rem; border-top: 1px solid var(--border-color); color: var(--text-muted); font-size: .72rem; text-align: right; }
    .spaced { margin-top: 1rem; }
    .note { margin-top: 1rem; font-size: .9rem; line-height: 1.6; padding: 1rem 1.2rem; }
    .compact { margin-top: .85rem; font-size: .88rem; line-height: 1.6; color: var(--text-secondary); }
    .compact strong { color: var(--text-primary); }
    .hero-number { display: block; font-family: var(--font-mono); font-size: 1.8rem; color: var(--accent-blue); margin-bottom: .6rem; }
    .adverse { color: var(--accent-rose) !important; }
    .favorable { color: var(--accent-green); }
    .number { white-space: nowrap; font-variant-numeric: tabular-nums; font-family: var(--font-mono); }
    a { color: var(--accent-blue); text-underline-offset: 3px; overflow-wrap: anywhere; }
    a:hover { color: var(--accent-purple); }
    .sources { margin-top: 1rem; color: var(--text-muted); font-size: .76rem; line-height: 1.7; }
    .card-desc p + p { margin-top: .4rem; }
    .card-desc ul { padding-left: 1.15rem; }
    .card-desc li + li { margin-top: .4rem; }
    code { overflow-wrap: anywhere; }
    .code-body { white-space: pre-wrap; overflow-wrap: anywhere; font-size: .8rem; }
    .code-header { flex-wrap: wrap; gap: .5rem; }
    table.data-table th, table.data-table td { padding: .55rem .75rem; }
    table.data-table th { font-size: .72rem; }
    table.data-table { font-size: .83rem; }
    table.data-table td { overflow-wrap: anywhere; }
    .table-container, .card, .code-frame, .cell-value { min-width: 0; }
    .pipeline { display: flex; align-items: center; flex-wrap: wrap; gap: .4rem; margin: 1rem 0 .2rem; }
    .pipeline span { flex: 1; min-width: 75px; padding: .7rem .45rem; border: 1px solid var(--border-color); border-radius: 8px; text-align: center; font-size: .8rem; }
    .pipeline b { color: var(--text-muted); }
    .boundary { margin-top: 1rem; }
    .timed { border-color: var(--accent-blue) !important; color: var(--accent-blue); }
    #slideSelect { max-width: 200px; background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-color); padding: .45rem; border-radius: 6px; font: inherit; font-size: .78rem; }
    .btn:focus-visible, select:focus-visible, a:focus-visible { outline: 2px solid var(--accent-blue); outline-offset: 3px; }
    @media (max-width: 992px) {
      main.presentation-stage { padding: 1rem; }
      .slide { padding: 1.4rem; }
      .slide-header { flex-direction: column; }
      .slide-wrapper { min-height: 0; }
      .brand-group { gap: .5rem; }
      header.deck-header { padding: .65rem .8rem; }
      .nav-controls { width: 100%; gap: .4rem; }
      .nav-controls .btn { font-size: .75rem; padding: .4rem .55rem; }
      #slideSelect { max-width: 100%; flex: 1; }
    }
    @media (max-width: 600px) {
      .slide { padding: 1rem; border-radius: 12px; }
      main.presentation-stage { padding: .7rem; }
      .slide-title { font-size: 1.35rem; }
      .card { padding: 1rem; }
      .table-container { overflow: visible; }
      table.data-table, table.data-table tbody { display: block; }
      table.data-table thead { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip-path: inset(50%); }
      table.data-table tr { display: block; border-bottom: 1px solid var(--border-color); padding: .5rem 0; }
      table.data-table tr:last-child { border: 0; }
      table.data-table td { display: grid; grid-template-columns: 40% minmax(0,1fr); gap: .65rem; padding: .3rem .65rem; border: 0; }
      table.data-table td::before { content: attr(data-label); font-size: .73rem; color: var(--text-muted); font-family: var(--font-sans); font-weight: 600; }
      .pipeline { flex-direction: column; align-items: stretch; }
      .pipeline b { text-align: center; transform: rotate(90deg); }
      .slide-indicator { font-size: .72rem; padding: .2rem .4rem; }
    }
`;
head=head.replace('</style>',css+'\n  </style>');
const titles=slides.map(s=>s.match(/<h[12] class="slide-title">(.*?)<\/h[12]>/)[1]);
const header=`<body><header class="deck-header"><div class="brand-group"><span class="brand-badge">BẢO VỆ ĐỀ TÀI</span><span class="deck-title">Compilation cache · MAIN–COMPILE</span><span class="chip chip-blue">V6 · 2 lượt VALID</span></div><nav class="nav-controls" aria-label="Điều hướng slide"><span class="slide-indicator" id="slideIndicator" aria-live="polite">Slide 1 / ${slides.length}</span><button class="btn btn-sm" id="prevBtn" title="Mũi tên trái / PageUp">◄ Trước</button><button class="btn btn-sm btn-primary" id="nextBtn" title="Mũi tên phải / Space / PageDown">Tiếp theo ►</button><select id="slideSelect" aria-label="Chọn slide">${titles.map((t,i)=>`<option value="${i+1}">${i+1}. ${t.replace(/<[^>]+>/g,'')}</option>`).join('')}</select><button class="btn btn-sm" id="viewModeBtn" title="Chuyển Trình Chiếu / Xem Tất Cả (M)" aria-pressed="false">📑 Xem Tất Cả</button><button class="btn btn-sm" id="themeToggleBtn" title="Đổi giao diện Sáng / Tối" aria-label="Đổi sang giao diện tối">🌓 Giao diện</button><button class="btn btn-sm" id="fullscreenBtn" title="Toàn màn hình (F)" aria-label="Toàn màn hình">⛶</button></nav><div class="progress-track"><div class="progress-bar" id="progressBar" role="progressbar" aria-label="Tiến trình slide" aria-valuemin="1" aria-valuemax="${slides.length}" aria-valuenow="1"></div></div></header><main class="presentation-stage"><div class="slide-wrapper" id="slideWrapper">`;
const script=`
  <script>
    let currentSlide = 1;
    let isDocumentMode = false;
    const slides = Array.from(document.querySelectorAll('.slide'));
    const totalSlides = slides.length;
    const slideIndicator = document.getElementById('slideIndicator');
    const progressBar = document.getElementById('progressBar');
    const prevBtn = document.getElementById('prevBtn');
    const nextBtn = document.getElementById('nextBtn');
    const viewModeBtn = document.getElementById('viewModeBtn');
    const themeToggleBtn = document.getElementById('themeToggleBtn');
    const fullscreenBtn = document.getElementById('fullscreenBtn');
    const slideSelect = document.getElementById('slideSelect');
    function updateSlide(newSlide, syncHash = true) {
      if (!Number.isInteger(newSlide) || newSlide < 1 || newSlide > totalSlides) return;
      currentSlide = newSlide;
      slides.forEach((slide, i) => slide.classList.toggle('active', i + 1 === currentSlide));
      slideIndicator.textContent = isDocumentMode ? 'Xem tất cả · ' + totalSlides + ' slides' : 'Slide ' + currentSlide + ' / ' + totalSlides;
      progressBar.style.width = (totalSlides > 1 ? (currentSlide - 1) / (totalSlides - 1) * 100 : 100) + '%';
      progressBar.setAttribute('aria-valuenow', currentSlide);
      prevBtn.disabled = isDocumentMode || currentSlide === 1;
      nextBtn.disabled = isDocumentMode || currentSlide === totalSlides;
      slideSelect.value = String(currentSlide);
      if (syncHash) history.replaceState(null, '', '#slide-' + currentSlide);
      window.scrollTo({ top: 0, behavior: 'instant' });
    }
    prevBtn.addEventListener('click', () => updateSlide(currentSlide - 1));
    nextBtn.addEventListener('click', () => updateSlide(currentSlide + 1));
    slideSelect.addEventListener('change', () => {
      const selected = Number(slideSelect.value);
      if (isDocumentMode) toggleViewMode();
      updateSlide(selected);
    });
    function toggleViewMode() {
      isDocumentMode = !isDocumentMode;
      document.body.classList.toggle('document-mode', isDocumentMode);
      viewModeBtn.textContent = isDocumentMode ? '🖥️ Trình Chiếu' : '📑 Xem Tất Cả';
      viewModeBtn.setAttribute('aria-pressed', String(isDocumentMode));
      updateSlide(currentSlide);
    }
    viewModeBtn.addEventListener('click', toggleViewMode);
    async function toggleFullScreen() {
      try {
        if (!document.fullscreenElement) await document.documentElement.requestFullscreen();
        else await document.exitFullscreen();
      } catch (error) {
        fullscreenBtn.title = 'Trình duyệt không cho phép fullscreen: ' + error.message;
      }
    }
    fullscreenBtn.addEventListener('click', toggleFullScreen);
    document.addEventListener('fullscreenchange', () => fullscreenBtn.setAttribute('aria-label', document.fullscreenElement ? 'Thoát toàn màn hình' : 'Toàn màn hình'));
    themeToggleBtn.addEventListener('click', () => {
      const light = document.documentElement.classList.toggle('light');
      document.documentElement.classList.toggle('dark', !light);
      themeToggleBtn.setAttribute('aria-label', light ? 'Đổi sang giao diện tối' : 'Đổi sang giao diện sáng');
    });
    document.addEventListener('keydown', event => {
      if (event.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName) || (event.key === ' ' && /^(BUTTON|A)$/.test(event.target.tagName)) || event.altKey || event.ctrlKey || event.metaKey) return;
      const key = event.key;
      if (key.toLowerCase() === 'f') { event.preventDefault(); toggleFullScreen(); }
      else if (key.toLowerCase() === 'm') { event.preventDefault(); toggleViewMode(); }
      else if (!isDocumentMode) {
        if (['ArrowRight', ' ', 'PageDown'].includes(key)) { event.preventDefault(); updateSlide(currentSlide + 1); }
        else if (['ArrowLeft', 'PageUp'].includes(key)) { event.preventDefault(); updateSlide(currentSlide - 1); }
        else if (key === 'Home') { event.preventDefault(); updateSlide(1); }
        else if (key === 'End') { event.preventDefault(); updateSlide(totalSlides); }
      }
    });
    window.addEventListener('hashchange', () => {
      const match = location.hash.match(/^#slide-(\\d+)$/);
      if (match) updateSlide(Number(match[1]), false);
    });
    const initial = location.hash.match(/^#slide-(\\d+)$/);
    updateSlide(initial ? Number(initial[1]) : 1);
  </script></body></html>`;
let destination='optimize_compilation_cache_langflow_CV.html';
if(fs.existsSync(destination)){
  const remembered=path.join(qa,'build_destination.json');
  if(!fs.existsSync(remembered)) destination='optimize_compilation_cache_langflow_CV_worker_20261006.html';
}
fs.writeFileSync(destination,head+header+slides.join('\n')+'</div></main>'+script);
fs.writeFileSync(path.join(qa,'build_destination.json'),JSON.stringify({destination:path.resolve(destination),slides:slides.length,source_html:path.resolve('optimize_final_langflow_CV.html')},null,2));
fs.writeFileSync(path.join(qa,'numeric_evidence.json'),JSON.stringify(numberEvidence,null,2));
console.log(JSON.stringify({destination,slides:slides.length,numeric_values:numberEvidence.length},null,2));
