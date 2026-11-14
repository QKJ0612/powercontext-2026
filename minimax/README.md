# MiniMax × OceanBase：SDD 工程上下文模块（eco-v2-resilient）

面向"规格驱动开发（SDD）"的工程上下文模块：给一个开发任务，它从 **seekdb** 里检索适用的规格、代码和测试，叠加 **PowerContext** 里保存的任务进展，输出一份可直接交给 AI 开发助手的上下文；随后由 **MiniMax** 完成一次真实的代码修改，并用同一组测试验证改前失败、改后通过。

本分支的重点是**鲁棒性与离线可复现**：没有 MiniMax 额度、没有外网、甚至换了机器，也能用 `--offline` 回放已录制的真实模型响应，或用 `--selfcheck` 在临时目录里完整验证"改前 3 个目标测试失败 → 改后 6 个测试全过"。回放日志会明确标注 `offline replay of recorded MiniMax response`，不会伪装成实时调用。

## 目录

```
minimax/
├── context_module/         # 上下文模块（CLI 入口）
│   ├── cli.py              # --task ... [--json] [--record-status ...]
│   ├── docs_store.py       # seekdb 工程资料存取与检索
│   ├── selector.py         # 规格/代码/测试选择 + 任务状态合并
│   └── state_store.py      # PowerContext 任务状态写入与重启复用
├── sample_project/         # 验证样例工程
│   ├── app/orders.py
│   └── tests/test_orders.py
├── scripts/
│   ├── init_seekdb.py      # 把 docs/ 与样例代码导入 seekdb
│   ├── minimax_patch.py    # 上下文 → MiniMax 改码（或离线回放）→ 测试 → 证据
│   ├── run_tests.py        # 跑样例测试
│   ├── state_reuse_demo.py # 清空状态 → 写 → 重启读 → 再写 → 再读
│   ├── state_read.py       # 新进程读取状态并断言
│   └── spec_update_demo.py # 改规格 → 重新导入 → 上下文变化 → 还原
├── run_demo.sh             # 一键复现（默认离线）
├── docs/                   # 规格、设计备忘、验收对照、技术说明
└── evidence/               # 运行证据（改前/改后/diff/测试日志/模型记录）
```

## 依赖

- Python 3.11+（本机 3.14.4）
- `pip install -r requirements.txt`：固定 `powercontext==1.2.0`、`pymysql`、`pytest`
- seekdb：**默认 `127.0.0.1:2881`**（官方默认端口），root 空密码。本机实验实例在 **2896**，先 `export SEEDB_PORT=2896`
- 统一环境变量（所有脚本、CLI、`run_demo.sh` 都读取同一组）：`SEEDB_HOST`（默认 127.0.0.1）、`SEEDB_PORT`（默认 2881）、`SEEDB_USER`（默认 root）、`SEEDB_PASSWORD`（默认空）、`SEEDB_DATABASE`（默认 test）；旧拼写 `SEEKDB_*` 仍兼容
- 备选：从比赛仓库源码运行 powercontext，`export POWERCONTEXT_SRC=/path/to/powercontext-2026/src`（`run_demo.sh` 会加入 `PYTHONPATH`）；直接跑 Python 时也可 `export PYTHONPATH="$PWD:$POWERCONTEXT_SRC"`
- MiniMax API Key：仅在线模式需要。环境变量 `MINIMAX_API_KEY`，或 `MINIMAX_KEY_FILE=/path/to/key`（默认 `~/.minimax_key`）。**不要提交 key**

## 一键复现

```bash
cd minimax
python3 -m pip install -r requirements.txt
export SEEDB_PORT=2896   # 本机实验实例；评审机用默认 2881 时可省略

# 默认：离线回放，不需要 API key、不联网
bash run_demo.sh

# 在线：真实调用 MiniMax（需要可用额度的 key）
export MINIMAX_API_KEY=你的 key        # 或 export MINIMAX_KEY_FILE=/path/to/key-file
bash run_demo.sh --online

# 重置到验证样例的初始状态（恢复 before 快照 + 清空 .sdd + 重新导入资料）
bash run_demo.sh --reset
```

`run_demo.sh` 会依次：恢复 before 快照 → 初始化 seekdb → **清空状态后写入并重启读取 PowerContext 状态** → 输出任务上下文 → 离线回放（或在线调用）MiniMax 改码 → 跑改前/改后测试 → 记录任务状态 → **新进程再读状态** → 校验"改前有失败、改后全过"。每一步的日志和 JSON 都在 `evidence/demo/`。

