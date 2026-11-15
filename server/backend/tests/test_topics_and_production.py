"""Topic selection, research → script production, and auto-advancing broadcast."""
import asyncio, json, random, sys, uuid
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest

from roadpod.engine import Director, MemoryStore, ProtocolError
from roadpod.llm import Completion
from roadpod.producer import EpisodeProducer, Brief, ground_brief, check_script, MAX_SEGMENT_CHARS
from roadpod.topics import Catalog, TopicSelector

CATALOG = Catalog()


def event(sid, typ, **payload):
    return dict(schema_version='1.0', event_id=str(uuid.uuid4()), event_type=typ, session_id=sid,
                timestamp=datetime.now(timezone.utc).isoformat(), payload=payload)


def run(d, sid, typ, **payload):
    return asyncio.run(d.process(sid, event(sid, typ, **payload)))


def ack(d, sid, cmd, status='COMPLETED'):
    return run(d, sid, 'command.ack', command_id=cmd['command_id'], status=status)


def director(seed=1):
    return Director(MemoryStore(), rng=random.Random(seed))


# --- catalog ---

def test_catalog_covers_five_categories_with_competing_angles():
    assert set(CATALOG.categories) == {'MIND', 'RELATION', 'WORK', 'CULTURE', 'SOCIETY'}
    for cat in CATALOG.categories:
        assert sum(t.category == cat for t in CATALOG.topics) >= 5
    for t in CATALOG.topics:
        assert len(t.angles) >= 2 and t.question and t.summary
        assert set(t.commute_fit) == {'TO_WORK', 'FROM_WORK'}
    assert all(t.fresh_sources for t in CATALOG.topics if t.category == 'SOCIETY')


# --- broadcast flow ---

def test_session_started_opens_show_without_a_tap():
    d = director(); sid = d.start('A', 'TO_WORK')
    out = run(d, sid, 'session.started')
    assert out[0]['type'] == 'dj.speak'
    text = out[0]['payload']['text']
    assert '마이큐' in text and '큐큐' in text and text.endswith(d.states[sid].topic.question)  # opens the show, then the question
    assert len(text) <= 480
    assert run(d, sid, 'session.started') == []  # does not restart a running show


def test_episode_auto_advances_then_rolls_into_next_topic():
    d = director(); sid = d.start('A', 'FROM_WORK')
    cmd = run(d, sid, 'user.action', action='select_topic', topic_key='relationships')[0]
    first_eid = d.states[sid].episode_id
    texts = [cmd['payload']['text']]
    for _ in range(10):
        cmd = ack(d, sid, cmd)[0]
        texts.append(cmd['payload']['text'])
        if d.states[sid].episode_id != first_eid:
            break
    assert d.states[sid].episode_id != first_eid, 'should roll into a new episode'
    assert texts[-1].startswith('다음 이야기로 넘어가 볼게요.')
    assert d.states[sid].topic_key != 'relationships'
    assert not any('다른 청취자' in t for t in texts), 'no promise of stories we cannot play yet'
    row = d.store.conn.execute('SELECT * FROM topic_history WHERE episode_id=?', (first_eid,)).fetchone()
    assert row['completed'] == 1 and row['skipped'] == 0 and row['segments_heard'] == len(texts) - 1


def test_segment_cut_by_navigation_is_replayed_after():
    d = director(); sid = d.start('A', 'TO_WORK')
    hook = run(d, sid, 'user.action', action='select_topic', topic_key='focus')[0]
    frame = ack(d, sid, hook)[0]
    assert run(d, sid, 'navigation.guidance_started')[0]['type'] == 'media.pause'
    assert ack(d, sid, frame, 'INTERRUPTED') == []
    out = run(d, sid, 'navigation.guidance_ended')
    assert [c['type'] for c in out] == ['media.resume', 'dj.speak']
    assert out[1]['payload']['text'] == '이어서 들려드릴게요. ' + frame['payload']['text']


