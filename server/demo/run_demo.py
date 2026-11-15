"""ROADPOD 데모: 페르소나 정하은의 출근길(시나리오 A)과 퇴근길(시나리오 B)을 실제 서버 엔진으로 돌려 본다.

    cd server
    .venv/bin/python demo/run_demo.py            # .env 설정대로 (OpenAI 키가 있으면 실제 AI)
    .venv/bin/python demo/run_demo.py --mock     # AI 없이 바로 (고정 문구)
    .venv/bin/python demo/run_demo.py --pace 1   # 발표용: 한 줄씩 천천히
    .venv/bin/python demo/run_demo.py --only B   # 퇴근길만
    .venv/bin/python demo/run_demo.py --sound 8  # 음악 코너 음원을 8초씩 실제로 재생 (파일을 넣었을 때)

차 앱 대신 이 스크립트가 차 역할을 한다: 차량 데이터(context.updated)를 보내고, DJ 말을 "재생"하고(ACK),
하은의 말(STT 결과)을 보낸다. 서버 쪽은 실제 엔진(backend/roadpod) 그대로다.
시계는 시나리오 시간(07:30, 19:10 …)으로 맞춘다. 결과 Q Diary는 demo/output/*.html 로 저장된다.
"""
from __future__ import annotations
import argparse, asyncio, base64, os, random, re, shutil, sqlite3, sys, textwrap, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'demo'
OUT = DEMO / 'output'
CACHE_DB = DEMO / 'episode_cache.sqlite'   # researched episodes survive between demo runs
RUN_DB = DEMO / 'demo_run.sqlite'          # stories, notes, diaries: fresh every run
KST = timezone(timedelta(hours=9))
DAY = datetime(2026, 11, 12, tzinfo=KST)   # a Thursday

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('--mock', action='store_true', help='AI 없이 고정 문구로 (즉시 실행)')
ap.add_argument('--pace', type=float, default=0.0, help='줄 사이 대기 배수 (발표용 1~2)')
ap.add_argument('--only', choices=['A', 'B'], help='한 시나리오만')
ap.add_argument('--no-open', action='store_true', help='끝나고 Q Diary를 브라우저로 열지 않음')
ap.add_argument('--sound', type=float, default=0, metavar='초', help='음악 코너 음원을 실제로 몇 초씩 틀어 줌 (맥, 파일이 있을 때)')
args = ap.parse_args()

if args.mock:
    os.environ['LLM_PROVIDER'] = 'mock'
for f in (RUN_DB, Path(str(RUN_DB) + '-journal')):
    f.unlink(missing_ok=True)
os.environ['ROADPOD_DB'] = str(RUN_DB)
os.environ['PUBLIC_BASE_URL'] = os.environ.get('PUBLIC_BASE_URL', 'http://127.0.0.1:8765')
sys.path.insert(0, str(ROOT / 'backend'))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / '.env')       # does not override what was set above
from roadpod.engine import Director  # noqa: E402
from roadpod import diary as diarylib  # noqa: E402

# ---------------------------------------------------------------- output
TTY = sys.stdout.isatty()
def color(code, s): return f'\033[{code}m{s}\033[0m' if TTY else s
WHO = {'music': ('♪  음악', '1;32'), 'dj': ('🎙  큐큐', '1;36'), 'story': ('💌 사연', '1;35'), 'me': ('🙋 하은', '1;33'), 'car': ('🚗 차량', '2'),
       'sys': ('⚙  서버', '2'), 'scene': ('', '1')}


def say(who, text, clock=None):
    label, code = WHO[who]
    stamp = clock.strftime('%H:%M') if clock else '     '
    if who == 'scene':
        print('\n' + color('1;4', f'{stamp}  {text}'))
        return
    wrapped = textwrap.fill(text, width=78, subsequent_indent=' ' * 17)
    print(f'{color("2", stamp)}  {color(code, label):<{12 + (11 if TTY else 0)}} {wrapped}')
    if args.pace:
        time.sleep(min(6.0, 0.4 + len(text) / 60) * args.pace)


