# 六项验收对照（eco-v2-resilient）

官方评分规则：六项必做项全过 = 20 分，任一不过 = 0 分。本文件逐项给出实现方式、证据文件与可复现命令。所有路径相对 `minimax/`。

## 总览

| # | 验收点 | 结论 | 关键证据 |
| --- | --- | --- | --- |
| 1 | 能够运行 | 通过 | `run_demo.sh`、`evidence/demo/run.log` |
| 2 | MiniMax 实际参与 | 通过（2026-11-14 实时在线调用，会话与 diff 一一对应） | `evidence/minimax_session.json`、`evidence/diff.patch`、`evidence/model_attempts.json` |
| 3 | 上下文正确 / 规格更新生效 | 通过 | `evidence/spec_update/` |
| 4 | 数据库实际参与 | 通过 | `evidence/context.json`、`scripts/init_seekdb.py`、`evidence/spec_update/run.log` |
| 5 | 状态能够复用 | 通过 | `evidence/state_reuse/`、`evidence/demo/state_after_restart.json` |
| 6 | 代码修改有效 | 通过 | `evidence/test_before.log`、`evidence/test_after.log`、`evidence/selfcheck.json` |

## 1. 能够运行

- 依赖、初始化、入口、测试命令、已知限制均写在 `README.md`；依赖用 `pip install -r requirements.txt` 安装（`powercontext==1.2.0`）。
- seekdb 连接统一走环境变量：`SEEDB_HOST/SEEDB_PORT/SEEDB_USER/SEEDB_PASSWORD/SEEDB_DATABASE`，默认 `127.0.0.1:2881`；本机实验实例用 `export SEEDB_PORT=2896 SEEDB_DATABASE=sdd_context`。
- 一键入口：`bash run_demo.sh`（默认离线回放，不需要 API key/外网）。脚本开头会检查依赖与 seekdb 连通性，缺失时打印可复制的安装/启动提示并以非 0 退出。
- 它会完成：恢复 before 快照 → `init_seekdb.py` 导入资料 → 状态写入/重启读取 → 输出上下文 → MiniMax 改码（离线回放）→ 改前/改后测试 → 记录状态 → 新进程读状态 → 断言计数。
- 证据：`evidence/demo/run.log`（每步命令、退出码、输出）、`evidence/demo/patch/{summary.json,test_before.log,test_after_offline.log,diff.patch,minimax_session.json}`。
- 复现：

  ```bash
  cd minimax
  export PYTHONPATH="$PWD"   # powercontext 由 pip 安装（见 requirements.txt）
  bash run_demo.sh
  # 末尾应输出 DEMO OK mode=offline_replay ... before_failed=3 after_passed=6
  ```

- 纯离线自检（连 seekdb 都不需要）：`python scripts/minimax_patch.py --selfcheck`，结果 `evidence/selfcheck.json`。

## 2. MiniMax 实际参与

