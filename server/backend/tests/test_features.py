"""Spec features ①–⑦ on top of the broadcast engine (mock LLM: deterministic fallbacks)."""
import asyncio, random, uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from roadpod.engine import Director, MemoryStore
from roadpod import drive, relay


def event(sid, typ, **payload):
    return dict(schema_version='1.0', event_id=str(uuid.uuid4()), event_type=typ, session_id=sid,
                timestamp=datetime.now(timezone.utc).isoformat(), payload=payload)


def ctx(*, congestion='UNKNOWN', traffic_ok=False, weather='UNKNOWN', weather_ok=False, wiper=False, eta=1200,
        segment=None, road=None, parked=False, guidance=False, commute='TO_WORK'):
    return dict(commute_type=commute,
                vehicle={'motion_state': 'PARKED' if parked else 'MOVING', 'speed_kmh': 0 if parked else 40,
                         'wiper_active': wiper, 'parked_confirmed': parked},
                navigation={'road_name': road, 'road_segment_id': segment, 'eta_seconds': eta, 'guidance_active': guidance},
                traffic={'congestion': congestion, 'verified': traffic_ok},
                weather={'condition': weather, 'verified': weather_ok}, source_mode='SIMULATED')


def director():
    d = Director(MemoryStore(), rng=random.Random(1))
    d.followup_seconds = 0.01
    return d


async def send(d, sid, typ, **payload):
    return await d.process(sid, event(sid, typ, **payload))


async def ack(d, sid, command):
    return await send(d, sid, 'command.ack', command_id=command['command_id'], status='COMPLETED')


async def play_until(d, sid, first, stop, limit=40):
    """ACK each command the car receives until `stop(command)`; returns that command (or None)."""
    c = first
    for _ in range(limit):
        if stop(c):
            return c
        out = await ack(d, sid, c)
        if not out:
            return None
        c = out[0]
    return None


def text(c):
    return c['payload'].get('text', '')


# --- ① ETA 기반 길이 조정 ---

def test_short_eta_jumps_to_outro_then_signs_off():
    async def scenario():
        d = director(); sid = d.start('A', 'TO_WORK')
        await send(d, sid, 'context.updated', **ctx(eta=1200))
        hook = (await send(d, sid, 'user.action', action='select_topic', topic_key='focus'))[0]
        frame = (await ack(d, sid, hook))[0]
        await send(d, sid, 'context.updated', **ctx(eta=40))          # suddenly almost there
        outro = (await ack(d, sid, frame))[0]
        goodbye = (await ack(d, sid, outro))[0]
        after = await ack(d, sid, goodbye)
        return d, sid, outro, goodbye, after
    d, sid, outro, goodbye, after = asyncio.run(scenario())
    assert d.states[sid].segment_idx == d.store.conn.execute(
        "SELECT MAX(idx) m FROM episode_segments WHERE episode_id=?", (d.states[sid].episode_id,)).fetchone()['m']
    assert '여기까지' in text(outro)                 # the OUTRO, not the next perspective
    assert text(goodbye) in drive.SIGN_OFF and after == []   # no new topic started on arrival


# --- ② 주행 맥락 코멘트 ---

def test_jam_comment_plays_after_current_segment_once():
    async def scenario():
        d = director(); sid = d.start('A', 'TO_WORK')
        await send(d, sid, 'context.updated', **ctx())
        hook = (await send(d, sid, 'user.action', action='select_topic', topic_key='focus'))[0]
        queued = await send(d, sid, 'context.updated', **ctx(congestion='JAM', traffic_ok=True))
        again = await send(d, sid, 'context.updated', **ctx(congestion='JAM', traffic_ok=True))
        nxt = (await ack(d, sid, hook))[0]
        return queued, again, nxt
    queued, again, nxt = asyncio.run(scenario())
    assert queued == [] and again == []            # waits for the hook to finish; a persisting jam is not news
    # Aired right after the current segment, then hands back to the episode.
    assert any(text(nxt).startswith(c) for c in drive.COMMENTS['jam'])
    assert text(nxt).endswith('그럼 아까 이야기 마저 이어 갈게요.')


