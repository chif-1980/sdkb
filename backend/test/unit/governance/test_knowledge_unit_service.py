from yuxi.governance.domain import ReviewOutcome
from yuxi.governance.knowledge_unit_service import KnowledgeUnitService, build_knowledge_unit_drafts
from yuxi.storage.postgres.models_knowledge import FeishuSourceSegment


def _segment(
    index: int,
    content: str,
    *,
    slide: int | None = None,
    title: str = "产品能力",
) -> FeishuSourceSegment:
    return FeishuSourceSegment(
        segment_id=f"segment-{index}",
        segment_key=f"segment-key-{index}",
        version_id="version-1",
        item_id="item-1",
        segment_index=index,
        segment_type="slide" if slide is not None else "text",
        title_path=[title],
        locator_json={"slide": slide} if slide is not None else {},
        content=content,
        content_hash=f"hash-{index}",
        token_count=20,
        publication_state="PENDING",
        status="ACTIVE",
    )


def test_same_slide_segments_form_one_unit_and_slide_boundary_splits_units():
    segments = [
        _segment(0, "第一段介绍产品的核心能力。", slide=1),
        _segment(1, "第二段补充产品的适用场景。", slide=1),
        _segment(2, "第二页说明产品的部署要求。", slide=2, title="部署要求"),
    ]

    drafts = build_knowledge_unit_drafts(segments, item_id="item-1")

    assert len(drafts) == 2
    assert drafts[0].source_segment_ids == ["segment-0", "segment-1"]
    assert drafts[1].source_segment_ids == ["segment-2"]
    assert drafts[0].locator["slide"] == 1
    assert drafts[1].locator["slide"] == 2


def test_every_active_source_segment_is_assigned_once():
    segments = [
        _segment(0, "第一章包含完整的准备工作说明。", title="准备工作"),
        _segment(1, "第二章包含完整的实施步骤说明。", title="实施步骤"),
        _segment(2, "第三章包含完整的验收检查说明。", title="验收检查"),
    ]

    drafts = build_knowledge_unit_drafts(segments, item_id="item-1")
    assigned = [segment_id for draft in drafts for segment_id in draft.source_segment_ids]

    assert sorted(assigned) == ["segment-0", "segment-1", "segment-2"]
    assert len(assigned) == len(set(assigned))


def test_cover_or_directory_unit_is_recommended_for_exclusion():
    draft = build_knowledge_unit_drafts(
        [_segment(0, "目录\n产品介绍\n实施方案", slide=1, title="目录")],
        item_id="item-1",
    )[0]

    outcome, _, confidence, manual_required = KnowledgeUnitService._recommendation(
        draft,
        previous_unit=None,
        change_type="NEW",
    )

    assert outcome == ReviewOutcome.EXCLUDE
    assert confidence == 0.98
    assert manual_required is False
