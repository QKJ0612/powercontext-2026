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
| 第 5 步边界账本 | `IDEMPOTENCY_KEY=gm-final-20261113`，`scope_id=scp_2xy5ttc7716hxzprs9v01menrx` |
| 模板分支账本 | `IDEMPOTENCY_KEY=gm-tpl-check-20261113`（同时清空 `MODEL_BASE_URL` / `MODEL_API_KEY`），`scope_id=scp_10mymz1pt0eh8ngy0bt9btfa4f` |

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
| `下周三前把 baseline 跑完并整理成表` | **decision** | 承诺类词 `['下周']`，截止「周三前」 |
| `评测机器不够，跑全量要 6 小时` | **blocker** | 卡点类词 `['不够']` |

每条返回的关键字段：`kind` / `why` / `text` / `date` / `memory_id` / `deduplicated`。
本次三条 `deduplicated` 均为 `false`（`changed=true`，各自新增了一条记忆）。

第一句是分类器的回归重点：修复前它因为命中单字「掉」被判成 **blocker**，现在按「完成类词」判为 **progress**。

### 分类器补充实测：截止日期识别（修复后新增的能力）

上面三句之后又补了一类输入，实测于第 5 步那个账本（`gm-final-20261113`）：

| 输入 | 实测 `kind` | 实测 `why` |
|---|---|---|
| `周五前读完 contrastive learning 那三篇` | **decision** | 截止「周五前」压倒完成类词 `['读完']` |

这条能力是第 5 步排查时补上的。修复前的实测（账本 `gm-fixcheck4-20261113`）：同一句被记成 **progress**，
第 1 段把它当进展列出，`counts.pending` 只有 1 而不是 2，第二段自然也不会提它。

现在 `DEADLINE`（`app.py:105`）认「周X前 / 星期X前 / 下周前 / 本周前 / 这周前 / 明天前 / 后天前 /
月底前 / 本月前 / 下个月前 / N号前、N日前」，命中即判 decision，且优先于完成类词。
过去时态不误判（24 例回归，含这 3 例）：`两周前的数据已经清洗完了`、`三周前就读完了那篇`、
`2个月前就跑完了`、`上个月前的旧数据不用管` 全部仍是 **progress**；`把评审时间提前了两天` 不被
「后天前」误伤，仍是 **progress**。

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

## 第 5 步：`checks` 边界与四段结构——第二段跟着账本，不跟着参数

第 2 步传的是「用户逐条点完核对」的正常路径，这一步专打反常路径。账本
`IDEMPOTENCY_KEY=gm-final-20261113`，`scope_id=scp_2xy5ttc7716hxzprs9v01menrx`：
progress、blocker 各 1 条 + 2 条 active 承诺（`下周三前把 baseline 跑完并整理成表`、
`周五前读完 contrastive learning 那三篇`）。

出口有三道，按顺序：`commit_section()` 算正文 → `canonicalize_sections()` 裁掉非规范标题 →
`enforce_commit_section()` 用确定性正文覆盖模型写的同名段落（`app.py:432`、`app.py:421`、`app.py:451`）。
返回体新增两个字段：`commit_section_from`（`checks` / `pending` / `none`）与 `dropped_sections`。

| 请求 `checks` | `commit_section_from` | 第二段实测正文 | `written_back` | `skipped_checks` | `dropped_sections` |
|---|---|---|---|---|---|
| `[]` | `pending` | `- 「周五前读完 contrastive learning 那三篇」→ 未核对`<br>`- 「下周三前把 baseline 跑完并整理成表」→ 未核对` | `[]` | 0 | `[{"title":"上次承诺未完成情况","lines":2,"unmatched":2}]` |
| 1 条 `partial` | `checks` | `- 「下周三前…」→ 部分完成`<br>`- 「周五前…」→ 未核对` | `["承诺「下周三前把 baseline 跑完并整理」→ 部分完成"]` | 0 | `[]` |
| 2 条全 `unknown` | `checks` | 两条都 `→ 未核对` | `[]` | 2 | `[]` |
| 1 条 `undone`，另一条不传 | `checks` | `- 「下周三前…」→ 未完成`<br>`- 「周五前…」→ 未核对` | `["承诺「下周三前把 baseline 跑完并整理」→ 未完成"]` | 0 | `[]` |

