import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from yuxi.product_chat.answer_service import (
    DETAILED_PROMPT_VERSION,
    DETAILED_SYSTEM_PROMPT,
    INSUFFICIENT_TEXT,
    NO_MODEL_VERSION,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    AnswerDelta,
    AnswerService,
    GroundedAnswer,
)
from yuxi.product_chat.source_policy_service import ProductKnowledgeScope


pytestmark = pytest.mark.unit


PUBLISHED_AT = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)


def _published_material(file_id: str, *, item_id: str = "item-1", title: str = "产品手册"):
    item = SimpleNamespace(
        source_id="source-1",
        item_id=item_id,
        title=title,
        source_url=f"https://quickdone.feishu.cn/wiki/{item_id}",
        path_text="产品 / 手册",
    )
    version = SimpleNamespace(
        version_id=f"version-{item_id}",
        yuxi_file_id=file_id,
        published_at=PUBLISHED_AT,
    )
    return item, version


class _PolicyService:
    def __init__(self, allowed_file_ids=("file-1", "file-2")):
        self.scope = ProductKnowledgeScope(
            source_id="source-1",
            kb_id="kb-1",
            allowed_file_ids=tuple(allowed_file_ids),
        )
        self.calls = []

    async def resolve_scope(self, user):
        self.calls.append(user)
        return self.scope


class _KnowledgeManager:
    def __init__(self, chunks, *, error=None, file_contents=None, open_windows=None, find_results=None):
        self.chunks = chunks
        self.error = error
        self.file_contents = file_contents or {}
        self.open_windows = open_windows or {}
        self.find_results = find_results or {}
        self.query_calls = []
        self.info_calls = []
        self.content_calls = []
        self.open_calls = []
        self.find_calls = []

    async def aquery(self, question, kb_id, **kwargs):
        self.query_calls.append((question, kb_id, kwargs))
        if self.error is not None:
            raise self.error
        return self.chunks

    async def get_database_info(self, kb_id):
        self.info_calls.append(kb_id)
        return {"llm_model_spec": "provider:model-1"}

    async def get_file_content(self, kb_id, file_id):
        self.content_calls.append((kb_id, file_id))
        return self.file_contents.get(file_id, {"lines": []})

    async def open_file_content(self, kb_id, file_id, *, offset, limit):
        self.open_calls.append((kb_id, file_id, offset, limit))
        return self.open_windows.get(file_id, {"content": "", "start_line": 0, "end_line": 0})

    async def find_file_content(self, kb_id, file_id, patterns, **kwargs):
        self.find_calls.append((kb_id, file_id, tuple(patterns), kwargs))
        return self.find_results.get(file_id, {"windows": []})


class _Repository:
    def __init__(self, published, *, history=()):
        self.published = published
        self.history = history
        self.calls = []
        self.history_calls = []

    async def get_published_evidence(self, source_id, file_ids):
        self.calls.append((source_id, tuple(file_ids)))
        return self.published

    async def list_recent_messages(self, conversation_id, owner_user_id, *, limit):
        self.history_calls.append((conversation_id, owner_user_id, limit))
        return self.history


class _Model:
    def __init__(self, content=None, *, error=None, stream_chunks=None):
        self.content = content
        self.error = error
        self.stream_chunks = stream_chunks if stream_chunks is not None else [content]
        self.model_name = "model-1"
        self.model = object()
        self.calls = []

    async def call(self, message, stream=None):
        self.calls.append((message, stream))
        if self.error is not None:
            raise self.error
        if stream:

            async def chunks():
                for content in self.stream_chunks:
                    yield SimpleNamespace(content=content)

            return chunks()
        return SimpleNamespace(content=self.content)


class _ModelSelector:
    def __init__(self, model):
        self.model = model
        self.calls = []

    def __call__(self, model_spec):
        self.calls.append(model_spec)
        return self.model