# ---------------------------------------------------------------- the car
class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def set(self, hh, mm): self.t = self.t.replace(hour=hh, minute=mm); return self.t


def context(*, commute, speed=70, stopped=False, parked=False, eta=None, road=None, segment=None,
            jam=False, rain=False, wiper=False, guidance=False):
    return dict(commute_type=commute,
                vehicle={'motion_state': 'PARKED' if parked else 'STOPPED' if stopped else 'MOVING',
                         'speed_kmh': 0 if (stopped or parked) else speed, 'wiper_active': wiper, 'parked_confirmed': parked},
                navigation={'road_name': road, 'road_segment_id': segment, 'eta_seconds': eta, 'guidance_active': guidance},
                traffic={'congestion': 'JAM' if jam else 'FREE', 'verified': True},
                weather={'condition': 'RAIN' if rain else 'CLEAR', 'verified': True},
                source_mode='SIMULATED')


class Car:
    """Plays the role of the Pleos app: sends events, 'speaks' commands, ACKs them."""

    def __init__(self, d: Director, client_id, commute, clock):
        self.d, self.commute, self.clock = d, commute, clock
        self.sid = d.start(client_id, commute)
        self.queue: list[dict] = []
        self.playing: dict | None = None
        self.diary: dict | None = None
        async def pushed(c): self.queue.append(c)
        d.attach(self.sid, pushed)

    async def send(self, typ, **payload):
        out = await self.d.process(self.sid, {'schema_version': '1.0', 'event_id': str(uuid.uuid4()), 'event_type': typ,
                                              'session_id': self.sid, 'timestamp': datetime.now(timezone.utc).isoformat(),
                                              'payload': payload})
        self.queue.extend(out)
        return out

    async def drive(self, **kw):
        return await self.send('context.updated', **context(commute=self.commute, **kw))

    async def speak(self, text):
        say('me', text, self.clock())
        await self.send('user.utterance', text=text, is_final=True, input_mode='STT')

    def show(self, c):
        t, p = c['type'], c['payload']
        if t == 'dj.speak':
            say('dj', p['text'], self.clock())
        elif t == 'story.incoming':
            say('story', p['text'], self.clock())
        elif t == 'media.pause':
            say('car', '⏸  방송 일시정지 (내비게이션 안내 우선)', self.clock())
        elif t == 'media.resume':
            say('car', '▶  방송 재개', self.clock())
        elif t == 'story.preview':
            say('car', f'📝 사연 초안이 화면 대기열에 저장됨 (주차 후 승인): {p["preview_text"]}', self.clock())
        elif t == 'music.play':
            mins, secs = divmod(p['duration_seconds'], 60)
            path = ROOT / 'backend' / 'roadpod' / 'data' / 'music' / p['url'].rsplit('/', 1)[-1]
            note = '' if path.is_file() else '  (음원 파일 없음: 샘플 슬롯)'
            say('music', f'음악 코너 · {p["title"]}{" - " + p["artist"] if p.get("artist") else ""} ({mins}분 {secs:02d}초){note}', self.clock())
            if args.sound and path.is_file() and sys.platform == 'darwin':
                os.system(f'afplay -t {args.sound} "{path}"')
        elif t == 'diary.ready':
            self.diary = p
            say('car', f'📔 Q Diary 완성 → 화면에 QR 표시: {p["view_url"]}', self.clock())

    async def play(self, lines=1, *, until=None, before=None, finish_last=True):
        """Speak up to `lines` commands from the queue, ACKing each (which makes the server send the next).
        until: stop after a command that matches. before: stop before one (it stays queued, i.e. playing next)."""
        spoken = 0
        while self.queue and spoken < lines:
            if before and before(self.queue[0]):
                return None
            c = self.queue.pop(0)
            self.show(c)
            if c['type'] not in ('dj.speak', 'story.incoming', 'music.play'):
                if c['type'] in ('story.preview', 'diary.ready', 'media.pause', 'media.resume'):
                    await self.send('command.ack', command_id=c['command_id'], status='COMPLETED')
                continue
            spoken += 1
            if spoken == lines and not finish_last:
                self.playing = c            # still talking: the next event arrives mid-sentence
                return c
            await self.send('command.ack', command_id=c['command_id'], status='COMPLETED')
            if until and until(c):
                return c
        return None

    async def interrupt(self):
        if self.playing:
            await self.send('command.ack', command_id=self.playing['command_id'], status='INTERRUPTED')
            self.playing = None

    async def finish(self):
        if self.playing:
            c, self.playing = self.playing, None
            await self.send('command.ack', command_id=c['command_id'], status='COMPLETED')


