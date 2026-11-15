"""Episode production: two LLM roles with code checks in between.

  1) Researcher (LLM + web_search) → research brief: dated facts with source URLs, perspectives, questions
  2) Grounding check (code)        → keep only facts whose URL the search tool actually returned
  3) Script writer (LLM)           → spoken segments that cite fact ids
  4) Script check (code)           → drop unknown refs and unsourced statistics, enforce length limits

Any failure degrades instead of breaking the broadcast:
  research fails → 'discussion_only' brief (perspectives, no facts) → writer still writes, with no factual claims
  writer fails   → deterministic catalog script.
Briefs are cached per topic (shared by all listeners); scripts per topic + commute type.
"""
from __future__ import annotations
import asyncio, json, logging, os, re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, date
from urllib.parse import urlparse, unquote

from .topics import Topic

log = logging.getLogger('roadpod')

WRITER_ROLES = ('FRAME', 'PERSPECTIVE', 'BOOK', 'OUTRO')  # BOOK: 퇴근길 '책 한 권' corner, only when research found a real book  # HOOK is aired instantly from the catalog; STORY_SLOT waits for Story Relay
MAX_SEGMENT_CHARS = 450        # dj.speak contract allows 500
MIN_SCRIPT_CHARS = 1500        # ~4.5 min at ~330 chars/min; shorter drafts get one retry
FRESH_MAX_AGE_DAYS = 730       # 시사 facts older than this are dropped; the writer must say the year aloud
MAX_FACTS_PER_SOURCE = 3
MAX_FACTS = 8
# User-generated or self-published platforms are not sources. Live test: two personal blogs supplied all 18
# "facts" for one topic and the script then called a blog post "a study".
BLOCKED_HOSTS = ('tistory.com', 'blog.naver.com', 'cafe.naver.com', 'brunch.co.kr', 'velog.io', 'medium.com',
                 'blogspot.com', 'wordpress.com', 'imweb.me', 'pages.dev', 'vercel.app', 'github.io', 'namu.wiki',
                 'youtube.com', 'instagram.com', 'facebook.com', 'x.com', 'twitter.com', 'reddit.com', 'quora.com')
# Parenthetical citations like "(fnnews.com, 2026)" would be read aloud by TTS.
CITATION_IN_TEXT = re.compile(r'\s*\((?=[^)]*(\.(com|kr|net|org|go|ac)\b|,\s*\d{4}))[^)]{1,60}\)')
FACT_ID_IN_TEXT = re.compile(r'\s*[(\[]\s*F\d+(\s*[,·/]\s*F\d+)*\s*[)\]]')
# Script-structure labels are production talk, not radio. Live run: a FRAME opened with "HOOK은 이미 던져졌죠."
PRODUCTION_TALK = re.compile(r'(?<![A-Za-z])(HOOK|FRAME|PERSPECTIVE|OUTRO|STORY_SLOT)(?![A-Za-z])|세그먼트', re.I)
TTL_FRESH = timedelta(hours=12)
TTL_EVERGREEN = timedelta(days=30)

# A segment that states numbers or cites "research" without a fact id is treated as unsourced.
UNSOURCED_STAT = re.compile(r'\d[\d,.]*\s*(%|퍼센트|배|명|억|조|만\s*명|개국|달러|원)')
UNSOURCED_CLAIM = re.compile(r'(연구|조사|보고서|통계|논문|실험)\s*(에\s*따르면|결과|에서\s*밝혀)')

