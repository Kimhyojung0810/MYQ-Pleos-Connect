"""Driving-context DJ (기능 ②, ⑥, 재생 제어 보조).

- Detects meaningful changes between two context.updated payloads (traffic jam starts/clears, rain/wiper,
  arrival approaching) and turns them into short DJ comments. Templates only: they are instant, never invent
  road facts, and never block the WebSocket loop the way an LLM call would.
- Music notes (마이크 드롭): a song a driver leaves on a road segment, introduced to other drivers who pass
  the same segment or sit in similar traffic at a similar time of day.
- ETA helpers for fitting an episode into the time left.
"""
from __future__ import annotations
import random, re
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
CHARS_PER_SECOND = 5.5          # ~330 chars/min of Korean TTS
ARRIVAL_WRAP_SECONDS = 180      # under this ETA: close the current topic instead of starting a new one
COOLDOWN_SECONDS = {'jam': 600, 'clear': 600, 'rain': 900, 'snow': 900, 'music': 600}
GLOBAL_GAP_SECONDS = 90         # never two driving comments closer than this

COMMENTS = {
    'jam': ['길이 조금 막히기 시작하네요. 서두르지 않아도 괜찮아요. 이야기는 천천히 이어 갈게요.',
            '앞이 꽤 밀리네요. 이럴 땐 차간 거리만 넉넉히 두고, 귀는 편하게 열어 두세요.'],
    'clear': ['막혔던 길이 풀리고 있어요. 속도가 붙을 때일수록 앞차와 거리를 조금 더 두고 가요.',
              '이제 길이 좀 뚫리네요. 급하게 차선 바꾸지 말고 흐름 따라 가요.'],
    'rain': ['와이퍼가 움직이기 시작했네요. 빗길에서는 평소보다 거리를 조금 더 두고 가요.',
             '비가 오는 것 같아요. 노면이 미끄러울 수 있으니 브레이크는 부드럽게 밟아 주세요.'],
    'snow': ['눈이 오네요. 오늘은 평소보다 천천히, 거리를 넉넉히 두고 가요.'],
    'arrival': ['곧 도착이네요. 오늘 이야기는 여기서 차근차근 정리해 볼게요.'],
}
SIGN_OFF = ['곧 도착이네요. 남은 길도 천천히, 조심히 가요.',
            '목적지가 가까워졌어요. 마지막까지 안전하게 가요.']


def _traffic(ctx):
    t = (ctx or {}).get('traffic') or {}
    return t.get('congestion') if t.get('verified') else None


def _weather(ctx):
    w = (ctx or {}).get('weather') or {}
    return w.get('condition') if w.get('verified') else None


def _wiper(ctx):
    return ((ctx or {}).get('vehicle') or {}).get('wiper_active')


def eta_of(ctx):
    eta = ((ctx or {}).get('navigation') or {}).get('eta_seconds')
    return eta if isinstance(eta, (int, float)) and eta >= 0 else None


def segment_of(ctx):
    return ((ctx or {}).get('navigation') or {}).get('road_segment_id')


def road_name_of(ctx):
    return ((ctx or {}).get('navigation') or {}).get('road_name')


def traffic_of(ctx):
    return _traffic(ctx)


def weather_of(ctx):
    return _weather(ctx)


def driving_events(prev: dict, new: dict) -> list[str]:
    """Transitions only: a state that merely persists never triggers a second comment."""
    events = []
    before, after = _traffic(prev), _traffic(new)
    if after == 'JAM' and before != 'JAM':
        events.append('jam')
    elif before == 'JAM' and after == 'FREE':
        events.append('clear')
    wet = ('RAIN', 'SNOW')
    if (_weather(new) in wet and _weather(prev) not in wet) or (_wiper(new) is True and _wiper(prev) is False):
        events.append('snow' if _weather(new) == 'SNOW' else 'rain')
    pe, ne = eta_of(prev), eta_of(new)
    if ne is not None and ne <= ARRIVAL_WRAP_SECONDS and (pe is None or pe > ARRIVAL_WRAP_SECONDS):
        events.append('arrival')
    return events


