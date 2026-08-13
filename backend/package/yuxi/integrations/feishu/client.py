from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from yuxi.integrations.feishu.schemas import FeishuAttachment, FeishuDownload, FeishuError, FeishuNode
from yuxi.utils import logger

FEISHU_OPEN_API_BASE_URL = "https://open.feishu.cn"
DEFAULT_CREDENTIAL_ENV_NAME = "FEISHU_ACCESS_TOKEN"
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


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


class FeishuApiError(FeishuClientError):
    pass


class FeishuClient:
    """Minimal read-only client for the Feishu Open API."""

    def __init__(
        self,
        *,
        credential_env_name: str = DEFAULT_CREDENTIAL_ENV_NAME,
        environ: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        environment = os.environ if environ is None else environ
        token = environment.get(credential_env_name)
        if not token:
            raise FeishuCredentialError(f"Missing Feishu credential environment variable: {credential_env_name}")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")

        self._token = token
        self._client = client or httpx.AsyncClient(base_url=FEISHU_OPEN_API_BASE_URL, timeout=30.0)
        self._max_retries = max_retries
        self._sleep = sleep

    async def aclose(self) -> None:
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
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token:
                return nodes

    async def list_attachments(self, folder_token: str) -> list[FeishuAttachment]:
        attachments: list[FeishuAttachment] = []
        page_token: str | None = None
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
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token:
                return attachments

    async def download(self, file_token: str) -> FeishuDownload:
        response = await self._get_response(f"/open-apis/drive/v1/files/{file_token}/download")
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
        except ValueError as exc:
            raise FeishuApiError(
                "Feishu API returned an invalid JSON response", error=self._error_from_response(response)
            ) from exc
        if not isinstance(payload, dict):
            raise FeishuApiError(
                "Feishu API returned an invalid JSON response", error=self._error_from_response(response)
            )
        if payload.get("code", 0) != 0:
            code = payload.get("code")
            raise FeishuApiError(
                "Feishu API returned an error",
                error=FeishuError(
                    status_code=response.status_code,
                    request_id=self._request_id(response),
                    code=code if isinstance(code, int) else None,
                ),
            )
        return payload

    async def _get_response(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(f"{FEISHU_OPEN_API_BASE_URL}{path}", params=params, headers=headers)
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise FeishuApiError("Feishu request failed") from exc
                await self._sleep(self._backoff_delay(attempt))
                continue

            if response.status_code in RETRY_STATUS_CODES and attempt < self._max_retries:
                self._log_response(response)
                await self._sleep(self._retry_delay(response, attempt))
                continue
            if response.status_code == 401:
                raise FeishuAuthenticationError(
                    "Feishu authentication failed", error=self._error_from_response(response)
                )
            if response.status_code == 403:
                raise FeishuPermissionError("Feishu permission denied", error=self._error_from_response(response))
            if response.is_error:
                raise FeishuApiError("Feishu request failed", error=self._error_from_response(response))
            self._log_response(response)
            return response
        raise FeishuApiError("Feishu request exhausted retries")

    @staticmethod
    def _as_mapping(value: object, field_name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise FeishuApiError(f"Feishu response had invalid {field_name}")
        return value

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
                return min(max(float(retry_after), 0.0), 60.0)
            except ValueError:
                pass
        return cls._backoff_delay(attempt)

    @classmethod
    def _log_response(cls, response: httpx.Response) -> None:
        logger.info("Feishu response: status={}, request_id={}", response.status_code, cls._request_id(response))
