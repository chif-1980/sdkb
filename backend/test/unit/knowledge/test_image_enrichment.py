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


def test_decoration_filter_retains_information_and_uncertain_ocr():
    from PIL import ImageDraw
    from yuxi.knowledge.parser.image_enrichment import decoration_reason
    output = BytesIO()
    image = Image.new('RGB', (32, 32), 'white')
    ImageDraw.Draw(image).ellipse((3, 3, 29, 29), fill='blue')
    image.save(output, format='PNG')
    data = output.getvalue()
    assert decoration_reason(data, ocr_text='', repeated_margin=True) == 'repeated_margin_icon'
    assert decoration_reason(data, ocr_text=None, repeated_margin=True) is None
    assert decoration_reason(data, ocr_text='收益增长 25%', repeated_margin=True) is None
    assert decoration_reason(data, ocr_text='', repeated_margin=True, has_caption=True) is None
    assert decoration_reason(data, ocr_text='') is None
    assert decoration_reason(data, ocr_text='', small_pictogram=True) == 'small_pictogram'
    assert decoration_reason(data, ocr_text='营业收入', small_pictogram=True) is None
    assert decoration_reason(_png_bytes(), ocr_text='') == 'solid_fill'
    assert decoration_reason(b'invalid', ocr_text='') is None


def test_docling_filters_blank_images_but_preserves_captioned_figures(monkeypatch):
    from types import SimpleNamespace as NS
    import base64
    from pathlib import Path
    from yuxi.knowledge.parser import unified
    uri = 'data:image/png;base64,' + base64.b64encode(_png_bytes()).decode()
    pictures = [NS(image=NS(uri=uri), prov=[], captions=[]), NS(image=NS(uri=uri), prov=[], captions=['caption'])]
    doc = NS(pictures=pictures, pages={}, export_to_markdown=lambda: 'before\n<!-- image -->\n<!-- image -->\nafter')
    converter = NS(convert=lambda _: NS(status=NS(name='SUCCESS'), document=doc))
    monkeypatch.setattr(unified, '_get_docling_converter', lambda: converter)
    monkeypatch.setattr(unified, 'enrich_image', lambda *a: NS(ocr_text='', preview_data=None))
    uploads = []
    monkeypatch.setattr(unified, '_upload_image_to_minio', lambda *a: uploads.append(a) or '/figure.png')
    result = unified._convert_with_docling(Path('test.pptx'))
    assert len(uploads) == 1
    assert result.count('/figure.png') == 1
    assert '<!-- image -->' not in result
    assert 'before' in result and 'after' in result


def test_white_transparent_artwork_needs_layout_and_known_empty_ocr():
    from PIL import ImageDraw
    from yuxi.knowledge.parser.image_enrichment import decoration_reason

    picture = Image.new('RGBA', (96, 96), (0, 0, 0, 0))
    ImageDraw.Draw(picture).line([(10, 80), (40, 50), (80, 10)], fill='white', width=5)
    output = BytesIO()
    picture.save(output, format='PNG')
    data = output.getvalue()
    assert decoration_reason(data, ocr_text='') is None
    assert decoration_reason(data, ocr_text=None, small_pictogram=True) is None
    assert decoration_reason(data, ocr_text='', small_pictogram=True) == 'small_pictogram'
