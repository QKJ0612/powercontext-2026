# MiniMax × OceanBase：SDD 工程上下文模块（eco-v3-service）

面向"规格驱动开发（SDD）"的工程上下文模块：给一个开发任务，它从 **seekdb** 里检索适用的规格、代码和测试，叠加 **PowerContext** 里保存的任务进展，输出一份可直接交给 AI 开发助手的上下文；随后由 **MiniMax** 完成一次真实的代码修改，并用同一组测试验证改前失败、改后通过。

本分支（`eco-v3-service`）与 `eco-v2-resilient` 使用**同一个任务、同一份改前代码、同一份已录制的真实 MiniMax 会话**（`evidence/minimax_session.json`，`request_id=074715721a0fdb458a170fd4d08934b8`），把模块的对外形态换成了 **HTTP 服务 + MCP 风格 tool 定义**，检索算法换成 **BM25 + 规格版本号排序**；两个分支的差异在工程形态与检索实现，可用同一份会话对照复现。

## 目录

```
minimax/
├── context_module/         # 原有上下文模块（CLI 入口，保留兼容）
│   ├── cli.py
│   ├── docs_store.py       # seekdb 工程资料存取
│   ├── selector.py         # 旧的关键词/LIKE 选择器
│   └── state_store.py      # PowerContext 任务状态
├── context_service/        # 本分支新增：HTTP + BM25
│   ├── bm25.py             # BM25 索引、规格版本解析与加权
│   ├── service.py          # 上下文契约 + GET /state 数据
│   └── server.py           # POST /context, GET /state, /health, /mcp/tool
├── sample_project/
├── scripts/
│   ├── init_seekdb.py      # 导入 docs/spec-*.md、docs/design-*.md、样例代码/测试
│   ├── minimax_patch.py    # 改码流程；新增 --context-url 从服务取上下文
│   ├── service_smoke.py    # 启动服务并逐个端点验证
│   ├── service_e2e.py      # 服务提供上下文 → 回放改码 → 测试 → 证据
│   ├── state_reuse_demo.py
│   ├── state_read.py
│   └── spec_update_demo.py
├── tests/test_service.py   # BM25/版本排序单元测试
├── docs/mcp-tool.json      # MCP 风格 tool 定义
├── run_demo.sh
└── evidence/               # 含 service/ 服务证据
```

## 依赖

- Python 3.11+（本机 3.14.4）；HTTP 服务只用标准库，无额外 Web 框架
- `pip install -r requirements.txt`：`powercontext==1.2.0`、`pymysql`、`pytest`
- seekdb：**默认 `127.0.0.1:2881`**（官方默认），root 空密码；本机实验实例在 **2896**，先 `export SEEDB_PORT=2896`
- 统一环境变量：`SEEDB_HOST`（127.0.0.1）、`SEEDB_PORT`（2881）、`SEEDB_USER`（root）、`SEEDB_PASSWORD`（空）、`SEEDB_DATABASE`（test）；旧拼写 `SEEKDB_*` 兼容
- 备选：`export POWERCONTEXT_SRC=/path/to/powercontext-2026/src` 从比赛源码树运行
- MiniMax API Key：仅在线模式需要；`MINIMAX_API_KEY` 或 `MINIMAX_KEY_FILE`。不要提交 key

## HTTP 服务入口（本分支核心）

```bash
cd minimax
export SEEDB_PORT=2896   # 本机实验实例；评审机默认 2881 可省略
python3 -m pip install -r requirements.txt

# 启动服务（前台；数据库参数也可用 SEEDB_* 覆盖）
python -m context_service.server --host 127.0.0.1 --port 8765

# 另开终端：
curl -s http://127.0.0.1:8765/health | python -m json.tool

curl -s -X POST http://127.0.0.1:8765/context \
  -H 'Content-Type: application/json' \
  -d '{"task":"给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变"}' \
  | python -m json.tool

curl -sG http://127.0.0.1:8765/state \
  --data-urlencode 'task=给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变' \
  | python -m json.tool

curl -s http://127.0.0.1:8765/mcp/tool | python -m json.tool
```

端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务状态、seekdb 地址、文档数、检索算法 |
| GET | `/state?task=...` | 只返回 PowerContext 的 `state` 切片 |
| POST | `/context` | 请求体 `{"task": "...", "per_kind": 3}`，返回上下文契约 |
| GET | `/mcp/tool` | 返回 `docs/mcp-tool.json`（MCP 风格 tool 定义） |

