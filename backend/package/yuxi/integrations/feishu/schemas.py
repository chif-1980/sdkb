from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FeishuNode:
    space_id: str
    node_token: str
    obj_token: str | None = None
    obj_type: str | None = None
    title: str | None = None
    parent_node_token: str | None = None
    has_child: bool = False
    revision: str | None = None
    source_updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class FeishuAttachment:
    file_token: str
    name: str
    file_type: str | None = None
    download_type: str = "file"
    size: int | None = None
    revision: str | None = None
    source_updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class FeishuPageContent:
    content: bytes
    attachments: tuple[FeishuAttachment, ...] = ()
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class FeishuDownload:
    file_token: str
    content: bytes
    content_type: str | None = None
    file_name: str | None = None


@dataclass(frozen=True, slots=True)
class FeishuError:
    status_code: int | None
    request_id: str | None
    code: int | None = None
    message: str | None = None