预期末尾输出：

```text
DEMO OK mode=offline_replay model=MiniMax-M3.1-Flash before_failed=3 after_passed=6
```

## 手动步骤

```bash
cd minimax
export SEEDB_PORT=2896   # 本机实验实例；评审机默认 2881 可省略

# 1) 初始化 seekdb（建库、导入规格/代码/测试）
python scripts/init_seekdb.py

# 2) 取一次任务上下文（--json 看完整结构）
python -m context_module.cli --port 2896 \
  --task "给订单查询增加金额下限筛选（含边界），保持既有状态筛选与合计行为不变" --json

# 3) 完整流程：上下文 → MiniMax（或离线回放）→ 前后测试 → 证据 → 状态
python scripts/minimax_patch.py --port 2896 --offline          # 回放
python scripts/minimax_patch.py --port 2896                    # 在线（需 key/额度）
python scripts/minimax_patch.py --port 2896 --selfcheck        # 纯离线自检，不需要 key

# 4) 跑测试（任意时刻）
python scripts/run_tests.py
```

## 离线回放与自检

- `--offline [session.json]`：不发起任何网络请求，读取 `evidence/minimax_session.json` 中记录的真实模型响应，提取代码块后写入 `sample_project/app/orders.py`。运行日志固定包含
  `offline replay of recorded MiniMax response from ...; no network call was made`，会话 JSON 的 `mode` 为 `offline_replay`，并校验 `target_sha256_before`，防止把回放套到错误的代码版本上。
- `--selfcheck`：在 `tempfile` 临时目录复制样例工程，把 `app/orders.py` 还原为 `evidence/before_orders.py`，只跑 `tests/test_orders.py`；断言恰好 3 个目标测试失败、3 个回归测试通过，再离线回放并断言 6 个测试全过。**不需要 API key、不联网、不改工作区**，结果写入 `evidence/selfcheck.json`。

`evidence/minimax_session.json` 现在是 **2026-11-14 的实时调用**：`MiniMax-M3.1-Flash`、`request_id=074715721a0fdb458a170fd4d08934b8`、耗时 4877 ms、`usage.total_tokens=1574`（prompt 1050 / completion 524，其中 reasoning 291），会话保存 prompt 原文、模型返回代码块、改前/改后 SHA 与最终 diff，可三方对应。2026-11-13 的历史会话归档在 `evidence/minimax_session.2026-11-13.json`（该次 `prompt` 为确定性重建，标记 `prompt_reconstructed: true`）；每次在线尝试都记录在 `model_attempts.json`。

## 模型与端点回退

在线调用按以下顺序逐个尝试，每次尝试（host、model、HTTP 状态、耗时、错误体）都写入 `evidence/model_attempts.json`：

1. 模型：`MINIMAX_MODEL`（若设置）→ `--model`（可重复）→ `MiniMax-M3.1-Flash` → `MiniMax-M2.5`
2. 端点：`MINIMAX_API_HOST`（若设置）→ `--endpoint`（可重复）→ `https://api.minimaxi.com` → `https://api.minimax.chat`
3. 每个组合内做有限重试：默认 `--retries 2`、指数退避 `--backoff 1.0` 秒、单请求超时 `--timeout 120` 秒；仅对 408/409/429/5xx 和网络错误重试。

全部组合失败时以非 0 退出，但 `model_attempts.json` 仍会落盘（含 `failed: true`、`winner: null`），方便评审看到失败原因。

## 失败模式

| 现象 | 含义 | 处理 |
| --- | --- | --- |
| `HTTP 429 ... 已达到 Token Plan 用量上限 (2056)` | MiniMax 套餐额度用尽 | 等额度恢复/升级套餐；或直接用 `--offline` 回放已录制会话 |
| `HTTP 402 insufficient balance (1008)` | 普通按量 key 余额不足 | 换有余额的 key；或离线回放 |
| `HTTP 401 invalid api key (2049)` | key 与端点不匹配 | 检查 `MINIMAX_API_HOST` 与 key 类型 |
| `seekdb is not reachable` | 数据库未启动/端口不对 | 启动 seekdb；默认 2881，本机实验实例用 `export SEEDB_PORT=2896` |
| `ModuleNotFoundError: powercontext` | 依赖没装或源码路径没加 | `pip install -r requirements.txt`，或 `export POWERCONTEXT_SRC=<repo>/src` |
| `ModuleNotFoundError: pymysql` | 依赖没装 | `pip install -r requirements.txt` |
| 改前没有失败测试 | 工作区已经是 after 版本 | `bash run_demo.sh --reset` 或从 `evidence/before_orders.py` 恢复 |

