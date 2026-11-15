"""Tests never call a real LLM, whatever .env says (load_dotenv does not override variables already set)."""
import os
os.environ['LLM_PROVIDER'] = 'mock'
os.environ.pop('OPENAI_API_KEY', None)
os.environ['TTS_PROVIDER'] = ''
os.environ.pop('FISH_API_KEY', None)   # voice tests turn the server-made voice on with a fake maker
# Set, not popped: roadpod.main runs load_dotenv, which would fill a missing ROADPOD_DB from .env and
# make tests write into the real episode DB.
os.environ['ROADPOD_DB'] = ':memory:'
# Music breaks off by default so episode-flow tests count spoken segments only; music tests turn it on.
os.environ['MUSIC_EVERY'] = '0'
# The browser-demo routes are tested too, so tests run with the demo mode on.
os.environ['DEMO_MODE'] = '1'