def _service(
    *,
    chunks,
    published,
    model_content=None,
    retrieval_error=None,
    model_error=None,
    stream_chunks=None,
    history=(),
    file_contents=None,
    open_windows=None,
    find_results=None,
    agent_factory=None,
):
    policy = _PolicyService()
    knowledge = _KnowledgeManager(
        chunks,
        error=retrieval_error,
        file_contents=file_contents,
        open_windows=open_windows,
        find_results=find_results,
    )
    repository = _Repository(published, history=history)
    model = _Model(model_content, error=model_error, stream_chunks=stream_chunks)
    selector = _ModelSelector(model)
    service = AnswerService(
        db=object(),
        repository=repository,
        policy_service=policy,
        knowledge_base=knowledge,
        model_selector=selector,
        agent_factory=agent_factory,
    )
    return service, policy, knowledge, repository, model, selector


@pytest.mark.asyncio
async def test_supported_answer_uses_only_revalidated_evidence_in_retrieval_order():
    current = _published_material("file-1")
    chunks = [
        {"content": "支持在企业内网私有部署。", "metadata": {"file_id": "file-1", "chunk_index": 2}},
        {"content": "已经撤回的旧内容", "metadata": {"file_id": "file-stale", "chunk_index": 0}},
    ]
    payload = json.dumps(
        {"status": "SUPPORTED", "answer": "该产品支持企业内网私有部署。", "citation_ids": ["E1"]},
        ensure_ascii=False,
    )
    service, policy, knowledge, repository, model, selector = _service(
        chunks=chunks,
        published={"file-1": current},
        model_content=payload,
    )

    result = await service.answer("是否支持私有部署？", {"id": 7}, "conversation-1")

    assert result.status == "SUPPORTED"
    assert result.content == "该产品支持企业内网私有部署。"
    assert result.model_version == "model-1"
    assert result.prompt_version == PROMPT_VERSION
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.evidence_id == "E1"
    assert citation.item_id == "item-1"
    assert citation.version_id == "version-item-1"
    assert citation.yuxi_file_id == "file-1"
    assert citation.title == "产品手册"
    assert citation.source_url == "https://quickdone.feishu.cn/wiki/item-1"
    assert citation.path_text == "产品 / 手册"
    assert citation.locator == "第3段"
    assert citation.excerpt == "支持在企业内网私有部署。"
    assert citation.source_version_at == PUBLISHED_AT
    with pytest.raises(FrozenInstanceError):
        citation.title = "changed"

    assert policy.calls == [{"id": 7}]
    assert knowledge.query_calls == [
        (
            "是否支持私有部署？",
            "kb-1",
            {
                "search_mode": "hybrid",
                "allowed_file_ids": ["file-1", "file-2"],
                "use_graph_retrieval": False,
                "final_top_k": 12,
                "recall_top_k": 40,
            },
        )
    ]
    assert repository.calls == [("source-1", ("file-1", "file-stale"))]
    assert repository.history_calls == [("conversation-1", 7, 6)]
    assert knowledge.content_calls == [("kb-1", "file-1")]
    assert knowledge.info_calls == ["kb-1"]
    assert selector.calls == ["provider:model-1"]
    assert len(model.calls) == 1
    expected_evidence = json.dumps(
        [
            {
                "evidence_id": "E1",
                "title": "产品手册",
                "locator": "第3段",
                "excerpt": "支持在企业内网私有部署。",
                "source_version_at": PUBLISHED_AT.isoformat(),
            }
        ],
        ensure_ascii=False,
    )
    assert model.calls == [
        (
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"CONVERSATION_HISTORY:\n[]\n\nEVIDENCE:\n{expected_evidence}\n\nQUESTION:\n是否支持私有部署？"
                    ),
                },
            ],
            True,
        )
    ]


@pytest.mark.asyncio
async def test_answer_events_streams_markdown_before_the_validated_result():
    stream_chunks = [
        (
            '{"status":"SUPPORTED","citation_ids":["E1"],'
            '"answer":"## \u7ed3\u8bba\\n\u652f\u6301\u79c1\u6709\u5316\u90e8\u7f72\u3002[E'
        ),
        "1]\\n\u53ef\u7528\u4e8e\u4f01\u4e1a\u73af\u5883\u3002\\uD83D",
        '\\uDE80"}',
    ]
    payload = "".join(stream_chunks)
    service, *_ = _service(
        chunks=[{"content": "支持私有化部署。", "metadata": {"file_id": "file-1"}}],
        published={"file-1": _published_material("file-1")},
        model_content=payload,
        stream_chunks=stream_chunks,
    )

    events = [event async for event in service.answer_events("是否支持私有部署？", {"id": 7}, "conversation-1")]

    deltas = [event.content for event in events if isinstance(event, AnswerDelta)]
    result = next(event for event in events if isinstance(event, GroundedAnswer))
    assert deltas
    assert "".join(deltas) == result.content
    assert result.content == "## 结论\n支持私有化部署。[1]\n可用于企业环境。🚀"
    assert events.index(next(event for event in events if isinstance(event, AnswerDelta))) < events.index(result)


