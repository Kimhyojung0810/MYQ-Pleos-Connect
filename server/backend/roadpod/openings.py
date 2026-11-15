"""Radio-style lines the car hears instantly (no LLM wait): show opening, topic hook, and the bridge while research runs.

Templates, not generation: they must play the moment a session starts, and they must never invent facts
(no weather or traffic unless the car sent a verified value).
"""
from __future__ import annotations
import random
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
WEEKDAY = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']


def _greeting(hour):
    if 5 <= hour < 11:
        return '좋은 아침이에요'
    if 11 <= hour < 17:
        return '안녕하세요'
    if 17 <= hour < 22:
        return '좋은 저녁이에요'
    return '늦은 시간까지 운전하시네요'


def _trip(commute, hour):
    # Don't say "출근길" at 10 pm: fall back to a neutral label when the clock disagrees with the commute type.
    if commute == 'TO_WORK' and hour < 13:
        return '출근길'
    if commute == 'FROM_WORK' and hour >= 14:
        return '퇴근길'
    return '가는 길'


def _day_phrase(commute, weekday):
    day = WEEKDAY[weekday]
    if weekday == 0:
        return '한 주를 여는 월요일이네요.' if commute != 'FROM_WORK' else '월요일 하루, 시작부터 쉽지 않으셨죠.'
    if weekday == 2:
        return '한 주의 한가운데, 수요일이에요.'
    if weekday == 4:
        return '드디어 금요일이에요.' if commute != 'FROM_WORK' else '금요일 퇴근길이에요. 한 주 정말 고생 많으셨어요.'
    if weekday >= 5:
        return f'{day}에도 길 위에 계시네요.'
    return f'{day}이에요.'


def opening_line(commute_type, rng: random.Random, weather=None, now: datetime | None = None):
    """First words of the show. `weather` only if the car reported it as verified."""
    now = (now or datetime.now(KST)).astimezone(KST)
    greet, trip, day = _greeting(now.hour), _trip(commute_type, now.hour), _day_phrase(commute_type, now.weekday())
    weather_bit = {'RAIN': '비가 오네요. 오늘은 조금 천천히 가요.', 'SNOW': '눈이 오네요. 오늘은 조금 천천히 가요.'}.get(weather or '', '')
    if trip == '출근길':
        options = [
            f'{greet}. {trip} 라디오 마이큐, 큐큐예요. {day} {weather_bit} 목적지까지 제가 옆에서 말동무해 드릴게요.',
            f'{greet}, 큐큐예요. {day} {weather_bit} 출근길이 조금 덜 지루하도록, 오늘은 곱씹어 볼 만한 이야기 하나 챙겨 왔어요.',
            f'{trip} 마이큐, 큐큐입니다. {greet}. {day} {weather_bit} 회사 도착하기 전까지, 생각할 거리 하나 같이 꺼내 볼게요.',
        ]
    elif trip == '퇴근길':
        options = [
            f'오늘 하루도 수고 많으셨어요. {trip} 마이큐, 큐큐예요. {day} {weather_bit} 일은 잠깐 내려놓고, 집까지 천천히 같이 가요.',
            f'{greet}, {trip} 라디오 마이큐입니다. {day} {weather_bit} 집으로 가는 길, 큐큐가 함께할게요.',
            f'고생한 하루 끝, 마이큐 큐큐예요. {day} {weather_bit} 오늘은 마음 편하게 들을 수 있는 이야기 하나 준비했어요.',
        ]
    else:
        options = [
            f'{greet}, 마이큐 큐큐예요. {day} {weather_bit} 가는 길에 이야기 하나 곁들여 볼게요.',
            f'{greet}. 길 위의 라디오 마이큐, 큐큐입니다. {day} {weather_bit} 오늘도 끝까지 같이 갈게요.',
        ]
    return ' '.join(rng.choice(options).split())


def hook_line(question, rng: random.Random):
    return rng.choice([
        f'오늘 함께 붙잡아 볼 질문은 이거예요. {question}',
        f'오늘의 질문, 먼저 드릴게요. {question}',
        f'오늘 이야기는 이 질문에서 시작해요. {question}',
        f'자, 오늘은 이런 질문을 들고 왔어요. {question}',
    ])


def bridge_line(summary, rng: random.Random):
    """Fills the gap while the episode body is still being researched and written."""
    return rng.choice([
        f'질문은 짧은데, 답은 생각보다 여러 갈래예요. {summary} 천천히 하나씩 짚어 볼게요.',
        f'{summary} 정답을 정해 두기보다, 몇 가지 시선으로 차근차근 따라가 볼게요.',
        f'바로 답하기엔 조금 아까운 질문이죠. {summary} 오늘은 여러 쪽의 이야기를 들어 볼게요.',
    ])
