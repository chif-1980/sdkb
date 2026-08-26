from __future__ import annotations

import io

import fitz
from openpyxl import Workbook
from PIL import Image

import yuxi.governance.document_layout_service as document_layout_service
from yuxi.governance.document_layout_service import (
    extract_image_layout,
    extract_pdf_layout,
    extract_xlsx_layout,
    render_document_page,
)


def test_extract_pdf_layout_keeps_page_and_text_block_coordinates() -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((60, 100), "Project Scope")
    content = document.tobytes()
    document.close()

    layout = extract_pdf_layout(content, filename="方案.pdf")

    assert layout["file_type"] == ".pdf"
    assert layout["page_count"] == 1
    assert layout["pages"][0]["blocks"][0]["content"] == "Project Scope"
    assert layout["pages"][0]["blocks"][0]["left"] > 0


def test_extract_xlsx_layout_preserves_sheet_and_cell_edit_targets() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "实施清单"
    sheet["A1"] = "模块"
    sheet["B1"] = "负责人"
    sheet["A2"] = "数据接入"
    sheet["B2"] = "张三"
    stream = io.BytesIO()
    workbook.save(stream)

    layout = extract_xlsx_layout(stream.getvalue(), filename="实施清单.xlsx")

    assert layout["pages"][0]["label"] == "实施清单"
    cells = {block["locator"]["cell"]: block for block in layout["pages"][0]["blocks"]}
    assert cells["A2"]["content"] == "数据接入"
    assert cells["A2"]["kind"] == "cell"


def test_extract_image_layout_uses_ocr_bbox_when_available() -> None:
    stream = io.BytesIO()
    Image.new("RGB", (1000, 500), "white").save(stream, format="PNG")
    segments = [
        {
            "segment_id": "segment-1",
            "content": "合同金额",
            "locator_json": {"page": 1, "bbox": [100, 50, 300, 100]},
        }
    ]

    layout = extract_image_layout(stream.getvalue(), filename="报价.png", segments=segments)

    block = layout["pages"][0]["blocks"][0]
    assert block["kind"] == "ocr"
    assert block["source_segment_ids"] == ["segment-1"]
    assert block["left"] == 10
    assert block["top"] == 10


async def test_oversized_image_layout_and_preview_are_downscaled(monkeypatch) -> None:
    monkeypatch.setattr(document_layout_service, "_MAX_LAYOUT_IMAGE_PIXELS", 10_000)
    monkeypatch.setattr(document_layout_service, "_MAX_LAYOUT_IMAGE_DIMENSION", 100)

    stream = io.BytesIO()
    Image.new("RGB", (1000, 500), "white").save(stream, format="PNG")
    source = stream.getvalue()

    layout = extract_image_layout(source, filename="超大图片.png")
    page = layout["pages"][0]
    assert page["preview_scaled"] is True
    assert page["preview_width"] <= 100
    assert page["preview_height"] <= 100

    preview, media_type = await render_document_page("超大图片.png", source, page_number=1)
    assert media_type == "image/png"
    with fitz.open(stream=preview, filetype="png") as document:
        rect = document[0].rect
        assert rect.width <= 100
        assert rect.height <= 100
