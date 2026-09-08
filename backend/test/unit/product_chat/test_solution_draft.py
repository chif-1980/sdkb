from __future__ import annotations

import json

from yuxi.product_chat.solution_draft import (
    SolutionDraftQuality,
    SolutionDraftStatus,
    blocked_solution_draft,
    parse_solution_draft,
)
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


def test_rich_questions_and_evidence_gaps_preserve_the_generated_blueprint():
    payload = extract_solution_payload(_complete_payload(
        open_questions=[
            {"id": "Q-1", "question": "首期是否包含微信支付？", "priority": "高"},
            "库存是否对接现有系统？",
        ],
        clarification_questions=[
            {"id": "Q-1", "question": "首期是否包含微信支付？"},
            {"id": "Q-2", "question": "库存是否对接现有系统？"},
        ],
        evidence_gaps=[{
            "id": "GAP-1",
            "description": "未取得已交付商城的正式资料",
            "resolution": "补充产品资料并核验",
        }],
    ))

    assert payload.title == "客户方案"
    assert len(payload.sections) == 5
    assert payload.sections[2].content_markdown == "方案设计正文"
    assert payload.open_questions == ["首期是否包含微信支付？", "库存是否对接现有系统？"]
    assert payload.evidence_gaps == ["未取得已交付商城的正式资料（待补充：补充产品资料并核验）"]
    assert payload.clarification_questions[0].id == "Q-1"
    assert payload.clarification_questions[0].question == "首期是否包含微信支付？"
    assert payload.clarification_questions[1].total == 2
    assert payload.quality.status is SolutionDraftStatus.NEEDS_REVIEW


def test_open_question_aliases_become_resumable_interaction_questions():
    payload = extract_solution_payload(_complete_payload(
        open_questions=[
            {"questionId": "SCOPE", "prompt": "首期范围？"},
            {"id": "DEPLOYMENT", "text": "部署方式？"},
        ],
    ))

    assert payload.open_questions == ["首期范围？", "部署方式？"]
    # Older Agents emitted only ``openQuestions``.  Preserve the compact
    # display list while deriving a resumable question batch for the product.
    assert [item.id for item in payload.clarification_questions] == ["SCOPE", "DEPLOYMENT"]
    assert payload.clarification_questions[0].type == "MULTIPLE_CHOICE"
    assert payload.clarification_questions[1].type == "SINGLE_CHOICE"


def test_open_question_with_known_dimension_gets_safe_choices_and_other_input():
    payload = extract_solution_payload(_complete_payload(
        open_questions=["首期范围是否必须包含哪些功能？"],
    ))

    question = payload.clarification_questions[0]
    assert question.type == "MULTIPLE_CHOICE"
    assert any(option.id == "other" for option in question.options)


def test_legacy_pending_requirements_become_a_choice_dialog():
    payload = extract_solution_payload(_complete_payload(
        customer_context="宠物用品电子商城",
        requirements=[{
            "id": "REQ-SCOPE",
            "text": "建议纳入首期范围：微信小程序首页、商品分类；购物车、下单和支付。",
            "source": "需求分析，待确认",
        }],
    ))

    assert payload.clarification_questions
    question = payload.clarification_questions[0]
    assert question.id == "REQUIREMENT_SCOPE"
    assert question.type == "MULTIPLE_CHOICE"
    assert question.question == "以下建议内容是否纳入首期建设范围？"
    assert any(option.id == "other" or option.id == "OTHER" for option in question.options)
    assert payload.open_questions == [question.question]


def test_empty_options_are_replaced_by_safe_choices():
    payload = extract_solution_payload(_complete_payload(
        clarification_questions=[{
            "id": "MODE",
            "question": "本次采用哪种运营模式？",
            "options": [],
        }],
    ))

    question = payload.clarification_questions[0]
    assert question.type == "SINGLE_CHOICE"
    assert [option.id for option in question.options] == [
        "self_operated", "platform", "distribution", "store_delivery", "other",
    ]


def test_unrecognized_question_objects_still_fail_validation():
    payload = extract_solution_payload(_complete_payload(open_questions=[{"id": "Q-1"}]))

    assert payload.quality.status is SolutionDraftStatus.BLOCKED
    assert "校验失败" in payload.evidence_gaps[0]


def test_invalid_structured_result_is_blocked_instead_of_retried_forever():
    payload = extract_solution_payload({"sections": [{"title": "缺少 id"}]})

    assert payload.quality is not None
    assert payload.quality.status is SolutionDraftStatus.BLOCKED
    assert payload.evidence_gaps


def test_blocked_solution_keeps_editable_sections_and_clarification_question():
    payload = blocked_solution_draft("没有匹配到正式知识", "设计投标一体机国产化方案")

    assert payload.quality.status is SolutionDraftStatus.BLOCKED
    assert payload.executive_summary
    assert len(payload.sections) == 5
    assert all(section.content_markdown.strip() for section in payload.sections)
    assert payload.clarification_questions[0].question
    assert payload.requirements[0].text == "设计投标一体机国产化方案"


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
