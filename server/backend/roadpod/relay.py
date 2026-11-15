"""Story Catch (기능 ④) and Story Relay (기능 ⑤): data and rules. Director wires these into the broadcast.

Life of a story
  1. Catch:   request_story / intent=CREATE_STORY → driver speaks → DJ asks ONE follow-up → draft
              (LLM rewrites it as an anonymous first-person story; deterministic redaction when no LLM)
  2. Approve: driver approves the preview after parking → moderation → PUBLISHED or BLOCKED
  3. Relay:   published stories reach other drivers whose topic / commute / traffic match
              (live at the next segment boundary, or in the STORY_SLOT just before an episode's OUTRO)
  4. Reply:   a driver who just heard a story can answer it (맞사연) → EMPATHY / COUNTERPOINT / FOLLOW_UP
              relation; the original author hears the reply in this or their next session
  5. Expand:  a thread with replies, or a topic with several stories, becomes a topic candidate, and
              threads can be aired as their own short program (play_relay)
"""
from __future__ import annotations
import json, re, sqlite3, uuid
from datetime import datetime, timezone

SCHEMA = '''
CREATE TABLE IF NOT EXISTS story_meta(story_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, session_id TEXT NOT NULL,
    topic_key TEXT, category TEXT, commute_type TEXT, congestion TEXT, weather TEXT, road_segment_id TEXT,
    raw_text TEXT NOT NULL, parent_story_id TEXT, relation_type TEXT NOT NULL DEFAULT 'INDEPENDENT',
    published_at TEXT, block_reason TEXT);
CREATE TABLE IF NOT EXISTS story_deliveries(story_id TEXT NOT NULL, client_id TEXT NOT NULL, session_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL, PRIMARY KEY(story_id, client_id));
CREATE TABLE IF NOT EXISTS music_notes(note_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, session_id TEXT NOT NULL,
    road_segment_id TEXT, road_name TEXT, time_bucket TEXT NOT NULL, traffic_bucket TEXT, track TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS music_deliveries(note_id TEXT NOT NULL, client_id TEXT NOT NULL, delivered_at TEXT NOT NULL,
    PRIMARY KEY(note_id, client_id));
CREATE TABLE IF NOT EXISTS diaries(diary_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, client_id TEXT NOT NULL,
    created_at TEXT NOT NULL, data_json TEXT NOT NULL, image_path TEXT);
CREATE TABLE IF NOT EXISTS pool_topics(topic_key TEXT PRIMARY KEY, category TEXT NOT NULL, question TEXT NOT NULL,
    summary TEXT NOT NULL, angles_json TEXT NOT NULL, commute_fit_json TEXT NOT NULL, from_candidate TEXT, created_at TEXT NOT NULL);
-- 개인 메모리: only stored when the driver turned memory on; never shared or relayed.
CREATE TABLE IF NOT EXISTS reflections(client_id TEXT NOT NULL, session_id TEXT NOT NULL, commute_type TEXT,
    text TEXT NOT NULL, created_at TEXT NOT NULL);
'''

# --- voice commands (운전 중에는 말로만) ---
_CALL = r'^((큐큐|붕붕)[아야]?[,\s]*)?'
SKIP_TOPIC_VOICE = re.compile(_CALL + r'(다른\s*(거|걸|주제|이야기|얘기)|넘겨|다음\s*(거|주제|이야기))')
STORY_TRIGGER = re.compile(r'((큐큐|붕붕)[아야]?[,\s]*)?(내\s*(생각|이야기|얘기)\s*)?사연(으로)?\s*(남겨|남길|보내|보낼|전해)[^\s.,!?]*(\s*(둘래|줘|줄래|볼래|주세요))?[.,!?…]*')
VOICE_YES = re.compile(r'^(응|어|네|예|그래|좋아|보내|오케이|ㅇㅇ|웅|넵)')
VOICE_NO = re.compile(r'아니|안\s*보내|보내지\s*마|하지\s*마|취소|됐어|말래')

