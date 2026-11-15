"""Local browser demo (dev only). Every route answers only to requests from this machine."""
from __future__ import annotations
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

STATIC = Path(__file__).resolve().parent / 'static'
LOCAL = {'127.0.0.1', '::1', 'localhost', 'testclient'}


def build_router(director, token):
    router = APIRouter()

    def local_only(request: Request):
        if not request.client or request.client.host not in LOCAL:
            raise HTTPException(404)

    @router.get('/demo', include_in_schema=False)
    def page(request: Request):
        local_only(request)
        return FileResponse(STATIC / 'demo.html', headers={'Cache-Control': 'no-store'})

    @router.get('/demo/config', include_in_schema=False)
    def config(request: Request):
        local_only(request)
        llm = director.llm
        return {'token': token, 'live': llm.live, 'research_model': llm.research_model, 'writer_model': llm.writer_model}

    @router.get('/demo/topics', include_in_schema=False)
    def topics(request: Request):
        local_only(request)
        cat = director.catalog
        return {'categories': cat.categories,
                'topics': [{'key': t.key, 'category': t.category, 'question': t.question} for t in cat.topics]}

    @router.get('/demo/sessions/{sid}/episode', include_in_schema=False)
    def episode(sid: str, request: Request):
        local_only(request)
        s = director.states.get(sid)
        if not s:
            raise HTTPException(404, 'Unknown session')
        if not s.topic:
            return {'topic': None}
        store = director.store
        rows = store.conn.execute('SELECT idx, role, text, source_refs FROM episode_segments WHERE episode_id=? ORDER BY idx',
                                  (s.episode_id,)).fetchall()
        brief = store.get_brief(s.topic.key)
        script = store.get_script(s.topic.key, s.commute_type)
        facts = brief['brief']['facts'] if brief else []
        return {
            'topic': {'key': s.topic.key, 'question': s.topic.question, 'summary': s.topic.summary,
                      'category': director.catalog.categories.get(s.topic.category, s.topic.category),
                      'angles': list(s.topic.angles)},
            'mode': script['mode'] if script else ('catalog' if s.content_ready else 'producing'),
            'content_ready': s.content_ready,
            'current_idx': s.segment_idx,
            'segments': [{'idx': r['idx'], 'role': r['role'], 'text': r['text'], 'refs': json.loads(r['source_refs'])} for r in rows],
            'facts': [{'id': f['id'], 'claim': f['claim'], 'title': f.get('source_title'),
                       'publisher': f.get('publisher_name') or f.get('publisher'), 'url': f.get('source_url'),
                       'published': f.get('published')} for f in facts],
            'next_topic': s.next_topic.question if s.next_topic else None,
        }

    return router