def test_wiper_starting_triggers_rain_comment_but_not_during_navigation():
    async def scenario():
        d = director(); sid = d.start('A', 'FROM_WORK')
        await send(d, sid, 'context.updated', **ctx(wiper=False))
        rain = await send(d, sid, 'context.updated', **ctx(wiper=True))
        d2 = director(); s2 = d2.start('B', 'FROM_WORK')
        await send(d2, s2, 'context.updated', **ctx(wiper=False, guidance=True))
        quiet = await send(d2, s2, 'context.updated', **ctx(wiper=True, guidance=True))
        return rain, quiet
    rain, quiet = asyncio.run(scenario())
    assert text(rain[0]) in drive.COMMENTS['rain'] and rain[0]['payload']['voice_style'] == 'CALM'
    assert quiet == []


# --- ③ 출퇴근 DJ 분위기 ---

def test_voice_style_follows_commute():
    async def scenario():
        d = director()
        out = {}
        for commute in ('TO_WORK', 'FROM_WORK', 'UNKNOWN'):
            sid = d.start(commute, commute)
            out[commute] = (await send(d, sid, 'session.started'))[0]['payload']['voice_style']
        return out
    assert asyncio.run(scenario()) == {'TO_WORK': 'BRIGHT', 'FROM_WORK': 'CALM', 'UNKNOWN': 'WARM'}


# --- ④ Story Catch ---

def test_story_catch_asks_one_followup_and_combines_answer():
    async def scenario():
        d = director(); sid = d.start('A', 'TO_WORK')
        await send(d, sid, 'user.action', action='select_topic', topic_key='focus')
        await send(d, sid, 'user.action', action='request_story')
        q = await send(d, sid, 'user.utterance', text='회의 때마다 알림 때문에 집중이 깨져요', is_final=True)
        done = await send(d, sid, 'user.utterance', text='그래서 요즘은 폰을 뒤집어 둬요', is_final=True)
        return d, q, done
    d, q, done = asyncio.run(scenario())
    assert q == [] or q[0]['type'] == 'dj.speak'   # follow-up queued behind the playing hook
    preview = done[0]
    assert preview['type'] == 'story.preview' and preview['payload']['requires_explicit_approval'] is True
    assert '알림' in preview['payload']['preview_text'] and '뒤집어' in preview['payload']['preview_text']
    meta = d.stories.story(preview['payload']['story_id'])
    assert meta['topic_key'] == 'focus' and meta['status'] == 'DRAFT'


def test_unanswered_followup_drafts_story_after_timeout():
    async def scenario():
        d = director(); sid = d.start('A', 'TO_WORK'); pushed = []
        async def car(c): pushed.append(c)
        d.attach(sid, car)
        first = await send(d, sid, 'user.utterance', text='오늘은 그냥 다 내려놓고 싶다', intent='CREATE_STORY', is_final=True)
        await asyncio.sleep(0.05)
        return first, pushed
    first, pushed = asyncio.run(scenario())
    assert first[0]['type'] == 'dj.speak' and first[0]['payload']['text'] == relay.FOLLOWUP_FALLBACK
    assert pushed and pushed[0]['type'] == 'story.preview'


def test_story_with_phone_number_is_refused():
    async def scenario():
        d = director(); sid = d.start('A', 'TO_WORK')
        return await send(d, sid, 'user.utterance', text='제 번호 010-1234-5678로 연락 주세요', intent='CREATE_STORY', is_final=True)
    out = asyncio.run(scenario())
    assert out[0]['type'] == 'dj.speak' and '개인정보' in text(out[0])


# --- ⑤ Story Relay ---

