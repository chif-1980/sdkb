"""Enterprise assistant identity, conversation, message, and citation models."""

from enum import StrEnum
from secrets import token_bytes
from time import time_ns

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    JSON,
    UniqueConstraint,
)

from yuxi.storage.postgres.models_business import Base
from yuxi.utils.datetime_utils import utc_now_naive

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _new_ulid() -> str:
    value = ((time_ns() // 1_000_000) << 80) | int.from_bytes(token_bytes(10), "big")
    return "".join(_ULID_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


class AuthorizationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class ConversationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class MessageRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class AnswerStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"


class CitationKind(StrEnum):
    ENTERPRISE_EVIDENCE = "ENTERPRISE_EVIDENCE"


class FeishuUserBinding(Base):
    __tablename__ = "feishu_user_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    feishu_open_id = Column(String(128), nullable=False, unique=True)
    feishu_user_id = Column(String(128), nullable=True, unique=True)
    feishu_union_id = Column(String(128), nullable=True)
    tenant_key = Column(String(128), nullable=False)
    display_name = Column(String(255), nullable=False)
    avatar_url = Column(String(1024), nullable=True)
    authorization_status = Column(
        Enum(AuthorizationStatus, name="feishu_authorization_status", native_enum=False, create_constraint=True),
        nullable=False,
        default=AuthorizationStatus.ACTIVE,
    )
    last_login_at = Column(DateTime, nullable=False, default=utc_now_naive)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)


class FeishuDepartmentBinding(Base):
    __tablename__ = "feishu_department_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_key = Column(String(128), nullable=False)
    feishu_department_id = Column(String(128), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False, unique=True)
    display_name = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    __table_args__ = (
        UniqueConstraint(
            "tenant_key",
            "feishu_department_id",
            name="uq_feishu_department_bindings_tenant_department",
        ),
    )


class FeishuUserDepartmentMembership(Base):
    __tablename__ = "feishu_user_department_memberships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    department_binding_id = Column(Integer, ForeignKey("feishu_department_bindings.id"), nullable=False)
    position = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "department_binding_id",
            name="uq_feishu_user_department_memberships_user_department",
        ),
        Index(
            "ix_feishu_user_department_memberships_user_position",
            "user_id",
            "position",
        ),
    )


class ProductConversation(Base):
    __tablename__ = "product_conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(26), nullable=False, unique=True, default=_new_ulid)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(80), nullable=True)
    status = Column(
        Enum(ConversationStatus, name="product_conversation_status", native_enum=False, create_constraint=True),
        nullable=False,
        default=ConversationStatus.ACTIVE,
    )
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    __table_args__ = (Index("ix_product_conversations_owner_status_updated", "owner_user_id", "status", "updated_at"),)


class ProductMessage(Base):
    __tablename__ = "product_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(26), nullable=False, unique=True, default=_new_ulid)
    conversation_id = Column(String(26), ForeignKey("product_conversations.conversation_id"), nullable=False)
    role = Column(
        Enum(MessageRole, name="product_message_role", native_enum=False, create_constraint=True),
        nullable=False,
    )
    content = Column(Text, nullable=False)
    answer_status = Column(
        Enum(AnswerStatus, name="product_answer_status", native_enum=False, create_constraint=True),
        nullable=True,
    )
    model_version = Column(String(128), nullable=True)
    prompt_version = Column(String(128), nullable=True)
    feedback_rating = Column(String(8), nullable=True)
    feedback_reason_type = Column(String(32), nullable=True)
    feedback_reason_text = Column(Text, nullable=True)
    solution_draft_id = Column(String(64), nullable=True, index=True)
    # A product request can be retried after a browser/network interruption.
    # Keep the id on both messages so the repository can return the original
    # exchange instead of appending a duplicate pair.
    request_id = Column(String(128), nullable=True, index=True)
    skill_id = Column(String(32), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)

    __table_args__ = (
        CheckConstraint(
            "(role != 'USER' OR (answer_status IS NULL AND model_version IS NULL AND prompt_version IS NULL)) "
            "AND (role != 'ASSISTANT' OR answer_status IS NOT NULL)",
            name="ck_product_messages_role_payload",
        ),
        CheckConstraint(
            "feedback_rating IS NULL OR feedback_rating IN ('LIKE', 'DISLIKE')",
            name="ck_product_messages_feedback_rating",
        ),
        Index("ix_product_messages_conversation_created", "conversation_id", "created_at"),
    )