## 验证样例

任务：给 `find_orders` 增加 `min_amount_cents` 下限筛选（边界包含、可与 `status` 组合），且不改变既有行为。

- 改动前（`evidence/before_orders.py`）：`test_filters_by_minimum_amount`、`test_minimum_amount_is_inclusive_boundary`、`test_combines_status_and_minimum_amount` 三个目标测试 **失败**（`TypeError: unexpected keyword argument 'min_amount_cents'`），三个回归测试通过。见 `evidence/test_before.log`。
- 改动后（`evidence/after_orders.py`，来自真实 MiniMax 返回）：6 个测试 **全部通过**。见 `evidence/test_after.log`。
- 差异：`evidence/diff.patch`；模型记录：`evidence/minimax_session.json`（2026-11-14 实时调用，`request_id=074715721a0fdb458a170fd4d08934b8`，耗时 4877 ms）；尝试历史：`evidence/model_attempts.json`。
- 一键复现：`bash run_demo.sh`（离线）或 `bash run_demo.sh --online`（在线）。

## 规格更新演示（验收项 3）

```bash
python scripts/spec_update_demo.py --port 2896
```

脚本改 `docs/spec-orders.md` 的标题与 REQ-ORDER-3 边界条目（加 marker `SPEC-UPDATE-DEMO-2026-11-14`）→ 重新 `init_seekdb.py` → 再取上下文，断言 spec excerpt 发生变化且包含 marker；最后还原原文并重新导入，断言 excerpt 回到基线。证据在 `evidence/spec_update/`（`context_before.json`、`context_after.json`、`excerpt_diff.txt`、`run.log`、`result.json`）。

## 状态复用演示（验收项 5）

```bash
python scripts/state_reuse_demo.py --port 2896
```

从空 `.sdd/state.db` 开始：进程 1 写入 `pending` → 进程 2（新进程）读取 → 进程 3 写入 `completed` → 进程 4（新进程）读取。日志里能看到 PowerContext 的 `[powercontext] wrote state ...` / `[powercontext] read N state record(s) ...`。证据在 `evidence/state_reuse/`。`run_demo.sh` 还会在 MiniMax 改码后，用**另一个新进程** `state_read.py` 读回本次任务状态并断言其出现在 `context.state.completed`。

## 已知限制

- seekdb 需自行启动：默认 `127.0.0.1:2881`，本机实验实例是 2896（`export SEEDB_PORT=2896`）。`run_demo.sh` 连不上时会打印尝试的 host/port、常见原因和启动参考命令，并以退出码 2 结束。
- 在线模式需要可用的 MiniMax 额度。额度不可用时用 `--offline` 回放已录制的真实会话，日志/会话会标注 `offline replay`，不会冒充在线调用。
- `evidence/minimax_session.2026-11-13.json` 的 `prompt` 是确定性重建（`prompt_reconstructed: true`）；当前 `minimax_session.json` 的 prompt 是实时调用原文。
- 上下文选择目前是关键词/LIKE 打分（`docs_store.search`），不是语义检索；样例规模下足够，规模大时应换成 BM25/向量检索（`eco-v3-service` 分支演示了 BM25 + 规格版本号排序）。

## 重置方法

```bash
# 恢复样例到"改前"，清空任务状态，重新导入资料
bash run_demo.sh --reset

# 只清任务状态
rm -f .sdd/state.db

# 只恢复样例代码
cp evidence/before_orders.py sample_project/app/orders.py
```

## 验收材料

- `docs/ACCEPTANCE.md`：六项验收要求 × 实现方式 × 证据文件 × 复现命令。
- `docs/TECHNOTE.md`：一页技术说明（模块结构、上下文选择算法、MiniMax/seekdb/PowerContext 分工）。
- `evidence/` 已随仓库提交（`.gitignore` 默认忽略，使用 `git add -f minimax/evidence`）。
