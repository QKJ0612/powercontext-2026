# 技术说明（一页）

**作品**：端侧知识库 —— 浏览器本地向量化 + seekdb 混合检索
**赛题**：seekdb × 阶跃星辰 · 端侧 AI 推理性能优化与 seekdb 混合检索

---

## 1. 模型与量化

| 项 | 值 |
|---|---|
| 模型 | `BAAI/bge-small-zh-v1.5`（ONNX 版本：`Xenova/bge-small-zh-v1.5`） |
| 来源 | https://huggingface.co/BAAI/bge-small-zh-v1.5 ，ONNX 权重由 Xenova 转换并公开 |
| 向量维度 | 512 |
| 量化方案 | **INT8**（`dtype: 'q8'`，ONNX Runtime 动态量化权重）；基线为 FP32（`dtype: 'fp32'`） |
| 推理运行时 | Transformers.js v3（`@huggingface/transformers`）+ ONNX Runtime Web |
| 池化 | mean pooling + L2 归一化 |

**体积对比（同一口径：仅统计 onnx 权重文件字节数）**

| 精度 | 文件 | 字节数 | MiB | 压缩比 |
|---|---|---|---|---|
| FP32 | `onnx/model.onnx` | 94,851,877 | 90.46 | 1.00×（基线） |
| INT8 | `onnx/model_quantized.onnx` | 24,010,842 | 22.90 | **3.95×** |

**检索质量变化**（同一语料、同一查询集，只换精度）

| 精度 | Recall@5 | MRR@5 | Top1 命中率 |
|---|---|---|---|
| FP32 | 1.000 | 0.938 | 87.5% |
| INT8 | 1.000 | 0.917 | 87.5% |
| 变化 | ±0.000 | **−0.021** | ±0.0 |

- 语料 24 条（8 个主题 × 3 句），8 个查询各标注 1 条 gold 文档
- Recall@5 = gold 是否进入前 5；MRR@5 = 1/名次
- 结论：**INT8 召回无损，排序略有下降（8 个查询里有 1 个从第 2 名掉到第 3 名）**，换回 3.95× 的体积收益是划算的

## 2. 端侧运行与后端降级

- **推理位置**：文档与查询的 embedding 全部在浏览器里计算。服务端只接收 `vector + text`，代码里没有任何 embedding 调用（`server/server.mjs` 只有 seekdb 客户端）。
- **后端选择**：优先 `device: 'webgpu'`；先 `navigator.gpu.requestAdapter()` 探测，适配器为空或初始化抛错时**自动降级**到 `device: 'wasm'`，并在顶部标签与导入状态里明确写出当前后端（`设备：WASM（降级）`）。
- **离线可用**：`env.allowRemoteModels = false`，模型从本机 `/models/` 读取；ORT 的 `.wasm` 也从 `/vendor/` 由本机提供（`env.backends.onnx.wasm.wasmPaths`），不访问任何 CDN。
- **单线程策略**：`wasm.numThreads = 1`，避免未开 COOP/COEP 时 SharedArrayBuffer 不可用导致的偶发失败。

验证记录（无头 Chromium，SwiftShader 无 WebGPU）：

```
设备：WASM（降级）  精度：q8(INT8)  模型：bge-small-zh-v1.5
控制台错误：0
```

> **诚实说明**：WebGPU 路径在无头浏览器里不可用（无 GPU 适配器），本次只验证了探测→降级链路。WebGPU 实际加速效果需在真实 Chrome（开启硬件加速）中查看——应用会在该环境下自动选择 `webgpu`。

## 3. seekdb 使用

**集合结构**（`SeekdbClient.createCollection`）：

