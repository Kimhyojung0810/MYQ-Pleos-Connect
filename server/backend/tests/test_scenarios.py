"""Behaviour the two demo scenarios (출근길 A / 퇴근길 B) depend on."""
import asyncio, random, uuid
from datetime import datetime, timedelta, timezone

from roadpod.engine import Director, MemoryStore
from roadpod import diary as diarylib, relay

T0 = datetime(2026, 11, 12, 7, 30, tzinfo=timezone(timedelta(hours=9)))


def event(sid, typ, **payload):
    return dict(schema_version='1.0', event_id=str(uuid.uuid4()), event_type=typ, session_id=sid,
                timestamp=datetime.now(timezone.utc).isoformat(), payload=payload)


def ctx(*, speed=60, stopped=False, eta=4800, road=None, segment=None, jam=False, rain=False, wiper=False,
        parked=False, commute='TO_WORK'):
    return dict(commute_type=commute,
                vehicle={'motion_state': 'PARKED' if parked else 'STOPPED' if stopped else 'MOVING',
                         'speed_kmh': 0 if (stopped or parked) else speed, 'wiper_active': wiper, 'parked_confirmed': parked},
                navigation={'road_name': road, 'road_segment_id': segment, 'eta_seconds': eta, 'guidance_active': False},
                traffic={'congestion': 'JAM' if jam else 'FREE', 'verified': True},
                weather={'condition': 'RAIN' if rain else 'CLEAR', 'verified': True}, source_mode='SIMULATED')


class Clock:
    def __init__(self):
        self.t = T0
    def __call__(self):
        return self.t
    def at(self, hh, mm):
        self.t = self.t.replace(hour=hh, minute=mm)


def director():
    d = Director(MemoryStore(), rng=random.Random(3))
    d.clock = Clock()
    return d


async def send(d, sid, typ, **payload):
    return await d.process(sid, event(sid, typ, **payload))


def free(d, sid):
    """Pretend the car finished whatever it was saying."""
    d.states[sid].playing_command_id = None
    d.states[sid].pending.clear()


def test_voice_skip_changes_topic():
    async def go():
        d = director(); sid = d.start('A', 'TO_WORK')
        await send(d, sid, 'session.started', topic_key='relationships'); free(d, sid)
        out = await send(d, sid, 'user.utterance', text='큐큐야, 다른 거', is_final=True)
        return d.states[sid], out
    s, out = asyncio.run(go())
    assert s.topic_key != 'relationships' and out[0]['payload']['text'].startswith('다른 이야기로 넘어가 볼게요.')


def test_spoken_story_at_a_stop_is_read_back_and_sent_by_voice():
    said = '아 근데 운전하면서 생각해 봤는데… 큐큐야, 내 생각 사연으로 남겨 둘래. 나는 무례한 사람을 꼭 내 편으로 만들어야 할까? 그런 생각도 들어. 어렵긴 한데 암튼 그래.'
    async def go():
        d = director(); sid = d.start('A', 'TO_WORK')
        await send(d, sid, 'session.started', topic_key='relationships')
        await send(d, sid, 'context.updated', **ctx(stopped=True)); free(d, sid)
        ask = await send(d, sid, 'user.utterance', text=said, is_final=True)
        free(d, sid)
        sent = await send(d, sid, 'user.utterance', text='응. 보내', is_final=True)
        await asyncio.gather(*d._tasks)
        return d, ask, sent
    d, ask, sent = asyncio.run(go())
    line = ask[0]['payload']['text']
    assert ask[0]['type'] == 'dj.speak' and line.startswith(relay.EMPATHY_FALLBACK) and line.endswith('보낼까요?')
    assert '사연으로 남겨' not in line and '무례한 사람' in line      # the request words are not part of the story
    assert sent[0]['payload']['text'] == relay.SENT_FALLBACK
    story = d.store.conn.execute("SELECT status FROM story_drafts").fetchone()
    assert story['status'] == 'PUBLISHED'
    assert d.stories.candidates()   # the story's question is offered to the common topic pool


def test_question_dropped_when_car_moves_and_preview_waits_for_parking():
    async def go():
        d = director(); sid = d.start('A', 'TO_WORK')
        await send(d, sid, 'context.updated', **ctx(stopped=True))
        await send(d, sid, 'user.utterance', is_final=True,
                   text='큐큐야 사연으로 남겨 줘. 오늘 회의에서 내 말이 끝까지 안 들린 것 같아서 좀 속상했어. 다음엔 천천히 다시 말해 보려고.')
        assert d.states[sid].awaiting_approval
        moved = await send(d, sid, 'context.updated', **ctx(speed=30))
        return d.states[sid], moved
    s, moved = asyncio.run(go())
    assert s.awaiting_approval is None
    assert moved[0]['type'] == 'story.preview'