EMPATHY_SYSTEM = '''너는 차량 라디오 DJ 큐큐다. 운전자가 사연으로 남기고 싶은 생각을 말했다.
- 한국어 2문장, 90자 이내. 먼저 그 생각에 공감하고, 그 생각의 핵심을 운전자가 말한 뜻 그대로 한 번 짚어 준다.
- 마지막 문장은 "이 이야기, 사연으로 전해 볼게요."로 끝낸다. 판단하거나 충고하지 않는다.'''
EMPATHY_FALLBACK = '그런 생각이 드는 날이 있죠. 참 중요한 포인트 같아요. 이 이야기, 사연으로 전해 볼게요.'
SENT_SYSTEM = '''너는 차량 라디오 DJ 큐큐다. 운전자가 방금 사연 공유를 승인했다.
- "큐큐~"으로 시작하는 한국어 2~3문장, 150자 이내. 사연이 말한 뜻 그대로 짧게 응원한다. 운전자가 하지 않은 행동을 칭찬하지 않는다.
- 사연 내용과 맞는 명언이 확실히 떠오를 때만 한 문장 인용하고 누구의 말인지 밝힌다. 출처가 확실하지 않으면 인용하지 않는다.
- 마지막은 이 사연이 다른 운전자에게 닿을 거라는 말로 닫는다.'''
SENT_FALLBACK = '큐큐~ 좋은 사연이에요. 비슷한 길 위의 누군가에게 꼭 전해 드릴게요.'
REFLECTION_QUESTION = '오늘 하루에서 한 장면만 고른다면요?'
REFLECTION_SYSTEM = '''너는 차량 라디오 DJ 큐큐다. "오늘 하루에서 한 장면만 고른다면요?"에 운전자가 답했다.
- 한국어 1~2문장, 70자 이내로 따뜻하게 받아 준다. 더 묻지 않는다. 마지막은 그 장면을 오늘 일기에 담아 두겠다는 말.'''
REFLECTION_FALLBACK = '좋은 장면이네요. 그 순간, 오늘 일기에 꼭 담아 둘게요.'
POOL_SUMMARY = '청취자 사연에서 나온 질문이에요. 여러 운전자의 생각을 함께 나눠 봐요.'
POOL_ANGLES = ('그렇다고 보는 관점', '꼭 그렇지는 않다고 보는 관점', '상황과 관계에 따라 달라진다는 관점')


def is_voice_skip(text):
    t = (text or '').strip()
    return len(t) <= 15 and bool(SKIP_TOPIC_VOICE.search(t))


def story_trigger(text):
    """'큐큐야, 내 생각 사연으로 남겨 둘래. …' → (True, the story part without the request words)."""
    m = STORY_TRIGGER.search(text or '')
    if not m:
        return False, text
    before, after = text[:m.start()].strip(' .,…'), text[m.end():].strip(' .,…')
    # The story is what follows the request; what came before is usually lead-in ("운전하면서 생각해 봤는데…").
    rest = after if len(after) >= 10 else (before + ' ' + after).strip()
    rest = re.sub(r'^(아\s*)?(근데|그런데)\s*', '', rest)
    return True, re.sub(r'\s+', ' ', rest).strip()


def voice_answer(text):
    t = (text or '').strip()
    if VOICE_NO.search(t):
        return 'NO'
    if len(t) <= 12 and VOICE_YES.search(t):
        return 'YES'
    return None

# Personal data that must never reach another car: phone, e-mail, resident number, licence plate.
# Digit lookarounds, not \b: in Korean a particle sticks to the number ("5678로") and \b never matches there.
PII = re.compile(r'(?<!\d)0\d{1,2}[- .]?\d{3,4}[- .]?\d{4}(?!\d)'          # phone
                 r'|[\w.+-]+@[\w-]+\.[\w.]+|@'                              # e-mail (and any @handle)
                 r'|(?<!\d)\d{6}-?[1-4]\d{6}(?!\d)'                         # resident registration number
                 r'|(?<!\d)\d{2,3}[가나다라마거너더러머버서어저고노도로모보소오조구누두루무부수우주아바사자배하허호]\s?\d{4}(?!\d)')  # plate