@pytest.mark.asyncio
async def test_answer_events_keeps_valid_inline_evidence_omitted_from_citation_ids():
    payload = json.dumps(
        {
            "status": "SUPPORTED",
            "citation_ids": ["E1"],
            "answer": "实施方案需要先完成范围确认。[E1]\n再按部署条件准备环境。[E2]",
        },
        ensure_ascii=False,
    )
    service, *_ = _service(
        chunks=[
            {"content": "范围确认。", "metadata": {"file_id": "file-1"}},
            {"content": "部署条件。", "metadata": {"file_id": "file-2"}},
        ],
        published={
            "file-1": _published_material("file-1", item_id="item-1"),
            "file-2": _published_material("file-2", item_id="item-2"),
        },
        model_content=payload,
        stream_chunks=[payload],
    )

    events = [event async for event in service.answer_events("如何制定实施方案？", {"id": 7}, "conversation-1")]

    result = next(event for event in events if isinstance(event, GroundedAnswer))
    assert result.status == "SUPPORTED"
    assert result.content == "实施方案需要先完成范围确认。[1]\n再按部署条件准备环境。[2]"
    assert [citation.evidence_id for citation in result.citations] == ["E1", "E2"]


@pytest.mark.asyncio
async def test_detailed_mode_uses_controlled_multi_step_tools_and_streams_the_grounded_answer():
    class InvestigatingGraph:
        def __init__(self, tools):
            self.tools = {item.name: item for item in tools}
            self.config = None

        async def astream(self, input_value, *, config, stream_mode):
            self.config = config
            assert stream_mode == "updates"
            assert "QUESTION:\n标准版如何部署？" in input_value["messages"][0]["content"]
            yield {
                "model": {
                    "messages": [SimpleNamespace(tool_calls=[{"name": "search_enterprise_knowledge"}])]
                }
            }
            await self.tools["search_enterprise_knowledge"].ainvoke({"query": "标准版 部署 条件"})
            yield {
                "model": {
                    "messages": [SimpleNamespace(tool_calls=[{"name": "open_enterprise_source"}])]
                }
            }
            await self.tools["open_enterprise_source"].ainvoke(
                {"file_id": "file-1", "offset": 0, "window_size": 80}
            )
            yield {
                "model": {
                    "messages": [SimpleNamespace(tool_calls=[{"name": "open_enterprise_source"}])]
                }
            }
            denied = await self.tools["open_enterprise_source"].ainvoke(
                {"file_id": "file-stale", "offset": 0, "window_size": 80}
            )
            assert "不可访问" in denied

    created_graphs = []

    def agent_factory(**kwargs):
        assert kwargs["system_prompt"]
        assert [item.name for item in kwargs["tools"]] == [
            "search_enterprise_knowledge",
            "open_enterprise_source",
            "find_in_enterprise_source",
        ]
        graph = InvestigatingGraph(kwargs["tools"])
        created_graphs.append(graph)
        return graph

    payload = json.dumps(
        {
            "status": "SUPPORTED",
            "citation_ids": ["E1", "E2"],
            "answer": "## 部署结论\n\n支持私有化部署。[E1]\n\n部署前需要完成环境检查。[E2]",
        },
        ensure_ascii=False,
    )
    service, policy, knowledge, repository, model, _selector = _service(
        chunks=[
            {"content": "支持私有化部署。", "metadata": {"file_id": "file-1", "chunk_index": 0}},
            {"content": "未发布内容", "metadata": {"file_id": "file-stale", "chunk_index": 0}},
        ],
        published={"file-1": _published_material("file-1")},
        model_content=payload,
        agent_factory=agent_factory,
        open_windows={
            "file-1": {
                "content": "1 | 部署前需要完成环境检查。",
                "start_line": 1,
                "end_line": 1,
            }
        },
    )

    events = [
        event
        async for event in service.answer_events(
            "标准版如何部署？",
            {"id": 7},
            "conversation-1",
            mode="DETAILED",
        )
    ]

    result = next(event for event in events if isinstance(event, GroundedAnswer))
    progress_messages = [event.message for event in events if hasattr(event, "stage")]
    assert result.status == "SUPPORTED"
    assert result.prompt_version == DETAILED_PROMPT_VERSION
    assert [item.yuxi_file_id for item in result.citations] == ["file-1", "file-1"]
    assert all(item.yuxi_file_id != "file-stale" for item in result.citations)
    assert "正在展开候选文档的相关上下文" in progress_messages
    assert created_graphs[0].config == {"recursion_limit": 16}
    assert len(knowledge.query_calls) == 1
    assert knowledge.query_calls[0][2]["allowed_file_ids"] == ["file-1", "file-2"]
    assert knowledge.open_calls == [("kb-1", "file-1", 0, 80)]
    assert repository.calls[0] == ("source-1", ("file-1", "file-stale"))
    assert len(policy.calls) >= 6
    assert model.calls[0][0][0] == {"role": "system", "content": DETAILED_SYSTEM_PROMPT}
    assert "未发布内容" not in model.calls[0][0][1]["content"]