def comment_for(kind, rng: random.Random):
    return rng.choice(COMMENTS[kind])


def _subject(word):
    """'올림픽대로' → '올림픽대로가', '경인고속도로' → '경인고속도로가', '강변북로' → '강변북로가'; 받침 있으면 '이'."""
    last = word[-1]
    if '가' <= last <= '힣' and (ord(last) - 0xAC00) % 28:
        return word + '이'
    return word + '가'


def situation_comment(events, *, road_name=None, delay_minutes=None, resume=False, rng: random.Random):
    """One short briefing when traffic and weather change together, e.g.
    "올림픽대로가 비 때문에 조금 느려요. 도착은 10분쯤 늦어질 것 같아요. 그럼 아까 이야기 마저 이어 갈게요."
    Only verified values reach here; the delay comes from the car's own ETA, never guessed."""
    jam = 'jam' in events
    wet = 'rain' if 'rain' in events else 'snow' if 'snow' in events else None
    if not (jam and wet) and not (jam and road_name):
        kind = 'jam' if jam else wet or events[0]
        line = comment_for(kind, rng)
    else:
        where = _subject(road_name) if road_name else '길이'
        cause = {'rain': '비 때문에 ', 'snow': '눈 때문에 '}.get(wet, '')
        line = f'{where} {cause}조금 느려요.'
    if delay_minutes and delay_minutes >= 3:
        line += f' 도착은 {delay_minutes}분쯤 늦어질 것 같아요.'
    if resume:
        line += ' 그럼 아까 이야기 마저 이어 갈게요.'
    return line


def sign_off(rng: random.Random):
    return rng.choice(SIGN_OFF)


def speech_seconds(text):
    return len(text) / CHARS_PER_SECOND


def time_bucket(now: datetime | None = None):
    hour = (now or datetime.now(KST)).astimezone(KST).hour
    if 5 <= hour < 10:
        return 'MORNING'
    if 10 <= hour < 16:
        return 'DAY'
    if 16 <= hour < 21:
        return 'EVENING'
    return 'NIGHT'


# --- 마이크 드롭 ---
MUSIC_REQUEST = re.compile(r'마이크\s*드[롭랍]|음표|(노래|곡|음악)\s*(을|를|하나)?\s*.{0,8}(추천|남길|남겨|드[롭랍]|놓고)')
_QUOTED = re.compile(r'[「『"“‘\'](.+?)[」』"”’\']')
_FILLER = re.compile(r'(큐큐야|마이크\s*드[롭랍]|음표|이\s*(노래|곡)|(노래|곡|음악)\s*(을|를|하나)?|추천(할게|해|할래|해요|합니다)?|'
                     r'남겨\s*(줘|주세요|둘게|놓을게)?|남길게|드[롭랍](해\s*줘|해\s*주세요|할게|해)?|여기에|이\s*구간에|놓고\s*갈게)')

MUSIC_SYSTEM = '''운전자가 다른 운전자에게 추천하려고 말한 문장에서 곡 정보만 뽑는다.
- "곡 제목 - 가수" 한 줄만 출력한다. 가수를 모르면 곡 제목만 쓴다.
- 곡 이름이 없으면 NONE 만 출력한다. 설명 금지.'''


def is_music_request(text):
    return bool(MUSIC_REQUEST.search(text or ''))


def guess_track(text):
    """Deterministic fallback: a quoted title if present, else the utterance minus the request words."""
    m = _QUOTED.search(text or '')
    if m:
        return m.group(1).strip()[:60]
    rest = _FILLER.sub(' ', text or '')
    rest = re.sub(r'\s+', ' ', rest).strip(' .,!?~')
    return rest[:60] if len(rest) >= 2 else ''   # "이 노래 드랍해줘" names no song: use what is playing


def note_intro(track, road_name=None):
    where = '이 구간을' if not road_name else '이 길을'
    return (f'{where} 지나던 다른 운전자가 음표 하나를 남겨 두었어요. 「{track}」. '
            '지금 길에 어울린다고 생각했나 봐요. 나중에 편할 때 한번 들어 보세요.')