def test_segment_finishing_during_navigation_waits_for_it_to_end():
    d = director(); sid = d.start('A', 'TO_WORK')
    hook = run(d, sid, 'user.action', action='select_topic', topic_key='focus')[0]
    run(d, sid, 'navigation.guidance_started')
    assert ack(d, sid, hook) == []           # nothing new while the driver is being guided
    out = run(d, sid, 'navigation.guidance_ended')
    assert out[0]['type'] == 'media.resume' and out[1]['type'] == 'dj.speak'


def test_skip_topic_records_skip_and_changes_topic():
    d = director(); sid = d.start('A', 'TO_WORK')
    hook = run(d, sid, 'user.action', action='select_topic', topic_key='focus')[0]
    eid = d.states[sid].episode_id
    out = run(d, sid, 'user.action', action='skip_topic')
    assert d.states[sid].topic_key != 'focus'
    assert d.store.conn.execute('SELECT skipped FROM topic_history WHERE episode_id=?', (eid,)).fetchone()['skipped'] == 1
    assert out == []  # new hook queued behind the hook still playing
    nxt = ack(d, sid, hook)
    assert nxt[0]['payload']['text'].startswith('다른 이야기로 넘어가 볼게요.')


def test_set_interests_validates_categories():
    d = director(); sid = d.start('A', 'TO_WORK')
    with pytest.raises(ProtocolError):
        run(d, sid, 'user.action', action='set_interests', categories=['SPORTS'])
    assert run(d, sid, 'user.action', action='set_interests', categories=['SOCIETY']) == []
    for _ in range(5):
        run(d, sid, 'user.action', action='skip_topic')
        assert d.states[sid].topic.category == 'SOCIETY'


# --- selection ---

def _history(store, client, commute, topic_key, **flags):
    t = CATALOG.get(topic_key); eid = 'ep-' + uuid.uuid4().hex[:8]
    store.start_topic(eid, client, commute, t)
    store.conn.execute('UPDATE topic_history SET segments_heard=?, completed=?, engaged=?, skipped=? WHERE episode_id=?',
                       (flags.get('heard', 1), flags.get('completed', 0), flags.get('engaged', 0), flags.get('skipped', 0), eid))
    store.conn.commit()


def test_commute_memories_are_separate_and_signals_have_direction():
    store = MemoryStore(); sel = TopicSelector(CATALOG, store, rng=random.Random(0))
    _history(store, 'A', 'TO_WORK', 'four_day_week', skipped=1, heard=1)
    _history(store, 'A', 'TO_WORK', 'ai_jobs', skipped=1, heard=0)
    _history(store, 'A', 'TO_WORK', 'endurance', completed=1, engaged=1, heard=6)
    work = sel.category_affinity('A', 'TO_WORK')
    assert work['SOCIETY'] < 0 < work['RELATION']
    assert sel.category_affinity('A', 'FROM_WORK') == {}


def test_selector_prefers_liked_category_and_avoids_recent_repeats():
    store = MemoryStore(); sel = TopicSelector(CATALOG, store, rng=random.Random(3), explore_rate=0)
    for key in ('endurance', 'saying_no'):
        _history(store, 'A', 'FROM_WORK', key, completed=1, engaged=1, heard=6)
    picks = [sel.pick('A', 'FROM_WORK') for _ in range(40)]
    assert all(p.key not in ('endurance', 'saying_no') for p in picks)
    assert sum(p.category == 'RELATION' for p in picks) > 20


def test_selector_respects_remaining_drive_time():
    sel = TopicSelector(CATALOG, MemoryStore(), rng=random.Random(0), explore_rate=0)
    for _ in range(20):
        assert sel.pick('A', 'TO_WORK', eta_seconds=330).min_minutes <= 5


def test_exploration_goes_to_least_heard_category():
    store = MemoryStore(); sel = TopicSelector(CATALOG, store, rng=random.Random(0))
    for key in ('focus', 'anxiety', 'relationships', 'procrastination', 'books'):
        _history(store, 'A', 'TO_WORK', key, completed=1)
    assert sel.pick('A', 'TO_WORK', explore=True).category == 'SOCIETY'


# --- grounding & script checks ---

FRESH = CATALOG.get('four_day_week'); EVERGREEN = CATALOG.get('focus')
TODAY = date(2026, 11, 11)


