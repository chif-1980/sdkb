from __future__ import annotations

import json

from yuxi.product_chat.solution_draft import SolutionDraftQuality, SolutionDraftStatus, parse_solution_draft
from yuxi.product_chat.solution_draft_service import extract_solution_payload


def _complete_payload(**overrides):
    sections = [
        {
            "id": f"SEC-{index}",
            "title": title,
            "content_markdown": f"{title}正文",
            "citation_ids": ["CIT-1"],
        }
        for index, title in enumerate(
            ["执行摘要", "需求与范围", "方案设计", "实施计划", "风险与待确认"],
            start=1,
        )
    ]
    payload = {
        "title": "客户方案",
        "executive_summary": "基于正式知识形成的方案摘要",
        "sections": sections,
        "citations": [
            {
                "id": "CIT-1",
                "title": "正式知识",
                "locator": "第 1 页",
                "excerpt": "可追溯证据",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_extract_solution_payload_keeps_decoded_mapping_and_normalizes_aliases():
    payload = extract_solution_payload(_complete_payload(customerContext="零售客户"))

    assert payload.customer_context == "零售客户"
    assert payload.quality is not None
    assert payload.quality.status is SolutionDraftStatus.READY


def test_extract_solution_payload_accepts_json_wrapped_in_prose():
    payload = extract_solution_payload(
        "下面是草稿：\n```json\n" + json.dumps(_complete_payload()) + "\n```"
    )

    assert payload.title == "客户方案"
    assert payload.quality.status is SolutionDraftStatus.READY


def test_extract_solution_payload_accepts_langchain_message_envelope():
    encoded = json.dumps(_complete_payload(), ensure_ascii=False)
    payload = extract_solution_payload({
        "output": {
            "content": [{"type": "text", "text": encoded}],
            "type": "ai",
        }
    })

    assert payload.title == "客户方案"
    assert payload.quality.status is SolutionDraftStatus.READY


def test_quality_percentage_is_normalized_to_ratio():
    quality = SolutionDraftQuality.model_validate({"status": "NEEDS_REVIEW", "evidence_coverage": 68})

    assert quality.status is SolutionDraftStatus.NEEDS_REVIEW
    assert quality.evidence_coverage == 0.68


def test_rich_risk_objects_are_normalized_without_blocking_the_blueprint():
    payload = extract_solution_payload(_complete_payload(risks=[{
        "id": "RISK-1",
        "description": "国产化版本尚未确认",
        "mitigation": "建立兼容性矩阵并完成验证",
    }]))

    assert payload.quality.status is SolutionDraftStatus.NEEDS_REVIEW
    assert payload.risks == ["国产化版本尚未确认（缓解措施：建立兼容性矩阵并完成验证）"]


def test_invalid_structured_result_is_blocked_instead_of_retried_forever():
    payload = extract_solution_payload({"sections": [{"title": "缺少 id"}]})

    assert payload.quality is not None
    assert payload.quality.status is SolutionDraftStatus.BLOCKED
    assert payload.evidence_gaps


def test_unresolved_conflict_blocks_even_when_all_sections_have_evidence():
    payload = parse_solution_draft(
        _complete_payload(
            conflicts=[
                {
                    "claim": "同一场景存在两个报价",
                    "status": "UNRESOLVED",
                    "citation_ids": ["CIT-1"],
                }
            ]
        )
    )

    assert payload.quality.status is SolutionDraftStatus.BLOCKED
    assert any(note.startswith("存在未解决冲突") for note in payload.quality.notes)


def test_editable_risks_degrade_to_needs_review_without_becoming_blocked():
    payload = parse_solution_draft(_complete_payload(risks=["需客户确认部署范围"]))

    assert payload.quality.status is SolutionDraftStatus.NEEDS_REVIEW


def test_capability_gap_is_explicit_and_requires_human_review():
    payload = parse_solution_draft(
        _complete_payload(
            requirements=[{"id": "REQ-1", "text": "建设预测性维护能力"}],
            capability_matches=[{
                "requirement_id": "REQ-1",
                "capability_name": "待从企业能力目录确认",
                "match_type": "UNKNOWN",
                "delivery_status": "UNKNOWN",
            }],
        )
    )

    assert payload.quality.status is SolutionDraftStatus.NEEDS_REVIEW
    assert payload.confidence_summary.enterprise_coverage == 0
    assert payload.review.status == "REQUIRED"
    assert payload.review.pending_items


def test_string_review_decisions_are_normalized_without_blocking_the_blueprint():
    payload = parse_solution_draft(
        _complete_payload(
            review={
                "status": "REQUIRED",
                "decisions": ["需要确认国产化适配范围"],
            }
        )
    )

    assert payload.quality.status is SolutionDraftStatus.NEEDS_REVIEW
    assert payload.review.decisions[0]["decision"] == "需要确认国产化适配范围"
