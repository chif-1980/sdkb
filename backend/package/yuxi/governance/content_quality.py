"""Content quality checks used before a Feishu material can be published."""

from __future__ import annotations

import re


_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s*")
_HTML_TAG = re.compile(r"<[^>]+>")
_DECORATIVE_LINE = re.compile(r"^(?:[-*_]\s*){3,}$")


def _normalize_text(value: str) -> str:
    value = _MARKDOWN_HEADING.sub("", value.strip())
    value = _HTML_TAG.sub("", value)
    return "".join(value.split()).strip().lower()


def assess_content(*, content: str | None, title: str | None = None) -> dict:
    """Assess whether parsed content contains a reviewable body."""

    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    normalized_title = _normalize_text(title or "")
    body_lines = []
    for line in lines:
        if _DECORATIVE_LINE.fullmatch(line):
            continue
        normalized_line = _normalize_text(line)
        if normalized_title and normalized_line == normalized_title:
            continue
        body_lines.append(line)

    body_length = sum(len(line) for line in body_lines)
    has_body = bool(body_lines and body_length > 0)
    return {
        "checked": True,
        "has_body": has_body,
        "body_length": body_length,
        "reason": "正文缺失：解析结果只有标题" if not has_body else None,
    }