RESEARCH_SYSTEM = """너는 차량용 팟캐스트 ROADPOD의 리서처다. 주어진 질문으로 5~8분짜리 오디오 에피소드를 만들 재료를 웹에서 찾아 정리한다.
규칙:
- 반드시 web_search로 실제 자료를 찾고, 찾은 자료에 있는 내용만 쓴다. 기억에 의존해 출처를 만들지 않는다.
- facts에는 검증 가능한 사실·통계·연구 결과·사례만 넣는다. 각 항목에 실제로 열람한 source_url과 게시일(published, YYYY-MM-DD, 모르면 null)을 단다.
- 신뢰할 수 있는 출처(공공기관, 학술지, 주요 언론, 연구기관)만 쓴다. 개인 블로그(티스토리, 네이버 블로그 등), 카페, 커뮤니티, 나무위키, 계산기·쇼핑·부동산 사이트는 출처로 쓰지 않는다.
- 같은 출처에서 비슷한 내용을 여러 항목으로 쪼개지 않는다. 서로 다른 출처에서 3~8개를 고른다.
- 최근 12개월 자료를 우선하고, 시사 주제는 최대 2년 이내 자료만 쓴다.
- 심리·관계·일·문화 주제는 영어로도 검색해 학술지, 대학, 공공기관, 주요 언론의 자료를 찾는다. claim은 한국어로 쓴다.
- perspectives에는 서로 긴장 관계에 있는 관점을 공정하게 담는다. 한쪽 편을 들지 않는다.
- 결과는 아래 JSON 하나만 출력한다. 설명 문장, 코드펜스 금지.
{"facts":[{"id":"F1","claim":"한국어 한두 문장","source_title":"...","publisher":"자료를 낸 매체·학술지·기관 이름(예: 연합뉴스, Nature Human Behaviour, 고용노동부). PubMed·Google Scholar 같은 검색 DB 이름은 쓰지 않는다","source_url":"https://...","published":"YYYY-MM-DD"}],
 "perspectives":[{"id":"P1","position":"관점 한 줄","reasoning":"근거 두세 문장"}],
 "open_questions":["청취자에게 던질 만한 질문"],
 "book":{"title":"책 제목","author":"저자","idea":"책의 핵심 아이디어 한두 문장(직접 인용 아님)","source_url":"https://..."}}
- book에는 질문과 관련된 실제 책 한 권을 검색해서 확인한 경우에만 넣는다. source_url은 출판사·서점·언론의 책 소개 페이지. 확인하지 못하면 null."""

WRITER_SYSTEM = """너는 차량용 팟캐스트 ROADPOD의 DJ '큐큐'의 원고 작가다. 출퇴근 운전자가 듣는 한국어 오디오 에세이를 쓴다.
규칙:
- 하나의 질문을 깊게 다룬다. 정보 나열식 브리핑이 아니라 생각을 따라가는 이야기로 쓴다.
- 오프닝(HOOK)은 이미 방송됐다. FRAME으로 시작해서 PERSPECTIVE 여러 개를 거쳐 OUTRO로 끝낸다.
- HOOK, FRAME, PERSPECTIVE, OUTRO, 세그먼트 같은 구성 용어는 role에만 쓰고 본문(text)에는 절대 쓰지 않는다. "오프닝은 이미 나갔죠" 같은 제작 이야기도 하지 않는다. 오프닝에서 던진 질문을 다시 읽지 말고 바로 이야기로 들어간다.
- 사실·통계·연구를 말할 때는 브리프의 facts에 있는 것만 쓰고, 그 세그먼트의 source_refs에 fact id를 넣고, 말로도 출처를 밝힌다.
- 출처는 publisher와 published 연도를 말로 풀어 쓴다("2025년 연합뉴스 보도에 따르면"). 웹 주소나 괄호 표기는 쓰지 않는다. 기사나 칼럼을 '연구'라고 부르지 않는다.
- 외국 학술지·기관 이름은 귀로 듣기 쉽게 한국어로 풀어 말한다("영국의 한 지방의회", "미국의 실험심리학 학술지"). 단, 네이처처럼 널리 알려진 이름은 그대로 써도 된다.
- PubMed, Google Scholar 같은 검색 데이터베이스를 출처처럼 말하지 않는다.
- 본문(text)에 F1 같은 id나 괄호 표기를 절대 쓰지 않는다. id는 source_refs에만 넣는다.
- 출근길·퇴근길 언급은 FRAME에서 한 번만 한다.
- 브리프에 없는 숫자, 연구, 책, 인용, 기사, 사례를 지어내지 않는다. facts에 없는 고유명사(지역, 국가, 기관, 기업, 인물, 책 제목)도 쓰지 않는다.
- facts가 비어 있으면 사실 주장 없이 관점, 일상적인 장면, 질문으로만 구성한다.
- 서로 다른 관점을 공정하게 들려준다. DJ가 정답을 정해 주지 않는다.
- 운전 중이므로 화면을 보거나 무언가를 조작하라고 하지 않는다. 대답을 강요하지 않는다.
- 진단하거나 청취자의 심리 상태를 판단하지 않는다.
- 말하듯 짧은 문장을 쓴다. 세그먼트 하나는 생각 하나, 280~420자.
- 분량을 반드시 채운다: 세그먼트 8~10개, 전체 2400~3000자. 관점마다 '왜 그렇게 보는지 → 일상의 장면 → 반대쪽에서 보면'으로 깊이 들어간다.
- 마지막 OUTRO는 결론을 내리지 말고 청취자가 곱씹을 질문 하나로 닫는다.
- 입력에 book이 있으면 OUTRO 바로 앞에 role이 BOOK인 세그먼트 하나를 넣는다: "책 한 권" 코너로, 제목과 저자를 밝히고 book.idea만 바탕으로 2~3문장. 책에 없는 내용을 지어내지 않고, 책 문장을 길게 그대로 읽지 않는다.
- 결과는 아래 JSON 하나만 출력한다. 설명 문장, 코드펜스 금지.
{"segments":[{"role":"FRAME|PERSPECTIVE|BOOK|OUTRO","text":"...","source_refs":["F1"]}]}"""

