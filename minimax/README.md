# MiniMax × OceanBase：SDD 工程上下文模块

面向"规格驱动开发（SDD）"的工程上下文模块：给一个开发任务，它从 **seekdb** 里检索适用的规格、代码和测试，叠加 **PowerContext** 里保存的任务进展，输出一份可直接交给 AI 开发助手的上下文；随后由 **MiniMax** 完成一次真实的代码修改，并用同一组测试验证改前失败、改后通过。

## 目录

```
minimax/
├── context_module/         # 上下文模块（CLI 入口）
│   ├── cli.py              # --task ... [--json] [--record-status ...]
│   ├── docs_store.py       # seekdb 工程资料存取与检索
│   ├── selector.py         # 规格/代码/测试选择 + 任务状态合并
│   └── state_store.py      # PowerContext 任务状态写入与重启复用
├── sample_project/         # 验证样例工程（改动前状态）
│   ├── app/orders.py
│   └── tests/test_orders.py
├── scripts/
│   ├── init_seekdb.py      # 把 docs/ 与样例代码导入 seekdb
│   ├── minimax_patch.py    # 完整流程：上下文 → MiniMax 改码 → 测试 → 证据
│   └── run_tests.py        # 跑样例测试
├── docs/                   # 规格与设计备忘（会被导入 seekdb）
└── evidence/               # 运行后生成的证据（改前/改后/差异/测试日志/模型记录）
```

## 依赖

- Python 3.12
- `pymysql`（连接 seekdb）
- `powercontext`：使用比赛仓库的产品源码（`PYTHONPATH=<repo>/src`）或 `pip install powercontext==1.2.0`
- seekdb：默认 `127.0.0.1:2881`，root 空密码
- MiniMax API Key：环境变量 `MINIMAX_API_KEY`，或写入 `~/.minimax_key`（不要提交）

## 运行步骤

```bash
cd minimax
export PYTHONPATH="$PWD:<path-to-powercontext>/src"
pip install pymysql

# 1) 初始化 seekdb（建库、导入规格/代码/测试）
python scripts/init_seekdb.py

# 2) 取一次任务上下文（可选 --json 看完整结构）
python -m context_module.cli \
  --task "给订单查询增加金额下限筛选（min_amount_cents），边界值要包含，保持 status 筛选与 total_amount 行为不变"

# 3) 记录任务状态（写入 PowerContext）
python -m context_module.cli --task "同上" --record-status pending --record-summary "初始任务"

# 4) 完整跑一遍：MiniMax 改码 + 前后测试 + 证据落盘
python scripts/minimax_patch.py
```

第 2、3 步是同一进程重跑也成立的：任务状态写在 `.sdd/state.db`，**模块重启后仍能读到**（这也是验收项"状态能够复用"）。

## 上下文输出契约

```json
{
  "task": "...",
  "specs": [{"doc_id": "...", "source": "docs/spec-orders.md", "title": "...", "score": 0.65, "excerpt": "..."}],
  "code":  [{"source": "sample_project/app/orders.py", "...": "..."}],
  "tests": [{"source": "sample_project/tests/test_orders.py", "...": "..."}],
  "state": {"completed": [], "pending": ["..."], "verified": [], "history": []}
}
```

- `source` 均为真实存在的文件路径，`excerpt` 直接截取自文件正文。
- 规格更新后重新执行 `init_seekdb.py`，输出会跟随新内容变化，不返回固定答案。

## 验证样例

任务：给 `find_orders` 增加 `min_amount_cents` 下限筛选（边界包含、可与 `status` 组合），且不改变既有行为。

- 改动前：`test_filters_by_minimum_amount`、`test_minimum_amount_is_inclusive_boundary`、
  `test_combines_status_and_minimum_amount` 三个目标测试 **失败**（`TypeError: unexpected keyword argument`），
  三个回归测试通过。
- 改动后（MiniMax 产出）：目标测试与回归测试**全部通过**。

`evidence/` 下会生成 `before_orders.py`、`after_orders.py`、`diff.patch`、
`test_before.log`、`test_after.log`、`context.json`、`minimax_session.json`。

## 已知限制

- seekdb 必须已启动（`systemctl start seekdb`）。
- MiniMax 账号需要有可用额度；没有额度时 `minimax_patch.py` 会在调用处报 `insufficient balance`，此时前两步与测试仍可独立运行。