# ---------------------------------------------------------------- setup
def seed_story(d, client, commute, topic_key, category, text, parent=None, relation='INDEPENDENT'):
    """Other listeners' stories that already passed moderation (before the demo starts)."""
    sid = d.start(client, commute)
    story_id = d.store.add_draft(sid, None, text)
    d.stories.add_meta(story_id, client_id=client, session_id=sid, topic_key=topic_key, category=category,
                       commute_type=commute, congestion=None, weather=None, road_segment_id=None, raw_text=text,
                       parent_story_id=parent, relation_type=relation)
    d.stories.set_status(story_id, 'PUBLISHED')
    if parent:
        d.stories.add_relation(parent, story_id, relation)
    d.states.pop(sid, None)
    return story_id


def load_cache(d):
    if not CACHE_DB.exists():
        return
    src = sqlite3.connect(CACHE_DB)
    for table in ('episode_briefs', 'episode_scripts'):
        rows = src.execute(f'SELECT * FROM {table}').fetchall()
        if rows:
            marks = ','.join('?' * len(rows[0]))
            d.store.conn.executemany(f'INSERT OR REPLACE INTO {table} VALUES({marks})', rows)
    d.store.conn.commit(); src.close()


def save_cache(d):
    dst = sqlite3.connect(CACHE_DB)
    dst.execute('CREATE TABLE IF NOT EXISTS episode_briefs(topic_key TEXT PRIMARY KEY, brief_json TEXT NOT NULL, created_at TEXT NOT NULL)')
    dst.execute('CREATE TABLE IF NOT EXISTS episode_scripts(topic_key TEXT NOT NULL, commute_type TEXT NOT NULL, segments_json TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(topic_key, commute_type))')
    for table in ('episode_briefs', 'episode_scripts'):
        rows = d.store.conn.execute(f'SELECT * FROM {table}').fetchall()
        if rows:
            dst.executemany(f'INSERT OR REPLACE INTO {table} VALUES({",".join("?" * len(rows[0]))})', [tuple(r) for r in rows])
    dst.commit(); dst.close()


async def prepare(d, jobs):
    todo = [(d.catalog.get(k), c) for k, c in jobs if d.producer.cached(d.catalog.get(k), c) is None]
    if not todo:
        return
    if d.llm.live:
        say('sys', f'에피소드 리서치·원고 작성 중… (처음 한 번만, 주제 {len(todo)}개 동시에 약 1~2분)')
    await asyncio.gather(*(d.producer.produce(t, c) for t, c in todo))
    save_cache(d)


def save_diary(d, car, name):
    if not car.diary:
        return None
    data = d.stories.diary(car.diary['diary_id'])
    html = diarylib.page(data)
    if data.get('image_path') and Path(data['image_path']).exists():
        src = 'data:image/png;base64,' + base64.b64encode(Path(data['image_path']).read_bytes()).decode()
    else:
        svg = diarylib.svg(data.get('mood'), data.get('commute_type'), data.get('weather'))
        src = 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()
    html = re.sub(r'src="/diaries/[^"]+/image"', f'src="{src}"', html)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f'{name}.html'
    path.write_text(html, encoding='utf-8')
    return path, data