上下文契约在旧版基础上增加 `retrieval` 字段：

```json
{
  "task": "...",
  "specs": [{"doc_id": "...", "source": "docs/spec-orders.md", "title": "...", "score": 1.23, "excerpt": "..."}],
  "code":  [{"source": "sample_project/app/orders.py", "...": "..."}],
  "tests": [{"source": "sample_project/tests/test_orders.py", "...": "..."}],
  "state": {"completed": [], "pending": [], "verified": [], "history": []},
  "retrieval": {"algorithm": "bm25", "k1": 1.5, "b": 0.75, "documents": 4, "spec_version": 2, "version_weight": 0.2}
}
```

## 检索算法：BM25 + 规格版本号

1. 服务启动后从 seekdb 读取全部 `engineering_docs`（spec/code/test）。
2. `context_service/bm25.py` 对 `title + body` 建内存索引，`docs_store.tokens` 负责英文词与中文 bigram 切分；标准 BM25（k1=1.5, b=0.75）。
3. spec 文档额外读取 `- 规格版本：v2` 这类声明，按 `1 + 0.2 * (version - 1)` 加权；code/test 不参与版本加权。
4. 每种 kind 取前 3 条，输出 `excerpt`（400 字符）；为空时回退到 `list_kind`。
5. 规格正文变更后必须重新 `init_seekdb.py`，否则索引里仍是旧正文——这保证上下文不是固定答案。

单元测试：

```bash
python -m pytest -q tests/test_service.py   # 4 passed
```

## 服务证据

```bash
python scripts/service_smoke.py   # 启动服务、逐个端点、断言 BM25/版本/来源
python scripts/service_e2e.py     # 服务提供上下文 → minimax_patch --context-url → 回放 → 测试
```

- `evidence/service/{health.json,context.json,state.json,mcp-tool.json,result.json,run.log,server.log}`
- `evidence/service/e2e/{patch/summary.json,patch/test_before.log,patch/test_after_offline.log,patch/diff.patch,patch/minimax_session.json,result.json,run.log}`
- e2e 的会话记录 `context_source=http`、`context_url=http://127.0.0.1:<port>/context`、`context.retrieval.algorithm=bm25`，证明上下文确实来自服务而不是本地直查。

## 一键复现（沿用离线回放）

```bash
python3 -m pip install -r requirements.txt
export SEEDB_PORT=2896      # 评审机默认 2881 可省略

bash run_demo.sh            # 默认离线，不联网、不需要 key
bash run_demo.sh --online   # 真实调用（需可用额度）
bash run_demo.sh --reset    # 恢复 before 快照 + 清空状态 + 重新导入
```

`run_demo.sh` 使用原有 CLI 取上下文；服务是同一批 seekdb 数据上的另一种入口。两者选出的材料都来自 `engineering_docs`，都可复现。

## 手动步骤

```bash
python scripts/init_seekdb.py                 # SEEDB_PORT=2896 / 默认 2881
python -m context_module.cli --task "..." --json
python scripts/minimax_patch.py --offline
python scripts/minimax_patch.py --selfcheck
python scripts/minimax_patch.py --offline --context-url http://127.0.0.1:8765/context --evidence-dir evidence/service/manual
python scripts/run_tests.py
```

## 离线回放与自检

- `--offline [session.json]`：不联网，回放 `evidence/minimax_session.json` 中 **2026-11-14 实时调用**的响应（该会话由 HTTP 服务提供上下文，`context_source=http`）；日志固定包含
  `offline replay of recorded MiniMax response from ...; no network call was made`，会话 `mode=offline_replay`，并校验 `target_sha256_before`。
- `--selfcheck`：在临时目录复制工程，还原 before 快照，断言 3 个目标测试失败、3 个回归通过，再回放断言 6 个全过；不需要 key/网络/seekdb。
- 2026-11-13 的历史实时会话归档在 `evidence/minimax_session.2026-11-13.json`（其 `prompt` 标记 `prompt_reconstructed: true`）；当前会话的 `prompt` 是实时调用原文，`raw_reply` 与 `diff.patch` 一一对应。
- `evidence/model_attempts.json` 逐条记录每次在线尝试（host、model、HTTP 状态、耗时）。