def test_rain_and_jam_briefing_names_road_and_delay():
    async def go():
        d = director(); sid = d.start('A', 'TO_WORK')
        await send(d, sid, 'context.updated', **ctx(eta=4800, road='경인고속도로'))
        d.clock.at(8, 15)                       # 45 min later; on time would be ETA 2100 s
        return await send(d, sid, 'context.updated', **ctx(eta=2700, road='올림픽대로', jam=True, rain=True, wiper=True))
    out = asyncio.run(go())
    assert out[0]['payload']['text'] == '올림픽대로가 비 때문에 조금 느려요. 도착은 10분쯤 늦어질 것 같아요.'


def test_story_corner_airs_a_story_with_its_reply():
    async def go():
        d = director()
        a = d.start('writer', 'TO_WORK'); b = d.start('replier', 'TO_WORK')
        await send(d, a, 'session.started', topic_key='relationships')
        root = d.store.add_draft(a, None, '팀장님 말투 때문에 회의만 하면 작아져요.')
        d.stories.add_meta(root, client_id='writer', session_id=a, topic_key='relationships', category='RELATION',
                           commute_type='TO_WORK', congestion=None, weather=None, road_segment_id=None, raw_text='x')
        d.stories.set_status(root, 'PUBLISHED')
        rep = d.store.add_draft(b, None, '저는 그 말투를 그 사람의 습관이라고 생각하기로 했어요.')
        d.stories.add_meta(rep, client_id='replier', session_id=b, topic_key='relationships', category='RELATION',
                           commute_type='TO_WORK', congestion=None, weather=None, road_segment_id=None, raw_text='y',
                           parent_story_id=root, relation_type='FOLLOW_UP')
        d.stories.set_status(rep, 'PUBLISHED')
        h = d.start('haeun', 'TO_WORK')
        c = (await send(d, h, 'session.started', topic_key='relationships'))[0]
        for _ in range(30):
            if c['type'] == 'story.incoming':
                break
            c = (await send(d, h, 'command.ack', command_id=c['command_id'], status='COMPLETED'))[0]
        reply = (await send(d, h, 'command.ack', command_id=c['command_id'], status='COMPLETED'))[0]
        comment = (await send(d, h, 'command.ack', command_id=reply['command_id'], status='COMPLETED'))[0]
        return c, reply, comment
    story, reply, comment = asyncio.run(go())
    assert story['payload']['text'].startswith(relay.SLOT_LEAD) and '팀장님' in story['payload']['text']
    assert reply['type'] == 'story.incoming' and reply['payload']['text'].startswith(relay.SLOT_REPLY_LEAD)
    # 큐큐 does not stop at reading the reply: its own comment follows
    assert comment['type'] == 'dj.speak' and comment['payload']['text'] == relay.REPLY_COMMENT_FALLBACK['slot']


def test_morning_story_replies_greet_the_evening_show():
    async def go():
        d = director()
        m = d.start('haeun', 'TO_WORK')
        root = d.store.add_draft(m, None, '무례한 사람을 꼭 내 편으로 만들어야 할까요?')
        d.stories.add_meta(root, client_id='haeun', session_id=m, topic_key='relationships', category='RELATION',
                           commute_type='TO_WORK', congestion=None, weather=None, road_segment_id=None, raw_text='x')
        d.stories.set_status(root, 'PUBLISHED')
        for who, words in (('p1', '저도 요즘 모두와 잘 지낼 필요는 없다는 걸 배우는 중이에요.'), ('p2', '내 편까진 아니어도, 적이 아니면 충분하대요.')):
            sid = d.start(who, 'TO_WORK')
            r = d.store.add_draft(sid, None, words)
            d.stories.add_meta(r, client_id=who, session_id=sid, topic_key='relationships', category='RELATION',
                               commute_type='TO_WORK', congestion=None, weather=None, road_segment_id=None, raw_text=words,
                               parent_story_id=root, relation_type='EMPATHY')
            d.stories.set_status(r, 'PUBLISHED')
        e = d.start('haeun', 'FROM_WORK')
        c = (await send(d, e, 'session.started', topic_key='work_off'))[0]
        first = (await send(d, e, 'command.ack', command_id=c['command_id'], status='COMPLETED'))[0]
        second = (await send(d, e, 'command.ack', command_id=first['command_id'], status='COMPLETED'))[0]
        third = (await send(d, e, 'command.ack', command_id=second['command_id'], status='COMPLETED'))[0]
        return first, second, third
    first, second, third = asyncio.run(go())
    assert first['payload']['text'].startswith('아침에 남겨 주신 사연에 답장이 왔어요.')
    assert second['payload']['text'].startswith(relay.AUTHOR_NEXT_LEAD)
    assert third['type'] == 'dj.speak' and third['payload']['text'] == relay.REPLY_COMMENT_FALLBACK['author']


