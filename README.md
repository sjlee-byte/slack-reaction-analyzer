# Slack Reaction Analyzer

Slack에서 🤔 이모지를 달면 해당 메시지(+ 스레드 전체)를 Claude가 분석해서 DM으로 보내줍니다.

---

## 동작 플로우

1. 슬랙 메시지에 🤔 반응
2. 서버가 `reaction_added` 이벤트 수신
3. 반응 달린 메시지 + 스레드 전체 읽기
4. Claude API로 분석 (J CEO 컨텍스트 기반)
5. 분석 결과 + 원문 링크를 본인에게 DM 발송

---

## 환경변수

| 변수 | 설명 |
|------|------|
| `SLACK_TOKEN` | Slack **User** OAuth Token (`xoxp-...`) |
| `SLACK_SIGNING_SECRET` | Slack App의 Signing Secret |
| `ANTHROPIC_API_KEY` | Anthropic API Key |
| `MY_SLACK_USER_ID` | 본인 Slack User ID (예: `U012AB3CD`) |

---

## Slack App 설정 방법

### 1. Slack App 생성
1. https://api.slack.com/apps 접속
2. **Create New App** → **From scratch**
3. App 이름 입력, 워크스페이스 선택

### 2. User Token Scopes 설정
**OAuth & Permissions** → **Scopes** → **User Token Scopes**에 아래 권한 추가:

> Bot Token Scopes가 아닌 **User Token Scopes** 섹션입니다. User Token을 쓰면 본인이 이미 접속한 모든 채널(퍼블릭/프라이빗 무관)에 봇 초대 없이 접근 가능합니다.

| Scope | 용도 |
|-------|------|
| `channels:history` | 퍼블릭 채널 메시지 읽기 |
| `groups:history` | 프라이빗 채널 메시지 읽기 |
| `im:history` | DM 메시지 읽기 |
| `mpim:history` | 그룹 DM 메시지 읽기 |
| `reactions:read` | 이모지 반응 읽기 |
| `chat:write` | 메시지 전송 |
| `im:write` | DM 채널 열기 |

### 3. App 설치 및 Token 발급
1. **OAuth & Permissions** → **Install to Workspace**
2. **User OAuth Token** (`xoxp-...`) 복사 → `SLACK_TOKEN`

### 4. Signing Secret 복사
**Basic Information** → **App Credentials** → **Signing Secret** 복사 → `SLACK_SIGNING_SECRET`

### 5. Event Subscriptions 설정
1. **Event Subscriptions** 탭 → **Enable Events** ON
2. **Request URL**에 배포된 서버 주소 입력:
   ```
   https://your-app.railway.app/slack/events
   ```
   → Slack이 자동으로 challenge 검증 수행 (200 OK 확인)
3. **Subscribe to events on behalf of users**에 추가:
   - `reaction_added`

> "Subscribe to bot events"가 아닌 **"Subscribe to events on behalf of users"** 섹션입니다.

### 6. 본인 User ID 확인
슬랙 앱 → 프로필 클릭 → **Copy member ID** → `MY_SLACK_USER_ID`

---

## Railway 배포

### 1. Railway 프로젝트 생성
```bash
# Railway CLI 사용 시
npm install -g @railway/cli
railway login
railway init
railway up
```

또는 Railway 웹 대시보드에서 GitHub 레포 연결

### 2. 환경변수 설정
Railway 대시보드 → 프로젝트 → **Variables** 탭에서 아래 4개 추가:
```
SLACK_TOKEN=xoxp-...
SLACK_SIGNING_SECRET=...
ANTHROPIC_API_KEY=sk-ant-...
MY_SLACK_USER_ID=U012AB3CD
```

### 3. 배포 확인
```
https://your-app.railway.app/health
# → {"status": "ok"}
```

---

## 로컬 실행

```bash
pip install -r requirements.txt

export SLACK_TOKEN=xoxp-...
export SLACK_SIGNING_SECRET=...
export ANTHROPIC_API_KEY=sk-ant-...
export MY_SLACK_USER_ID=U012AB3CD

uvicorn main:app --reload --port 8000
```

로컬 테스트 시 Slack Event URL은 ngrok 등으로 터널링:
```bash
ngrok http 8000
# → https://xxxx.ngrok.io/slack/events
```
