import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from roadpod.engine import Director,MemoryStore
from roadpod.main import app,director,TOKEN
import pytest

HEAD={'Authorization':f'Bearer {TOKEN}'}

def event(sid,typ,**payload):
    import uuid
    from datetime import datetime,timezone
    return dict(schema_version='1.0',event_id=str(uuid.uuid4()),event_type=typ,session_id=sid,timestamp=datetime.now(timezone.utc).isoformat(),payload=payload)

def test_independent_episodes():
    d=Director(MemoryStore());a=d.start('car-a','TO_WORK');b=d.start('car-b','FROM_WORK')
    import asyncio
    c1=asyncio.run(d.process(a,event(a,'user.action',action='select_topic',topic_key='relationships')))
    c2=asyncio.run(d.process(b,event(b,'user.action',action='select_topic',topic_key='focus')))
    assert '무례한 사람' in c1[0]['payload']['text']
    assert '집중' in c2[0]['payload']['text']
    assert c1[0]['session_id'] != c2[0]['session_id']

def test_safety_nav_and_dedup():
    import asyncio
    d=Director(MemoryStore());sid=d.start('car-a','TO_WORK')
    ev=event(sid,'navigation.guidance_started')
    assert asyncio.run(d.process(sid,ev))[0]['type']=='media.pause'
    assert asyncio.run(d.process(sid,ev))==[]
    assert asyncio.run(d.process(sid,event(sid,'user.utterance',text='안녕',is_final=True)))==[]
    assert asyncio.run(d.process(sid,event(sid,'navigation.guidance_ended')))[0]['type']=='media.resume'

def test_no_publish_without_approval_and_owner_check():
    import asyncio
    d=Director(MemoryStore());a=d.start('A','TO_WORK');b=d.start('B','TO_WORK')
    d.followup_seconds=0.01
    async def tell():
        # Story Catch: the DJ asks one follow-up first; "이대로" means draft it as it is.
        first=await d.process(a,event(a,'user.utterance',text='나는 내 경계를 지키고 싶어',intent='CREATE_STORY',is_final=True))
        assert first[0]['type']=='dj.speak'
        return await d.process(a,event(a,'user.utterance',text='이대로 보내 줘',intent='CREATE_STORY',is_final=True))
    c=asyncio.run(tell())
    row=c[0]['payload'];assert c[0]['type']=='story.preview'
    db=d.store.conn.execute('SELECT status FROM story_drafts WHERE story_id=?',(row['story_id'],)).fetchone()
    assert db['status']=='DRAFT'
    with pytest.raises(PermissionError):d.store.approve(row['story_id'],b,True)
    _,state=d.store.approve(row['story_id'],a,True);assert state=='APPROVED'
    assert d.store.conn.execute('SELECT COUNT(*) n FROM story_drafts WHERE status="PUBLISHED"').fetchone()['n']==0

def test_no_diary_on_stop():
    import asyncio
    d=Director(MemoryStore());sid=d.start('A','TO_WORK')
    assert asyncio.run(d.process(sid,event(sid,'vehicle.parked')))==[]

def test_command_ack_and_queue():
    import asyncio
    d=Director(MemoryStore());sid=d.start('A','TO_WORK')
    first=asyncio.run(d.process(sid,event(sid,'user.utterance',text='질문',is_final=True)))[0]
    assert asyncio.run(d.process(sid,event(sid,'user.utterance',text='다른 말',is_final=True)))==[]
    nxt=asyncio.run(d.process(sid,event(sid,'command.ack',command_id=first['command_id'],status='COMPLETED')))
    assert nxt and nxt[0]['command_id']!=first['command_id']

def test_http_auth():
    with TestClient(app) as c:
        assert c.post('/api/v1/sessions',json={'client_id':'A','commute_type':'TO_WORK'}).status_code==401
        r=c.post('/api/v1/sessions',headers=HEAD,json={'client_id':'A','commute_type':'TO_WORK'})
        assert r.status_code==201

def test_websocket_roundtrip():
    with TestClient(app) as c:
        sid=c.post('/api/v1/sessions',headers=HEAD,json={'client_id':'WS','commute_type':'TO_WORK'}).json()['session_id']
        with c.websocket_connect(f'/ws/v1/sessions/{sid}',headers=HEAD) as ws:
            ws.send_json(event(sid,'user.action',action='select_topic',topic_key='relationships'))
            result=ws.receive_json()
            assert result['type']=='dj.speak'
            assert result['session_id']==sid

