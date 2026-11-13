# 提测清单（生态赛道 · Qoder + Dify）

> 平台：https://open.oceanbase.com/train/detail/21
> 提交仓库：`QKJ0612/powercontext-2026` · 分支 **`2026-competition-ecosystem`**
> 分支 HEAD：**`8d5851a`**（已推入 `dify/`、`qoder/`、`SUBMIT.md`）

---

## 一、今晚已经做完的

| 项 | 状态 | 位置 |
|---|---|---|
| Dify 应用 DSL（可导入、已跑通） | ✅ | `dify/app.dsl.yml` |
| PowerContext 侧车镜像 + compose 覆盖 | ✅ | `dify/Dockerfile.powercontext`、`dify/docker-compose.override.yml` |
| 运行说明 | ✅ | `dify/README.md` |
| 验证样例 + 实测记录 + 证据 | ✅ | `dify/VALIDATION.md` |
| Qoder 项目规格 | ✅ | `qoder/SPEC.md` |
| Qoder 提示词序列（开发记录来源） | ✅ | `qoder/PROMPTS.md` |
| Qoder Sites 页面文案 | ✅ | `qoder/SITE.md` |

## 二、还需要你做的（只有你能做）

### A. Dify 题（20 分）

1. **确认本地跑通**（可选但强烈建议）：浏览器开 `http://localhost`，用管理员账号进「工作室」，找到「记仇酒馆」应用，发一句「老板，来杯酒」，确认有回复。
   - 管理员账号：`tavern@example.com` / `Tavern#2026pass`
   - 若服务已停：`wsl -d Ubuntu -u root` 后 `cd /opt/dify/docker && docker compose up -d`
2. **截图**：对话页截 2-3 张（生面孔-记一笔-翻旧账），存到 `dify/screenshots/`
3. **提测**：平台 → Dify 题（questionId **600076**）→ 立即提测 → 填 Branch `2026-competition-ecosystem` + Commit

### B. Qoder 题（20 分）

1. **在 Qoder 里跑**：按 `qoder/PROMPTS.md` 的 5 条提示词，逐条发给 Qoder，让它把代码写到仓库的 `qoder/` 下
2. **截图开发记录**：至少 4 张（需求拆解 / 代码生成 / 调试 / 运行验证）
3. **发布站点**：Qoder → Sites → 用 `qoder/SITE.md` 的文案建站 → 可见范围「任何人都可访问」→ 发布，拿到 URL
4. **提测**：平台 → Qoder 题 → 填项目网站 URL、代码仓库地址、Qoder 账号 UID `01a1cf90-b6ea-737f-8c44-450ee95d9c53`

### C. 推送

```bash
cd <仓库目录>
git checkout 2026-competition-ecosystem
git add dify qoder
git commit -m "feat(dify): 记仇酒馆 —— Dify Chatflow × PowerContext 跨会话记忆"
git -c http.version=HTTP/1.1 push
```

---

## 三、提测注意事项（踩过的坑）

| 坑 | 说明 |
|---|---|
| 仓库可见性 | 平台按报名时登记的地址拉代码，**私有没有问题但 token 要有效**；改名/改可见性会导致 `failMsg=4` |
| 提测额度 | 每道题每天 10 次，间隔 10 分钟 |
| 判题时长 | 约 8-15 分钟，别反复提交 |
| DSL 里的 key | 提交的 `app.dsl.yml` 必须是**占位值**（`__MODEL_API_KEY__`），不要带真实 key |
| 站点可见性 | Qoder Sites 必须「任何人都可访问」，企业版账号发布不了公开站点 |
| 评审期间 | 不要暂停或删除已发布的站点 |

---

## 四、如果明天提测出问题

| 现象 | 先查 |
|---|---|
| Dify 题提示仓库拉不到 | 仓库地址、token、分支名 |
| 文件缺 | `git ls-tree origin/2026-competition-ecosystem -- dify` 看是否真的推上去了 |
| 评审说 DSL 导不进去 | Dify 版本差异，让他用 1.19.0；或者把 `version: "0.7.0"` 改成他实例导出的版本 |
| 评审说跑不起来 | 大概率没配 `SSRF_PROXY_ALLOW_PRIVATE_IPS` 或没开 `POWERCONTEXT_SERVER_ALLOW_UNAUTHENTICATED_NON_LOOPBACK`，README 里都写了 |

---

## 五、本地环境速查

| 东西 | 位置 / 命令 |
|---|---|
| WSL 发行版 | `wsl -d Ubuntu -u root` |
| Dify | `/opt/dify/docker`，`docker compose ps` |
| Dify 控制台 | `http://localhost` |
| PowerContext | 容器 `powercontext`，`http://127.0.0.1:8000/health/live` |
| Dify 应用 | `记仇酒馆 The Grudge Tavern`，app_id `bea189c2-463c-482d-b594-c1d87a7e665f` |
| 应用 API Key | `app-qqUkTQpZtchPHg3ptQxrrp1O` |
| Docker 镜像加速 | `/etc/docker/daemon.json`（dockerproxy.net 优先） |
| 端口映射 | Windows `127.0.0.1:80/8000` → WSL `172.30.16.200`（`netsh interface portproxy`） |
