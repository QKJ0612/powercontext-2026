import { pipeline, env } from '/vendor/transformers.min.js';

// ---- 本地模型：禁止远端下载，全部从本机 /models 读取 -----------------------
env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = '/models/';
// ORT 的 wasm 与 dist 同目录，随 /vendor/*.wasm 一起由本机提供，不访问 CDN
env.backends.onnx.wasm.wasmPaths = '/vendor/';
env.backends.onnx.wasm.numThreads = 1; // 单线程更稳，避免无 COOP/COEP 时 SharedArrayBuffer 不可用

const MODEL_ID = 'bge-small-zh-v1.5';
const DIM = 512;
// 精度可由 URL 指定：?dtype=q8（默认，INT8）| ?dtype=fp32（基线）
// FP32 权重需要先跑 scripts/fetch-fp32.sh，否则会加载失败并回退 q8
const DTYPE = (new URLSearchParams(location.search).get('dtype') || 'q8').toLowerCase();
// BGE 中文模型对「查询」建议加指令前缀，文档侧不加
const QUERY_PREFIX = '为这个句子生成表示以用于检索相关文章：';

const $ = (id) => document.getElementById(id);
const now = () => performance.now();
const fmt = (ms) => (ms >= 1000 ? (ms / 1000).toFixed(2) + ' s' : ms.toFixed(0) + ' ms');

let extractor = null;
let device = 'unknown';
let bootMs = 0;
let firstInferMs = null;

// ---------------- 后端（seekdb）健康检查 ----------------
async function health() {
  try {
    const r = await fetch('/api/health');
    const j = await r.json();
    $('dotBackend').className = 'dot ok';
    $('badgeDocs').textContent = `库内：${j.count} 条`;
    $('envNote').textContent = `seekdb ${j.seekdb.host}:${j.seekdb.port} · collection=${j.collection} · dim=${j.dim}`;
    return j;
  } catch (e) {
    $('dotBackend').className = 'dot bad';
    $('envNote').textContent = 'seekdb 未连接：' + e.message;
    return null;
  }
}
async function refreshCount() {
  const j = await health();
  if (j) $('badgeDocs').textContent = `库内：${j.count} 条`;
}

// ---------------- 模型加载：优先 WebGPU，失败自动降级 WASM ----------------
async function pickDevice() {
  const hasGpu = typeof navigator !== 'undefined' && !!navigator.gpu;
  if (!hasGpu) return 'wasm';
  try {
    const adapter = await navigator.gpu.requestAdapter();
    return adapter ? 'webgpu' : 'wasm';
  } catch {
    return 'wasm';
  }
}

async function loadModel() {
  const want = await pickDevice();
  const order = want === 'webgpu' ? ['webgpu', 'wasm'] : ['wasm'];
  let lastErr = null;
  for (const dev of order) {
    try {
      const t0 = now();
      extractor = await pipeline('feature-extraction', MODEL_ID, { dtype: DTYPE, device: dev });
      bootMs = now() - t0;
      device = dev;
      break;
    } catch (e) {
      lastErr = e;
      console.warn(`[model] device=${dev} 失败：`, e?.message || e);
    }
  }
  if (!extractor) throw lastErr || new Error('模型加载失败');

  $('badgeDevice').textContent = `设备：${device === 'webgpu' ? 'WebGPU' : 'WASM（降级）'}`;
  $('badgeDevice').classList.toggle('warn', device !== 'webgpu');
  $('pLoad').textContent = fmt(bootMs);

  // 预热一次，单独记录首次推理耗时
  const t1 = now();
  await extract('warmup');
  firstInferMs = now() - t1;
  $('pFirst').textContent = fmt(firstInferMs);

  if (want === 'webgpu' && device === 'wasm') {
    $('importStatus').textContent = 'WebGPU 不可用，已自动降级到 WASM（功能不受影响，速度较慢）';
  }
  return device;
}

// ---------------- 本地 embedding ----------------
async function extract(texts) {
  const out = await extractor(Array.isArray(texts) ? texts : [texts], { pooling: 'mean', normalize: true });
  return out.tolist();
}

async function embedBatch(texts, batchSize, onProgress) {
  const vectors = [];
  const t0 = now();
  for (let i = 0; i < texts.length; i += batchSize) {
    const slice = texts.slice(i, i + batchSize);
    const vs = await extract(slice);
    vectors.push(...vs);
    onProgress?.(Math.min(i + batchSize, texts.length), texts.length);
    // 让出主线程，保持进度条和动画
    await new Promise((r) => setTimeout(r, 0));
  }
  return { vectors, ms: now() - t0 };
}

// ---------------- 语料切分 ----------------
function splitCorpus(raw) {
  return raw
    .split(/\n\s*\n|\r\n\s*\r\n/)
    .flatMap((block) => (block.includes('\n') ? block.split('\n') : [block]))
    .map((s) => s.trim())
    .filter((s) => s.length >= 8);
}

// 内容哈希 → 稳定 id。配合后端 upsert，同一段文本重复导入只会覆盖，不会产生重复条目。
function hash53(str) {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let i = 0; i < str.length; i += 1) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return 4294967296 * (2097151 & h2) + (h1 >>> 0);
}

