from pathlib import Path

import httpx

from app.core.config import get_settings

SYSTEM_PROMPT = Path(__file__).resolve().parents[1].joinpath("prompts", "assistant_system.txt").read_text(encoding="utf-8")


async def complete(messages: list[dict[str, str]]) -> str:
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError("OpenRouter is not configured")
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json", "X-Title": settings.openrouter_app_name}
    payload = {"model": settings.openrouter_model, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages], "temperature": 0.7, "max_tokens": 1200}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]