# Not relayed to strangers. Self-harm words block relay; the driver still gets a calm reply elsewhere.
BLOCKED_WORDS = re.compile(r'씨발|시발|ㅅㅂ|병신|좆|개새끼|썅|죽여\s*버|자살|죽고\s*싶')
SKIP_FOLLOWUP = re.compile(r'없어|없습니다|괜찮아|됐어|이대로|그대로|보내\s*(줘|주세요|도)|그냥\s*(보내|정리|올려)|끝')
COUNTERPOINT = re.compile(r'반대|다르게|아니라고|글쎄|동의\s*(못|하지|안)|오히려|그렇지\s*않')
EMPATHY = re.compile(r'공감|맞아|저도|나도|같은\s*마음|위로|이해\s*(돼|가|해)')

STORY_SYSTEM = '''운전자가 라디오 DJ에게 음성으로 말한 내용을, 다른 운전자에게 들려줄 익명 사연으로 정리한다.
- 1인칭, 말하듯 2~4문장, 120~220자.
- 운전자의 핵심 의견과 감정을 그대로 살린다. 뜻을 바꾸지 않는다: 운전자가 의문을 던졌으면 의문으로 남기고, 하지 않은 행동·결심·노력을 지어내지 않는다.
- 군말("아 근데", "암튼 그래", "어렵긴 한데")은 정리하되 망설임의 결은 살린다.
- 마지막 문장은 다른 운전자에게 생각을 묻는 한 문장으로 끝내도 좋다.
- 예: 운전자 "나는 무례한 사람을 꼭 내 편으로 만들어야 할까? 그런 생각도 들어." → "무례한 사람을 꼭 내 편으로 만들어야 할까요? 내 편보다 먼저 챙겨야 할 건 내 마음이 아닐까 싶어요. 여러분은 어떻게 생각하세요?"
- 이름, 회사, 학교, 구체적 지명, 차량 번호, 연락처, 나이 같은 신상 정보는 빼거나 '한 동료', '어느 동네'처럼 흐린다.
- 욕설은 순화한다. 사연 본문만 출력한다. 따옴표나 머리말 없이.'''
FOLLOWUP_SYSTEM = '''너는 차량 라디오 DJ 큐큐다. 운전자가 사연을 들려줬다. 사연을 더 생생하게 만들 짧은 후속 질문 하나만 한다.
- 한국어 1~2문장, 70자 이내. 질문 하나 뒤에 "편하실 때 한마디만 들려주셔도 좋아요"처럼 부담을 덜어 주는 말을 붙인다.
- '선택', '대답하실래요' 같은 딱딱한 말은 쓰지 않는다.
- 신상 정보(이름, 회사, 장소)를 묻지 않는다. 감정이나 그때의 생각을 묻는다.
- 예: "그 순간 가장 크게 든 마음은 뭐였어요? 편하실 때 한마디만 들려주셔도 좋아요."'''
FOLLOWUP_FALLBACK = '그 순간 어떤 마음이 드셨어요? 한마디 더 들려주셔도 좋고, 이대로 정리해도 괜찮아요.'
CANDIDATE_SYSTEM = '''여러 운전자가 주고받은 익명 사연을 보고, 이걸 바탕으로 다음 라디오 에피소드에서 다룰 질문 하나를 만든다.
- 한국어 질문 한 문장, 40자 이내, 물음표로 끝낸다. 한쪽 답을 정하지 않는 열린 질문. 질문만 출력한다.
- 사연 문장을 그대로 되풀이하지 말고, 그 고민을 더 많은 사람이 자기 일로 느낄 수 있게 넓힌다.
- '왜 중요할까요?'처럼 답을 전제하는 질문은 피한다. 예: "모두와 잘 지내지 않아도 괜찮을까?"'''


