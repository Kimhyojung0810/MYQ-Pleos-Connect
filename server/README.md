# MY Q 서버 — AI 방송 백엔드

주제 딥다이브, 청취자끼리의 사연 네트워크(Dear. Q), 사연 기반 새 주제, 출퇴근 메모리, 음표(Music Q), 주차 그림일기(Q Diary)를 만드는 MY Q 방송 서버.

## 시작 (Mac / Python 3.10+)

```bash
cd server
# macOS 기본 python3는 3.9라 실패한다. 3.10+ 를 지정 (예: brew install python@3.11)
/opt/homebrew/bin/python3.11 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
PYTHONPATH=backend python -m pytest backend/tests -q
PYTHONPATH=backend python -m uvicorn roadpod.main:app --host 127.0.0.1 --port 8765
```

다른 터미널에서:

```bash
cd server
source .venv/bin/activate
python mocks/client_v1_1.py
```

- Mock 모드가 기본이라 API 키가 없어도 실행된다.
- 실제 GPT: `.env`에서 `LLM_PROVIDER=openai`, `OPENAI_API_KEY=...` 입력. 외부 호출 비용과 네트워크가 발생한다.
- 큐큐 목소리(OpenAI TTS): `.env`에서 `TTS_PROVIDER=openai` (키는 `OPENAI_API_KEY` 공용). 서버가 멘트·사연마다 mp3를 만들어 `audio_url`로 보내고 차는 그 파일을 재생한다. 늦거나 실패하면 차의 기본 음성으로 읽는다. 목소리는 `OPENAI_TTS_VOICE`(기본 coral), 말투·속도는 `roadpod/voice.py`의 지시문으로 조절. 음성 분량만큼 비용이 발생하고, 만든 파일은 DB 옆 `tts_cache/`에 쌓인다.
- 최종 데모 영상용 목소리(Fish Audio): `TTS_PROVIDER=fish`, `FISH_API_KEY`, `FISH_REFERENCE_ID`를 `.env`에. 비싸서 데모 촬영 때만 켠다. 같은 멘트는 `tts_cache/`에서 재사용(무료)하고, 새로 만든 글자 수가 `FISH_MAX_CHARS`(기본 15000)를 넘으면 차 기본 음성으로 넘어간다. 사용량은 `tts_cache/fish_usage.json`.
- 개발용 인증: `DEV_TOKEN`. 운영에서 사용 금지. 로컬 WS 연결을 위한 테스트용 키이며 사용자별 권한을 보장하지 않는다.
- `http://127.0.0.1:8765/docs`에서 REST API 볼 수 있다.
- 예전 Mock 구현은 `mocks/mock_server.py`, `mocks/mock_client.py`로 함께 보관. **새 엔진은 `backend/roadpod/main.py`를 실행**한다.

## 구성

```text
contracts/roadpod-api-v1.yaml                     기존 공통 API (변경 없음)
contracts/schemas/vehicle-context.schema.json      기존 차량 데이터 계약 (변경 없음)
contracts/schemas/broadcast-command.schema.json    기존 방송 명령 계약 (변경 없음)
contracts/schemas/episode-context.schema.json      신규 에피소드 모델 초안
contracts/schemas/story-relationship.schema.json   신규 사연 관계 모델 초안
contracts/EXTENSION_NOTES_v1_1.md                  v1 호환 확장 설계
backend/roadpod/main.py                            FastAPI REST+WebSocket
backend/roadpod/engine.py                          Orchestrator+Arbiter+SQLite+GPT provider
backend/tests/test_engine.py                       초기 단위·통합 테스트
mocks/client_v1_1.py                              차량 A/B Mock 클라이언트
mocks/mock_server.py, mock_client.py                이전 버전 Mock
.env.example                                       키 입력 템플릿
.gitignore                                         비밀키·로컬 DB 무시
docs/개발_상세_계획_Notion.md                          전체 개발 로드맵·DB·합의
docs/주제_선택과_에피소드_생성.md                      주제 선택·리서치·원고 파이프라인, 계약 변경안
 docs/개발_협업_합의서.md                           친구와 공유할 기준
```

## 중요: 무엇이 구현되었나?

실행 가능: 세션 생성 / `session.started` 시 서버가 주제 자동 선택(출퇴근별 이력·탐색·남은 시간 반영, 30개 카탈로그) / 리서처 LLM(웹 검색)+작가 LLM 에피소드 제작과 근거 검증 / 세그먼트 자동 진행·다음 주제 자동 연결 / 내비로 끊긴 세그먼트 재생 / 사용자 주제 선택·스킵 / 확정 발화에 대한 Mock 또는 GPT 응답 / ACK 기반 단일 TTS 큐 / 내비 우선 pause-resume / 사연 초안·승인 (외부 미발행).

미구현: 실제 OpenAI 키로 검증한 원고 품질, 영속 DB, 자동차 API 연동, 사연 검수·전송, 타인 사이 사연 그래프 방송, 새 주제 발굴 실행, 완성된 개인화 업데이트, 음표 지리 추천, 일기 그림 생성. 관련 DB 테이블은 설계의 출발점일 뿐이다.

**주의:** 차량 정차만으로 주차가 확정되지 않는다. 주차 일기 명령은 현 스캐폴드가 발행하지 않는다. 개인정보 필터는 규칙 예시이며 운영 사용 금지.