def test_grounding_keeps_only_facts_the_search_returned():
    raw = {'facts': [
        {'id': 'F1', 'claim': 'A', 'source_url': 'https://www.example.org/report/', 'published': '2026-05-01'},
        {'id': 'F2', 'claim': 'B', 'source_url': 'https://made-up.example/x', 'published': '2026-05-01'},
        {'id': 'F3', 'claim': 'C', 'source_url': 'https://news.example.com/a', 'published': '2026-09-01'},
    ]}
    cited = ['https://example.org/report?utm_source=openai', 'https://news.example.com/a']
    b = ground_brief(raw, cited, FRESH, today=TODAY)
    assert [f['id'] for f in b.facts] == ['F1', 'F3'] and b.mode == 'researched'


def test_fresh_topic_needs_recent_dates_and_two_sources():
    old = {'facts': [{'id': 'F1', 'claim': 'A', 'source_url': 'https://a.org/1', 'published': '2023-01-01'},
                     {'id': 'F2', 'claim': 'B', 'source_url': 'https://b.org/1', 'published': None}]}
    b = ground_brief(old, ['https://a.org/1', 'https://b.org/1'], FRESH, today=TODAY)
    assert b.facts == [] and b.mode == 'discussion_only' and b.perspectives
    one_domain = {'facts': [{'id': 'F1', 'claim': 'A', 'source_url': 'https://a.org/1', 'published': '2026-10-01'},
                            {'id': 'F2', 'claim': 'B', 'source_url': 'https://a.org/2', 'published': '2026-10-01'}]}
    assert ground_brief(one_domain, ['https://a.org/1', 'https://a.org/2'], FRESH, today=TODAY).facts == []
    # Evergreen topics accept a single undated but retrieved source.
    ev = {'facts': [{'id': 'F1', 'claim': 'A', 'source_url': 'https://a.org/1', 'published': None}]}
    assert len(ground_brief(ev, ['https://a.org/1'], EVERGREEN, today=TODAY).facts) == 1


def test_script_check_drops_unsourced_numbers_and_unknown_refs():
    brief = Brief([{'id': 'F1', 'claim': 'x', 'source_title': 't', 'source_url': 'https://a.org', 'published': None}], [], [], 'researched')
    raw = {'segments': [
        {'role': 'FRAME', 'text': '오늘은 이 질문을 천천히 따라가 볼게요.', 'source_refs': []},
        {'role': 'PERSPECTIVE', 'text': '한 조사에 따르면 직장인의 63%가 그렇다고 해요.', 'source_refs': []},
        {'role': 'PERSPECTIVE', 'text': '어느 연구 결과를 보면 그렇대요.', 'source_refs': ['F9']},
        {'role': 'PERSPECTIVE', 'text': 'A기관 조사에 따르면 41%가 그렇게 답했어요.', 'source_refs': ['F1']},
        {'role': 'PERSPECTIVE', 'text': '다른 한편으로는 이렇게 볼 수도 있어요. ' * 30, 'source_refs': []},
        {'role': 'STORY_SLOT', 'text': '청취자 사연을 들려드릴게요.', 'source_refs': []},
    ]}
    segs = check_script(raw, brief)
    texts = [s['text'] for s in segs]
    assert not any('63%' in t or '어느 연구' in t or '사연' in t for t in texts)
    assert any('41%' in t for t in texts)
    assert all(len(t) <= MAX_SEGMENT_CHARS for t in texts)
    assert segs[-1]['role'] == 'OUTRO'


# --- producer with a fake live LLM ---

class FakeLLM:
    live = True
    research_model = writer_model = 'fake'

    def __init__(self, research=None, writer=None, delay=0):
        self.research, self.writer, self.delay, self.calls = research, writer, delay, []

    async def generate(self, system, user, fallback):
        return fallback

    async def complete(self, system, user, *, max_tokens, timeout, web_search=False, model=None, **_):
        self.calls.append('research' if web_search else 'writer')
        await asyncio.sleep(self.delay)
        return self.research if web_search else self.writer


