# 记仇酒馆 The Grudge Tavern

> Dify × PowerContext 参赛作品 · Just for Fun 方向
>
> 一个会记住你干过什么的酒馆老板娘。关掉重开、换个会话，她照样能从账本里翻出你的旧账。

---

## 一、这是什么 / 有什么好玩的

你推开酒馆的门，老板娘阿蛮头也不抬。你说什么、赊了什么账、吹了什么牛，她都会记进账本。
**下一次**（哪怕刷新页面、换一个新会话）你再进门，她会主动提起上次的事：

> 「上回你赊了三杯烈酒，还顺走了我一只杯子，今儿个还想再来一杯？」

玩法就是聊天，没有操作门槛。乐趣在于：她记得住，而且会记仇。

## 二、Dify 与 PowerContext 的分工

| 组件 | 在作品里干什么 |
|---|---|
| **Dify（Chatflow）** | 承担全部流程编排：接收输入 → 调 PowerContext 建账本 → 翻账本 → 调模型 → 写账本 → 输出回复。模型调用、数据读写、结果渲染都在 Dify 的图里完成 |
| **PowerContext** | 承担**跨会话记忆**：每个酒客一个 `scope`（数据分区），记忆以 `atomic-memory` artifact 存储，带版本、可追溯。这就是「关掉重开还记得你」的来源 |

工作流节点顺序：

```
开始 → 取酒客账本(scopes) → 解析 scope_id → 翻账本(atomic-memory/list)
     → 整理账本(拼人设) → 老板娘开口(模型) → 记一笔(memory/remember)
     → 取出回答 → 直接回复
```

> 为什么不用 Dify 内置的 LLM 节点：本作品用 HTTP Request 节点直接调 OpenAI 兼容接口，好处是**换模型只改一处**（url / key / model），不依赖任何插件市场，评审复现成本最低。Dify 依然承担完整的编排职责。

## 三、运行环境与依赖

| 项 | 版本 / 说明 |
|---|---|
| Dify | **1.19.0**（`docker/docker-compose.yaml`，官方 compose 部署） |
| PowerContext | **1.5.1**（`powercontext[cli,server]==1.5.1`，本仓提供侧车 Dockerfile） |
| 数据库 | Dify 自带 postgres/redis/weaviate；PowerContext 用容器内 SQLite |
| 模型 | 任意 OpenAI 兼容对话接口。验证时用**智谱 GLM-4-Flash**（免费） |
| 硬件 | 4 核 8G 可跑；本次验证环境 24 核 7.8G |

### 两个必须的配置（否则跑不通）

**1）PowerContext 无鉴权绑定非回环地址**

PowerContext 默认拒绝把**无鉴权**的服务绑到 `0.0.0.0`，容器里必须显式放开：

```yaml
environment:
  - POWERCONTEXT_SERVER_ALLOW_UNAUTHENTICATED_NON_LOOPBACK=true
```

**2）Dify 的 SSRF 保护（1.15.0 起默认拒绝私网/回环）**

Dify 会拦截 HTTP 节点访问内网地址。因为 PowerContext 与 Dify 在**同一个 Docker 网络**里，用服务名 `http://powercontext:8000` 访问，并放行该网段：

```bash
# docker/.env
SSRF_PROXY_ALLOW_PRIVATE_IPS=172.16.0.0/12,10.0.0.0/8,192.168.0.0/16
SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=powercontext,host.docker.internal
```

改完重建 `ssrf_proxy` 容器。**注意：HTTP 节点里不要写 `127.0.0.1`**，那是 api 容器自己的回环。

## 四、从零复现

### 1. 起 Dify

```bash
git clone https://github.com/langgenius/dify.git
cd dify/docker
cp .env.example .env
# 按上面「两个必须的配置」改 .env
docker compose pull
docker compose up -d
```

### 2. 把 PowerContext 挂成侧车

把本目录的 `Dockerfile.powercontext` 和 `docker-compose.override.yml` 放进 `dify/docker/`，然后：

```bash
mkdir -p volumes/powercontext
docker compose up -d          # 会自动 build 出 powercontext-tavern:1.5.1
curl -s http://127.0.0.1:8000/health/live   # 期望 {"status":"ok"}
```

### 3. 导入 DSL

1. 浏览器打开 `http://localhost`，完成 Dify 初始化（建管理员账号）
2. 「工作室」→「创建空白应用」→ 选 **Chatflow**
3. 右上角「导入 DSL」→ 选 `app.dsl.yml`
4. 打开「老板娘开口」节点，把两处占位值换成你自己的：

| 占位值 | 换成 |
|---|---|
| `__MODEL_API_KEY__` | 你的模型 API Key |
| `__MODEL_NAME__` | 你的模型名，如 `glm-4-flash` |

> 模型地址默认是智谱 `https://open.bigmodel.cn/api/paas/v4/chat/completions`。换别家就同时改这个节点的 URL（例如硅基流动 `https://api.siliconflow.cn/v1/chat/completions`）。

5. 点「发布」→「更新」

### 4. 验证

见 `VALIDATION.md`。

## 五、关键文件

| 文件 | 用途 |
|---|---|
| `app.dsl.yml` | 应用 DSL（**已脱敏，key 为占位值**） |
| `Dockerfile.powercontext` | PowerContext 侧车镜像 |
| `docker-compose.override.yml` | 把侧车挂进 Dify 的 compose 网络 |
| `VALIDATION.md` | 验证样例、预期结果、实测记录、证据 |
| `NOTES.md` | 踩坑记录：SSRF、HTTP 节点响应是字符串、幂等键冲突等 |

## 六、已知限制

1. **账本是逐条罗列的**，不是语义检索。这样「翻旧账」稳定可复现；代价是账目多了以后 prompt 会变长（当前 `limit: 50`）。
2. 记忆写入用酒客原话，不做摘要，所以语气会比较"原汁原味"。
3. 首次进店（账本为空）时，模型偶尔仍会顺着话头编一句"上回你…"。规则 2 已明确禁止并要求表明面生，实测生面孔第一轮已能正确说「没你的名，面生得很」。
4. PowerContext 的 `scopes` 接口按 `idempotency_key` 幂等：**同一酒客第一次建账本时的 title/summary 会固定下来**，之后改这两个字段再调会返回 409。生产使用应把账本元信息也做成版本化写入。
5. 本地无 GPU，验证时用的是云端免费模型；换算力模型只需改节点 URL 与模型名。

## 七、来源与致谢

- Dify：https://github.com/langgenius/dify （Apache-2.0）
- PowerContext：https://github.com/oceanbase/powercontext （Apache-2.0）
- 模型：智谱 GLM-4-Flash（验证用）
- 本作品为 2026 第六届 OceanBase 数据库大赛参赛作品，代码与素材自行编写，未复用其他参赛队作品。
