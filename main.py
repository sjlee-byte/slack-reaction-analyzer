import hashlib
import hmac
import json
import os
import time

import anthropic
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pyathena import connect
from pyathena.cursor import DictCursor

app = FastAPI()

SLACK_BOT_TOKEN = os.environ.get("SLACK_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MY_SLACK_USER_ID = os.environ.get("MY_SLACK_USER_ID", "")

# Athena 설정
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
ATHENA_S3_OUTPUT = os.environ.get("ATHENA_S3_OUTPUT", "")  # s3://your-bucket/athena-results/
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "default")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")

TARGET_EMOJIS = {"thinking_face", "loading", "확인중", "saved-for-later"}

# 중복 이벤트 방지: event_id → 처리 시각
_processed_events: dict[str, float] = {}
_EVENT_TTL = 300  # 5분 후 만료

# 동일 메시지 중복 분석 방지: "channel:ts" → 처리 시각 (이모지 여러 개 달아도 1회만)
_processed_messages: dict[str, float] = {}
_MSG_TTL = 3600  # 1시간

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Athena 쿼리 실행 ─────────────────────────────────────────────────────────

def get_athena_connection():
    """Athena 커넥션 생성"""
    if not all([AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, ATHENA_S3_OUTPUT]):
        raise RuntimeError("Athena 환경변수가 설정되지 않았습니다 (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, ATHENA_S3_OUTPUT)")

    return connect(
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        s3_staging_dir=ATHENA_S3_OUTPUT,
        region_name=AWS_REGION,
        work_group=ATHENA_WORKGROUP,
        schema_name=ATHENA_DATABASE,
        cursor_class=DictCursor,
    )


def execute_athena_query(query: str, max_rows: int = 100) -> dict:
    """
    Athena 쿼리 실행 후 결과 반환

    Args:
        query: 실행할 SQL 쿼리
        max_rows: 최대 반환 행 수 (기본 100)

    Returns:
        {"columns": [...], "rows": [...], "row_count": int, "query": str}
    """
    conn = get_athena_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(query)

        rows = cursor.fetchmany(max_rows)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        total_rows = cursor.rowcount if cursor.rowcount >= 0 else len(rows)

        return {
            "columns": columns,
            "rows": rows,
            "row_count": total_rows,
            "query": query,
            "truncated": len(rows) >= max_rows,
        }
    finally:
        conn.close()


def format_athena_result(result: dict) -> str:
    """Athena 결과를 읽기 좋은 텍스트로 포맷팅"""
    if not result["rows"]:
        return "결과 없음"

    lines = []
    lines.append(f"*쿼리:* ```{result['query']}```")
    lines.append(f"*결과:* {result['row_count']}건" + (" (일부만 표시)" if result.get("truncated") else ""))
    lines.append("")

    # 테이블 형태로 포맷팅
    columns = result["columns"]
    rows = result["rows"]

    # 헤더
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "|".join(["---"] * len(columns)) + "|")

    # 데이터
    for row in rows[:20]:  # 최대 20행만 표시
        values = [str(row.get(col, "")) for col in columns]
        lines.append("| " + " | ".join(values) + " |")

    if len(rows) > 20:
        lines.append(f"... 외 {len(rows) - 20}건")

    return "\n".join(lines)