RESEARCH = Completion(json.dumps({
    'facts': [{'id': 'F1', 'claim': '알림이 집중을 끊는다는 연구가 있다.', 'source_title': 'Study', 'source_url': 'https://a.org/s', 'published': '2020-01-01'}],
    'perspectives': [{'id': 'P1', 'position': '환경의 문제', 'reasoning': '...'}], 'open_questions': ['당신의 알림은?']}),
    cited_urls=['https://a.org/s'])
WRITTEN = Completion(json.dumps({'segments': [
    {'role': 'FRAME', 'text': '집중이 흔들리는 순간을 떠올려 볼게요.', 'source_refs': []},
    {'role': 'PERSPECTIVE', 'text': 'Study의 연구 결과를 보면 알림이 집중을 끊는다고 해요.', 'source_refs': ['F1']},
    {'role': 'PERSPECTIVE', 'text': '반대로 의지의 문제라고 보는 사람도 있어요.', 'source_refs': []},
    {'role': 'OUTRO', 'text': '오늘은 여기까지예요.', 'source_refs': []}]}))


def test_producer_researches_writes_and_caches():
    store = MemoryStore(); llm = FakeLLM(RESEARCH, WRITTEN); prod = EpisodeProducer(store, llm)
    segs = asyncio.run(prod.produce(EVERGREEN, 'TO_WORK'))
    assert segs[1]['source_refs'] == ['F1'] and store.get_script('focus', 'TO_WORK')['mode'] == 'researched'
    asyncio.run(prod.produce(EVERGREEN, 'TO_WORK'))
    asyncio.run(prod.produce(EVERGREEN, 'FROM_WORK'))  # new script, but research brief is reused
    # Research runs once and is shared; each short fake script gets exactly one retry for length.
    assert llm.calls == ['research', 'writer', 'writer', 'writer', 'writer']


def test_search_result_list_counts_as_retrieved_sources():
    from types import SimpleNamespace as NS
    from roadpod.llm import _cited_urls
    answer = NS(output=[NS(type='web_search_call', action=NS(sources=[NS(url='https://a.org/x?utm_source=openai')])),
                        NS(type='message', content=[NS(annotations=[NS(type='url_citation', url='https://b.org/y')])])])
    assert _cited_urls(answer) == ['https://a.org/x?utm_source=openai', 'https://b.org/y']


def test_unverified_research_reasoning_never_reaches_writer():
    raw = {'facts': [], 'perspectives': [{'id': 'P1', 'position': '지역마다 다르다', 'reasoning': '충남과 전북이 도입했다'}]}
    b = ground_brief(raw, [], FRESH, today=TODAY)
    assert b.perspectives == [{'id': 'P1', 'position': '지역마다 다르다', 'reasoning': ''}]


def test_producer_degrades_instead_of_failing():
    store = MemoryStore(); prod = EpisodeProducer(store, FakeLLM(None, None))
    segs = asyncio.run(prod.produce(FRESH, 'TO_WORK'))
    assert segs[0]['role'] == 'FRAME' and store.get_script('four_day_week', 'TO_WORK')['mode'] == 'catalog'
    assert store.get_brief('four_day_week')['brief']['mode'] == 'discussion_only'


def test_body_researched_in_background_is_pushed_when_ready():
    async def scenario():
        d = Director(MemoryStore(), FakeLLM(RESEARCH, WRITTEN, delay=0.05), rng=random.Random(0))
        sid = d.start('A', 'TO_WORK'); pushed = []
        async def send(c): pushed.append(c)
        d.attach(sid, send)
        hook = (await d.process(sid, event(sid, 'user.action', action='select_topic', topic_key='focus')))[0]
        bridge = (await d.process(sid, event(sid, 'command.ack', command_id=hook['command_id'], status='COMPLETED')))[0]
        assert CATALOG.get('focus').summary in bridge['payload']['text']   # no silence while research runs
        assert await d.process(sid, event(sid, 'command.ack', command_id=bridge['command_id'], status='COMPLETED')) == []
        assert d.states[sid].waiting
        await asyncio.gather(*d._tasks)
        return pushed
    pushed = asyncio.run(scenario())
    assert pushed and pushed[0]['payload']['text'] == '집중이 흔들리는 순간을 떠올려 볼게요.'


