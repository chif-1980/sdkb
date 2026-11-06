"""Run the publication/writeback contract against PostgreSQL constraints and JSONB."""

import pytest

from test.unit.governance.test_source_change_service import (
    source_change_engine,  # noqa: F401 -- reused pytest fixtures
    source_change_session,  # noqa: F401 -- reused pytest fixtures
    test_invalidated_source_never_closes_linked_suggestion as verify_invalidated_source,
    test_one_request_closes_every_linked_suggestion_across_meetings as verify_multi_suggestion,
    test_publication_closes_linked_suggestion_only_after_success as verify_publication,
    test_rerun_keeps_request_link_so_publication_still_closes_suggestion as verify_rerun,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
# 参数名与上方导入的 fixture 同名是 pytest 的解析要求，因此逐处忽略 ruff 的重复定义提示。
contract = pytest.mark.parametrize("source_change_engine", ["postgres"], indirect=True)


@contract
async def test_publication_writeback_preserves_atomicity_and_idempotency(
    source_change_session,  # noqa: F811
):
    await verify_publication(source_change_session)


@contract
async def test_invalidated_source_blocks_writeback_under_postgres_jsonb(
    source_change_session,  # noqa: F811
):
    await verify_invalidated_source(source_change_session)


@contract
async def test_one_request_closes_multiple_suggestions_under_postgres_jsonb(
    source_change_session,  # noqa: F811
):
    await verify_multi_suggestion(source_change_session)


@contract
async def test_rerun_link_survives_postgres_jsonb_writeback(
    source_change_session,  # noqa: F811
):
    await verify_rerun(source_change_session)
