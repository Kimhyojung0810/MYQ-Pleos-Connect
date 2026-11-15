"""ROADPOD API v1 mock: only /health, /sessions, WebSocket happy-path implemented.
No real GPT, vehicle SDK, moderation, or storage. NEVER use with real personal data.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict
from jsonschema import Draft202012Validator, FormatChecker
import json

ROOT = Path(__file__).resolve().parent
CONTEXT = json.loads((ROOT / 'schemas/vehicle-context.schema.json').read_text())
COMMAND = json.loads((ROOT / 'schemas/broadcast-command.schema.json').read_text())
validate_context = Draft202012Validator(CONTEXT, format_checker=FormatChecker())
validate_command = Draft202012Validator(COMMAND, format_checker=FormatChecker())
TOKEN = os.getenv('ROADPOD_DEV_TOKEN', 'local-demo-token')
app = FastAPI(title='ROADPOD Mock Contract API', version='1.0.0')
sessions = {}
seen_events = set()

class StartSession(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_id: str
    commute_type: Literal['TO_WORK', 'FROM_WORK', 'OTHER', 'UNKNOWN']

def utc():
    return datetime.now(timezone.utc)

def timestamp(dt=None):
    return (dt or utc()).isoformat()

def check_token(authorization):
    if authorization != 'Bearer ' + TOKEN:
        raise HTTPException(401, 'Invalid development token')

def make_command(sid, typ, payload, ttl_sec=20):
    now = utc()
    obj = {'schema_version': '1.0', 'command_id': str(uuid.uuid4()),
           'session_id': sid, 'type': typ, 'issued_at': timestamp(now),
           'expires_at': timestamp(now + timedelta(seconds=ttl_sec)), 'payload': payload}
    validate_command.validate(obj)
    return obj

@app.get('/api/v1/health')
def health():
    return {'status':'ok', 'schema_version':'1.0'}

@app.post('/api/v1/sessions', status_code=201)
def start_session(req: StartSession, authorization: str | None = Header(None)):
    check_token(authorization)
    sid = str(uuid.uuid4())
    sessions[sid] = {'client_id': req.client_id, 'commute_type': req.commute_type,
                     'nav_active': False, 'seen_commands': {}, 'parked': False}
    return {'schema_version':'1.0', 'session_id':sid, 'ws_path':f'/ws/v1/sessions/{sid}'}

@app.websocket('/ws/v1/sessions/{sid}')
async def ws_session(ws: WebSocket, sid: str):
    if ws.headers.get('authorization') != 'Bearer ' + TOKEN or sid not in sessions:
        await ws.close(code=1008)
        return
    await ws.accept()
    state = sessions[sid]
    try:
        while True:
            event = await ws.receive_json()
            if event.get('schema_version') != '1.0' or event.get('session_id') != sid:
                await ws.send_json({'error':'BAD_ENVELOPE'})
                continue
            eid = event.get('event_id')
            if not eid or (sid, eid) in seen_events:
                # Repeat events have no duplicate side effects.
                continue
            seen_events.add((sid, eid))
            typ = event.get('event_type')
            if typ == 'context.updated':
                errors = list(validate_context.iter_errors(event))
                if errors:
                    await ws.send_json({'error':'SCHEMA_VALIDATION', 'detail':errors[0].message})
                    continue
                payload = event['payload']
                state['nav_active'] = payload['navigation']['guidance_active']
                state['parked'] = payload['vehicle']['parked_confirmed']
                if state['nav_active']:
                    await ws.send_json(make_command(sid,'media.pause',{}))
                elif payload['traffic']['congestion'] in ['SLOW','JAM'] and payload['traffic']['verified']:
                    road = payload['navigation']['road_name'] or '현재 구간'
                    await ws.send_json(make_command(sid,'dj.speak',{
                        'text':f'{road} 교통 상황을 확인했어요. 잠시 음악 듣고 이야기 이어가요.',
                        'voice_style':'WARM','interrupt_policy':'DEFER_IF_NAV'}))
            elif typ == 'navigation.guidance_started':
                state['nav_active'] = True
                await ws.send_json(make_command(sid,'media.pause',{}))
            elif typ == 'navigation.guidance_ended':
                state['nav_active'] = False
                await ws.send_json(make_command(sid,'media.resume',{}))
            elif typ == 'user.utterance':
                payload = event.get('payload',{})
                if not payload.get('is_final',False):
                    continue
                if state['nav_active']:
                    # Critical safety gating: do not create immediate speech while navigation speaks.
                    continue
                text = str(payload.get('text',''))[:160]
                if text:
                    await ws.send_json(make_command(sid,'dj.speak',{
                        'text':f'말씀하신 「{text}」에 대해 조금 더 이야기해 볼까요?',
                        'voice_style':'WARM', 'interrupt_policy':'DEFER_IF_NAV'}))
            elif typ == 'command.ack':
                p = event.get('payload',{})
                if p.get('status') in ('RECEIVED','STARTED','COMPLETED','FAILED','INTERRUPTED'):
                    state['seen_commands'][p.get('command_id')] = p['status']
            elif typ == 'vehicle.parked':
                if state['parked']:
                    await ws.send_json(make_command(sid,'ui.state',{'state':'PARKED'}))
            elif typ == 'session.ended':
                break
    except WebSocketDisconnect:
        pass

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8765)
