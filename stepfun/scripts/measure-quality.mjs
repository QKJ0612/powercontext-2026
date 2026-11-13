/**
 * 量化前后检索质量对比：INT8(q8) vs FP32
 *
 * 口径（可解释、可复现）：
 *   - 语料 24 条，覆盖 8 个主题，每条一句话
 *   - 8 个查询，每个标注 1 条 gold 文档（与查询同义、指向同一知识点）
 *   - Recall@5 = gold 是否出现在前 5 条（0/1）
 *   - MRR@5    = 1 / gold 的名次（未进前 5 记 0）
 *   - 因为只有一条 gold，Recall@5 与「命中率」等价，便于横向对比两个精度
 *
 * 用法：node scripts/measure-quality.mjs
 * 前提：http://127.0.0.1:3080 可访问；FP32 权重已下载。
 */
import { chromium } from 'playwright';

const APP = process.env.APP_URL || 'http://127.0.0.1:3080';

const DOCS = [
  // 0 混合检索
  '混合检索把向量相似度和关键词命中放进同一次查询。',
  'RRF 是倒数排名融合，只看名次不看分数。',
  '两路结果在数据库内部融合，不在客户端拼。',
  // 1 向量索引
  'HNSW 是常用的近似最近邻索引。',
  '向量索引需要固定维度，这里是 512 维。',
  '建索引时会占用额外的内存。',
  // 2 全文索引
  '全文索引依赖分词器，中文常用 ik。',
  '分词粒度影响召回，粗粒度会漏词。',
  '全文检索擅长精确的关键词匹配。',
  // 3 端侧推理
  '端侧推理指模型在用户设备上运行。',
  '浏览器里可以用 WASM 跑小模型。',
  'WebGPU 能加速端侧的矩阵运算。',
  // 4 量化
  'INT8 量化把权重压到 8 位整数。',
  '量化后模型体积约为原来的四分之一。',
  '量化会带来轻微的质量下降。',
  // 5 评测指标
  'Recall@K 衡量前 K 条里召回了多少相关文档。',
  'MRR 关注第一个相关结果的名次。',
  'nDCG 同时考虑排序位置和相关性等级。',
  // 6 数据写入
  '写入新文档后要刷新索引才能检索到。',
  '批量写入比逐条写入的吞吐更高。',
  '集合可以隔离不同的知识库。',
  // 7 数据库选型
  'seekdb 同时支持向量检索和全文检索。',
  '嵌入式模式适合本地的小应用。',
  '服务端模式可以跨网络访问。',
];

// [查询, gold 文档下标]
const EVAL = [
  ['向量和关键词怎么在一次查询里融合', 0],
  ['近似最近邻用什么索引', 3],
  ['中文分词器影响什么', 6],
  ['模型怎么在浏览器里跑', 10],
  ['把权重压到 8 位会怎样', 12],
  ['怎么衡量检索排序质量', 15],
  ['新写入的内容为什么搜不到', 18],
  ['哪种数据库能同时做向量和全文', 21],
];

const log = (...a) => console.log(...a);

async function runOnce(page, dtype) {
  await page.goto(`${APP}/?dtype=${dtype}`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(
    () => {
      const s = document.getElementById('importStatus');
      return s && (s.textContent.includes('模型就绪') || s.textContent.includes('失败'));
    },
    { timeout: 240000 },
  );
  const st = await page.textContent('#importStatus');
  if (st.includes('失败')) throw new Error(`dtype=${dtype} 模型加载失败`);

  await page.evaluate(() => fetch('/api/reset', { method: 'POST' }));
  await page.fill('#corpus', DOCS.join('\n\n'));
  await page.click('#btnImport');
  await page.waitForFunction(
    () => {
      const s = document.getElementById('importStatus');
      return s && (s.textContent.includes('已导入') || s.textContent.includes('失败'));
    },
    { timeout: 300000 },
  );
  if ((await page.textContent('#importStatus')).includes('失败')) throw new Error(`dtype=${dtype} 导入失败`);

  const rowsPerQuery = [];
  for (const [q, gold] of EVAL) {
    await page.fill('#query', q);
    await page.click('#btnSearch');
    await page.waitForFunction(
      () => {
        const l = document.getElementById('results');
        return l && !l.textContent.includes('还没有结果') && !l.textContent.includes('检索失败');
      },
      { timeout: 120000 },
    );
    const texts = await page.$$eval('#results li .text', (els) => els.map((e) => e.textContent));
    const rank = texts.indexOf(DOCS[gold]) + 1; // 0 = 未命中
    rowsPerQuery.push({ q, rank });
  }

  const recall5 = rowsPerQuery.filter((r) => r.rank > 0 && r.rank <= 5).length / rowsPerQuery.length;
  const mrr5 = rowsPerQuery.reduce((a, r) => a + (r.rank > 0 && r.rank <= 5 ? 1 / r.rank : 0), 0) / rowsPerQuery.length;
  const top1 = rowsPerQuery.filter((r) => r.rank === 1).length / rowsPerQuery.length;
  return { dtype, recall5, mrr5, top1, rowsPerQuery };
}

const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));

const out = [];
for (const dtype of ['q8', 'fp32']) {
  try {
    const r = await runOnce(page, dtype);
    out.push(r);
    log(`[${dtype}] Recall@5=${r.recall5.toFixed(3)}  MRR@5=${r.mrr5.toFixed(3)}  Top1=${(r.top1 * 100).toFixed(1)}%`);
    log('   ranks: ' + r.rowsPerQuery.map((x) => x.rank).join(','));
  } catch (e) {
    log(`[${dtype}] 失败：${e.message}`);
  }
}

if (out.length === 2) {
  log('\n| 精度 | Recall@5 | MRR@5 | Top1 命中率 |');
  log('|---|---|---|---|');
  for (const r of out) {
    log(`| ${r.dtype === 'q8' ? 'INT8 (q8)' : 'FP32'} | ${r.recall5.toFixed(3)} | ${r.mrr5.toFixed(3)} | ${(r.top1 * 100).toFixed(1)}% |`);
  }
  log(`\nΔ Recall@5 = ${(out[0].recall5 - out[1].recall5).toFixed(3)}（INT8 − FP32）`);
}
if (errors.length) log('page errors:', errors.slice(0, 3));
await browser.close();
log('QUALITY_DONE');