SYSTEM_PROMPT = """당신은 오늘의집 CEO J의 AI 어시스턴트입니다. 슬랙 메시지를 보고 J라면 어떻게 판단할지 의견을 줘.

답변 형식 (슬랙 마크다운 사용):

*[카테고리]* 의사결정 필요 / 피드백 요청 / 단순 정보 공유
*[중요도]* 상/중/하 | *[시급도]* 상/중/하
*[데드라인]* (시급도 상인 경우만, 오늘이면 🚨 오늘)

*[예상 답장]*
(바로 보낼 수 있는 답장 메시지)

*[핵심 요약]*
- 요약 1
- 요약 2
- 요약 3

*[상세 의견]*
(중요도 높을수록 상세하게, 최대 100줄)

---

# User Context: J (CEO, Ohouse / 오늘의집)

## Identity & Role
- CEO of Ohouse (오늘의집), a Korean home interior and lifestyle platform
- 700 employees, 200 developers, 4 million monthly active users
- Platform spans content, commerce, and construction services
- Currently expanding into Japan; planning US market entry

## Communication Style
- Bilingual: Korean (primary) and English
- Prefers precise, authentic communication

## Current Strategic Priorities
- AI integration as core competitive advantage in the AGI era
  → Thesis: physical execution dependency + vertical market specialization
- "OS for the Home" vision
- "Build as One" company culture messaging
- Evaluating AI usage integration into performance evaluations
- Technical debt resolution: over-engineered microservices (819 services / 200 devs)

## Organizational Context
- Recently hired: Head of Technology, Head of HR, Head of Product
- Working on matrix reporting structures and mid-year hire evaluation policies
- Restructuring Japan and US operations

## Recurring Frameworks & Thinking Patterns
- "Crazy Mode Tetris" / "All Clear Block" metaphor for complex life event solutions
- Late-join strategy for platform shifts (e.g., Google's Universal Commerce Protocol)
- Entrepreneurship mindset (curiosity, aspiration, determination) as human edge over AI
- Organizational design as a strategic lever, not just operations

## Personal Context
- Lives with brother
- Interested in interior design, travel, and theoretical physics
- Business class traveler for international markets

## How to Interact with J
- Get to the point quickly; avoid filler or excessive preamble
- When analyzing, start with the core tension or trade-off
- Offer structured options when choices are needed, but don't over-format casual answers
- Match language to whatever J uses (Korean or English) in the message
- Challenge assumptions constructively; J values intellectual pushback"""


# ── Slack signature verification ─────────────────────────────────────────────

def verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    base = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Slack API helpers ─────────────────────────────────────────────────────────

def slack_get(path: str, params: dict) -> dict:
    resp = httpx.get(
        f"https://slack.com/api/{path}",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        params=params,
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack {path} error: {data.get('error')}")
    return data


def slack_post(path: str, payload: dict) -> dict:
    resp = httpx.post(
        f"https://slack.com/api/{path}",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack {path} error: {data.get('error')}")
    return data


def get_message(channel: str, ts: str) -> dict:
    """단일 메시지 dict 반환. reactions.get으로 top-level/스레드 답글 모두 처리."""
    data = slack_get("reactions.get", {"channel": channel, "timestamp": ts, "full": "true"})
    msg = data.get("message")
    if not msg:
        raise RuntimeError("Message not found")
    return msg


def get_thread_messages(channel: str, thread_ts: str) -> list[dict]:
    """스레드 전체 메시지 리스트 반환 (부모 포함)."""
    messages = []
    cursor = None
    while True:
        params = {"channel": channel, "ts": thread_ts, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = slack_get("conversations.replies", params)
        messages.extend(data.get("messages", []))
        meta = data.get("response_metadata", {})
        cursor = meta.get("next_cursor")
        if not cursor:
            break
    return messages


def get_permalink(channel: str, ts: str) -> str:
    data = slack_get("chat.getPermalink", {"channel": channel, "message_ts": ts})
    return data["permalink"]


def open_dm_channel(user_id: str) -> str:
    data = slack_post("conversations.open", {"users": user_id})
    return data["channel"]["id"]


def send_dm(user_id: str, text: str) -> None:
    channel_id = open_dm_channel(user_id)
    slack_post("chat.postMessage", {"channel": channel_id, "text": text})


# ── Thread context builder ────────────────────────────────────────────────────

def build_thread_context(channel: str, ts: str) -> tuple[str, str]:
    """
    (thread_context_text, permalink) 반환.
    - 반응 달린 메시지가 스레드에 속하면 전체 스레드를 읽음
    - 독립 메시지면 단일 메시지만 읽음
    """
    msg = get_message(channel, ts)
    permalink = get_permalink(channel, ts)
    thread_ts = msg.get("thread_ts")

    if thread_ts:
        # 스레드 전체 읽기 (부모 + 모든 리플)
        messages = get_thread_messages(channel, thread_ts)
        lines = []
        for i, m in enumerate(messages):
            prefix = "[원메시지]" if i == 0 else f"[리플 {i}]"
            text = m.get("text", "").strip()
            if text:
                lines.append(f"{prefix} {text}")
        context = "\n".join(lines)
    else:
        # 독립 메시지
        context = msg.get("text", "").strip()

    return context, permalink


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyze_with_claude(thread_context: str) -> str:
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "다음 슬랙 메시지(스레드 포함)를 분석해줘:\n\n"
                    f"{thread_context}"
                ),
            }
        ],
    )
    return response.content[0].text


