"""오늘의 Q Diary (기능 ⑦): built once the car is confirmed parked.

Contents come only from what happened in this session: topics heard, what the driver said, stories sent and
received, music notes. The diary text is written by the LLM when live (deterministic template otherwise), and the
illustration is an SVG drawn from the trip's mood, commute and weather. With DIARY_IMAGES=openai an AI picture is
generated in the background and replaces the SVG when ready. The car shows view_url as a QR code; the phone opens it.
"""
from __future__ import annotations
import base64, html, logging, os, re
from pathlib import Path

log = logging.getLogger('roadpod')

MOODS = {
    # mood: (label, sky top, sky bottom, accent)
    'CALM': ('차분함', '#a7c7e7', '#f3e9d2', '#5b8db8'),
    'WARM': ('따뜻함', '#f6c28b', '#fbe7c6', '#d9822b'),
    'THOUGHTFUL': ('생각이 많은', '#b8b5e0', '#ece8f7', '#6b63b5'),
    'TIRED': ('지친', '#8e9aaf', '#cbc0d3', '#4a5568'),
    'BRIGHT': ('가벼운', '#9ad0ec', '#fdf6c3', '#2b9348'),
}
MOOD_WORDS = [
    ('TIRED', r'피곤|지치|힘들|졸려|야근|녹초'),
    ('WARM', r'고마|감사|행복|좋았|따뜻|웃'),
    ('BRIGHT', r'신나|설레|기대|재밌|즐거'),
    ('THOUGHTFUL', r'고민|생각|불안|걱정|왜|어떻게'),
]
DIARY_SYSTEM = '''운전자의 오늘 운행 기록을 보고 짧은 그림일기 글을 쓴다.
- 운전자의 시점, 한국어 3~4문장, 200자 이내, 일기체("~했다").
- 기록에 있는 것만 쓴다. 없는 사건, 장소, 사람을 지어내지 않는다.
- 일기는 링크로 열리므로 다른 사람의 이름, 회사, 구체적 지명, 연락처는 쓰지 않는다. '한 동료', '어느 길'처럼 흐린다.
- stories_heard·replies는 라디오에서 들은 '다른 운전자의 사연'이다. 내 주변 사람의 일처럼 쓰지 않는다.
- 마지막 문장은 내일의 나에게 건네는 짧은 한마디로 끝낸다. 본문만 출력한다.'''
COMMUTE_WORD = {'TO_WORK': '출근길', 'FROM_WORK': '퇴근길'}


def mood_of(texts):
    joined = ' '.join(texts)
    for mood, pattern in MOOD_WORDS:
        if re.search(pattern, joined):
            return mood
    return 'CALM'


def facts_of(*, commute_type, topics, said, sent, heard, notes_left, notes_heard, weather, congestion,
             replies=(), reflection=None):
    return {'commute': COMMUTE_WORD.get(commute_type, '오늘 길'), 'topics': topics, 'said': said[-5:],
            'stories_sent': sent, 'stories_heard': heard, 'notes_left': notes_left, 'notes_heard': notes_heard,
            'weather': weather, 'traffic': congestion, 'replies': list(replies), 'reflection': reflection}


CLOSING = {
    'TO_WORK': '큐큐~ 오늘 드라이브를 Q Diary로 만들어 봤어요! 오늘 출근도 파이팅입니다! 저녁에 만나요~',
    'FROM_WORK': '큐큐~ 오늘 드라이브를 Q Diary로 만들어 봤어요! 푹 쉬어요. 고생 많으셨습니다.',
}
CLOSING_DEFAULT = '큐큐~ 오늘 드라이브를 Q Diary로 만들어 봤어요! 남은 하루도 편안하게 보내세요.'
COMMENT_SYSTEM = '''너는 차량 라디오 DJ 큐큐다. 운전자의 오늘 운행 기록을 보고 그림일기 한쪽에 들어갈 짧은 코멘트를 쓴다.
- 한국어 1문장, 50자 이내, 다정한 존댓말("~요"). 반말과 '너'는 쓰지 않는다. 기록에 없는 일은 말하지 않는다. 이름·회사·지명은 쓰지 않는다.'''
COMMENT_FALLBACK = {
    'TIRED': '오늘 많이 애썼어요. 오늘 밤은 푹 쉬어요.',
    'WARM': '오늘 마음이 조금 따뜻해진 길이었네요.',
    'BRIGHT': '가벼운 마음으로 달린 날이었어요.',
    'THOUGHTFUL': '생각이 많았던 만큼 한 뼘 자란 하루예요.',
    'CALM': '차분하게 잘 달려온 하루였어요.',
}


