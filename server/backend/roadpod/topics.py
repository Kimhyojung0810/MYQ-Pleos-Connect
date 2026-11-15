"""Topic catalog and per-listener topic selection (commute-aware, history-aware, with exploration)."""
from __future__ import annotations
import json, math, random
from dataclasses import dataclass
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent / 'data' / 'topics.json'


@dataclass(frozen=True)
class Topic:
    key: str
    category: str
    question: str
    summary: str
    angles: tuple
    commute_fit: dict
    min_minutes: int
    fresh_sources: bool  # needs recent, dated sources (시사·사회)
    sensitivity: str     # 'normal' | 'careful' → writer must stay balanced and take no side
    search_hint: str

    def fit(self, commute_type):
        return float(self.commute_fit.get(commute_type, 0.7))


class Catalog:
    def __init__(self, path=CATALOG_PATH):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        self.categories: dict[str, str] = data['categories']
        self.topics: list[Topic] = []
        for t in data['topics']:
            if t['category'] not in self.categories:
                raise ValueError(f"topic {t['key']} has unknown category {t['category']}")
            self.topics.append(Topic(t['key'], t['category'], t['question'], t['summary'], tuple(t['angles']),
                                     t['commute_fit'], int(t['min_minutes']), bool(t['fresh_sources']),
                                     t['sensitivity'], t['search_hint']))
        self.by_key = {t.key: t for t in self.topics}
        if len(self.by_key) != len(self.topics):
            raise ValueError('duplicate topic keys in catalog')

    def get(self, key):
        return self.by_key.get(key)

    def add(self, topic: Topic):
        """Topics grown from listeners' stories (approved candidates) join the pool at runtime."""
        if topic.category not in self.categories or topic.key in self.by_key:
            return False
        self.topics.append(topic)
        self.by_key[topic.key] = topic
        return True


# Listening signals. Speaking up or leaving a story is the strongest sign a topic worked.
SIGNAL_COMPLETED = 1.0
SIGNAL_ENGAGED = 2.0
SIGNAL_EARLY_SKIP = -2.0   # skipped after hearing at most the opening
SIGNAL_LATE_SKIP = -1.0


class TopicSelector:
    def __init__(self, catalog: Catalog, store, rng: random.Random | None = None,
                 explore_rate=0.25, no_repeat_days=14):
        self.catalog = catalog
        self.store = store
        self.rng = rng or random.Random()
        self.explore_rate = explore_rate
        self.no_repeat_days = no_repeat_days

    def category_affinity(self, client_id, commute_type) -> dict[str, float]:
        """Per-category preference in [-1, 1] from this commute's history only (출근·퇴근 분리 메모리)."""
        raw: dict[str, float] = {}
        for row in self.store.topic_history(client_id, commute_type):
            score = 0.0
            if row['skipped']:
                score += SIGNAL_EARLY_SKIP if row['segments_heard'] <= 1 else SIGNAL_LATE_SKIP
            if row['completed']:
                score += SIGNAL_COMPLETED
            if row['engaged']:
                score += SIGNAL_ENGAGED
            raw[row['category']] = raw.get(row['category'], 0.0) + score
        return {c: math.tanh(v / 3) for c, v in raw.items()}

    def pick(self, client_id, commute_type, *, eta_seconds=None, exclude=(), explore=False) -> Topic:
        recent = set(self.store.recent_topic_keys(client_id, self.no_repeat_days))
        pool = [t for t in self.catalog.topics if t.key not in exclude and t.key not in recent]
        if not pool:  # heard everything recently: allow repeats rather than go silent
            pool = [t for t in self.catalog.topics if t.key not in exclude] or list(self.catalog.topics)

        if eta_seconds is not None:
            remaining = eta_seconds / 60
            fits = [t for t in pool if t.min_minutes <= remaining]
            if not fits:
                shortest = min(t.min_minutes for t in pool)
                fits = [t for t in pool if t.min_minutes == shortest]
            pool = fits

        interests = set(self.store.get_interests(client_id))
        if interests:
            preferred = [t for t in pool if t.category in interests]
            pool = preferred or pool

        if explore or self.rng.random() < self.explore_rate:
            # Exploration: least-heard categories in this commute, so taste doesn't collapse to one category.
            counts = self.store.category_counts(client_id, commute_type)
            least = min(counts.get(t.category, 0) for t in pool)
            candidates = [t for t in pool if counts.get(t.category, 0) == least]
            return self._weighted(candidates, [t.fit(commute_type) for t in candidates])

        affinity = self.category_affinity(client_id, commute_type)
        scored = sorted(pool, key=lambda t: t.fit(commute_type) + 0.6 * affinity.get(t.category, 0.0), reverse=True)
        top = scored[:3]  # some variety instead of the same argmax every trip
        weights = [max(0.05, t.fit(commute_type) + 0.6 * affinity.get(t.category, 0.0)) for t in top]
        return self._weighted(top, weights)

    def _weighted(self, items, weights):
        return self.rng.choices(items, weights=[max(w, 0.01) for w in weights], k=1)[0]