@pytest.mark.asyncio
async def test_detailed_mode_stops_knowledge_tools_at_the_call_limit():
    class RepeatingGraph:
        def __init__(self, search_tool):
            self.search_tool = search_tool

        async def astream(self, _input_value, *, config, stream_mode):
            for index in range(8):
                yield {
                    "model": {
                        "messages": [SimpleNamespace(tool_calls=[{"name": "search_enterprise_knowledge"}])]
                    }
                }
                await self.search_tool.ainvoke({"query": f"检索角度 {index}"})

    def agent_factory(**kwargs):
        return RepeatingGraph(kwargs["tools"][0])

    payload = json.dumps(
        {"status": "SUPPORTED", "citation_ids": ["E1"], "answer": "支持私有化部署。[E1]"},
        ensure_ascii=False,
    )
    service, _policy, knowledge, *_ = _service(
        chunks=[{"content": "支持私有化部署。", "metadata": {"file_id": "file-1"}}],
        published={"file-1": _published_material("file-1")},
        model_content=payload,
        agent_factory=agent_factory,
    )

    result = await service.answer("是否支持私有部署？", {"id": 7}, "conversation-1", mode="DETAILED")

    assert result.status == "SUPPORTED"
    assert len(knowledge.query_calls) == 6


@pytest.mark.asyncio
async def test_empty_revalidated_evidence_returns_exact_insufficient_without_model_call():
    service, _policy, knowledge, _repository, model, selector = _service(
        chunks=[{"content": "旧内容", "metadata": {"file_id": "file-stale"}}],
        published={},
    )

    result = await service.answer("问题", object(), "conversation-1")

    assert result.status == "INSUFFICIENT"
    assert result.content == INSUFFICIENT_TEXT
    assert result.citations == ()
    assert result.model_version == NO_MODEL_VERSION
    assert result.prompt_version == PROMPT_VERSION
    assert selector.calls == []
    assert model.calls == []
    assert knowledge.info_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_url",
    [
        pytest.param(None, id="null"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("not-a-url", id="not-a-url"),
        pytest.param("http://quickdone.feishu.cn/wiki/item-1", id="http"),
        pytest.param("ftp://quickdone.feishu.cn/wiki/item-1", id="ftp"),
        pytest.param("https:///missing-host", id="missing-host"),
        pytest.param("https://user@quickdone.feishu.cn/wiki/item-1", id="username"),
        pytest.param("https://user:secret@quickdone.feishu.cn/wiki/item-1", id="password"),
        pytest.param("https://quickdone.feishu.cn:8443/wiki/item-1", id="nonstandard-port"),
        pytest.param("https://quickdone.feishu.cn:invalid/wiki/item-1", id="invalid-port"),
        pytest.param(" https://quickdone.feishu.cn/wiki/item-1", id="leading-space"),
        pytest.param("https://quickdone.feishu.cn/wiki/item-1\x00", id="control-character"),
        pytest.param("https://quickdone\u200d.feishu.cn/wiki/item-1", id="format-control"),
        pytest.param("https://localhost/wiki/item-1", id="localhost"),
        pytest.param("https://127.0.0.1/wiki/item-1", id="ipv4"),
        pytest.param("https://10.0.0.1/wiki/item-1", id="private-ipv4"),
        pytest.param("https://[::1]/wiki/item-1", id="ipv6"),
        pytest.param("https://quickdone.feishu.cn./wiki/item-1", id="trailing-dot"),
        pytest.param("https://-bad.feishu.cn/wiki/item-1", id="leading-hyphen-label"),
        pytest.param("https://bad..feishu.cn/wiki/item-1", id="empty-label"),
        pytest.param("https://bad_host.feishu.cn/wiki/item-1", id="invalid-label-character"),
        pytest.param("https://\ud800.feishu.cn/wiki/item-1", id="invalid-idna"),
        pytest.param("https://example.test/wiki/item-1", id="untrusted-domain"),
        pytest.param("https://evilfeishu.cn/wiki/item-1", id="feishu-lookalike"),
        pytest.param("https://quickdone.feishu.cn.evil.test/wiki/item-1", id="feishu-suffix-bypass"),
        pytest.param("https://evillarksuite.com/wiki/item-1", id="lark-lookalike"),
    ],
)
async def test_evidence_without_an_openable_source_url_fails_closed(source_url):
    material = _published_material("file-1")
    material[0].source_url = source_url
    service, *_rest, model, selector = _service(
        chunks=[{"content": "正式内容", "metadata": {"file_id": "file-1"}}],
        published={"file-1": material},
    )

    result = await service.answer("问题", object(), "conversation-1")

    assert result.status == "INSUFFICIENT"
    assert result.citations == ()
    assert selector.calls == []
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_url",
    [
        "https://quickdone.feishu.cn/wiki/item-1",
        "https://feishu.cn/wiki/item-1",
        "https://tenant.larksuite.com/wiki/item-1",
        "https://larksuite.com/wiki/item-1",
        "https://quickdone.feishu.cn:443/wiki/item-1",
    ],
)
async def test_evidence_from_trusted_feishu_and_lark_domains_is_usable(source_url):
    material = _published_material("file-1")
    material[0].source_url = source_url
    payload = json.dumps(
        {"status": "SUPPORTED", "answer": "有正式资料支持。", "citation_ids": ["E1"]},
        ensure_ascii=False,
    )
    service, *_rest, model, selector = _service(
        chunks=[{"content": "正式内容", "metadata": {"file_id": "file-1"}}],
        published={"file-1": material},
        model_content=payload,
    )

    result = await service.answer("问题", object(), "conversation-1")

    assert result.status == "SUPPORTED"
    assert result.citations[0].source_url == source_url
    assert selector.calls == ["provider:model-1"]
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("title", [None, "", "   "])
async def test_missing_evidence_title_uses_a_stable_fallback(title):
    material = _published_material("file-1")
    material[0].title = title
    payload = json.dumps(
        {"status": "SUPPORTED", "answer": "有正式资料支持。", "citation_ids": ["E1"]},
        ensure_ascii=False,
    )
    service, *_ = _service(
        chunks=[{"content": "正式内容", "metadata": {"file_id": "file-1"}}],
        published={"file-1": material},
        model_content=payload,
    )

    result = await service.answer("问题", object(), "conversation-1")

    assert result.citations[0].title == "未命名文档"


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_index", [-1, -2, True, 1.5, "1", None])
async def test_invalid_chunk_index_uses_document_body_locator(chunk_index):
    payload = json.dumps(
        {"status": "SUPPORTED", "answer": "有正式资料支持。", "citation_ids": ["E1"]},
        ensure_ascii=False,
    )
    service, *_ = _service(
        chunks=[{"content": "正式内容", "metadata": {"file_id": "file-1", "chunk_index": chunk_index}}],
        published={"file-1": _published_material("file-1")},
        model_content=payload,
    )

    result = await service.answer("问题", object(), "conversation-1")

    assert result.citations[0].locator == "文档正文"


