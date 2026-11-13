/**
 * seekdb 侧车服务
 *
 * 职责边界（对着赛题要求）：
 *   - 只做两件事：把浏览器算好的向量写进 seekdb；把混合检索交给 seekdb 执行
 *   - 绝不在服务端做 embedding，也绝不把 vector / fulltext 两路结果取回来自己拼
 *   - 混合检索由 seekdb 的 hybridSearch（RRF 融合）在库内完成
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SeekdbClient, Schema, FulltextIndexConfig, VectorIndexConfig } from 'seekdb';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const WEB_DIR = path.join(ROOT, 'web');
const MODELS_DIR = path.join(ROOT, 'models');
const HF_DIST = path.join(ROOT, 'node_modules', '@huggingface', 'transformers', 'dist');

const PORT = Number(process.env.PORT || 3080);
const SEEKDB_HOST = process.env.SEEKDB_HOST || '127.0.0.1';
const SEEKDB_PORT = Number(process.env.SEEKDB_PORT || 2881);
const SEEKDB_USER = process.env.SEEKDB_USER || 'root';
const SEEKDB_PASSWORD = process.env.SEEKDB_PASSWORD || '';
const SEEKDB_DB = process.env.SEEKDB_DATABASE || 'test';
const COLLECTION = process.env.KB_COLLECTION || 'kb_zh_512';
const DIM = 512;

const client = new SeekdbClient({
  host: SEEKDB_HOST,
  port: SEEKDB_PORT,
  user: SEEKDB_USER,
  password: SEEKDB_PASSWORD,
  database: SEEKDB_DB,
});

// 向量维度由 embeddingFunction 声明；浏览器端算好的向量直接传入，这个 stub 不会被调用去生成
const dimStub = {
  name: 'browser-local-bge-zh',
  dimension: DIM,
  generate: async (texts) => texts.map(() => new Array(DIM).fill(0)),
  getConfig: () => ({ model: 'bge-small-zh-v1.5', dimension: DIM }),
};

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.onnx': 'application/octet-stream',
  '.wasm': 'application/wasm',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

function send(res, code, body, headers = {}) {
  const data = typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body);
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', ...headers });
  res.end(data);
}

function readBody(req, limit = 64 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > limit) {
        reject(new Error('payload too large'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      try {
        resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {});
      } catch {
        reject(new Error('invalid json'));
      }
    });
    req.on('error', reject);
  });
}

async function ensureCollection() {
  try {
    return await client.getCollection({ name: COLLECTION, embeddingFunction: dimStub });
  } catch {
    const schema = new Schema({
      fulltextIndex: new FulltextIndexConfig('ik'),
      vectorIndex: new VectorIndexConfig({ embeddingFunction: dimStub }),
    });
    return client.createCollection({ name: COLLECTION, schema, embeddingFunction: dimStub });
  }
}

let collection = null;

async function collectionStats() {
  const col = collection || (await ensureCollection());
  let count = 0;
  let ids = [];
  try {
    const got = await col.get({ limit: 1000, include: ['documents'] });
    ids = got?.ids ?? [];
    count = ids.length;
  } catch {}
  return { collection: COLLECTION, count };
}

function serveStatic(req, res, urlPath) {
  let base = WEB_DIR;
  let rel = urlPath;
  if (urlPath.startsWith('/models/')) {
    base = MODELS_DIR;
    rel = urlPath.slice('/models'.length);
  } else if (urlPath.startsWith('/vendor/')) {
    base = HF_DIST;
    rel = urlPath.slice('/vendor'.length);
  }
  const safeRel = path.normalize(rel).replace(/^(\.\.[/\\])+/, '');
  const full = path.join(base, safeRel);
  if (!full.startsWith(base)) return send(res, 403, { error: 'forbidden' });
  fs.stat(full, (err, st) => {
    if (err || !st.isFile()) return send(res, 404, { error: 'not found', path: urlPath });
    const ext = path.extname(full).toLowerCase();
    res.writeHead(200, {
      'content-type': MIME[ext] || 'application/octet-stream',
      'content-length': st.size,
      'cache-control': ext === '.onnx' ? 'public, max-age=86400' : 'no-cache',
    });
    fs.createReadStream(full).pipe(res);
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const p = url.pathname;

  try {
    if (p === '/api/health') {
      const t0 = performance.now();
      const s = await collectionStats();
      return send(res, 200, {
        ok: true,
        ...s,
        dim: DIM,
        seekdb: { host: SEEKDB_HOST, port: SEEKDB_PORT, database: SEEKDB_DB },
        healthMs: +(performance.now() - t0).toFixed(2),
      });
    }

    if (p === '/api/import' && req.method === 'POST') {
      const body = await readBody(req);
      const docs = Array.isArray(body.docs) ? body.docs : [];
      if (!docs.length) return send(res, 400, { error: 'docs 为空' });
      for (const d of docs) {
        if (!d.id || typeof d.text !== 'string' || !Array.isArray(d.vector)) {
          return send(res, 400, { error: 'docs 每项需含 id / text / vector' });
        }
        if (d.vector.length !== DIM) {
          return send(res, 400, { error: `vector 维度应为 ${DIM}，收到 ${d.vector.length}` });
        }
      }
      collection = collection || (await ensureCollection());
      const t0 = performance.now();
      // upsert + 内容哈希 id：同一段文本重复导入只会覆盖，不会产生重复条目
      await collection.upsert({
        ids: docs.map((d) => String(d.id)),
        documents: docs.map((d) => d.text),
        embeddings: docs.map((d) => d.vector),
        metadatas: docs.map((d) => ({ source: d.source || 'user' })),
      });
      const addMs = performance.now() - t0;
      return send(res, 200, { ok: true, added: docs.length, addMs: +addMs.toFixed(2) });
    }

    if (p === '/api/search' && req.method === 'POST') {
      const body = await readBody(req);
      const { query, vector, k = 5 } = body || {};
      if (!Array.isArray(vector) || vector.length !== DIM) {
        return send(res, 400, { error: `vector 维度应为 ${DIM}` });
      }
      if (typeof query !== 'string' || !query.trim()) {
        return send(res, 400, { error: 'query 不能为空' });
      }
      collection = collection || (await ensureCollection());
      const t0 = performance.now();
      // 关键：向量与全文两路都在 seekdb 内部检索并由 RRF 融合，服务端只拿最终排序
      const result = await collection.hybridSearch({
        query: { whereDocument: { $contains: query } },
        knn: { queryEmbeddings: [vector] },
        rank: { rrf: { rankWindowSize: 40, rankConstant: 60 } },
        nResults: Math.max(1, Math.min(20, Number(k) || 5)),
        include: ['documents', 'metadatas', 'distances'],
      });
      const searchMs = performance.now() - t0;
      const rows = result?.rows ?? result;
      const ids = rows?.ids?.[0] ?? [];
      const docs = rows?.documents?.[0] ?? [];
      const metas = rows?.metadatas?.[0] ?? [];
      const dists = rows?.distances?.[0] ?? [];
      return send(res, 200, {
        ok: true,
        engine: 'seekdb.hybridSearch(rrf)',
        searchMs: +searchMs.toFixed(2),
        hits: ids.map((id, i) => ({ id, text: docs[i], meta: metas[i], score: dists[i] })),
      });
    }

    if (p === '/api/reset' && req.method === 'POST') {
      let delErr = null;
      try {
        // 注意：deleteCollection 的签名是 deleteCollection(name: string)
        await client.deleteCollection(COLLECTION);
      } catch (e) {
        delErr = String(e?.message || e);
      }
      collection = await ensureCollection();
      return send(res, 200, { ok: true, collection: COLLECTION, delErr });
    }

    if (p === '/' || p === '/index.html') return serveStatic(req, res, '/index.html');
    if (p.startsWith('/api/')) return send(res, 404, { error: 'unknown api' });
    return serveStatic(req, res, p);
  } catch (e) {
    return send(res, 500, { error: String(e?.message || e) });
  }
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[gmb] listening on http://0.0.0.0:${PORT}  seekdb=${SEEKDB_HOST}:${SEEKDB_PORT}  collection=${COLLECTION}`);
});