def _first_sentence(text, limit=40):
    s = re.split(r'(?<=[.!?…])\s+', (text or '').strip())[0].strip()
    return s if len(s) <= limit else s[:limit].rstrip() + '…'


def headline(f):
    """오늘의 한 줄: the evening reflection if there was one, then the driver's own story, then the day's question."""
    if f.get('reflection'):
        return _first_sentence(f['reflection'])
    if f['stories_sent']:
        questions = [q for q in re.split(r'(?<=[.!?…])\s+', f['stories_sent'][-1]) if q.endswith('?')]
        return _first_sentence(questions[0] if questions else f['stories_sent'][-1])
    return f['topics'][0] if f['topics'] else '오늘도 무사히 도착'


def template_text(f):
    lines = [f"{f['commute']}에 큐큐 라디오를 들었다."]
    if f['topics']:
        lines.append(f"「{f['topics'][0]}」라는 질문을 곱씹으며 달렸다.")
    if f['stories_sent']:
        lines.append('떠오른 생각을 익명 사연으로 남겼다.')
    if f.get('replies'):
        lines.append('내 사연에 다른 운전자들이 답장을 보내 주었다.')
    elif f['stories_heard']:
        lines.append('다른 운전자의 사연을 들으며 나만 그런 게 아니구나 싶었다.')
    if f.get('reflection'):
        lines.append(f"오늘의 한 장면은 '{_first_sentence(f['reflection'], 30)}'.")
    if f['notes_left']:
        lines.append(f"길 위에 「{f['notes_left'][0]}」 음표 하나를 두고 왔다.")
    elif f['notes_heard']:
        lines.append(f"누군가 남긴 「{f['notes_heard'][0]}」 음표를 받았다.")
    lines.append('내일의 나도 천천히, 안전하게.')
    return ' '.join(lines)


