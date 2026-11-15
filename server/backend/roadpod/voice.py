"""큐큐 voice: text-to-speech made on the server, played by the car as an mp3.

The API key stays on the server (never in the car app). Each dj.speak / story.incoming gets an `audio_url`;
the car plays it and falls back to its own on-device voice when the field is missing or the file fails.

TTS_PROVIDER picks the engine (off when empty):
  openai  gpt-4o-mini-tts (OPENAI_API_KEY); tone and pace come from the instructions below.
  fish    Fish Audio custom voice (FISH_API_KEY, FISH_REFERENCE_ID). Expensive: meant for the final demo video only,
          so it is capped by FISH_MAX_CHARS (characters actually generated, counted across restarts).
Files are cached by (engine, voice, style, text): replaying the same demo costs nothing.
"""
from __future__ import annotations
import asyncio, hashlib, json, logging, os
from pathlib import Path

log = logging.getLogger('roadpod')

# How 큐큐 should sound. gpt-4o-mini-tts follows these like a voice director's notes (it has no speed knob).
BASE_INSTRUCTIONS = ('한국어 라디오 DJ "큐큐"입니다. 운전 중인 청취자에게 말하듯 자연스럽고 다정하게, '
                     '서두르지 말고 조금 느긋한 속도로 말하세요. 문장 사이에는 짧게 숨을 고르세요. '
                     '과장된 연기나 광고 같은 말투는 피하세요.')
STYLE_NOTES = {
    'BRIGHT': '출근길이라 밝고 기분 좋게, 하지만 들뜨지는 않게.',
    'CALM': '퇴근길이라 차분하고 편안하게, 하루를 마무리해 주듯이.',
    'WARM': '따뜻하고 편안하게.',
}
STORY_NOTE = '청취자가 보낸 사연을 대신 읽어 주는 부분입니다. 사연 속 사람의 마음이 전해지도록 조금 더 천천히, 담담하게 읽으세요.'

FILE_CHARS = set('0123456789abcdef')


class Voice:
    def __init__(self, cache_dir: Path, base_url: str):
        self.mode = (os.getenv('TTS_PROVIDER') or '').lower()
        if self.mode == 'fish':
            self.model = os.getenv('FISH_TTS_MODEL') or 's2.1-pro'
            self.voice = os.getenv('FISH_REFERENCE_ID') or ''
            self.speed = float(os.getenv('FISH_SPEED') or 1.0)
            self.max_chars = int(os.getenv('FISH_MAX_CHARS') or 15000)
        else:
            self.model = os.getenv('OPENAI_TTS_MODEL') or 'gpt-4o-mini-tts'
            self.voice = os.getenv('OPENAI_TTS_VOICE') or 'coral'
        self.timeout = float(os.getenv('TTS_WAIT_SECONDS') or 12)   # how long a line may wait for its audio
        self.dir = Path(cache_dir)
        self.base_url = base_url.rstrip('/')
        self._jobs: dict[str, asyncio.Task] = {}

    @property
    def enabled(self) -> bool:
        if self.mode == 'fish':
            return bool(os.getenv('FISH_API_KEY') and self.voice)
        return self.mode == 'openai' and bool(os.getenv('OPENAI_API_KEY'))

    # --- Fish Audio budget: characters sent for synthesis (cache hits are free) ---
    def _usage_file(self):
        return self.dir / 'fish_usage.json'

    def used_chars(self) -> int:
        try:
            return int(json.loads(self._usage_file().read_text()).get('chars', 0))
        except (OSError, ValueError):
            return 0

    def _add_usage(self, n):
        self.dir.mkdir(parents=True, exist_ok=True)
        total = self.used_chars() + n
        self._usage_file().write_text(json.dumps({'chars': total}))
        log.info('Fish TTS: %d / %d chars used', total, self.max_chars)

    @staticmethod
    def instructions(style: str | None, story: bool = False) -> str:
        notes = [BASE_INSTRUCTIONS, STYLE_NOTES.get(style or '', STYLE_NOTES['WARM'])]
        if story:
            notes.append(STORY_NOTE)
        return ' '.join(notes)

    def _name(self, text, instructions) -> str:
        if self.mode == 'fish':   # no instructions: the custom voice has one fixed tone
            instructions = f'fish speed={self.speed}'
        key = '\n'.join((self.model, self.voice, instructions, text))
        return hashlib.sha256(key.encode('utf-8')).hexdigest()[:32] + '.mp3'

    def path(self, name: str) -> Path | None:
        """The cached file for a /tts/<name> request, or None (plain generated names only)."""
        stem, _, ext = name.partition('.')
        if ext != 'mp3' or len(stem) != 32 or not set(stem) <= FILE_CHARS:
            return None
        p = self.dir / name
        return p if p.is_file() else None

    def prefetch(self, text: str, style: str | None, story: bool = False):
        """Start making the audio now (e.g. while the previous line is still playing)."""
        if self.enabled and text:
            self._job(text, self.instructions(style, story))

    async def url(self, text: str, style: str | None, story: bool = False) -> str | None:
        """URL of the line's audio, or None (voice off, too slow, or failed) → the car uses its own voice."""
        if not self.enabled or not text:
            return None
        task = self._job(text, self.instructions(style, story))
        try:
            name = await asyncio.wait_for(asyncio.shield(task), self.timeout)
        except asyncio.TimeoutError:
            log.warning('TTS took longer than %.0fs; the car will use its own voice', self.timeout)
            return None
        return f'{self.base_url}/tts/{name}' if name else None

    def _job(self, text, instructions) -> asyncio.Task:
        name = self._name(text, instructions)
        task = self._jobs.get(name)
        if task is None or (task.done() and task.result() is None):   # retry a failed one
            task = asyncio.get_running_loop().create_task(self._make(name, text, instructions))
            self._jobs[name] = task
        return task

    async def _make(self, name, text, instructions) -> str | None:
        path = self.dir / name
        if path.is_file():
            return name
        if self.mode == 'fish':
            return await self._make_fish(name, text)
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'], timeout=30.0, max_retries=1)
            audio = await client.audio.speech.create(model=self.model, voice=self.voice, input=text,
                                                     instructions=instructions, response_format='mp3')
            data = audio.content
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.part')
            tmp.write_bytes(data)
            tmp.replace(path)
            return name
        except Exception as e:
            log.warning('TTS failed (%s); the car will use its own voice', type(e).__name__)
            return None

    async def _make_fish(self, name, text) -> str | None:
        path = self.dir / name
        if self.used_chars() + len(text) > self.max_chars:
            log.warning('Fish TTS budget reached (%d chars); the car will use its own voice', self.max_chars)
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    os.getenv('FISH_TTS_URL') or 'https://api.fish.audio/v1/tts',
                    headers={'Authorization': f"Bearer {os.environ['FISH_API_KEY']}", 'model': self.model},
                    json={'text': text, 'reference_id': self.voice, 'format': 'mp3',
                          'prosody': {'speed': self.speed, 'volume': 0}})
            if r.status_code != 200 or not r.content:
                log.warning('Fish TTS failed (HTTP %s); the car will use its own voice', r.status_code)
                return None
            self._add_usage(len(text))
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.part')
            tmp.write_bytes(r.content)
            tmp.replace(path)
            return name
        except Exception as e:
            log.warning('Fish TTS failed (%s); the car will use its own voice', type(e).__name__)
            return None
