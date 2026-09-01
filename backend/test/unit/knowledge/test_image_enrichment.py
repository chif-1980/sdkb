from __future__ import annotations

from io import BytesIO

from PIL import Image

from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.knowledge.parser.image_enrichment import enrich_image, image_markdown


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (1200, 800), "white").save(output, format="PNG")
    return output.getvalue()


def test_embedded_image_gets_bounded_webp_preview_and_ocr_text(monkeypatch):
    monkeypatch.setattr(DocumentProcessorFactory, "process_file", lambda *_args, **_kwargs: "接入层 服务层 数据层")

    enriched = enrich_image(_png_bytes(), "architecture.png", {"ocr_engine": "rapid_ocr"})

    assert enriched.preview_data is not None
    with Image.open(BytesIO(enriched.preview_data)) as preview:
        assert preview.format == "WEBP"
        assert max(preview.size) <= 640
    assert enriched.ocr_text == "接入层 服务层 数据层"


def test_image_markdown_keeps_original_preview_and_ocr_for_retrieval():
    markdown = image_markdown(
        alt="系统架构图",
        image_url="/minio/public/docs/architecture.png",
        preview_url="/minio/public/docs/previews/architecture.webp",
        ocr_text="接入层\n服务层",
    )

    assert markdown == (
        '![系统架构图](/minio/public/docs/architecture.png "/minio/public/docs/previews/architecture.webp")\n\n'
        "图片文字：接入层 服务层"
    )
