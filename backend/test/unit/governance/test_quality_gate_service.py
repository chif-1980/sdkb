from types import SimpleNamespace

import pytest

from yuxi.governance.domain import (
    ReviewItemStatus,
    ReviewPackageStatus,
    ReviewSubjectType,
    ReviewTriggerType,
    ReviewType,
)
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


def _auto_close_fixture(**overrides):
    package = SimpleNamespace(
        assignee_id="admin",
        title_snapshot="资料",
        path_snapshot="资料/正文",
        trigger_type=ReviewTriggerType.SOURCE_VERSION,
        workflow_status=ReviewPackageStatus.OPEN,
    )
    package.__dict__.update(overrides.pop("package", {}))
    version = SimpleNamespace(
        version_id="version-current",
        processing_status="awaiting_review",
        processing_params={"content_quality": {"checked": True, "has_body": True}},
    )
    source = SimpleNamespace(source_validity="valid")
    unit = SimpleNamespace(
        unit_id="unit-1",
        publication_state="PENDING",
        content="可保留的正式正文",
        source_segment_ids=["segment-1"],
        locator_json={"page": 1},
        review_due_at=None,
        owner_id="owner-1",
        owner_name=None,
        change_type="UNCHANGED",
    )
    item = SimpleNamespace(
        item_status=ReviewItemStatus.PENDING,
        subject_type=ReviewSubjectType.KNOWLEDGE_UNIT,
        subject_id="unit-1",
        review_type=ReviewType.UPDATE,
        problem_tags=[],
        evidence_json={"manual_review_required": False},
    )
    segment = SimpleNamespace(
        segment_type="text",
        content_hash="body-hash",
        locator_json={"page": 1},
        title_path=["正文"],
        content="可保留的正式正文",
    )
    values = {
        "package": package,
        "version": version,
        "source_item": source,
        "units": [unit],
        "review_items": [item],
        "relations": [],
        "current_segments": [segment],
        "previous_segments": [segment],
        "previous_version": SimpleNamespace(version_id="version-previous"),
    }
    values.update(overrides)
    return values


def test_auto_close_requires_a_source_update_with_matching_unchanged_units():
    values = _auto_close_fixture()

    result = QualityGateService(None)._evaluate(**values)

    assert result["autoCloseEligible"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"package": {"trigger_type": ReviewTriggerType.FEEDBACK}},
        {"review_items": [SimpleNamespace(
            item_status=ReviewItemStatus.PENDING,
            subject_type=ReviewSubjectType.KNOWLEDGE_UNIT,
            subject_id="unit-1",
            review_type=ReviewType.UPDATE,
            problem_tags=[],
            evidence_json={"manual_review_required": True},
        )]},
        {"review_items": [SimpleNamespace(
            item_status=ReviewItemStatus.PENDING,
            subject_type=ReviewSubjectType.KNOWLEDGE_UNIT,
            subject_id="different-unit",
            review_type=ReviewType.UPDATE,
            problem_tags=[],
            evidence_json={},
        )]},
        {"review_items": [SimpleNamespace(
            item_status=ReviewItemStatus.PENDING,
            subject_type=ReviewSubjectType.KNOWLEDGE_UNIT,
            subject_id="unit-1",
            review_type=ReviewType.UPDATE,
            problem_tags=[],
            evidence_json={},
        )] * 501},
        {"review_items": [SimpleNamespace(
            item_status=ReviewItemStatus.PENDING,
            subject_type=ReviewSubjectType.KNOWLEDGE_UNIT,
            subject_id="unit-1",
            review_type=ReviewType.UPDATE,
            problem_tags=[],
            evidence_json={},
        )] * 2},
        {"current_segments": [SimpleNamespace(
            segment_type="text",
            content_hash="changed-hash",
            locator_json={"page": 1},
            title_path=["正文"],
            content="正文已变化",
        )]},
    ],
)
def test_auto_close_is_disabled_for_manual_or_changed_cases(overrides):
    values = _auto_close_fixture(**overrides)

    result = QualityGateService(None)._evaluate(**values)

    assert result["autoCloseEligible"] is False