async def tell_story(d, sid, words):
    await send(d, sid, 'user.action', action='request_story')
    await send(d, sid, 'user.utterance', text=words, is_final=True)
    out = await send(d, sid, 'user.utterance', text='이대로 보내 줘', is_final=True)
    story_id = out[0]['payload']['story_id']
    d.store.approve(story_id, sid, True)
    return story_id, await d.publish(story_id)


def test_published_story_reaches_matching_driver_live_only():
    async def scenario():
        d = director()
        a = d.start('A', 'TO_WORK'); b = d.start('B', 'TO_WORK'); c = d.start('C', 'FROM_WORK')
        got = {b: [], c: []}
        for sid in (b, c):
            async def car(cmd, sid=sid): got[sid].append(cmd)
            d.attach(sid, car)
        await send(d, a, 'user.action', action='select_topic', topic_key='focus')
        await send(d, b, 'context.updated', **ctx())
        hook = (await send(d, b, 'user.action', action='select_topic', topic_key='focus'))[0]
        await ack(d, b, hook)                                  # B listening, then idle between commands below
        b_state = d.states[b]
        b_state.playing_command_id = None; b_state.pending.clear()
        await send(d, c, 'user.action', action='select_topic', topic_key='self_esteem')
        story_id, status = await tell_story(d, a, '알림을 끄니까 하루가 조금 길어진 기분이에요')
        return d, b, c, got, story_id, status
    d, b, c, got, story_id, status = asyncio.run(scenario())
    assert status == 'PUBLISHED'
    incoming = [x for x in got[b] if x['type'] == 'story.incoming']
    assert incoming and incoming[0]['payload']['story_id'] == story_id
    assert relay.REPLY_INVITE.strip() in incoming[0]['payload']['text']
    assert not any(x['type'] == 'story.incoming' for x in got[c])   # other topic, other commute: no match


def test_story_slot_before_outro_then_reply_reaches_author():
    async def scenario():
        d = director()
        a = d.start('A', 'TO_WORK')
        await send(d, a, 'user.action', action='select_topic', topic_key='focus')
        story_id, _ = await tell_story(d, a, '집중은 의지보다 환경이라고 생각해요')
        # Later, listener B plays the same topic to its end: the story airs just before the OUTRO.
        b = d.start('B', 'FROM_WORK')
        first = (await send(d, b, 'user.action', action='select_topic', topic_key='focus'))[0]
        slot = await play_until(d, b, first, lambda c: c['type'] == 'story.incoming')
        outro = (await ack(d, b, slot))[0]
        # B answers it (맞사연) while the OUTRO plays; A is driving again in a new session.
        a_pushed = []
        reply_id, status = await tell_story(d, b, '저는 오히려 반대로 의지가 더 중요하다고 봐요')
        a2 = d.start('A', 'TO_WORK')
        async def car(cmd): a_pushed.append(cmd)
        d.attach(a2, car)
        hook = (await send(d, a2, 'session.started'))[0]
        after_hook = await play_until(d, a2, hook, lambda c: c['type'] == 'story.incoming')
        return d, story_id, slot, outro, reply_id, status, after_hook
    d, story_id, slot, outro, reply_id, status, after_hook = asyncio.run(scenario())
    assert slot['payload']['story_id'] == story_id and '여기까지' in text(outro)
    assert status == 'PUBLISHED'
    rel = d.store.conn.execute('SELECT * FROM story_relations WHERE child_story_id=?', (reply_id,)).fetchone()
    assert rel['parent_story_id'] == story_id and rel['relation_type'] == 'COUNTERPOINT'
    assert after_hook and after_hook['payload']['story_id'] == reply_id
    assert after_hook['payload']['text'].startswith('지난번 보내 주신 사연에')


def test_unsafe_story_is_blocked_not_relayed():
    async def scenario():
        d = director(); a = d.start('A', 'TO_WORK')
        await send(d, a, 'user.action', action='select_topic', topic_key='focus')
        return await tell_story(d, a, '진짜 씨발 오늘 너무 짜증나')
    story_id, status = asyncio.run(scenario())
    assert status == 'BLOCKED'


