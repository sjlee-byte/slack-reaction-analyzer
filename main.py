import hashlib
import hmac
import json
import os
import time

import anthropic
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI()

SLACK_BOT_TOKEN = os.environ.get("SLACK_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MY_SLACK_USER_ID = os.environ.get("MY_SLACK_USER_ID", "")

TARGET_EMOJIS = {"thinking_face", "loading", "확인중", "saved-for-later"}

# 중복 이벤트 방지: event_id → 처리 시각
_processed_events: dict[str, float] = {}
_EVENT_TTL = 300  # 5분 후 만료

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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
    """단일 메시지 dict 반환."""
    data = slack_get(
        "conversations.history",
        {"channel": channel, "oldest": ts, "latest": ts, "inclusive": "true", "limit": 1},
    )
    messages = data.get("messages", [])
    if not messages:
        raise RuntimeError("Message not found")
    return messages[0]


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
        max_tokens=2048,
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
    }
