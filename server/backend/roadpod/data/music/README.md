# 음악 코너 음원 폴더

여기에 넣은 파일은 서버가 `/music/<파일명>`으로 내보내고, 차는 `music.play` 명령의 `url`로 재생합니다.

- **라이선스가 이 용도를 허락하는 음원만** 넣습니다. 예: Pixabay Content License, CC0, CC BY(출처 표기 필요).
- 파일을 넣은 뒤 `../music.json`의 `title`, `artist`, `file`, `duration_seconds`, `license`, `source_url`을 실제 값으로 바꿉니다.
- `commute_fit`: 출근(TO_WORK)·퇴근(FROM_WORK)에 얼마나 어울리는지 0~1. 아침엔 밝은 곡, 저녁엔 차분한 곡을 높게.
- mp3, m4a, ogg, wav 만 내보냅니다.