def test_thread_with_two_replies_becomes_topic_candidate_and_relay_program():
    async def scenario():
        d = director()
        a = d.start('A', 'TO_WORK')
        await send(d, a, 'user.action', action='select_topic', topic_key='focus')
        root, _ = await tell_story(d, a, '퇴근 후에도 업무 메신저가 와요')
        for name, words in (('B', '저도 같은 마음이에요'), ('C', '저는 오히려 바로 답하는 게 편해요')):
            sid = d.start(name, 'TO_WORK')
            d.states[sid].reply_to = root
            await tell_story(d, sid, words)
        await asyncio.gather(*d._tasks)
        e = d.start('E', 'TO_WORK')
        intro = await send(d, e, 'user.action', action='play_relay')
        program = await play_until(d, e, intro[0], lambda c: False, limit=4) or None
        aired = [x for x in d.store.conn.execute('SELECT story_id FROM story_deliveries WHERE client_id=?', ('E',))]
        return d, root, intro, aired
    d, root, intro, aired = asyncio.run(scenario())
    cands = d.stories.candidates()
    assert cands and cands[0]['from_story_id'] == root
    assert '3편' in text(intro[0]) and len(aired) == 3


# --- ⑥ 마이크 드롭 ---

def test_music_note_reaches_next_driver_on_same_segment_once():
    async def scenario():
        d = director()
        a = d.start('A', 'TO_WORK')
        await send(d, a, 'context.updated', **ctx(segment='seg-42', road='강변북로'))
        left = await send(d, a, 'user.utterance', text='큐큐야 이 구간에 「밤양갱」 노래 하나 남겨줘', is_final=True)
        b = d.start('B', 'TO_WORK')
        await send(d, b, 'context.updated', **ctx(segment='seg-1'))
        heard = await send(d, b, 'context.updated', **ctx(segment='seg-42', road='강변북로'))
        d.states[b].last_comment.clear(); d.states[b].playing_command_id = None
        await send(d, b, 'context.updated', **ctx(segment='seg-2'))
        twice = await send(d, b, 'context.updated', **ctx(segment='seg-42'))
        return left, heard, twice
    left, heard, twice = asyncio.run(scenario())
    assert '밤양갱' in text(left[0]) and '음표' in text(left[0])
    assert '밤양갱' in text(heard[0])
    assert twice == []


def test_drop_music_action_with_track():
    async def scenario():
        d = director(); a = d.start('A', 'FROM_WORK')
        return d, await send(d, a, 'user.action', action='drop_music', track='Square - 백예린')
    d, out = asyncio.run(scenario())
    assert 'Square - 백예린' in text(out[0])
    assert d.store.conn.execute('SELECT track FROM music_notes').fetchone()['track'] == 'Square - 백예린'


# --- ⑦ 오늘의 Q Diary ---

def test_diary_after_confirmed_parking_and_viewable_page():
    import roadpod.main as main_mod
    d = main_mod.director
    sid = d.start('diary-car', 'FROM_WORK')
    async def scenario():
        await send(d, sid, 'user.action', action='select_topic', topic_key='focus')
        await send(d, sid, 'user.utterance', text='오늘 너무 피곤했어', is_final=True)
        await send(d, sid, 'context.updated', **ctx(parked=True, eta=0, weather='RAIN', weather_ok=True, commute='FROM_WORK'))
        first = await send(d, sid, 'vehicle.parked')
        second = await send(d, sid, 'vehicle.parked')
        return first, second
    first, second = asyncio.run(scenario())
    closing, ready = first
    assert closing['type'] == 'dj.speak' and closing['payload']['text'].startswith('큐큐~')   # the one line after parking
    assert ready['type'] == 'diary.ready' and second == []
    url = ready['payload']['view_url']
    assert url.endswith('/diaries/' + ready['payload']['diary_id'])
    with TestClient(main_mod.app) as c:
        page = c.get('/diaries/' + ready['payload']['diary_id'])
        image = c.get('/diaries/' + ready['payload']['diary_id'] + '/image')
        missing = c.get('/diaries/diary-nope')
    assert page.status_code == 200 and '오늘의 Q Diary' in page.text and '퇴근길' in page.text
    assert image.status_code == 200 and image.headers['content-type'].startswith('image/svg')
    assert missing.status_code == 404
    diary = d.stories.diary(ready['payload']['diary_id'])
    assert diary['mood'] == 'TIRED' and diary['weather'] == 'RAIN'