@pytest.mark.asyncio
async def test_multiple_chunks_from_one_unambiguous_file_remain_usable():
    payload = json.dumps(
        {"status": "SUPPORTED", "answer": "两段正式资料均支持。", "citation_ids": ["E1", "E2"]},
        ensure_ascii=False,
    )
    service, *_ = _service(
        chunks=[
            {"content": "第一段正式内容", "metadata": {"file_id": "file-1", "chunk_index": 0}},
            {"content": "第二段正式内容", "metadata": {"file_id": "file-1", "chunk_index": 1}},
        ],
        published={"file-1": _published_material("file-1")},
        model_content=payload,
    )

    result = await service.answer("问题", object(), "conversation-1")

    assert [citation.evidence_id for citation in result.citations] == ["E1", "E2"]
    assert [citation.yuxi_file_id for citation in result.citations] == ["file-1", "file-1"]
    assert [citation.locator for citation in result.citations] == ["第1段", "第2段"]


@pytest.mark.asyncio
async def test_follow_up_query_uses_recent_questions_and_expands_adjacent_source_chunks():
    history = [
        SimpleNamespace(role="USER", content="企业版支持什么部署方式？"),
        SimpleNamespace(role="ASSISTANT", content="支持私有化部署。"),
    ]
    payload = json.dumps(
        {
            "status": "SUPPORTED",
            "answer": "部署前需要完成环境检查。[E2]\n\n核心结论仍是支持私有化部署。[E1]",
            "citation_ids": ["E2", "E1"],
        },
        ensure_ascii=False,
    )
    service, _policy, knowledge, repository, _model, _selector = _service(
        chunks=[{"content": "支持私有化部署。", "metadata": {"file_id": "file-1", "chunk_index": 1}}],
        published={"file-1": _published_material("file-1")},
        model_content=payload,
        history=history,
        file_contents={
            "file-1": {
                "lines": [
                    {"chunk_order_index": 0, "content": "部署前需要完成环境检查。"},
                    {"chunk_order_index": 1, "content": "支持私有化部署。"},
                    {"chunk_order_index": 2, "content": "部署后需要进行健康检查。"},
                ]
            }
        },
    )

    result = await service.answer("那部署前要准备什么？", {"id": 7}, "conversation-1")

    assert knowledge.query_calls[0][0] == ("前文问题：\n企业版支持什么部署方式？\n\n当前问题：\n那部署前要准备什么？")
    assert repository.history_calls == [("conversation-1", 7, 6)]
    assert result.content == "部署前需要完成环境检查。[1]\n\n核心结论仍是支持私有化部署。[2]"
    assert [citation.locator for citation in result.citations] == ["第1段", "第2段"]
    prompt = _model.calls[0][0][1]["content"]
    assert '"role": "USER", "content": "企业版支持什么部署方式？"' in prompt
    assert '"evidence_id": "E3"' in prompt


