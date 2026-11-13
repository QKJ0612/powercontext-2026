# VALIDATION.md · 端到端验证记录

本文所有返回字段均为**实测值**，取自 2026-11-13 的一次完整跑通，不是预期输出。
未实测的内容一律显式标注，不当作实测呈现。

## 环境

| 组件 | 值 |
|---|---|
| PowerContext | **1.5.1**（`GET http://127.0.0.1:8000/openapi.json` → `info.version`），监听 **127.0.0.1:8000** |
| 组会搭子后端 | FastAPI，监听 **127.0.0.1:8080**（8000 已被 PowerContext 占用，不可用同一端口） |
| 模型 | 智谱 **GLM-4-Flash**，OpenAI 兼容接口 `https://open.bigmodel.cn/api/paas/v4` |
| Python | 3.12.10，依赖 fastapi / uvicorn / httpx |
| 主验证账本 | `IDEMPOTENCY_KEY=gm-validation-20261113`<br>`SCOPE_TITLE=组会搭子-验证`，`scope_id=scp_1e4vmndy877wtw7w9vg0c7820k` |
| 空账本探针账本 | `SCOPE_TITLE=组会搭子-空账本验证`，`scope_id=scp_1v6myyj7dey0remqsk83m7k4kv` |

`scope_id` 是实例相关的，换台机器或重建库就会变，**不要把它当固定值**；靠 `idempotency_key` 拿回即可。

---

## 第 0 步：初始状态——空账本

用一个新的 `idempotency_key` 起服务，PowerContext 侧此时该 scope 下没有任何记忆。

scope 由后端启动时自动创建：`POST /v1/scopes`，实测成功状态码是 **201**（不是 200），返回体是完整的
`ScopeDescriptor`（含 `scope_id` / `title` / `summary` / `version`），不只有 `scope_id`。

实测空账本返回：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/atomic-memory/list \
  -H "Content-Type: application/json" \
  -d '{"scope_id":"scp_1v6myyj7dey0remqsk83m7k4kv","states":["active"],"limit":100}'
```

```json
{"items": [], "next_cursor": null}
```

HTTP **200**。

> **这一条是在独立探针 scope 上抓的**（`组会搭子-空账本验证`，`scp_1v6myyj7dey0remqsk83m7k4kv`），
> 因为主验证 scope `scp_1e4vmndy877wtw7w9vg0c7820k` 跑完第 1-4 步后已有 4 条记忆，不再是空状态，
> 在它上面无法复现初始返回。两者是同一套代码、同一份空账本语义，只是分别取自跑之前和跑之后。

同时 `/api/pending` 在空账本上返回 `{"items":[],"checked":0}`。

> 这一步同时是"**恢复初始状态**"的方法：PowerContext 不提供删除单个 scope 的入口，本应用也不做删除。
> 想要一个干净账本，**换一个 `IDEMPOTENCY_KEY` 重启服务**即可，旧账本原样留在库里不受影响。上面的探针
> scope 就是这么建出来的。本次验证前那份日常账本（`gm-groupmeeting-buddy`）里还留着分类器修复前写错
> 类型的两条，也是靠这个办法隔离开的。

---

## 第 1 步：记三句话（`POST /api/log`）

依次输入三句，全部用「自动判断」：

| 输入 | 实测 `kind` | 实测 `why` |
|---|---|---|
| `今天把检索重排接完了，recall 掉了 2 个点` | **progress** | 完成类词 `['接完']` |
| `下周三前把 baseline 跑完并整理成表` | **decision** | 承诺类词 |
| `评测机器不够，跑全量要 6 小时` | **blocker** | 卡点类词 `['不够']` |

每条返回的关键字段：`kind` / `why` / `text` / `date` / `memory_id` / `deduplicated`。
本次三条 `deduplicated` 均为 `false`（`changed=true`，各自新增了一条记忆）。

第一句是分类器的回归重点：修复前它因为命中单字「掉」被判成 **blocker**，现在按「完成类词」判为 **progress**。

存储侧实际写入的文本带日期前缀（页面上看不到，见第 4 步的 PowerContext 原始返回）：

```
[2026-11-13] 今天把检索重排接完了，recall 掉了 2 个点
```

---

## 第 2 步：生成汇报并核对承诺（`GET /api/pending` → `POST /api/report`）

点「生成组会汇报」，前端先拉待核对承诺，实测返回：

```json
{"items": [{"memory_id": "art_486b0a2be55a4bb2b0dfac696e3c6ef0",
            "kind": "decision",
            "text": "下周三前把 baseline 跑完并整理成表",
            "date": "2026-11-13"}],
 "checked": 0}