# ---------------------------------------------------------------- scenario A
async def scenario_a(d):
    clock = Clock(DAY.replace(hour=7, minute=30))
    d.clock = clock
    print(color('1;7', '\n  시나리오 A · 평일 출근길 — 무례한 사람을 꼭 내 편으로 만들어야 할까?  '))

    say('scene', '부평, 시동과 함께 방송 시작', clock())
    say('sys', '출근 프로필: 최근 ‘관계와 소통’ 완청, 어제 시사 코너 스킵 → 오늘의 토픽 편성', clock())
    car = Car(d, 'haeun', 'TO_WORK', clock)
    await car.drive(eta=80 * 60, road='경인고속도로', segment='gyeongin-up-03')
    await car.send('session.started', topic_key='relationships')
    await car.play(2)

    say('scene', '토픽 딥다이브', clock.set(7, 40))
    corner = lambda c: c['type'] == 'story.incoming'
    await car.play(20, before=corner)

    say('scene', '사연 코너: 다른 청취자들끼리 오간 이야기', clock.set(7, 55))
    await car.play(3)          # story, reply, OUTRO

    say('scene', '신월 IC 합류: 내비게이션 우선', clock.set(8, 5))
    await car.drive(eta=45 * 60, road='신월IC', segment='sinwol-ic')
    await car.play(1, finish_last=False)
    say('car', '🧭 내비게이션: "300m 앞에서 오른쪽 방향, 올림픽대로 방면입니다"', clock())
    await car.send('navigation.guidance_started')
    await car.interrupt()
    await car.play(1)          # media.pause
    clock.set(8, 6)
    await car.send('navigation.guidance_ended')
    await car.play(3)          # media.resume, the cut sentence again, the next one

    say('scene', '올림픽대로 정체 + 비', clock.set(8, 15))
    await car.play(1, finish_last=False)
    await car.drive(eta=47 * 60, road='올림픽대로', segment='olympic-east-21', jam=True, rain=True, wiper=True, speed=15)
    await car.finish()
    await car.play(2)

    clock.set(8, 32)
    await car.play(1, finish_last=False)
    say('car', '정체 구간을 빠져나옴', clock())
    await car.drive(eta=14 * 60, road='올림픽대로', segment='olympic-east-30', rain=True, wiper=True, speed=60)
    await car.finish()
    await car.play(2)

    say('scene', '강남 도착 직전, 신호 대기 중', clock.set(8, 45))
    await car.play(1, finish_last=False)
    await car.drive(eta=6 * 60, road='테헤란로', segment='teheran-07', stopped=True, rain=True, wiper=True)
    await car.speak('아 근데 운전하면서 생각해 봤는데… 큐큐야, 내 생각 사연으로 남겨 둘래. 나는 무례한 사람을 꼭 내 편으로 '
                    '만들어야 할까? 그런 생각도 들어. 어렵긴 한데 암튼 그래.')
    await car.finish()
    await car.play(1)          # empathy + draft + "보낼까요?" — then the show leaves air for the answer
    await car.speak('응. 보내')
    await car.play(1)          # "큐큐~ …"
    await asyncio.gather(*d._tasks)   # the story's question goes to the topic pool in the background
    say('car', '신호가 바뀌어 다시 출발 · 도착 2분 전', clock.set(8, 47))
    await car.drive(eta=2 * 60, road='테헤란로', segment='teheran-08', speed=30, rain=True, wiper=True)
    await car.play(3)          # arriving: the topic wraps up (OUTRO) and the show says goodbye

    say('scene', '주차: 클로징과 Q Diary', clock.set(8, 50))
    say('car', '목적지 도착 · 기어 P · 5분간 페달 입력 없음 → 주차 확인', clock())
    car.queue.clear()          # nothing left playing once the engine is off
    await car.drive(eta=0, parked=True, rain=True, wiper=False)
    await car.send('vehicle.parked')
    await car.play(3)
    saved = save_diary(d, car, 'A_출근길_Q Diary')

    say('scene', '주차 이후 · 서비스 공통 메모리', clock())
    cand = next((c for c in d.stories.candidates() if c['status'] == 'PENDING'), None)
    pool_key = None
    if cand:
        say('sys', f'하은의 사연 검수 통과 → 원문이 아닌 질문만 뽑아 토픽 후보로: “{cand["topic_text"]}”', clock())
        pool_key = d.adopt_candidate(cand['candidate_id'])
        say('sys', f'운영자 승인 → 공통 토픽 풀에 추가 ({pool_key})', clock())
    return saved, pool_key