四组 `model_status` 全为 `model`，`counts` 全为
`{"week_progress":1,"week_blockers":1,"pending":2,"open_blockers":0}`，`draft` 里
`[YYYY-MM-DD]` 命中数均为 **0**（同一批请求里 PowerContext 存储原文仍带前缀，对比见第 4 步），
`标题` 恰好四个且一字不差：`["本周进展","上次承诺完成情况","卡点","下周计划"]`。

**确定性复现（不靠模型配合）**：把早前实测抓到的那份真实不合规草稿——第二段已列两条「未核对」，但后面多出
`## 当前仍待兑现的承诺` 一节（重复列同样两句承诺）——原样喂回出口管道（只替换模型客户端，
账本与写回都走真实 PowerContext）：

```
dropped_sections = [{"title": "当前仍待兑现的承诺", "lines": 2, "unmatched": 0}]
commit_section_from = pending
标题恰好四个 = True        第五节已消失 = True
第二段 = - 「周五前读完 contrastive learning 那三篇」→ 未核对
         - 「下周三前把 baseline 跑完并整理成表」→ 未核对
```

判定要点：

- **空 `checks` 不等于没承诺**。`counts.pending=2` 时段落照样逐条列出，只把状态标成「未核对」——
  修复前这里写的是「本段无记录」，等于把产品卖点交给一个参数细节。
- **`unknown` 只进草稿，不写回记忆**。跳过核对不是「已完成」，写回就等于把没核对过的事当成发生过。
- **传了部分 `checks` 不能抹掉剩下的承诺**（第 4 行）：未覆盖到的条目仍以「未核对」列出。
- **非规范标题一律裁掉**，评审按四段看结构。裁掉的行若都能对上承诺账本，信息不丢——第二段紧接着由
  `commit_section()` 整段重建，账本里的承诺必然在里面；对不上账本的行以 `unmatched` 计数如实上报，
  不静默吞掉（第 1 行就是这种：模型把标题写成了「上次承诺**未**完成情况」，两条行按 `「…」→ 未核对`
  的写法对不上原文，`unmatched=2`，但第二段仍逐条列出这两条承诺）。
- **模型不听话也由代码兜**。实测 GLM-4-Flash 曾对 `pending=1` 输出「- 本段无记录」，即使 prompt 里加粗
  写了禁止条款；现在模型返回之后、响应发出之前，这段由代码算出来并强行覆盖它写的同名段落，
  模型整段漏写时还会自动补在「## 卡点」之前。

---

## 不配模型也能跑通全流程

三个 `MODEL_*` 变量全部留空，或填了但调用失败，上面每一步都不受影响，**只有第 2 步的 `draft` 来源不同**：

- 降级判据任一命中即触发：`MODEL_BASE_URL` 为空 / `MODEL_API_KEY` 为空 / `MODEL_NAME` 为空 / 请求超时 30s / 返回非 200 / 返回体取不到正文。
- 降级时返回 **`model_status: "template"`**，`reason` 写明具体原因（如 `MODEL_API_KEY 未配置`、`模型返回 404：<地址>（模型名 <name>）｜<上游原文>`）。
- 模板草稿四段标题不变，内容取自本 scope 的真实条目逐条列出；某段没素材就写「本段无记录」，不会出现编造的示例内容。
- 前端在草稿上方显示黄色横幅，明确标出这是模板稿及其原因，不会把模板输出伪装成模型输出。
- 第 1 步的分类、第 3 步的召回、第 2 步的承诺核对与写回**都不依赖模型**，所以不配 key 时闭环仍然成立。

> 上面第 1-4 步走的是 `model_status: "model"` 分支。`template` 分支的实测响应在下一小节。

### 模板分支实测（清空 `MODEL_BASE_URL` / `MODEL_API_KEY` 后重启服务）

新账本 `IDEMPOTENCY_KEY=gm-tpl-check-20261113`，`scope_id=scp_10mymz1pt0eh8ngy0bt9btfa4f`：

