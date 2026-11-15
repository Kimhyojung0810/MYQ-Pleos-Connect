"""Two independent mock-car sessions; does not require a vehicle or API key.

CAR_A: server picks the topic on session.started (no tap).   CAR_B: listener picks 'focus'.
Each car: hook → auto-advance → listener speaks mid-episode → reply → next segment
          → nav guidance cuts the segment → pause → resume and replay the cut segment.
"""
import asyncio, json, os, uuid
from datetime import datetime,timezone
import httpx, websockets
BASE=os.getenv('BASE_URL','http://127.0.0.1:8765')
TOKEN=os.getenv('DEV_TOKEN','local-development-only')
HEAD={'Authorization':'Bearer '+TOKEN}
# Real research + writing can take a while with LLM_PROVIDER=openai.
WAIT=float(os.getenv('MOCK_WAIT_SECONDS','120'))
OPINIONS={
    'relationships':'저는 무례한 사람까지 굳이 내 편으로 만들 필요는 없다고 생각해요.',
    'focus':'저는 휴대폰 알림 때문에 집중이 자주 끊기는 것 같아요.',
}
DEFAULT_OPINION='저는 조금 다르게 생각해요. 상황에 따라 답이 달라질 것 같아요.'

def event(sid,typ,**payload):
    return {'schema_version':'1.0','event_id':str(uuid.uuid4()),'event_type':typ,'session_id':sid,'timestamp':datetime.now(timezone.utc).isoformat(),'payload':payload}

async def recv(ws,expected):
    msg=json.loads(await asyncio.wait_for(ws.recv(),WAIT))
    assert msg['type']==expected,msg
    return msg

async def car(name,topic=None):
    async with httpx.AsyncClient() as client:
        response=await client.post(BASE+'/api/v1/sessions',headers=HEAD,json={'client_id':name,'commute_type':'TO_WORK'})
        response.raise_for_status();sid=response.json()['session_id']
    ws_url=BASE.replace('http://','ws://').replace('https://','wss://')+f'/ws/v1/sessions/{sid}'
    async with websockets.connect(ws_url,additional_headers=HEAD) as ws:
        send=lambda typ,**p:ws.send(json.dumps(event(sid,typ,**p)))
        done=lambda c,status='COMPLETED':send('command.ack',command_id=c['command_id'],status=status)
        if topic:await send('user.action',action='select_topic',topic_key=topic)
        else:await send('session.started')
        hook=await recv(ws,'dj.speak');print(name,'HOOK :',hook['payload']['text'])
        await done(hook)
        frame=await recv(ws,'dj.speak');print(name,'NEXT :',frame['payload']['text'][:70],'…')
        opinion=OPINIONS.get(topic,DEFAULT_OPINION)
        await send('user.utterance',text=opinion,is_final=True)   # reply waits for the segment playing
        await done(frame)
        reply=await recv(ws,'dj.speak');print(name,'USER :',opinion);print(name,'REPLY:',reply['payload']['text'])
        await done(reply)
        seg=await recv(ws,'dj.speak');print(name,'NEXT :',seg['payload']['text'][:70],'…')
        await send('navigation.guidance_started')
        await recv(ws,'media.pause');print(name,'NAV  : media.pause')
        await done(seg,'INTERRUPTED')
        await send('navigation.guidance_ended')
        await recv(ws,'media.resume')
        again=await recv(ws,'dj.speak')
        assert again['payload']['text'].startswith('이어서 들려드릴게요.'),again
        print(name,'NAV  : media.resume → replayed cut segment  PASS')

async def main():await asyncio.gather(car('CAR_A'),car('CAR_B','focus'))
if __name__=='__main__':asyncio.run(main())