async def next_morning(d, pool_key):
    if not pool_key:
        return
    clock = Clock((DAY + timedelta(days=1)).replace(hour=7, minute=50))
    d.clock = clock
    say('scene', '다음 날 · 다른 청취자의 출근길', clock())
    other = Car(d, 'another-driver', 'TO_WORK', clock)
    await other.drive(eta=40 * 60, road='강변북로', segment='gangbyeon-11')
    await other.send('session.started', topic_key=pool_key)
    await other.play(1)


# ---------------------------------------------------------------- scenario B
async def scenario_b(d, morning_story):
    clock = Clock(DAY.replace(hour=19, minute=10))
    d.clock = clock
    print(color('1;7', '\n  시나리오 B · 평일 퇴근길 — 나는 왜 퇴근하고도 회사 생각을 멈추지 못할까?  '))

    say('scene', '강남, 시동과 함께 퇴근 방송 시작', clock())
    say('sys', '퇴근 프로필: 차분한 진행(CALM 목소리), 회고형 토픽 · 개인 메모리 사용 동의', clock())
    car = Car(d, 'haeun', 'FROM_WORK', clock)
    await car.send('user.action', action='set_memory_consent', enabled=True)
    await car.drive(eta=80 * 60, road='테헤란로', segment='teheran-07')
    await car.send('session.started', topic_key='work_off')
    await car.play(1)

    say('scene', '아침 사연에 답장이 도착', clock.set(19, 15))
    await car.play(2)

    say('scene', '토픽 딥다이브 (+ 책 한 권 코너: 리서치가 실제 책을 확인했을 때만)', clock.set(19, 25))
    corner = lambda c: c['type'] == 'story.incoming'
    await car.play(20, before=corner)

    say('scene', '사연 코너: 퇴근길 청취자들의 이야기', clock.set(19, 50))
    await car.play(3)

    say('scene', '경인고속도로 정체: 도로 위 마이크 드롭', clock.set(20, 0))
    await car.send('user.action', action='now_playing', track='밤편지 - 아이유')
    await car.play(1, finish_last=False)
    say('car', '♪ 지금 차에서 나오는 곡: 밤편지 - 아이유', clock())
    await car.drive(eta=40 * 60, road='경인고속도로', segment='gyeongin-down-12', jam=True, speed=12)
    await car.finish()
    await car.play(1, finish_last=False)   # the traffic line is playing…
    await car.speak('큐큐야, 이 노래 드랍해줘.')
    await car.finish()
    await car.play(2)

    say('scene', '정체로 멈춰 있고 내비게이션 안내 없음: 퇴근길 회고 질문', clock.set(20, 15))
    await car.play(1, finish_last=False)
    await car.drive(eta=20 * 60, road='경인고속도로', segment='gyeongin-down-14', jam=True, stopped=True)
    await car.finish()
    await car.play(1)          # the question — then air for the answer
    await car.speak('아침에 사연 보낸 거. 그거 생각보다 좋았어.')
    await car.play(2)

    say('scene', '귀가 및 주차: Q Diary', clock.set(20, 28))
    await car.drive(eta=2 * 60, road='부평대로', segment='bupyeong-02', speed=30)
    await car.play(3)
    clock.set(20, 30)
    car.queue.clear()
    await car.drive(eta=0, parked=True)
    await car.send('vehicle.parked')
    await car.play(3)
    saved = save_diary(d, car, 'B_퇴근길_Q Diary')

    say('scene', '그 후 · 비슷한 퇴근길 정체를 지나는 다른 운전자', clock.set(20, 40))
    other = Car(d, 'evening-driver', 'FROM_WORK', clock)
    await other.drive(eta=50 * 60, road='경인고속도로', segment='gyeongin-down-03')
    await other.send('session.started', topic_key='rest')
    await other.play(1, finish_last=False)
    await other.drive(eta=48 * 60, road='경인고속도로', segment='gyeongin-down-12', jam=True, speed=10)
    await other.finish()
    await other.play(2)
    return saved


