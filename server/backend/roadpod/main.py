"""Development-only server; static token and in-memory sessions must be replaced before public deployment."""
import json, logging, os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2]/'.env')
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from .engine import Director, ProtocolError
from .demo import build_router
from . import diary as diarylib
from . import music as musiclib
from fastapi.responses import FileResponse, HTMLResponse, Response

log=logging.getLogger('roadpod')

app=FastAPI(title='ROADPOD Backend Prototype',version='1.1.0')
director=Director()
TOKEN=os.getenv('DEV_TOKEN','local-development-only')
# DEMO_MODE=1: the local browser demo (/demo) and its WebSocket token handshake. Off for the real service.
DEMO_MODE=os.getenv('DEMO_MODE','').lower() in ('1','true','yes','on')
if DEMO_MODE:
    app.include_router(build_router(director,TOKEN))
# Browsers cannot set an Authorization header on WebSocket; the local demo sends the dev token as a subprotocol.
DEMO_SUBPROTOCOL=f'roadpod.bearer.{TOKEN}'

class SessionStart(BaseModel):
    client_id:str
    commute_type:str='UNKNOWN'
class Approval(BaseModel):
    session_id:str
    approved:bool

def require_auth(authorization:str|None):
    if authorization != f'Bearer {TOKEN}':raise HTTPException(status_code=401,detail='Unauthorized')

@app.get('/api/v1/health')
def health():return {'status':'ok','schema_version':'1.0','implementation':'prototype-v1.1'}

@app.get('/api/v1/topics')
def topics(authorization:str|None=Header(default=None)):
    """Topic list for the car screen's 주제 sheet (select with user.action select_topic + topic_key)."""
    require_auth(authorization)
    cat=director.catalog
    return {'categories':cat.categories,
            'topics':[{'key':t.key,'category':t.category,'question':t.question} for t in cat.topics]}

@app.get('/api/v1/sessions/{sid}/now')
def now_on_air(sid:str,authorization:str|None=Header(default=None)):
    """What the car screen shows: today's question, category, progress through the episode, current song.
    Read-only; the broadcast itself still flows over the WebSocket."""
    require_auth(authorization)
    s=director.states.get(sid)
    if not s:raise HTTPException(404,'Unknown session')
    out={'commute_type':s.commute_type,'topic':None,'progress':None,'now_playing':s.now_playing,
         'content_ready':s.content_ready,'nav_active':s.nav_active,'parked':s.parked}
    if s.topic:
        out['topic']={'key':s.topic.key,'question':s.topic.question,'category':s.topic.category,
                      'category_label':director.catalog.categories.get(s.topic.category,s.topic.category)}
        rows=director.store.conn.execute('SELECT idx,role FROM episode_segments WHERE episode_id=? ORDER BY idx',(s.episode_id,)).fetchall()
        cur=next((r['role'] for r in rows if r['idx']==s.segment_idx),None)
        out['progress']={'done':max(0,s.segment_idx+1),'total':len(rows),'role':cur}
    return out

@app.post('/api/v1/sessions',status_code=201)
def sessions(data:SessionStart,authorization:str|None=Header(default=None)):
    require_auth(authorization)
    if data.commute_type not in ('TO_WORK','FROM_WORK','OTHER','UNKNOWN'):
        raise HTTPException(422,'Invalid commute_type')
    sid=director.start(data.client_id,data.commute_type)
    return {'schema_version':'1.0','session_id':sid,'ws_path':f'/ws/v1/sessions/{sid}'}

@app.post('/api/v1/stories/{story_id}/approval')
async def approve(story_id:str,data:Approval,authorization:str|None=Header(default=None)):
    require_auth(authorization)
    if data.session_id not in director.states:raise HTTPException(404,'Unknown session')
    try: row,status=director.store.approve(story_id,data.session_id,data.approved)
    except PermissionError:raise HTTPException(403,'Not story owner')
    except ValueError:raise HTTPException(409,'Approval already decided')
    if status=='APPROVED':
        # Story Relay: moderation, then other drivers whose topic/commute/traffic match hear it.
        status=await director.publish(story_id)
    return {'story_id':story_id,'status':status,'published':status=='PUBLISHED'}

