"""组会搭子 · 单文件 FastAPI 后端。

记忆全部存在 PowerContext，本进程不落任何本地状态。
"""

from __future__ import annotations

import os
import re
import json
import asyncio
from datetime import date, datetime, timedelta
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 配置


def load_dotenv(path: str) -> None:
    """只填真实环境里没设过的键；不引入 python-dotenv。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

POWERCONTEXT_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
POWERCONTEXT_TOKEN = os.getenv("POWERCONTEXT_TOKEN", "").strip()

SCOPE_TITLE = os.getenv("SCOPE_TITLE", "组会搭子")
SCOPE_SUMMARY = os.getenv("SCOPE_SUMMARY", "研究生组会周报记忆本：进展 / 承诺 / 卡点")
IDEMPOTENCY_KEY = os.getenv("IDEMPOTENCY_KEY", "gm-groupmeeting-buddy")

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "").rstrip("/")
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "").strip()
MODEL_NAME = os.getenv("MODEL_NAME", "").strip()

PORT = int(os.getenv("PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))

KINDS = ("progress", "decision", "blocker")
CHECK_LABELS = {"done": "完成", "partial": "部分完成", "undone": "未完成"}

TEXT_MAX_BYTES = 8192 - 16  # 留一点余量给日期前缀
LIST_PAGE_LIMIT = 100
LIST_MAX_PAGES = 20

# ---------------------------------------------------------------- 日期前缀

DATE_PREFIX = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s?")
# 核对销账记录：承诺「<原文前20字>」→ 完成|部分完成|未完成
CHECK_LINE = re.compile(r"^承诺「(.{1,20})」→ (完成|部分完成|未完成)$")


def stamp(text: str, day: date | None = None) -> str:
    return f"[{(day or date.today()).isoformat()}] {text}"


def unstamp(text: str) -> tuple[str | None, str]:
    m = DATE_PREFIX.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def today() -> date:
    return datetime.now().astimezone().date()


def week_start(ref: date | None = None) -> date:
    d = ref or today()
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------- 分类规则
# 只用语义明确的多字词。早期用过「卡/掉/挂/缺/复现」这类单字，实测会把
# 「接完了，但 recall 掉了 2 个点」判成卡点——这句是 SPEC 里记进展的范例，判反了。
DECISION_HINTS = ("承诺", "说好", "约定", "定好", "决定", "拍板", "以后", "下周", "本周要", "本周得",
                  "这周要", "计划要", "必须", "负责", "改成", "定成", "要在", "得在", "安排", "约定好")
BLOCKER_HINTS = ("卡住", "卡了", "卡点", "卡壳", "跑不通", "起不来", "打不开", "连不上", "对不上",
                 "上不去", "没解决", "待解决", "还是不行", "一直没", "报错", "阻塞", "失败",
                 "复现不了", "没头绪", "不够", "缺标注", "缺数据", "掉点", "崩", "超时",
                 "没法", "缺了", "做不了", "跑不了", "试不出来", "没跑通")
DONE_HINTS = ("跑完", "接完", "做完", "完成", "修好", "解决", "实现", "验证", "上线", "整理好",
              "读完", "接通", "调好", "配好", "部署", "补齐")


def hits_in(text: str, hints: tuple[str, ...]) -> list[str]:
    low = text.lower()
    return [h for h in hints if h in low]


def classify(text: str) -> tuple[str, str]:
    """优先级：承诺 > 卡点 > 完成类。承诺压倒完成，因为「下周把 X 跑完」是承诺不是进展。"""
    d = hits_in(text, DECISION_HINTS)
    b = hits_in(text, BLOCKER_HINTS)
    f = hits_in(text, DONE_HINTS)
    if d:
        return "decision", f"承诺类词：{d}（完成类词 {f} 不压倒承诺）"
    if b:
        return "blocker", f"卡点类词：{b}"
    if f:
        return "progress", f"完成类词：{f}"
    return "progress", "未命中关键词，默认记为进展"


# ---------------------------------------------------------------- PowerContext 客户端


class PcError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"PowerContext {status}: {message}")


def _error_message(status: int, body: Any) -> str:
    """两种错误体：路由校验 422 是 {detail:[...]}，处理器内 5xx 是 {error:{...}}。"""
    if not isinstance(body, dict):
        return str(body)[:300] or f"HTTP {status}"
    err = body.get("error")
    if isinstance(err, dict):
        return f"{err.get('code') or 'error'}: {err.get('message') or ''}".strip()
    detail = body.get("detail")
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                parts.append(f"{loc} {item.get('msg', '')}".strip())
            else:
                parts.append(str(item))
        return "422 " + "; ".join(parts)
    if isinstance(detail, str):
        return detail
    return json.dumps(body, ensure_ascii=False)[:300]


class PowerContext:
    def __init__(self, client: httpx.AsyncClient):
        self._c = client
        self.scope_id: str | None = None

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            r = await self._c.post(f"{POWERCONTEXT_URL}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise PcError(0, f"连不上 PowerContext（{POWERCONTEXT_URL}）：{exc.__class__.__name__}") from exc
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise PcError(r.status_code, _error_message(r.status_code, body))
        return body if isinstance(body, dict) else {"data": body}

    async def ensure_scope(self) -> str:
        if self.scope_id:
            return self.scope_id
        payload = {"title": SCOPE_TITLE, "summary": SCOPE_SUMMARY, "idempotency_key": IDEMPOTENCY_KEY}
        try:
            # 成功是 201，不是 200
            body = await self._post("/v1/scopes", payload)
        except PcError as exc:
            if exc.status in (401, 403):
                raise PcError(exc.status, f"PowerContext 拒绝了请求（{exc.message}）。若该实例开启了鉴权，请设置 POWERCONTEXT_TOKEN。")
            if exc.status not in (409, 400, 422):
                raise
            # 幂等键撞车或标题重复：从已有 scope 里找回
            body = await self._find_existing_scope()
            if not body:
                raise exc
        sid = body.get("scope_id")
        if not sid:
            raise PcError(500, f"创建 scope 后没拿到 scope_id，返回体：{str(body)[:200]}")
        self.scope_id = sid
        return sid

    async def _find_existing_scope(self) -> dict | None:
        try:
            r = await self._c.get(f"{POWERCONTEXT_URL}/v1/scopes", params={"limit": 200})
            if r.status_code != 200:
                return None
            for item in (r.json() or {}).get("items", []):
                if item.get("title") == SCOPE_TITLE and item.get("scope_id"):
                    return item
        except (httpx.HTTPError, ValueError):
            return None
        return None

    async def remember(self, kind: str, text: str, reason: str) -> dict:
        sid = await self.ensure_scope()
        body = await self._post("/v1/memory/remember", {
            "scope_id": sid, "kind": kind, "text": text, "reason": reason,
        })
        records = body.get("records") or []
        changed = bool(body.get("changed"))
        return {"changed": changed, "record": records[0] if records else None}

    async def list_active(self, kind: str | None = None) -> list[dict]:
        """全量翻页。next_cursor 实测可能为 null，按 null/空 终止。"""
        sid = await self.ensure_scope()
        items: list[dict] = []
        cursor: str | None = None
        for _ in range(LIST_MAX_PAGES):
            payload: dict[str, Any] = {
                "scope_id": sid, "states": ["active"], "limit": LIST_PAGE_LIMIT,
            }
            if kind:
                payload["kind"] = kind
            if cursor:
                payload["cursor"] = cursor
            body = await self._post("/v1/atomic-memory/list", payload)
            items.extend(body.get("items") or [])
            cursor = body.get("next_cursor")
            if not cursor:
                break
        return items

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        """只走 mode=text：实测 vector / hybrid 在本实例直接 500（未配 embedding）。
        search 没有 states 参数，非 active 的记录也会进来。"""
        sid = await self.ensure_scope()
        body = await self._post("/v1/atomic-memory/search", {
            "scope_id": sid, "query": query, "mode": "text", "limit": limit,
        })
        return body.get("hits") or []  # 字段名是 hits，不是 items


def memory_id(rec: dict) -> str:
    """AtomicMemoryRecord 没有顶层 id，唯一可用的稳定标识是 artifact.artifact_id。"""
    art = rec.get("artifact") or {}
    return art.get("artifact_id") or rec.get("text", "")[:32]


def to_entry(rec: dict) -> dict:
    clean_date, clean_text = unstamp(rec.get("text", ""))
    return {
        "memory_id": memory_id(rec),
        "kind": rec.get("kind", ""),
        "text": clean_text,
        "raw_text": rec.get("text", ""),
        "date": clean_date,
        "state": rec.get("state", ""),
        "merged_into_id": rec.get("merged_into_id"),
    }


def sort_by_date(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: e.get("date") or "0000-00-00", reverse=True)


# ---------------------------------------------------------------- 模型客户端


VERSION_TAIL = re.compile(r"/v\d+(?:\.\d+)?/?$")


def _chat_endpoint() -> str | None:
    """不猜默认地址：MODEL_BASE_URL 没配就返回 None，由调用方给出降级原因。
    末尾已带版本段的只补路径——智谱是 /api/paas/v4，硬拼 /v1 会 404。"""
    if not MODEL_BASE_URL:
        return None
    base = MODEL_BASE_URL.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if VERSION_TAIL.search(base):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


async def call_model(client: httpx.AsyncClient, prompt: str) -> tuple[str | None, str]:
    """返回 (正文, None) 或 (None, 降级原因)。任一失败条件都走降级。"""
    if not MODEL_BASE_URL:
        return None, "MODEL_BASE_URL 未配置"
    if not MODEL_API_KEY:
        return None, "MODEL_API_KEY 未配置"
    if not MODEL_NAME:
        return None, "MODEL_NAME 未配置"
    url = _chat_endpoint()
    try:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {MODEL_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=30.0,
        )
    except httpx.TimeoutException:
        return None, f"模型请求超时（>30s）：{url}"
    except httpx.HTTPError as exc:
        return None, f"模型请求失败：{type(exc).__name__}，地址 {url}"
    if r.status_code != 200:
        # 把地址和上游原文带出来，404 这种才可能在页面上直接看出是配错还是模型名错
        return None, f"模型返回 {r.status_code}：{url}（模型名 {MODEL_NAME}）｜{r.text[:200]}"
    try:
        content = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content")
    except (ValueError, AttributeError, IndexError):
        return None, "模型返回体无法解析"
    if not content or not str(content).strip():
        return None, "模型返回里没有正文"
    return str(content).strip(), None


# ---------------------------------------------------------------- 请求体


class LogIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: str | None = None


class CheckIn(BaseModel):
    memory_id: str
    status: str


class ReportIn(BaseModel):
    checks: list[CheckIn] = Field(default_factory=list)


class RecallIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------- 业务


def entries_with_dates(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("date")]


def this_week(entries: list[dict]) -> list[dict]:
    start = week_start().isoformat()
    return [e for e in entries if (e.get("date") or "") >= start]


def parse_check_lines(entries: list[dict]) -> dict[str, dict]:
    """把销账记录归拢成 {承诺前20字: 最近一次核对信息}。"""
    out: dict[str, dict] = {}
    for e in sorted(entries, key=lambda x: x.get("date") or ""):
        if e["kind"] != "progress":
            continue
        m = CHECK_LINE.match(e["text"].strip())
        if not m:
            continue
        out[m.group(1)] = {"status": m.group(2), "date": e.get("date")}
    return out


def already_done(decision_text: str, checks: dict[str, dict]) -> bool:
    """只有最近一次核对为「完成」才销账；未完成/部分完成下周继续出现。"""
    hit = checks.get(decision_text[:20])
    return bool(hit and hit["status"] == "完成")


def pending_decisions(entries: list[dict], limit: int = 5) -> tuple[list[dict], int]:
    checks = parse_check_lines(entries)
    decisions = [e for e in entries if e["kind"] == "decision" and e["state"] == "active"]
    out = [e for e in sort_by_date(decisions) if not already_done(e["text"], checks)]
    return out[:limit], len(decisions) - len(out)


# ---------------------------------------------------------------- 翻旧账的检索词
# 实测：mode=text 是 AND 语义。整句「上次说好要做的做完了吗」里「上次」在记录中不存在，
# 于是一个词不中就全部落空；但单独查「承诺」「下周三」都能命中。所以必须把问句拆成短词。

QUERY_NOISE = ("请问", "帮我", "看一下", "有没有", "是不是", "之前", "上次", "上回", "之前说", "一下",
               "这个", "那个", "什么", "怎样", "如何", "是否", "还", "就", "也", "把", "被", "给",
               "让", "和", "与", "及", "的", "了", "呢", "吗", "么", "吧", "啊", "呀", "哦", "嗯",
               "在", "有", "是", "我", "你", "他", "它", "不", "都", "个", "这", "那")


def query_terms(question: str, max_terms: int = 8) -> list[str]:
    terms: list[str] = re.findall(r"[A-Za-z0-9_\-\.]{2,}", question)
    han = re.sub(r"[^\u4e00-\u9fff]", " ", question)
    for noise in QUERY_NOISE:
        han = han.replace(noise, " ")
    for seg in han.split():
        if len(seg) >= 3:
            terms.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        elif len(seg) >= 2:
            terms.append(seg)
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    return seen[:max_terms]


def local_score(text: str, terms: list[str]) -> float:
    return sum((3 if len(t) >= 2 else 1) for t in terms if t in text)


# 问句里出现这些词，说明问的是「承诺兑现情况」，答案就是承诺账本本身
COMMIT_INTENT = re.compile(
    r"(承诺|说好|约定|说过|要做|该做|没做|做完|完成|搞定|落没落|欠|交代|进度|进展如何|还差)")


REPORT_SECTIONS = ("本周进展", "上次承诺完成情况", "卡点", "下周计划")


def render_template(payload: dict) -> str:
    """降级草稿：只用真实条目拼接，缺素材就写「本段无记录」，不放示例内容。"""
    progress = "\n".join(f"- {e['date']} {e['text']}" for e in payload["week_progress"]) or "本周没有新的进展记录"
    if payload["checks"]:
        done = [c for c in payload["checks"] if c["status"] == "完成"]
        rest = [c for c in payload["checks"] if c["status"] != "完成"]
        lines = [f"- 「{c['text']}」→ {c['status']}" for c in done + rest]
        commit = "\n".join(lines)
    elif payload["pending"]:
        commit = "\n".join(f"- 「{e['text']}」→ 未核对" for e in payload["pending"])
    else:
        commit = "本段无记录"
    blockers = "\n".join(f"- {e['date']} {e['text']}" for e in payload["week_blockers"]) or "本段无记录"

    hints = payload["undone"] + payload["week_blockers"] + payload["open_blockers"]
    plan = "\n".join(f"- 计划处理：{e['text']}" for e in hints[:4]) or "本段无记录"

    return (
        f"## 本周进展\n{progress}\n\n"
        f"## 上次承诺完成情况\n{commit}\n\n"
        f"## 卡点\n{blockers}\n\n"
        f"## 下周计划\n{plan}\n"
    )


def build_prompt(payload: dict) -> str:
    def dump(items: list[dict]) -> str:
        return "\n".join(f"- [{e.get('date') or '无日期'}] {e['text']}" for e in items) or "（无）"

    checks = "\n".join(f"- 「{c['text']}」→ {c['status']}" for c in payload["checks"]) or "（本次没有核对记录）"

    return f"""你是研究生组会汇报助手。下面是「组会搭子」记忆本里的真实条目，据此生成一份组会汇报草稿。

