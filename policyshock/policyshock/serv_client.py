from __future__ import annotations

import hashlib, json, os
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / ".serv_cache"
CACHE.mkdir(exist_ok=True)


def _load_dotenv():
    """Load .env from the project root if python-dotenv is installed"""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    except ImportError:
        pass


_load_dotenv()


def _client():
    from openai import OpenAI
    return OpenAI(base_url=os.environ["SERV_BASE_URL"], api_key=os.environ["SERV_API_KEY"])


def chat(system: str, user: str, max_tokens: int = 700, use_cache: bool = True) -> dict:
    model = os.environ["SERV_MODEL"]
    key = hashlib.sha256(json.dumps([model, system, user, max_tokens]).encode()).hexdigest()[:32]
    path = CACHE / f"{key}.json"
    if use_cache and path.exists():
        return {**json.loads(path.read_text()), "cached": True}
    resp = _client().chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_completion_tokens=max_tokens,
    )
    out = {"text": resp.choices[0].message.content,
           "usage": resp.usage.model_dump() if resp.usage else None, "model": model}
    path.write_text(json.dumps(out))
    return {**out, "cached": False}