CAREFUL_NOTE = '이 주제는 민감할 수 있다. 어떤 입장도 지지하거나 비판하지 말고, 각 관점을 그 관점을 가진 사람들의 이유와 함께 소개한다.'


@dataclass
class Brief:
    facts: list
    perspectives: list
    open_questions: list
    mode: str  # 'researched' | 'discussion_only'
    book: dict | None = None  # {title, author, idea, source_url}: kept only if the search tool returned its page


@dataclass
class Script:
    segments: list  # [{role, text, source_refs}]
    mode: str       # 'researched' | 'discussion_only' | 'catalog'
    brief: Brief


def utcnow():
    return datetime.now(timezone.utc)


def _parse_json(text):
    if not text:
        return None
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _norm_url(url):
    """Compare URLs by host + path; the search tool often appends tracking queries like ?utm_source=openai."""
    try:
        p = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    if p.scheme not in ('http', 'https') or not p.netloc:
        return None
    host = p.netloc.lower().removeprefix('www.')
    return host + unquote(p.path.rstrip('/') or '')


def _host(url):
    try:
        return urlparse(url).netloc.lower().removeprefix('www.')
    except (ValueError, AttributeError):
        return ''


def _parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def ground_brief(raw, cited_urls, topic: Topic, today: date | None = None) -> Brief:
    """Keep only facts whose source the web_search tool actually returned (and that are recent, for 시사)."""
    today = today or utcnow().date()
    cited = {_norm_url(u) for u in cited_urls} - {None}
    facts, seen_ids, seen_claims, per_source = [], set(), set(), {}
    for f in (raw or {}).get('facts') or []:
        if not isinstance(f, dict):
            continue
        fid, claim, url = str(f.get('id') or ''), str(f.get('claim') or '').strip(), str(f.get('source_url') or '')
        if not fid or fid in seen_ids or not claim or _norm_url(url) not in cited:
            continue
        published = _parse_date(f.get('published'))
        if topic.fresh_sources and (published is None or (today - published).days > FRESH_MAX_AGE_DAYS):
            continue
        host = _host(url)
        if any(host == b or host.endswith('.' + b) for b in BLOCKED_HOSTS):
            continue
        if per_source.get(host, 0) >= MAX_FACTS_PER_SOURCE or claim in seen_claims:
            continue
        seen_ids.add(fid); seen_claims.add(claim); per_source[host] = per_source.get(host, 0) + 1
        facts.append({'id': fid, 'claim': claim[:400], 'source_title': str(f.get('source_title') or '')[:200],
                      'publisher': host, 'publisher_name': str(f.get('publisher') or '')[:100] or None, 'source_url': url, 'published': published.isoformat() if published else None})
        if len(facts) >= MAX_FACTS:
            break
    domains = {f['publisher'] for f in facts}
    # 시사 needs at least two independent sources before we air any factual claim.
    enough = len(facts) >= 2 and len(domains) >= 2 if topic.fresh_sources else len(facts) >= 1
    # Keep only the position line. Live test: unverified reasoning text carried made-up facts into the script.
    perspectives = [{'id': str(p.get('id') or f'P{i + 1}'), 'position': str(p['position'])[:160], 'reasoning': ''}
                    for i, p in enumerate((raw or {}).get('perspectives') or []) if isinstance(p, dict) and p.get('position')]
    questions = [str(q) for q in (raw or {}).get('open_questions') or [] if isinstance(q, str)]
    if not enough:
        facts = []
    if not perspectives:
        perspectives = catalog_perspectives(topic)
    return Brief(facts, perspectives[:4], questions[:4], 'researched' if facts else 'discussion_only',
                 ground_book((raw or {}).get('book'), cited))


def ground_book(book, cited):
    """A book for the '책 한 권' corner, only if its page came back from the search tool (no remembered books)."""
    if not isinstance(book, dict):
        return None
    title, author, idea, url = (str(book.get(k) or '').strip() for k in ('title', 'author', 'idea', 'source_url'))
    host = _host(url) if url else ''
    if not (title and author and idea) or _norm_url(url) not in cited or any(host == b or host.endswith('.' + b) for b in BLOCKED_HOSTS):
        return None
    return {'title': title[:80], 'author': author[:60], 'idea': idea[:300], 'source_url': url}