def test_evening_reflection_only_when_stopped_in_jam_and_kept_with_consent():
    async def go():
        d = director(); sid = d.start('haeun', 'FROM_WORK')
        await send(d, sid, 'user.action', action='set_memory_consent', enabled=True)
        moving = await send(d, sid, 'context.updated', **ctx(jam=True, commute='FROM_WORK'))
        free(d, sid)
        d.clock.at(20, 15)
        asked = await send(d, sid, 'context.updated', **ctx(jam=True, stopped=True, commute='FROM_WORK'))
        await send(d, sid, 'command.ack', command_id=asked[-1]['command_id'], status='COMPLETED')
        free(d, sid)
        answer = await send(d, sid, 'user.utterance', text='아침에 사연 보낸 거. 그거 생각보다 좋았어.', is_final=True)
        return d, moving, asked, answer
    d, moving, asked, answer = asyncio.run(go())
    assert not any(c['payload'].get('text') == relay.REFLECTION_QUESTION for c in moving)
    assert asked[-1]['payload']['text'] == relay.REFLECTION_QUESTION
    assert answer[0]['payload']['text'] == relay.REFLECTION_FALLBACK
    assert d.store.conn.execute('SELECT text FROM reflections').fetchone()['text'].startswith('아침에 사연')


def test_drop_the_song_that_is_playing():
    async def go():
        d = director(); sid = d.start('haeun', 'FROM_WORK')
        await send(d, sid, 'context.updated', **ctx(jam=True, road='경인고속도로', segment='gyeongin-up-12', commute='FROM_WORK'))
        await send(d, sid, 'user.action', action='now_playing', track='밤편지 - 아이유')
        free(d, sid)
        return d, await send(d, sid, 'user.utterance', text='큐큐야, 이 노래 드랍해줘.', is_final=True)
    d, out = asyncio.run(go())
    assert '밤편지 - 아이유' in out[0]['payload']['text']
    note = d.store.conn.execute('SELECT * FROM music_notes').fetchone()
    assert note['track'] == '밤편지 - 아이유' and note['traffic_bucket'] == 'JAM' and note['time_bucket'] == 'MORNING'


def test_adopted_candidate_can_open_another_listeners_show():
    async def go():
        d = director(); a = d.start('haeun', 'TO_WORK')
        sid_story = d.store.add_draft(a, None, '무례한 사람을 꼭 내 편으로 만들어야 할까요?')
        d.stories.add_meta(sid_story, client_id='haeun', session_id=a, topic_key='relationships', category='RELATION',
                           commute_type='TO_WORK', congestion=None, weather=None, road_segment_id=None, raw_text='x')
        d.stories.add_candidate(sid_story, '모두와 잘 지내지 않아도 괜찮을까?')
        cand = d.stories.candidates()[0]['candidate_id']
        key = d.adopt_candidate(cand)
        other = d.start('someone', 'TO_WORK')
        hook = (await send(d, other, 'session.started', topic_key=key))[0]
        return d, key, hook
    d, key, hook = asyncio.run(go())
    assert d.catalog.get(key).category == 'RELATION'
    assert '모두와 잘 지내지 않아도 괜찮을까?' in hook['payload']['text']
    assert d.stories.candidates()[0]['status'] == 'ADOPTED'


def test_diary_headline_prefers_the_drivers_own_question():
    f = diarylib.facts_of(commute_type='TO_WORK', topics=['무례한 사람을 꼭 내 편으로 만들어야 할까?'], said=[],
                          sent=['무례한 사람을 꼭 내 편으로 만들어야 할까요? 내 편보다 중요한 건 내 마음이 아닐까요?'],
                          heard=[], notes_left=[], notes_heard=[], weather=None, congestion=None)
    assert diarylib.headline(f) == '무례한 사람을 꼭 내 편으로 만들어야 할까요?'