## 模型与端点回退

在线调用顺序：`MINIMAX_MODEL` → `--model` → `MiniMax-M3.1-Flash` → `MiniMax-M2.5`；端点：`MINIMAX_API_HOST` → `--endpoint` → `https://api.minimaxi.com` → `https://api.minimax.chat`。每组合有限重试（默认 2 次，退避 1s，超时 120s），每次尝试写入 `evidence/model_attempts.json`；全部失败也会落盘 `failed: true`。

## 失败模式

| 现象 | 含义 | 处理 |
| --- | --- | --- |
| `HTTP 429 ... 已达到 Token Plan 用量上限 (2056)` | 编程套餐额度用尽 | 等额度/升级；用 `--offline` 回放 |
| `HTTP 402 insufficient balance (1008)` | 按量 key 余额不足 | 换 key 或离线 |
| `HTTP 401 invalid api key (2049)` | key 与端点不匹配 | 检查 host 与 key 类型 |
| `seekdb is not reachable` | 数据库未启动/端口错 | 启动 seekdb；默认 2881，本机实验实例 `export SEEDB_PORT=2896` |
| `ModuleNotFoundError: powercontext` | 依赖没装或源码路径没加 | `pip install -r requirements.txt`，或 `export POWERCONTEXT_SRC=<repo>/src` |
| 服务端口占用 | 8765 已被占用 | `--port` 换端口 |
| 改前没有失败测试 | 工作区已是 after | `bash run_demo.sh --reset` |

## 验证样例

任务：给 `find_orders` 增加 `min_amount_cents` 下限筛选（边界包含、可与 `status` 组合）。

- 改前（`evidence/before_orders.py`）：3 个目标测试失败（`TypeError: unexpected keyword argument 'min_amount_cents'`），3 个回归通过。
- 改后（`evidence/after_orders.py`，来自真实 MiniMax 返回）：6 个测试全过。
- 本次实时调用：`MiniMax-M3.1-Flash`、`request_id=074715721a0fdb458a170fd4d08934b8`、耗时 4877 ms、`usage.total_tokens=1574`；diff：`evidence/diff.patch`；模型记录：`evidence/minimax_session.json`；尝试历史：`evidence/model_attempts.json`。
- 服务路径复现：`python scripts/service_e2e.py`，结果 `evidence/service/e2e/result.json`。

## 规格更新演示（验收项 3）

```bash
python scripts/spec_update_demo.py
```

修改 `docs/spec-orders.md` 的标题与 REQ-ORDER-3（marker `SPEC-UPDATE-DEMO-2026-11-14`）→ 重新导入 → 再取上下文，断言 excerpt 变化且含 marker；还原后断言回到基线。证据 `evidence/spec_update/`。

## 状态复用演示（验收项 5）

```bash
python scripts/state_reuse_demo.py
python scripts/state_read.py --expect-summary "给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变"
```

PowerContext 日志（`[powercontext] wrote state ...` / `[powercontext] read N state record(s) ...`）在 `evidence/state_reuse/run.log`；`run_demo.sh` 还会在改码后用新进程读回 `completed` 并断言。

## 已知限制

- 服务是无鉴权的只读接口，默认绑定 `127.0.0.1`，不要直接暴露到公网。
- seekdb 需自行启动：默认 2881，本机实验实例用 `SEEDB_PORT=2896`。未启动时 `run_demo.sh` 打印诊断并退出 2，服务端返回 500。
- 在线 MiniMax 依赖额度：额度不可用时用 `--offline` 回放已录制的同一份响应，日志与会话会标注，不冒充在线。
- BM25 是词法检索，不做语义改写；样例规模足够，大规模应加向量召回。
- 服务与 CLI 都要求规格更新后重新导入 seekdb，否则上下文仍是旧正文。

## 重置方法

```bash
bash run_demo.sh --reset
rm -f .sdd/state.db
cp evidence/before_orders.py sample_project/app/orders.py
```

## 验收材料

- `docs/ACCEPTANCE.md`：六项 × 实现 × 证据 × 复现命令（本分支含服务证据）。
- `docs/TECHNOTE.md`：模块结构、BM25/版本算法、MiniMax/seekdb/PowerContext 分工。
- `docs/mcp-tool.json`：MCP 风格 tool 定义。
- `evidence/` 随仓库提交（`git add -f minimax/evidence`）。