# ---------------------------------------------------------------- main
async def main():
    d = Director(rng=random.Random(7))
    load_cache(d)
    d.answer_wait = 30   # the demo answers at once; never let the window close on its own
    mode = f'실제 AI ({d.llm.writer_model})' if d.llm.live else 'AI 없이 고정 문구 (mock)'
    print(color('1', f'ROADPOD 데모 · 페르소나 정하은 · {mode}'))
    await prepare(d, [('relationships', 'TO_WORK'), ('work_off', 'FROM_WORK'), ('rest', 'FROM_WORK')])

    # Stories other listeners already exchanged (scenario wording).
    seed_root = seed_story(d, 'listener-01', 'TO_WORK', 'relationships', 'RELATION', '팀장님 말투 때문에 회의만 하면 작아져요.')
    seed_story(d, 'listener-02', 'TO_WORK', 'relationships', 'RELATION',
               '저는 그 말투를 그 사람의 습관이라고 생각하기로 했어요. 나한테 하는 말이 아니라고 생각하려고 노력해요.',
               parent=seed_root, relation='FOLLOW_UP')

    results = []
    morning_story = None
    if args.only in (None, 'A'):
        saved, pool_key = await scenario_a(d)
        results.append(saved)
        row = d.store.conn.execute("SELECT d.story_id FROM story_drafts d JOIN story_meta m USING(story_id) "
                                   "WHERE m.client_id='haeun' AND d.status='PUBLISHED'").fetchone()
        morning_story = row['story_id'] if row else None
        await next_morning(d, pool_key)
    if args.only in (None, 'B'):
        clock = Clock(DAY.replace(hour=12, minute=0)); d.clock = clock
        if morning_story is None:   # --only B: Haeun's morning story as if scenario A had run
            morning_story = seed_story(d, 'haeun', 'TO_WORK', 'relationships', 'RELATION',
                                       '무례한 사람을 꼭 내 편으로 만들어야 할까요? 내 편보다 중요한 건 우선 내 마음이 아닐까요?')
        say('scene', '낮 동안 · 하은의 아침 사연에 다른 운전자들이 답장 (검수 통과)', clock())
        for who, words, rel in (('listener-05', '저도 요즘 모두와 잘 지낼 필요는 없다는 걸 배우는 중이에요.', 'EMPATHY'),
                                ('listener-06', '내 편까진 아니어도, 적이 아니면 충분하대요.', 'FOLLOW_UP')):
            seed_story(d, who, 'TO_WORK', 'relationships', 'RELATION', words, parent=morning_story, relation=rel)
        say('scene', '퇴근 시간대 · 다른 운전자들 사이에 오간 사연 (검수 통과)', clock.set(18, 50))
        evening = seed_story(d, 'listener-03', 'FROM_WORK', 'communication', 'RELATION', '오늘 처음으로 회의에서 내 의견을 끝까지 말했어요.')
        seed_story(d, 'listener-04', 'FROM_WORK', 'communication', 'RELATION', '그 한 번이 제일 어려운 거 다들 알 거예요. 멋져요!',
                   parent=evening, relation='EMPATHY')
        results.append(await scenario_b(d, morning_story))

    save_cache(d)
    print(color('1', '\nQ Diary'))
    for item in results:
        if item:
            path, data = item
            print(f'  {path}')
            print(f'    오늘의 한 줄: {data.get("headline")}')
            print(f'    큐큐\'s 코멘트: {data.get("dj_comment")}')
    if results and results[-1] and not args.no_open and sys.platform == 'darwin':
        os.system(f'open "{results[-1][0]}"')


asyncio.run(main())
