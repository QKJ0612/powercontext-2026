/**
 * 性能实测：分阶段计时 + 多次取中位数
 *
 * 用法：node scripts/bench.mjs [--docs 500] [--queries 20] [--dtype q8]
 * 前提：服务已在 http://127.0.0.1:3080 运行，seekdb 可连。
 */
import { chromium } from 'playwright';

const APP = process.env.APP_URL || 'http://127.0.0.1:3080';
const arg = (name, def) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 ? Number(process.argv[i + 1]) : def;
};
const N_DOCS = arg('docs', 500);
const N_QUERIES = arg('queries', 20);
const DTYPE = process.argv.includes('--dtype') ? process.argv[process.argv.indexOf('--dtype') + 1] : 'q8';

// 合成语料：尽量贴近真实技术文档的用词分布
const VERBS = ['实现了', '优化了', '重构了', '修复了', '接入了', '验证了', '压测了', '上线了'];
const NOUNS = ['向量检索', '全文索引', '混合排序', '分词器', '索引刷新', '批量写入', '端侧推理', '量化流程', '缓存命中', '并发控制'];
const TAILS = ['后延迟下降 12%', '后吞吐提升 30%', '后发现一处边界缺陷', '后召回率保持不变', '后内存占用降低', '后稳定性明显改善'];

function makeDocs(n) {
  const out = [];
  for (let i = 0; i < n; i += 1) {
    const v = VERBS[i % VERBS.length];
    const a = NOUNS[(i * 3) % NOUNS.length];
    const b = NOUNS[(i * 7 + 1) % NOUNS.length];
    const t = TAILS[(i * 5) % TAILS.length];
    out.push(`实验 ${i}：${v}${a}与${b}的联动，${t}。这是第 ${i} 组对照。`);
  }
  return out;
}

const median = (xs) => {
  const s = [...xs].sort((x, y) => x - y);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const p95 = (xs) => {
  const s = [...xs].sort((x, y) => x - y);
  return s[Math.min(s.length - 1, Math.floor(s.length * 0.95))];
};

const num = (txt) => {
  if (!txt) return NaN;
  const m = txt.match(/([\d.]+)\s*(ms|s)/);
  if (!m) return NaN;
  return m[2] === 's' ? Number(m[1]) * 1000 : Number(m[1]);
};

const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));

await page.goto(`${APP}/?dtype=${DTYPE}`, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(
  () => {
    const s = document.getElementById('importStatus');
    return s && (s.textContent.includes('模型就绪') || s.textContent.includes('失败'));
  },
  { timeout: 240000 },
);
const device = (await page.textContent('#badgeDevice')).replace('设备：', '');
console.log(`dtype=${DTYPE}  device=${device}`);

// 清库 + 导入
await page.evaluate(() => fetch('/api/reset', { method: 'POST' }));
const docs = makeDocs(N_DOCS);
await page.fill('#corpus', docs.join('\n\n'));
const tImport = Date.now();
await page.click('#btnImport');
await page.waitForFunction(
  () => {
    const s = document.getElementById('importStatus');
    return s && (s.textContent.includes('已导入') || s.textContent.includes('失败'));
  },
  { timeout: 900000 },
);
const importWall = Date.now() - tImport;
const st = await page.textContent('#importStatus');
if (st.includes('失败')) throw new Error(st);

const perf = await page.evaluate(() => {
  const g = (id) => document.getElementById(id)?.textContent;
  return {
    load: g('pLoad'), first: g('pFirst'), docs: g('pDocs'), docsNote: g('pDocsNote'),
    write: g('pWrite'), count: g('badgeDocs'),
  };
});
console.log('import:', JSON.stringify(perf), `wall=${importWall}ms`);

// 查询延迟：重复 N_QUERIES 次
const qs = [];
for (let i = 0; i < N_QUERIES; i += 1) {
  const q = `${NOUNS[i % NOUNS.length]} 在实验 ${(i * 37) % N_DOCS} 里的表现`;
  await page.fill('#query', q);
  const t0 = Date.now();
  await page.click('#btnSearch');
  await page.waitForFunction(
    () => {
      const l = document.getElementById('results');
      return l && !l.textContent.includes('还没有结果') && !l.textContent.includes('检索失败');
    },
    { timeout: 120000 },
  );
  const e2e = Date.now() - t0;
  const row = await page.evaluate(() => ({
    qemb: document.getElementById('pQEmb')?.textContent,
    search: document.getElementById('pSearch')?.textContent,
    e2e: document.getElementById('pE2E')?.textContent,
    n: document.querySelectorAll('#results li').length,
  }));
  qs.push({ qemb: num(row.qemb), search: num(row.search), e2e, n: row.n });
}

const qeg = qs.map((x) => x.qemb).filter((x) => !Number.isNaN(x));
const srch = qs.map((x) => x.search).filter((x) => !Number.isNaN(x));
const e2e = qs.map((x) => x.e2e).filter((x) => !Number.isNaN(x));
console.log(
  `query latency (n=${qs.length}) → 查询向量化 median=${median(qeg).toFixed(1)}ms p95=${p95(qeg).toFixed(1)}ms | ` +
    `seekdb检索 median=${median(srch).toFixed(1)}ms p95=${p95(srch).toFixed(1)}ms | ` +
    `端到端 median=${median(e2e).toFixed(0)}ms p95=${p95(e2e).toFixed(0)}ms`,
);
if (errors.length) console.log('page errors:', errors.slice(0, 3));
await browser.close();
console.log('BENCH_DONE');