class SolutionDraft(Base):
    """Current immutable projection of one solution-draft Agent Run."""

    __tablename__ = "solution_drafts"

    id = Column(String(64), primary_key=True)
    conversation_id = Column(String(26), ForeignKey("product_conversations.conversation_id"), nullable=False)
    source_run_id = Column(String(64), nullable=False, unique=True)
    current_version = Column(Integer, nullable=False, default=1)
    status = Column(String(32), nullable=False, default="GENERATING")
    title = Column(String(512), nullable=False, default="方案草稿")
    customer_context = Column(Text, nullable=False, default="")
    executive_summary = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    __table_args__ = (
        Index("ix_solution_drafts_conversation_updated", "conversation_id", "updated_at"),
    )


class SolutionDraftVersion(Base):
    """Append-only version history for a solution draft."""

    __tablename__ = "solution_draft_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    draft_id = Column(String(64), ForeignKey("solution_drafts.id", ondelete="CASCADE"), nullable=False)
    version = Column(Integer, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    editor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)

    __table_args__ = (
        UniqueConstraint("draft_id", "version", name="uq_solution_draft_versions_draft_version"),
        Index("ix_solution_draft_versions_draft_created", "draft_id", "created_at"),
    )


class CapabilityCatalog(Base):
    """Governed enterprise capability map used by the solution architect."""

    __tablename__ = "capability_catalog"

    id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    category = Column(String(128), nullable=False, default="")
    delivery_status = Column(String(32), nullable=False, default="UNKNOWN")
    description = Column(Text, nullable=False, default="")
    supported_scopes = Column(JSON, nullable=False, default=list)
    limitations = Column(JSON, nullable=False, default=list)
    owner = Column(String(255), nullable=True)
    valid_until = Column(DateTime, nullable=True)
    tenant_key = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    __table_args__ = (
        Index("ix_capability_catalog_tenant_status", "tenant_key", "delivery_status"),
        Index("ix_capability_catalog_name", "name"),
    )


class CapabilityEvidence(Base):
    """Traceable evidence backing a catalog capability."""

    __tablename__ = "capability_evidence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    capability_id = Column(String(64), ForeignKey("capability_catalog.id", ondelete="CASCADE"), nullable=False)
    citation_id = Column(String(128), nullable=False)
    evidence_type = Column(String(64), nullable=False, default="ENTERPRISE_FORMAL")
    status = Column(String(32), nullable=False, default="ACTIVE")
    valid_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)

    __table_args__ = (
        Index("ix_capability_evidence_capability_status", "capability_id", "status"),
        UniqueConstraint("capability_id", "citation_id", name="uq_capability_evidence_capability_citation"),
    )


class MessageCitation(Base):
    __tablename__ = "message_citations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    citation_id = Column(String(26), nullable=False, unique=True, default=_new_ulid)
    message_id = Column(String(26), ForeignKey("product_messages.message_id"), nullable=False)
    kind = Column(
        Enum(CitationKind, name="message_citation_kind", native_enum=False, create_constraint=True),
        nullable=False,
        default=CitationKind.ENTERPRISE_EVIDENCE,
    )
    source_id = Column(String(64), nullable=False)
    item_id = Column(String(64), nullable=False)
    version_id = Column(String(64), nullable=False)
    yuxi_file_id = Column(String(64), nullable=False)
    chunk_id = Column(String(128), nullable=True)
    title = Column(String(512), nullable=False)
    source_url = Column(String(2048), nullable=False)
    path_text = Column(Text, nullable=True)
    locator = Column(Text, nullable=False)
    excerpt = Column(Text, nullable=False)
    source_version_at = Column(DateTime, nullable=True)
    media_type = Column(String(16), nullable=True)
    image_url = Column(String(2048), nullable=True)
    preview_url = Column(String(2048), nullable=True)
    image_alt = Column(String(512), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)

    __table_args__ = (
        Index("ix_message_citations_message_id", "message_id"),
        Index("ix_message_citations_version_id", "version_id"),
    )