def svg(mood, commute_type, weather):
    label, top, bottom, accent = MOODS.get(mood, MOODS['CALM'])
    sun_y = 70 if commute_type == 'TO_WORK' else 120
    sun = '#ffd166' if commute_type != 'FROM_WORK' else '#ff8c61'
    rain = ''
    if weather in ('RAIN', 'SNOW'):
        drop = '<circle r="2.5" fill="#ffffff"/>' if weather == 'SNOW' else '<rect width="2" height="10" rx="1" fill="#e0ecf8"/>'
        rain = ''.join(f'<g transform="translate({x},{y})">{drop}</g>' for x, y in
                       [(40, 40), (90, 80), (150, 30), (210, 70), (270, 40), (330, 90), (60, 120), (300, 130)])
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" role="img" aria-label="오늘의 Q Diary 그림: {label}">
<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/></linearGradient></defs>
<rect width="400" height="300" fill="url(#sky)"/>
<circle cx="320" cy="{sun_y}" r="28" fill="{sun}" opacity="0.9"/>
<path d="M0 210 Q100 170 200 200 T400 190 L400 300 L0 300Z" fill="{accent}" opacity="0.35"/>
<path d="M0 240 L400 225 L400 300 L0 300Z" fill="#555b66"/>
<path d="M20 262 L80 259 M140 256 L200 253 M260 250 L320 247" stroke="#f4f1de" stroke-width="4" stroke-linecap="round"/>
<g transform="translate(150,214)"><rect x="0" y="10" width="90" height="26" rx="10" fill="{accent}"/>
<path d="M18 10 L32 -6 L64 -6 L76 10Z" fill="{accent}"/><path d="M36 -2 L46 -2 L46 10 L28 10Z M50 -2 L62 -2 L70 10 L50 10Z" fill="#e8f1fb"/>
<circle cx="22" cy="38" r="9" fill="#2b2d42"/><circle cx="70" cy="38" r="9" fill="#2b2d42"/></g>
{rain}
</svg>'''


def page(d):
    esc = html.escape
    img = (f'<img src="/diaries/{esc(d["diary_id"])}/image" alt="오늘의 Q Diary 그림">')
    items = ''
    if d.get('topics'):
        items += f'<li>들은 이야기: {esc(", ".join(d["topics"]))}</li>'
    for s in d.get('stories_sent') or []:
        items += f'<li>내가 보낸 사연: “{esc(s)}”</li>'
    for r in d.get('replies') or []:
        items += f'<li>돌아온 답장: “{esc(r)}”</li>'
    if d.get('reflection'):
        items += f'<li>저녁 회고 한 줄: “{esc(d["reflection"])}”</li>'
    others = len(d.get('stories_heard') or []) - len(d.get('replies') or [])
    if others > 0:
        items += f'<li>들은 사연 {others}개</li>'
    if d.get('notes_left') or d.get('notes_heard'):
        items += f'<li>음표: {esc(", ".join((d.get("notes_left") or []) + (d.get("notes_heard") or [])))}</li>'
    label = MOODS.get(d.get('mood'), MOODS['CALM'])[0]
    top = ''
    if d.get('headline'):
        top += f'<div class="headline">“{esc(d["headline"])}”</div>'
    if d.get('dj_comment'):
        top += f'<div class="dj">큐큐\'s 코멘트 · {esc(d["dj_comment"])}</div>'
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>오늘의 Q Diary</title><style>
body{{margin:0;font-family:-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;background:#faf7f2;color:#2b2d42}}
main{{max-width:560px;margin:0 auto;padding:24px 16px 48px}}h1{{font-size:22px;margin:0 0 4px}}
.meta{{color:#6b7280;font-size:14px;margin-bottom:16px}}img{{width:100%;border-radius:16px;display:block}}
p{{font-size:18px;line-height:1.75;margin:20px 0}}ul{{padding-left:18px;color:#4b5563;line-height:1.8}}
.mood{{display:inline-block;background:#efe9ff;color:#4c3fa8;border-radius:999px;padding:2px 10px;font-size:13px}}
.headline{{font-size:20px;font-weight:700;margin:4px 0 8px}}.dj{{background:#fff4d6;border-radius:12px;padding:10px 12px;margin:0 0 16px;font-size:15px}}
</style></head><body><main><h1>오늘의 Q Diary</h1>
<div class="meta">{esc(d.get("commute", ""))} · {esc(d.get("created_at", "")[:10])} <span class="mood">{esc(label)}</span></div>
{top}{img}<p>{esc(d.get("text", ""))}</p><ul>{items}</ul></main></body></html>'''


async def generate_image(llm, prompt, out_dir: Path, diary_id):
    """Optional AI illustration. Returns a file path or None; never raises."""
    if os.getenv('DIARY_IMAGES', '').lower() != 'openai' or not getattr(llm, 'live', False):
        return None
    try:
        client = llm._client(120.0)
        result = await client.images.generate(model=os.getenv('OPENAI_IMAGE_MODEL', 'gpt-image-1'),
                                              prompt=prompt, size='1024x1024')
        data = base64.b64decode(result.data[0].b64_json)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f'{diary_id}.png'
        path.write_bytes(data)
        return str(path)
    except Exception as e:
        log.warning('diary image failed (%s); keeping the SVG', type(e).__name__)
        return None


def image_prompt(f, mood):
    weather = {'RAIN': '비 오는', 'SNOW': '눈 오는'}.get(f.get('weather'), '맑은')
    return (f"따뜻한 색연필 그림일기 스타일, {weather} 날 {f['commute']} 도로 위를 달리는 작은 자동차 한 대, "
            f"분위기: {MOODS.get(mood, MOODS['CALM'])[0]}. 글자, 번호판, 사람 얼굴 없이.")
