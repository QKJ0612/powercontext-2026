# Qoder 开发记录 · 提示词序列

> 用法：在 **Qoder CN（个人账号）** 里新建一个工作区，按顺序把下面 5 段提示词逐条发给 Qoder。
> 每条发出后等它改完、跑通，再发下一条。**不要一次全贴**，否则开发记录看不出过程。
> 每轮结束记得截图（节点：需求拆解 / 生成代码 / 调试），这些截图就是「Qoder 开发记录」材料。

---

## 第 1 轮 · 让它先理解需求，再动手

```
我要做一个小的本地 Web 应用，名字叫「组会搭子」。

解决的问题：研究生每周组会前要花半小时拼凑"这周干了什么、上次承诺的做完了吗"，很烦。

核心功能三个：
1. 记进展：输入一句话，存下来，并且要记住它属于「进展」还是「决定/承诺」还是「卡点」
2. 生成汇报：点一下，按「本周进展 / 上次承诺完成情况 / 卡点 / 下周计划」四段生成草稿
3. 翻旧账：提问"上次说好要做的做完了吗"，能把之前的「决定/承诺」捞出来对照

技术约束（很重要，别改）：
- 后端用 FastAPI + Python 3.11，单文件 app.py
- 记忆必须存在 PowerContext 里，接口是 HTTP，地址 http://127.0.0.1:8000
  用到的三个接口：
  * POST /v1/scopes            body: {"title","summary","idempotency_key"}  → {"scope_id"}
  * POST /v1/memory/remember   body: {"scope_id","kind","text","reason"}
  * POST /v1/atomic-memory/list body: {"scope_id","limit","states":["active"]} → {"items":[{"text","kind"}]}
- 模型用任何 OpenAI 兼容接口，从环境变量读 BASE_URL / API_KEY / MODEL_NAME
- 前端只要一个 static/index.html，原生 fetch，不引入任何框架

先不要写代码。请先：
1) 复述你对需求的理解
2) 给出文件结构和你打算怎么实现
3) 指出你觉得有风险的地方（比如 PowerContext 返回的 items 字段结构不确定）
```

---

## 第 2 轮 · 后端

```
按刚才的方案写 app.py，要求：

- FastAPI，启动时从 .env 读取 POWERCONTEXT_URL、SCOPE_TITLE、MODEL_BASE_URL、MODEL_API_KEY、MODEL_NAME
- 启动时确保 scope 存在：POST /v1/scopes，idempotency_key 用 gm-{SCOPE_TITLE}；
  如果返回 409（幂等键已存在但载荷不同），改成用同样的载荷重试一次并把错误信息打日志，不要让服务起不来
- 三个接口：
  POST /api/log     {"text": "...", "kind": "fact|decision|todo"}  → 写入 PowerContext，返回 ok
  POST /api/recall  {}  → 读出所有记忆，按 kind 分组返回
  POST /api/report  {}  → 读出所有记忆，拼成 prompt 调模型，返回四段式草稿
- /api/report 的 prompt 要让模型严格按四段输出：本周进展 / 上次承诺完成情况 / 卡点 / 下周计划
- 所有外部调用加超时（10s）和异常兜底：PowerContext 挂了要返回清晰的错误 JSON，不要 500 堆栈
- 静态文件挂在 /，index.html 从 static/ 提供

写完后自己检查一遍：变量名、异常路径、超时。
```

---

## 第 3 轮 · 前端

```
写 static/index.html，单文件，原生 JS，风格干净一点（浅色、圆角、无框架）：

- 顶部一行标题「组会搭子」+ 一个输入框（选类型：进展/决定/卡点）+ 「记一笔」按钮
- 中间一块「记忆」区：按类型分组列出已记录的内容，每条带时间
- 底部一个「生成组会汇报」按钮，点了之后把 /api/report 返回的四段草稿渲染出来
- 页面加载时调 /api/recall 渲染已有记忆
- 所有请求失败都要在页面上给出可读提示，不要静默失败

注意：中文排版，字号别太小，按钮要明显。
```

---

## 第 4 轮 · 自测与修 bug

```
现在帮我自测：
1) 列出这个应用启动需要的前提（PowerContext 在跑、模型 key 已配）
2) 给出 curl 命令，把三个接口各测一次
3) 你自己检查一遍代码里可能的 bug：PowerContext 的 items 里 kind 字段缺失怎么办？
   scope 创建 409 怎么办？模型返回不是四段怎么办？
4) 把发现的问题直接改掉，并告诉我改了什么
```

---

## 第 5 轮 · 收尾材料

```
最后帮我产出两份文档：
1) README.md：项目简介、依赖、从安装到启动的完整步骤、.env 各项说明（用占位值，不要真实 key）
2) VALIDATION.md：一组可复现的验证样例，包含初始状态、三步输入、预期结果、以及怎么在 PowerContext 里核对记忆确实写进去了

两份都要给评审看，写得具体、可执行。
```

---

## 开发记录要截图的关键节点

| 节点 | 截什么 |
|---|---|
| 需求拆解 | 第 1 轮 Qoder 的回复（它复述需求 + 列风险那段） |
| 代码生成 | 第 2、3 轮生成 app.py / index.html 的过程 |
| 调试 | 第 4 轮它找出问题并改掉的那几屏 |
| 运行验证 | 浏览器里跑通的截图（记一条 → 生成汇报） |

> 评审会核对「项目主要由 Qoder 完成」。所以**别在别的编辑器里大改**；要改也回到 Qoder 里让它在对话中改，这样记录是连续的。