def catalog_perspectives(topic: Topic):
    return [{'id': f'P{i + 1}', 'position': a, 'reasoning': ''} for i, a in enumerate(topic.angles)]


def _split_spoken(text, limit=MAX_SEGMENT_CHARS):
    """Split at sentence ends so no dj.speak exceeds the contract limit."""
    text = ' '.join(text.split())
    if len(text) <= limit:
        return [text]
    parts, current = [], ''
    for sentence in re.split(r'(?<=[.!?。…])\s+', text):
        if not sentence:
            continue
        candidate = (current + ' ' + sentence).strip()
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                parts.append(current)
            current = sentence[:limit]
    if current:
        parts.append(current)
    return parts


def strip_production_talk(text):
    """Drop whole sentences that mention script labels; trimming just the word would leave broken grammar."""
    if not PRODUCTION_TALK.search(text):
        return text
    kept = [s for s in re.split(r'(?<=[.!?。…])\s+', text) if s and not PRODUCTION_TALK.search(s)]
    return ' '.join(kept).strip()


def clean_segments(segments):
    """Applied to cached scripts too, so episodes saved before a filter existed are repaired on read."""
    out = []
    for seg in segments:
        text = strip_production_talk(seg['text'])
        if text:
            out.append(dict(seg, text=text) if text != seg['text'] else seg)
    return out


def check_script(raw, brief: Brief):
    """Validate writer output. Returns segments or None if too little survives."""
    fact_ids = {f['id'] for f in brief.facts}
    out = []
    for seg in (raw or {}).get('segments') or []:
        if not isinstance(seg, dict):
            continue
        role = seg.get('role')
        text = CITATION_IN_TEXT.sub('', FACT_ID_IN_TEXT.sub('', str(seg.get('text') or ''))).strip()  # TTS reads these aloud
        text = strip_production_talk(text)
        if role not in WRITER_ROLES or not text:
            continue
        if role == 'BOOK':
            if not brief.book:   # no verified book: the writer must not invent one
                continue
            for piece in _split_spoken(text):   # the book page is its source; no fact ids
                out.append({'role': 'BOOK', 'text': piece, 'source_refs': []})
            continue
        refs = [r for r in seg.get('source_refs') or [] if r in fact_ids]
        if not refs and (UNSOURCED_STAT.search(text) or UNSOURCED_CLAIM.search(text)):
            log.info('dropping unsourced %s segment', role)
            continue
        for piece in _split_spoken(text):
            out.append({'role': role, 'text': piece, 'source_refs': refs})
    if len(out) < 3 or not any(s['role'] == 'PERSPECTIVE' for s in out):
        return None
    if out[-1]['role'] != 'OUTRO':
        out.append(_outro())
    return out


def _outro():
    return {'role': 'OUTRO', 'text': '오늘의 질문은 여기까지예요. 생각이 남았다면 편할 때 큐큐를 불러 주세요.', 'source_refs': []}


def _chars(segments):
    return sum(len(s['text']) for s in segments)


def catalog_script(topic: Topic):
    """Deterministic script for mock mode or when the writer fails. Makes no factual claims."""
    ordinals = ['첫 번째', '두 번째', '세 번째', '네 번째']
    segs = [{'role': 'FRAME', 'text': f'{topic.summary} 오늘은 이 질문을 몇 가지 관점에서 나눠 볼게요.', 'source_refs': []}]
    for i, angle in enumerate(topic.angles):
        tail = ' 여러분은 어느 쪽에 더 가까운가요? 꼭 대답하지 않으셔도 괜찮아요.' if i == len(topic.angles) - 1 else ''
        segs.append({'role': 'PERSPECTIVE', 'text': f'{ordinals[i]}로 생각해 볼 것은 {angle}이에요.{tail}', 'source_refs': []})
    segs.append(_outro())
    return segs


def hook_text(topic: Topic):
    return f'오늘 함께 생각해 볼 질문은 이거예요. {topic.question}'


