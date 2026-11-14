# 六项验收对照（eco-v3-service）

官方规则：六项必做项全过 = 20 分，任一不过 = 0 分。本文件逐项给出实现、证据与复现命令，路径相对 `minimax/`。

## 总览

| # | 验收点 | 结论 | 关键证据 |
| --- | --- | --- | --- |
| 1 | 能够运行 | 通过（`pip install -r requirements.txt` + `SEEDB_*` 环境变量） | `run_demo.sh`、`evidence/demo/run.log` |
| 2 | MiniMax 实际参与 | 通过（2026-11-14 独立实时调用） | `evidence/minimax_session.json`、`evidence/diff.patch`、`evidence/model_attempts.json` |
| 3 | 上下文正确 / 规格更新生效 | 通过（BM25 + 版本号；规格改动可观察） | `evidence/service/context.json`、`evidence/spec_update/` |
| 4 | 数据库实际参与 | 通过（seekdb 是唯一资料来源） | `evidence/context.json`、`evidence/service/health.json` |
| 5 | 状态能够复用 | 通过（清空→写→重启→读） | `evidence/state_reuse/`、`evidence/demo/state_after_restart.json` |
| 6 | 代码修改有效 | 通过（3 failed → 6 passed） | `evidence/test_before.log`、`evidence/test_after.log`、`evidence/selfcheck.json` |

## 1. 能够运行

- 依赖：`pip install -r requirements.txt`（`powercontext==1.2.0`、`pymysql`、`pytest`）；不依赖本机 `/home/zhuxuanyu/powercontext-2026`，源码路径仅作 `POWERCONTEXT_SRC` 备选。
- seekdb：`SEEDB_HOST/SEEDB_PORT/SEEDB_USER/SEEDB_PASSWORD/SEEDB_DATABASE`，默认 `127.0.0.1:2881`、`root`、空密码、`test`；本机实验实例 `export SEEDB_PORT=2881 SEEDB_DATABASE=test`。
- `run_demo.sh` 开头检查 `powercontext/pymysql/pytest` 是否可导入，缺失时打印 `pip install -r requirements.txt` 并以退出码 2 结束；连不上 seekdb 时打印尝试的 host/port 与常见原因。
- 证据：`evidence/demo/run.log`（每步命令、退出码）、`evidence/demo/patch/*`。
- 复现：

  ```bash
  cd minimax
  python3 -m pip install -r requirements.txt
  export SEEDB_PORT=2881 SEEDB_DATABASE=test   # 评审机默认 2881/test 可省略
  bash run_demo.sh
  # DEMO OK mode=offline_replay model=MiniMax-M3.1-Flash before_failed=3 after_passed=6
  ```

## 2. MiniMax 实际参与

- `evidence/minimax_session.json` 是 **2026-11-14 的实时调用**：`mode=online`、`MiniMax-M3.1-Flash`、`request_id=074715721a0fdb458a170fd4d08934b8`、耗时 4877 ms、`usage.total_tokens=1574`（prompt 1050 / completion 524，其中 reasoning 291）。
- 会话保存当次 `prompt`（由 `POST /context` 的 BM25 上下文 + 改前代码组成）、`raw_reply`（模型原始代码块）、`target_sha256_before`、`patched_sha256`、`final_diff_sha256`，与 `evidence/diff.patch`、`evidence/after_orders.py` 三方对应。
- 2026-11-13 的历史会话归档为 `evidence/minimax_session.2026-11-13.json`；额度耗尽期的 4 次真实 429 与本次成功都保留在 `evidence/model_attempts.json`（`history_note` 说明"429 → 恢复"）。
- 复现：

  ```bash
  python -m json.tool evidence/minimax_session.json | head -30
  python -m json.tool evidence/model_attempts.json | head -40
  ```

## 3. 上下文正确 + 规格更新生效

- 服务 `POST /context` 返回 `{task, specs[], code[], tests[], state, retrieval}`；`source` 是真实文件路径，spec/code/test 均来自 seekdb `engineering_docs`。
- 选择算法：`context_service/bm25.py` 对 `title+body` 建 BM25 索引（k1=1.5,b=0.75），spec 按 `- 规格版本：v2` 做 `1+0.2*(v-1)` 加权；输出 `retrieval.algorithm=bm25`、`spec_version=2`、`documents=4`。
- 证据：`evidence/service/context.json`（含 `retrieval`）、`tests/test_service.py`（4 passed）、`evidence/service/run.log`。
- 规格更新演示：`scripts/spec_update_demo.py` 改 `docs/spec-orders.md` 标题与 REQ-ORDER-3（marker `SPEC-UPDATE-DEMO-2026-11-14`）→ 重新导入 → 上下文 excerpt 变化且含 marker → 还原后回到基线。证据 `evidence/spec_update/`。
- 复现：

  ```bash
  python -m pytest -q tests/test_service.py
  python scripts/spec_update_demo.py
  cat evidence/spec_update/excerpt_diff.txt
  ```

