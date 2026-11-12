# 组会搭子 GroupMeeting Buddy · 产品规格

> Qoder × OceanBase 参赛作品 · 主题：用 Qoder 做一个让学习科研更高效的项目
> 指定技术：**PowerContext**（跨会话记忆 + 交接）

## 一、要解决的痛点（真实场景）

每周组会前，你要回答三个问题：

1. 这周我干了什么？
2. 上次说好要做的，做完了吗？
3. 下周该汇报什么？

现在这些信息散在聊天记录、代码提交、笔记本里。**每週要花 30-60 分钟重新拼一遍**，而且经常漏掉上次的决定。

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
浏览器 (单页 HTML)
   │  fetch
   ▼
FastAPI (app.py)
   ├── POST /api/log       → PowerContext /v1/memory/remember
   ├── POST /api/report    → /v1/atomic-memory/list → 拼 prompt → 模型 → 返回草稿
   └── POST /api/recall    → /v1/atomic-memory/list → 筛选 kind=decision → 返回
   │
   ├── PowerContext  http://127.0.0.1:8000
   └── 模型（OpenAI 兼容）
```

### 数据约定

- 一个「项目」= 一个 PowerContext scope（`idempotency_key = gm-<项目名>`）
- 记忆 `kind` 取值：`fact`（进展）、`decision`（决定/承诺）、`preference`（偏好）、`todo`（待办）
- 每条 `reason` 写来源，如「组会搭子：2026-11-13 记」

## 四、验收标准（对着题面对齐）

| 题面要求 | 本作品怎么满足 |
|---|---|
| 围绕学习科研提效 | 组会汇报准备，每周真实发生 |
| 必须用 Qoder 开发 | 见 `PROMPTS.md`，全部开发在 Qoder 里完成 |
| 实际使用 seekdb 或 PowerContext | PowerContext 做记忆读写，不是"只连一下"：汇报内容直接由记忆拼出 |
| Qoder Sites 发布网站 | 见 `SITE.md` 的页面结构 |
| 提交：网站/仓库/运行说明/验证样例/开发记录 | 见 `DELIVERABLES.md` |

## 五、不做什么（控制范围）

- 不做用户系统、不做权限、不做多项目切换（一个项目一个实例够演示）
- 不接 Git/日历，避免依赖外部服务
- 不做前端框架，单文件 HTML + 原生 fetch，减少依赖

## 六、目录结构（交给 Qoder 生成）

```
qoder/
├── app.py              # FastAPI 后端
├── static/
│   └── index.html      # 单页前端
├── requirements.txt
├── .env.example        # POWERCONTEXT_URL / SCOPE_TITLE / MODEL_*
├── README.md           # 运行说明
├── VALIDATION.md       # 验证样例
└── SITE.md             # Qoder Sites 页面文案
```
