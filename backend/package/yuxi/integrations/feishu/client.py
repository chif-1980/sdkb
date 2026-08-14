from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from yuxi.integrations.feishu.schemas import (
    FeishuAttachment,
    FeishuDownload,
    FeishuError,
    FeishuNode,
    FeishuPageContent,
)
from yuxi.utils import logger

FEISHU_OPEN_API_BASE_URL = "https://open.feishu.cn"
FEISHU_TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
DEFAULT_APP_ID_ENV_NAME = "FEISHU_APP_ID"
DEFAULT_APP_SECRET_ENV_NAME = "FEISHU_APP_SECRET"


class FeishuClientError(RuntimeError):
    def __init__(self, message: str, *, error: FeishuError | None = None) -> None:
        super().__init__(message)
        self.error = error

    @property
    def status_code(self) -> int | None:
        return self.error.status_code if self.error else None

    @property
    def request_id(self) -> str | None:
        return self.error.request_id if self.error else None


class FeishuCredentialError(FeishuClientError):
    pass


class FeishuAuthenticationError(FeishuClientError):
    pass


class FeishuPermissionError(FeishuClientError):
    pass


class FeishuNotFoundError(FeishuClientError):
    pass


class FeishuApiError(FeishuClientError):
    pass


class FeishuClient:
    """Minimal read-only client for the Feishu Open API."""

    def __init__(
        self,
        *,
        app_id_env_name: str = DEFAULT_APP_ID_ENV_NAME,
        app_secret_env_name: str = DEFAULT_APP_SECRET_ENV_NAME,
        environ: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        environment = os.environ if environ is None else environ
        app_id = environment.get(app_id_env_name)
        if not app_id or not app_id.strip():
            raise FeishuCredentialError(f"Missing Feishu credential environment variable: {app_id_env_name}")
        app_secret = environment.get(app_secret_env_name)
        if not app_secret or not app_secret.strip():
            raise FeishuCredentialError(f"Missing Feishu credential environment variable: {app_secret_env_name}")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")

        self._app_id = app_id
        self._app_secret = app_secret
        self._tenant_token: str | None = None
        self._token_generation = 0
        self._token_refresh_at = 0.0
        self._token_lock = asyncio.Lock()
        self._monotonic = monotonic
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=FEISHU_OPEN_API_BASE_URL, timeout=30.0)
        self._max_retries = max_retries
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_node(self, node_token: str) -> FeishuNode:
        payload = await self._get("/open-apis/wiki/v2/spaces/get_node", params={"token": node_token})
        data = self._as_mapping(payload.get("data"), "data")
        node = self._as_mapping(data.get("node"), "node")
        return self._node_from_payload(node)

    async def list_children(self, parent_node_token: str) -> list[FeishuNode]:
        parent = await self.get_node(parent_node_token)
        if not parent.space_id:
            raise FeishuApiError("Feishu node response did not include a space ID")

        nodes: list[FeishuNode] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        while True:
            params: dict[str, str] = {"parent_node_token": parent_node_token}
            if page_token:
                params["page_token"] = page_token
            payload = await self._get(f"/open-apis/wiki/v2/spaces/{parent.space_id}/nodes", params=params)
            data = self._as_mapping(payload.get("data"), "data")
            items = data.get("items") or []
            if not isinstance(items, list):
                raise FeishuApiError("Feishu node list response had invalid items")
            nodes.extend(self._node_from_payload(self._as_mapping(item, "node item")) for item in items)
            if not data.get("has_more"):
                return nodes
            page_token = self._next_page_token(data, seen_page_tokens)

    async def list_attachments(self, folder_token: str) -> list[FeishuAttachment]:
        attachments: list[FeishuAttachment] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        while True:
            params: dict[str, str] = {"folder_token": folder_token}
            if page_token:
                params["page_token"] = page_token
            payload = await self._get("/open-apis/drive/v1/files", params=params)
            data = self._as_mapping(payload.get("data"), "data")
            files = data.get("files") or []
            if not isinstance(files, list):
                raise FeishuApiError("Feishu attachment list response had invalid files")
            attachments.extend(self._attachment_from_payload(self._as_mapping(item, "file item")) for item in files)
            if not data.get("has_more"):
                return attachments
            page_token = self._next_page_token(data, seen_page_tokens)

    async def get_wiki_document(self, node: FeishuNode) -> FeishuPageContent:
        if node.obj_type != "docx":
            raise FeishuApiError(f"Unsupported Feishu Wiki obj_type: {node.obj_type!r}")
        document_id = node.obj_token or node.node_token
        document_payload = await self._get(f"/open-apis/docx/v1/documents/{document_id}", params={})
        document_data = self._as_mapping(document_payload.get("data"), "data")
        document = self._as_mapping(document_data.get("document"), "document")
        revision_id = document.get("revision_id")
        if not isinstance(revision_id, int):
            raise FeishuApiError("Feishu document response did not include a numeric revision ID")
        revision = str(revision_id)
        content_payload = await self._get(
            f"/open-apis/docx/v1/documents/{document_id}/raw_content",
            params={},
        )
        content_data = self._as_mapping(content_payload.get("data"), "data")
        content = content_data.get("content")
        if not isinstance(content, str):
            raise FeishuApiError("Feishu document response did not include string content")

        blocks = await self._list_document_blocks(document_id, revision)
        attachments: list[FeishuAttachment] = []
        seen_tokens: set[str] = set()
        visited_blocks: set[str] = set()
        for block in blocks:
            self._append_block_attachments(block, attachments, seen_tokens)
        for block in blocks:
            await self._collect_block_attachments(
                document_id=document_id,
                revision=revision,
                block=block,
                attachments=attachments,
                seen_tokens=seen_tokens,
                visited_blocks=visited_blocks,
            )
        return FeishuPageContent(content=content.encode("utf-8"), attachments=tuple(attachments), revision=revision)

    async def _list_document_blocks(self, document_id: str, revision: str | None) -> list[Mapping[str, Any]]:
        return await self._list_blocks(f"/open-apis/docx/v1/documents/{document_id}/blocks", revision=revision)

    async def _list_block_children(
        self,
        document_id: str,
        block_id: str,
        revision: str,
    ) -> list[Mapping[str, Any]]:
        return await self._list_blocks(
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children",
            revision=revision,
        )

    async def _list_blocks(self, path: str, *, revision: str | None = None) -> list[Mapping[str, Any]]:
        blocks: list[Mapping[str, Any]] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        while True:
            params: dict[str, str] = {"page_size": "100"}
            if page_token:
                params["page_token"] = page_token
            if revision:
                params["document_revision_id"] = revision
            payload = await self._get(path, params=params)
            data = self._as_mapping(payload.get("data"), "data")
            items = data.get("items") or []
            if not isinstance(items, list):
                raise FeishuApiError("Feishu document block response had invalid items")
            blocks.extend(self._as_mapping(item, "block item") for item in items)
            if not data.get("has_more"):
                return blocks
            page_token = self._next_page_token(data, seen_page_tokens)

    async def _collect_block_attachments(
        self,
        *,
        document_id: str,
        revision: str,
        block: Mapping[str, Any],
        attachments: list[FeishuAttachment],
        seen_tokens: set[str],
        visited_blocks: set[str],
    ) -> None:
        block_id = block.get("block_id")
        if isinstance(block_id, str) and block_id:
            if block_id in visited_blocks:
                return
            visited_blocks.add(block_id)
        children = block.get("children")
        if not isinstance(block_id, str) or not block_id or not isinstance(children, list) or not children:
            return
        for child in await self._list_block_children(document_id, block_id, revision):
            self._append_block_attachments(child, attachments, seen_tokens)
            await self._collect_block_attachments(
                document_id=document_id,
                revision=revision,
                block=child,
                attachments=attachments,
                seen_tokens=seen_tokens,
                visited_blocks=visited_blocks,
            )

    @classmethod
    def _append_block_attachments(
        cls,
        block: Mapping[str, Any],
        attachments: list[FeishuAttachment],
        seen_tokens: set[str],
    ) -> None:
        file_block = block.get("file")
        if isinstance(file_block, Mapping):
            token = file_block.get("token") or file_block.get("file_token")
            if isinstance(token, str) and token and token not in seen_tokens:
                seen_tokens.add(token)
                name = file_block.get("name")
                attachments.append(
                    FeishuAttachment(
                        file_token=token,
                        name=name if isinstance(name, str) and name else f"file-{token}",
                        file_type="file",
                        download_type="media",
                    )
                )
        image_block = block.get("image")
        if isinstance(image_block, Mapping):
            token = image_block.get("token") or image_block.get("file_token")
            if isinstance(token, str) and token and token not in seen_tokens:
                seen_tokens.add(token)
                attachments.append(
                    FeishuAttachment(
                        file_token=token,
                        name=f"image-{token}",
                        file_type="image",
                        download_type="media",
                    )
                )
        text_block = block.get("text")
        if not isinstance(text_block, Mapping):
            return
        elements = text_block.get("elements")
        if not isinstance(elements, list):
            return
        for element in elements:
            if not isinstance(element, Mapping):
                continue
            inline_file = element.get("file")
            if not isinstance(inline_file, Mapping):
                continue
            token = inline_file.get("file_token") or inline_file.get("token")
            if isinstance(token, str) and token and token not in seen_tokens:
                seen_tokens.add(token)
                attachments.append(
                    FeishuAttachment(
                        file_token=token,
                        name=f"image-{token}",
                        file_type="image",
                        download_type="media",
                    )
                )

    async def download(self, file_token: str, *, download_type: str = "file") -> FeishuDownload:
        if download_type not in {"file", "media"}:
            raise ValueError("download_type must be 'file' or 'media'")
        response = await self._get_response(f"/open-apis/drive/v1/{download_type}s/{file_token}/download")
        return FeishuDownload(
            file_token=file_token,
            content=response.content,
            content_type=response.headers.get("content-type"),
            file_name=self._download_filename(response.headers.get("content-disposition")),
        )

    async def _get(self, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        response = await self._get_response(path, params=params)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise FeishuApiError(
                "Feishu API returned an invalid JSON response", error=self._error_from_response(response)
            )
        if payload.get("code", 0) != 0:
            code = payload.get("code")
            message = payload.get("msg") or payload.get("message")
            error_message = message if isinstance(message, str) and message else "Feishu API returned an error"
            raise FeishuApiError(
                error_message,
                error=FeishuError(
                    status_code=response.status_code,
                    request_id=self._request_id(response),
                    code=code if isinstance(code, int) else None,
                    message=error_message,
                ),
            )
        return payload

    async def _get_response(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        token, generation = await self._get_tenant_token()
        response = await self._get_with_retries(path, params=params, token=token)
        if response.status_code == 401:
            self._invalidate_tenant_token(token, generation)
            token, _ = await self._get_tenant_token()
            response = await self._get_with_retries(path, params=params, token=token)
            if response.status_code == 401:
                raise FeishuAuthenticationError(
                    "Feishu authentication failed", error=self._error_from_response(response)
                )
        if response.status_code == 403:
            raise FeishuPermissionError("Feishu permission denied", error=self._error_from_response(response))
        if response.status_code == 404:
            raise FeishuNotFoundError("Feishu resource not found", error=self._error_from_response(response))
        if response.is_error:
            raise FeishuApiError("Feishu request failed", error=self._error_from_response(response))
        self._log_response(response)
        return response

    async def _get_with_retries(self, path: str, *, params: dict[str, str] | None, token: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(self._max_retries + 1):
            request_failed = False
            try:
                response = await self._client.get(f"{FEISHU_OPEN_API_BASE_URL}{path}", params=params, headers=headers)
            except httpx.HTTPError:
                if attempt >= self._max_retries:
                    request_failed = True
                else:
                    await self._sleep(self._backoff_delay(attempt))
                    continue
            if request_failed:
                raise FeishuApiError("Feishu request failed")

            if (response.status_code == 429 or 500 <= response.status_code < 600) and attempt < self._max_retries:
                self._log_response(response)
                await self._sleep(self._retry_delay(response, attempt))
                continue
            return response
        raise FeishuApiError("Feishu request exhausted retries")

    async def _get_tenant_token(self) -> tuple[str, int]:
        if self._token_is_fresh():
            assert self._tenant_token is not None
            return self._tenant_token, self._token_generation
        async with self._token_lock:
            if not self._token_is_fresh():
                await self._exchange_tenant_token()
        assert self._tenant_token is not None
        return self._tenant_token, self._token_generation

    def _token_is_fresh(self) -> bool:
        return self._tenant_token is not None and self._monotonic() < self._token_refresh_at

    async def _exchange_tenant_token(self) -> None:
        response = await self._auth_response()
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise FeishuAuthenticationError(
                "Feishu authentication returned an invalid response", error=self._error_from_response(response)
            )
        if type(payload.get("code")) is not int or payload["code"] != 0:
            raise FeishuAuthenticationError("Feishu authentication failed", error=self._error_from_response(response))
        token = payload.get("tenant_access_token")
        expire = payload.get("expire")
        if not isinstance(token, str) or not token.strip():
            raise FeishuAuthenticationError(
                "Feishu authentication returned an invalid response", error=self._error_from_response(response)
            )
        normalized_expire: float | None = None
        if not isinstance(expire, bool) and isinstance(expire, (int, float)):
            try:
                normalized_expire = float(expire)
            except (TypeError, ValueError, OverflowError):
                pass
        if normalized_expire is None or not math.isfinite(normalized_expire) or normalized_expire <= 0:
            raise FeishuAuthenticationError(
                "Feishu authentication returned an invalid response", error=self._error_from_response(response)
            )
        refresh_margin = 300.0 if normalized_expire >= 600 else normalized_expire / 2
        self._tenant_token = token
        self._token_generation += 1
        self._token_refresh_at = self._monotonic() + normalized_expire - refresh_margin

    async def _auth_response(self) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            request_failed = False
            try:
                request = self._client.build_request(
                    "POST",
                    f"{FEISHU_OPEN_API_BASE_URL}{FEISHU_TENANT_TOKEN_PATH}",
                    json={"app_id": self._app_id, "app_secret": self._app_secret},
                )
                request.headers.pop("Authorization", None)
                response = await self._client.send(request, auth=None)
            except httpx.HTTPError:
                if attempt >= self._max_retries:
                    request_failed = True
                else:
                    await self._sleep(self._backoff_delay(attempt))
                    continue
            if request_failed:
                raise FeishuApiError("Feishu authentication request failed")

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt >= self._max_retries:
                    raise FeishuApiError(
                        "Feishu authentication request failed", error=self._error_from_response(response)
                    )
                self._log_response(response)
                await self._sleep(self._retry_delay(response, attempt))
                continue
            if response.is_error:
                raise FeishuAuthenticationError(
                    "Feishu authentication failed", error=self._error_from_response(response)
                )
            return response
        raise FeishuApiError("Feishu authentication request exhausted retries")

    def _invalidate_tenant_token(self, token: str, generation: int) -> None:
        if self._tenant_token == token and self._token_generation == generation:
            self._tenant_token = None
            self._token_refresh_at = 0.0

    @staticmethod
    def _as_mapping(value: object, field_name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise FeishuApiError(f"Feishu response had invalid {field_name}")
        return value

    @staticmethod
    def _next_page_token(data: Mapping[str, Any], seen_page_tokens: set[str]) -> str:
        page_token = data.get("page_token")
        if not isinstance(page_token, str) or not page_token:
            raise FeishuApiError("Feishu paginated response was missing a page token")
        if page_token in seen_page_tokens:
            raise FeishuApiError("Feishu paginated response reused a repeated page token")
        seen_page_tokens.add(page_token)
        return page_token

    @staticmethod
    def _node_from_payload(value: Mapping[str, Any]) -> FeishuNode:
        node_token = value.get("node_token")
        if not isinstance(node_token, str) or not node_token:
            raise FeishuApiError("Feishu node response did not include a node token")
        return FeishuNode(
            space_id=str(value.get("space_id") or ""),
            node_token=node_token,
            obj_token=FeishuClient._optional_text(value.get("obj_token")),
            obj_type=FeishuClient._optional_text(value.get("obj_type")),
            title=FeishuClient._optional_text(value.get("title")),
            parent_node_token=FeishuClient._optional_text(value.get("parent_node_token")),
            has_child=bool(value.get("has_child", False)),
            revision=FeishuClient._optional_text(value.get("revision")),
            source_updated_at=FeishuClient._optional_text(value.get("obj_edit_time")),
        )

    @staticmethod
    def _attachment_from_payload(value: Mapping[str, Any]) -> FeishuAttachment:
        file_token = value.get("token") or value.get("file_token")
        name = value.get("name")
        if not isinstance(file_token, str) or not file_token or not isinstance(name, str):
            raise FeishuApiError("Feishu attachment response was incomplete")
        size = value.get("size")
        try:
            parsed_size = int(size) if size is not None else None
        except (TypeError, ValueError):
            parsed_size = None
        return FeishuAttachment(
            file_token=file_token,
            name=name,
            file_type=FeishuClient._optional_text(value.get("type")),
            size=parsed_size,
            revision=FeishuClient._optional_text(value.get("revision")),
            source_updated_at=FeishuClient._optional_text(value.get("modified_time")),
        )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _download_filename(content_disposition: str | None) -> str | None:
        if not content_disposition:
            return None
        for part in content_disposition.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() == "filename":
                return value.strip('"')
        return None

    @staticmethod
    def _request_id(response: httpx.Response) -> str | None:
        return response.headers.get("x-tt-logid") or response.headers.get("x-request-id")

    @classmethod
    def _error_from_response(cls, response: httpx.Response) -> FeishuError:
        return FeishuError(status_code=response.status_code, request_id=cls._request_id(response))

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        return float(min(2**attempt, 8))

    @classmethod
    def _retry_delay(cls, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                retry_delay = float(retry_after)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if math.isfinite(retry_delay):
                    return min(max(retry_delay, 0.0), 60.0)
        return cls._backoff_delay(attempt)

    @classmethod
    def _log_response(cls, response: httpx.Response) -> None:
        logger.info("Feishu response: status={}, request_id={}", response.status_code, cls._request_id(response))