@pytest.mark.asyncio
async def test_exact_duplicate_chunks_are_sent_to_the_model_once():
    payload = json.dumps(
        {"status": "SUPPORTED", "answer": "支持私有化部署。[E1]", "citation_ids": ["E1"]},
        ensure_ascii=False,
    )
    service, *_ = _service(
        chunks=[
            {"content": "支持私有化部署。", "metadata": {"file_id": "file-1", "chunk_index": 0}},
            {"content": "支持私有化部署。", "metadata": {"file_id": "file-2", "chunk_index": 3}},
        ],
        published={
            "file-1": _published_material("file-1", item_id="item-1"),
            "file-2": _published_material("file-2", item_id="item-2"),
        },
        model_content=payload,
    )

    result = await service.answer("是否支持私有化部署？", object(), "conversation-1")

    assert len(result.citations) == 1
    assert result.citations[0].item_id == "item-1"


@pytest.mark.asyncio
async def test_conflicting_answer_keeps_model_citation_order_and_deduplicates_ids():
    chunks = [
        {"content": "标准版支持 100 人。", "metadata": {"file_id": "file-1"}},
        {"content": "标准版支持 80 人。", "metadata": {"file_id": "file-2", "chunk_index": 0}},
    ]
    payload = json.dumps(
        {
            "status": "CONFLICTING",
            "answer": "两份现行资料分别写明 80 人和 100 人。",
            "citation_ids": ["E2", "E1", "E2"],
        },
        ensure_ascii=False,
    )
    service, *_ = _service(
        chunks=chunks,
        published={
            "file-1": _published_material("file-1", item_id="item-1"),
            "file-2": _published_material("file-2", item_id="item-2", title="规格说明"),
        },
        model_content=payload,
    )

    result = await service.answer("标准版支持多少人？", object(), "conversation-1")

    assert result.status == "CONFLICTING"
    assert result.content == "两份现行资料分别写明 80 人和 100 人。"
    assert [citation.evidence_id for citation in result.citations] == ["E2", "E1"]
    assert [citation.locator for citation in result.citations] == ["第1段", "文档正文"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_content",
    [
        "not-json",
        '```json\n{"status":"SUPPORTED","answer":"回答","citation_ids":["E1"]}\n```',
        json.dumps({"status": "SUPPORTED", "answer": "回答", "citation_ids": ["E9"]}),
        json.dumps({"status": "SUPPORTED", "answer": "回答[E9]", "citation_ids": ["E1"]}),
        json.dumps({"status": "SUPPORTED", "answer": "   ", "citation_ids": ["E1"]}),
        json.dumps({"status": "SUPPORTED", "answer": "回答", "citation_ids": []}),
        json.dumps({"status": [], "answer": "回答", "citation_ids": ["E1"]}),
    ],
    ids=[
        "invalid-json",
        "json-fence",
        "unknown-evidence",
        "unknown-inline-evidence",
        "empty-answer",
        "missing-citation",
        "non-string-status",
    ],
)
async def test_invalid_model_payloads_fall_back_to_exact_insufficient(model_content):
    service, *_rest, model, _selector = _service(
        chunks=[{"content": "正式内容", "metadata": {"file_id": "file-1"}}],
        published={"file-1": _published_material("file-1")},
        model_content=model_content,
    )

    result = await service.answer("问题", object(), "conversation-1")

    assert result.status == "INSUFFICIENT"
    assert result.content == INSUFFICIENT_TEXT
    assert result.citations == ()
    assert result.model_version == "model-1"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_conflicting_answer_requires_at_least_two_valid_citations():
    payload = json.dumps(
        {
            "status": "CONFLICTING",
            "answer": "两份现行资料存在冲突。",
            "citation_ids": ["E1"],
        },
        ensure_ascii=False,
    )
    service, *_ = _service(
        chunks=[
            {"content": "标准版支持 100 人。", "metadata": {"file_id": "file-1"}},
            {"content": "标准版支持 80 人。", "metadata": {"file_id": "file-2"}},
        ],
        published={
            "file-1": _published_material("file-1", item_id="item-1"),
            "file-2": _published_material("file-2", item_id="item-2"),
        },
        model_content=payload,
    )

    result = await service.answer("标准版支持多少人？", object(), "conversation-1")

    assert result.status == "INSUFFICIENT"
    assert result.content == INSUFFICIENT_TEXT
    assert result.citations == ()


