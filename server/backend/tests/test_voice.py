"""큐큐 voice (server-made OpenAI TTS): spoken commands carry audio_url; any failure leaves the car's own voice."""
import asyncio, random

from fastapi.testclient import TestClient

from roadpod.engine import Director, MemoryStore
from roadpod.voice import Voice


def speak_cmd(d, sid, text='안녕하세요, 큐큐예요.'):
    return d.cmd(sid, 'dj.speak', {'text': text, 'voice_style': 'CALM', 'interrupt_policy': 'DROP_IF_NAV'})


def voice_director(tmp_path, monkeypatch, maker):
    monkeypatch.setenv('TTS_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    d = Director(MemoryStore(), rng=random.Random(1))
    d.voice = Voice(tmp_path, 'http://car-can-reach:8765')
    calls = []

    async def fake_make(name, text, instructions):
        calls.append((text, instructions))
        return await maker(tmp_path, name)
    d.voice._make = fake_make
    return d, calls


async def _ok(dir_, name):
    (dir_ / name).write_bytes(b'ID3fake')
    return name


def test_spoken_line_gets_audio_url_and_is_served(tmp_path, monkeypatch):
    d, calls = voice_director(tmp_path, monkeypatch, _ok)
    sid = d.start('client-1', 'FROM_WORK')
    c = asyncio.run(d.outgoing(speak_cmd(d, sid)))
    url = c['payload']['audio_url']
    assert url.startswith('http://car-can-reach:8765/tts/') and url.endswith('.mp3')
    assert '퇴근길' in calls[0][1]                       # CALM style → evening notes for the voice
    name = url.rsplit('/', 1)[1]
    assert d.voice.path(name) == tmp_path / name

    import roadpod.main as main
    monkeypatch.setattr(main.director, 'voice', d.voice)
    r = TestClient(main.app).get(f'/tts/{name}')
    assert r.status_code == 200 and r.content == b'ID3fake' and r.headers['content-type'] == 'audio/mpeg'
    assert TestClient(main.app).get('/tts/..%2F.env').status_code == 404


def test_same_line_is_made_once(tmp_path, monkeypatch):
    d, calls = voice_director(tmp_path, monkeypatch, _ok)
    sid = d.start('client-1', 'TO_WORK')

    async def run():
        d.voice.prefetch('같은 문장이에요.', 'CALM')       # e.g. queued while another line plays
        a = await d.outgoing(speak_cmd(d, sid, '같은 문장이에요.'))
        b = await d.outgoing(speak_cmd(d, sid, '같은 문장이에요.'))
        return a, b
    a, b = asyncio.run(run())
    assert a['payload']['audio_url'] == b['payload']['audio_url'] and len(calls) == 1


def test_story_is_read_in_story_voice(tmp_path, monkeypatch):
    d, calls = voice_director(tmp_path, monkeypatch, _ok)
    sid = d.start('client-1', 'TO_WORK')
    c = asyncio.run(d.outgoing(d.cmd(sid, 'story.incoming', {'story_id': 'st-1', 'text': '팀장님 말투 때문에 작아져요.'})))
    assert c['payload']['audio_url'] and '사연' in calls[0][1]


def test_failure_or_slowness_leaves_the_cars_own_voice(tmp_path, monkeypatch):
    async def fail(dir_, name):
        return None

    async def slow(dir_, name):
        await asyncio.sleep(1)
        return await _ok(dir_, name)
    for maker in (fail, slow):
        d, _ = voice_director(tmp_path, monkeypatch, maker)
        d.voice.timeout = 0.05
        sid = d.start('client-1', 'TO_WORK')
        c = asyncio.run(d.outgoing(speak_cmd(d, sid)))
        assert 'audio_url' not in c['payload'] and c['payload']['text']


def test_off_by_default(tmp_path, monkeypatch):
    d = Director(MemoryStore(), rng=random.Random(1))
    sid = d.start('client-1', 'TO_WORK')
    c = asyncio.run(d.outgoing(speak_cmd(d, sid)))
    assert not d.voice.enabled and 'audio_url' not in c['payload']
    monkeypatch.setenv('TTS_PROVIDER', 'openai')            # no key → still off
    assert not Voice(tmp_path, 'http://x').enabled


class FakeFish:
    """Stands in for httpx.AsyncClient: records what would be sent to Fish Audio."""
    sent = []

    def __init__(self, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, headers, json):
        FakeFish.sent.append((url, headers, json))
        class R: status_code = 200; content = b'ID3fish'
        return R()


def fish_voice(tmp_path, monkeypatch, max_chars):
    import httpx
    monkeypatch.setattr(httpx, 'AsyncClient', FakeFish)
    FakeFish.sent = []
    monkeypatch.setenv('TTS_PROVIDER', 'fish')
    monkeypatch.setenv('FISH_API_KEY', 'test-fish-key')
    monkeypatch.setenv('FISH_REFERENCE_ID', 'voice-123')
    monkeypatch.setenv('FISH_MAX_CHARS', str(max_chars))
    return Voice(tmp_path, 'http://x')


def test_fish_request_shape_and_cache(tmp_path, monkeypatch):
    v = fish_voice(tmp_path, monkeypatch, 1000)
    assert v.enabled
    url1 = asyncio.run(v.url('안녕하세요, 큐큐예요.', 'CALM'))
    url2 = asyncio.run(Voice(tmp_path, 'http://x').url('안녕하세요, 큐큐예요.', 'CALM'))   # after a restart: cached file
    assert url1 and url1 == url2 and len(FakeFish.sent) == 1
    url, headers, body = FakeFish.sent[0]
    assert headers['model'] == 's2.1-pro' and headers['Authorization'] == 'Bearer test-fish-key'
    assert body == {'text': '안녕하세요, 큐큐예요.', 'reference_id': 'voice-123', 'format': 'mp3',
                    'prosody': {'speed': 1.0, 'volume': 0}}
    assert v.used_chars() == len('안녕하세요, 큐큐예요.')


def test_fish_stops_at_the_budget(tmp_path, monkeypatch):
    v = fish_voice(tmp_path, monkeypatch, 20)
    assert asyncio.run(v.url('열 글자 정도의 문장', 'CALM'))          # 11 chars: within 20
    assert asyncio.run(v.url('이건 예산을 넘는 두 번째 문장', 'CALM')) is None   # would exceed → car's own voice
    assert len(FakeFish.sent) == 1


def test_fish_off_without_key_or_voice(tmp_path, monkeypatch):
    monkeypatch.setenv('TTS_PROVIDER', 'fish')
    monkeypatch.setenv('FISH_REFERENCE_ID', 'voice-123')
    assert not Voice(tmp_path, 'http://x').enabled
    monkeypatch.setenv('FISH_API_KEY', 'k'); monkeypatch.delenv('FISH_REFERENCE_ID')
    assert not Voice(tmp_path, 'http://x').enabled
