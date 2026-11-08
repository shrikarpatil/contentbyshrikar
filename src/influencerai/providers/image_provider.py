"""Image generation providers: local editorial artwork and OpenAI Images."""

from __future__ import annotations

import base64
import hashlib
import random
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont


class ImageProvider(Protocol):
    """Minimal interface for generating a single scene image."""

    name: str

    def generate(self, prompt: str, output_path: Path, width: int, height: int) -> None:
        """Generate and save one image to ``output_path``."""


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _draw_gradient(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    top = (10, 20, 45)
    bottom = (143, 91, 56)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(int(start + (end - start) * ratio) for start, end in zip(top, bottom))
        draw.line((0, y, width, y), fill=color)


class LocalIllustrationProvider:
    """Create consistent, offline editorial illustrations from prompt themes."""

    name = "local-illustration"

    def generate(self, prompt: str, output_path: Path, width: int, height: int) -> None:
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        _draw_gradient(draw, width, height)

        seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16], 16)
        randomizer = random.Random(seed)
        prompt_lower = prompt.lower()
        if any(word in prompt_lower for word in ("space", "pluto", "planet", "moon", "solar system")):
            self._draw_space(draw, width, height, randomizer)
        elif any(word in prompt_lower for word in ("ocean", "sea", "underwater", "wave")):
            self._draw_ocean(draw, width, height, randomizer)
        elif any(word in prompt_lower for word in ("technology", "server", "digital", "computer", "robot")):
            self._draw_technology(draw, width, height, randomizer)
        else:
            self._draw_desert_city(draw, width, height, randomizer)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path, format="PNG", optimize=True)

    @staticmethod
    def _draw_space(draw: ImageDraw.ImageDraw, width: int, height: int, rng: random.Random) -> None:
        for _ in range(max(30, width * height // 18000)):
            x, y = rng.randrange(width), rng.randrange(height)
            radius = rng.choice((2, 3, 4))
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(218, 231, 244))
        radius = int(min(width, height) * 0.31)
        cx, cy = width // 2, int(height * 0.48)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(163, 111, 83), outline=(240, 197, 138), width=8)
        for _ in range(24):
            x = rng.randint(cx - radius // 2, cx + radius // 2)
            y = rng.randint(cy - radius // 2, cy + radius // 2)
            crater = rng.randint(8, max(10, radius // 8))
            if (x - cx) ** 2 + (y - cy) ** 2 < (radius * 0.72) ** 2:
                draw.ellipse((x - crater, y - crater, x + crater, y + crater), fill=(135, 85, 67), outline=(194, 143, 103), width=3)

    @staticmethod
    def _draw_desert_city(draw: ImageDraw.ImageDraw, width: int, height: int, rng: random.Random) -> None:
        sun_radius = int(width * 0.17)
        sun_x, sun_y = int(width * 0.71), int(height * 0.39)
        draw.ellipse((sun_x - sun_radius, sun_y - sun_radius, sun_x + sun_radius, sun_y + sun_radius), fill=(247, 190, 113))
        draw.polygon([(0, int(height * 0.66)), (int(width * 0.35), int(height * 0.47)), (width, int(height * 0.66)), (width, height), (0, height)], fill=(110, 67, 52))
        horizon = int(height * 0.59)
        for index in range(9):
            building_width = rng.randint(width // 12, width // 7)
            x = int(width * (0.08 + index * 0.105))
            building_height = rng.randint(height // 8, height // 4)
            y = horizon - building_height
            color = rng.choice(((44, 43, 52), (61, 48, 47), (78, 55, 46)))
            draw.rectangle(
                (x, y, x + building_width, horizon + rng.randint(20, max(20, height // 18))),
                fill=color,
            )
            draw.polygon([(x - 5, y), (x + building_width // 2, y - height // 22), (x + building_width + 5, y)], fill=(42, 42, 50))
            for window_y in range(y + height // 24, horizon, max(24, height // 22)):
                draw.rectangle((x + building_width // 3, window_y, x + building_width // 3 + 9, window_y + 15), fill=(224, 160, 91))
        draw.arc((-width // 3, int(height * 0.63), width, int(height * 0.96)), 180, 350, fill=(226, 164, 99), width=max(4, width // 180))

    @staticmethod
    def _draw_ocean(draw: ImageDraw.ImageDraw, width: int, height: int, rng: random.Random) -> None:
        draw.ellipse((width // 4, height // 5, width * 3 // 4, height * 3 // 5), fill=(40, 153, 182), outline=(150, 227, 225), width=8)
        for row in range(8):
            y = int(height * (0.54 + row * 0.065))
            points = [(x, y + rng.randint(-16, 16)) for x in range(0, width + 1, max(30, width // 12))]
            draw.line(points, fill=(40, 133 + row * 8, 166 + row * 7), width=max(5, width // 120), joint="curve")

    @staticmethod
    def _draw_technology(draw: ImageDraw.ImageDraw, width: int, height: int, rng: random.Random) -> None:
        center = (width // 2, height // 2)
        for inset in range(0, min(width, height) // 2, max(32, width // 18)):
            color = (31 + inset // 7, 147 + inset // 12, 193 + inset // 14)
            draw.rounded_rectangle((center[0] - inset, center[1] - inset, center[0] + inset, center[1] + inset), radius=20, outline=color, width=5)
        for _ in range(40):
            x, y = rng.randrange(width), rng.randrange(height)
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(121, 231, 242))


class OpenAIImageProvider:
    """Generate images using the official OpenAI Python SDK."""

    name = "openai"

    def __init__(self, api_key: str | None, model: str, quality: str, size: str) -> None:
        if not api_key:
            raise ValueError("IMAGE_PROVIDER=openai requires OPENAI_API_KEY to be set in .env")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._quality = quality
        self._size = size

    def generate(self, prompt: str, output_path: Path, width: int, height: int) -> None:
        del width, height  # The configured provider size controls the returned image dimensions.
        try:
            response = self._client.images.generate(
                model=self._model,
                prompt=prompt,
                size=self._size,
                quality=self._quality,
                n=1,
            )
            image_data = response.data[0].b64_json
            if not image_data:
                raise RuntimeError("The image API returned no image data.")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(base64.b64decode(image_data))
        except Exception as exc:
            # Do not log request objects or credentials; the SDK itself retries transient failures.
            status_code = getattr(exc, "status_code", None)
            error_code = getattr(exc, "code", None)
            request_id = getattr(exc, "request_id", None)
            details = [f"model={self._model}"]
            if status_code is not None:
                details.append(f"HTTP {status_code}")
            if error_code:
                details.append(f"code={error_code}")
            if request_id:
                details.append(f"request_id={request_id}")
            if status_code == 403:
                details.append("check API organization verification and image-model access")
            elif status_code == 400:
                details.append("check the model name, image dimensions, quality, and prompt")
            raise RuntimeError(f"OpenAI image generation failed ({'; '.join(details)}).") from exc


def apply_on_screen_text(image_path: Path, text: str) -> None:
    """Add screenplay-provided text as a legible, mobile-safe image overlay."""
    if not text.strip():
        return
    with Image.open(image_path) as source:
        image = source.convert("RGBA")
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    font = _font(max(28, width // 18), bold=True)
    max_width = int(width * 0.84)
    words = text.split()
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font, stroke_width=2)[2] > max_width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    line_heights = [draw.textbbox((0, 0), item, font=font, stroke_width=2)[3] for item in lines]
    padding = max(20, width // 30)
    total_height = sum(line_heights) + max(0, len(lines) - 1) * padding // 2
    box_top = int(height * 0.80)
    box_bottom = min(height - width // 18, box_top + total_height + padding * 2)
    draw.rounded_rectangle((width * 0.06, box_top, width * 0.94, box_bottom), radius=padding, fill=(5, 11, 25, 190))
    y = box_top + padding
    for line_text, line_height in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line_text, font=font, stroke_width=2)
        x = (width - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), line_text, font=font, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(8, 13, 25, 255))
        y += line_height + padding // 2
    image.convert("RGB").save(image_path, format="PNG", optimize=True)