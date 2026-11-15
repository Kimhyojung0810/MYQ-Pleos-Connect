"""ROADPOD prototype orchestration. Not a vehicle safety-certified controller."""
from __future__ import annotations
import asyncio, json, logging, os, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .llm import TextProvider  # re-exported: tests and callers import TextProvider from here
from .producer import EpisodeProducer, catalog_script
from .openings import opening_line, hook_line, bridge_line
from .topics import Catalog, Topic, TopicSelector
from . import diary as diarylib, drive, relay
from . import music as musiclib
from .voice import Voice
from .relay import StoryBook

log = logging.getLogger('roadpod')

TERMINAL = ('COMPLETED', 'FAILED', 'INTERRUPTED')
PLAY_GRACE_SECONDS = 30  # plus ~1 s per 3 characters of speech before an unACKed command is presumed lost


def _body_wait_seconds():
    """BODY_WAIT_SECONDS: how long an episode waits for its researched body before airing the catalog script.
    Hook + bridge take ~25-30 s to speak, so 25 s keeps the car from going silent on a cold cache."""
    try:
        return max(0.0, float(os.getenv('BODY_WAIT_SECONDS') or 25))
    except ValueError:
        return 25.0


class ProtocolError(ValueError):
    """Client sent something the server rejects; reported as protocol.error, connection stays open."""
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix): return prefix + '-' + uuid.uuid4().hex[:12]


