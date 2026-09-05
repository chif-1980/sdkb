from __future__ import annotations

from yuxi.product_chat.progress import ProgressAccumulator, progress_stage_from_chunk


def test_progress_accumulator_preserves_real_repeated_action_order():
    progress = ProgressAccumulator()

    assert progress.push("RETRIEVING", "正在检索正式知识") is True
    assert progress.push("VERIFYING", "正在核验高风险事实与冲突") is True
    assert progress.push("RETRIEVING", "正在补充检索正式知识") is True

    assert [step["stage"] for step in progress.snapshot()["steps"]] == [
        "RETRIEVING",
        "VERIFYING",
        "RETRIEVING",
    ]


def test_progress_accumulator_only_merges_adjacent_same_stage():
    progress = ProgressAccumulator()

    assert progress.push("RETRIEVING", "正在检索正式知识") is True
    assert progress.push("RETRIEVING", "正在展开相关文档") is True
    assert progress.push("RETRIEVING", "正在展开相关文档") is False

    steps = progress.snapshot()["steps"]
    assert len(steps) == 1
    assert steps[0]["stage"] == "RETRIEVING"
    assert steps[0]["message"] == "正在展开相关文档"


def test_progress_stage_uses_explicit_tool_name_and_ignores_payload_keywords():
    assert progress_stage_from_chunk({
        "status": "loading",
        "message": "客户需求包含总体架构、证据检查和质量审核",
    }) is None

    assert progress_stage_from_chunk({
        "status": "loading",
        "stream_event": {"type": "tool_call", "name": "query_kb"},
    }) == ("RETRIEVING", "正在检索正式知识")
    assert progress_stage_from_chunk({
        "status": "loading",
        "stream_event": {"type": "tool_call", "name": "task"},
    }) == ("VERIFYING", "正在核验高风险事实与冲突")
    assert progress_stage_from_chunk({
        "status": "loading",
        "stream_event": {"type": "tool_call", "name": "query_kb"},
    }) == ("RETRIEVING", "正在检索正式知识")