```json
{"ok": true, "kind": "decision",
 "why": "承诺类词：['下周']（完成类词 ['跑完'] 不压倒承诺），截止「周三前」",
 "text": "下周三前把 baseline 跑完并整理成表", "date": "2026-11-13",
 "memory_id": "art_dffc6661a85c4fcda60a324c3a914664", "deduplicated": false}
```

`POST /api/report {"checks":[]}` 实测返回：

```
model_status        = template
reason              = MODEL_BASE_URL 未配置
commit_section_from = pending
dropped_sections    = []
标题                = ['本周进展', '上次承诺完成情况', '卡点', '下周计划']
第二段              = - 「下周三前把 baseline 跑完并整理成表」→ 未核对
draft 里 [YYYY-MM-DD] 命中 = False
```

`/api/health` 同期返回 `model_ready: false`。这一段补上后，`template` 分支不再是「代码逻辑保证、未留实测响应」——
四段标题、第二段跟着账本、日期前缀不泄漏三条都在降级路径上真跑过一遍。

### 返回非 200 时的 `reason` 原文（实测两条，均为 `model_status: "template"`）

把 `MODEL_BASE_URL` 填成版本段不对的地址，上游真实返回被整段带进 `reason`，页面上直接能看出是配错还是模型名错：

```
reason = 模型返回 404：https://open.bigmodel.cn/api/paas/v1/chat/completions（模型名 glm-4-flash）｜{"timestamp":"2026-11-13T01:43:42.602+00:00","status":404,"error":"Not Found","path":"/v1/chat/completions"}

reason = 模型返回 404：https://open.bigmodel.cn/api/paas/v99/chat/completions（模型名 glm-4-flash）｜{"timestamp":"2026-11-13T01:43:44.169+00:00","status":404,"error":"Not Found","path":"/v99/chat/completions"}
```

第三条降级判据也实测过：同一个 `MODEL_BASE_URL=.../api/paas/v99` 配上无效 key 时，上游回的是 **200**，
但返回体里没有 `choices`——此时 `reason = 模型返回里没有正文`，同样走 `template`，不报错、不白屏。

---

## 已知边界（如实记录；除第 6 条已修，其余未修）

1. `search` 的 `score` 是 raw RRF，同批命中值相同（实测 `0.01639`），所以排序实际靠命中词数，`score` 只在平手时起作用。
2. 记忆没有创建时间字段，「本周」的切分完全依赖 `[YYYY-MM-DD]` 前缀；手动写进 PowerContext 的记忆没有前缀，会落在时间窗口之外。
3. **重复记同一句话会存成两条**（实测）：同一 scope 内把 `下周三前把 baseline 跑完并整理成表` 记两次，
   返回两条不同 `memory_id`、两次都是 `deduplicated: false`，第二段因此把同一句列了两遍。
   代码里保留了 `deduplicated` 字段（取服务端 `changed`），但本次实测服务端未按内容去重，该字段恒为 `false`。
4. 本应用不提供编辑与删除：类型标错只能换 `idempotency_key` 起新账本，或在 PowerContext 侧用本应用未调用的 lifecycle 接口处理。
5. 核对面板最多 5 条承诺，超出部分要下周再看——这是有意的范围控制，不是遗漏。
6. ~~模型偶尔在四段之外自造一段~~（已修）：第 5 步用例①的真实返回里多出 `## 当前仍待兑现的承诺`，
   现在 `canonicalize_sections()` 把非规范标题裁掉，草稿恰好剩四个规范标题；那一段的行若都对得上承诺账本
   （实测 `unmatched: 0`）就一条不丢——第二段紧接着由 `commit_section()` 从账本整段重建。
   对不上账本的行不静默吞，记进返回体的 `dropped_sections`（实测用例①另有一个笔误标题
   `上次承诺未完成情况`，`lines: 2, unmatched: 2`，两条承诺仍以未核对列出）。
7. 模型仍可能自造标题的**写法**而不只是标题：实测用例①把「上次承诺完成情况」写成「上次承诺**未**完成情况」，
   按非规范标题处理被裁掉。规范标题比对用的是精确串，不做拼写容错——这是有意的，容错会把真的跑题也放进来。