def test_slow_research_falls_back_to_catalog_instead_of_silence():
    async def scenario():
        d = Director(MemoryStore(), FakeLLM(RESEARCH, WRITTEN, delay=0.3), rng=random.Random(0))
        d.body_wait = 0.05   # research (0.3 s × 2 calls) is far slower than the wait budget
        sid = d.start('A', 'TO_WORK'); pushed = []
        async def send(c): pushed.append(c)
        d.attach(sid, send)
        hook = (await d.process(sid, event(sid, 'user.action', action='select_topic', topic_key='focus')))[0]
        bridge = (await d.process(sid, event(sid, 'command.ack', command_id=hook['command_id'], status='COMPLETED')))[0]
        assert await d.process(sid, event(sid, 'command.ack', command_id=bridge['command_id'], status='COMPLETED')) == []
        await asyncio.gather(*d._tasks)
        first_pushed = list(pushed)
        await asyncio.gather(*d.producer._inflight.values())   # research keeps going after the fallback
        return first_pushed, d
    pushed, d = asyncio.run(scenario())
    summary = CATALOG.get('focus').summary
    # No silence: the catalog script airs, starting at its first perspective because the bridge already framed the topic.
    assert pushed and pushed[0]['payload']['text'].startswith('첫 번째로 생각해 볼 것은')
    assert not any(summary in c['payload']['text'] for c in pushed)
    assert d.producer.cached(CATALOG.get('focus'), 'TO_WORK')[0]['text'] == '집중이 흔들리는 순간을 떠올려 볼게요.'  # cached for next time


def test_next_topic_is_prefetched_and_used_on_skip():
    async def scenario():
        llm = FakeLLM(RESEARCH, WRITTEN)
        d = Director(MemoryStore(), llm, rng=random.Random(0))
        sid = d.start('A', 'TO_WORK')
        await d.process(sid, event(sid, 'user.action', action='select_topic', topic_key='focus'))
        planned = d.states[sid].next_topic
        await asyncio.gather(*d._tasks)          # body + next topic produced in the background
        assert planned and d.producer.cached(planned, 'TO_WORK') is not None
        await d.process(sid, event(sid, 'user.action', action='skip_topic'))
        return d.states[sid], planned
    state, planned = asyncio.run(scenario())
    assert state.topic_key == planned.key and state.content_ready   # no wait: body was already cached


def test_bridge_is_used_once_per_episode():
    async def scenario():
        d = Director(MemoryStore(), FakeLLM(RESEARCH, WRITTEN, delay=0.2), rng=random.Random(0))
        sid = d.start('A', 'TO_WORK')
        hook = (await d.process(sid, event(sid, 'user.action', action='select_topic', topic_key='focus')))[0]
        bridge = (await d.process(sid, event(sid, 'command.ack', command_id=hook['command_id'], status='COMPLETED')))[0]
        again = await d.process(sid, event(sid, 'command.ack', command_id=bridge['command_id'], status='COMPLETED'))
        for t in list(d._tasks): t.cancel()
        return again
    assert asyncio.run(scenario()) == []


# --- regressions from code review: paths where the car could go silent ---

def _ctx(guidance):
    return dict(commute_type='TO_WORK', source_mode='SIMULATED',
                vehicle={'motion_state': 'MOVING', 'speed_kmh': 30, 'wiper_active': False, 'parked_confirmed': False},
                navigation={'road_name': None, 'road_segment_id': None, 'eta_seconds': 1200, 'guidance_active': guidance},
                traffic={'congestion': 'UNKNOWN', 'verified': False}, weather={'condition': 'UNKNOWN', 'verified': False})


def test_nav_ending_via_context_update_resumes_show():
    d = director(); sid = d.start('A', 'TO_WORK')
    hook = run(d, sid, 'user.action', action='select_topic', topic_key='focus')[0]
    assert run(d, sid, 'context.updated', **_ctx(True)) == []
    assert ack(d, sid, hook) == []
    out = run(d, sid, 'context.updated', **_ctx(False))
    assert [c['type'] for c in out] == ['media.resume', 'dj.speak']


