"""Evidence-constrained answers for the enterprise assistant."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.product_chat.repository import ProductChatRepository
from yuxi.product_chat.source_policy_service import ProductSourcePolicyService
from yuxi.utils import logger

PROMPT_VERSION = "enterprise-grounded-v1"
INSUFFICIENT_TEXT = "暂无足够可靠资料"
NO_MODEL_VERSION = "not-called"

SYSTEM_PROMPT = """你是企业知识助手。只能依据 EVIDENCE 中的文字回答。
不得使用常识补充企业能力、参数、承诺或案例。
证据不足时 status 必须是 INSUFFICIENT，answer 必须是“暂无足够可靠资料”。
证据互相冲突且无法由版本时间消解时 status 必须是 CONFLICTING，并说明冲突条件。
返回严格 JSON：{"status":"SUPPORTED|INSUFFICIENT|CONFLICTING","answer":"中文答案","citation_ids":["E1"]}。
citation_ids 只能使用输入中的证据编号。"""


@dataclass(frozen=True, slots=True)
class GroundedCitation:
    evidence_id: str
    source_id: str
    item_id: str
    version_id: str
    yuxi_file_id: str
    title: str
    source_url: str
    path_text: str | None
    locator: str
    excerpt: str
    source_version_at: datetime | None


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    status: Literal["SUPPORTED", "INSUFFICIENT", "CONFLICTING"]
    content: str
    citations: tuple[GroundedCitation, ...]
    model_version: str
    prompt_version: str = PROMPT_VERSION


class AnswerService:
    def __init__(
        self,
        *,
        db: AsyncSession,
        repository: ProductChatRepository | None = None,
        policy_service: Any | None = None,
        knowledge_base: Any | None = None,
        model_selector: Callable[[str], Any] | None = None,
    ) -> None:
        if knowledge_base is None:
            from yuxi.knowledge.runtime import knowledge_base as runtime_knowledge_base

            knowledge_base = runtime_knowledge_base
        if model_selector is None:
            from yuxi.models import select_model

            model_selector = select_model

        self._repository = repository if repository is not None else ProductChatRepository(db)
        self._knowledge_base = knowledge_base
        self._policy_service = (
            policy_service
            if policy_service is not None
            else ProductSourcePolicyService(db=db, knowledge_base=knowledge_base)
        )
        self._model_selector = model_selector

    async def answer(self, question: str, user: Any, conversation_id: str) -> GroundedAnswer:
        started_at = perf_counter()
        evidence_count = 0
        try:
            scope = await self._policy_service.resolve_scope(user)
            chunks = await self._knowledge_base.aquery(
                question,
                scope.kb_id,
                search_mode="hybrid",
                allowed_file_ids=list(scope.allowed_file_ids),
                use_graph_retrieval=False,
            )
            evidence = await self._revalidate_evidence(scope.source_id, chunks)
            evidence_count = len(evidence)
            if not evidence:
                result = self._insufficient(NO_MODEL_VERSION)
            else:
                database_info = await self._knowledge_base.get_database_info(scope.kb_id)
                model_spec = database_info.get("llm_model_spec") if isinstance(database_info, dict) else None
                model = self._model_selector(model_spec)
                response = await model.call(self._build_prompt(question, evidence))
                result = self._parse_model_response(
                    getattr(response, "content", None),
                    evidence,
                    model.model_name,
                )
            logger.info(
                "product_answer conversation_id={} status={} evidence_count={} citation_count={} duration_ms={}",
                conversation_id,
                result.status,
                evidence_count,
                len(result.citations),
                round((perf_counter() - started_at) * 1000),
            )
            return result
        except Exception as exc:
            logger.error(
                "product_answer_failed conversation_id={} error_type={} evidence_count={} duration_ms={}",
                conversation_id,
                type(exc).__name__,
                evidence_count,
                round((perf_counter() - started_at) * 1000),
            )
            raise

    async def _revalidate_evidence(self, source_id: str, chunks: Any) -> tuple[GroundedCitation, ...]:
        usable_chunks: list[tuple[dict[str, Any], str]] = []
        file_ids: list[str] = []
        if isinstance(chunks, list):
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                metadata = chunk.get("metadata")
                content = chunk.get("content")
                if not isinstance(metadata, dict) or not isinstance(content, str) or not content.strip():
                    continue
                file_id = metadata.get("file_id")
                if not isinstance(file_id, str) or not file_id:
                    continue
                usable_chunks.append((chunk, file_id))
                file_ids.append(file_id)

        published = await self._repository.get_published_evidence(source_id, file_ids)
        evidence: list[GroundedCitation] = []
        for chunk, file_id in usable_chunks:
            material = published.get(file_id)
            if material is None:
                continue
            item, version = material
            metadata = chunk["metadata"]
            chunk_index = metadata.get("chunk_index")
            locator = (
                f"第{chunk_index + 1}段"
                if isinstance(chunk_index, int) and not isinstance(chunk_index, bool)
                else "文档正文"
            )
            evidence.append(
                GroundedCitation(
                    evidence_id=f"E{len(evidence) + 1}",
                    source_id=item.source_id,
                    item_id=item.item_id,
                    version_id=version.version_id,
                    yuxi_file_id=version.yuxi_file_id,
                    title=item.title or "",
                    source_url=item.source_url or "",
                    path_text=item.path_text,
                    locator=locator,
                    excerpt=chunk["content"],
                    source_version_at=version.published_at,
                )
            )
        return tuple(evidence)

    @staticmethod
    def _build_prompt(question: str, evidence: tuple[GroundedCitation, ...]) -> str:
        evidence_payload = [
            {
                "evidence_id": citation.evidence_id,
                "title": citation.title,
                "locator": citation.locator,
                "excerpt": citation.excerpt,
                "source_version_at": (
                    citation.source_version_at.isoformat() if citation.source_version_at is not None else None
                ),
            }
            for citation in evidence
        ]
        return (
            f"{SYSTEM_PROMPT}\n\nEVIDENCE:\n{json.dumps(evidence_payload, ensure_ascii=False)}\n\nQUESTION:\n{question}"
        )

    @classmethod
    def _parse_model_response(
        cls,
        raw_content: Any,
        evidence: tuple[GroundedCitation, ...],
        model_version: str,
    ) -> GroundedAnswer:
        fallback = cls._insufficient(model_version)
        if not isinstance(raw_content, str):
            return fallback
        try:
            payload = json.loads(raw_content)
        except (TypeError, json.JSONDecodeError):
            return fallback
        if not isinstance(payload, dict) or set(payload) != {"status", "answer", "citation_ids"}:
            return fallback

        status = payload["status"]
        content = payload["answer"]
        citation_ids = payload["citation_ids"]
        if (
            status not in {"SUPPORTED", "INSUFFICIENT", "CONFLICTING"}
            or not isinstance(content, str)
            or not isinstance(citation_ids, list)
            or any(not isinstance(evidence_id, str) for evidence_id in citation_ids)
        ):
            return fallback

        by_id = {citation.evidence_id: citation for citation in evidence}
        if any(evidence_id not in by_id for evidence_id in citation_ids):
            return fallback
        selected: list[GroundedCitation] = []
        seen: set[str] = set()
        for evidence_id in citation_ids:
            if evidence_id not in seen:
                seen.add(evidence_id)
                selected.append(by_id[evidence_id])

        normalized_content = content.strip()
        if status == "INSUFFICIENT":
            return fallback
        if status == "SUPPORTED" and (not normalized_content or not selected):
            return fallback
        if status == "CONFLICTING" and (not normalized_content or len(selected) < 2):
            return fallback
        return GroundedAnswer(
            status=status,
            content=normalized_content,
            citations=tuple(selected),
            model_version=model_version,
        )

    @staticmethod
    def _insufficient(model_version: str) -> GroundedAnswer:
        return GroundedAnswer(
            status="INSUFFICIENT",
            content=INSUFFICIENT_TEXT,
            citations=(),
            model_version=model_version,
        )
