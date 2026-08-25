from types import SimpleNamespace

import pytest

from yuxi.knowledge.parser.base import OCRException
from yuxi.knowledge.parser.rapid_ocr import RapidOCRParser, _bounded_render_scale


def test_bounded_render_scale_caps_pixels_and_long_edge():
    scale_x, scale_y = _bounded_render_scale(
        13608,
        34014,
        1.0,
        1.0,
        max_pixels=20_000_000,
        max_dimension=8192,
    )

    assert 0 < scale_x == scale_y < 1
    assert 13608 * scale_x * 34014 * scale_y <= 20_000_001
    assert 34014 * scale_y <= 8192


def test_process_pdf_prefers_embedded_text_without_loading_ocr(tmp_path, monkeypatch):
    pdf_path = tmp_path / "native.pdf"
    pdf_path.write_bytes(b"placeholder")
    page = SimpleNamespace(get_text=lambda _mode: "这是一段足够长的原生 PDF 正文。" * 10)

    class FakeDocument:
        page_count = 1

        def __iter__(self):
            return iter([page])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("yuxi.knowledge.parser.rapid_ocr.fitz.open", lambda _path: FakeDocument())
    parser = RapidOCRParser()
    monkeypatch.setattr(parser, "_load_model", lambda: pytest.fail("OCR model should not load"))

    result = parser.process_pdf(str(pdf_path), {"pdf_native_text_min_chars": 20})

    assert result.startswith("这是一段足够长的原生 PDF 正文")


def test_prepare_image_rejects_source_above_hard_limit(tmp_path, monkeypatch):
    image_path = tmp_path / "oversized.png"
    image_path.write_bytes(b"placeholder")
    page = SimpleNamespace(rect=SimpleNamespace(width=30_000, height=30_000))

    class FakeDocument:
        page_count = 1

        def __getitem__(self, _index):
            return page

        def close(self):
            return None

    monkeypatch.setattr("yuxi.knowledge.parser.rapid_ocr.fitz.open", lambda _path: FakeDocument())
    parser = RapidOCRParser()

    with pytest.raises(OCRException, match="超过安全上限"):
        parser._prepare_image_path(str(image_path), {"max_source_pixels": 100_000_000})
