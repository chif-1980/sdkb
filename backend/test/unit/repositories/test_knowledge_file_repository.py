from contextlib import asynccontextmanager

import pytest

from yuxi.repositories import knowledge_file_repository as repo_module
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository


@pytest.mark.asyncio
async def test_get_source_metadata_by_file_ids_filters_kb_and_projects_required_columns(monkeypatch):
    assert hasattr(KnowledgeFileRepository, "get_source_metadata_by_file_ids")

    statements = []

    class FakeResult:
        def all(self):
            return [("file-1", "guide.md", {"feishu": {"source_url": "https://example.feishu.cn/wiki/page"}})]

    class FakeSession:
        async def execute(self, statement):
            statements.append(statement)
            return FakeResult()

    @asynccontextmanager
    async def fake_session_context():
        yield FakeSession()

    monkeypatch.setattr(repo_module.pg_manager, "get_async_session_context", fake_session_context)

    metadata = await KnowledgeFileRepository().get_source_metadata_by_file_ids(
        kb_id="kb-a",
        file_ids=["file-1"],
    )

    assert metadata == {
        "file-1": {
            "filename": "guide.md",
            "processing_params": {"feishu": {"source_url": "https://example.feishu.cn/wiki/page"}},
        }
    }
    assert len(statements) == 1
    statement = statements[0]
    assert list(statement.selected_columns.keys()) == ["file_id", "filename", "processing_params"]
    assert "knowledge_files.kb_id" in str(statement)
    parameter_values = list(statement.compile().params.values())
    assert "kb-a" in parameter_values
    assert ["file-1"] in parameter_values
