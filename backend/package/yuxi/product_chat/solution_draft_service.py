"""Bridge between Yuxi Agent Run output and product draft persistence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from yuxi.product_chat.solution_draft import SolutionDraftPayload, blocked_solution_draft, parse_solution_draft


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)

# Keys that identify a Blueprint rather than an envelope such as
# ``{"output": {…}}`` or a LangChain message object.  Checking this before
# validating prevents a wrapper from being accepted as an empty BLOCKED draft.
_BLUEPRINT_KEYS = {
    "title",
    "customer",
    "customer_context",
    "customerContext",
    "executive_summary",
    "executiveSummary",
    "requirements",
    "sections",
    "citations",
    "risks",
    "conflicts",
    "evidence_gaps",
    "evidenceGaps",
}


class SolutionExtractionStatus(StrEnum):
    """Deterministic classification of a solution Agent result."""

    EMPTY = "EMPTY"
    MALFORMED = "MALFORMED"
    VALID = "VALID"


@dataclass(frozen=True)
class SolutionExtractionResult:
    status: SolutionExtractionStatus
    payload: SolutionDraftPayload | None = None
    reason: str | None = None


def _model_to_mapping(value: Any) -> Any:
    """Convert LangChain/Pydantic objects without stringifying mappings."""
    if isinstance(value, (SolutionDraftPayload, dict, list, tuple, str)):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump()
        except Exception:
            return value
    dict_method = getattr(value, "dict", None)
    if callable(dict_method):
        try:
            return dict_method()
        except Exception:
            return value
    return value


def _balanced_json_candidates(text: str) -> list[str]:
    """Extract balanced JSON objects from a response with optional prose."""
    candidates: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if start is None:
            if character == "{":
                start = index
                depth = 1
            continue
        if escaped:
            escaped = False
            continue
        if character == "\\" and in_string:
            escaped = True
            continue
        if character == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                candidates.append(text[start : index + 1])
                start = None
    return candidates


def _looks_like_blueprint(value: dict[str, Any]) -> bool:
    return bool(_BLUEPRINT_KEYS.intersection(value))


def _mapping_children(value: dict[str, Any]) -> list[Any]:
    """Return common result/message envelopes in deterministic order."""
    children: list[Any] = []
    # Keep this order aligned with the public extraction contract.  Structured
    # fields are intentionally last: normal assistant text remains preferred,
    # while LangChain structured responses are still recoverable.
    for key in (
        "output",
        "result",
        "payload",
        "data",
        "message",
        "content",
        "text",
        "messages",
        "structured_response",
        "additional_kwargs",
        "response_metadata",
    ):
        child = value.get(key)
        if child is not None:
            children.append(child)
    return children


def _iter_candidates(output: Any):
    """Yield possible Blueprint values from strings, mappings and messages."""
    output = _model_to_mapping(output)
    if isinstance(output, SolutionDraftPayload):
        yield output
        return
    if isinstance(output, dict):
        if _looks_like_blueprint(output):
            yield output
        for child in _mapping_children(output):
            yield from _iter_candidates(child)
        return
    if isinstance(output, (list, tuple)):
        for child in output:
            yield from _iter_candidates(child)
        return
    if not isinstance(output, str):
        return
    text = output.strip()
    if not text:
        return
    fenced = _FENCED_JSON.search(text)
    if fenced:
        yield fenced.group(1)
    yield text
    yield from _balanced_json_candidates(text)


def extract_solution_result(output: Any) -> SolutionExtractionResult:
    """Extract a Blueprint and distinguish empty from malformed output.

    The runtime has emitted all of the following shapes over time: plain text,
    fenced JSON, LangChain message objects, ``content[]`` blocks and decoded
    ``structured_response`` mappings.  Walk those shapes deterministically and
    never use ``str(dict)`` (which turns valid JSON into invalid Python syntax).
    """
    output = _model_to_mapping(output)
    if output is None or output == "" or output == [] or output == {}:
        return SolutionExtractionResult(SolutionExtractionStatus.EMPTY)

    saw_non_empty = False
    saw_blueprint = False
    malformed_reason: str | None = None
    for candidate in _iter_candidates(output):
        candidate = _model_to_mapping(candidate)
        if isinstance(candidate, str):
            if not candidate.strip():
                continue
            saw_non_empty = True
            try:
                value = json.loads(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        else:
            value = candidate

        value = _model_to_mapping(value)
        if not isinstance(value, dict):
            continue
        if not _looks_like_blueprint(value):
            continue
        saw_blueprint = True
        try:
            return SolutionExtractionResult(
                SolutionExtractionStatus.VALID,
                payload=parse_solution_draft(value),
            )
        except Exception as exc:
            malformed_reason = f"方案结构化结果校验失败：{exc.__class__.__name__}"

    # A non-empty response without a Blueprint is malformed for this skill;
    # an entirely absent/blank response is retryable and must not create a
    # misleading empty BLOCKED card.
    if saw_blueprint or saw_non_empty:
        return SolutionExtractionResult(
            SolutionExtractionStatus.MALFORMED,
            reason=malformed_reason or "未找到符合方案草稿格式的结构化结果",
        )
    return SolutionExtractionResult(SolutionExtractionStatus.EMPTY)


def extract_solution_payload(output: Any) -> SolutionDraftPayload:
    """Validate structured Agent output without stringifying mappings.

    ``get_agent_run_result`` normally returns the assistant message content as
    text, but alternate backends/tests may provide a decoded mapping.  Calling
    ``str(dict)`` produces single-quoted Python syntax which is not JSON and
    would incorrectly turn an otherwise valid draft into ``BLOCKED``.
    """
    extracted = extract_solution_result(output)
    if extracted.payload is not None:
        return extracted.payload
    return blocked_solution_draft(extracted.reason or "Agent 未返回有效的结构化方案草稿")


def render_solution_draft(payload: SolutionDraftPayload) -> str:
    """Keep the chat message useful while the structured card carries details."""
    lines = [f"## {payload.title}"]
    if payload.executive_summary:
        lines.extend(["", payload.executive_summary])
    for section in payload.sections:
        lines.extend(["", f"### {section.title}", section.content_markdown])
    if payload.quality:
        lines.extend(["", f"> 草稿状态：{payload.quality.status.value}"])
        for note in payload.quality.notes[:3]:
            if note and note not in lines:
                lines.extend(["", f"> {note}"])
    rendered = "\n".join(lines).strip()
    return rendered or "方案草稿暂不可用：Agent 未返回可展示内容，请重试。"


def payload_to_dict(payload: SolutionDraftPayload) -> dict[str, Any]:
    return payload.as_json()
