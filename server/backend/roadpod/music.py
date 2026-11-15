"""음악 코너 (C: 저작권 걱정 없는 음원을 서버가 직접 편성).

The server picks a royalty-free track that fits the commute (bright mornings, calm evenings) and airs it between
corners of an episode as a `music.play` command. The car plays `url` and ACKs COMPLETED when the track ends.
Because the server knows what it played, "이 노래 드랍해줘" works for these tracks too.

Music the driver plays themselves (멜론, 라디오 …) is the app's side (A): it reports the song with
`user.action: now_playing`, and the server never tries to play or pick it.

Tracks live in data/music.json; audio files go in data/music/ and are served at /music/<file>.
Only add files whose licence allows this use (e.g. Pixabay Content License, CC0, CC BY with credit).
"""
from __future__ import annotations
import json, os, random
from dataclasses import dataclass
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'
CATALOG_PATH = DATA / 'music.json'
FILES = DATA / 'music'
MAX_PER_EPISODE = 2

INTRO = {
    'TO_WORK': ['노래 한 곡 듣고 이어 갈게요. {title}.', '잠깐 음악으로 숨 한번 고르고 갈게요. {title}.'],
    'FROM_WORK': ['잠시 음악 한 곡 들으면서 쉬어 가요. {title}.', '천천히, 노래 한 곡 듣고 이어 갈게요. {title}.'],
}
INTRO_DEFAULT = ['노래 한 곡 듣고 이어 갈게요. {title}.']


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    file: str
    duration_seconds: int
    commute_fit: dict
    license: str
    source_url: str

    @property
    def label(self):
        return f'{self.title} - {self.artist}' if self.artist else self.title

    def available(self):
        return (FILES / self.file).is_file()


def every():
    """MUSIC_EVERY: a music break after this many spoken body segments (0 turns the corner off)."""
    try:
        return max(0, int(os.getenv('MUSIC_EVERY') or 3))
    except ValueError:
        return 3


def load(path: Path = CATALOG_PATH) -> list[Track]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    return [Track(t['id'], t['title'], t.get('artist', ''), t['file'], int(t['duration_seconds']),
                  t.get('commute_fit', {}), t.get('license', ''), t.get('source_url', '')) for t in data.get('tracks', [])]


def pick(tracks: list[Track], commute_type: str, played: set, rng: random.Random) -> Track | None:
    fits = [t for t in tracks if float(t.commute_fit.get(commute_type, 0.5)) >= 0.5] or list(tracks)
    pool = [t for t in fits if t.id not in played] or fits   # bright mornings, calm evenings; repeat before mismatching
    if not pool:
        return None
    weights = [max(0.05, float(t.commute_fit.get(commute_type, 0.5))) for t in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def intro(track: Track, commute_type: str, rng: random.Random):
    return rng.choice(INTRO.get(commute_type, INTRO_DEFAULT)).format(title=track.label)


def payload(track: Track, base_url: str):
    return {'track_id': track.id, 'title': track.title, 'artist': track.artist,
            'url': f'{base_url}/music/{track.file}', 'duration_seconds': track.duration_seconds,
            'license': track.license}