def ensure_tables(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    conn.commit()


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return prefix + '-' + uuid.uuid4().hex[:12]


def has_pii(text):
    return bool(PII.search(text or ''))


def redact(text):
    return PII.sub('', text or '').strip()


def moderation_problem(text):
    if has_pii(text):
        return 'PII'
    if BLOCKED_WORDS.search(text or ''):
        return 'UNSAFE_LANGUAGE'
    return None


def wants_no_followup(text):
    """Only a short "that's all" answer; "그냥 좀 서운했어요" is a real answer and must be kept."""
    t = (text or '').strip()
    return len(t) <= 15 and bool(SKIP_FOLLOWUP.search(t))


def classify_reply(text):
    if COUNTERPOINT.search(text or ''):
        return 'COUNTERPOINT'
    if EMPATHY.search(text or ''):
        return 'EMPATHY'
    return 'FOLLOW_UP'


def fallback_story(raw):
    """No LLM: keep the driver's words, minus anything that identifies them."""
    body = redact(raw)
    return body[:200] + ('…' if len(body) > 200 else '')


def fallback_candidate(story_text):
    """No LLM: the story's own question if it asked one, else an open question around it."""
    questions = [q.strip() for q in re.split(r'(?<=[.!?…])\s+', story_text or '') if q.strip().endswith('?')]
    if questions:
        return re.sub(r'^(나는|저는|난|전)\s+', '', questions[0])
    return f'「{(story_text or "")[:30].strip()}…」, 당신이라면 어떻게 생각하나요?'


# --- framing lines the DJ says around a relayed story (story.incoming text, ≤ 500 chars) ---
RELATION_LEAD = {
    'INDEPENDENT': '다른 운전자분이 보내 주신 사연이 도착했어요.',
    'EMPATHY': '앞서 들려드린 사연에 공감하는 답장이 왔어요.',
    'COUNTERPOINT': '앞서 들려드린 사연에 조금 다른 생각을 보내 주신 분이 있어요.',
    'FOLLOW_UP': '앞서 들려드린 사연에 이어지는 이야기가 왔어요.',
}
REPLY_INVITE = ' 이 이야기에 하고 싶은 말이 있으면, 편할 때 사연 보내기로 답해 주세요.'


def relay_text(story_text, relation='INDEPENDENT', *, to_author=False, invite=True, lead=None, tail=''):
    if lead is None:
        lead = ('지난번 보내 주신 사연에 다른 운전자가 답을 보냈어요.' if to_author
                else RELATION_LEAD.get(relation, RELATION_LEAD['INDEPENDENT']))
    text = f'{lead} {story_text}'
    if tail:
        text += ' ' + tail
    if invite and not to_author:
        text += REPLY_INVITE
    return text[:500]


# Story corner (사연 슬롯): a story and the reply it got, introduced like a radio host would.
SLOT_LEAD = '이 이야기와 딱 맞는 사연이 있어요. 한 청취자님이 이렇게 보내 주셨어요.'
SLOT_REPLY_LEAD = '여기에 다른 청취자님이 이렇게 답장을 보내셨어요.'
SLOT_REPLY_TAIL = '사연과 답장이 오가니까 라디오가 조금 더 따뜻해지네요.'   # kept for old callers; replies now get a 큐큐 comment instead

# After a reply is read, 큐큐 adds a line of its own: the reply alone ends the corner too flat.
REPLY_COMMENT_SYSTEM = '''너는 차량 라디오 DJ 큐큐다. 방금 청취자 사연과, 그 사연에 다른 운전자가 보낸 답장을 읽었다.
답장이 짚은 관점을 한 번 더 살려서, 지금 운전 중인 청취자에게 건네는 코멘트를 1~2문장으로 말한다.
- 존댓말, 라디오 진행자처럼 따뜻하고 담백하게. 120자 이내.
- 사연·답장 문장을 그대로 반복하지 말고, 새로운 사실이나 출처를 지어내지 않는다.
- 이름·연락처 같은 개인정보는 말하지 않는다. 출력은 말할 문장만.'''
REPLY_COMMENT_FALLBACK = {
    'slot': '답장 한 줄에 사연이 조금 가벼워진 것 같아요. 같은 길 위에서 비슷한 마음을 지나는 분이 생각보다 많네요.',
    'author': '아침에 꺼내 주신 고민이, 다른 운전자들의 마음을 거쳐 이렇게 돌아왔어요. 혼자만의 고민은 아니었던 거죠.',
    'relay': '사연이 다른 사연을 부르네요. 이렇게 이어지는 이야기가 라디오를 조금 더 따뜻하게 만들어 줘요.',
}
AUTHOR_FIRST_LEAD = {'morning': '아침에 남겨 주신 사연에 답장이 왔어요.', 'earlier': '지난번 보내 주신 사연에 답장이 왔어요.'}
AUTHOR_NEXT_LEAD = '다른 분은 이렇게 보내 주셨어요.'


class StoryBook:
    """Story tables on top of MemoryStore's connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add_meta(self, story_id, *, client_id, session_id, topic_key, category, commute_type, congestion, weather,
                 road_segment_id, raw_text, parent_story_id=None, relation_type='INDEPENDENT'):
        self.conn.execute('INSERT INTO story_meta(story_id,client_id,session_id,topic_key,category,commute_type,congestion,'
                          'weather,road_segment_id,raw_text,parent_story_id,relation_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                          (story_id, client_id, session_id, topic_key, category, commute_type, congestion, weather,
                           road_segment_id, raw_text, parent_story_id, relation_type))
        self.conn.commit()

    def story(self, story_id):
        return self.conn.execute(
            'SELECT d.story_id, d.preview_text AS text, d.status, d.session_id, d.episode_id, m.* '
            'FROM story_drafts d JOIN story_meta m USING(story_id) WHERE d.story_id=?', (story_id,)).fetchone()

    def set_status(self, story_id, status, reason=None):
        self.conn.execute('UPDATE story_drafts SET status=? WHERE story_id=?', (status, story_id))
        if status == 'PUBLISHED':
            self.conn.execute('UPDATE story_meta SET published_at=? WHERE story_id=?', (now(), story_id))
        if reason:
            self.conn.execute('UPDATE story_meta SET block_reason=? WHERE story_id=?', (reason, story_id))
        self.conn.commit()

    def add_relation(self, parent_id, child_id, relation_type):
        self.conn.execute('INSERT INTO story_relations VALUES(?,?,?,?)', (uid('rel'), parent_id, child_id, relation_type))
        self.conn.commit()

    def root_of(self, story_id):
        seen = set()
        while story_id and story_id not in seen:
            seen.add(story_id)
            row = self.conn.execute('SELECT parent_story_id FROM story_meta WHERE story_id=?', (story_id,)).fetchone()
            if not row or not row['parent_story_id']:
                return story_id
            story_id = row['parent_story_id']
        return story_id

    def thread(self, root_id):
        """Root story then its published replies, oldest first."""
        rows = [self.story(root_id)]
        rows += self.conn.execute(
            'SELECT d.story_id, d.preview_text AS text, d.status, m.* FROM story_drafts d JOIN story_meta m USING(story_id) '
            'WHERE m.parent_story_id=? AND d.status=\'PUBLISHED\' ORDER BY m.published_at', (root_id,)).fetchall()
        return [r for r in rows if r is not None]

    def reply_count(self, root_id):
        return self.conn.execute('SELECT COUNT(*) n FROM story_meta m JOIN story_drafts d USING(story_id) '
                                 'WHERE m.parent_story_id=? AND d.status=\'PUBLISHED\'', (root_id,)).fetchone()['n']

    def topic_story_count(self, topic_key):
        return self.conn.execute('SELECT COUNT(*) n FROM story_meta m JOIN story_drafts d USING(story_id) '
                                 'WHERE m.topic_key=? AND d.status=\'PUBLISHED\'', (topic_key,)).fetchone()['n']

    def mark_delivered(self, story_id, client_id, session_id):
        self.conn.execute('INSERT OR IGNORE INTO story_deliveries VALUES(?,?,?,?)', (story_id, client_id, session_id, now()))
        self.conn.commit()

    def delivered(self, story_id, client_id):
        return self.conn.execute('SELECT 1 FROM story_deliveries WHERE story_id=? AND client_id=?',
                                 (story_id, client_id)).fetchone() is not None

    def candidates_for_slot(self, client_id, topic_key, category, limit=5, commute_type=None):
        """Published root stories this listener has not heard: same topic, then same category, then same commute
        (퇴근길 청취자들의 이야기). Stories with replies first, so the corner can air a story and its answer."""
        return self.conn.execute(
            'SELECT d.story_id, d.preview_text AS text, m.relation_type, m.topic_key, m.category, '
            '(SELECT COUNT(*) FROM story_meta r JOIN story_drafts rd USING(story_id) WHERE r.parent_story_id=d.story_id '
            ' AND rd.status=\'PUBLISHED\') AS replies FROM story_drafts d '
            'JOIN story_meta m USING(story_id) WHERE d.status=\'PUBLISHED\' AND m.client_id<>? AND m.parent_story_id IS NULL '
            'AND (m.topic_key=? OR m.category=? OR m.commute_type=?) AND NOT EXISTS (SELECT 1 FROM story_deliveries x '
            'WHERE x.story_id=d.story_id AND x.client_id=?) '
            'ORDER BY (m.topic_key=?) DESC, (m.category=?) DESC, (replies>0) DESC, m.published_at DESC LIMIT ?',
            (client_id, topic_key, category, commute_type or '-', client_id, topic_key, category, limit)).fetchall()

    def first_reply(self, story_id, client_id):
        return self.conn.execute(
            'SELECT d.story_id, d.preview_text AS text, m.relation_type FROM story_drafts d JOIN story_meta m USING(story_id) '
            'WHERE m.parent_story_id=? AND d.status=\'PUBLISHED\' AND m.client_id<>? ORDER BY m.published_at LIMIT 1',
            (story_id, client_id)).fetchone()

    def replies_waiting_for(self, client_id):
        """Published replies to this listener's stories that they have not heard yet."""
        return self.conn.execute(
            'SELECT d.story_id, d.preview_text AS text, m.relation_type FROM story_drafts d JOIN story_meta m USING(story_id) '
            'JOIN story_meta p ON p.story_id=m.parent_story_id WHERE d.status=\'PUBLISHED\' AND p.client_id=? '
            'AND m.client_id<>? AND NOT EXISTS (SELECT 1 FROM story_deliveries x WHERE x.story_id=d.story_id '
            'AND x.client_id=?) ORDER BY m.published_at', (client_id, client_id, client_id)).fetchall()

    def best_thread_for(self, client_id):
        """Root of the most-replied thread with at least one reply (any author but at least one not this client)."""
        row = self.conn.execute(
            'SELECT m.parent_story_id AS root, COUNT(*) n FROM story_meta m JOIN story_drafts d USING(story_id) '
            'WHERE m.parent_story_id IS NOT NULL AND d.status=\'PUBLISHED\' GROUP BY m.parent_story_id '
            'HAVING SUM(m.client_id<>?) > 0 ORDER BY n DESC, MAX(m.published_at) DESC LIMIT 1', (client_id,)).fetchone()
        return row['root'] if row else None

    # --- topic candidates (사연 → 새 주제) ---
    def add_candidate(self, from_story_id, topic_text):
        if self.conn.execute('SELECT 1 FROM topic_candidates WHERE from_story_id=?', (from_story_id,)).fetchone():
            return None
        cid = uid('cand')
        self.conn.execute('INSERT INTO topic_candidates VALUES(?,?,?,?)', (cid, from_story_id, topic_text, 'PENDING'))
        self.conn.commit()
        return cid

    def candidates(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM topic_candidates ORDER BY rowid DESC')]

    def candidate(self, candidate_id):
        row = self.conn.execute('SELECT * FROM topic_candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
        return dict(row) if row else None

    def set_candidate_status(self, candidate_id, status):
        self.conn.execute('UPDATE topic_candidates SET status=? WHERE candidate_id=?', (status, candidate_id))
        self.conn.commit()

    def save_pool_topic(self, *, key, category, question, summary, angles, commute_fit, from_candidate):
        self.conn.execute('INSERT OR REPLACE INTO pool_topics VALUES(?,?,?,?,?,?,?,?)',
                          (key, category, question, summary, json.dumps(list(angles), ensure_ascii=False),
                           json.dumps(commute_fit), from_candidate, now()))
        self.conn.commit()

    def pool_topics(self):
        return self.conn.execute('SELECT * FROM pool_topics ORDER BY created_at').fetchall()

    # --- 개인 메모리 (회고) ---
    def add_reflection(self, client_id, session_id, commute_type, text):
        self.conn.execute('INSERT INTO reflections VALUES(?,?,?,?,?)', (client_id, session_id, commute_type, text, now()))
        self.conn.commit()

    def stories_by_client_since(self, client_id, since):
        return self.conn.execute(
            'SELECT d.story_id, d.preview_text AS text, d.status FROM story_drafts d JOIN story_meta m USING(story_id) '
            'WHERE m.client_id=? AND d.created_at>=? AND d.status IN (\'APPROVED\',\'PUBLISHED\') ORDER BY d.created_at',
            (client_id, since)).fetchall()

    def replies_heard_by_session(self, session_id, client_id):
        return self.conn.execute(
            'SELECT d.story_id, d.preview_text AS text FROM story_deliveries x JOIN story_drafts d USING(story_id) '
            'JOIN story_meta m USING(story_id) JOIN story_meta p ON p.story_id=m.parent_story_id '
            'WHERE x.session_id=? AND p.client_id=? ORDER BY x.delivered_at', (session_id, client_id)).fetchall()

    # --- music notes ---
    def add_note(self, *, client_id, session_id, road_segment_id, road_name, time_bucket, traffic_bucket, track):
        note_id = uid('note')
        self.conn.execute('INSERT INTO music_notes VALUES(?,?,?,?,?,?,?,?,?)',
                          (note_id, client_id, session_id, road_segment_id, road_name, time_bucket, traffic_bucket, track, now()))
        self.conn.commit()
        return note_id

    def note_for(self, client_id, *, road_segment_id, time_bucket, traffic_bucket):
        """Same road segment first; otherwise the same traffic at the same time of day."""
        base = ('SELECT * FROM music_notes n WHERE n.client_id<>? AND NOT EXISTS (SELECT 1 FROM music_deliveries x '
                'WHERE x.note_id=n.note_id AND x.client_id=?) AND ')
        if road_segment_id:
            row = self.conn.execute(base + 'n.road_segment_id=? ORDER BY n.created_at DESC LIMIT 1',
                                    (client_id, client_id, road_segment_id)).fetchone()
            if row:
                return row
        if traffic_bucket == 'JAM':
            return self.conn.execute(base + 'n.traffic_bucket=\'JAM\' AND n.time_bucket=? ORDER BY n.created_at DESC LIMIT 1',
                                     (client_id, client_id, time_bucket)).fetchone()
        return None

    def mark_note_delivered(self, note_id, client_id):
        self.conn.execute('INSERT OR IGNORE INTO music_deliveries VALUES(?,?,?)', (note_id, client_id, now()))
        self.conn.commit()

    def notes_by_session(self, session_id):
        return [r['track'] for r in self.conn.execute('SELECT track FROM music_notes WHERE session_id=?', (session_id,))]

    def notes_heard_by_session(self, client_id, since):
        return [r['track'] for r in self.conn.execute(
            'SELECT n.track FROM music_deliveries x JOIN music_notes n USING(note_id) WHERE x.client_id=? AND x.delivered_at>=?',
            (client_id, since))]

    def stories_by_session(self, session_id):
        return self.conn.execute('SELECT d.story_id, d.preview_text AS text, d.status FROM story_drafts d '
                                 'WHERE d.session_id=? ORDER BY d.created_at', (session_id,)).fetchall()

    def stories_heard_by_session(self, session_id):
        return self.conn.execute('SELECT d.story_id, d.preview_text AS text FROM story_deliveries x JOIN story_drafts d '
                                 'USING(story_id) WHERE x.session_id=? ORDER BY x.delivered_at', (session_id,)).fetchall()

    # --- diaries ---
    def save_diary(self, diary_id, session_id, client_id, data):
        self.conn.execute('INSERT INTO diaries(diary_id,session_id,client_id,created_at,data_json) VALUES(?,?,?,?,?)',
                          (diary_id, session_id, client_id, now(), json.dumps(data, ensure_ascii=False)))
        self.conn.commit()

    def set_diary_image(self, diary_id, path):
        self.conn.execute('UPDATE diaries SET image_path=? WHERE diary_id=?', (path, diary_id)); self.conn.commit()

    def diary(self, diary_id):
        row = self.conn.execute('SELECT * FROM diaries WHERE diary_id=?', (diary_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(row['data_json']), 'diary_id': row['diary_id'], 'created_at': row['created_at'],
                'image_path': row['image_path']}