# --- v1.1 hardening: a bad event or LLM misconfig must not drop the car's WebSocket ---
import json as _json
from roadpod.engine import TextProvider

def test_openai_mode_without_key_falls_back(monkeypatch):
    import asyncio
    monkeypatch.setenv('LLM_PROVIDER','openai');monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    d=Director(MemoryStore(),TextProvider());sid=d.start('A','TO_WORK')
    out=asyncio.run(d.process(sid,event(sid,'user.utterance',text='제 생각은 달라요',is_final=True)))
    assert out[0]['type']=='dj.speak' and out[0]['payload']['text']

def test_utterance_reply_keeps_episode_topic():
    import asyncio
    d=Director(MemoryStore());sid=d.start('B','TO_WORK')
    hook=asyncio.run(d.process(sid,event(sid,'user.action',action='select_topic',topic_key='focus')))[0]
    frame=asyncio.run(d.process(sid,event(sid,'command.ack',command_id=hook['command_id'],status='COMPLETED')))[0]
    # Reply waits for the segment that is playing, then goes before the next segment.
    assert asyncio.run(d.process(sid,event(sid,'user.utterance',text='알림 때문에 자꾸 끊겨요',is_final=True)))==[]
    reply=asyncio.run(d.process(sid,event(sid,'command.ack',command_id=frame['command_id'],status='COMPLETED')))[0]
    assert reply['type']=='dj.speak' and '집중' in reply['payload']['text']

def _ws_session(c):
    return c.post('/api/v1/sessions',headers=HEAD,json={'client_id':'WS','commute_type':'TO_WORK'}).json()['session_id']

def test_ws_valid_context_example_accepted():
    example=_json.loads((Path(__file__).resolve().parents[2]/'examples'/'context.updated.json').read_text(encoding='utf-8'))
    with TestClient(app) as c:
        sid=_ws_session(c)
        with c.websocket_connect(f'/ws/v1/sessions/{sid}',headers=HEAD) as ws:
            example.update(session_id=sid,event_id=event(sid,'x')['event_id'])
            ws.send_json(example)
            ws.send_json(event(sid,'user.action',action='select_topic',topic_key='focus'))
            assert ws.receive_json()['type'] in ('dj.speak',)  # no protocol.error before it

def test_ws_survives_invalid_context():
    with TestClient(app) as c:
        sid=_ws_session(c)
        with c.websocket_connect(f'/ws/v1/sessions/{sid}',headers=HEAD) as ws:
            ws.send_json(event(sid,'context.updated',vehicle={'speed_kph':'fast'}))
            err=ws.receive_json()
            assert err['type']=='protocol.error' and err['code']=='CONTEXT_SCHEMA_INVALID' and err['detail']
            ws.send_json(event(sid,'user.action',action='select_topic',topic_key='relationships'))
            assert ws.receive_json()['type']=='dj.speak'

def test_ws_survives_bad_json_and_reports_codes_cleanly():
    with TestClient(app) as c:
        sid=_ws_session(c)
        with c.websocket_connect(f'/ws/v1/sessions/{sid}',headers=HEAD) as ws:
            ws.send_text('{not json');assert ws.receive_json()['code']=='INVALID_JSON'
            ws.send_text('[1,2]');assert ws.receive_json()['code']=='INVALID_JSON'
            ws.send_json(event('other-session','user.action',action='select_topic'))
            assert ws.receive_json()['code']=='SESSION_MISMATCH'
            ws.send_json(event(sid,'user.action',action='select_topic',topic_key='focus'))
            assert ws.receive_json()['type']=='dj.speak'

def test_ws_survives_llm_misconfig(monkeypatch):
    import roadpod.main as main_mod
    monkeypatch.setenv('LLM_PROVIDER','openai');monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    monkeypatch.setattr(main_mod.director,'llm',TextProvider())
    with TestClient(app) as c:
        sid=_ws_session(c)
        with c.websocket_connect(f'/ws/v1/sessions/{sid}',headers=HEAD) as ws:
            ws.send_json(event(sid,'user.utterance',text='저는 반대예요',is_final=True))
            assert ws.receive_json()['type']=='dj.speak'
