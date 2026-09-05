"""Safe, event-ordered progress tracking for product-facing solution runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


ALLOWED_STAGES = {
    "UNDERSTANDING",
    "REQUIREMENTS_ANALYSIS",
    "CAPABILITY_MATCHING",
    "RETRIEVING",
    "ARCHITECTURE_DESIGN",
    "VERIFYING",
    "EVIDENCE_CHECK",
    "QUALITY_REVIEW",
    "COMPOSING",
    "WAITING_FOR_INPUT",
}

TOOL_PROGRESS = {
    "write_todos": ("REQUIREMENTS_ANALYSIS", "正在拆解需求并规划方案"),
    "match_enterprise_capabilities": ("CAPABILITY_MATCHING", "正在匹配企业能力边界"),
    "query_kb": ("RETRIEVING", "正在检索正式知识"),
    "find_kb_document": ("RETRIEVING", "正在定位相关知识文档"),
    "open_kb_document": ("RETRIEVING", "正在展开并阅读正式知识"),
    "read_file": ("RETRIEVING", "正在读取方案方法或会话资料"),
    "ocr_parse_file": ("RETRIEVING", "正在解析会话附件"),
    "task": ("VERIFYING", "正在核验高风险事实与冲突"),
    "ask_user_question": ("WAITING_FOR_INPUT", "等待补充方案所需信息"),
}

MAX_STAGES = 16
MAX_MESSAGE_CHARS = 240


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed(start: str | None, finish: str | None) -> int:
    begin = _parse_time(start)
    end = _parse_time(finish)
    if not begin or not end:
        return 0
    return max(0, int((end - begin).total_seconds() * 1000))


class ProgressAccumulator:
    """Accumulate a bounded trace in the order actions actually occur."""

    def __init__(self, initial: dict[str, Any] | None = None):
        source = initial if isinstance(initial, dict) else {}
        self.trace: dict[str, Any] = {
            "status": str(source.get("status") or "RUNNING"),
            "startedAt": source.get("startedAt") or _now(),
            "finishedAt": source.get("finishedAt"),
            "elapsedMs": int(source.get("elapsedMs") or 0),
            "steps": [],
        }
        for item in source.get("steps") or []:
            if not isinstance(item, dict):
                continue
            stage = str(item.get("stage") or "").strip()
            if stage not in ALLOWED_STAGES:
                continue
            step = {
                "stage": stage,
                "label": str(item.get("label") or stage),
                "message": str(item.get("message") or "")[:MAX_MESSAGE_CHARS],
                "status": str(item.get("status") or "COMPLETED"),
                "startedAt": item.get("startedAt") or self.trace["startedAt"],
                "finishedAt": item.get("finishedAt"),
                "elapsedMs": int(item.get("elapsedMs") or 0),
            }
            self.trace["steps"].append(step)
        self.trace["steps"] = self.trace["steps"][-MAX_STAGES:]
        self.dirty = False

    @property
    def current(self) -> dict[str, Any] | None:
        return self.trace["steps"][-1] if self.trace["steps"] else None

    def push(self, stage: str, message: str, *, status: str = "ACTIVE") -> bool:
        stage = str(stage or "").strip()
        message = str(message or "").strip()[:MAX_MESSAGE_CHARS]
        if stage not in ALLOWED_STAGES or not message:
            return False
        current = self.current
        now = _now()
        if current and current.get("stage") == stage:
            changed = current.get("message") != message or current.get("status") != status
            current["message"] = message
            current["status"] = status
            if changed:
                if status == "ACTIVE":
                    self.trace["status"] = "RUNNING"
                    self.trace["finishedAt"] = None
                self.dirty = True
            return changed
        if current and current.get("status") == "ACTIVE":
            current["status"] = "COMPLETED"
            current["finishedAt"] = now
            current["elapsedMs"] = _elapsed(current.get("startedAt"), now)
        self.trace["steps"].append({
            "stage": stage,
            "label": stage,
            "message": message,
            "status": status,
            "startedAt": now,
            "finishedAt": None,
            "elapsedMs": 0,
        })
        self.trace["steps"] = self.trace["steps"][-MAX_STAGES:]
        if status == "ACTIVE":
            self.trace["status"] = "RUNNING"
            self.trace["finishedAt"] = None
        self.dirty = True
        return True

    def finish(self, status: str) -> bool:
        status = str(status or "FAILED").upper()
        if status == "COMPLETED":
            step_status = "COMPLETED"
        elif status in {"WAITING_FOR_INPUT", "INTERRUPTED"}:
            status = "WAITING_FOR_INPUT"
            step_status = "INTERRUPTED"
        elif status == "CANCELLED":
            step_status = "INTERRUPTED"
        else:
            status = "FAILED"
            step_status = "FAILED"
        now = _now()
        current = self.current
        if current and current.get("status") == "ACTIVE":
            current["status"] = step_status
            current["finishedAt"] = now
            current["elapsedMs"] = _elapsed(current.get("startedAt"), now)
        self.trace["status"] = status
        self.trace["finishedAt"] = now
        self.trace["elapsedMs"] = _elapsed(self.trace.get("startedAt"), now)
        self.dirty = True
        return True

    def snapshot(self) -> dict[str, Any]:
        # Return a JSON-only copy and clear the write marker only after the
        # caller has successfully persisted it.
        return {
            "status": self.trace["status"],
            "startedAt": self.trace["startedAt"],
            "finishedAt": self.trace.get("finishedAt"),
            "elapsedMs": self.trace["elapsedMs"],
            "steps": [dict(step) for step in self.trace["steps"]],
        }


def _tool_name_from_chunk(chunk: dict[str, Any]) -> str:
    """Read a tool name from runtime protocol fields without inspecting args."""
    stream_event = chunk.get("stream_event")
    if isinstance(stream_event, dict) and stream_event.get("type") in {"tool_call", "tool_call_delta"}:
        name = stream_event.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()

    tool_event = chunk.get("event")
    tool_data = tool_event.get("data") if isinstance(tool_event, dict) else None
    if isinstance(tool_data, dict):
        name = tool_data.get("tool_name") or tool_data.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def progress_stage_from_chunk(chunk: dict[str, Any]) -> tuple[str, str] | None:
    """Map only explicit runtime actions to safe product-facing progress."""
    status = str(chunk.get("status") or "")
    if status in {"ask_user_question_required", "human_approval_required", "interrupted"}:
        return "WAITING_FOR_INPUT", "等待补充方案所需信息"

    tool_name = _tool_name_from_chunk(chunk)
    if tool_name:
        return TOOL_PROGRESS.get(tool_name, ("UNDERSTANDING", "正在执行方案所需操作"))

    stream_event = chunk.get("stream_event")
    if isinstance(stream_event, dict) and stream_event.get("type") == "message_delta":
        content = stream_event.get("content")
        if isinstance(content, str) and content:
            return "COMPOSING", "正在生成方案蓝图"
    return None