def test_show_waits_for_the_answer_then_moves_on_if_none_comes():
    async def go():
        d = director(); d.answer_wait = 0.01
        sid = d.start('A', 'TO_WORK'); pushed = []
        async def car(c): pushed.append(c)
        d.attach(sid, car)
        await send(d, sid, 'context.updated', **ctx(stopped=True))
        hook = (await send(d, sid, 'session.started', topic_key='relationships'))[0]
        await send(d, sid, 'user.utterance', is_final=True,
                   text='큐큐야, 내 생각 사연으로 남겨 둘래. 모두와 잘 지내지 않아도 괜찮다는 걸 요즘 조금씩 배우고 있어요.')
        ask = (await send(d, sid, 'command.ack', command_id=hook['command_id'], status='COMPLETED'))[0]
        quiet = await send(d, sid, 'command.ack', command_id=ask['command_id'], status='COMPLETED')
        await asyncio.sleep(0.05)
        return ask, quiet, pushed
    ask, quiet, pushed = asyncio.run(go())
    assert ask['payload']['text'].endswith('보낼까요?') and quiet == []      # air left for the answer
    assert pushed[0]['type'] == 'story.preview' and pushed[1]['type'] == 'dj.speak'   # then the show goes on


# --- 음악 코너 (C) ---

def test_music_break_between_corners_then_drop_that_song():
    async def go():
        d = director(); d.music_every = 2
        sid = d.start('haeun', 'TO_WORK')
        await send(d, sid, 'context.updated', **ctx(road='경인고속도로', segment='gyeongin-up-03'))
        c = (await send(d, sid, 'session.started', topic_key='relationships'))[0]
        seen = [c]
        for _ in range(12):
            c = (await send(d, sid, 'command.ack', command_id=c['command_id'], status='COMPLETED'))[0]
            seen.append(c)
            if c['type'] == 'music.play':
                break
        free(d, sid)
        dropped = await send(d, sid, 'user.utterance', text='큐큐야, 이 노래 드랍해줘.', is_final=True)
        return d, seen, dropped
    d, seen, dropped = asyncio.run(go())
    music = seen[-1]
    assert music['type'] == 'music.play' and music['payload']['url'].endswith('/music/' + music['payload']['track_id'] + '.mp3')
    assert music['payload']['track_id'].startswith('am-')                    # a morning track on the way to work
    assert seen[-2]['type'] == 'dj.speak' and music['payload']['title'] in seen[-2]['payload']['text']   # announced first
    assert music['payload']['title'] in dropped[0]['payload']['text']        # "이 노래" = the track the show played


def test_no_music_right_before_outro_or_during_wrap_up():
    async def go():
        d = director(); d.music_every = 1
        sid = d.start('A', 'FROM_WORK')
        await send(d, sid, 'context.updated', **ctx(eta=120, commute='FROM_WORK'))   # arriving: wrap-up mode
        c = (await send(d, sid, 'session.started', topic_key='work_off'))[0]
        types = [c['type']]
        for _ in range(12):
            out = await send(d, sid, 'command.ack', command_id=c['command_id'], status='COMPLETED')
            if not out:
                break
            c = out[0]; types.append(c['type'])
        return types
    assert 'music.play' not in asyncio.run(go())


def test_music_file_route_only_serves_audio_names():
    import roadpod.main as main_mod
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as c:
        assert c.get('/music/am-01.mp3').status_code == 404          # sample slot: no file yet
        assert c.get('/music/..%2F..%2F.env').status_code == 404
        assert c.get('/music/README.md').status_code == 404


def test_music_pick_matches_the_commute():
    import random as _r
    from roadpod import music
    tracks = music.load()
    for commute, prefix in (('TO_WORK', 'am-'), ('FROM_WORK', 'pm-')):
        picks = {music.pick(tracks, commute, set(), _r.Random(i)).id for i in range(40)}
        assert all(p.startswith(prefix) for p in picks), (commute, picks)


def test_screen_endpoints_now_and_topics():
    import roadpod.main as main_mod
    from roadpod.main import TOKEN
    from fastapi.testclient import TestClient
    H = {'Authorization': f'Bearer {TOKEN}'}
    d = main_mod.director
    sid = d.start('screen-car', 'TO_WORK')
    asyncio.run(send(d, sid, 'session.started', topic_key='relationships'))
    with TestClient(main_mod.app) as c:
        now = c.get(f'/api/v1/sessions/{sid}/now', headers=H)
        topics = c.get('/api/v1/topics', headers=H)
        assert c.get(f'/api/v1/sessions/{sid}/now').status_code == 401
        assert c.get('/api/v1/sessions/nope/now', headers=H).status_code == 404
    body = now.json()
    assert body['topic']['question'] == '무례한 사람을 꼭 내 편으로 만들어야 할까?' and body['topic']['category_label'] == '관계와 소통'
    assert body['progress']['done'] == 1 and body['progress']['role'] == 'HOOK' and body['commute_type'] == 'TO_WORK'
    assert any(t['key'] == 'work_off' for t in topics.json()['topics'])