- `evidence/minimax_session.json` 是 **2026-11-14 的实时在线调用**：`mode=online`、`request_model=MiniMax-M3.1-Flash`、`endpoint=https://api.minimaxi.com`、`request_id=074715721a0fdb458a170fd4d08934b8`、耗时 4877 ms、`usage.total_tokens=1574`（prompt 1050 / completion 524，其中 reasoning 291）、`raw_reply` 为模型原始返回（```python 代码块）。
- 同一文件保存了当次 prompt 原文（`prompt`，由当时的任务 + seekdb 上下文 + `before_orders.py` 组成）、改前代码 SHA（`target_sha256_before`）、模型返回代码 SHA（`patched_sha256`）、最终 diff 的 SHA 与路径（`final_diff_sha256`、`final_diff_path`）。
- `evidence/diff.patch` 正是该返回代码与 `evidence/before_orders.py` 的差异；`evidence/after_orders.py` 与 `patched_sha256` 一致，可三方对应。
- 2026-11-13 的历史实时会话归档为 `evidence/minimax_session.2026-11-13.json`（`usage.total_tokens=1759`，prompt 为确定性重建并标记 `prompt_reconstructed: true`），供离线回放与自检使用。
- 尝试历史：`evidence/model_attempts.json` 逐条记录每次尝试的 host、model、HTTP 状态与耗时；本次实时调用是其中成功的一条（HTTP 200）。
- 复现核对：

  ```bash
  python scripts/minimax_patch.py --offline
  # 日志含 "offline replay of recorded MiniMax response ...; no network call was made"
  # 会话 mode=offline_replay，且校验 target_sha256_before
  python -m json.tool evidence/model_attempts.json | head -40
  ```

## 3. 上下文正确 + 规格更新生效

- 上下文契约：`{task, specs[], code[], tests[], state{completed,pending,verified,history}}`，`source` 是真实文件路径，`excerpt` 截取自正文（`context_module/selector.py`）。
- 检索来源：`docs/spec-orders.md`、`docs/design-notes.md`、`sample_project/app/orders.py`、`sample_project/tests/test_orders.py`，由 `scripts/init_seekdb.py` 导入 seekdb。
- 规格更新演示：`scripts/spec_update_demo.py` 修改 `docs/spec-orders.md` 标题与 REQ-ORDER-3 边界条目（marker `SPEC-UPDATE-DEMO-2026-11-14`）→ 重新导入 → 再次取上下文，断言 excerpt 变化且包含 marker；最后还原原文并重新导入，断言 excerpt 回到基线。
- 证据：`evidence/spec_update/{context_before.json,context_after.json,context_restored.json,excerpt_diff.txt,spec_change.patch,result.json,run.log}`。
- 复现：

  ```bash
  python scripts/spec_update_demo.py    # 端口取自 SEEDB_PORT
  cat evidence/spec_update/excerpt_diff.txt
  ```

## 4. 数据库实际参与

- seekdb 保存全部工程资料与索引：`scripts/init_seekdb.py` 建库（默认 `test`，可用 `SEEDB_DATABASE` 覆盖；本机证据用 `sdd_context`）、表 `engineering_docs`，导入 4 个文档（2 规格 + 1 代码 + 1 测试），输出 `imported 4 documents into seekdb 127.0.0.1:2896/sdd_context`。
- 上下文中的 specs/code/tests 均来自 `DocStore.search()` 对 seekdb 的实时查询（`context_module/docs_store.py`），不是本地写死。
- 规格更新后必须重新导入才变化，反向证明查询真的走数据库：更新前的 `context_before.json` 与更新后的 `context_after.json` 的 excerpt 不同（`evidence/spec_update/excerpt_diff.txt`）。
- 证据：`evidence/context.json`、`evidence/demo/init_seekdb.log`、`evidence/spec_update/run.log`、`evidence/demo/context_cli.json`。

## 5. 状态能够复用

- 存储：PowerContext（比赛仓库 `powercontext` 包）写入 SQLite `.sdd/state.db`，scope `sdd-context-module`，kind 为 `sdd-task`。
- 演示 A（纯状态）：`scripts/state_reuse_demo.py` 从空库开始，进程 1 写 pending → 进程 2 新进程读取 → 进程 3 写 completed → 进程 4 新进程读取；日志含 `[powercontext] wrote state ...` 与 `[powercontext] read N state record(s) ...`。
- 演示 B（真实任务）：`run_demo.sh` 在 MiniMax 改码后由 `minimax_patch.py` 写入 completed，再由**新进程** `scripts/state_read.py` 读取，断言任务 summary 出现在 `context.state.completed`。
- 证据：`evidence/state_reuse/{run.log,result.json,read_after_restart.json,read_after_second_restart.json}`、`evidence/demo/state_after_restart.json`、`evidence/demo/context_after_restart.json`。
- 复现：

  ```bash
  python scripts/state_reuse_demo.py    # 端口取自 SEEDB_PORT
  grep powercontext evidence/state_reuse/run.log
  ```

## 6. 代码修改有效

- 改前：`evidence/before_orders.py`（`find_orders` 无 `min_amount_cents` 参数），只跑目标测试文件 → **3 failed, 3 passed**；失败原因是 `TypeError: find_orders() got an unexpected keyword argument 'min_amount_cents'`，即目标行为未实现，不是环境错误。日志 `evidence/test_before.log`、`evidence/test_before_offline.log`。
- 改后：`evidence/after_orders.py`（真实 MiniMax 返回），同一组测试 → **6 passed**。日志 `evidence/test_after.log`、`evidence/test_after_offline.log`。
- 回归：原 3 个状态筛选/合计测试在改前改后均通过，且改后仍全过。
- `--selfcheck` 在临时目录重复上述断言（不联网、不需要 key、不改工作区），`evidence/selfcheck.json` 中 `ok=true`；它回放的是当前 `evidence/minimax_session.json` 的录制内容（2026-11-14 实时调用），与在线路径共用同一份会话但不会联网。
- 复现：

  ```bash
  python scripts/minimax_patch.py    # 端口取自 SEEDB_PORT --selfcheck
  python -m pytest -q sample_project/tests
  ```

## 提交材料清单

| 要求 | 位置 |
| --- | --- |
| 完整源码 | `context_module/`、`scripts/`、`sample_project/`、`run_demo.sh` |
| 运行说明 | `README.md`（依赖版本、配置、初始化、入口、测试、已知限制、重置） |
| 验证样例 | `README.md`「验证样例」；`docs/spec-orders.md`；`sample_project/`；`evidence/before_orders.py` |
| 代码与测试 | `evidence/{before_orders.py,after_orders.py,diff.patch,test_before.log,test_after.log}`；新增功能测试在 `sample_project/tests/test_orders.py` |
| 运行日志 | `evidence/demo/run.log`、`evidence/demo/patch/run.log`、`evidence/*/run.log`（含命令、退出码、代码版本 SHA） |
| 技术使用记录 | `evidence/minimax_session.json`（MiniMax）、`evidence/context.json`（数据库）、`evidence/state_reuse/`（PowerContext） |
| 一页技术说明 | `docs/TECHNOTE.md` |
| 六项对照 | 本文件 |

## 在线调用与回退

`evidence/minimax_session.json` 记录的是 2026-11-14 的一次**实时在线调用**：`MiniMax-M3.1-Flash` 返回 HTTP 200，`request_id=074715721a0fdb458a170fd4d08934b8`，耗时 4877 ms，`usage.total_tokens=1574`，同一份返回经 `evidence/diff.patch` 应用到 `sample_project/app/orders.py`，测试结果 3 failed → 6 passed。

额度不可用时的回退路径是 `--offline`：它回放**已录制的同一份真实响应**，日志与 `minimax_session.json` 会标注为 `offline replay`，不会伪装成实时调用；`--selfcheck` 则完全不联网、不需要 key，只验证"改前失败 → 改后通过"这条链路。key 本身不写入仓库、日志或本文档。
