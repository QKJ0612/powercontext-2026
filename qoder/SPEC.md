# 组会搭子 GroupMeeting Buddy · 产品规格

> Qoder × OceanBase 参赛作品 · 主题：用 Qoder 做一个让学习科研更高效的项目
> 指定技术：**PowerContext**（跨会话记忆 + 交接）

## 一、要解决的痛点（真实场景）

每周组会前，你要回答三个问题：

1. 这周我干了什么？
2. 上次说好要做的，做完了吗？
3. 下周该汇报什么？

现在这些信息散在聊天记录、代码提交、笔记本里。**每周要花 30-60 分钟重新拼一遍**，而且经常漏掉上次的决定。

## 二、产品形态

一个本地 Web 应用：左边记「进展」，右边生成「组会汇报草稿」。核心是**它记得住上一次**。

### 三个功能

| 功能 | 输入 | 输出 |
|---|---|---|
| 记进展 | 一段自然语言（「今天把检索重排接完了，但 recall 掉了 2 个点」） | 存成一条带类型的记忆（进展/决定/卡点） |
| 生成汇报 | 点一下 | 按「本周进展 / 上次承诺完成情况 / 卡点 / 下周计划」四段生成草稿 |
| 翻旧账 | 提问（「上次说好要做的做完了吗」） | 从记忆里捞出相关的「决定/承诺」并对照 |

### 关键交互（这是它和普通笔记的区别）

- 每次生成汇报前，先**列出上次的承诺**，逐条问「完成 / 未完成 / 部分完成」
- 生成时把「上次承诺 + 本周进展」一起喂给模型，让它自己对照
- 所有记忆存在 PowerContext 的 scope 里，**换浏览器、重启服务都还在**

## 三、技术架构

```
浏览器 (static/index.html，原生 fetch，无框架)
   │
   ▼
FastAPI (app.py)  监听 127.0.0.1:8080   ← 8000 已被 PowerContext 占用，本服务不能用 8000
   ├── GET  /api/health    自检：PowerContext 可达 / scope 已建 / 模型就绪
   ├── POST /api/log       关键词分类 → 加 [YYYY-MM-DD] 前缀 → /v1/memory/remember
   ├── GET  /api/recent    /v1/atomic-memory/list 全量 → 剥日期前缀 → 流水
   ├── GET  /api/pending   最近 5 条 kind=decision，扣除已核对为「完成」的
   ├── POST /api/report    list 全量 → 核对结果写回记忆 → list（模型失败则模板拼装）
   └── POST /api/recall    /v1/atomic-memory/search(mode=text) → 过滤 active
                                          → 按 score 降序取 5，失败退回 list + 本地匹配
   │
   ├── PowerContext  http://127.0.0.1:8000
   └── 模型（OpenAI 兼容，读 MODEL_BASE_URL / MODEL_API_KEY / MODEL_NAME）
```

### 数据约定

- 一个「项目」= 一个 PowerContext scope（`idempotency_key = gm-groupmeeting-buddy`）
- 记忆 `kind` 只有三类，与汇报四段一一咬合：

  | kind | 语义 | 喂给汇报的哪一段 |
  |---|---|---|
  | `progress` | 本周做过的事 | 第一段「本周进展」 |
  | `decision` | 承诺／约定（不是泛泛的"决定"） | 第二段「上次承诺完成情况」 |
  | `blocker` | 卡点 | 第三段「卡点」 |

  第四段「下周计划」不单独存 kind，由未完成承诺 + 卡点推导。
- 存储格式固定 `[YYYY-MM-DD] 原文`（本地时区）。PowerContext 的记录里没有创建时间字段，
  时间只能靠这个前缀；**展示给页面和拼进 prompt 之前都要把前缀剥掉**。
- 核对销账也记成一条 `progress`：`[YYYY-MM-DD] 承诺「<原文前20字>」→ 完成/部分完成/未完成`。
  这条既进下一周的汇报，也是"已完成不再重复追问"的判断依据。
- 每条 `reason` 写来源，如「组会搭子：2026-11-13 记」。

> 注：本节取代早先写的 `fact / decision / preference / todo` 四词方案，那套作废。

## 四、验收标准（对着题面对齐）

| 题面要求 | 本作品怎么满足 |
|---|---|
| 围绕学习科研提效 | 组会汇报准备，每周真实发生 |
| 必须用 Qoder 开发 | 见 `PROMPTS.md`，全部开发在 Qoder 里完成 |
| 实际使用 seekdb 或 PowerContext | PowerContext 做记忆读写，不是"只连一下"：汇报内容直接由记忆拼出 |
| Qoder Sites 发布网站 | 见 `SITE.md` 的页面结构 |
| 提交：网站/仓库/运行说明/验证样例/开发记录 | 见仓库根 `SUBMIT.md`（提测清单） |

## 五、不做什么（控制范围）

- 不做用户系统、不做权限、不做多项目切换（一个项目一个实例够演示）
- 不接 Git/日历，避免依赖外部服务
- 不做前端框架，单文件 HTML + 原生 fetch，减少依赖

## 六、目录结构

```
qoder/                 # 交付时的目录名（本地工作区里叫 groupmeeting-buddy/，内容一致）
├── app.py              # FastAPI 后端，单文件，含内置 .env 加载（不依赖 python-dotenv）
├── static/
│   └── index.html      # 单页前端，原生 fetch，全部用 textContent 写入，不拼 innerHTML
├── requirements.txt    # 只锁 fastapi / uvicorn / httpx 三个实际装成功的版本
├── .env.example        # POWERCONTEXT_URL / PORT / SCOPE_* / MODEL_* / POWERCONTEXT_TOKEN
├── README.md           # 运行说明
├── SPEC.md             # 本文件
├── PROMPTS.md          # 开发过程记录（题面要求「围绕 Qoder 开发」的证据）
├── SITE.md             # Qoder Sites 页面文案
└── VALIDATION.md       # 端到端验证记录（全部为实测返回字段，非预期输出）
```

后端进程内不落任何本地状态文件，重启服务后靠 `IDEMPOTENCY_KEY` 找回同一个 scope。
