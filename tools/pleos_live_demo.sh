#!/bin/bash
# 마이큐 라이브 시연 (Pleos Connect 에뮬레이터, 약 1분)
# 멘트는 고정이 아니라 서버가 그때그때 생성합니다. 이 스크립트는 "차량 신호"와 "하은의 말"만 순서대로 넣어요.
# Enter를 누를 때마다 다음 장면으로 넘어가요. 큐큐가 말을 끝낸 걸 듣고 누르면 돼요.
#
#   bash pleos_live_demo.sh            # 에뮬레이터 1대가 연결돼 있을 때
#   ADB="adb -s emulator-5554" bash pleos_live_demo.sh
set -u
ADB=${ADB:-adb}
U="--user 10"                                   # Pleos 에뮬레이터의 운전자 계정
R="-n ai.roadpod.app/.debug.DebugReceiver"      # debug·mock·제출용 release에 있음 (adb에서만 동작)
send(){ $ADB shell am broadcast $U $R -a "ai.roadpod.debug.$1" "${@:2}" >/dev/null && echo "   ✓ $1 ${*:2}"; }
say(){ $ADB shell "am broadcast $U $R -a ai.roadpod.debug.SAY --es text '$1'" >/dev/null && echo "   🙋 \"$1\""; }
step(){ echo; echo "▶ $1"; read -r -p "   (Enter) " _; }

echo "마이큐 라이브 시연 · 앱이 켜져 있고 화면에 '방송 연결됨'이 보여야 해요."

step "1. 출발 — 주행 시작 (속도 40, 기어 D, 도착까지 10분). 큐큐가 오늘의 주제로 오프닝 (MY Q)"
send SIM --ef speed 40 --ei gear 8 --ei eta 600 --ez wiper false --ez clear_arrive true

step "2. 내비 안내 시작 — 큐큐가 바로 멈춰요 (재생 제어)"
send NAV --ez active true
sleep 4
echo "   (4초 안내 후 종료)"
send NAV --ez active false
echo "   → 끊긴 문장부터 다시 이어 말해요"

step "3. 신호 대기 — 차가 멈추면 말로 사연 보내기 (Dear. Q)"
send SIM --ef speed 0
sleep 1
say "큐큐야, 내 생각 사연으로 남겨 둘래. 나는 무례한 사람을 꼭 내 편으로 만들어야 할까? 어렵긴 한데 암튼 그래."
echo "   → 큐큐가 공감하고, 익명 사연 초안을 읽어 준 뒤 '보낼까요?'라고 물어요"

step "4. 공개 승인"
say "응, 보내."

step "5. 도착·주차 — 기어 P (Q Diary)"
send SIM --ef speed 0 --ei gear 4 --ez arrive true --ei eta 0
echo "   → 주차로 판단되면 클로징 멘트와 Q Diary QR이 떠요 (앱의 주차 대기 시간 설정만큼 걸려요)"

echo; echo "끝. 다시 하려면 앱의 '방송 종료'로 세션을 끝내고 앱을 다시 열어 주세요."
