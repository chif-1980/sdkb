from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.chunking.ragflow_like import nlp
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuSourceItem,
    FeishuSourceSegment,
)

TARGET_SEGMENT_TOKENS = 480
MAX_SEGMENT_TOKENS = 720
MIN_MEANINGFUL_TOKENS = 8
TABLE_HEADER_ROWS = 2

_MEDIA_REFERENCE = re.compile(r"^!?\[[^\]]*\]\([^)]+\)\s*$")
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PART_HEADING = re.compile(r"^(?:part\s*)?([0-9]{1,2})\s*[-—:：.]?\s*(.+)?$", re.IGNORECASE)
_PDF_PAGE_MARKER = re.compile(r"第\s*(\d+)\s*页(?:\s*/\s*共\s*(\d+)\s*页)?")
_FAQ_QUESTION = re.compile(r"^(?:Q(?:UESTION)?\s*[:：]|问题\s*\d*\s*[:：])\s*(.+)$", re.IGNORECASE)


def _hash(*parts: str, length: int = 40) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _normalized_content(value: str) -> str:
    text = re.sub(r"[`*_>#|]+", "", value or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def _content_hash(value: str) -> str:
    return hashlib.sha256(_normalized_content(value).encode("utf-8")).hexdigest()


def _is_media_reference(line: str) -> bool:
    return bool(_MEDIA_REFERENCE.fullmatch((line or "").strip()))


def _looks_like_table_line(line: str) -> bool:
    text = (line or "").strip()
    return text.startswith("|") and text.count("|") >= 2


def _looks_like_question(line: str) -> bool:
    text = (line or "").strip()
    if _FAQ_QUESTION.match(text):
        return True
    return len(text) <= 120 and text.endswith(("?", "？"))


def _locator_label(locator: dict[str, Any]) -> str | None:
    if locator.get("page"):
        return f"第{locator['page']}页"
    if locator.get("slide"):
        return f"第{locator['slide']}页幻灯片"
    if locator.get("sheet"):
        if locator.get("row_start"):
            return f"工作表 {locator['sheet']} · 第{locator['row_start']}行起"
        return f"工作表 {locator['sheet']}"
    if locator.get("block"):
        return f"正文片段 {locator['block']}"
    return None


@dataclass(slots=True)
class StructuralBlock:
    content: str
    segment_type: str = "paragraph"
    title_path: list[str] = field(default_factory=list)
    locator: dict[str, Any] = field(default_factory=dict)

    @property
    def token_count(self) -> int:
        return nlp.count_tokens(self.content)


@dataclass(frozen=True, slots=True)
class SourceSegmentDraft:
    segment_id: str
    segment_key: str
    segment_index: int
    segment_type: str
    title_path: tuple[str, ...]
    locator: dict[str, Any]
    content: str
    content_hash: str
    token_count: int


def _update_heading_path(stack: list[str], level: int, title: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", title).strip()
    level = max(1, min(level, 6))
    next_stack = list(stack[: level - 1])
    while len(next_stack) < level - 1:
        next_stack.append("")
    next_stack.append(normalized)
    return [item for item in next_stack if item]


def _generic_blocks(
    text: str,
    *,
    base_locator: dict[str, Any] | None = None,
    default_type: str = "paragraph",
) -> list[StructuralBlock]:
    locator = dict(base_locator or {})
    blocks: list[StructuralBlock] = []
    heading_path: list[str] = []
    paragraph_lines: list[str] = []
    table_lines: list[str] = []
    qa_lines: list[str] = []

    def append(content: str, segment_type: str, *, path: list[str] | None = None) -> None:
        cleaned = re.sub(r"\n{3,}", "\n\n", content or "").strip()
        if not cleaned or nlp.count_tokens(_normalized_content(cleaned)) < MIN_MEANINGFUL_TOKENS:
            return
        blocks.append(
            StructuralBlock(
                content=cleaned,
                segment_type=segment_type,
                title_path=list(path if path is not None else heading_path),
                locator=dict(locator),
            )
        )

    def flush_paragraph() -> None:
        if paragraph_lines:
            append("\n".join(paragraph_lines), default_type)
            paragraph_lines.clear()

    def flush_table() -> None:
        if table_lines:
            append("\n".join(table_lines), "table")
            table_lines.clear()

    def flush_qa() -> None:
        if qa_lines:
            append("\n".join(qa_lines), "qa")
            qa_lines.clear()

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if _is_media_reference(line):
            continue
        if _looks_like_table_line(line):
            flush_paragraph()
            flush_qa()
            table_lines.append(line)
            continue
        flush_table()
        if not line:
            flush_paragraph()
            if qa_lines and len(qa_lines) > 1:
                flush_qa()
            continue

        heading_match = _MARKDOWN_HEADING.match(line)
        if heading_match:
            flush_paragraph()
            flush_qa()
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            heading_path = _update_heading_path(heading_path, level, title)
            paragraph_lines.append(line)
            continue

        part_match = _PART_HEADING.match(line)
        if part_match and (line.lower().startswith("part") or len(line) <= 36):
            flush_paragraph()
            flush_qa()
            title = line
            heading_path = _update_heading_path(heading_path, 1, title)
            paragraph_lines.append(line)
            continue

        if _looks_like_question(line):
            flush_paragraph()
            flush_qa()
            qa_lines.append(line)
            continue
        if qa_lines:
            qa_lines.append(line)
            continue

        paragraph_lines.append(line)
        if nlp.count_tokens("\n".join(paragraph_lines)) >= TARGET_SEGMENT_TOKENS:
            flush_paragraph()

    flush_table()
    flush_paragraph()
    flush_qa()
    return blocks


def _split_pdf_pages(text: str) -> list[StructuralBlock]:
    lines: list[str] = []
    blocks: list[StructuralBlock] = []
    found_marker = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        lines.append(raw_line)
        marker = _PDF_PAGE_MARKER.search(line)
        if marker is None:
            continue
        found_marker = True
        page = int(marker.group(1))
        page_count = int(marker.group(2)) if marker.group(2) else None
        locator = {"page": page}
        if page_count:
            locator["page_count"] = page_count
        blocks.extend(_generic_blocks("\n".join(lines), base_locator=locator))
        lines = []
    if lines:
        locator = {"page": len({block.locator.get('page') for block in blocks if block.locator.get('page')}) + 1}
        blocks.extend(_generic_blocks("\n".join(lines), base_locator=locator if found_marker else None))
    return blocks


def _split_ppt_groups(text: str) -> list[StructuralBlock]:
    groups: list[str] = []
    current: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if _is_media_reference(line):
            if current and nlp.count_tokens("\n".join(current)) >= 80:
                groups.append("\n".join(current))
                current = []
            continue
        current.append(raw_line)
        if nlp.count_tokens("\n".join(current)) >= MAX_SEGMENT_TOKENS:
            groups.append("\n".join(current))
            current = []
    if current:
        groups.append("\n".join(current))
    return [
        block
        for slide, group in enumerate(groups, start=1)
        for block in _generic_blocks(group, base_locator={"slide": slide}, default_type="slide")
    ]


def _split_table_block(block: StructuralBlock) -> list[StructuralBlock]:
    if block.segment_type != "table" or block.token_count <= MAX_SEGMENT_TOKENS:
        return [block]
    rows = [line for line in block.content.splitlines() if line.strip()]
    if len(rows) <= TABLE_HEADER_ROWS:
        return [block]
    headers = rows[:TABLE_HEADER_ROWS]
    data_rows = rows[TABLE_HEADER_ROWS:]
    result: list[StructuralBlock] = []
    current = list(headers)
    row_start = TABLE_HEADER_ROWS + 1
    for offset, row in enumerate(data_rows, start=TABLE_HEADER_ROWS + 1):
        candidate = "\n".join([*current, row])
        if len(current) > TABLE_HEADER_ROWS and nlp.count_tokens(candidate) > TARGET_SEGMENT_TOKENS:
            result.append(
                StructuralBlock(
                    content="\n".join(current),
                    segment_type="table",
                    title_path=list(block.title_path),
                    locator={**block.locator, "row_start": row_start, "row_end": offset - 1},
                )
            )
            current = [*headers, row]
            row_start = offset
        else:
            current.append(row)
    if len(current) > TABLE_HEADER_ROWS:
        result.append(
            StructuralBlock(
                content="\n".join(current),
                segment_type="table",
                title_path=list(block.title_path),
                locator={**block.locator, "row_start": row_start, "row_end": len(rows)},
            )
        )
    return result or [block]


def _hard_split_block(block: StructuralBlock) -> list[StructuralBlock]:
    if block.token_count <= MAX_SEGMENT_TOKENS:
        return [block]
    pieces = nlp.hard_split_by_token_limit(
        block.content,
        TARGET_SEGMENT_TOKENS,
        hard_limit_token_num=MAX_SEGMENT_TOKENS,
    )
    return [
        StructuralBlock(
            content=piece,
            segment_type=block.segment_type,
            title_path=list(block.title_path),
            locator={**block.locator, "part": part},
        )
        for part, piece in enumerate(pieces, start=1)
    ]


def _pack_blocks(blocks: Sequence[StructuralBlock]) -> list[StructuralBlock]:
    expanded: list[StructuralBlock] = []
    for block in blocks:
        for table_part in _split_table_block(block):
            expanded.extend(_hard_split_block(table_part))

    packed: list[StructuralBlock] = []
    current: list[StructuralBlock] = []

    def flush() -> None:
        if not current:
            return
        first = current[0]
        types = {item.segment_type for item in current}
        segment_type = first.segment_type if len(types) == 1 else "section"
        packed.append(
            StructuralBlock(
                content="\n\n".join(item.content for item in current),
                segment_type=segment_type,
                title_path=list(first.title_path),
                locator=dict(first.locator),
            )
        )
        current.clear()

    for block in expanded:
        structured = block.segment_type in {"table", "qa", "slide", "ocr"}
        if structured:
            flush()
            packed.append(block)
            continue
        if not current:
            current.append(block)
            continue
        first = current[0]
        same_context = first.title_path == block.title_path and first.locator == block.locator
        combined_tokens = nlp.count_tokens("\n\n".join([*(item.content for item in current), block.content]))
        if not same_context or combined_tokens > TARGET_SEGMENT_TOKENS:
            flush()
        current.append(block)
    flush()
    return packed


def build_source_segment_drafts(
    content: str,
    *,
    version_id: str,
    item_id: str,
    filename: str,
) -> list[SourceSegmentDraft]:
    extension = PurePosixPath(filename or "").suffix.lower()
    if extension == ".pdf":
        blocks = _split_pdf_pages(content)
    elif extension in {".ppt", ".pptx"}:
        blocks = _split_ppt_groups(content)
    elif extension in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
        blocks = _generic_blocks(content, base_locator={"block": 1}, default_type="ocr")
    else:
        blocks = _generic_blocks(content)

    packed = _pack_blocks(blocks)
    occurrence: defaultdict[str, int] = defaultdict(int)
    drafts: list[SourceSegmentDraft] = []
    for index, block in enumerate(packed):
        normalized = _normalized_content(block.content)
        if nlp.count_tokens(normalized) < MIN_MEANINGFUL_TOKENS:
            continue
        anchor_payload = {
            "type": block.segment_type,
            "title_path": block.title_path,
            "locator": block.locator,
        }
        anchor = json.dumps(anchor_payload, ensure_ascii=False, sort_keys=True)
        occurrence[anchor] += 1
        segment_key = _hash(item_id, anchor, str(occurrence[anchor]), length=48)
        segment_id = f"seg-{_hash(version_id, segment_key, length=40)}"
        drafts.append(
            SourceSegmentDraft(
                segment_id=segment_id,
                segment_key=segment_key,
                segment_index=len(drafts),
                segment_type=block.segment_type,
                title_path=tuple(block.title_path),
                locator={**block.locator, "block": len(drafts) + 1},
                content=block.content.strip(),
                content_hash=_content_hash(block.content),
                token_count=nlp.count_tokens(block.content),
            )
        )
    return drafts


def build_retrieval_chunks(
    segments: Sequence[FeishuSourceSegment],
    *,
    file_id: str,
    document_title: str,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: list[FeishuSourceSegment] = []

    def flush() -> None:
        if not current:
            return
        first = current[0]
        prefix = [f"文档：{document_title or '未命名资料'}"]
        title_path = list(first.title_path or [])
        if title_path:
            prefix.append(f"章节：{' > '.join(str(item) for item in title_path if item)}")
        locator_label = _locator_label(dict(first.locator_json or {}))
        if locator_label:
            prefix.append(f"位置：{locator_label}")
        body = "\n\n".join(segment.content.strip() for segment in current if segment.content.strip())
        content = "\n".join([*prefix, "", body]).strip()
        index = len(chunks)
        chunk_id = f"{file_id}_chunk_{index}"
        chunks.append(
            {
                "id": chunk_id,
                "content": content,
                "file_id": file_id,
                "filename": document_title,
                "chunk_index": index,
                "source": document_title,
                "chunk_id": chunk_id,
                "start_char_pos": None,
                "end_char_pos": None,
                "start_token_pos": None,
                "end_token_pos": None,
                "extraction_result": None,
                "tags": {
                    "source_segment_ids": [segment.segment_id for segment in current],
                    "segment_types": list(dict.fromkeys(segment.segment_type for segment in current)),
                    "title_path": title_path,
                    "locator": dict(first.locator_json or {}),
                },
            }
        )
        current.clear()

    for segment in sorted(segments, key=lambda item: item.segment_index):
        if segment.status != "ACTIVE" or segment.publication_state != "INCLUDED":
            continue
        structured = segment.segment_type in {"table", "qa", "slide", "ocr"}
        if structured:
            flush()
            current.append(segment)
            flush()
            continue
        if not current:
            current.append(segment)
            continue
        first = current[0]
        same_context = list(first.title_path or []) == list(segment.title_path or [])
        candidate = "\n\n".join([*(item.content for item in current), segment.content])
        if not same_context or nlp.count_tokens(candidate) > TARGET_SEGMENT_TOKENS:
            flush()
        current.append(segment)
    flush()
    return chunks


class SourceSegmentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def replace_for_version(
        self,
        version: FeishuMaterialVersion,
        item: FeishuSourceItem,
        *,
        yuxi_file_id: str,
        content: str,
    ) -> list[FeishuSourceSegment]:
        filename = item.title or PurePosixPath(item.path_text or "material.md").name
        if not PurePosixPath(filename).suffix and item.item_type:
            filename = f"{filename}.{item.item_type}"
        drafts = build_source_segment_drafts(
            content,
            version_id=version.version_id,
            item_id=item.item_id,
            filename=filename,
        )
        existing = {
            segment.segment_key: segment
            for segment in await self.session.scalars(
                select(FeishuSourceSegment).where(FeishuSourceSegment.version_id == version.version_id)
            )
        }
        active_keys: set[str] = set()
        records: list[FeishuSourceSegment] = []
        for draft in drafts:
            active_keys.add(draft.segment_key)
            record = existing.get(draft.segment_key)
            if record is None:
                record = FeishuSourceSegment(
                    segment_id=draft.segment_id,
                    segment_key=draft.segment_key,
                    version_id=version.version_id,
                    item_id=item.item_id,
                    yuxi_file_id=yuxi_file_id,
                    publication_state="PENDING",
                )
                self.session.add(record)
            elif record.content_hash and record.content_hash != draft.content_hash:
                record.publication_state = "PENDING"
            record.yuxi_file_id = yuxi_file_id
            record.segment_index = draft.segment_index
            record.segment_type = draft.segment_type
            record.title_path = list(draft.title_path)
            record.locator_json = draft.locator
            record.content = draft.content
            record.content_hash = draft.content_hash
            record.token_count = draft.token_count
            record.status = "ACTIVE"
            records.append(record)

        for key, record in existing.items():
            if key not in active_keys:
                record.status = "OBSOLETE"
        await self.session.flush()
        return records

    async def list_active(self, version_id: str) -> list[FeishuSourceSegment]:
        return list(
            await self.session.scalars(
                select(FeishuSourceSegment)
                .where(
                    FeishuSourceSegment.version_id == version_id,
                    FeishuSourceSegment.status == "ACTIVE",
                )
                .order_by(FeishuSourceSegment.segment_index.asc())
            )
        )

    async def transition_pending_publication_state(self, version_id: str, *, target_state: str) -> int:
        if target_state not in {"INCLUDED", "EXCLUDED"}:
            raise ValueError(f"Unsupported source-segment publication state: {target_state}")
        result = await self.session.execute(
            update(FeishuSourceSegment)
            .where(
                FeishuSourceSegment.version_id == version_id,
                FeishuSourceSegment.status == "ACTIVE",
                FeishuSourceSegment.publication_state == "PENDING",
            )
            .values(publication_state=target_state)
        )
        await self.session.flush()
        return int(result.rowcount or 0)

    async def build_publish_chunks(
        self,
        version_id: str,
        *,
        file_id: str,
        document_title: str,
    ) -> list[dict[str, Any]]:
        segments = await self.list_active(version_id)
        return build_retrieval_chunks(segments, file_id=file_id, document_title=document_title)


def source_segment_dict(segment: FeishuSourceSegment) -> dict[str, Any]:
    return {
        "segment_id": segment.segment_id,
        "segment_key": segment.segment_key,
        "segment_index": segment.segment_index,
        "segment_type": segment.segment_type,
        "title_path": list(segment.title_path or []),
        "locator": dict(segment.locator_json or {}),
        "locator_label": _locator_label(dict(segment.locator_json or {})),
        "content": segment.content,
        "content_hash": segment.content_hash,
        "token_count": segment.token_count,
        "publication_state": segment.publication_state,
        "status": segment.status,
    }


def segment_ids_from_chunk_tags(tags: Any) -> tuple[str, ...]:
    if not isinstance(tags, dict):
        return ()
    values = tags.get("source_segment_ids")
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if isinstance(value, str) and value)
