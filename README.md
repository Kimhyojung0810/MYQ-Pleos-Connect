# MY Q (마이큐)

**차량의 주행 상황에 맞춰 개인화된 방송을 만들고, 운전자끼리 사연과 음악을 나누는 AI 차량용 팟캐스트 앱**

진행자 **큐큐**가 출퇴근길/도로 상황/날씨/남은 시간에 맞춰 방송을 이어 가고, 내비게이션 안내가 나오면 바로 멈췄다가 끊긴 곳부터 다시 이어 갑니다.

| 기능 | 내용 |
|---|---|
| **MY Q** | 출근/퇴근별 주제, 리서치 기반 딥다이브, 비/정체 같은 주행 상황 코멘트 |
| **Dear. Q** | 말로 사연 보내기(초안 → 공개 확인), 다른 청취자 사연과 답장, 사연에서 새 주제 발굴 |
| **Music Q** | 듣던 곡을 지금 도로 구간에 음표로 남기고, 같은 구간을 지나는 운전자에게 소개 |
| **Q Diary** | 주차 후 하루의 사연/답장/회고를 그림일기로 만들어 차 화면 QR로 전달 |
| **상황 기반 재생 제어** | 내비 안내/주행 이벤트 때 발화 보류, 안정되면 이어 말하기 |

---

## APK

