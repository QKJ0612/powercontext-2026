# 技术说明（一页）

## 模块结构

```text
任务文本 ──► context_module.cli
                 │
                 ├─ docs_store.DocStore ──► seekdb (sdd_context.engineering_docs)
                 │     search(): 分词 → LIKE 匹配 → 打分 → 取 specs/code/tests 各 3 条
                 │
                 ├─ selector.build_context() ──► 上下文契约
                 │     {task, specs[], code[], tests[], state{completed,pending,verified,history}}
                 │
                 └─ state_store ──► PowerContext → SQLite .sdd/state.db
                       record_task_state(): 写 kind=sdd-task
                       load_task_states():  读并按任务文本相关度合并进 state
```

- `context_module/docs_store.py`：负责 seekdb 连接、建表、导入与检索。表 `engineering_docs(doc_id, kind, path, title, body)`，`kind` ∈ `spec|code|test`。
- `context_module/selector.py`：把检索结果整理成固定契约；某类检索为空时用 `list_kind()` 兜底，保证输出结构稳定。
- `context_module/state_store.py`：PowerContext 的薄封装，scope 固定为 `sdd-context-module`，并在 stderr 打印写入/读取日志（验收项 5 的可观察证据）。
- `scripts/minimax_patch.py`：编排器。取上下文 → 调 MiniMax（或离线回放）→ 提取代码块 → 跑改前/改后测试 → 写 diff/日志/会话 → 记录任务状态。
- `scripts/init_seekdb.py`：把 `docs/spec-*.md`、`docs/design-*.md`、`sample_project/app/*.py`、`sample_project/tests/*.py` 导入 seekdb；连接参数统一从 `SEEDB_*` 环境变量读取（默认 2881，本机实验用 2896）。
- `run_demo.sh`：一键串起上述步骤，默认离线。

## 上下文选择算法

1. 对任务文本做小写化、按非字母数字切分，去停用词后得到 token 集合（`docs_store.tokens`）。
2. 对每条文档，按 token 在 `title/body` 中的出现次数累加得分，标题命中加权；`search()` 按得分降序返回。
3. 按 `kind` 各取前 3 条（`per_kind=3`），每条截取前 400 字符作为 `excerpt`。
4. 规格更新后，必须重新执行 `init_seekdb.py`（`replace_documents` 覆盖同 `doc_id`），下一次查询才会返回新正文——这保证输出不是固定答案（验收项 3）。
5. `state` 由 PowerContext 读取后按任务文本相关度排序，completed/pending/verified 分别列出，供模型了解"做到哪了"。

## 三项技术分工

| 技术 | 负责 | 不负责 |
| --- | --- | --- |
| **seekdb / OceanBase** | 工程资料（规格、代码、测试）的持久化与检索；`engineering_docs` 表是上下文的唯一事实来源 | 任务进度、模型调用 |
| **PowerContext** | 任务状态（pending/completed/verified/history）的写入与跨进程复用；scope `sdd-context-module` | 文档检索、代码生成 |
| **MiniMax** | 根据上下文产出代码修改（`MiniMax-M3.1-Flash`，OpenAI 兼容 `/v1/chat/completions`）；失败时按模型/端点链回退 | 不直接改数据库、不直接写状态 |

## 一次完整调用

```text
init_seekdb.py            # SEEDB_PORT=2896 SEEDB_DATABASE=sdd_context（评审机默认 2881/test）
  → 导入 4 个文档到 seekdb

cli --task "..."          # 同样读 SEEDB_*
  → DocStore.search() 查 seekdb
  → load_task_states() 查 PowerContext
  → 输出上下文 JSON（specs/code/tests/state）

minimax_patch.py [--offline]
  → 取上下文（同上）
  → 在线：模型/端点回退 + 有限重试；离线：回放 evidence/minimax_session.json
  → 写 app/orders.py
  → pytest 改前（before 快照）与改后
  → 写 evidence/{before,after,diff,test_*.log,session}.json
  → record_task_state(status=completed, summary=task)

state_read.py（新进程）
  → load_task_states() 读回 completed，断言任务 summary 在 context.state.completed
```

## 可复现性设计

- 所有外部输入都有落盘副本：`evidence/before_orders.py`（改前）、`evidence/minimax_session.json`（模型返回）、`evidence/context.json`（上下文）。
- `--offline` 校验 `target_sha256_before`，避免回放套错代码版本；日志与会话 `mode=offline_replay` 明确标注非实时调用。
- `--selfcheck` 在 `tempfile` 中复制工程，不依赖 seekdb/网络/API key，断言 3 failed → 6 passed。
- 时间、模型、HTTP 状态、退出码均写日志，便于评审复核（`evidence/model_attempts.json`）。

## 已知取舍

- 关键词/LIKE 检索适合小规模样例，语义泛化有限；服务化与 BM25/版本排序在 `eco-v3-service` 分支实现。
- 在线调用受 MiniMax 额度约束：本次用 `MiniMax-M3.1-Flash` 完成实时调用（`request_id=074715721a0fdb458a170fd4d08934b8`，4877 ms，1574 tokens）；额度不可用时以离线回放保证可复现，回放不会被标成在线调用。
