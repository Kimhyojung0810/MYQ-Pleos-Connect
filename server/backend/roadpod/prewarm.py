"""Produce episodes ahead of time so no listener waits for research.

    ROADPOD_DB=roadpod.sqlite PYTHONPATH=backend python -m roadpod.prewarm --category SOCIETY
    ROADPOD_DB=roadpod.sqlite PYTHONPATH=backend python -m roadpod.prewarm --all

Needs LLM_PROVIDER=openai and a key (costs one research call per topic and 1–2 writer calls per topic × commute),
and ROADPOD_DB set to the same file the server uses, otherwise the results vanish when this process exits.
Cached topics are skipped, so re-running only fills gaps and refreshes expired 시사 topics.
"""
from __future__ import annotations
import argparse, asyncio, os, sys, time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / '.env')

from .engine import MemoryStore  # noqa: E402
from .llm import TextProvider  # noqa: E402
from .producer import EpisodeProducer  # noqa: E402
from .topics import Catalog  # noqa: E402


async def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--all', action='store_true', help='every topic in the catalog')
    ap.add_argument('--category', nargs='*', default=[], help='MIND RELATION WORK CULTURE SOCIETY')
    ap.add_argument('--topic', nargs='*', default=[], help='topic keys')
    ap.add_argument('--commute', nargs='*', default=['TO_WORK', 'FROM_WORK'])
    ap.add_argument('--concurrency', type=int, default=3)
    args = ap.parse_args(argv)

    db = os.getenv('ROADPOD_DB')
    if not db:
        sys.exit('Set ROADPOD_DB to the SQLite file the server uses; an in-memory cache would be thrown away.')
    llm = TextProvider()
    if not llm.live:
        sys.exit('Needs LLM_PROVIDER=openai and OPENAI_API_KEY in .env.')
    catalog = Catalog()
    topics = [t for t in catalog.topics if args.all or t.category in args.category or t.key in args.topic]
    if not topics:
        sys.exit('Nothing selected. Use --all, --category or --topic.')

    producer = EpisodeProducer(MemoryStore(db), llm)
    jobs = [(t, c) for t in topics for c in args.commute]
    todo = [(t, c) for t, c in jobs if producer.cached(t, c) is None]
    print(f'{len(jobs)} episodes selected, {len(jobs) - len(todo)} already cached, producing {len(todo)} '
          f'(research={llm.research_model}, writer={llm.writer_model})')
    gate = asyncio.Semaphore(args.concurrency)

    async def one(topic, commute):
        async with gate:
            started = time.time()
            segments = await producer.produce(topic, commute)
            row = producer.store.get_script(topic.key, commute)
            mode = row['mode'] if row else 'catalog'
            chars = sum(len(s['text']) for s in segments)
            print(f'  {topic.key:<20} {commute:<9} {mode:<16} {chars:>5}자 ~{chars / 330:.1f}분  {time.time() - started:.0f}s')

    await asyncio.gather(*(one(t, c) for t, c in todo))


if __name__ == '__main__':
    asyncio.run(main())
