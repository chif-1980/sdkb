import pytest

from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.knowledge.implementations.milvus import MilvusKB, _retrieval_config_options
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository


def test_milvus_retrieval_config_exposes_graph_and_dependencies():
    options = _retrieval_config_options()
    by_key = {option["key"]: option for option in options}

    assert by_key["use_graph_retrieval"]["default"] is False
    assert by_key["graph_max_nodes"]["default"] == 10000
    assert by_key["graph_max_nodes"]["depend_on"] == ("use_graph_retrieval", True)
    assert by_key["graph_top_k"]["depend_on"] == ("use_graph_retrieval", True)
    assert by_key["reranker_model"]["depend_on"] == ("use_reranker", True)


def test_graph_ppr_ranks_chunk_nodes_from_seed_entities():
    subgraph = {
        "nodes": [
            {"id": "e1", "type": "Entity", "properties": {"entity_id": "seed"}},
            {"id": "c1", "type": "Chunk", "properties": {"chunk_id": "chunk_a"}},
            {"id": "e2", "type": "Entity", "properties": {"entity_id": "other"}},
            {"id": "c2", "type": "Chunk", "properties": {"chunk_id": "chunk_b"}},
        ],
        "edges": [
            {"source_id": "e1", "target_id": "c1"},
            {"source_id": "e1", "target_id": "e2"},
            {"source_id": "e2", "target_id": "c2"},
        ],
    }

    ranked = MilvusGraphService.rank_chunks_by_ppr(subgraph, {"seed": 1.0}, top_k=2, damping=0.85)

    assert [chunk_id for chunk_id, _ in ranked] == ["chunk_a", "chunk_b"]


def test_rrf_fusion_merges_chunk_and_graph_rankings():
    kb = object.__new__(MilvusKB)
    base_chunks = [
        {"content": "base a", "metadata": {"chunk_id": "a"}, "score": 0.9},
        {"content": "base b", "metadata": {"chunk_id": "b"}, "score": 0.8},
    ]
    graph_chunks = [
        {"content": "graph b", "metadata": {"chunk_id": "b"}, "score": 0.7, "graph_score": 0.7},
        {"content": "graph c", "metadata": {"chunk_id": "c"}, "score": 0.6, "graph_score": 0.6},
    ]

    fused = kb._fuse_chunk_rankings(base_chunks, graph_chunks, graph_weight=1.0)

    assert [chunk["metadata"]["chunk_id"] for chunk in fused] == ["b", "a", "c"]
    assert fused[0]["graph_score"] == 0.7
    assert fused[0]["fusion_sources"] == ["chunk", "graph"]


def test_allowed_file_expression_is_sorted_deduplicated_and_escaped():
    kb = object.__new__(MilvusKB)

    assert kb._build_allowed_file_expr([r"b\path", 'a"q', r"b\path"]) == (
        'file_id in ["a\\"q", "b\\\\path"]'
    )
    assert kb._build_allowed_file_expr([]) == "file_id in []"
    assert kb._build_allowed_file_expr(None) is None


@pytest.mark.asyncio
async def test_file_name_expression_escapes_backslashes_and_quotes(monkeypatch):
    kb = object.__new__(MilvusKB)

    async def list_file_ids(_repository, *, kb_id, filename_pattern):
        assert (kb_id, filename_pattern) == ("kb-1", "report")
        return ['folder\\report"current']

    monkeypatch.setattr(KnowledgeFileRepository, "list_file_ids_by_filename_contains", list_file_ids)

    assert await kb._build_file_name_expr("kb-1", "report") == 'file_id == "folder\\\\report\\"current"'


def test_file_expressions_are_combined_with_and():
    kb = object.__new__(MilvusKB)

    assert kb._combine_expr('file_id == "name"', 'file_id in ["current"]') == (
        '(file_id == "name") and (file_id in ["current"])'
    )
    assert kb._combine_expr(None, "file_id in []") == "(file_id in [])"
    assert kb._combine_expr(None, None) is None


class _RecordingCollection:
    def __init__(self):
        self.search_calls = []
        self.hybrid_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [[]]

    def hybrid_search(self, **kwargs):
        self.hybrid_calls.append(kwargs)
        return [[]]


@pytest.mark.asyncio
@pytest.mark.parametrize("search_mode", ["vector", "keyword", "hybrid"])
async def test_aquery_applies_allowed_file_filter_to_every_search_mode(monkeypatch, search_mode):
    kb = object.__new__(MilvusKB)
    collection = _RecordingCollection()
    kb.databases_meta = {"kb-1": {"embedding_model_spec": "fake"}}
    monkeypatch.setattr(kb, "_get_milvus_collection", lambda kb_id: _async_value(collection))
    monkeypatch.setattr(kb, "_get_query_params", lambda kb_id: {})
    monkeypatch.setattr(kb, "_build_file_name_expr", lambda kb_id, file_name: _async_value('file_id == "name"'))
    monkeypatch.setattr(kb, "_get_embedding_function", lambda spec, sync=False: lambda values: [[0.1]])

    await kb.aquery(
        "query",
        "kb-1",
        search_mode=search_mode,
        file_name="name",
        allowed_file_ids=["file-current"],
        include_distances=False,
    )

    expected = '(file_id == "name") and (file_id in ["file-current"])'
    if search_mode == "hybrid":
        requests = collection.hybrid_calls[0]["reqs"]
        assert [request.expr for request in requests] == [expected, expected]
    else:
        assert collection.search_calls[0]["expr"] == expected


@pytest.mark.asyncio
async def test_aquery_with_empty_allowed_files_does_not_touch_milvus(monkeypatch):
    kb = object.__new__(MilvusKB)
    calls = []

    async def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Milvus must not be called for an empty whitelist")

    monkeypatch.setattr(kb, "_get_milvus_collection", fail_if_called)

    assert await kb.aquery("query", "kb-1", allowed_file_ids=[]) == []
    assert calls == []


@pytest.mark.asyncio
async def test_aquery_with_empty_allowed_file_tuple_short_circuits_before_any_milvus_call(monkeypatch):
    kb = object.__new__(MilvusKB)
    calls = []

    def fail_query_params(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Query configuration must not be read for an empty whitelist")

    async def fail_collection(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Milvus must not be called for an empty whitelist")

    monkeypatch.setattr(kb, "_get_query_params", fail_query_params)
    monkeypatch.setattr(kb, "_get_milvus_collection", fail_collection)

    assert await kb.aquery("query", "kb-1", allowed_file_ids=()) == []
    assert calls == []


async def _async_value(value):
    return value