@pytest.mark.asyncio
async def test_model_insufficient_status_forces_exact_text_and_no_citations():
    payload = json.dumps(
        {"status": "INSUFFICIENT", "answer": "模型自定义不足说明", "citation_ids": ["E1"]},
        ensure_ascii=False,
    )
    service, *_ = _service(
        chunks=[{"content": "正式内容", "metadata": {"file_id": "file-1"}}],
        published={"file-1": _published_material("file-1")},
        model_content=payload,
    )

    result = await service.answer("问题", object(), "conversation-1")

    assert result.status == "INSUFFICIENT"
    assert result.content == INSUFFICIENT_TEXT
    assert result.citations == ()
    assert result.model_version == "model-1"


@pytest.mark.asyncio
async def test_retrieval_and_model_failures_propagate():
    service, *_ = _service(chunks=[], published={}, retrieval_error=RuntimeError("retrieval unavailable"))
    with pytest.raises(RuntimeError, match="retrieval unavailable"):
        await service.answer("问题", object(), "conversation-1")

    service, *_ = _service(
        chunks=[{"content": "正式内容", "metadata": {"file_id": "file-1"}}],
        published={"file-1": _published_material("file-1")},
        model_error=RuntimeError("model unavailable"),
    )
    with pytest.raises(RuntimeError, match="model unavailable"):
        await service.answer("问题", object(), "conversation-1")
