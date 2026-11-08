"""Generate and cache one consistent image per screenplay scene."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from influencerai.config.settings import Settings
from influencerai.providers.image_provider import (
    ImageProvider,
    LocalIllustrationProvider,
    OpenAIImageProvider,
    apply_on_screen_text,
)
from influencerai.schemas.screenplay import Screenplay


ART_DIRECTION = (
    "Photorealistic cinematic documentary still, natural materials and believable human anatomy, "
    "physically plausible lighting, detailed real-world textures, subtle film color grading, "
    "clear focal point with layered foreground, midground, and background, composed for vertical 9:16 "
    "motion-crop storytelling. No illustration, cartoon, comic-book linework, lettering, captions, logos, or readable text."
)


@dataclass(frozen=True, slots=True)
class ImageGenerationStats:
    provider: str
    requests: int
    cache_hits: int


def create_image_provider(settings: Settings) -> ImageProvider:
    """Instantiate the selected scene-image provider."""
    if settings.image_provider == "local":
        return LocalIllustrationProvider()
    if settings.image_provider == "openai":
        return OpenAIImageProvider(
            settings.openai_api_key,
            settings.openai_image_model,
            settings.image_quality,
            settings.image_size,
        )
    raise ValueError(f"Unsupported IMAGE_PROVIDER: {settings.image_provider}")


def generate_scene_images(
    screenplay: Screenplay,
    run_directory: Path,
    settings: Settings,
    provider: ImageProvider,
    logger,
) -> tuple[dict[int, Path], ImageGenerationStats]:
    """Generate scenes with a cross-run prompt/configuration cache."""
    cache_directory = settings.output_directory / ".cache" / "images"
    cache_directory.mkdir(parents=True, exist_ok=True)
    images: dict[int, Path] = {}
    requests = cache_hits = 0
    for scene in screenplay.scenes:
        prompt = f"{ART_DIRECTION}\nScene description: {scene.image_prompt}"
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "provider": provider.name,
                    "model": settings.openai_image_model if provider.name == "openai" else "local-v1",
                    "quality": settings.image_quality,
                    "size": settings.image_size,
                    "prompt": prompt,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cached_path = cache_directory / f"{cache_key}.png"
        scene_path = run_directory / "images" / f"scene_{scene.scene_id:03d}.png"
        if cached_path.is_file():
            shutil.copyfile(cached_path, scene_path)
            cache_hits += 1
            logger.info("Reused cached image for scene %03d.", scene.scene_id)
        else:
            logger.info("Generating scene %03d image with %s.", scene.scene_id, provider.name)
            provider.generate(prompt, cached_path, settings.video_width, settings.video_height)
            shutil.copyfile(cached_path, scene_path)
            requests += 1
        apply_on_screen_text(scene_path, scene.on_screen_text)
        images[scene.scene_id] = scene_path
    return images, ImageGenerationStats(provider.name, requests, cache_hits)