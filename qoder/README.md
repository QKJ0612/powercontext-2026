# 组会搭子 · 运行说明

一个本地 Web 应用：平时记一句话，组会前一键生成四段汇报草稿，并且能翻出上次承诺对照。
记忆全部存在 PowerContext，本应用不落任何本地文件，重启服务、换浏览器都还在。

## 0. 前置条件

**PowerContext 必须已经在 `127.0.0.1:8000` 运行。** 检查：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/v1/scopes
```

返回 `200` 就可以继续。连不上时后端会返回 502，页面顶部「PowerContext」指示灯为红。

Python 3.11+（3.12 同样可用）。

> 端口说明：本服务默认监听 **8080**，因为 **8000 已被 PowerContext 占用**。这两件事别搞混。

## 1. 建虚拟环境并装依赖

```bash
python -m venv .venv

# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

只有三个依赖：`fastapi`、`uvicorn`、`httpx`。

## 2. 配环境变量

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

`.env` 和进程环境变量都能用，**已存在的进程环境变量优先于 `.env`**。逐项说明：

| 变量 | 必填 | 说明 |
|---|---|---|
| `POWERCONTEXT_URL` | 否 | 记忆服务地址，默认 `http://127.0.0.1:8000` |
| `POWERCONTEXT_TOKEN` | 否 | Bearer token。本地实例允许匿名，留空即可；部署实例开了鉴权才需要填 |
| `SCOPE_TITLE` / `SCOPE_SUMMARY` / `IDEMPOTENCY_KEY` | 否 | 记忆本身份。`IDEMPOTENCY_KEY` 固定，保证重启后复用同一个 `scope_id` |
| `MODEL_BASE_URL` | 生成时需要 | 任何 OpenAI 兼容接口，如 `https://api.openai.com/v1`。已带 `/v1` 或已带 `/chat/completions` 都能识别 |
| `MODEL_API_KEY` | 生成时需要 | **不要提交真实 key。** 留空时走模板降级，功能不受阻 |
| `MODEL_NAME` | 生成时需要 | 模型名，如 `gpt-4o-mini` |
| `PORT` | 否 | 本服务端口，默认 `8080`，别设成 `8000` |

### 不填 key 也能跑通全流程

三个 `MODEL_*` 全部留空，四个功能照样可用，只有草稿质量不同：

| | 配了模型 | 没配模型 |
|---|---|---|
| 记进展 | ✅ | ✅（关键词分类，不调模型） |
| 生成汇报 | 模型综合记忆写出连贯四段 | **模板拼装**：标题不变，内容是记忆本里的真实条目逐条列出 |
| 翻旧账 | ✅ | ✅（走 PowerContext 文字搜索，不调模型） |
| 承诺核对 | ✅ | ✅（核对写回记忆这条闭环不依赖模型） |

降级触发条件是任一：`MODEL_API_KEY` 为空、请求超时 30 秒、返回非 200、返回体取不到正文。
此时响应里 `model_status = "template"`，并在 `reason` 里写明原因，**页面草稿上方会出现黄色横幅**，不会把模板输出伪装成模型输出。

## 3. 启动

```bash
# Windows
.venv\Scripts\python.exe app.py

# macOS / Linux
.venv/bin/python app.py
```

浏览器打开 **http://127.0.0.1:8080** 。

改端口：`set PORT=9000 && .venv\Scripts\python.exe app.py`（Windows）。

自检接口：http://127.0.0.1:8080/api/health —— 一次性报告 PowerContext 是否可达、scope 是否建立成功、模型是否就绪。

## 4. 上手顺序

1. 左上输入框写一句话，例如「今天把检索重排接完了，但 recall 掉了 2 个点」，点**存下来**。类型默认自动判断，也可手动指定进展／承诺／卡点。
2. 记几条 `承诺`（含「承诺／说好／下周／决定」这类词会自动判成承诺）。
3. 点**生成组会汇报** → 弹出上次承诺核对面板，逐条选完成／部分完成／未完成 → **确认，生成**。
4. 核对结果会作为新的一条记忆写回，下周生成时自动带上，且已完成不再重复追问。
5. 右下**翻旧账**输入「上次说好要做的做完了吗」，看捞出来的承诺与核对记录。

> 说明：「记忆本」只列原始记录，核对后写回的销账记录（`承诺「…」→ 完成/部分完成/未完成`）
> 不在流水里重复显示，只在「翻旧账」的结果里带出，避免同一件事出现两次。

## 5. 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 依赖自检 |
| POST | `/api/log` | 记一条（自动分类 + 日期前缀） |
| GET | `/api/recent?limit=40` | 记忆本流水 |
| GET | `/api/pending` | 待核对承诺（最多 5 条） |
| POST | `/api/report` | 核对写回 + 生成四段草稿 |
| POST | `/api/recall` | 翻旧账 |

前端只用原生 `fetch`，无框架、无构建步骤。
