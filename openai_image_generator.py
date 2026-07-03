"""OpenAI image generation helper for BETGPTAI Anime Vault cards.

The bot calls this only when IMAGE_GENERATION_ENABLED=true. It saves generated
images locally and returns the saved path. If the OpenAI Images API fails, the
caller catches the exception and sends the prompt instead.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import requests
from openai import OpenAI


DEFAULT_IMAGE_MODEL = "gpt-image-1"


def generate_image(prompt: str, output_path: str) -> str:
    """Generate one image with the official OpenAI Python SDK.

    Args:
        prompt: The image prompt.
        output_path: Where the PNG should be saved.

    Returns:
        The saved image path as a string.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    client = OpenAI()
    model = os.getenv("OPENAI_IMAGE_MODEL", DEFAULT_IMAGE_MODEL)

    print("Generating OpenAI image...", flush=True)
    response = client.images.generate(
        model=model,
        prompt=prompt,
    )
    image_item = response.data[0]

    # Most modern OpenAI image responses include base64 image bytes.
    b64_json = getattr(image_item, "b64_json", None)
    if b64_json:
        output.write_bytes(base64.b64decode(b64_json))
        print(f"Image successfully created:\n{output}", flush=True)
        return str(output)

    # Some older model responses may provide a temporary URL instead.
    image_url = getattr(image_item, "url", None)
    if image_url:
        download = requests.get(image_url, timeout=60)
        download.raise_for_status()
        output.write_bytes(download.content)
        print(f"Image successfully created:\n{output}", flush=True)
        return str(output)

    raise RuntimeError("OpenAI image response did not include image data.")


def generate_image_from_prompt(prompt: str, output_path: str) -> str:
    """Backward-compatible wrapper used by older image workflows."""
    return generate_image(prompt, output_path)
