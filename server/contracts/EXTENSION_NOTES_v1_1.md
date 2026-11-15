# Contract v1.1 Extension Notes (v1-compatible)

**기존 v1.0 JSON Schema 2개와 OpenAPI 명세는 원문 그대로 보관.** 새로운 `episode-context.schema.json` 및 `story-relationship.schema.json`은 **백엔드 내부 모델/추후 협의용**이며 기존 차량 앱에서 반드시 전송할 필드가 아니다.

- 기존 `user.action`의 `payload.action=select_topic` + `topic_key`로 주제 시작 (현재 prototype 지원).
- `payload.action=next_segment`로 다음 에피소드 구간 요청 (prototype 지원).
- 기존 `user.utterance`에 `payload.intent=CREATE_STORY`는 사연 잡기 시작 (후속 질문 1회 → 초안, 승인 전 공개 금지). `intent=DROP_MUSIC`은 마이크 드롭.
- `payload.action`: `request_story`(사연/맞사연 시작), `drop_music`+`track`(음표), `play_relay`(사연 릴레이 방송) 추가 — 모두 기존 `user.action` 안의 값이라 스키마 변경 없음.
- 기존 `story.preview`를 공유 전 확인에 사용 (prototype 지원).
- `/api/v1/stories/{story_id}/approval`: 승인 시 검수(개인정보·욕설·자해 표현) 후 `PUBLISHED` 또는 `BLOCKED`, 응답 `published` 불리언. 발행된 사연은 매칭되는 차에 `story.incoming`으로 전달 (2026-11-13 구현).
- **새 명령 `music.play {track_id, title, artist?, url, duration_seconds, license?}`** (스키마에 추가): 음악 코너 음원 재생, 끝나면 COMPLETED ACK. 모르는 앱은 FAILED ACK해도 방송은 이어진다.
- `diary.ready {diary_id, view_url}` 발송 구현, `view_url`은 `GET /diaries/{id}` 페이지. 동작 전체는 `docs/엔진_동작_정리.md`.
- **계획 중인 확장:** episode segment metadata, `story_relations`, 재생 위치/안전 타임라인 동기화, 위치 및 큐 상태. 실제 앱과 계약 합의 후 v1.1 스키마로 추가 예정.

**배포 전 블로커:** 사용자/세션 인증, 개인정보·콘텐츠 검수, 서드파티 콘텐츠 권리, 음성/위치 동의, 안전한 실차 환경 검증.