@app.get('/api/v1/topic-candidates')
def topic_candidates(authorization:str|None=Header(default=None)):
    """New episode questions grown from story threads (operator review before they join the catalog)."""
    require_auth(authorization)
    return {'candidates':director.stories.candidates()}

@app.post('/api/v1/topic-candidates/{candidate_id}/adopt')
def adopt_candidate(candidate_id:str,authorization:str|None=Header(default=None)):
    """Operator review passed: the question joins the common topic pool (any listener's next show can open with it)."""
    require_auth(authorization)
    key=director.adopt_candidate(candidate_id)
    if not key:raise HTTPException(404,'Unknown or already decided candidate')
    return {'candidate_id':candidate_id,'topic_key':key,'question':director.catalog.get(key).question}

@app.get('/music/{name}',include_in_schema=False)
def music_file(name:str):
    # 음악 코너 audio (royalty-free files in roadpod/data/music). Plain names only: no paths.
    path=musiclib.FILES/name
    if '/' in name or name.startswith('.') or path.suffix.lower() not in ('.mp3','.m4a','.ogg','.wav') or not path.is_file():
        raise HTTPException(404,'Unknown track')
    return FileResponse(path)

@app.get('/tts/{name}',include_in_schema=False)
def tts_file(name:str):
    # 큐큐 voice made by the server (OpenAI TTS); generated hex names only.
    path=director.voice.path(name)
    if not path:raise HTTPException(404,'Unknown audio')
    return FileResponse(path,media_type='audio/mpeg')

@app.get('/diaries/{diary_id}',response_class=HTMLResponse,include_in_schema=False)
def diary_page(diary_id:str):
    # Opened on the driver's phone from the QR code; the long random id is the only key (prototype).
    d=director.stories.diary(diary_id)
    if not d:raise HTTPException(404,'Unknown diary')
    return HTMLResponse(diarylib.page(d),headers={'Cache-Control':'no-store'})

@app.get('/diaries/{diary_id}/image',include_in_schema=False)
def diary_image(diary_id:str):
    d=director.stories.diary(diary_id)
    if not d:raise HTTPException(404,'Unknown diary')
    if d.get('image_path') and Path(d['image_path']).exists():
        return FileResponse(d['image_path'],media_type='image/png')
    return Response(diarylib.svg(d.get('mood'),d.get('commute_type'),d.get('weather')),media_type='image/svg+xml')

@app.websocket('/ws/v1/sessions/{sid}')
async def ws(websocket:WebSocket,sid:str):
    # Android client sets the Authorization header; the browser demo offers the token as a subprotocol (never in the URL).
    offered=[p.strip() for p in (websocket.headers.get('sec-websocket-protocol') or '').split(',') if p.strip()]
    header_ok=websocket.headers.get('authorization')==f'Bearer {TOKEN}'
    demo_ok=DEMO_MODE and DEMO_SUBPROTOCOL in offered
    if not (header_ok or demo_ok) or sid not in director.states:
        await websocket.close(code=1008);return
    await websocket.accept(subprotocol=DEMO_SUBPROTOCOL if demo_ok and not header_ok else None)
    # Episode bodies are researched in the background; this lets them reach the car when ready.
    director.attach(sid,websocket.send_json)
    try:
        while True:
            raw=await websocket.receive_text()
            # One bad event must never close the car's connection: report it and keep listening.
            try:
                message=json.loads(raw)
                if not isinstance(message,dict):raise ProtocolError('INVALID_JSON','event must be a JSON object')
                for command in await director.process(sid,message):await websocket.send_json(await director.outgoing(command))
            except json.JSONDecodeError:
                await websocket.send_json(protocol_error(sid,'INVALID_JSON'))
            except ProtocolError as e:
                await websocket.send_json(protocol_error(sid,e.code,e.detail))
            except (ValueError,KeyError) as e:
                await websocket.send_json(protocol_error(sid,str(e.args[0]) if e.args else type(e).__name__))
            except Exception:
                log.exception('Unhandled error while processing event for %s',sid)
                await websocket.send_json(protocol_error(sid,'INTERNAL_ERROR'))
    except WebSocketDisconnect:pass
    finally:director.detach(sid)

def protocol_error(sid,code,detail=None):
    error={'type':'protocol.error','session_id':sid,'code':code}
    if detail:error['detail']=detail
    return error
