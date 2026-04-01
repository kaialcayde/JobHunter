"""OpenAI client and screenshot helpers for the vision agent."""

import base64
import json
import os

from openai import OpenAI

from .common import VISION_MODEL_DEFAULT, logger


def _get_vision_client(settings: dict) -> OpenAI:
    """Get OpenAI client for vision calls."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your-openai-api-key-here":
        raise ValueError("OPENAI_API_KEY not set in .env file.")
    return OpenAI(api_key=api_key, timeout=60)


def _get_vision_model(settings: dict) -> str:
    """Get vision model from settings."""
    return settings.get("automation", {}).get("vision_model", VISION_MODEL_DEFAULT)


def _is_vision_logging(settings: dict) -> bool:
    """Check if vision agent logging is enabled."""
    return settings.get("automation", {}).get("vision_logging", True)


def _get_vision_detail(settings: dict) -> str:
    """Get image detail level from settings."""
    return settings.get("automation", {}).get("vision_detail", "high")


def _take_screenshot(page) -> str:
    """Take a screenshot and return as base64-encoded string."""
    screenshot_bytes = page.screenshot(type="png")
    return base64.b64encode(screenshot_bytes).decode("utf-8")


def _decide_actions(client: OpenAI, model: str, screenshot_b64: str,
                    system_prompt: str, history: list[str],
                    detail: str = "low") -> dict:
    """Send screenshot to vision model, get batch of actions back."""
    history_text = ""
    if history:
        recent = history[-10:]
        history_text = "\n\nPrevious rounds:\n" + "\n".join(f"- {h}" for h in recent)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": f"What actions should I take for all visible fields?{history_text}"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{screenshot_b64}",
                "detail": detail,
            }},
        ]},
    ]

    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        max_tokens=2000,
        messages=messages,
    )

    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    usage = response.usage
    logger.info(f"Vision API: {usage.prompt_tokens}+{usage.completion_tokens} tokens, model={model}")

    return json.loads(text)