```

选「部分完成」后提交，实测 `/api/report` 的字段：

| 字段 | 实测值 | 含义 |
|---|---|---|
| `model_status` | **`"model"`** | 走的是真模型，不是模板降级 |
| `reason` | **`null`** | 没有降级原因 = 模型调用成功 |
| `written_back` | `["承诺「下周三前把 baseline 跑完并整理」→ 部分完成"]` | 核对结果已作为新记忆入库 |
| `unknown_memory_ids` | `[]` | 核对项全部对上了记忆 |

实测 `draft` 正文原样如下：

```markdown
## 本周进展
- 今天把检索重排接完了，recall 掉了 2 个点

## 上次承诺完成情况
- 「下周三前把 baseline 跑完并整理成表」→ 部分完成

## 卡点
- 评测机器不够，跑全量要 6 小时

## 下周计划
- 计划完成 baseline 跑全量实验
- 计划优化评测机器资源
- 计划整理 baseline 实验结果
- 计划讨论实验结果
```

这份草稿能读出的四条判定：

1. **四段标题齐全且一字不改** —— 与代码里的 `REPORT_SECTIONS` 完全一致。
2. **第二段严格等于核对结果** —— 状态是「部分完成」，与 `written_back` 那条一致，不是模型猜的。
3. **内容零编造** —— 前三段的每一行都能在 scope 里找到对应记忆原文；草稿里没有出现任何未记录的数字、实验名或结论。
4. **日期前缀已剥除** —— 正文各行都不含 `[2026-11-13]`，与第 4 步的存储原文对比即见差异。
5. **第四段确为推导** —— 这四条在记忆里不存在任何对应条目，是模型按 prompt 约束（以「计划」开头、最多 4 条）
   从前面的卡点和未完成承诺生成的，实际正好 4 条。

---

## 第 3 步：翻旧账（`POST /api/recall`）

闭环建立后再问（此时库里已有那条核对记录）：

```
上次说好要做的做完了吗
```

实测返回：

```json
{"source": "search",
 "terms": ["说好", "好要", "要做", "做完"],
 "items": [ {"kind": "decision",
             "text": "下周三前把 baseline 跑完并整理成表",
             "last_check": "部分完成",
             "hit_terms": 1},
            {"kind": "progress",
             "text": "承诺「下周三前把 baseline 跑完并整理」→ 部分完成",
             "hit_terms": 1} ]}
```

命中 2 条。这一条同时证明了两件事：

1. **拆词有效。** 整句直接喂 `search(mode=text)` 命中数是 **0**——FTS 是 AND 语义，问句里的「上次」在记忆中不存在就整体落空。把问句拆成 `terms` 里这几个短词并发查询后才有命中。
2. **状态带出来了。** `last_check: "部分完成"` 来自第 2 步写回的那条记忆，不是模型猜的。

> 未单独抓取：在第 2 步之前问同一句，承诺条目应为 `last_check: null`（尚未核对过）。这是设计行为，本次没有留下实测响应。

---

## 第 4 步：PowerContext 侧核对——记忆真的落库了

这一步不经过本应用，直接打 PowerContext，证明数据在服务端而不在页面里。此处用**主验证 scope**（已跑完前 4 步）：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/atomic-memory/list \
  -H "Content-Type: application/json" \
  -d '{"scope_id":"scp_1e4vmndy877wtw7w9vg0c7820k","states":["active"],"limit":100}'
```