class MemoryStore:
    def __init__(self, path=':memory:'):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, commute_type TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS commute_profiles(client_id TEXT NOT NULL, commute_type TEXT NOT NULL, topics_json TEXT NOT NULL DEFAULT '[]', music_json TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(client_id,commute_type));
        CREATE TABLE IF NOT EXISTS episodes(episode_id TEXT PRIMARY KEY, topic_key TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS episode_segments(episode_id TEXT NOT NULL, idx INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, source_refs TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(episode_id,idx));
        CREATE TABLE IF NOT EXISTS story_drafts(story_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, episode_id TEXT, preview_text TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','PUBLISHED','REJECTED','BLOCKED','REVOKED')), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS story_relations(relation_id TEXT PRIMARY KEY, parent_story_id TEXT NOT NULL, child_story_id TEXT NOT NULL, relation_type TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS topic_candidates(candidate_id TEXT PRIMARY KEY, from_story_id TEXT, topic_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING');
        CREATE TABLE IF NOT EXISTS conversation_memories(client_id TEXT NOT NULL, commute_type TEXT NOT NULL, summary TEXT NOT NULL, consented INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(client_id,commute_type));
        CREATE TABLE IF NOT EXISTS processed_events(session_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(session_id,event_id));
        CREATE TABLE IF NOT EXISTS command_log(command_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS note_drops(note_id TEXT PRIMARY KEY, road_segment_id TEXT, time_bucket TEXT, traffic_bucket TEXT, music_track_id TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS diary_cards(diary_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL);
        -- Listening memory, split by commute type (출근·퇴근 분리).
        CREATE TABLE IF NOT EXISTS topic_history(episode_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, commute_type TEXT NOT NULL, topic_key TEXT NOT NULL, category TEXT NOT NULL, started_at TEXT NOT NULL, segments_heard INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0, engaged INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS topic_history_client ON topic_history(client_id, commute_type, started_at);
        CREATE TABLE IF NOT EXISTS listener_interests(client_id TEXT PRIMARY KEY, categories_json TEXT NOT NULL, updated_at TEXT NOT NULL);
        -- Shared content cache: research brief per topic, script per topic + commute type.
        CREATE TABLE IF NOT EXISTS episode_briefs(topic_key TEXT PRIMARY KEY, brief_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS episode_scripts(topic_key TEXT NOT NULL, commute_type TEXT NOT NULL, segments_json TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(topic_key, commute_type));
        ''')
        self.conn.commit()
        relay.ensure_tables(self.conn)   # stories, music notes, diaries

    def session(self, sid, client_id, commute):
        self.conn.execute('INSERT INTO sessions VALUES (?,?,?,?)', (sid, client_id, commute, now()))
        self.conn.execute('INSERT OR IGNORE INTO commute_profiles(client_id,commute_type) VALUES(?,?)', (client_id, commute))
        self.conn.commit()

    def dedup(self, sid, event_id):
        try:
            self.conn.execute('INSERT INTO processed_events VALUES (?,?)', (sid, event_id)); self.conn.commit(); return False
        except sqlite3.IntegrityError:
            return True

    def add_draft(self, sid, episode_id, text):
        story_id = uid('story')
        self.conn.execute('INSERT INTO story_drafts VALUES (?,?,?,?,?,?)', (story_id, sid, episode_id, text, 'DRAFT', now()))
        self.conn.commit(); return story_id

    def approve(self, story_id, sid, approved):
        row = self.conn.execute('SELECT * FROM story_drafts WHERE story_id=?', (story_id,)).fetchone()
        if not row or row['session_id'] != sid: raise PermissionError('NOT_OWNER')
        if row['status'] != 'DRAFT': raise ValueError('NOT_DRAFT')
        status = 'APPROVED' if approved else 'REJECTED'
        self.conn.execute('UPDATE story_drafts SET status=? WHERE story_id=?', (status, story_id)); self.conn.commit()
        return dict(row), status

    # --- episodes ---
    def add_episode(self, episode_id, topic: Topic):
        self.conn.execute('INSERT INTO episodes VALUES(?,?,?,?)', (episode_id, topic.key, topic.question, now())); self.conn.commit()

    def add_segments(self, episode_id, start_idx, segments):
        self.conn.executemany('INSERT INTO episode_segments VALUES(?,?,?,?,?)',
                              [(episode_id, start_idx + i, s['role'], s['text'], json.dumps(s.get('source_refs', [])))
                               for i, s in enumerate(segments)])
        self.conn.commit()

    def segment_after(self, episode_id, idx):
        return self.conn.execute('SELECT idx,role,text FROM episode_segments WHERE episode_id=? AND idx>? ORDER BY idx LIMIT 1',
                                 (episode_id, idx)).fetchone()

    def segment(self, episode_id, idx):
        return self.conn.execute('SELECT idx,role,text FROM episode_segments WHERE episode_id=? AND idx=?', (episode_id, idx)).fetchone()

    # --- listening memory ---
    def start_topic(self, episode_id, client_id, commute_type, topic: Topic):
        self.conn.execute('INSERT INTO topic_history(episode_id,client_id,commute_type,topic_key,category,started_at) VALUES(?,?,?,?,?,?)',
                          (episode_id, client_id, commute_type, topic.key, topic.category, now())); self.conn.commit()

    def mark_heard(self, episode_id, count):
        self.conn.execute('UPDATE topic_history SET segments_heard=MAX(segments_heard,?) WHERE episode_id=?', (count, episode_id)); self.conn.commit()

    def mark_completed(self, episode_id):
        self.conn.execute('UPDATE topic_history SET completed=1 WHERE episode_id=?', (episode_id,)); self.conn.commit()

    def mark_engaged(self, episode_id):
        self.conn.execute('UPDATE topic_history SET engaged=1 WHERE episode_id=?', (episode_id,)); self.conn.commit()

    def mark_skipped(self, episode_id):
        self.conn.execute('UPDATE topic_history SET skipped=1 WHERE episode_id=? AND completed=0', (episode_id,)); self.conn.commit()

    def topic_history(self, client_id, commute_type):
        return self.conn.execute('SELECT * FROM topic_history WHERE client_id=? AND commute_type=? ORDER BY started_at',
                                 (client_id, commute_type)).fetchall()

    def recent_topic_keys(self, client_id, days):
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return [r['topic_key'] for r in self.conn.execute(
            'SELECT DISTINCT topic_key FROM topic_history WHERE client_id=? AND started_at>=?', (client_id, since))]

    def category_counts(self, client_id, commute_type):
        return {r['category']: r['n'] for r in self.conn.execute(
            'SELECT category, COUNT(*) n FROM topic_history WHERE client_id=? AND commute_type=? GROUP BY category',
            (client_id, commute_type))}

    def set_interests(self, client_id, categories):
        self.conn.execute('INSERT INTO listener_interests VALUES(?,?,?) ON CONFLICT(client_id) DO UPDATE SET categories_json=excluded.categories_json, updated_at=excluded.updated_at',
                          (client_id, json.dumps(sorted(set(categories))), now())); self.conn.commit()

    def get_interests(self, client_id):
        row = self.conn.execute('SELECT categories_json FROM listener_interests WHERE client_id=?', (client_id,)).fetchone()
        return json.loads(row['categories_json']) if row else []

    # --- content cache ---
    def get_brief(self, topic_key):
        row = self.conn.execute('SELECT * FROM episode_briefs WHERE topic_key=?', (topic_key,)).fetchone()
        return {'brief': json.loads(row['brief_json']), 'created_at': row['created_at']} if row else None

    def save_brief(self, topic_key, brief):
        self.conn.execute('INSERT OR REPLACE INTO episode_briefs VALUES(?,?,?)', (topic_key, json.dumps(brief, ensure_ascii=False), now())); self.conn.commit()

    def get_script(self, topic_key, commute_type):
        row = self.conn.execute('SELECT * FROM episode_scripts WHERE topic_key=? AND commute_type=?', (topic_key, commute_type)).fetchone()
        return {'segments': json.loads(row['segments_json']), 'mode': row['mode'], 'created_at': row['created_at']} if row else None

    def save_script(self, topic_key, commute_type, segments, mode):
        self.conn.execute('INSERT OR REPLACE INTO episode_scripts VALUES(?,?,?,?,?)',
                          (topic_key, commute_type, json.dumps(segments, ensure_ascii=False), mode, now())); self.conn.commit()


SYSTEM = '''너는 차량용 팟캐스트 ROADPOD의 DJ 큐큐다. 운전 중인 청취자가 방송 도중 의견을 말했다.
- 한국어 1~2문장, 120자 안팎으로 답한다. 자기소개나 인사로 시작하지 않는다.
- 청취자의 말을 짧게 받아 주고 오늘의 질문(episode_question)과 이어 준 뒤, 방송으로 돌아갈 수 있게 마무리한다. 질문 문장을 그대로 되풀이하지 않는다.
- 더 자세히 말해 달라거나 구체적인 경험을 공유해 달라고 요구하지 않는다. 운전 중에는 말하는 것도 부담이다.
- 질문은 꼭 필요할 때만, 짧게 답할 수 있는 것 하나만 한다. 대답하지 않아도 괜찮다는 느낌을 준다.
- 감정에 무조건 동조하지 않는다. 다른 관점이 있으면 한마디로만 보여 준다.
- 검증되지 않은 사실, 연구결과, 책 인용, 도로명, 교통정보를 꾸미지 않는다. 화면을 보거나 조작하라고 하지 않는다.
- 대화 원문은 외부에 공개하지 않는다. 사연은 별도 초안 미리보기와 개별 승인을 거친다.'''


@dataclass
class SessionState:
    session_id: str
    client_id: str
    commute_type: str
    episode_id: str | None = None
    topic: Topic | None = None
    segment_idx: int = -1              # last segment actually sent to the car
    content_ready: bool = False        # researched/written body installed for this episode
    waiting: bool = False              # hook finished before the body was ready
    bridged: bool = False              # already filled one gap with the topic intro line
    opened: bool = False               # the show's opening greeting has been said
    next_topic: Topic | None = None    # picked and produced in advance so the next episode starts without a gap
    segment_cmds: dict = field(default_factory=dict)  # command_id -> (episode_id, idx)
    replay: tuple | None = None        # segment cut off by nav/user; replay when free
    context: dict = field(default_factory=dict)
    nav_active: bool = False
    parked: bool = False
    playing_command_id: str | None = None
    playing_deadline: datetime | None = None  # give up on an ACK that never comes (lost packet, buggy client)
    pending: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    seen_navigation: bool = False
    # Story Catch / Relay
    story_parts: list[str] = field(default_factory=list)   # what the driver said for the story being caught
    story_mode: bool = False           # next final utterance is a story (request_story)
    story_followup: bool = False       # follow-up question asked; next utterance completes the story
    story_seq: int = 0                 # bumps per caught story so a stale follow-up timer does nothing
    reply_to: str | None = None        # story just heard here; a new story becomes its 맞사연
    inbox: list[dict] = field(default_factory=list)         # {id, text, check, reply_target}: air at next boundary
    slot_used: bool = False            # STORY_SLOT before OUTRO used for this episode
    # Driving context
    last_comment: dict = field(default_factory=dict)        # kind -> epoch seconds
    wrap_up: bool = False              # arrival close: finish this topic, start no new one
    signed_off: bool = False
    awaiting_approval: str | None = None   # story asked "보낼까요?" by voice while stopped
    awaiting_text: str = ''
    arrival_at: float | None = None        # first ETA → expected arrival (scenario clock); delay = later ETA minus this
    now_playing: str | None = None         # song on the car's audio, reported by the app ("이 노래 드랍해줘")
    reflection_asked: bool = False
    awaiting_reflection: bool = False
    reflection: str | None = None
    memory_consent: bool = False           # 개인 메모리: store reflections only when the driver turned it on
    body_since_music: int = 0              # spoken body segments since the last music break
    music_in_episode: int = 0
    played_tracks: set = field(default_factory=set)
    # Diary
    said: list[str] = field(default_factory=list)
    topics_heard: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    diary_id: str | None = None

    @property
    def topic_key(self):
        return self.topic.key if self.topic else None


class Director:
    def __init__(self, store=None, provider=None, catalog=None, rng=None):
        # ROADPOD_DB=path/to/roadpod.sqlite keeps listening memory and produced episodes across restarts.
        self.store = store or MemoryStore(os.getenv('ROADPOD_DB') or ':memory:')
        self.llm = provider or TextProvider()
        self.catalog = catalog or Catalog()
        self.selector = TopicSelector(self.catalog, self.store, rng=rng)
        self.producer = EpisodeProducer(self.store, self.llm)
        self.states: dict[str, SessionState] = {}
        self.senders: dict = {}   # session_id -> async send(command); lets background work reach the car
        self._tasks: set = set()
        self.body_wait = _body_wait_seconds()
        self.stories = StoryBook(self.store.conn)
        # The car turns this into a QR code; the phone must be able to reach it.
        self.public_base = (os.getenv('PUBLIC_BASE_URL') or 'http://127.0.0.1:8765').rstrip('/')
        self.followup_seconds = float(os.getenv('STORY_FOLLOWUP_SECONDS') or 45)
        self.answer_wait = float(os.getenv('ANSWER_WAIT_SECONDS') or 10)   # silence left for "보낼까요?" and similar
        db = os.getenv('ROADPOD_DB')
        data_dir = Path(db).resolve().parent if db and db != ':memory:' else Path.cwd()
        self.diary_dir = data_dir / 'diary_images'
        self.voice = Voice(data_dir / 'tts_cache', self.public_base)   # 큐큐 voice (OpenAI TTS), off unless TTS_PROVIDER=openai
        self.clock = None   # demo/tests: a function returning an aware datetime; None = real time
        self.tracks = musiclib.load()            # 음악 코너 (C): royalty-free tracks the server airs itself
        self.music_every = musiclib.every()
        for row in self.stories.pool_topics():   # topics grown from stories, approved earlier
            self.catalog.add(self._pool_topic(row))

    def now(self):
        return self.clock() if self.clock else datetime.now(timezone.utc)

    @staticmethod
    def _pool_topic(row):
        return Topic(row['topic_key'], row['category'], row['question'], row['summary'], tuple(json.loads(row['angles_json'])),
                     json.loads(row['commute_fit_json']), 5, False, 'normal', row['question'])

    # --- sessions & transport ---
    def start(self, client_id, commute_type):
        sid = uid('trip'); self.store.session(sid, client_id, commute_type)
        self.states[sid] = SessionState(sid, client_id, commute_type)
        return sid

    def attach(self, sid, send):
        self.senders[sid] = send

    def detach(self, sid):
        self.senders.pop(sid, None)

    async def outgoing(self, c):
        """Last step before a command goes to the car: spoken lines get the server-made voice (audio_url)."""
        if c.get('type') in ('dj.speak', 'story.incoming') and self.voice.enabled:
            p = c['payload']
            s = self.states.get(c.get('session_id'))
            style = p.get('voice_style') or (self._style(s) if s else None)
            url = await self.voice.url(p.get('text', ''), style, story=c['type'] == 'story.incoming')
            if url:
                p['audio_url'] = url
        return c

    def _prefetch(self, s, c):
        if c.get('type') in ('dj.speak', 'story.incoming'):
            p = c['payload']
            self.voice.prefetch(p.get('text', ''), p.get('voice_style') or self._style(s), story=c['type'] == 'story.incoming')

    async def _push(self, sid, commands):
        send = self.senders.get(sid)
        if not send:
            return
        for c in commands:
            try:
                await send(await self.outgoing(c))
            except Exception:
                log.info('push to %s failed (client likely disconnected)', sid)
                return

    def cmd(self, session_id, typ, payload, ttl=30):
        issued = datetime.now(timezone.utc)
        command = {'schema_version': '1.0', 'command_id': uid('cmd'), 'session_id': session_id, 'type': typ,
                   'issued_at': issued.isoformat(), 'expires_at': (issued + timedelta(seconds=ttl)).isoformat(), 'payload': payload}
        self.store.conn.execute('INSERT INTO command_log VALUES(?,?,?)', (command['command_id'], session_id, 'PENDING')); self.store.conn.commit()
        return command

    # --- speaking ---
    def speak(self, s, text, *, segment=None, ttl=30, priority=False, listen_after=False):
        """listen_after: the line asks the driver something; the car opens the mic when it ends."""
        if s.nav_active or s.parked:
            return []
        payload = {'text': text[:480], 'voice_style': self._style(s), 'interrupt_policy': 'DROP_IF_NAV'}
        if listen_after:
            payload['listen_after'] = True
        c = self.cmd(s.session_id, 'dj.speak', payload, ttl=ttl)
        if segment:
            s.segment_cmds[c['command_id']] = segment
        return self._queue(s, c, priority)

    @staticmethod
    def _style(s):
        """출퇴근별 DJ 분위기: a brighter voice to start the day, a calmer one on the way home."""
        return {'TO_WORK': 'BRIGHT', 'FROM_WORK': 'CALM'}.get(s.commute_type, 'WARM')

    def _queue(self, s, c, priority=False):
        # At most one active TTS command. Wait for COMPLETED/INTERRUPTED ACK.
        if s.playing_command_id:
            if priority:
                s.pending.insert(0, c)   # right after the current segment, before queued talk
            else:
                s.pending.append(c)
            self._prefetch(s, c)          # its voice is made while the current line plays
            return []
        self._playing(s, c)
        return [c]

    def air_story(self, s, story_id, text, ttl=180, comment=None):
        """story.incoming goes through the same one-at-a-time slot as dj.speak (the car speaks it and ACKs).
        comment: 큐큐's own line to say right after a reply."""
        if s.nav_active or s.parked:
            if not any(x['id'] == story_id for x in s.inbox):   # hold, never drop: retried at the next boundary
                s.inbox.append({'id': story_id, 'text': text, 'check': False, 'reply_target': False, 'comment': comment})
            return []
        self.stories.mark_delivered(story_id, s.client_id, s.session_id)
        c = self.cmd(s.session_id, 'story.incoming', {'story_id': story_id, 'text': text[:500]}, ttl=ttl)
        out = self._queue(s, c)
        if comment:   # queued behind the story, so it plays once the car has read the reply
            out = out + self.speak(s, comment)
        return out

    def _comment_on(self, item, kind, story_text, reply_text):
        """Attach 큐큐's comment to a reply in the inbox: the fallback now, the LLM line when it is ready."""
        item['comment'] = relay.REPLY_COMMENT_FALLBACK[kind]
        if not getattr(self.llm, 'live', False):
            return item
        async def make():
            try:
                text = await self.llm.generate(relay.REPLY_COMMENT_SYSTEM, f'사연: {story_text}\n답장: {reply_text}', item['comment'])
                if text and text.strip():
                    item['comment'] = text.strip()[:200]
            except Exception:
                log.exception('reply comment failed')
        try:
            asyncio.get_running_loop().create_task(make())
        except RuntimeError:
            pass
        return item

    def _playing(self, s, c):
        s.playing_command_id = c['command_id']
        # Not expires_at: that is a start-by deadline (30 s), while a long segment plays for ~80 s.
        spoken = len(c['payload'].get('text', ''))
        length = c['payload'].get('duration_seconds') or spoken / 3   # music plays for its duration
        s.playing_deadline = datetime.now(timezone.utc) + timedelta(seconds=PLAY_GRACE_SECONDS + length)
        seg = s.segment_cmds.get(c['command_id'])
        if seg and seg[0] == s.episode_id:
            s.segment_idx = seg[1]
            nxt = self.store.segment_after(s.episode_id, seg[1])   # make the next segment's voice ahead of time
            if nxt and self.voice.enabled:
                self.voice.prefetch(nxt['text'], self._style(s))

    def _expire_lost_command(self, s):
        """True if the playing command's ACK never arrived in time and the slot was freed."""
        if not s.playing_command_id or s.nav_active or s.playing_deadline is None:
            return False
        if datetime.now(timezone.utc) < s.playing_deadline:
            return False
        log.warning('command %s got no ACK before its deadline; moving on', s.playing_command_id)
        s.segment_cmds.pop(s.playing_command_id, None)
        s.playing_command_id = s.playing_deadline = None
        return True

    def _idle(self, s):
        return not (s.playing_command_id or s.pending or s.nav_active or s.parked)

    def _eta(self, s):
        eta = (s.context.get('navigation') or {}).get('eta_seconds')
        return eta if isinstance(eta, (int, float)) and eta >= 0 else None

    # --- episodes ---
    async def start_episode(self, s, topic: Topic, intro=''):
        if s.episode_id:
            self.store.mark_skipped(s.episode_id)  # no-op if it was completed
        # Anything still queued (segments or a reply) belongs to the old topic.
        for c in s.pending:
            s.segment_cmds.pop(c['command_id'], None)
        s.pending.clear()
        eid = uid('episode')
        s.episode_id, s.topic, s.segment_idx = eid, topic, -1
        s.content_ready, s.waiting, s.replay, s.bridged = False, False, None, False
        s.slot_used, s.reply_to = False, None
        s.body_since_music, s.music_in_episode = 0, 0
        if topic.question not in s.topics_heard:
            s.topics_heard.append(topic.question)
        self.store.add_episode(eid, topic)
        self.store.start_topic(eid, s.client_id, s.commute_type, topic)
        # The hook comes from templates so the car hears something immediately, before research finishes.
        # The very first one also opens the show, like a radio DJ would, before getting to the question.
        rng = self.selector.rng
        if not s.opened:
            s.opened = True
            # No weather in the opening: it is briefed only when it matters on the road (driving comments).
            opening = opening_line(s.commute_type, rng, None, now=self.now()) + ' '
        else:
            opening = intro
        self.store.add_segments(eid, 0, [{'role': 'HOOK', 'text': opening + hook_line(topic.question, rng), 'source_refs': []}])
        segments = self.producer.cached(topic, s.commute_type)
        if segments is None and self.producer.instant:
            segments = await self.producer.produce(topic, s.commute_type)
        if segments is not None:
            self._install(s, eid, segments)
        else:
            self._background(self._fetch_body(s.session_id, eid, topic))
        self._prefetch_next(s)
        return await self.advance(s)

    def _background(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task); task.add_done_callback(self._tasks.discard)

    def _prefetch_next(self, s):
        """Choose the next topic now and produce it in the background, so auto-advance and skip have no gap."""
        try:
            s.next_topic = self._pick(s, exclude={s.topic_key})
        except Exception:
            log.exception('prefetch pick failed'); s.next_topic = None; return
        if not self.producer.instant and self.producer.cached(s.next_topic, s.commute_type) is None:
            self._background(self._produce_quietly(s.next_topic, s.commute_type))

    async def _produce_quietly(self, topic, commute_type):
        try:
            await self.producer.produce(topic, commute_type)
        except Exception:
            log.exception('prefetch production failed for %s', topic.key)

    def _take_next(self, s):
        nxt, s.next_topic = s.next_topic, None
        if nxt and nxt.key != s.topic_key:
            return nxt
        return self._pick(s, exclude={s.topic_key})

    def _install(self, s, eid, segments):
        if s.episode_id != eid:   # listener already moved on
            return False
        self.store.add_segments(eid, 1, segments)
        s.content_ready = True
        return True

    async def _fetch_body(self, sid, eid, topic):
        try:
            s = self.states.get(sid)
            if not s:
                return
            try:
                # produce() shields its job: on timeout research keeps running and is cached for the next listener.
                segments = await asyncio.wait_for(self.producer.produce(topic, s.commute_type), self.body_wait)
            except asyncio.TimeoutError:
                log.info('body for %s not ready after %.0fs; airing catalog script', topic.key, self.body_wait)
                segments = catalog_script(topic)
                s = self.states.get(sid)
                if s and s.bridged:   # the bridge already said the summary and "let's look at it from several sides"
                    segments = [seg for seg in segments if seg['role'] != 'FRAME']
            s = self.states.get(sid)
            if not s or not self._install(s, eid, segments):
                return
            if s.waiting and self._idle(s):
                await self._push(sid, await self.advance(s))
        except Exception:
            log.exception('background episode body failed for %s/%s', sid, eid)

    async def advance(self, s):
        """Send the next segment; at the end of an episode, move on to the next topic like a radio would.
        Between segments: relayed stories first; before the OUTRO: one matching story (STORY_SLOT);
        short on time (ETA): jump to the OUTRO; at the end, close the show instead of starting a topic."""
        if s.signed_off:
            return []
        if s.segment_idx >= 0 or not s.episode_id:   # never before an episode's opening line
            boundary = self._from_inbox(s)
            if boundary is not None:
                return boundary
        if not s.episode_id:
            return []
        row = self.store.segment_after(s.episode_id, s.segment_idx)
        if row:
            s.waiting = False
            if s.content_ready and row['role'] != 'OUTRO':
                row = self._fit_to_eta(s, row)
            if row['role'] == 'OUTRO' and not s.slot_used:
                s.slot_used = True
                slot = self._story_slot(s)
                if slot:
                    return slot   # its ACK brings us back here, and the OUTRO airs
            if self._music_due(s, row):
                return self._music_break(s)   # its ACK brings us back here, and this segment airs
            if row['role'] in ('FRAME', 'PERSPECTIVE', 'BOOK'):
                s.body_since_music += 1
            return self.speak(s, row['text'], segment=(s.episode_id, row['idx']))
        if not s.content_ready:
            s.waiting = True   # body still being researched/written; _fetch_body resumes
            if not s.bridged and s.topic:
                # Fill the first gap with the catalog intro instead of silence (not a segment: never replayed).
                s.bridged = True
                return self.speak(s, bridge_line(s.topic.summary, self.selector.rng))
            return []
        self.store.mark_completed(s.episode_id)
        eta = self._eta(s)
        if s.wrap_up or (eta is not None and eta <= drive.ARRIVAL_WRAP_SECONDS):
            s.signed_off = True   # arriving: say goodbye rather than open a topic we cannot finish
            return self.speak(s, drive.sign_off(self.selector.rng))
        return await self.start_episode(s, self._take_next(s), intro='다음 이야기로 넘어가 볼게요. ')

    # --- 음악 코너 (C) ---
    def _music_due(self, s, row):
        return (self.tracks and self.music_every and s.content_ready and not s.wrap_up
                and row['role'] in ('PERSPECTIVE', 'BOOK')          # between corners, never right before the OUTRO
                and s.body_since_music >= self.music_every and s.music_in_episode < musiclib.MAX_PER_EPISODE
                and not (s.awaiting_approval or s.awaiting_reflection or s.story_mode or s.story_followup))

    def _music_break(self, s):
        track = musiclib.pick(self.tracks, s.commute_type, s.played_tracks, self.selector.rng)
        if not track:
            return []
        s.body_since_music, s.music_in_episode = 0, s.music_in_episode + 1
        s.played_tracks.add(track.id)
        s.now_playing = track.label   # so "이 노래 드랍해줘" works for what the show itself played
        intro = self.speak(s, musiclib.intro(track, s.commute_type, self.selector.rng))
        play = self.cmd(s.session_id, 'music.play', musiclib.payload(track, self.public_base),
                        ttl=track.duration_seconds + 120)
        s.pending.insert(0 if intro else 1, play)   # right after the intro line
        return intro

    def _fit_to_eta(self, s, row):
        """기능 ① ETA 기반 길이 조정: if the rest of the episode cannot fit before arrival, go to its OUTRO."""
        eta = self._eta(s)
        if not s.wrap_up and eta is None:
            return row
        rows = self.store.conn.execute('SELECT idx,role,text FROM episode_segments WHERE episode_id=? AND idx>=? ORDER BY idx',
                                       (s.episode_id, row['idx'])).fetchall()
        remaining = sum(drive.speech_seconds(r['text']) for r in rows)
        if s.wrap_up or remaining > eta + 30:
            outro = next((r for r in reversed(rows) if r['role'] == 'OUTRO'), None)
            if outro is not None and outro['idx'] != row['idx']:
                log.info('ETA %ss < %.0fs left in %s; jumping to OUTRO', eta, remaining, s.topic_key)
                return outro
        return row

    # --- Story Relay: airing ---
    def _from_inbox(self, s):
        while s.inbox:
            item = s.inbox.pop(0)
            if item.get('check', True) and self.stories.delivered(item['id'], s.client_id):
                continue
            if item.get('reply_target', True):
                s.reply_to = item['id']
            return self.air_story(s, item['id'], item['text'], comment=item.get('comment'))
        return None

    def _story_slot(self, s):
        if not s.topic:
            return None
        for row in self.stories.candidates_for_slot(s.client_id, s.topic.key, s.topic.category, 1, s.commute_type):
            reply = self.stories.first_reply(row['story_id'], s.client_id)
            if reply and not self.stories.delivered(reply['story_id'], s.client_id):
                # Story corner: the story, then the answer another driver sent to it.
                s.inbox.insert(0, self._comment_on({'id': reply['story_id'], 'reply_target': True,
                                                    'text': relay.relay_text(reply['text'], lead=relay.SLOT_REPLY_LEAD)},
                                                   'slot', row['text'], reply['text']))
                s.reply_to = row['story_id']
                return self.air_story(s, row['story_id'], relay.relay_text(row['text'], lead=relay.SLOT_LEAD, invite=False))
            s.reply_to = row['story_id']
            return self.air_story(s, row['story_id'], relay.relay_text(row['text'], lead=relay.SLOT_LEAD))
        return None

    def _deliver_now(self, s, item):
        """Queue a story for this session; returns commands to send right away if the car is idle."""
        s.inbox.append(item)
        if self._idle(s):   # nothing playing (also while a body is still being written): air it now
            return self._from_inbox(s) or []
        return []

    async def _continue(self, s):
        """Called when the car is free: queued speech first, then a cut-off segment, then the next segment."""
        while s.pending:
            c = s.pending.pop(0)
            if datetime.fromisoformat(c['expires_at']) < datetime.now(timezone.utc):
                continue
            self._playing(s, c); return [c]
        if s.replay and s.replay[0] == s.episode_id:
            row = self.store.segment(*s.replay); s.replay = None
            if row:
                return self.speak(s, '이어서 들려드릴게요. ' + row['text'], segment=(s.episode_id, row['idx']))
        return await self.advance(s)

    async def ack(self, s, payload):
        command_id = payload.get('command_id'); status = payload.get('status')
        if status not in ('RECEIVED', 'STARTED') + TERMINAL: return []
        row = self.store.conn.execute('SELECT status FROM command_log WHERE command_id=? AND session_id=?', (command_id, s.session_id)).fetchone()
        if not row: return []
        if row['status'] in TERMINAL: return []
        self.store.conn.execute('UPDATE command_log SET status=? WHERE command_id=?', (status, command_id)); self.store.conn.commit()
        if s.playing_command_id != command_id or status not in TERMINAL: return []
        s.playing_command_id = s.playing_deadline = None
        seg = s.segment_cmds.pop(command_id, None)
        if seg and seg[0] == s.episode_id:
            if status == 'COMPLETED':
                self.store.mark_heard(seg[0], seg[1] + 1)
            elif status == 'INTERRUPTED':
                s.replay = seg
        if s.nav_active or s.parked: return []
        if (s.awaiting_approval or s.awaiting_reflection) and not s.pending:
            # The DJ just asked something (and said it): leave a moment of air for the answer instead of rolling on.
            self._background(self._answer_window(s.session_id, s.awaiting_approval, s.awaiting_reflection))
            return []
        return await self._continue(s)

    async def _answer_window(self, sid, story_id, reflection):
        await asyncio.sleep(self.answer_wait)
        s = self.states.get(sid)
        if not s:
            return
        out = []
        if story_id and s.awaiting_approval == story_id:      # no answer: decide on the screen after parking
            s.awaiting_approval = None
            out.append(self._preview(s, story_id, s.awaiting_text))
        elif reflection and s.awaiting_reflection:             # no answer is an answer too
            s.awaiting_reflection = False
        else:
            return
        if self._idle(s):
            out += await self._continue(s)
        await self._push(sid, out)

    async def _nav_ended(self, s):
        out = [self.cmd(s.session_id, 'media.resume', {}, ttl=5)]
        if s.playing_command_id:
            # The car paused that command itself; restart its ACK clock from now.
            self._playing(s, {'command_id': s.playing_command_id, 'payload': {}})
            return out
        return out + await self._continue(s)

    def _pick(self, s, **kw):
        return self.selector.pick(s.client_id, s.commute_type, eta_seconds=self._eta(s), **kw)

    async def process(self, sid, msg):
        if sid not in self.states: raise KeyError('UNKNOWN_SESSION')
        s = self.states[sid]
        if msg.get('session_id') != sid: raise ValueError('SESSION_MISMATCH')
        event_id = msg.get('event_id')
        if not isinstance(event_id, str) or not event_id: raise ValueError('MISSING_EVENT_ID')
        if self.store.dedup(sid, event_id): return []
        recovered = self._expire_lost_command(s)
        out = await self._handle(s, msg)
        if recovered and sid in self.states and not (s.playing_command_id or s.nav_active or s.parked):
            out += await self._continue(s)
        return out

    async def _handle(self, s, msg):
        sid = s.session_id
        etype = msg.get('event_type'); p = msg.get('payload') or {}
        if etype == 'context.updated':
            # Strict validation of the exact shared v1 contract before accepting SDK-derived context.
            from jsonschema import Draft202012Validator, FormatChecker, ValidationError
            schema_path = Path(__file__).resolve().parents[2] / 'contracts' / 'schemas' / 'vehicle-context.schema.json'
            if schema_path.exists():
                schema = json.loads(schema_path.read_text(encoding='utf-8'))
                try:
                    Draft202012Validator(schema, format_checker=FormatChecker()).validate(msg)
                except ValidationError as e:
                    # Reject only this event; previous context stays in effect.
                    where = '/'.join(str(x) for x in e.absolute_path) or '(root)'
                    raise ProtocolError('CONTEXT_SCHEMA_INVALID', f'{where}: {e.message}'[:300]) from None
            was_nav, prev = s.nav_active, s.context
            s.context = p; s.nav_active = bool(p.get('navigation', {}).get('guidance_active', False))
            # Physical parking requires independently confirmed signal, not speed=0.
            s.parked = bool(p.get('vehicle', {}).get('parked_confirmed', False))
            if was_nav and not s.nav_active and not s.parked:
                return await self._nav_ended(s)
            return self._react_to_drive(s, prev, p)
        if etype == 'session.started':
            # The server opens the show: pick a topic for this listener and commute, no tap needed.
            if s.episode_id: return []
            # Replies to this listener's stories that arrived while they were away air after the opening.
            first_lead = relay.AUTHOR_FIRST_LEAD['morning' if s.commute_type == 'FROM_WORK' else 'earlier']
            waiting = self.stories.replies_waiting_for(s.client_id)
            for i, r in enumerate(waiting):
                item = {'id': r['story_id'], 'text': relay.relay_text(
                    r['text'], to_author=True, lead=first_lead if i == 0 else relay.AUTHOR_NEXT_LEAD)}
                if i == len(waiting) - 1:
                    self._comment_on(item, 'author', '', ' / '.join(x['text'] for x in waiting))
                s.inbox.append(item)
            # A topic can be asked for at start (demo, or a topic picked on the car screen before driving).
            topic = self.catalog.get(p.get('topic_key')) if p.get('topic_key') else None
            return await self.start_episode(s, topic or self._pick(s))
        if etype == 'navigation.guidance_started':
            s.nav_active = True; s.seen_navigation = True
            for c in s.pending:   # Don't replay stale traffic/user utterances.
                s.segment_cmds.pop(c['command_id'], None)
            s.pending.clear()
            return [self.cmd(sid, 'media.pause', {}, ttl=5)]
        if etype == 'navigation.guidance_ended':
            s.nav_active = False
            if s.parked: return []
            return await self._nav_ended(s)
        if etype == 'session.ended':
            # Drop in-memory state here, not on socket disconnect, so a car can reconnect after a network blip.
            if s.episode_id: self.store.mark_skipped(s.episode_id)
            self.states.pop(sid, None); self.senders.pop(sid, None)
            return []
        if etype == 'command.ack': return await self.ack(s, p)
        if etype == 'user.action':
            action = p.get('action')
            if action == 'select_topic':
                topic = self.catalog.get(p.get('topic_key')) or self._pick(s, exclude={s.topic_key})
                return await self.start_episode(s, topic)
            if action == 'skip_topic':
                return await self.start_episode(s, self._take_next(s), intro='다른 이야기로 넘어가 볼게요. ')
            if action == 'random_topic':
                return await self.start_episode(s, self._pick(s, exclude={s.topic_key}, explore=True))
            if action == 'set_interests':
                cats = p.get('categories')
                if not isinstance(cats, list) or not cats or any(c not in self.catalog.categories for c in cats):
                    raise ProtocolError('INVALID_CATEGORIES', f'allowed: {sorted(self.catalog.categories)}')
                self.store.set_interests(s.client_id, cats); return []
            if action == 'next_segment':
                if not s.episode_id: return await self.start_episode(s, self._pick(s))
                return [] if s.playing_command_id else await self.advance(s)
            if action == 'request_story':
                s.story_mode, s.story_followup, s.story_parts = True, False, []
                if s.reply_to:
                    return self.speak(s, '네, 방금 들은 사연에 답하는 이야기로 받을게요. 편하게 말씀해 주세요.', listen_after=True)
                return self.speak(s, '네, 편하게 말씀해 주세요. 사연 초안을 만든 뒤 공유할지 따로 확인할게요.', listen_after=True)
            if action == 'drop_music':
                return await self._drop_music(s, str(p.get('track') or ''), extract=False)
            if action == 'play_relay':
                return self._play_relay(s)
            if action == 'now_playing':   # the app reports the song on the car audio (for "이 노래 드랍해줘")
                s.now_playing = str(p.get('track') or '').strip()[:60] or None
                return []
            if action == 'set_memory_consent':
                s.memory_consent = bool(p.get('enabled'))
                return []
            return []
        if etype == 'user.utterance':
            if not p.get('is_final', False): return []
            text = p.get('text', '').strip()[:1200]
            if not text: return []
            s.said.append(text[:200])
            if s.awaiting_approval:
                return await self._voice_approval(s, text)
            if s.awaiting_reflection:
                return await self._take_reflection(s, text)
            if not s.story_followup and relay.is_voice_skip(text):   # "큐큐야, 다른 거"
                return await self.start_episode(s, self._take_next(s), intro='다른 이야기로 넘어가 볼게요. ')
            if p.get('intent') == 'DROP_MUSIC' or (not s.story_followup and drive.is_music_request(text)):
                return await self._drop_music(s, text)
            triggered, story_text = relay.story_trigger(text)
            if triggered:   # "큐큐야, 내 생각 사연으로 남겨 둘래. …"
                s.story_mode, s.story_followup, s.story_parts = True, False, []
                if len(story_text) < 8:
                    return self.speak(s, '네, 편하게 말씀해 주세요. 사연 초안을 만든 뒤 공유할지 따로 확인할게요.', listen_after=True)
                return await self._catch_story(s, story_text)
            if p.get('intent') == 'CREATE_STORY' or s.story_mode or s.story_followup:
                return await self._catch_story(s, text)
            if s.episode_id: self.store.mark_engaged(s.episode_id)  # strongest "this topic works" signal
            s.history.append({'role': 'user', 'content': text})
            current = self.store.segment(s.episode_id, s.segment_idx) if s.episode_id and s.segment_idx >= 0 else None
            if s.topic:
                base = f'그렇게 생각하신 이유가 있군요. 오늘 이야기인 「{s.topic.question}」와도 연결되는 생각이에요.'
            else:
                base = '그렇게 생각하신 이유가 있군요. 조금 더 이야기 나눠 볼까요?'
            prompt = {'episode_question': s.topic.question if s.topic else None,
                      'current_segment': current['text'] if current else None,
                      'history': s.history[-6:], 'vehicle_context': s.context}
            answer = await self.llm.generate(SYSTEM, json.dumps(prompt, ensure_ascii=False), base)
            s.history.append({'role': 'assistant', 'content': answer})
            # Replies may wait behind a long segment, so give them longer than the default 30 s to live.
            return self.speak(s, answer, ttl=120)
        if etype == 'vehicle.parked':
            # Only accept an already confirmed parking signal from current context.
            if s.context.get('vehicle', {}).get('parked_confirmed') is True:
                s.parked = True
                if s.diary_id:
                    return []
                ready = await self._make_diary(s)
                # The one line allowed after parking: the closing, pointing at the diary on the screen.
                style = self._style(s)
                closing = self.cmd(sid, 'dj.speak', {'text': diarylib.CLOSING.get(s.commute_type, diarylib.CLOSING_DEFAULT),
                                                     'voice_style': style, 'interrupt_policy': 'DROP_IF_NAV'}, ttl=60)
                return [closing] + ready
            return []
        return []

    # --- 기능 ② 주행 맥락 코멘트 / ⑥ 마이크 드롭 전달 ---
    def _may_comment(self, s, kind, at):
        last_kind = s.last_comment.get(kind, float('-inf'))
        last_any = s.last_comment.get('_any', float('-inf'))
        return (at - last_kind >= drive.COOLDOWN_SECONDS.get(kind, 600)
                and at - last_any >= drive.GLOBAL_GAP_SECONDS)

    def _react_to_drive(self, s, prev, new):
        at = self.now().timestamp()
        eta = drive.eta_of(new)
        if eta is not None and s.arrival_at is None:
            s.arrival_at = at + eta          # first estimate; later ETAs are compared with it
        out = []
        if s.awaiting_approval and not self._stopped(s):
            # Moving again: stop asking, back to the show. The draft waits on the screen until parking.
            story_id, s.awaiting_approval = s.awaiting_approval, None
            out.append(self._preview(s, story_id, s.awaiting_text))
        if s.nav_active or s.parked or s.signed_off:
            return out
        events = drive.driving_events(prev, new)
        if 'arrival' in events:
            s.wrap_up = True   # finish the current topic; advance() jumps to its OUTRO and then signs off
        if s.story_mode or s.story_followup or s.awaiting_approval or s.awaiting_reflection:
            return out         # never talk over a story being told or a question being answered
        talk = [k for k in events if k != 'arrival' and self._may_comment(s, k, at)]   # arrival: OUTRO says goodbye
        seg = drive.segment_of(new)
        note = None
        if (seg and seg != drive.segment_of(prev)) or 'jam' in events:
            note = self.stories.note_for(s.client_id, road_segment_id=seg, time_bucket=drive.time_bucket(self.now()),
                                         traffic_bucket=drive.traffic_of(new))
            if note and not (talk or self._may_comment(s, 'music', at)):
                note = None
        if talk:
            for k in talk:
                s.last_comment[k] = at
            s.last_comment['_any'] = at
            delay = round((at + eta - s.arrival_at) / 60) if eta is not None and s.arrival_at else None
            line = drive.situation_comment(talk, road_name=drive.road_name_of(new), delay_minutes=delay,
                                           resume=bool(s.playing_command_id and s.episode_id) and not note,
                                           rng=self.selector.rng)
            # Priority: right after the current segment, then the episode carries on where it was.
            spoken = self.speak(s, line, priority=True, ttl=60)
            out += spoken
        if note:   # 마이크 드롭: a song another driver left here (right after the traffic line, if there was one)
            self.stories.mark_note_delivered(note['note_id'], s.client_id)
            s.last_comment['music'] = s.last_comment['_any'] = at
            intro = drive.note_intro(note['track'], note['road_name'])
            if not talk:
                return out + self.speak(s, intro, priority=True, ttl=120)
            c = self.cmd(s.session_id, 'dj.speak', {'text': intro, 'voice_style': self._style(s),
                                                    'interrupt_policy': 'DROP_IF_NAV'}, ttl=120)
            # The traffic line is either playing now (sent) or first in the queue; the song goes right after it.
            s.pending.insert(0 if spoken else 1, c)
            return out
        if talk:
            return out
        if (s.commute_type == 'FROM_WORK' and not s.reflection_asked and self._stopped(s)
                and drive.traffic_of(new) == 'JAM' and self._may_comment(s, 'reflection', at)):
            # 퇴근길 회고: only while stopped in traffic with no guidance, once per trip.
            s.reflection_asked = s.awaiting_reflection = True
            s.last_comment['reflection'] = s.last_comment['_any'] = at
            return out + self.speak(s, relay.REFLECTION_QUESTION, priority=True, ttl=60, listen_after=True)
        return out

    async def _drop_music(self, s, text, extract=True):
        guess = drive.guess_track(text) if extract else text.strip()
        track = (await self.llm.generate(drive.MUSIC_SYSTEM, text, guess)) if extract and text else guess
        track = track.strip().strip('「」『』"\'“”').strip()[:60]
        if (not track or track.upper() == 'NONE') and s.now_playing:
            track = s.now_playing   # "이 노래 드랍해줘": the song playing right now
        if not track or track.upper() == 'NONE' or relay.has_pii(track):
            return self.speak(s, '어떤 곡을 남길지, 곡 제목을 한 번만 더 말씀해 주세요.', listen_after=True)
        ctx = s.context
        self.stories.add_note(client_id=s.client_id, session_id=s.session_id, road_segment_id=drive.segment_of(ctx),
                              road_name=drive.road_name_of(ctx), time_bucket=drive.time_bucket(self.now()),
                              traffic_bucket=drive.traffic_of(ctx), track=track)
        where = '이 구간에' if drive.segment_of(ctx) else '지금 이 시간 길 위에'
        return self.speak(s, f'좋아요. {where} 「{track}」 음표를 남겨 둘게요. 비슷한 길을 지나는 운전자에게 전해 드릴게요.',
                          priority=True)

    # --- 기능 ④ Story Catch ---
    async def _catch_story(self, s, text):
        if relay.has_pii(text):
            s.story_mode = s.story_followup = False; s.story_parts = []
            return self.speak(s, '개인정보가 포함되어 사연으로 정리할 수 없어요. 내용을 바꾸어 다시 말씀해 주세요.')
        if s.episode_id: self.store.mark_engaged(s.episode_id)
        if not s.story_followup and len(text) >= 30:
            # Already a whole thought (the driver said it in one go): no follow-up needed.
            s.story_parts, s.story_mode = [text], False
            return await self._draft_story(s)
        if not s.story_followup:
            # One short follow-up, like a DJ would; the driver may answer, decline, or just keep driving.
            s.story_parts, s.story_mode, s.story_followup = [text], False, True
            s.story_seq += 1
            self._background(self._followup_timeout(s.session_id, s.story_seq))
            question = await self.llm.generate(relay.FOLLOWUP_SYSTEM, text, relay.FOLLOWUP_FALLBACK)
            return self.speak(s, question, ttl=60, priority=True, listen_after=True)
        s.story_followup = False
        if not relay.wants_no_followup(text):
            s.story_parts.append(text)
        return await self._draft_story(s)

    async def _followup_timeout(self, sid, seq):
        await asyncio.sleep(self.followup_seconds)
        s = self.states.get(sid)
        if s and s.story_followup and s.story_seq == seq:   # no answer: draft from what was said
            s.story_followup = False
            await self._push(sid, await self._draft_story(s))

    async def _draft_story(self, s):
        raw = ' '.join(s.story_parts); s.story_parts = []
        if not raw:
            return []
        fallback = relay.fallback_story(raw)
        text = relay.redact(await self.llm.generate(relay.STORY_SYSTEM, raw, fallback)) or fallback
        parent = s.reply_to
        story_id = self.store.add_draft(s.session_id, s.episode_id, text)
        self.stories.add_meta(story_id, client_id=s.client_id, session_id=s.session_id, topic_key=s.topic_key,
                              category=s.topic.category if s.topic else None, commute_type=s.commute_type,
                              congestion=drive.traffic_of(s.context), weather=drive.weather_of(s.context),
                              road_segment_id=drive.segment_of(s.context), raw_text=raw, parent_story_id=parent,
                              relation_type=relay.classify_reply(raw) if parent else 'INDEPENDENT')
        if self._stopped(s) and not s.nav_active:
            # Stopped (signal, jam): read the draft back and ask once, by voice. If the car moves before the
            # answer, the question is dropped and the preview waits for parking instead.
            s.awaiting_approval, s.awaiting_text = story_id, text
            empathy = await self.llm.generate(relay.EMPATHY_SYSTEM, raw, relay.EMPATHY_FALLBACK)
            return self.speak(s, f"{empathy} '{text}' 이렇게 전할게요. 보낼까요?", ttl=120, priority=True, listen_after=True)
        # Not published: the car shows this preview after parking and the driver approves it explicitly.
        return [self._preview(s, story_id, text)] + self.speak(
            s, '사연 초안을 만들어 두었어요. 차를 세운 뒤에 공유할지 여쭤볼게요.', priority=True)

    def _preview(self, s, story_id, text):
        return self.cmd(s.session_id, 'story.preview', {'story_id': story_id, 'preview_text': text,
                                                        'requires_explicit_approval': True}, ttl=300)

    @staticmethod
    def _stopped(s):
        v = (s.context or {}).get('vehicle') or {}
        speed = v.get('speed_kmh')
        return v.get('motion_state') in ('STOPPED', 'PARKED') or (isinstance(speed, (int, float)) and speed < 1)

    async def _voice_approval(self, s, text):
        story_id, s.awaiting_approval = s.awaiting_approval, None
        answer = relay.voice_answer(text)
        if answer is None:   # not a yes/no: leave the decision to the screen after parking
            return [self._preview(s, story_id, s.awaiting_text)] + self.speak(
                s, '보낼지는 차를 세운 뒤에 화면에서 정해 주셔도 돼요.', priority=True)
        self.store.approve(story_id, s.session_id, answer == 'YES')
        if answer == 'NO':
            return self.speak(s, '알겠어요. 이번 이야기는 저만 간직할게요.', priority=True)
        status = await self.publish(story_id)
        if status == 'BLOCKED':
            return self.speak(s, '이번 사연에는 다른 분께 전하기 어려운 표현이 있어서, 저만 간직할게요.', priority=True)
        closing = await self.llm.generate(relay.SENT_SYSTEM, s.awaiting_text, relay.SENT_FALLBACK)
        return self.speak(s, closing, priority=True)

    async def _take_reflection(self, s, text):
        s.awaiting_reflection = False
        s.reflection = text[:200]
        if s.memory_consent:   # 개인 메모리: kept only with consent, never shared or relayed
            self.stories.add_reflection(s.client_id, s.session_id, s.commute_type, s.reflection)
        if s.episode_id:
            self.store.mark_engaged(s.episode_id)   # a moment worth keeping: today's topic worked
        reply = await self.llm.generate(relay.REFLECTION_SYSTEM, text, relay.REFLECTION_FALLBACK)
        return self.speak(s, reply, priority=True)

    # --- 기능 ⑤ Story Relay ---
    async def publish(self, story_id):
        """Called after the owner approves. Moderation, then relay to matching cars. Returns the final status."""
        st = self.stories.story(story_id)
        if not st:
            return 'APPROVED'   # draft from before Story Relay existed: approval only
        problem = relay.moderation_problem(st['text'])
        if problem:
            self.stories.set_status(story_id, 'BLOCKED', problem)
            return 'BLOCKED'
        self.stories.set_status(story_id, 'PUBLISHED')
        if st['parent_story_id']:
            self.stories.add_relation(st['parent_story_id'], story_id, st['relation_type'])
        await self._relay_live(self.stories.story(story_id))
        self._maybe_candidate(self.stories.story(story_id))
        return 'PUBLISHED'

    def _relay_score(self, s, st):
        score = 0
        if s.topic_key and s.topic_key == st['topic_key']:
            score += 3
        elif s.topic and s.topic.category == st['category']:
            score += 1
        if s.commute_type == st['commute_type'] and s.commute_type not in ('UNKNOWN', 'OTHER'):
            score += 1
        congestion = drive.traffic_of(s.context)
        if congestion and congestion == st['congestion']:
            score += 1
        seg = drive.segment_of(s.context)
        if seg and seg == st['road_segment_id']:
            score += 1
        return score

    async def _relay_live(self, st):
        parent = self.stories.story(st['parent_story_id']) if st['parent_story_id'] else None
        for s in list(self.states.values()):
            if s.client_id == st['client_id'] or self.stories.delivered(st['story_id'], s.client_id):
                continue
            if parent and s.client_id == parent['client_id']:
                item = self._comment_on({'id': st['story_id'], 'text': relay.relay_text(st['text'], to_author=True)},
                                        'author', parent['text'], st['text'])
            elif self._relay_score(s, st) >= 2:
                item = {'id': st['story_id'], 'text': relay.relay_text(st['text'])}
                if parent:
                    self._comment_on(item, 'relay', parent['text'], st['text'])
            else:
                continue
            commands = self._deliver_now(s, item)
            if commands:
                await self._push(s.session_id, commands)

    def _maybe_candidate(self, st):
        """Every published story offers its question to the common topic pool (operator approves it);
        a thread that keeps growing offers it again with the replies in view."""
        root = self.stories.root_of(st['story_id'])
        if not st['parent_story_id'] or self.stories.reply_count(root) >= 2:
            self._background(self._make_candidate(root))

    def adopt_candidate(self, candidate_id):
        """Operator approved a topic candidate: it joins the topic pool and can open anyone's next show."""
        cand = self.stories.candidate(candidate_id)
        if not cand or cand['status'] != 'PENDING':
            return None
        root = self.stories.story(cand['from_story_id'])
        category = (root['category'] if root and root['category'] in self.catalog.categories else 'MIND')
        commute = root['commute_type'] if root else None
        fit = {'TO_WORK': 0.9 if commute == 'TO_WORK' else 0.7, 'FROM_WORK': 0.9 if commute == 'FROM_WORK' else 0.7}
        key = 'pool_' + candidate_id.split('-')[-1]
        self.stories.save_pool_topic(key=key, category=category, question=cand['topic_text'], summary=relay.POOL_SUMMARY,
                                     angles=relay.POOL_ANGLES, commute_fit=fit, from_candidate=candidate_id)
        self.catalog.add(self._pool_topic(self.store.conn.execute('SELECT * FROM pool_topics WHERE topic_key=?',
                                                                  (key,)).fetchone()))
        self.stories.set_candidate_status(candidate_id, 'ADOPTED')
        return key

    async def _make_candidate(self, root):
        try:
            thread = self.stories.thread(root)
            if not thread:
                return
            fallback = relay.fallback_candidate(thread[0]['text'])
            question = await self.llm.generate(relay.CANDIDATE_SYSTEM, '\n'.join(r['text'] for r in thread), fallback)
            self.stories.add_candidate(root, question.strip()[:120])
        except Exception:
            log.exception('topic candidate failed for %s', root)

    def _play_relay(self, s):
        """Stories that drivers passed between each other, aired as their own short program."""
        root = self.stories.best_thread_for(s.client_id)
        if not root:
            return self.speak(s, '아직 들려드릴 사연 릴레이가 없어요. 사연이 모이면 꼭 전해 드릴게요.')
        thread = self.stories.thread(root)
        for i, row in enumerate(thread):
            relation = 'INDEPENDENT' if i == 0 else row['relation_type']
            last = i == len(thread) - 1
            item = {'id': row['story_id'], 'text': relay.relay_text(row['text'], relation, invite=last),
                    'check': False, 'reply_target': last}
            if last and len(thread) > 1:
                self._comment_on(item, 'relay', thread[0]['text'], ' / '.join(r['text'] for r in thread[1:]))
            s.inbox.append(item)
        return self.speak(s, f'지금부터 운전자들 사이에 오간 사연 {len(thread)}편을 이어서 들려드릴게요.', priority=True)

    # --- 기능 ⑦ 오늘의 Q Diary ---
    async def _make_diary(self, s):
        sid = s.session_id
        # The whole day, not just this trip: the morning story shows up again in the evening diary.
        today = (datetime.now(timezone.utc) - timedelta(hours=18)).isoformat()
        f = diarylib.facts_of(
            commute_type=s.commute_type, topics=s.topics_heard, said=s.said,
            sent=[r['text'] for r in self.stories.stories_by_client_since(s.client_id, today)],
            heard=[r['text'] for r in self.stories.stories_heard_by_session(sid)],
            notes_left=self.stories.notes_by_session(sid),
            notes_heard=self.stories.notes_heard_by_session(s.client_id, s.started_at),
            weather=drive.weather_of(s.context), congestion=drive.traffic_of(s.context),
            replies=[r['text'] for r in self.stories.replies_heard_by_session(sid, s.client_id)],
            reflection=s.reflection)
        mood = diarylib.mood_of(s.said + f['stories_sent'])
        record = json.dumps(f, ensure_ascii=False)
        text = await self.llm.generate(diarylib.DIARY_SYSTEM, record, diarylib.template_text(f))
        comment = await self.llm.generate(diarylib.COMMENT_SYSTEM, record, diarylib.COMMENT_FALLBACK.get(mood, ''))
        diary_id = 'diary-' + uuid.uuid4().hex   # the link is the only key: long enough not to be guessed
        self.stories.save_diary(diary_id, sid, s.client_id, {**f, 'mood': mood, 'text': text, 'commute_type': s.commute_type,
                                                             'headline': diarylib.headline(f), 'dj_comment': comment})
        s.diary_id = diary_id
        self._background(self._diary_image(diary_id, f, mood))
        return [self.cmd(sid, 'diary.ready', {'diary_id': diary_id, 'view_url': f'{self.public_base}/diaries/{diary_id}'},
                         ttl=600)]

    async def _diary_image(self, diary_id, f, mood):
        path = await diarylib.generate_image(self.llm, diarylib.image_prompt(f, mood), self.diary_dir, diary_id)
        if path:
            self.stories.set_diary_image(diary_id, path)