# ── Main event endpoint ───────────────────────────────────────────────────────

def process_reaction(channel: str, ts: str) -> None:
    try:
        thread_context, permalink = build_thread_context(channel, ts)
        analysis = analyze_with_claude(thread_context)
        dm_text = (
            f":thinking_face: *J's AI Assistant 분석*\n"
            f"*원문 링크:* {permalink}\n\n"
            f"{analysis}"
        )
        send_dm(MY_SLACK_USER_ID, dm_text)
    except Exception as e:
        try:
            send_dm(MY_SLACK_USER_ID, f":warning: 분석 중 오류 발생\n```{e}```")
        except Exception:
            pass


@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    body_bytes = await request.body()
    payload = json.loads(body_bytes)

    # URL verification challenge (서명 검증 전에 처리)
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload["challenge"]})

    # 중복 이벤트 제거
    event_id = payload.get("event_id", "")
    now = time.time()
    # 만료된 항목 정리
    for eid in list(_processed_events):
        if now - _processed_events[eid] > _EVENT_TTL:
            del _processed_events[eid]
    if event_id and event_id in _processed_events:
        return Response(status_code=200)
    if event_id:
        _processed_events[event_id] = now

    # 일반 이벤트는 서명 검증
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(body_bytes, timestamp, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    event = payload.get("event", {})
    if event.get("type") != "reaction_added":
        return Response(status_code=200)

    reaction = event.get("reaction", "")
    reactor_id = event.get("user", "")

    # 지정 이모지이고 본인이 단 경우만 처리
    if reaction not in TARGET_EMOJIS or reactor_id != MY_SLACK_USER_ID:
        return Response(status_code=200)

    item = event.get("item", {})
    if item.get("type") != "message":
        return Response(status_code=200)

    channel = item.get("channel", "")
    ts = item.get("ts", "")

    # 동일 메시지 중복 분석 방지 (여러 이모지 달아도 1회만)
    msg_key = f"{channel}:{ts}"
    now2 = time.time()
    for k in list(_processed_messages):
        if now2 - _processed_messages[k] > _MSG_TTL:
            del _processed_messages[k]
    if msg_key in _processed_messages:
        return Response(status_code=200)
    _processed_messages[msg_key] = now2

    # 즉시 200 반환 후 백그라운드에서 처리 (Slack 재전송 방지)
    background_tasks.add_task(process_reaction, channel, ts)
    return Response(status_code=200)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug")
def debug():
    return {
        "SLACK_TOKEN_set": bool(SLACK_BOT_TOKEN),
        "SLACK_SIGNING_SECRET_set": bool(SLACK_SIGNING_SECRET),
        "ANTHROPIC_API_KEY_set": bool(ANTHROPIC_API_KEY),
        "MY_SLACK_USER_ID": MY_SLACK_USER_ID,
        "AWS_ACCESS_KEY_ID_set": bool(AWS_ACCESS_KEY_ID),
        "AWS_SECRET_ACCESS_KEY_set": bool(AWS_SECRET_ACCESS_KEY),
        "ATHENA_S3_OUTPUT_set": bool(ATHENA_S3_OUTPUT),
        "ATHENA_DATABASE": ATHENA_DATABASE,
        "ATHENA_WORKGROUP": ATHENA_WORKGROUP,
        "AWS_REGION": AWS_REGION,
    }


@app.post("/athena/query")
async def athena_query(request: Request):
    """
    Athena 쿼리 실행 엔드포인트

    POST body: {"query": "SELECT ...", "max_rows": 100}
    """
    try:
        body = await request.json()
        query = body.get("query")
        max_rows = body.get("max_rows", 100)

        if not query:
            raise HTTPException(status_code=400, detail="query 필드가 필요합니다")

        result = execute_athena_query(query, max_rows)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"쿼리 실행 오류: {str(e)}")


@app.get("/athena/tables")
async def athena_tables():
    """현재 데이터베이스의 테이블 목록 조회"""
    try:
        result = execute_athena_query(f"SHOW TABLES IN {ATHENA_DATABASE}")
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"테이블 목록 조회 오류: {str(e)}")
