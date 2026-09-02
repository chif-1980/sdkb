from types import SimpleNamespace

from yuxi.governance.quality_gate_service import (
    QualityGateService,
    gate_status,
    score_quality_dimensions,
)


def test_quality_score_uses_documented_dimension_weights():
    dimensions = score_quality_dimensions(
        unit_count=4,
        traceable_unit_count=4,
        content_unit_count=4,
        parsing_complete=True,
        open_relation_count=0,
        open_conflict_count=0,
        source_valid=True,
        overdue_unit_count=0,
        assignee_present=True,
        owned_unit_count=4,
        source_metadata_present=True,
    )

    assert sum(item["score"] for item in dimensions.values()) == 100
    assert dimensions["traceability"]["maxScore"] == 30
    assert dimensions["completeness"]["maxScore"] == 25
    assert gate_status(95, []) == "RECOMMENDED"
    assert gate_status(95, [{"code": "SOURCE_INVALID"}]) == "BLOCKED"


def test_hard_gate_blocks_missing_body_location_conflict_and_invalid_source():
    blockers = QualityGateService._hard_blockers(
        version=None,
        source_item=None,
        units=[],
        relations=[],
    )

    assert {item["code"] for item in blockers} == {"CONTENT_INCOMPLETE", "SOURCE_INVALID"}


def test_excluded_unit_does_not_block_other_publishable_knowledge():
    version = SimpleNamespace(
        version_id="version-1",
        processing_status="awaiting_review",
        processing_params={"content_quality": {"checked": True, "has_body": True}},
    )
    source = SimpleNamespace(source_validity="valid")
    included = SimpleNamespace(
        publication_state="INCLUDED",
        content="可发布正文",
        source_segment_ids=["segment-1"],
        locator_json={"page": 1},
        review_due_at=None,
        owner_id="owner-1",
        owner_name=None,
        change_type="NEW",
    )
    excluded = SimpleNamespace(
        publication_state="EXCLUDED",
        content="",
        source_segment_ids=[],
        locator_json={},
        review_due_at=None,
        owner_id=None,
        owner_name=None,
        change_type="NEW",
    )

    result = QualityGateService(None)._evaluate(
        package=SimpleNamespace(assignee_id="admin", title_snapshot="资料", path_snapshot="资料/正文"),
        version=version,
        source_item=source,
        units=[included, excluded],
        review_items=[],
        relations=[],
        current_segments=[],
        previous_segments=[],
        previous_version=None,
    )

    assert result["qualityGate"]["blockers"] == []
    assert result["qualityScore"] == 100
