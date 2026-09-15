"""OCR and thumbnail enrichment for images embedded in parsed documents."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.utils import logger

PREVIEW_MAX_DIMENSION = 640
MAX_OCR_TEXT_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class ImageEnrichment:
    preview_data: bytes | None = None
    ocr_text: str | None = None


def enrich_image(image_data: bytes, filename: str, params: dict[str, Any] | None = None) -> ImageEnrichment:
    params = params or {}
    try:
        with Image.open(BytesIO(image_data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            preview = image.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning("embedded_image_invalid filename={} error_type={}", filename, type(exc).__name__)
        return ImageEnrichment()

    preview.thumbnail((PREVIEW_MAX_DIMENSION, PREVIEW_MAX_DIMENSION))
    preview_buffer = BytesIO()
    try:
        preview.save(preview_buffer, format="WEBP", quality=82, method=4)
        preview_data = preview_buffer.getvalue()
    except OSError as exc:
        logger.warning("embedded_image_preview_failed filename={} error_type={}", filename, type(exc).__name__)
        preview_data = None

    ocr_text = _extract_ocr_text(image_data, filename, params)
    return ImageEnrichment(preview_data=preview_data, ocr_text=ocr_text)


def image_markdown(
    *,
    alt: str,
    image_url: str,
    preview_url: str | None = None,
    ocr_text: str | None = None,
) -> str:
    safe_alt = str(alt or "图片").replace("[", "").replace("]", "").strip() or "图片"
    reference = f'![{safe_alt}]({image_url} "{preview_url}")' if preview_url else f"![{safe_alt}]({image_url})"
    normalized_ocr = " ".join((ocr_text or "").split())
    return f"{reference}\n\n图片文字：{normalized_ocr}" if normalized_ocr else reference


def _extract_ocr_text(image_data: bytes, filename: str, params: dict[str, Any]) -> str | None:
    if params.get("image_ocr_enabled", True) is False:
        return None
    from yuxi import config

    engine = str(params.get("ocr_engine") if "ocr_engine" in params else config.default_ocr_engine).strip()
    if not engine or engine == "disable":
        return None
    engine_config = params.get("ocr_engine_config")
    processor_params = dict(params)
    if isinstance(engine_config, dict):
        processor_params.update(engine_config)

    suffix = Path(filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}:
        suffix = ".png"
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            temp_file.write(image_data)
            temp_path = temp_file.name
        extracted = DocumentProcessorFactory.process_file(engine, temp_path, processor_params)
        normalized = " ".join(str(extracted or "").split())
        return normalized[:MAX_OCR_TEXT_CHARS]
    except Exception as exc:  # noqa: BLE001
        logger.warning("embedded_image_ocr_failed filename={} error_type={}", filename, type(exc).__name__)
        return None
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


def decoration_reason(
    image_data: bytes,
    *,
    ocr_text: str | None,
    repeated_margin: bool = False,
    small_pictogram: bool = False,
    has_caption: bool = False,
) -> str | None:
    """Only discard high-confidence decoration; unknown OCR is not empty OCR."""
    if has_caption or (ocr_text and ocr_text.strip()):
        return None
    try:
        with Image.open(BytesIO(image_data)) as source:
            rgba = source.convert("RGBA")
            if rgba.getchannel("A").getextrema()[1] == 0:
                return "transparent"
            # White artwork on a transparent background is content, not a blank image.
            alpha_range = rgba.getchannel("A").getextrema()
            background = Image.new("RGBA", rgba.size, "#808080")
            background.alpha_composite(rgba)
            rgb = background.convert("RGB")
            # Exact solid fills, including blank backgrounds and separators.
            if alpha_range[0] == alpha_range[1] and max(ImageStat.Stat(rgb).stddev) < 0.1:
                return "solid_fill"
            width, height = rgb.size
            if ocr_text is None:
                return None
            if min(width, height) <= 4 and max(width, height) / max(1, min(width, height)) >= 20:
                return "separator"
            if repeated_margin:
                return "repeated_margin_icon"
            if small_pictogram and max(width, height) <= 512:
                # Measure native colors before resizing adds antialiasing shades to thin icons.
                colors = sorted(rgb.quantize(colors=8).getcolors(), reverse=True)
                flat_fraction = sum(count for count, _ in colors[:2]) / (width * height)
                thumb = rgb.copy()
                thumb.thumbnail((64, 64))
                gray = thumb.convert("L")
                pixels = list(gray.getdata())
                transitions = sum(abs(pixels[y * thumb.width + x] - pixels[y * thumb.width + x - 1]) > 80
                                  for y in range(thumb.height) for x in range(1, thumb.width))
                density = transitions / max(1, thumb.height * (thumb.width - 1))
                # Dense binary patterns (QR codes, plots) remain available as evidence.
                if flat_fraction >= 0.9 and density < 0.18:
                    return "small_pictogram"
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return None
