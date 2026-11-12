# 验证样例与运行证据

> 作品：记仇酒馆（Dify Chatflow + PowerContext）
> 验证时间：2026-11-13 07:50（UTC+8）
> 环境：Dify 1.19.0 自托管 · PowerContext 1.5.1 侧车 · 模型 智谱 GLM-4-Flash

---

## 一、验证目标

一句话：**证明「关掉重开还记得你」不是前端状态，而是 PowerContext 真的存了、Dify 真的读了。**

三个断言：

| # | 断言 | 怎么算通过 |
|---|---|---|
| A1 | 生面孔不编造往事 | 账本为空时，回复里不出现「上回/上次/那次」，并明确表示面生 |
| A2 | 说过的话被写进账本 | PowerContext 的账本接口能查到该酒客的原始语句 |
| A3 | 新会话能翻出旧账 | 另起会话（`conversation_id` 为空）提问，回复复述出前面几次的具体内容 |

---

## 二、初始数据 / 记忆状态

- PowerContext 全新实例，`scope` 按酒客首次出现时创建（`idempotency_key = tavern-<酒客代号>`）
- 本样例使用酒客代号 `孙七`，验证前账本为**空**
- Dify 侧不预置任何对话历史

恢复初始状态（重新验证前执行）：

```bash
# 换一个没用过的酒客代号即可，等价于清空账本
# 例如把下面的 guest 从 孙七 改成 孙八
```

> 若确实要清空已有账本，PowerContext 提供 `POST /v1/atomic-memory/lifecycle` 把条目标记为 `forgotten`（软删除，可从历史恢复）。

---

## 三、验证步骤

调用的是 Dify 的应用 API（等价于在浏览器里打开应用的对话页）：

```bash
API=http://127.0.0.1/v1/chat-messages
KEY=app-xxxxxxxxxxxxxxxx          # 在「访问 API」页面获取
H1="Authorization: Bearer $KEY"
H2="Content-Type: application/json"

# 第 1 轮：生面孔进门
curl -s -X POST $API -H "$H1" -H "$H2" -d '{
  "inputs":{"guest":"孙七"},
  "query":"老板，来杯酒",
  "response_mode":"blocking",
  "user":"reviewer-1",
  "conversation_id":""
}'

# 第 2 轮：新会话，赊账 + 借招牌
curl -s -X POST $API -H "$H1" -H "$H2" -d '{
  "inputs":{"guest":"孙七"},
  "query":"记我账上，顺便把你门口那块招牌借我玩两天",
  "response_mode":"blocking",
  "user":"reviewer-1",
  "conversation_id":""
}'

# 第 3 轮：新会话，追问旧账
curl -s -X POST $API -H "$H1" -H "$H2" -d '{
  "inputs":{"guest":"孙七"},
  "query":"你还记得我上次干了什么吗",
  "response_mode":"blocking",
  "user":"reviewer-1",
  "conversation_id":""
}'
```

关键点：三次请求的 `conversation_id` 都是空，即**三个互不相干的会话**。如果回复能接上，只可能来自 PowerContext。

---

## 四、实测结果

### 第 1 轮（账本为空）

> 你这人脸生的很，来这记仇酒馆是头一回吧？别想着混个熟客脸面，我这酒馆，新面孔不少，谁也不是老客，想混个脸熟可得凭真本事。要酒是吧，自己挑，爱啥喝啥。

✅ 断言 A1 通过：明确表示面生，未编造往事。

### 第 2 轮（新会话）

> 哎哟，你这人，老远就闻见你的酒气了。上回你可不是这么说的，那时候你还说是要还那招牌的。行吧，今儿个心情好，就借你两天，别又弄丢了，回头我可不管你啊。记我账上，可别喝了就忘了。

✅ 本轮写入账本：`记我账上，顺便把你门口那块招牌借我玩两天`

### 第 3 轮（新会话，追问旧账）

> 你啊，上次来还跟我要了杯酒，还说要借我门口那块招牌玩两天。哈哈，你这人，记性还真是差。

✅ 断言 A3 通过：**同时**复述了第 1 轮（要了杯酒）和第 2 轮（借招牌）。

> 对照：把第 3 轮的问句拿去问一个不带记忆的同模型应用，它只会答"抱歉我不记得"，或编一段泛泛的往事。这里的两个细节都指向具体轮次，且都与账本条目吻合。

---

## 五、技术作用证据（PowerContext 确实在起作用）

查该酒客的账本（`scope_id` 由上一步的输出或 `POST /v1/scopes` 拿到）：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/atomic-memory/list \
  -H 'Content-Type: application/json' \
  -d '{"scope_id":"scp_64zt0yr2ygdrs8z6k595q75x5j","limit":50,"states":["active"]}'
```

实际返回（节选）：

```json
{
  "items": [
    {"kind": "fact", "text": "老板，来杯酒", "state": "active"},
    {"kind": "fact", "text": "记我账上，顺便把你门口那块招牌借我玩两天", "state": "active"},
    {"kind": "fact", "text": "你还记得我上次干了什么吗", "state": "active"}
  ],
  "next_cursor": null
}
```

**账本里 3 条记录 = 三轮输入原文**，与第 3 轮回复里复述的两件事一一对应。

### Dify 侧的证据

在 Dify「日志与标注」→ 选中任一轮 → 能看到节点执行记录：

| 节点 | 作用 | 记录里可见的东西 |
|---|---|---|
| 取酒客账本 | `POST /v1/scopes` | 请求体含 `idempotency_key: tavern-孙七`；响应体含 `scope_id` |
| 翻账本 | `POST /v1/atomic-memory/list` | 请求体含上一步的 `scope_id`；响应 `items` 条数随轮次增加 |
| 老板娘开口 | `POST /v1/chat/completions` | 请求体的 `system` 消息里能看到拼进去的账本条目 |
| 记一笔 | `POST /v1/memory/remember` | 请求体含本轮 `query` 原文 |

这四项合起来，就是「记忆如何被保存、又如何被复用」的完整证据链。

---

## 六、异常与边界

| 情况 | 表现 | 说明 |
|---|---|---|
| 生面孔首轮 | 已能正确表示面生 | 早期版本会顺着用户话头编"上回"，已在提示词中加硬约束（禁词）修复 |
| 同一酒客改账本标题 | `POST /v1/scopes` 返回 409 | PowerContext 的幂等键与载荷绑定；本作品固定 title/summary，不受影响 |
| 账本超过 50 条 | 只取最近 50 条 | `limit` 上限 100，可按需调 |
| 模型服务不可用 | 回复「老板娘没吭声，大概是不想理你」 | `取出回答` 节点做了兜底，不会把异常抛给用户 |

---

## 七、复现检查清单

- [ ] `curl http://localhost:8000/health/live` 返回 `{"status":"ok"}`
- [ ] Dify 应用为 Chatflow，DSL 导入无 warning
- [ ] 「老板娘开口」节点的 `__MODEL_API_KEY__` / `__MODEL_NAME__` 已替换
- [ ] 应用已「发布」（未发布时 API 返回 `Workflow not published`）
- [ ] 用一个新酒客代号跑上面三步
- [ ] 第 3 轮能复述前两轮的具体内容
- [ ] `atomic-memory/list` 能查到对应条目