class EpisodeProducer:
    def __init__(self, store, llm):
        self.store = store
        self.llm = llm
        self._inflight: dict[tuple, asyncio.Task] = {}

    @property
    def mock_delay(self):
        """MOCK_PRODUCTION_DELAY=12 makes mock mode pretend research takes 12 s, to rehearse the loading flow for free."""
        try:
            return max(0.0, float(os.getenv('MOCK_PRODUCTION_DELAY') or 0))
        except ValueError:
            return 0.0

    @property
    def instant(self):
        """Mock/no-key mode: production is synchronous and deterministic."""
        return not self.llm.live and self.mock_delay == 0

    def cached(self, topic: Topic, commute_type):
        row = self.store.get_script(topic.key, commute_type)
        if not row:
            return None
        ttl = TTL_FRESH if topic.fresh_sources else TTL_EVERGREEN
        if utcnow() - datetime.fromisoformat(row['created_at']) > ttl:
            return None
        return clean_segments(row['segments'])

    async def produce(self, topic: Topic, commute_type):
        """Return script segments; never raises. Concurrent requests for the same topic share one job."""
        hit = self.cached(topic, commute_type)
        if hit is not None:
            return hit
        key = (topic.key, commute_type)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._produce(topic, commute_type))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        return await asyncio.shield(task)

    async def _produce(self, topic, commute_type):
        try:
            if not self.llm.live:
                await asyncio.sleep(self.mock_delay)
                return catalog_script(topic)
            brief = await self._brief(topic)
            segments, mode = await self._write(topic, commute_type, brief), brief.mode
            if segments is None:
                segments, mode = catalog_script(topic), 'catalog'
            self.store.save_script(topic.key, commute_type, segments, mode)
            log.info('episode ready: %s/%s mode=%s facts=%d segments=%d',
                     topic.key, commute_type, mode, len(brief.facts), len(segments))
            return segments
        except Exception:
            log.exception('episode production failed for %s', topic.key)
            return catalog_script(topic)

    async def _brief(self, topic: Topic) -> Brief:
        row = self.store.get_brief(topic.key)
        ttl = TTL_FRESH if topic.fresh_sources else TTL_EVERGREEN
        if row and utcnow() - datetime.fromisoformat(row['created_at']) <= ttl:
            b = row['brief']
            return Brief(b['facts'], b['perspectives'], b['open_questions'], b['mode'], b.get('book'))
        prompt = json.dumps({
            'question': topic.question, 'summary': topic.summary, 'angles': list(topic.angles),
            'search_hint': topic.search_hint, 'today': utcnow().date().isoformat(),
            'freshness': f'최근 {FRESH_MAX_AGE_DAYS}일 이내 자료만' if topic.fresh_sources else '오래돼도 검증된 자료면 됨',
        }, ensure_ascii=False)
        result = await self.llm.complete(RESEARCH_SYSTEM, prompt, max_tokens=2500, timeout=90.0,
                                         web_search=True, model=self.llm.research_model,
                                         effort=getattr(self.llm, 'research_effort', None))
        if result is None:
            brief = Brief([], catalog_perspectives(topic), [], 'discussion_only')
        else:
            brief = ground_brief(_parse_json(result.text), result.cited_urls, topic)
        self.store.save_brief(topic.key, brief.__dict__)
        return brief

    async def _write(self, topic, commute_type, brief: Brief):
        payload = {
            'question': topic.question, 'already_aired_hook': hook_text(topic),
            'commute': {'TO_WORK': '출근길(하루를 시작하는 마음)', 'FROM_WORK': '퇴근길(하루를 정리하는 마음)'}.get(commute_type, '이동 중'),
            'target_length': '6~8분 분량(약 2000~2600자), PERSPECTIVE 3개 이상',
            'facts': [{'id': f['id'], 'claim': f['claim'], 'source_title': f['source_title'],
                       'publisher': f.get('publisher_name') or f['source_title'], 'published': f['published']} for f in brief.facts],
            'perspectives': brief.perspectives, 'open_questions': brief.open_questions,
            # 책 한 권 corner: evening listeners only (slower, reflective show), and only a verified book.
            'book': ({k: brief.book[k] for k in ('title', 'author', 'idea')} if brief.book and commute_type == 'FROM_WORK' else None),
        }
        system = WRITER_SYSTEM + ('\n- ' + CAREFUL_NOTE if topic.sensitivity == 'careful' else '')
        best = None
        for attempt in range(2):
            result = await self.llm.complete(system, json.dumps(payload, ensure_ascii=False), max_tokens=4000,
                                             timeout=90.0, model=self.llm.writer_model,
                                             effort=getattr(self.llm, 'writer_effort', None),
                                             verbosity=getattr(self.llm, 'writer_verbosity', None))
            segments = check_script(_parse_json(result.text), brief) if result else None
            if segments and (best is None or _chars(segments) > _chars(best)):
                best = segments
            if best and _chars(best) >= MIN_SCRIPT_CHARS:
                break
            got = _chars(best) if best else 0
            log.info('script too short (%d chars), retrying once', got)
            payload['revision_note'] = f'이전 원고는 {got}자로 너무 짧았다. 관점마다 더 깊이 들어가 전체 2400~3000자를 채워라.'
        return best