def test_lost_ack_does_not_silence_the_car_forever():
    d = director(); sid = d.start('A', 'TO_WORK')
    run(d, sid, 'user.action', action='select_topic', topic_key='focus')   # hook ACK never arrives
    d.states[sid].playing_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    out = run(d, sid, 'context.updated', **_ctx(False))
    assert out and out[0]['type'] == 'dj.speak' and out[0]['payload']['text'] != ''


def test_long_segment_is_not_presumed_lost_while_playing():
    d = director(); sid = d.start('A', 'TO_WORK')
    run(d, sid, 'user.action', action='select_topic', topic_key='focus')
    s = d.states[sid]
    assert (s.playing_deadline - datetime.now(timezone.utc)).total_seconds() > 30
    assert run(d, sid, 'context.updated', **_ctx(False)) == []


def test_topic_switch_drops_queued_reply_for_old_topic():
    d = director(); sid = d.start('A', 'TO_WORK')
    hook = run(d, sid, 'user.action', action='select_topic', topic_key='focus')[0]
    run(d, sid, 'user.utterance', text='저는 반대예요', is_final=True)       # reply about 'focus' queued
    run(d, sid, 'user.action', action='select_topic', topic_key='relationships')
    nxt = ack(d, sid, hook)[0]
    assert '무례한 사람' in nxt['payload']['text'] and '집중' not in nxt['payload']['text']


def test_session_ended_releases_state():
    d = director(); sid = d.start('A', 'TO_WORK')
    run(d, sid, 'session.started')
    assert run(d, sid, 'session.ended') == [] and sid not in d.states


def test_push_to_closed_socket_does_not_crash_background_work():
    async def scenario():
        d = Director(MemoryStore(), FakeLLM(RESEARCH, WRITTEN, delay=0.02), rng=random.Random(0))
        sid = d.start('A', 'TO_WORK')
        async def broken(c): raise RuntimeError('socket closed')
        d.attach(sid, broken)
        hook = (await d.process(sid, event(sid, 'user.action', action='select_topic', topic_key='focus')))[0]
        await d.process(sid, event(sid, 'command.ack', command_id=hook['command_id'], status='COMPLETED'))
        results = await asyncio.gather(*d._tasks, return_exceptions=True)
        return results
    assert not any(isinstance(r, Exception) for r in asyncio.run(scenario()))


# --- regressions from the live OpenAI run ---

def test_blogs_are_not_sources_and_one_source_cannot_flood_the_brief():
    raw = {'facts': [{'id': 'B1', 'claim': '블로그 주장', 'source_url': 'https://rosly.tistory.com/102', 'published': '2026-04-07'}]
                    + [{'id': f'N{i}', 'claim': f'기사 내용 {i}', 'source_url': 'https://www.yna.co.kr/view/A', 'published': '2026-01-01'} for i in range(6)]}
    b = ground_brief(raw, ['https://rosly.tistory.com/102?utm_source=openai', 'https://www.yna.co.kr/view/A'], EVERGREEN, today=TODAY)
    assert [f['id'] for f in b.facts] == ['N0', 'N1', 'N2'] and b.facts[0]['publisher'] == 'yna.co.kr'


def test_fact_ids_are_never_spoken():
    brief = Brief([{'id': 'F1', 'claim': 'x', 'source_title': 't', 'source_url': 'https://a.org', 'published': None}], [], [], 'researched')
    raw = {'segments': [{'role': 'FRAME', 'text': '시작해 볼게요.', 'source_refs': []},
                        {'role': 'PERSPECTIVE', 'text': 'A일보 보도에 따르면 알림이 몰입을 끊는다고 해요(F1). 또 다른 말도 있죠 (F1, F2).', 'source_refs': ['F1']},
                        {'role': 'OUTRO', 'text': '끝.', 'source_refs': []}]}
    texts = ' '.join(s['text'] for s in check_script(raw, brief))
    assert 'F1' not in texts and 'F2' not in texts and '해요.' in texts