const DEMO = `seekdb 是面向 AI 应用的数据库，同时支持向量检索和全文检索。
混合检索把向量相似度和关键词命中放进同一次查询，用 RRF 融合两路排序。
RRF 即倒数排名融合，不需要把两路分数归一化，只看各自的名次。
全文检索依赖分词器，中文常用 ik 分词，能识别"检索""数据库"这类词。
向量检索依赖 embedding 模型，bge-small-zh 输出 512 维向量。
端侧推理指模型在用户浏览器里运行，文档和查询都不离开设备。
WebGPU 能显著加速矩阵运算，浏览器不支持时回退到 WASM。
INT8 量化把权重从 32 位浮点压到 8 位整数，体积约为原来的四分之一。
知识库应用通常需要增量写入，新文档写入后要刷新索引才能被检索到。
检索质量常用 Recall@K 和 nDCG@K 衡量，量化会带来轻微下降。`;

// ---------------- 导入 ----------------
async function doImport() {
  const raw = $('corpus').value.trim();
  if (!raw) return alert('先粘贴或载入一些文本');
  const docs = splitCorpus(raw);
  if (!docs.length) return alert('切分后没有有效段落（每段至少 8 个字符）');

  $('btnImport').disabled = true;
  $('btnDemo').disabled = true;
  const t0 = now();
  try {
    const batchSize = 8;
    $('importStatus').textContent = `向量化 0/${docs.length}（${device.toUpperCase()}）`;
    const { vectors, ms } = await embedBatch(docs, batchSize, (done, total) => {
      $('bar').style.width = `${(done / total) * 60}%`;
      $('importStatus').textContent = `向量化 ${done}/${total}（${device.toUpperCase()}）`;
    });
    $('pDocs').textContent = fmt(ms);
    $('pDocsNote').textContent = `吞吐 ${(docs.length / (ms / 1000)).toFixed(1)} 段/秒 · 批大小 ${batchSize}`;

    $('importStatus').textContent = '写入 seekdb…';
    $('bar').style.width = '70%';
    const payload = {
      docs: docs.map((text, i) => ({
        id: `d${hash53(text).toString(36)}`,
        text,
        vector: vectors[i],
        source: 'user',
      })),
    };
    const tw = now();
    const r = await fetch('/api/import', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || '写入失败');
    const writeMs = now() - tw;
    $('pWrite').textContent = fmt(writeMs);
    $('bar').style.width = '100%';
    $('importStatus').textContent = `已导入 ${j.added} 段 · 端到端 ${fmt(now() - t0)}`;
    await refreshCount();
  } catch (e) {
    $('importStatus').textContent = '导入失败：' + (e?.message || e);
  } finally {
    $('btnImport').disabled = false;
    $('btnDemo').disabled = false;
    setTimeout(() => ($('bar').style.width = '0%'), 1200);
  }
}

// ---------------- 检索 ----------------
async function doSearch() {
  const q = $('query').value.trim();
  if (!q) return alert('输入查询');
  if (!extractor) return alert('模型还在加载');
  const k = Math.max(1, Math.min(20, Number($('k').value) || 5));

  const t0 = now();
  $('btnSearch').disabled = true;
  try {
    const te = now();
    const [vec] = await extract(QUERY_PREFIX + q);
    const embMs = now() - te;
    $('pQEmb').textContent = fmt(embMs);

    const r = await fetch('/api/search', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query: q, vector: vec, k }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || '检索失败');
    $('pSearch').textContent = fmt(j.searchMs);

    const list = $('results');
    list.innerHTML = '';
    if (!j.hits.length) {
      list.innerHTML = '<li class="empty">没有命中，先导入一些语料</li>';
    }
    for (const [i, h] of j.hits.entries()) {
      const li = document.createElement('li');
      const score = typeof h.score === 'number' ? h.score.toFixed(4) : '-';
      li.innerHTML = `<div class="rank">${i + 1}</div>
        <div class="body"><div class="text"></div><div class="meta">id=${h.id} · rrf=${score} · source=${h.meta?.source || '-'}</div></div>`;
      li.querySelector('.text').textContent = h.text; // 纯文本写入，避免 XSS
      list.appendChild(li);
    }
    $('pE2E').textContent = fmt(now() - t0);
  } catch (e) {
    $('results').innerHTML = `<li class="empty">检索失败：${e?.message || e}</li>`;
  } finally {
    $('btnSearch').disabled = false;
  }
}

// ---------------- 事件与启动 ----------------
$('btnImport').addEventListener('click', doImport);
$('btnDemo').addEventListener('click', () => ($('corpus').value = DEMO));
$('btnSearch').addEventListener('click', doSearch);
$('query').addEventListener('keydown', (e) => e.key === 'Enter' && doSearch());
$('btnReset').addEventListener('click', async () => {
  if (!confirm('清空 seekdb 里的这个知识库？')) return;
  await fetch('/api/reset', { method: 'POST' });
  $('results').innerHTML = '<li class="empty">已清空</li>';
  $('importStatus').textContent = '已清空';
  await refreshCount();
});
$('file').addEventListener('change', async (e) => {
  const files = [...e.target.files];
  if (!files.length) return;
  const texts = await Promise.all(files.map((f) => f.text()));
  $('corpus').value = texts.join('\n\n');
});

(async function boot() {
  $('importStatus').textContent = '加载模型…（首次需解析本地模型文件）';
  try {
    await loadModel();
    $('importStatus').textContent = `模型就绪（${device.toUpperCase()} · ${DTYPE}）`;
  } catch (e) {
    $('importStatus').textContent = '模型加载失败：' + (e?.message || e);
    $('badgeDevice').textContent = '设备：不可用';
    $('badgeDevice').classList.add('error');
  }
  await health();
})();