- 위치: [`apk/QQ_MYQ_release.apk`](apk/)
- **release 빌드, 바로 실행 가능** — 서버/인터넷/API 키 없이 동작합니다.
  방송 내용은 미리 만든 대본과 음성(앱에 포함)으로 재생되고, **차량 데이터, 내비게이션 연동, 음성 인식, 주차 판단, 화면은 실제 앱 코드 그대로** 동작합니다.
  GPT로 방송을 실시간 생성하는 전체 구성은 아래 [서버 연결 실행](#3-서버-연결-실행-gpt로-방송-생성)으로 실행할 수 있습니다. 다만 이는 유료 GPT API Key가 과금됩니다. 

## 실행 방법

### 1. APK 설치 (Pleos Connect 에뮬레이터)
```bash
adb install -r --user 10 apk/QQ_MYQ_release.apk
adb shell am start --user 10 -n ai.roadpod.app/.MainActivity
```
처음 실행 시 권한 2개(마이크, 차량 속도)를 허용합니다. 내비게이션을 켠 상태에서 MY Q를 분할 화면 오른쪽에 두면 실제 차량 화면 구성으로 볼 수 있습니다.

**체험 흐름**
- 시동 후 시간대에 맞춰(오후 3시 전 출근길/이후 퇴근길) 주제로 오프닝 → 딥다이브 → 청취자 사연 → 잠시 뒤 다른 청취자의 답장과 큐큐 코멘트
- 내비 안내가 시작되면 즉시 멈추고, 끝나면 이어서 말합니다
- 와이퍼를 켜면 빗길 코멘트, 정차하면 사연 보내기 안내
- **사연 보내기** 버튼(또는 마이크 버튼을 누르고 "큐큐야, 내 생각 사연으로 남겨 둘래 …") → 공감 → 초안 낭독 → "보낼까요?" 뒤 마이크가 자동으로 열림 → "응, 보내"
- 마이크 버튼 → "이 노래 드랍해줘" → 지금 구간에 음표 남기기
- 기어 P로 주차(20초 뒤 확정) → 마무리 멘트와 Q Diary(그림일기 + QR)

**adb로 장면 재현하기** — 에뮬레이터는 내비 안내/위치 재생이 어려워서, adb로 차량 신호와 말을 넣어 각 기능을 바로 볼 수 있게 했습니다.
(adb 셸에서만 동작합니다. 수신기에 `android.permission.DUMP` 권한을 걸어 다른 앱은 신호를 넣을 수 없습니다.)
```bash
R="--user 10 -n ai.roadpod.app/.debug.DebugReceiver"
adb shell am broadcast $R -a ai.roadpod.debug.SIM --ef speed 40 --ei gear 8        # 주행 시작 (기어 D)
adb shell am broadcast $R -a ai.roadpod.debug.SIM --ez wiper true                   # 비 · 와이퍼 → 빗길 코멘트
adb shell am broadcast $R -a ai.roadpod.debug.NAV --ez active true                  # 내비 안내 시작 → 즉시 멈춤
adb shell am broadcast $R -a ai.roadpod.debug.NAV --ez active false                 # 안내 끝 → 이어서 말하기
adb shell am broadcast $R -a ai.roadpod.debug.SIM --ef speed 0                      # 정차 → 사연 보내기 안내
adb shell am broadcast $R -a ai.roadpod.debug.SAY --es text '큐큐야 내 생각 사연으로 남겨 둘래'   # 말 대신 문장 넣기
adb shell am broadcast $R -a ai.roadpod.debug.SAY --es text '응 보내'               # "보낼까요?"에 대답
adb shell am broadcast $R -a ai.roadpod.debug.SAY --es text '이 노래 드랍해줘'       # Music Q
adb shell am broadcast $R -a ai.roadpod.debug.SIM --ei gear 4 --ez arrive true      # 도착·기어 P → 주차 판단 후 Q Diary
```
주차는 "목적지 도착 후 기어 P 상태로 20초"가 지나면 확정됩니다(체험용 APK 기준. 서버 연결 버전은 실제 기준인 5분). 순서대로 한 번에 돌리려면 `bash tools/pleos_live_demo.sh`를 실행하세요.

### 2. 소스에서 빌드
Android Studio(JDK 17), Android SDK 34. Pleos SDK는 `nexus-playground.pleos.ai` 공개 저장소에서 자동으로 받습니다.
```bash
./gradlew assembleRelease          # app/build/outputs/apk/release/app-release.apk
```
`local.properties`에 `sdk.dir`만 있으면 됩니다. (`roadpod.prodUrl`을 넣으면 그 서버에 연결하는 release가 만들어집니다.)

### 3. 서버 연결 실행 (GPT로 방송 생성)
```bash
cd server
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env               # LLM_PROVIDER=openai, OPENAI_API_KEY 등 입력
PYTHONPATH=backend python -m uvicorn roadpod.main:app --host 127.0.0.1 --port 8765
```
앱은 `local.properties`에 `roadpod.baseUrl=http://10.0.2.2:8765`, `roadpod.devToken=<서버 DEV_TOKEN>`을 넣고 `./gradlew assembleDebug`로 빌드합니다.
API 키는 서버 `.env`에만 두며 앱에는 포함되지 않습니다. 서버 테스트: `PYTHONPATH=backend python -m pytest backend/tests -q`

## 테스트 환경 및 필요한 설정

| 항목 | 값 |
|---|---|
| 에뮬레이터 | **Pleos Connect v2.1.0** (Android 14 / API 34, 운전자 계정 user 10) |
| 화면 규격 | **2560 × 1440 px, 160 dpi** (Pleos Connect 에뮬레이터 v2.1.0, Android 14 / API 34) |
| 앱 | minSdk 28 / targetSdk 34, Kotlin, Pleos SDK (Vehicle 2.3.2, NaviHelper 3.0.5, TextToSpeech 3.3.1, SpeechToText 3.3.1) |
| 권한 | 마이크(RECORD_AUDIO), 차량 속도(CAR_SPEED) |
| 음성 입력 | 에뮬레이터 Extended controls → Microphone → 호스트 오디오 입력 사용 |
| 서버(선택) | Python 3.10+, FastAPI. `.env`: `LLM_PROVIDER`, `OPENAI_API_KEY`, `TTS_PROVIDER`, `DEV_TOKEN` |

음성 인식·합성은 Pleos STT/TTS(기기 내 처리)를 우선 사용하고, 사용할 수 없는 환경에서는 안드로이드 기본 엔진으로 자동 전환됩니다.

## 구조

```
app/        Pleos Connect 차량 앱 (Kotlin)
  vehicle/  NaviHelper·Pleos Vehicle SDK·CarPropertyManager → 공통 차량 컨텍스트
  logic/    주차 판단, 주의 구간 판단, context.updated 생성
  command/  명령 만료·중복·세션 검증, 내비 안내 중 발화 억제
  speech/   Pleos TTS·STT (+ 안드로이드 기본 엔진 대체)
  net/      REST + WebSocket 클라이언트, 앱 내장 방송(release 체험 모드)
  assets/headunit/  차량 화면 UI (WebView)
server/     AI 방송 백엔드 (Python, FastAPI)
  backend/roadpod/  방송 오케스트레이터, 에피소드 생성, Dear. Q, Music Q, Q Diary, 음성
  contracts/        앱 ↔ 서버 API 계약 (JSON Schema)
tools/      에뮬레이터 시나리오 스크립트 (adb로 장면 재현)
apk/        제출 APK
```