def test_approval_endpoint_publishes():
    import roadpod.main as main_mod
    from roadpod.main import TOKEN
    d = main_mod.director
    sid = d.start('http-author', 'TO_WORK')
    async def scenario():
        await send(d, sid, 'user.action', action='request_story')
        await send(d, sid, 'user.utterance', text='출근길 하늘이 예뻤어요', is_final=True)
        return (await send(d, sid, 'user.utterance', text='이대로', is_final=True))[0]['payload']['story_id']
    story_id = asyncio.run(scenario())
    with TestClient(main_mod.app) as c:
        r = c.post(f'/api/v1/stories/{story_id}/approval', headers={'Authorization': f'Bearer {TOKEN}'},
                   json={'session_id': sid, 'approved': True})
        cands = c.get('/api/v1/topic-candidates', headers={'Authorization': f'Bearer {TOKEN}'})
    assert r.status_code == 200 and r.json() == {'story_id': story_id, 'status': 'PUBLISHED', 'published': True}
    assert cands.status_code == 200 and 'candidates' in cands.json()


# --- contract & edge cases ---

def test_every_new_command_matches_the_shared_contract():
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' / 'schemas' / 'broadcast-command.schema.json').read_text())
    check = Draft202012Validator(schema, format_checker=FormatChecker())
    seen = []
    async def scenario():
        d = director()
        a = d.start('A', 'FROM_WORK'); b = d.start('B', 'FROM_WORK')
        for sid in (a, b):
            async def car(c): seen.append(c)
            d.attach(sid, car)
        seen.extend(await send(d, a, 'context.updated', **ctx(segment='s1', commute='FROM_WORK')))
        seen.extend(await send(d, a, 'user.action', action='select_topic', topic_key='focus'))
        seen.extend(await send(d, a, 'context.updated', **ctx(segment='s1', congestion='JAM', traffic_ok=True, commute='FROM_WORK')))
        seen.extend(await send(d, a, 'user.action', action='drop_music', track='Square'))
        await send(d, a, 'user.action', action='request_story')
        seen.extend(await send(d, a, 'user.utterance', text='퇴근길 노을이 위로가 됐어요', is_final=True))
        out = await send(d, a, 'user.utterance', text='이대로', is_final=True); seen.extend(out)
        d.store.approve(out[0]['payload']['story_id'], a, True)
        seen.extend(await send(d, b, 'user.action', action='select_topic', topic_key='focus'))
        d.states[b].playing_command_id = None
        await d.publish(out[0]['payload']['story_id'])
        seen.extend(await send(d, a, 'context.updated', **ctx(parked=True, eta=0, commute='FROM_WORK')))
        seen.extend(await send(d, a, 'vehicle.parked'))
    asyncio.run(scenario())
    types = {c['type'] for c in seen}
    assert {'dj.speak', 'story.preview', 'story.incoming', 'diary.ready'} <= types
    for c in seen:
        errors = list(check.iter_errors(c))
        assert not errors, (c['type'], errors[0].message)


def test_pii_next_to_korean_particle_and_real_answers_are_kept():
    assert relay.has_pii('010-1234-5678로 연락') and relay.has_pii('12가3456차량이')
    assert not relay.has_pii('12시 1000원') and not relay.has_pii('2025년에 3000명이')
    assert relay.wants_no_followup('이대로 보내 줘') and relay.wants_no_followup('없어요')
    assert not relay.wants_no_followup('그냥 좀 서운했어요, 내 마음을 아무도 몰라서')
