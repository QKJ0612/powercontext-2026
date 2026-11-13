# 端侧知识库 · 浏览器本地向量化 + seekdb 混合检索

阶跃星辰赛道参赛作品（20 分）。**embedding 全部在你的浏览器里算**，向量与原文写入 seekdb，检索由 seekdb 在库内完成向量 + 全文的 RRF 融合。

```
浏览器（Transformers.js · WebGPU / WASM 降级）
   │  只传「向量 + 原文」
   ▼
Node 侧车（seekdb-js）──► seekdb 服务端 :2881（向量索引 + ik 全文索引，库内 hybridSearch）
```

## 一、环境要求

| 项 | 要求 |
|---|---|
| Node.js | **≥ 20**（seekdb-js 的 engines 要求） |
| seekdb | Docker 镜像 `oceanbase/seekdb`，暴露 2881 |
| 浏览器 | Chrome / Edge 最新版（WebGPU 可用时自动启用，否则自动降级 WASM） |

## 二、启动

### 1. 起 seekdb

```bash
docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:latest
# 等日志出现 "SeekDB SQL ready"
docker logs -f seekdb
```

### 2. 装依赖

```bash
npm install --legacy-peer-deps
```

> **为什么要 `--legacy-peer-deps`**：`seekdb@1.4.0` 把 `@seekdb/js-bindings`（嵌入式模式的原生绑定）列为可选依赖，而该包的元数据里带 `workspace:*` 协议，npm 在 monorepo 外解析会直接报 `EUNSUPPORTEDPROTOCOL`。本题只用 **server 模式**，不依赖这个原生绑定，跳过 peer 解析即可正常安装。这是上游打包问题，不是本项目的配置问题。

### 3. 模型文件

仓库已内置 INT8 量化模型（`models/bge-small-zh-v1.5/onnx/model_quantized.onnx`，**22.90 MiB**），**离线可用，不会访问 HuggingFace**。

模型为 `BAAI/bge-small-zh-v1.5`（中文，512 维）；ONNX 版本来自 `Xenova/bge-small-zh-v1.5`。

若要做「量化前后」对比，需要 FP32 权重（**90.46 MiB**，不入库）：

```bash
bash scripts/fetch-fp32.sh   # 从 hf-mirror.com 拉取 onnx/model.onnx
```

### 4. 启动应用

```bash
node server/server.mjs
# [gmb] listening on http://0.0.0.0:3080  seekdb=127.0.0.1:2881  collection=kb_zh_512
```

打开 <http://localhost:3080>。

### 5. 走一遍

1. 点「载入示例语料」→「切分并导入」，观察进度条与「性能观测」里的分阶段耗时
2. 输入查询（例如「向量数据库怎么做混合检索」）→「检索」，看排序结果与 `rrf` 分数

## 三、环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `3080` | 本服务端口 |
| `SEEKDB_HOST` / `SEEKDB_PORT` | `127.0.0.1` / `2881` | seekdb 地址 |
| `SEEKDB_USER` / `SEEKDB_PASSWORD` | `root` / 空 | 连接凭据 |
| `SEEKDB_DATABASE` | `test` | 库名 |
| `KB_COLLECTION` | `kb_zh_512` | 集合名（一个集合 = 一个知识库；名字带维度后缀便于换模型） |

## 四、接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 自检：seekdb 连通性、集合、条数、维度 |
| POST | `/api/import` | 批量写入 `{docs:[{id,text,vector}]}` |
| POST | `/api/search` | 混合检索 `{query, vector, k}` → 库内 RRF 排序 |
| POST | `/api/reset` | 删除并重建集合 |

服务端**不做 embedding**，也不做两路结果的客户端拼合——`/api/search` 里只有一次 `collection.hybridSearch()` 调用。

## 五、验证与实验

- `scripts/measure-quality.mjs`：在固定查询集上同时跑 INT8 与 FP32，输出 Recall@5 / MRR 对比
- `scripts/bench.mjs`：分阶段计时（向量化、写入、检索），重复多次取中位数
- `TECHNOTE.md`：一页技术说明，含实测数据

## 六、已知边界

1. 示例语料仅 10 段，用于功能演示；性能实验用 `scripts/` 里的批量生成器扩到 500 段。
2. WebGPU 在无头浏览器（SwiftShader）下不可用，自动化测试走的是 WASM 路径；WebGPU 路径需在真实 Chrome 中验证。
3. 中文全文检索使用 seekdb 的 `ik` 分词器；英文短查询同样可用，但停用词较多的长句建议改写成关键词。