固定输出四段，标题一字不改：
## 本周进展
## 上次承诺完成情况
## 卡点
## 下周计划

规则：
1. 只依据给定条目，禁止编造未出现的数字、实验、论文或结论。
2. 「上次承诺完成情况」必须严格使用下面这份核对结果，不得改写状态、不得补充推测。
3. 「下周计划」由未完成的承诺和卡点推导，最多 4 条，用「计划…」开头。
4. 某段没有素材就只写「本段无记录」，不要凑字数。
5. 日期前缀 [YYYY-MM-DD] 是元数据，不要出现在正文里。
6. 用中文，简洁，每条不超过 40 字。

【本周进展候选】
{dump(payload["week_progress"])}

【上周及更早的进展（仅用于对照，不要写进第一段）】
{dump(payload["older_progress"])}

【上次承诺核对结果】
{checks}

【当前仍待兑现的承诺】
{dump(payload["pending"])}

【本周卡点】
{dump(payload["week_blockers"])}

【历史遗留卡点】
{dump(payload["open_blockers"])}
"""


# ---------------------------------------------------------------- 应用


def _model_client() -> httpx.AsyncClient:
    """模型是外网地址，要尊重系统代理；但代理变量写得难看时 httpx 会在构造期就抛
    InvalidURL（实测 NO_PROXY 含 "[::1]" 即触发），所以退一步关掉 trust_env。"""
    try:
        return httpx.AsyncClient(timeout=30.0)
    except Exception:
        return httpx.AsyncClient(timeout=30.0, trust_env=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    headers = {}
    if POWERCONTEXT_TOKEN:
        headers["Authorization"] = f"Bearer {POWERCONTEXT_TOKEN}"
    # PowerContext 是本机回环，绝不经过代理，所以 trust_env=False——这同时绕开了上面那个构造期崩溃
    app.state.http = httpx.AsyncClient(headers=headers, timeout=15.0, trust_env=False)
    app.state.model_http = _model_client()
    app.state.pc = PowerContext(app.state.http)
    try:
        app.state.pc.scope_id = await app.state.pc.ensure_scope()
    except PcError as exc:
        print(f"[warn] 启动时未能建立 scope：{exc}")
    yield
    await app.state.http.aclose()
    await app.state.model_http.aclose()


app = FastAPI(title="组会搭子", version="1.0.0", lifespan=lifespan)


def pc_http_error(exc: PcError) -> HTTPException:
    return HTTPException(status_code=502 if exc.status == 0 else exc.status, detail=exc.message)


@app.get("/api/health")
async def health(request: Request):
    pc = request.app.state.pc
    out = {
        "powercontext_url": POWERCONTEXT_URL,
        "powercontext_ok": False,
        "scope_id": pc.scope_id,
        "scope_ok": False,
        "model_ready": bool(MODEL_API_KEY and MODEL_NAME and MODEL_BASE_URL),
        "model_name": MODEL_NAME or None,
    }
    try:
        r = await request.app.state.http.get(f"{POWERCONTEXT_URL}/v1/scopes", params={"limit": 1})
        out["powercontext_ok"] = r.status_code == 200
    except httpx.HTTPError:
        pass
    try:
        out["scope_ok"] = bool(await pc.ensure_scope())
    except PcError as exc:
        out["scope_error"] = exc.message
    return out


@app.post("/api/log")
async def log(body: LogIn, request: Request):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "内容为空")
    kind, why = classify(text)
    if body.kind:
        if body.kind not in KINDS:
            raise HTTPException(422, f"kind 只能是 {KINDS}")
        kind, why = body.kind, "用户手动指定"

    stored = stamp(text)
    if len(stored.encode("utf-8")) > TEXT_MAX_BYTES:
        raise HTTPException(422, f"这条太长（{len(stored.encode('utf-8'))} 字节，上限 {TEXT_MAX_BYTES}），请拆成两句")

    try:
        res = await request.app.state.pc.remember(kind, stored, f"组会搭子：{today().isoformat()} 记")
    except PcError as exc:
        raise pc_http_error(exc) from exc

    record = res.get("record") or {}
    return {
        "ok": True,
        "kind": kind,
        "why": why,
        "text": text,
        "date": today().isoformat(),
        "memory_id": record.get("artifact", {}).get("artifact_id"),
        # changed=false 通常意味着服务端把这行判为重复，前端要能提示
        "deduplicated": not res.get("changed", True),
    }


@app.get("/api/recent")
async def recent(request: Request, limit: int = 30):
    try:
        recs = await request.app.state.pc.list_active()
    except PcError as exc:
        raise pc_http_error(exc) from exc
    entries = [e for e in (to_entry(r) for r in recs) if not CHECK_LINE.match(e["text"].strip())]
    return {"items": sort_by_date(entries)[:max(1, min(limit, 200))]}


@app.get("/api/pending")
async def pending(request: Request, limit: int = 5):
    """待核对承诺：active 的 decision，且最近一次核对不是「完成」。"""
    try:
        entries = [to_entry(r) for r in await request.app.state.pc.list_active()]
    except PcError as exc:
        raise pc_http_error(exc) from exc
    items, n_checked = pending_decisions(entries, limit)
    return {"items": items, "checked": n_checked}


@app.post("/api/report")
async def report(body: ReportIn, request: Request):
    pc = request.app.state.pc
    client = request.app.state.model_http
    try:
        recs = await pc.list_active()
    except PcError as exc:
        raise pc_http_error(exc) from exc

    entries = [to_entry(r) for r in recs]
    by_id = {e["memory_id"]: e for e in entries}

    # 把核对结果落成记忆，并组装本次要写进第二段的内容
    resolved: list[dict] = []
    written: list[str] = []
    unknown: list[str] = []
    for c in body.checks:
        if c.status not in CHECK_LABELS:
            raise HTTPException(422, f"status 只能是 {list(CHECK_LABELS)}")
        target = by_id.get(c.memory_id)
        if not target or target["kind"] != "decision":
            unknown.append(c.memory_id)
            continue
        label = CHECK_LABELS[c.status]
        line = f"承诺「{target['text'][:20]}」→ {label}"
        try:
            await pc.remember("progress", stamp(line), f"组会搭子：{today().isoformat()} 承诺核对")
            written.append(line)
        except PcError as exc:
            raise pc_http_error(exc) from exc
        resolved.append({"text": target["text"], "status": label})

    # 写回后重新读一次，保证草稿反映最新账本
    try:
        entries = [to_entry(r) for r in await pc.list_active()]
    except PcError as exc:
        raise pc_http_error(exc) from exc

    # 重新归拢销账表：本次刚勾选的结果也要算进去
    checks_map = parse_check_lines(entries)
    for r in resolved:
        prior = checks_map.get(r["text"][:20])
        if not prior or prior["status"] != r["status"]:
            checks_map[r["text"][:20]] = {"status": r["status"], "date": today().isoformat()}

    dated = entries_with_dates(entries)
    non_check = [e for e in dated if not CHECK_LINE.match(e["text"].strip())]
    week = this_week(non_check)
    decisions = [e for e in sorted(non_check, key=lambda x: x.get("date") or "") if e["kind"] == "decision"]

    week_ids = {e["memory_id"] for e in week}
    week_blocker_ids = {e["memory_id"] for e in week if e["kind"] == "blocker"}
    progress_all = [e for e in non_check if e["kind"] == "progress"]
    blockers_all = [e for e in non_check if e["kind"] == "blocker"]

    payload = {
        "week_progress": [e for e in progress_all if e["memory_id"] in week_ids],
        "older_progress": [e for e in progress_all if e["memory_id"] not in week_ids],
        "week_blockers": [e for e in week if e["kind"] == "blocker"],
        "open_blockers": [e for e in blockers_all if e["memory_id"] not in week_blocker_ids],
        "pending": [e for e in decisions if not already_done(e["text"], checks_map)][-8:],
        "checks": resolved,
        "undone": [e for e in decisions
                   if checks_map.get(e["text"][:20], {}).get("status") in ("部分完成", "未完成")],
    }

    draft, reason = await call_model(client, build_prompt(payload))
    return {
        "draft": draft or render_template(payload),
        "model_status": "model" if draft else "template",
        "reason": reason,
        "sections": list(REPORT_SECTIONS),
        "counts": {k: len(payload[k]) for k in
                   ("week_progress", "week_blockers", "pending", "open_blockers")},
        "written_back": written,
        "unknown_memory_ids": unknown,
    }


@app.post("/api/recall")
async def recall(body: RecallIn, request: Request):
    """翻旧账。三条通路依次兜底，任何一条出结果就返回，全失败也不报错：
    1) search(mode=text) 逐短词并发查——FTS 是 AND 语义，整句查必然落空
    2) 本地短词匹配 list 全量
    3) 承诺类提问直接给承诺账本（这类问题的答案本来就是账本，不是检索运气）
    """
    pc = request.app.state.pc
    question = body.question.strip()

    try:
        all_entries = [to_entry(r) for r in await pc.list_active()]
    except PcError as exc:
        raise pc_http_error(exc) from exc
    terms = query_terms(question)
    merged: dict[str, dict] = {}
    search_failed = False
    queries = [question, *terms]
    try:
        results = await asyncio.gather(*(pc.search(q, limit=20) for q in queries), return_exceptions=True)
    except PcError as exc:
        raise pc_http_error(exc) from exc
    for res in results:
        if isinstance(res, BaseException):
            search_failed = True
            continue
        for h in res:
            rec = h.get("memory") or {}
            if rec.get("state") != "active":          # search 没有 states 参数，只能本地过
                continue
            if rec.get("merged_into_id"):
                continue
            e = to_entry(rec)
            slot = merged.setdefault(e["memory_id"], {**e, "score": 0.0, "hit_terms": 0, "matched_by": []})
            slot["score"] = max(slot["score"], h.get("score") or 0)
            slot["hit_terms"] += 1
            slot["matched_by"] = sorted({*slot["matched_by"], *(h.get("matched_by") or [])})

    checks = parse_check_lines(all_entries)

    def attach_last_check(e: dict) -> dict:
        """承诺条目顺手带出最近一次核对结果，否则「做完了吗」只返回一句裸承诺。"""
        if e["kind"] != "decision":
            return e
        last = checks.get(e["text"][:20])
        e["last_check"] = last["status"] if last else None
        e["last_check_date"] = last["date"] if last else None
        return e

    def rank_key(e: dict):
        # score 实测是 RRF，同批命中值相同（1/61），所以命中词数优先，kind 只作平手裁决
        kind_weight = 3 if e["kind"] == "decision" else 2 if CHECK_LINE.match(e["text"].strip()) else 1
        return (e["hit_terms"], e["score"], kind_weight, e.get("date") or "")

    items = sorted((attach_last_check(e) for e in merged.values()), key=rank_key, reverse=True)
    source = "search"

    if not items:
        scored = sorted(
            ((local_score(e["text"], terms), e) for e in all_entries),
            key=lambda x: (x[0], x[1].get("date") or ""), reverse=True)
        items = [attach_last_check(e) for s, e in scored if s > 0][:5]
        source = "local"

    if not items and COMMIT_INTENT.search(question):
        open_items, _ = pending_decisions(all_entries, limit=5)
        items = [{**attach_last_check(e), "score": None, "hit_terms": 0} for e in open_items]
        if not items:
            done_items = [e for e in all_entries if CHECK_LINE.match(e["text"].strip())]
            items = [{**e, "score": None, "hit_terms": 0} for e in sort_by_date(done_items)[:5]]
        source = "ledger"

    for e in items:
        if isinstance(e.get("score"), float):
            e["score"] = round(e["score"], 5)

    return {
        "question": question,
        "source": source,
        "terms": terms,
        "search_degraded": search_failed and not items,
        "items": items[:5],
    }


# ---------------------------------------------------------------- 静态页面


@app.get("/")
async def index():
    path = os.path.join(HERE, "static", "index.html")
    if not os.path.exists(path):
        return JSONResponse({"hint": "static/index.html 还没生成，后端单独可用。"}, status_code=200)
    return FileResponse(path)


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
