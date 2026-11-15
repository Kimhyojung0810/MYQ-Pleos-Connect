"""LLM access. Deterministic mock by default; OpenAI only when explicitly enabled and a key is supplied."""
from __future__ import annotations
import logging, os
from dataclasses import dataclass, field

log = logging.getLogger('roadpod')


@dataclass
class Completion:
    text: str
    cited_urls: list[str] = field(default_factory=list)  # URLs the web_search tool actually returned


class TextProvider:
    def __init__(self):
        self.mode = os.getenv('LLM_PROVIDER', 'mock').lower()
        self.model = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
        # Research needs a model that supports the web_search tool; defaults to the chat model.
        self.research_model = os.getenv('OPENAI_RESEARCH_MODEL') or self.model
        self.writer_model = os.getenv('OPENAI_WRITER_MODEL') or self.model
        # Only for reasoning models (gpt-5*, o*): how hard to think, and how long to write.
        self.research_effort = os.getenv('OPENAI_RESEARCH_EFFORT') or 'low'
        self.writer_effort = os.getenv('OPENAI_WRITER_EFFORT') or 'medium'
        self.writer_verbosity = os.getenv('OPENAI_WRITER_VERBOSITY') or 'high'

    @property
    def live(self) -> bool:
        """True when real LLM calls will be made. False → callers use deterministic fallbacks instantly."""
        if self.mode != 'openai':
            return False
        if not os.getenv('OPENAI_API_KEY'):
            log.warning('LLM_PROVIDER=openai but OPENAI_API_KEY is empty; using fallback text')
            return False
        return True

    def _client(self, timeout):
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'], timeout=timeout, max_retries=1)

    async def generate(self, system_prompt, user_prompt, fallback):
        """Short in-drive reply (chat with the DJ)."""
        if not self.live:
            return fallback
        try:
            extra = {}
            if is_reasoning_model(self.model):  # reasoning tokens would otherwise eat the whole 180-token budget
                extra = {'max_output_tokens': 2000, 'reasoning': {'effort': 'low'}}
            answer = await self._client(12.0).responses.create(
                **{'model': self.model, 'instructions': system_prompt, 'input': user_prompt, 'max_output_tokens': 180, 'store': False, **extra})   # store=False: nothing kept in the OpenAI dashboard logs
            value = (answer.output_text or '').strip()
            return value[:480] if value else fallback
        except Exception as e:
            log.warning('LLM call failed (%s); using fallback text', type(e).__name__)
            return fallback

    async def complete(self, system_prompt, user_prompt, *, max_tokens, timeout, web_search=False, model=None,
                       effort=None, verbosity=None):
        """Long-form call for episode research/writing. Returns None when not live or on failure."""
        if not self.live:
            return None
        model = model or self.model
        kwargs = dict(model=model, instructions=system_prompt, input=user_prompt, max_output_tokens=max_tokens,
                      store=False)   # Responses API stores by default; keep nothing in the dashboard logs
        if is_reasoning_model(model):
            # Reasoning tokens count against max_output_tokens, so leave room for thinking.
            kwargs['max_output_tokens'] = max_tokens + 12000
            if effort:
                kwargs['reasoning'] = {'effort': effort}
            if verbosity and model.startswith('gpt-5'):
                kwargs['text'] = {'verbosity': verbosity}
            timeout = timeout * 2
        if web_search:
            kwargs['tools'] = [{'type': 'web_search', 'search_context_size': 'medium'}]
            # Live test: with a JSON-only instruction the model skipped searching and answered from memory.
            kwargs['tool_choice'] = 'required'
            kwargs['include'] = ['web_search_call.action.sources']
        try:
            answer = await self._client(timeout).responses.create(**kwargs)
        except Exception as e:
            log.warning('LLM %s call failed (%s)', 'research' if web_search else 'writer', type(e).__name__)
            return None
        return Completion(text=(answer.output_text or '').strip(), cited_urls=_cited_urls(answer))


def is_reasoning_model(model: str) -> bool:
    return model.startswith(('gpt-5', 'o1', 'o3', 'o4'))


def _cited_urls(answer) -> list[str]:
    """URLs the search tool really retrieved: its result list plus inline citations.
    JSON-only answers usually carry no inline citations, so the search result list is the main evidence."""
    urls = []
    for item in getattr(answer, 'output', None) or []:
        if getattr(item, 'type', None) == 'web_search_call':
            for src in getattr(getattr(item, 'action', None), 'sources', None) or []:
                url = getattr(src, 'url', None) or (src.get('url') if isinstance(src, dict) else None)
                if url:
                    urls.append(url)
            continue
        if getattr(item, 'type', None) != 'message':
            continue
        for content in getattr(item, 'content', None) or []:
            for ann in getattr(content, 'annotations', None) or []:
                if getattr(ann, 'type', None) == 'url_citation' and getattr(ann, 'url', None):
                    urls.append(ann.url)
    return urls