## 4. 数据库实际参与

- `scripts/init_seekdb.py` 建库（默认 `test`，可 `SEEDB_DATABASE` 覆盖）与表 `engineering_docs`，只导入 `docs/spec-*.md`、`docs/design-*.md`、`sample_project/app/*.py`、`sample_project/tests/*.py`，共 4 个文档，输出 `imported 4 documents into seekdb ...`。
- 服务每次请求都 `list_kind()` 读 seekdb 并现场建 BM25 索引；规格改动不重新导入时 excerpt 不变，重新导入后变化，反向证明查询真的走数据库。
- 证据：`evidence/service/health.json`（db 地址、documents=4）、`evidence/context.json`、`evidence/spec_update/run.log`、`evidence/demo/init_seekdb.log`。

## 5. 状态能够复用

- PowerContext（`powercontext==1.2.0`）写 SQLite `.sdd/state.db`，scope `sdd-context-module`，kind `sdd-task`；`state_store.py` 打印 `[powercontext] wrote state ...` / `[powercontext] read N state record(s) ...`。
- 演示 A：`scripts/state_reuse_demo.py` 空库 → 进程 1 写 pending → 进程 2 读 → 进程 3 写 completed → 进程 4 读；证据 `evidence/state_reuse/`。
- 演示 B：`run_demo.sh` 在改码后写 completed，再由新进程 `scripts/state_read.py` 读取并断言 summary 出现在 `context.state.completed`；证据 `evidence/demo/state_after_restart.json`、`context_after_restart.json`。
- 复现：

  ```bash
  python scripts/state_reuse_demo.py
  grep powercontext evidence/state_reuse/run.log
  ```

## 6. 代码修改有效

- 改前：`evidence/before_orders.py`（无 `min_amount_cents`），只跑 `tests/test_orders.py` → **3 failed, 3 passed**，失败为 `TypeError: unexpected keyword argument 'min_amount_cents'`（目标行为未实现，非环境错误）。
- 改后：`evidence/after_orders.py`（本次实时调用返回）→ **6 passed**；原 3 个状态/合计回归测试也全过。
- `--selfcheck` 在临时目录重复 3 failed → 6 passed，不需要 key/网络/seekdb；`--offline` 回放同一会话并标注 `offline_replay`。
- 复现：

  ```bash
  python scripts/minimax_patch.py --selfcheck
  python -m pytest -q sample_project/tests
  ```

## 提交材料

| 要求 | 位置 |
| --- | --- |
| 完整源码 | `context_module/`、`context_service/`、`scripts/`、`tests/`、`run_demo.sh` |
| 运行说明 | `README.md`（依赖、`SEEDB_*`、初始化、服务/CLI 入口、测试、限制、重置） |
| 验证样例 | `README.md`「验证样例」、`docs/spec-orders.md`、`sample_project/`、`evidence/before_orders.py` |
| 代码与测试 | `evidence/{before_orders.py,after_orders.py,diff.patch,test_before.log,test_after.log}`、`sample_project/tests/test_orders.py`、`tests/test_service.py` |
| 运行日志 | `evidence/demo/run.log`、`evidence/service/run.log`、`evidence/*/run.log`（含命令/退出码/版本） |
| 技术记录 | `evidence/minimax_session.json`（MiniMax）、`evidence/service/context.json`（数据库）、`evidence/state_reuse/`（PowerContext） |
| 一页说明 | `docs/TECHNOTE.md` |
| MCP 定义 | `docs/mcp-tool.json` |

## 429 → 恢复说明

`evidence/minimax_session.json` 记录的是 2026-11-14 的一次实时在线调用：`MiniMax-M3.1-Flash` 返回 HTTP 200、`request_id=074715721a0fdb458a170fd4d08934b8`、耗时 4877 ms、`usage.total_tokens=1574`；额度不可用时用 `--offline` 回放同一份已录制响应，日志与会话标注为 `offline replay`，不冒充实时调用；key 不写入仓库/日志/文档。