def test_web_addresses_in_parentheses_are_not_spoken():
    brief = Brief([{'id': 'F1', 'claim': 'x', 'source_title': 't', 'source_url': 'https://a.org', 'published': None}], [], [], 'researched')
    raw = {'segments': [{'role': 'FRAME', 'text': '시작해 볼게요(관점 하나).', 'source_refs': []},
                        {'role': 'PERSPECTIVE', 'text': '사직률이 줄었다는 보도가 있었죠(m.news.nate.com, 2026). 재정 부담도 있었어요(fnnews.com).', 'source_refs': ['F1']},
                        {'role': 'OUTRO', 'text': '끝.', 'source_refs': []}]}
    texts = ' '.join(s['text'] for s in check_script(raw, brief))
    assert '.com' not in texts and '2026)' not in texts and '(관점 하나)' in texts


def test_fresh_topic_accepts_two_year_old_dated_news():
    raw = {'facts': [{'id': 'F1', 'claim': 'A', 'source_url': 'https://www.yna.co.kr/a', 'published': '2025-02-17'},
                     {'id': 'F2', 'claim': 'B', 'source_url': 'https://www.hankookilbo.com/b', 'published': '2025-11-07'}]}
    b = ground_brief(raw, ['https://www.yna.co.kr/a', 'https://www.hankookilbo.com/b'], FRESH, today=TODAY)
    assert b.mode == 'researched' and len(b.facts) == 2


# --- radio opening ---

def test_opening_matches_time_of_day_and_commute():
    from roadpod.openings import opening_line, KST
    rng = random.Random(0)
    morning = datetime(2026, 11, 9, 8, 10, tzinfo=KST)   # Monday
    night = datetime(2026, 11, 13, 22, 30, tzinfo=KST)   # Friday
    for _ in range(10):
        m = opening_line('TO_WORK', rng, now=morning)
        assert '좋은 아침' in m or '출근길' in m
        assert '월요일' in m
        n = opening_line('TO_WORK', rng, now=night)
        assert '출근길' not in n and '아침' not in n          # clock beats the commute label at 10 pm
        assert '금요일' in n
    assert '비가 오네요' in opening_line('FROM_WORK', rng, weather='RAIN', now=night)
    assert '비' not in opening_line('FROM_WORK', random.Random(1), weather=None, now=night)


def test_only_the_first_episode_opens_the_show():
    d = director(); sid = d.start('A', 'TO_WORK')
    first = run(d, sid, 'user.action', action='select_topic', topic_key='focus')[0]['payload']['text']
    run(d, sid, 'user.action', action='select_topic', topic_key='relationships')
    second = d.store.segment(d.states[sid].episode_id, 0)['text']
    assert '마이큐' in first and '마이큐' not in second and second.endswith('무례한 사람을 꼭 내 편으로 만들어야 할까?')


# --- script labels must never be spoken ---

def test_sentences_naming_script_labels_are_dropped():
    raw = {'segments': [
        {'role': 'FRAME', 'text': 'HOOK은 이미 던져졌죠. 출근길에 불안을 떠올려 볼게요.', 'source_refs': []},
        {'role': 'PERSPECTIVE', 'text': '첫 번째 PERSPECTIVE입니다. 불안은 신호일 수 있어요.', 'source_refs': []},
        {'role': 'PERSPECTIVE', 'text': '반대로 불안은 줄여야 할 대상이기도 해요.', 'source_refs': []},
        {'role': 'OUTRO', 'text': '이번 세그먼트는 여기까지.', 'source_refs': []}]}
    texts = [s['text'] for s in check_script(raw, Brief([], [], [], 'discussion_only'))]
    assert texts[:3] == ['출근길에 불안을 떠올려 볼게요.', '불안은 신호일 수 있어요.', '반대로 불안은 줄여야 할 대상이기도 해요.']
    assert not any('HOOK' in t or 'PERSPECTIVE' in t or '세그먼트' in t for t in texts)   # emptied OUTRO replaced by default


def test_cached_script_saved_before_the_filter_is_cleaned_on_read():
    store = MemoryStore(); prod = EpisodeProducer(store, FakeLLM())
    store.save_script('focus', 'TO_WORK', [{'role': 'FRAME', 'text': 'HOOK은 이미 던져졌죠. 집중 이야기를 해 볼게요.', 'source_refs': []}], 'researched')
    assert prod.cached(EVERGREEN, 'TO_WORK')[0]['text'] == '집중 이야기를 해 볼게요.'
