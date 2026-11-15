"""Two independent simulated vehicle clients, for validating app-backend contract."""
import asyncio
import os
import uuid
from datetime import datetime, timezone

import httpx
import websockets

HTTP = os.getenv('ROADPOD_HTTP', 'http://127.0.0.1:8765')
WS = HTTP.replace('http://','ws://').replace('https://','wss://')
TOKEN = os.getenv('ROADPOD_DEV_TOKEN','local-demo-token')
HEADERS = {'Authorization': f'Bearer {TOKEN}'}

def event(sid, typ, payload):
    return {'schema_version':'1.0','event_id':str(uuid.uuid4()),'event_type':typ,
            'session_id':sid,'timestamp':datetime.now(timezone.utc).isoformat(),
            'payload':payload}

def context(sid, road, congested):
    return event(sid,'context.updated',{
        'commute_type':'TO_WORK',
        'vehicle':{'motion_state':'MOVING','speed_kmh':18 if congested else 58,
                   'wiper_active':None,'parked_confirmed':False},
        'navigation':{'road_name':road,'road_segment_id':None,
                      'eta_seconds':900,'guidance_active':False},
        'traffic':{'congestion':'SLOW' if congested else 'FREE','verified':True},
        'weather':{'condition':'UNKNOWN','verified':False},'source_mode':'SIMULATED'})

async def run_car(name, road, congested):
    async with httpx.AsyncClient() as http:
        resp = await http.post(f'{HTTP}/api/v1/sessions', headers=HEADERS,
                               json={'client_id':name, 'commute_type':'TO_WORK'})
        resp.raise_for_status()
        sid = resp.json()['session_id']
    async with websockets.connect(f'{WS}/ws/v1/sessions/{sid}',additional_headers=HEADERS) as ws:
        await ws.send(__import__('json').dumps(context(sid,road,congested), ensure_ascii=False))
        if congested:
            cmd = __import__('json').loads(await asyncio.wait_for(ws.recv(),5))
            print(name,'received:',cmd['type'], cmd['payload'])
            assert cmd['session_id'] == sid
            await ws.send(__import__('json').dumps(event(sid,'command.ack',{
                'command_id':cmd['command_id'],'status':'COMPLETED'})))
        await ws.send(__import__('json').dumps(event(sid,'user.utterance',{
            'text':f'{name} 출근길 이야기','is_final':True,'input_mode':'STT',
            'share_mode_enabled':False}),ensure_ascii=False))
        cmd = __import__('json').loads(await asyncio.wait_for(ws.recv(),5))
        print(name,'received:',cmd['type'], cmd['payload'])
        assert cmd['session_id']==sid and cmd['type']=='dj.speak'
        await ws.send(__import__('json').dumps(event(sid,'command.ack',{
            'command_id':cmd['command_id'],'status':'COMPLETED'})))
        await ws.send(__import__('json').dumps(event(sid,'navigation.guidance_started',{})))
        cmd = __import__('json').loads(await asyncio.wait_for(ws.recv(),5))
        print(name,'nav:',cmd['type'])
        assert cmd['type']=='media.pause'
        await ws.send(__import__('json').dumps(event(sid,'navigation.guidance_ended',{})))
        cmd = __import__('json').loads(await asyncio.wait_for(ws.recv(),5))
        print(name,'nav:',cmd['type'])
        assert cmd['type']=='media.resume'
        await ws.send(__import__('json').dumps(event(sid,'session.ended',{})))
    print(name,'PASS')

async def main():
    await asyncio.gather(run_car('CAR_A','올림픽대로',True),run_car('CAR_B','강변북로',False))

if __name__ == '__main__':
    asyncio.run(main())
