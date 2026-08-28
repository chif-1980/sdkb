"""PostgreSQL 知识库模型 - KnowledgeBase、KnowledgeFile、评估相关表"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from yuxi.storage.postgres.models_business import Base
from yuxi.utils.datetime_utils import utc_now_naive

JSON_VALUE = JSON().with_variant(JSONB, "postgresql")


class KnowledgeBase(Base):
    """知识库模型"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("kb_id", name="uq_knowledge_bases_kb_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    kb_id = Column(String(80), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text)
    kb_type = Column(String(32), nullable=False, index=True)
    embedding_model_spec = Column(String(512))
    llm_model_spec = Column(String(512))
    query_params = Column(JSON_VALUE)
    additional_params = Column(JSON_VALUE)
    share_config = Column(JSON_VALUE)
    mindmap = Column(JSON_VALUE)
    mindmap_file_ids = Column(JSON_VALUE)
    mindmap_metadata = Column(JSON_VALUE)
    sample_questions = Column(JSON_VALUE)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeFile(Base):
    """知识文件模型"""

    __tablename__ = "knowledge_files"
    __table_args__ = (UniqueConstraint("file_id", name="uq_knowledge_files_file_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(String(64), unique=True, nullable=False, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    parent_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="SET NULL"), index=True)
    filename = Column(String(512), nullable=False)
    original_filename = Column(String(512))
    file_type = Column(String(64))
    path = Column(String(1024))
    minio_url = Column(String(1024))
    markdown_file = Column(String(1024))
    status = Column(String(32), default="uploaded", index=True)
    content_hash = Column(String(128), index=True)
    file_size = Column(BigInteger)
    chunk_count = Column(Integer, default=0)
    token_count = Column(BigInteger, default=0)
    content_type = Column(String(64))
    processing_params = Column(JSON_VALUE)
    is_folder = Column(Boolean, default=False)
    error_message = Column(Text)
    created_by = Column(String(64))
    updated_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeChunk(Base):
    """知识库 Chunk 模型"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("chunk_id", name="uq_knowledge_chunks_chunk_id"),
        Index("ix_knowledge_chunks_file_id", "file_id"),
        Index("ix_knowledge_chunks_kb_id", "kb_id"),
        Index("ix_knowledge_chunks_graph_indexed", "graph_indexed"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    chunk_id = Column(String(128), nullable=False)
    file_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="CASCADE"), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    start_char_pos = Column(Integer)
    end_char_pos = Column(Integer)
    start_token_pos = Column(Integer)
    end_token_pos = Column(Integer)
    graph_indexed = Column(Boolean, default=False)
    ent_ids = Column(JSON_VALUE)
    tags = Column(JSON_VALUE)
    extraction_result = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeGraphEntity(Base):
    """知识图谱实体"""

    __tablename__ = "knowledge_graph_entities"
    __table_args__ = (
        UniqueConstraint("entity_id", name="uq_knowledge_graph_entities_entity_id"),
        UniqueConstraint("kb_id", "normalized_name", "label", name="uq_knowledge_graph_entities_identity"),
        Index("ix_knowledge_graph_entities_kb_id", "kb_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(String(64), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    normalized_name = Column(String(512), nullable=False)
    label = Column(String(128), nullable=False)
    name = Column(String(512), nullable=False)
    attributes = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeGraphEntityMention(Base):
    """知识图谱实体在 chunk 中的引用"""

    __tablename__ = "knowledge_graph_entity_mentions"
    __table_args__ = (
        UniqueConstraint("entity_id", "chunk_id", name="uq_knowledge_graph_entity_mentions_entity_chunk"),
        Index("ix_knowledge_graph_entity_mentions_kb_id", "kb_id"),
        Index("ix_knowledge_graph_entity_mentions_file_id", "file_id"),
        Index("ix_knowledge_graph_entity_mentions_chunk_id", "chunk_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(String(64), ForeignKey("knowledge_graph_entities.entity_id", ondelete="CASCADE"), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    file_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="CASCADE"), nullable=False)
    chunk_id = Column(String(128), ForeignKey("knowledge_chunks.chunk_id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeGraphTriple(Base):
    """知识图谱三元组"""

    __tablename__ = "knowledge_graph_triples"
    __table_args__ = (
        UniqueConstraint("triple_id", name="uq_knowledge_graph_triples_triple_id"),
        Index("ix_knowledge_graph_triples_kb_id", "kb_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    triple_id = Column(String(64), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    source_entity_id = Column(
        String(64), ForeignKey("knowledge_graph_entities.entity_id", ondelete="CASCADE"), nullable=False
    )
    target_entity_id = Column(
        String(64), ForeignKey("knowledge_graph_entities.entity_id", ondelete="CASCADE"), nullable=False
    )
    relation_type = Column(String(256), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeGraphTripleMention(Base):
    """知识图谱三元组在 chunk 中的引用"""

    __tablename__ = "knowledge_graph_triple_mentions"
    __table_args__ = (
        UniqueConstraint("triple_id", "chunk_id", name="uq_knowledge_graph_triple_mentions_triple_chunk"),
        Index("ix_knowledge_graph_triple_mentions_kb_id", "kb_id"),
        Index("ix_knowledge_graph_triple_mentions_file_id", "file_id"),
        Index("ix_knowledge_graph_triple_mentions_chunk_id", "chunk_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    triple_id = Column(String(64), ForeignKey("knowledge_graph_triples.triple_id", ondelete="CASCADE"), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    file_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="CASCADE"), nullable=False)
    chunk_id = Column(String(128), ForeignKey("knowledge_chunks.chunk_id", ondelete="CASCADE"), nullable=False)
    text = Column(Text)
    extractor_type = Column(String(128))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class EvaluationDataset(Base):
    """评估数据集模型"""

    __tablename__ = "evaluation_datasets"
    __table_args__ = (UniqueConstraint("dataset_id", name="uq_evaluation_datasets_dataset_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(String(64), unique=True, nullable=False, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    item_count = Column(Integer, default=0)
    has_gold_chunks = Column(Boolean, default=False)
    has_gold_answers = Column(Boolean, default=False)
    build_metadata = Column(JSON_VALUE)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class EvaluationDatasetItem(Base):
    """评估数据集题目模型"""

    __tablename__ = "evaluation_dataset_items"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_evaluation_dataset_items_item_id"),
        UniqueConstraint("dataset_id", "item_index", name="uq_evaluation_dataset_items_dataset_index"),
        Index("ix_evaluation_dataset_items_dataset_index", "dataset_id", "item_index"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(String(64), unique=True, nullable=False, index=True)
    dataset_id = Column(
        String(64),
        ForeignKey("evaluation_datasets.dataset_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    item_index = Column(Integer, nullable=False)
    query_text = Column(Text, nullable=False)
    gold_chunk_ids = Column(JSON_VALUE)
    gold_answer = Column(Text)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class EvaluationRun(Base):
    """评估运行模型"""

    __tablename__ = "evaluation_runs"
    __table_args__ = (UniqueConstraint("run_id", name="uq_evaluation_runs_run_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_id = Column(
        String(64),
        ForeignKey("evaluation_datasets.dataset_id", ondelete="SET NULL"),
        index=True,
    )
    status = Column(String(32), default="running", index=True)
    retrieval_config = Column(JSON_VALUE)
    metrics = Column(JSON_VALUE)
    overall_score = Column(Float)
    total_items = Column(Integer, default=0)
    completed_items = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), default=utc_now_naive, index=True)
    completed_at = Column(DateTime(timezone=True))
    created_by = Column(String(64))


class EvaluationRunItem(Base):
    """评估逐题结果模型"""

    __tablename__ = "evaluation_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "item_index", name="uq_evaluation_run_items_run_index"),
        Index("ix_evaluation_run_items_run_index", "run_id", "item_index"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(
        String(64),
        ForeignKey("evaluation_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_item_id = Column(
        String(64), ForeignKey("evaluation_dataset_items.item_id", ondelete="SET NULL"), index=True
    )
    item_index = Column(Integer, nullable=False)
    query_text = Column(Text, nullable=False)
    gold_chunk_ids = Column(JSON_VALUE)
    gold_answer = Column(Text)
    generated_answer = Column(Text)
    retrieved_chunks = Column(JSON_VALUE)
    metrics = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class FeishuSource(Base):
    """飞书只读知识源配置（仅保存凭据变量名，不保存凭据正文）。"""

    __tablename__ = "feishu_sources"
    __table_args__ = (UniqueConstraint("source_id", name="uq_feishu_sources_source_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    wiki_root_token = Column(String(255), nullable=False)
    wiki_root_url = Column(String(1024))
    # root 保持历史行为；space 扫描同一知识空间下的全部顶层节点。
    scan_scope = Column(String(16), nullable=False, default="root", server_default="root")
    target_kb_id = Column(String(80), nullable=False, index=True)
    credential_env_name = Column(String(255), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class FeishuUserOAuthCredential(Base):
    """Encrypted user OAuth credentials for one Feishu knowledge source."""

    __tablename__ = "feishu_user_oauth_credentials"
    __table_args__ = (UniqueConstraint("source_id", name="uq_feishu_user_oauth_credentials_source_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(
        String(64), ForeignKey("feishu_sources.source_id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    access_token_ciphertext = Column(Text, nullable=False)
    refresh_token_ciphertext = Column(Text, nullable=False)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=False)
    refresh_token_expires_at = Column(DateTime(timezone=True), nullable=False)
    feishu_open_id = Column(String(128))
    display_name = Column(String(255))
    scopes = Column(Text)
    authorization_status = Column(String(32), nullable=False, default="active", index=True)
    authorized_by = Column(String(64))
    last_error = Column(String(512))
    last_refreshed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class FeishuSyncRun(Base):
    """一次飞书全量或增量扫描批次。"""

    __tablename__ = "feishu_sync_runs"
    __table_args__ = (Index("ix_feishu_sync_runs_source_status", "source_id", "status"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, unique=True, index=True)
    source_id = Column(
        String(64), ForeignKey("feishu_sources.source_id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_type = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="running", index=True)
    started_at = Column(DateTime(timezone=True), default=utc_now_naive)
    finished_at = Column(DateTime(timezone=True))
    operator_id = Column(String(64))
    scanned_count = Column(Integer, default=0)
    new_count = Column(Integer, default=0)
    changed_count = Column(Integer, default=0)
    unchanged_count = Column(Integer, default=0)
    unsupported_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    invalidated_count = Column(Integer, default=0)
    error_summary = Column(Text)


class FeishuSourceItem(Base):
    """稳定的飞书页面或附件对象，active_version_id 指向当前发布版本。"""

    __tablename__ = "feishu_source_items"
    __table_args__ = (
        UniqueConstraint("item_key", name="uq_feishu_source_items_item_key"),
        Index("ix_feishu_source_items_source_validity", "source_id", "source_validity"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(String(64), nullable=False, unique=True, index=True)
    source_id = Column(
        String(64), ForeignKey("feishu_sources.source_id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_key = Column(String(512), nullable=False, unique=True)
    item_type = Column(String(32), nullable=False)
    title = Column(String(512))
    parent_item_key = Column(String(512))
    path_text = Column(String(2048))
    source_url = Column(String(2048))
    last_seen_at = Column(DateTime(timezone=True))
    source_updated_at = Column(DateTime(timezone=True))
    source_validity = Column(String(32), nullable=False, default="valid")
    active_version_id = Column(String(64), index=True)
    publication_status = Column(String(32), nullable=False, default="ACTIVE", index=True)
    lifecycle_note = Column(Text)
    lifecycle_updated_by = Column(String(64))
    lifecycle_updated_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class FeishuMaterialVersion(Base):
    """素材内容版本及解析、审核、发布状态。"""

    __tablename__ = "feishu_material_versions"
    __table_args__ = (
        UniqueConstraint("item_id", "revision", "content_hash", name="uq_feishu_material_versions_identity"),
        Index("ix_feishu_material_versions_item_status", "item_id", "processing_status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    version_id = Column(String(64), nullable=False, unique=True, index=True)
    item_id = Column(
        String(64), ForeignKey("feishu_source_items.item_id", ondelete="CASCADE"), nullable=False, index=True
    )
    sync_run_id = Column(
        String(64), ForeignKey("feishu_sync_runs.run_id", ondelete="SET NULL"), nullable=True, index=True
    )
    revision = Column(String(128), nullable=False)
    content_hash = Column(String(128), nullable=False)
    source_object_path = Column(String(1024))
    parsed_object_path = Column(String(1024))
    processing_status = Column(String(32), nullable=False, default="discovered", index=True)
    processing_params = Column(JSON_VALUE)
    error_code = Column(String(64))
    error_message = Column(Text)
    retry_count = Column(Integer, nullable=False, default=0)
    review_status = Column(String(32), nullable=False, default="pending", index=True)
    reviewer_id = Column(String(64))
    reviewed_at = Column(DateTime(timezone=True))
    review_comment = Column(Text)
    yuxi_file_id = Column(String(64), index=True)
    chunk_count = Column(Integer, default=0)
    token_count = Column(BigInteger, default=0)
    published_at = Column(DateTime(timezone=True))
    replaced_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class FeishuSourceSegment(Base):
    """Stable, versioned source segment produced from one parsed Feishu material."""

    __tablename__ = "feishu_source_segments"
    __table_args__ = (
        UniqueConstraint("segment_id", name="uq_feishu_source_segments_segment_id"),
        UniqueConstraint("version_id", "segment_key", name="uq_feishu_source_segments_version_key"),
        Index("ix_feishu_source_segments_version_status", "version_id", "status"),
        Index("ix_feishu_source_segments_file_index", "yuxi_file_id", "segment_index"),
        Index("ix_feishu_source_segments_hash", "content_hash"),
        Index("ix_feishu_source_segments_publication", "version_id", "publication_state"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    segment_id = Column(String(64), nullable=False, unique=True, index=True)
    segment_key = Column(String(128), nullable=False)
    version_id = Column(
        String(64), ForeignKey("feishu_material_versions.version_id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id = Column(
        String(64), ForeignKey("feishu_source_items.item_id", ondelete="CASCADE"), nullable=False, index=True
    )
    yuxi_file_id = Column(String(64), nullable=False, index=True)
    segment_index = Column(Integer, nullable=False)
    segment_type = Column(String(32), nullable=False, default="paragraph", index=True)
    title_path = Column(JSON_VALUE, nullable=False, default=list)
    locator_json = Column(JSON_VALUE, nullable=False, default=dict)
    content = Column(Text, nullable=False)
    content_hash = Column(String(128), nullable=False, index=True)
    token_count = Column(Integer, nullable=False, default=0)
    publication_state = Column(String(32), nullable=False, default="PENDING", index=True)
    status = Column(String(32), nullable=False, default="ACTIVE", index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive, nullable=False)


class FeishuKnowledgeUnit(Base):
    """Versioned semantic review unit composed from one or more source segments."""

    __tablename__ = "feishu_knowledge_units"
    __table_args__ = (
        UniqueConstraint("unit_id", name="uq_feishu_knowledge_units_unit_id"),
        UniqueConstraint("version_id", "unit_key", name="uq_feishu_knowledge_units_version_key"),
        Index("ix_feishu_knowledge_units_version_status", "version_id", "status"),
        Index("ix_feishu_knowledge_units_item_lineage", "item_id", "lineage_key"),
        Index("ix_feishu_knowledge_units_publication", "version_id", "publication_state"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    unit_id = Column(String(64), nullable=False, unique=True, index=True)
    unit_key = Column(String(128), nullable=False)
    lineage_key = Column(String(128), nullable=False, index=True)
    version_id = Column(
        String(64), ForeignKey("feishu_material_versions.version_id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id = Column(
        String(64), ForeignKey("feishu_source_items.item_id", ondelete="CASCADE"), nullable=False, index=True
    )
    unit_index = Column(Integer, nullable=False)
    unit_type = Column(String(32), nullable=False, default="SECTION", index=True)
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    content_hash = Column(String(128), nullable=False, index=True)
    source_segment_ids = Column(JSON_VALUE, nullable=False, default=list)
    locator_json = Column(JSON_VALUE, nullable=False, default=dict)
    change_type = Column(String(24), nullable=False, default="NEW", index=True)
    previous_unit_id = Column(String(64), index=True)
    matched_logical_knowledge_id = Column(String(64), index=True)
    recommended_outcome = Column(String(48), nullable=False)
    recommendation_reason = Column(Text, nullable=False)
    recommendation_confidence = Column(Float, nullable=False, default=0.0)
    manual_review_required = Column(Boolean, nullable=False, default=False, index=True)
    publication_state = Column(String(32), nullable=False, default="PENDING", index=True)
    lifecycle_status = Column(String(32), nullable=False, default="ACTIVE", index=True)
    owner_id = Column(String(64))
    owner_name = Column(String(255))
    valid_from = Column(DateTime(timezone=True))
    valid_until = Column(DateTime(timezone=True))
    review_due_at = Column(DateTime(timezone=True))
    lifecycle_note = Column(Text)
    lifecycle_updated_by = Column(String(64))
    lifecycle_updated_at = Column(DateTime(timezone=True))
    status = Column(String(32), nullable=False, default="ACTIVE", index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive, nullable=False)


class FeishuProcessingEvent(Base):
    """追加式素材加工审计事件。"""

    __tablename__ = "feishu_processing_events"
    __table_args__ = (Index("ix_feishu_processing_events_item_created", "item_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(
        String(64), ForeignKey("feishu_sources.source_id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id = Column(String(64), ForeignKey("feishu_source_items.item_id", ondelete="CASCADE"), index=True)
    version_id = Column(String(64), ForeignKey("feishu_material_versions.version_id", ondelete="CASCADE"), index=True)
    event_type = Column(String(64), nullable=False)
    from_status = Column(String(32))
    to_status = Column(String(32))
    operator_id = Column(String(64))
    message = Column(Text)
    payload_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False, index=True)


class FeishuGovernanceReview(Base):
    """One persistent human-review task for a Feishu material version."""

    __tablename__ = "feishu_governance_reviews"
    __table_args__ = (
        UniqueConstraint("review_id", name="uq_feishu_governance_reviews_review_id"),
        UniqueConstraint("version_id", name="uq_feishu_governance_reviews_version_id"),
        Index("ix_feishu_governance_reviews_status_assignee", "status", "assignee_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(String(64), nullable=False, unique=True, index=True)
    version_id = Column(
        String(64), ForeignKey("feishu_material_versions.version_id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status = Column(String(32), nullable=False, default="pending", index=True)
    assignee_id = Column(String(64), index=True)
    decision = Column(String(32))
    action = Column(String(32))
    problem_tags = Column(JSON_VALUE, nullable=False, default=list)
    decision_comment = Column(Text)
    applicability_scope = Column(JSON_VALUE, nullable=False, default=dict)
    decided_by = Column(String(64))
    decided_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive, nullable=False)


class FeishuCrossDocumentRelation(Base):
    """Persisted evidence and adjudication for one cross-document comparison."""

    __tablename__ = "feishu_cross_document_relations"
    __table_args__ = (
        UniqueConstraint("relation_id", name="uq_feishu_cross_document_relations_relation_id"),
        UniqueConstraint("comparison_key", name="uq_feishu_cross_document_relations_comparison_key"),
        Index("ix_feishu_cross_document_relations_source_status", "source_version_id", "status"),
        Index("ix_feishu_cross_document_relations_target_status", "target_version_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    relation_id = Column(String(64), nullable=False, unique=True, index=True)
    comparison_key = Column(String(256), nullable=False, unique=True)
    source_version_id = Column(
        String(64), ForeignKey("feishu_material_versions.version_id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_version_id = Column(
        String(64), ForeignKey("feishu_material_versions.version_id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type = Column(String(32), nullable=False, index=True)
    similarity = Column(Float)
    confidence = Column(Float)
    same_content = Column(JSON_VALUE, nullable=False, default=list)
    different_content = Column(JSON_VALUE, nullable=False, default=list)
    scope_difference = Column(JSON_VALUE, nullable=False, default=dict)
    reasoning = Column(Text)
    status = Column(String(32), nullable=False, default="open", index=True)
    human_decision = Column(String(32))
    human_comment = Column(Text)
    resolved_by = Column(String(64))
    resolved_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive, nullable=False)


class FeishuLogicalKnowledge(Base):
    """One governed knowledge fact shared by one primary and multiple duplicate sources."""

    __tablename__ = "feishu_logical_knowledge"
    __table_args__ = (
        UniqueConstraint("logical_knowledge_id", name="uq_feishu_logical_knowledge_id"),
        Index("ix_feishu_logical_knowledge_source_status", "source_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    logical_knowledge_id = Column(String(64), nullable=False, unique=True, index=True)
    source_id = Column(
        String(64), ForeignKey("feishu_sources.source_id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String(512), nullable=False)
    status = Column(String(32), nullable=False, default="ACTIVE", index=True)
    primary_source_ref_id = Column(String(64), index=True)
    merged_into_id = Column(String(64), index=True)
    created_by = Column(String(64))
    updated_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive, nullable=False)


class FeishuKnowledgeSourceFragment(Base):
    """A chunk-level source for governed logical knowledge."""

    __tablename__ = "feishu_knowledge_source_fragments"
    __table_args__ = (
        UniqueConstraint("source_ref_id", name="uq_feishu_knowledge_source_fragments_ref"),
        UniqueConstraint(
            "logical_knowledge_id",
            "version_id",
            "chunk_id",
            name="uq_feishu_knowledge_source_fragments_identity",
        ),
        Index("ix_feishu_knowledge_source_fragments_logical_role", "logical_knowledge_id", "source_role"),
        Index("ix_feishu_knowledge_source_fragments_version_chunk", "version_id", "chunk_id"),
        Index("ix_feishu_knowledge_source_fragments_version_segment", "version_id", "segment_id"),
        Index("ix_feishu_knowledge_source_fragments_relation", "relation_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_ref_id = Column(String(64), nullable=False, unique=True, index=True)
    logical_knowledge_id = Column(
        String(64),
        ForeignKey("feishu_logical_knowledge.logical_knowledge_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relation_id = Column(
        String(64),
        ForeignKey("feishu_cross_document_relations.relation_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    version_id = Column(
        String(64), ForeignKey("feishu_material_versions.version_id", ondelete="CASCADE"), nullable=False, index=True
    )
    yuxi_file_id = Column(String(64), nullable=False, index=True)
    chunk_id = Column(String(128), nullable=False, index=True)
    segment_id = Column(String(64), index=True)
    chunk_index = Column(Integer, nullable=False)
    content_hash = Column(String(128), nullable=False, index=True)
    content_snapshot = Column(Text, nullable=False)
    locator_json = Column(JSON_VALUE, nullable=False, default=dict)
    source_role = Column(String(32), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="ACTIVE", index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive, nullable=False)


class FeishuDuplicateRelationDecision(Base):
    """Idempotent adjudication that links one duplicate relation to logical knowledge groups."""

    __tablename__ = "feishu_duplicate_relation_decisions"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_feishu_duplicate_relation_decisions_id"),
        UniqueConstraint("relation_id", name="uq_feishu_duplicate_relation_decisions_relation"),
        UniqueConstraint("request_id", name="uq_feishu_duplicate_relation_decisions_request"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    decision_id = Column(String(64), nullable=False, unique=True, index=True)
    relation_id = Column(
        String(64),
        ForeignKey("feishu_cross_document_relations.relation_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    request_id = Column(String(64), nullable=False, unique=True, index=True)
    strategy = Column(String(32), nullable=False)
    primary_version_id = Column(String(64), index=True)
    logical_knowledge_ids = Column(JSON_VALUE, nullable=False, default=list)
    fragment_match_ids = Column(JSON_VALUE, nullable=False, default=list)
    comment = Column(Text)
    decided_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)


class FeishuReviewPackage(Base):
    """Business-facing review container for one source version or lifecycle trigger."""

    __tablename__ = "feishu_review_packages"
    __table_args__ = (
        UniqueConstraint("package_id", name="uq_feishu_review_packages_package_id"),
        UniqueConstraint("package_key", name="uq_feishu_review_packages_package_key"),
        Index("ix_feishu_review_packages_source_status_assignee", "source_id", "workflow_status", "assignee_id"),
        Index("ix_feishu_review_packages_source_item", "source_item_id"),
        Index("ix_feishu_review_packages_source_version", "source_version_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    package_id = Column(String(64), nullable=False, unique=True, index=True)
    package_key = Column(String(256), nullable=False, unique=True)
    source_id = Column(
        String(64), ForeignKey("feishu_sources.source_id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_item_id = Column(
        String(64), ForeignKey("feishu_source_items.item_id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_version_id = Column(
        String(64), ForeignKey("feishu_material_versions.version_id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_type = Column(String(32), nullable=False, default="SOURCE_VERSION", index=True)
    title_snapshot = Column(String(512))
    path_snapshot = Column(String(2048))
    source_url_snapshot = Column(String(2048))
    workflow_status = Column(String(40), nullable=False, default="OPEN", index=True)
    assignee_id = Column(String(64), index=True)
    risk_level = Column(String(16), nullable=False, default="MEDIUM")
    draft_json = Column(JSON_VALUE, nullable=False, default=dict)
    lock_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive, nullable=False)
    completed_at = Column(DateTime(timezone=True))


class FeishuReviewItem(Base):
    """One independently decidable item inside a review package."""

    __tablename__ = "feishu_review_items"
    __table_args__ = (
        UniqueConstraint("review_item_id", name="uq_feishu_review_items_review_item_id"),
        UniqueConstraint("package_id", "candidate_key", name="uq_feishu_review_items_package_candidate"),
        Index("ix_feishu_review_items_package_status", "package_id", "item_status"),
        Index("ix_feishu_review_items_type_status", "review_type", "item_status"),
        Index("ix_feishu_review_items_subject", "subject_type", "subject_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_item_id = Column(String(64), nullable=False, unique=True, index=True)
    package_id = Column(
        String(64), ForeignKey("feishu_review_packages.package_id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_key = Column(String(256), nullable=False)
    review_type = Column(String(24), nullable=False, index=True)
    subject_type = Column(String(32), nullable=False)
    subject_id = Column(String(64), nullable=False)
    title = Column(String(512))
    summary = Column(Text)
    subject_locator_json = Column(JSON_VALUE, nullable=False, default=dict)
    evidence_json = Column(JSON_VALUE, nullable=False, default=dict)
    relation_ids = Column(JSON_VALUE, nullable=False, default=list)
    problem_tags = Column(JSON_VALUE, nullable=False, default=list)
    applicability_scope = Column(JSON_VALUE, nullable=False, default=dict)
    item_status = Column(String(40), nullable=False, default="PENDING", index=True)
    outcome = Column(String(48))
    internal_action = Column(String(32))
    decision_comment = Column(Text)
    decision_payload = Column(JSON_VALUE, nullable=False, default=dict)
    decided_by = Column(String(64))
    decided_at = Column(DateTime(timezone=True))
    reopened_from_item_id = Column(
        String(64), ForeignKey("feishu_review_items.review_item_id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive, nullable=False)


class FeishuSourceChangeRequest(Base):
    """Request for the source owner to correct the read-only Feishu original."""

    __tablename__ = "feishu_source_change_requests"
    __table_args__ = (
        UniqueConstraint("change_request_id", name="uq_feishu_source_change_requests_request_id"),
        UniqueConstraint(
            "review_item_id",
            "round_number",
            name="uq_feishu_source_change_requests_item_round",
        ),
        Index("ix_feishu_change_requests_status_source_item", "status", "source_item_id"),
        Index("ix_feishu_change_requests_review_status", "review_item_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    change_request_id = Column(String(64), nullable=False, unique=True, index=True)
    review_item_id = Column(
        String(64), ForeignKey("feishu_review_items.review_item_id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_item_id = Column(
        String(64), ForeignKey("feishu_source_items.item_id", ondelete="SET NULL"), nullable=True, index=True
    )
    requested_version_id = Column(
        String(64), ForeignKey("feishu_material_versions.version_id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_url = Column(String(2048))
    status = Column(String(40), nullable=False, default="OPEN", index=True)
    request_text = Column(Text, nullable=False)
    responsible_user_id = Column(String(128))
    responsible_user_name = Column(String(255))
    round_number = Column(Integer, nullable=False, default=1)
    received_version_id = Column(
        String(64), ForeignKey("feishu_material_versions.version_id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive, nullable=False)
    resolved_at = Column(DateTime(timezone=True))