```js
new Schema({
  fulltextIndex: new FulltextIndexConfig('ik'),          // 中文分词，倒排索引
  vectorIndex:   new VectorIndexConfig({ ... }),         // 512 维稠密向量（HNSW）
})
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | `d<时间戳>_<序号>` |
| `document` | text | 原文（用于全文索引 + 结果展示） |
| `metadata.source` | string | 来源标记（`user` / 导入批次） |
| `embedding` | float[512] | 浏览器算出的向量 |

**混合检索方式**：**一次调用、库内融合**。

```js
await collection.hybridSearch({
  query: { whereDocument: { $contains: q } },   // 全文路
  knn:   { queryEmbeddings: [vec] },            // 向量路
  rank:  { rrf: { rankWindowSize: 40, rankConstant: 60 } },
  nResults: k,
  include: ['documents', 'metadatas', 'distances'],
});
```

服务端拿到的已经是**融合后的单一排序**，不存在「两路结果取回来再自己拼」——这是赛题的硬性要求，代码里只有这一处检索调用。

**增量写入与索引刷新**：文档 id 取**内容哈希**，写入用 `collection.upsert()`——同一段文本重复导入只覆盖不重复（实测：同一 id 连写两次，库内仍为 1 条）。写入即对后续 `hybridSearch` 可见（实测：导入后立刻检索即可命中新文档）；每次导入按 8 段一批做 embedding，最后一次性批量写入，减少往返。

**集合与会话状态**：一个集合 = 一个知识库（`kb_zh_512`），集合名带维度后缀，避免换模型时维度冲突；`/api/reset` 通过 `deleteCollection(name)` 重建，保证「换会话/清空后检索结果与当前知识库一致」。

## 4. 状态与优化

**已做的优化**

| 优化点 | 做法 | 效果（实测） |
|---|---|---|
| 批处理 | embedding 每批 8 段 | 500 篇 11.11 s，≈45 段/秒（INT8） |
| 权重精度 | INT8 替代 FP32 | 体积 3.95×↓，向量化 1.43×↑ |
| 模型加载 | 本地文件 + 浏览器缓存 | 冷启动 710 ms（INT8） |
| 结果集大小 | `nResults` 限制 5–20 | 检索 8 ms（中位） |
| 写入 | 批量 `add` 一次提交 | 500 篇写入 334 ms |

**分阶段实测**（WSL2 / 24 核 / 7.8 GB，无头 Chromium，**WASM 单线程**，500 篇语料）

| 配置 | 模型加载 | 首推理 | 文档向量化 | 写入 seekdb | 查询向量化（中位） | seekdb 检索（中位） | 端到端（中位 / p95） | 检索质量 |
|---|---|---|---|---|---|---|---|---|
| FP32 + WASM | 1.13 s | 61 ms | 15.90 s（31.5 段/秒） | 326 ms | 28 ms | 7 ms | 75 ms / 105 ms | R@5 1.000 / MRR 0.938 |
| **INT8 + WASM** | **710 ms** | 74 ms | **11.11 s（45.0 段/秒）** | 334 ms | **20 ms** | 8 ms | **60 ms / 123 ms** | R@5 1.000 / MRR 0.917 |
| INT8 + WebGPU | 未实测（见 §2 说明） | — | — | — | — | — | — | — |

> 计时口径：查询阶段为「点击检索 → 结果渲染完成」；查询向量的耗时取 20 次重复的中位数与 p95，未挑选最快一次。首次加载与预热后性能分开记录（`模型加载` / `首推理` 单列）。

## 5. 复现

```bash
docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:latest
npm install --legacy-peer-deps
bash scripts/fetch-fp32.sh          # 仅做 FP32 对比时需要
node server/server.mjs              # http://localhost:3080
node scripts/measure-quality.mjs    # 质量对比
node scripts/bench.mjs --docs 500 --queries 20 --dtype q8
```

数据与生成方式：`scripts/measure-quality.mjs` 内置 24 条标注语料与 8 个查询；`scripts/bench.mjs` 用模板生成 500 条合成文档，种子固定、可重复。

## 6. 已知边界

1. WebGPU 未在真实 GPU 上实测，只验证了探测与降级链路。
2. 中文长句作为全文检索条件时，seekdb 的 `$contains` 对多词做与运算，命中率不如短关键词；检索质量主要来自向量路，全文路起到精确词补充作用。
3. 语料规模上限受浏览器内存限制；500 篇规模下页面内存占用约 300 MB（含模型）。
4. INT8 在 8 条查询里有 1 条排序下降 1 位，样本量小，不能外推为普遍规律。