实测 4 条，`next_cursor: null`（无下一页）：

| `kind` | `text`（存储原文，含日期前缀） | `artifact_id` | `state` |
|---|---|---|---|
| progress | `[2026-11-13] 承诺「下周三前把 baseline 跑完并整理」→ 部分完成` | `art_0dbb0e95…e47c` | active |
| progress | `[2026-11-13] 今天把检索重排接完了，recall 掉了 2 个点` | `art_1a782282…b10d` | active |
| decision | `[2026-11-13] 下周三前把 baseline 跑完并整理成表` | `art_486b0a2b…6ef0` | active |
| blocker | `[2026-11-13] 评测机器不够，跑全量要 6 小时` | `art_747df9bd…c75c` | active |

对着看三件事：

- **第 2 步的写回真的落库了** —— 第一条 `承诺「…」→ 部分完成` 是核对生成的，不在页面内存里。它的
  `kind` 是 `progress`，正是设计意图：完成情况本身也是一条进展。
- **日期前缀只存在于存储层** —— `text` 里有 `[2026-11-13]`，而第 2 步的草稿和第 3 步的召回结果里都没有它。
- **三类 kind 各就各位** —— progress / decision / blocker 在同一 scope 下都存住了，`kind` 是自由字符串，没有枚举约束。

持久化的最终证明：重启 `python app.py`（甚至换浏览器），`/api/recent` 仍返回这 4 条。

---

## 不配模型也能跑通全流程

三个 `MODEL_*` 变量全部留空，或填了但调用失败，上面每一步都不受影响，**只有第 2 步的 `draft` 来源不同**：

- 降级判据任一命中即触发：`MODEL_BASE_URL` 为空 / `MODEL_API_KEY` 为空 / `MODEL_NAME` 为空 / 请求超时 30s / 返回非 200 / 返回体取不到正文。
- 降级时返回 **`model_status: "template"`**，`reason` 写明具体原因（如 `MODEL_API_KEY 未配置`、`模型返回 404：<地址>（模型名 <name>）｜<上游原文>`）。
- 模板草稿四段标题不变，内容取自本 scope 的真实条目逐条列出；某段没素材就写「本段无记录」，不会出现编造的示例内容。
- 前端在草稿上方显示黄色横幅，明确标出这是模板稿及其原因，不会把模板输出伪装成模型输出。
- 第 1 步的分类、第 3 步的召回、第 2 步的承诺核对与写回**都不依赖模型**，所以不配 key 时闭环仍然成立。

> 本次实测走的是 `model_status: "model"` 分支。`template` 分支由代码逻辑保证，未在本次记录中留实测响应。
> 早前修复 `MODEL_BASE_URL` 版本段识别之前，同一个 `.env` 命中过真实 404 并正确降级为 `template`。

---

## 已知边界（如实记录，未修）

1. `search` 的 `score` 是 raw RRF，同批命中值相同（实测 `0.01639`），所以排序实际靠命中词数，`score` 只在平手时起作用。
2. 记忆没有创建时间字段，「本周」的切分完全依赖 `[YYYY-MM-DD]` 前缀；手动写进 PowerContext 的记忆没有前缀，会落在时间窗口之外。
3. 同一句话重复记录时 `changed` 可能为 `false`，页面提示「这条和之前一模一样，没新增」，但服务端具体去重规则未做进一步验证。
4. 本应用不提供编辑与删除：类型标错只能换 `idempotency_key` 起新账本，或在 PowerContext 侧用本应用未调用的 lifecycle 接口处理。
5. 核对面板最多 5 条承诺，超出部分要下周再看——这是有意的范围控制，不是遗漏。
