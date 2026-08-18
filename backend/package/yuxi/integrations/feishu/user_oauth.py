from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.integrations.feishu.client import FeishuClient
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    FeishuProcessingEvent,
    FeishuSource,
    FeishuUserOAuthCredential,
)
from yuxi.utils.datetime_utils import ensure_utc, utc_now

FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_PROFILE_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
FEISHU_USER_OAUTH_SCOPES = "offline_access wiki:wiki:readonly docx:document:readonly drive:file:download"
STATE_TTL_SECONDS = 300
ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 120
_STATE_KEY_PREFIX = "feishu-knowledge:user-oauth-state:"
FeishuOAuthMode = Literal["redirect", "qr"]


class FeishuUserOAuthError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.status_code = status_code


class FeishuTokenCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (TypeError, ValueError) as exc:
            raise FeishuUserOAuthError(
                "FEISHU_OAUTH_ENCRYPTION_NOT_CONFIGURED",
                503,
                "飞书用户授权令牌加密密钥未正确配置",
            ) from exc

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> FeishuTokenCipher:
        environment = os.environ if environ is None else environ
        key = environment.get("FEISHU_OAUTH_TOKEN_ENCRYPTION_KEY", "").strip()
        if not key:
            jwt_secret = environment.get("JWT_SECRET_KEY", "").strip()
            if not jwt_secret:
                raise FeishuUserOAuthError(
                    "FEISHU_OAUTH_ENCRYPTION_NOT_CONFIGURED",
                    503,
                    "飞书用户授权令牌加密密钥未配置",
                )
            digest = hashlib.sha256(b"quickdone-feishu-oauth-v1\x00" + jwt_secret.encode("utf-8")).digest()
            key = urlsafe_b64encode(digest).decode("ascii")
        return cls(key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise FeishuUserOAuthError(
                "FEISHU_OAUTH_TOKEN_DECRYPT_FAILED",
                503,
                "飞书用户授权令牌无法解密，请重新授权",
            ) from exc


class FeishuUserOAuthService:
    def __init__(
        self,
        *,
        db: AsyncSession,
        redis_client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        self._db = db
        self._redis = redis_client
        self._http_client = http_client
        self._environ = environment
        self._app_id = environment.get("FEISHU_APP_ID", "").strip()
        self._app_secret = environment.get("FEISHU_APP_SECRET", "").strip()
        self._redirect_uri = environment.get("FEISHU_KNOWLEDGE_REDIRECT_URI", "").strip()
        self._qr_redirect_uri = environment.get("FEISHU_KNOWLEDGE_QR_REDIRECT_URI", "").strip()

    async def create_authorization_url(
        self,
        *,
        source_id: str,
        operator_id: str,
        mode: FeishuOAuthMode = "redirect",
    ) -> str:
        redirect_uri = self._redirect_uri_for_mode(mode)
        self._require_configuration(
            include_secret=False,
            include_encryption=False,
            redirect_uri=redirect_uri,
        )
        if self._redis is None:
            raise FeishuUserOAuthError("FEISHU_OAUTH_STATE_UNAVAILABLE", 503, "飞书授权状态服务不可用")
        source = await self._get_source(source_id)
        state = await self._create_state(
            source_id=source.source_id,
            operator_id=operator_id,
            redirect_uri=redirect_uri,
            mode=mode,
        )
        query = urlencode(
            {
                "app_id": self._app_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": FEISHU_USER_OAUTH_SCOPES,
                "state": state,
            }
        )
        return f"{FEISHU_AUTHORIZE_URL}?{query}"

    async def complete_authorization(self, *, code: str | None, state: str | None) -> FeishuUserOAuthCredential:
        state_payload = await self._consume_state(state)
        redirect_uri = str(state_payload["redirect_uri"])
        self._require_configuration(
            include_secret=True,
            include_encryption=True,
            redirect_uri=redirect_uri,
        )
        if not code:
            raise FeishuUserOAuthError("FEISHU_USER_OAUTH_FAILED", 401, "飞书未返回授权码")

        source = await self._get_source(str(state_payload["source_id"]))
        token_payload = await self._exchange_code(
            code,
            redirect_uri=redirect_uri,
        )
        access_token = self._required_token_string(token_payload, "access_token")
        refresh_token = self._required_token_string(token_payload, "refresh_token")
        access_expires_in = self._required_positive_int(token_payload, "expires_in")
        refresh_expires_in = self._required_positive_int(token_payload, "refresh_token_expires_in")

        profile = await self._fetch_profile(access_token)
        await self._validate_source_access(source, access_token)

        cipher = FeishuTokenCipher.from_environ(self._environ)
        now = utc_now()
        credential = await self._get_credential(source.source_id, for_update=True)
        if credential is None:
            credential = FeishuUserOAuthCredential(source_id=source.source_id)
            self._db.add(credential)
        credential.access_token_ciphertext = cipher.encrypt(access_token)
        credential.refresh_token_ciphertext = cipher.encrypt(refresh_token)
        credential.access_token_expires_at = now + timedelta(seconds=access_expires_in)
        credential.refresh_token_expires_at = now + timedelta(seconds=refresh_expires_in)
        credential.feishu_open_id = self._optional_string(profile, "open_id")
        credential.display_name = self._optional_string(profile, "name")
        credential.scopes = self._optional_string(token_payload, "scope")
        credential.authorization_status = "active"
        credential.authorized_by = str(state_payload["operator_id"])
        credential.last_error = None
        credential.last_refreshed_at = now
        credential.updated_at = now
        self._db.add(
            FeishuProcessingEvent(
                source_id=source.source_id,
                event_type="user_oauth_authorized",
                operator_id=str(state_payload["operator_id"]),
                message="飞书知识源已切换为用户 OAuth 授权",
                payload_json={"display_name": credential.display_name},
            )
        )
        await self._db.flush()
        return credential

    async def get_authorization_status(self, source_id: str) -> dict[str, Any]:
        await self._get_source(source_id)
        credential = await self._get_credential(source_id)
        if credential is None:
            return {"authorized": False, "status": "not_authorized"}
        refresh_expires_at = ensure_utc(credential.refresh_token_expires_at)
        status = credential.authorization_status
        if refresh_expires_at <= utc_now() and status == "active":
            status = "reauthorization_required"
        return {
            "authorized": status == "active",
            "status": status,
            "display_name": credential.display_name,
            "access_token_expires_at": ensure_utc(credential.access_token_expires_at).isoformat(),
            "refresh_token_expires_at": refresh_expires_at.isoformat(),
            "last_refreshed_at": (
                ensure_utc(credential.last_refreshed_at).isoformat()
                if credential.last_refreshed_at is not None
                else None
            ),
            "last_error": credential.last_error,
        }

    async def get_access_token(self, source_id: str, *, force_refresh: bool = False) -> str:
        credential = await self._get_credential(source_id, for_update=True)
        if credential is None:
            raise FeishuUserOAuthError("FEISHU_USER_AUTHORIZATION_REQUIRED", 424, "请先完成飞书用户授权")
        if credential.authorization_status != "active":
            raise FeishuUserOAuthError(
                "FEISHU_USER_REAUTHORIZATION_REQUIRED",
                424,
                "飞书用户授权已失效，请重新授权",
            )

        cipher = FeishuTokenCipher.from_environ(self._environ)
        now = utc_now()
        refresh_expires_at = ensure_utc(credential.refresh_token_expires_at)
        if refresh_expires_at <= now:
            credential.authorization_status = "reauthorization_required"
            credential.last_error = "refresh_token_expired"
            credential.updated_at = now
            raise FeishuUserOAuthError(
                "FEISHU_USER_REAUTHORIZATION_REQUIRED",
                424,
                "飞书用户授权已过期，请重新授权",
            )

        access_expires_at = ensure_utc(credential.access_token_expires_at)
        refresh_before = now + timedelta(seconds=ACCESS_TOKEN_REFRESH_MARGIN_SECONDS)
        if not force_refresh and access_expires_at > refresh_before:
            return cipher.decrypt(credential.access_token_ciphertext)

        refresh_token = cipher.decrypt(credential.refresh_token_ciphertext)
        try:
            token_payload = await self._refresh_tokens(refresh_token)
            access_token = self._required_token_string(token_payload, "access_token")
            access_expires_in = self._required_positive_int(token_payload, "expires_in")
            rotated_refresh_token = self._optional_string(token_payload, "refresh_token") or refresh_token
            refresh_expires_in = self._optional_positive_int(token_payload, "refresh_token_expires_in")
        except FeishuUserOAuthError:
            credential.authorization_status = "reauthorization_required"
            credential.last_error = "refresh_failed"
            credential.updated_at = now
            raise

        credential.access_token_ciphertext = cipher.encrypt(access_token)
        credential.refresh_token_ciphertext = cipher.encrypt(rotated_refresh_token)
        credential.access_token_expires_at = now + timedelta(seconds=access_expires_in)
        if refresh_expires_in is not None:
            credential.refresh_token_expires_at = now + timedelta(seconds=refresh_expires_in)
        credential.scopes = self._optional_string(token_payload, "scope") or credential.scopes
        credential.last_refreshed_at = now
        credential.last_error = None
        credential.updated_at = now
        await self._db.flush()
        return access_token

    async def _exchange_code(self, code: str, *, redirect_uri: str) -> dict[str, Any]:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            error_code="FEISHU_USER_OAUTH_FAILED",
        )

    async def _refresh_tokens(self, refresh_token: str) -> dict[str, Any]:
        self._require_configuration(include_secret=True, include_encryption=True)
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "refresh_token": refresh_token,
            },
            error_code="FEISHU_USER_TOKEN_REFRESH_FAILED",
        )

    async def _token_request(self, payload: dict[str, str], *, error_code: str) -> dict[str, Any]:
        async def send(client: httpx.AsyncClient) -> dict[str, Any]:
            try:
                response = await client.post(FEISHU_TOKEN_URL, json=payload)
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                raise FeishuUserOAuthError(error_code, 401, "飞书用户令牌请求失败") from exc
            if not isinstance(body, dict) or body.get("code", 0) != 0:
                raise FeishuUserOAuthError(error_code, 401, "飞书用户令牌请求失败")
            data = body.get("data")
            return data if isinstance(data, dict) else body

        if self._http_client is not None:
            return await send(self._http_client)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            return await send(client)

    async def _fetch_profile(self, access_token: str) -> dict[str, Any]:
        async def send(client: httpx.AsyncClient) -> dict[str, Any]:
            try:
                response = await client.get(
                    FEISHU_PROFILE_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                raise FeishuUserOAuthError("FEISHU_USER_PROFILE_FAILED", 401, "无法读取飞书授权用户信息") from exc
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(body, dict) or body.get("code") != 0 or not isinstance(data, dict):
                raise FeishuUserOAuthError("FEISHU_USER_PROFILE_FAILED", 401, "无法读取飞书授权用户信息")
            return data

        if self._http_client is not None:
            return await send(self._http_client)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            return await send(client)

    async def _validate_source_access(self, source: FeishuSource, access_token: str) -> None:
        async def token_provider(_force_refresh: bool) -> str:
            return access_token

        client = FeishuClient(environ={}, token_provider=token_provider)
        try:
            root = await client.get_node(source.wiki_root_token)
            if (getattr(source, "scan_scope", "root") or "root") == "space":
                await client.list_nodes(root.space_id)
            if root.obj_type == "docx":
                await client.get_wiki_document(root)
        except Exception as exc:
            raise FeishuUserOAuthError(
                "FEISHU_USER_SOURCE_PERMISSION_DENIED",
                424,
                "授权用户没有读取该飞书知识空间目录或文档正文的权限",
            ) from exc
        finally:
            await client.aclose()

    async def _create_state(
        self,
        *,
        source_id: str,
        operator_id: str,
        redirect_uri: str,
        mode: FeishuOAuthMode,
    ) -> str:
        for _ in range(3):
            state = secrets.token_urlsafe(32)
            state_hash = self._hash_state(state)
            payload = json.dumps(
                {
                    "state_hash": state_hash,
                    "source_id": source_id,
                    "operator_id": operator_id,
                    "redirect_uri": redirect_uri,
                    "mode": mode,
                    "expires_at": int(time.time()) + STATE_TTL_SECONDS,
                },
                separators=(",", ":"),
            )
            try:
                created = await self._redis.set(
                    self._state_key(state_hash),
                    payload,
                    ex=STATE_TTL_SECONDS,
                    nx=True,
                )
            except Exception as exc:
                raise FeishuUserOAuthError("FEISHU_OAUTH_STATE_UNAVAILABLE", 503, "飞书授权状态服务不可用") from exc
            if created:
                return state
        raise FeishuUserOAuthError("FEISHU_OAUTH_STATE_UNAVAILABLE", 503, "飞书授权状态服务不可用")

    async def _consume_state(self, state: str | None) -> dict[str, Any]:
        if not state or self._redis is None:
            raise FeishuUserOAuthError("FEISHU_OAUTH_STATE_INVALID", 401, "飞书授权状态无效或已过期")
        state_hash = self._hash_state(state)
        try:
            raw_payload = await self._redis.getdel(self._state_key(state_hash))
        except Exception as exc:
            raise FeishuUserOAuthError("FEISHU_OAUTH_STATE_UNAVAILABLE", 503, "飞书授权状态服务不可用") from exc
        if not raw_payload:
            raise FeishuUserOAuthError("FEISHU_OAUTH_STATE_INVALID", 401, "飞书授权状态无效或已过期")
        try:
            payload = json.loads(raw_payload)
            valid = (
                secrets.compare_digest(str(payload["state_hash"]), state_hash)
                and int(payload["expires_at"]) >= int(time.time())
                and bool(str(payload["source_id"]).strip())
                and bool(str(payload["operator_id"]).strip())
                and bool(str(payload["redirect_uri"]).strip())
                and payload["mode"] in {"redirect", "qr"}
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            valid = False
            payload = {}
        if not valid:
            raise FeishuUserOAuthError("FEISHU_OAUTH_STATE_INVALID", 401, "飞书授权状态无效或已过期")
        return payload

    async def _get_source(self, source_id: str) -> FeishuSource:
        result = await self._db.execute(select(FeishuSource).where(FeishuSource.source_id == source_id))
        source = result.scalar_one_or_none()
        if source is None:
            raise FeishuUserOAuthError("FEISHU_SOURCE_NOT_FOUND", 404, "未找到飞书数据源")
        return source

    async def _get_credential(
        self,
        source_id: str,
        *,
        for_update: bool = False,
    ) -> FeishuUserOAuthCredential | None:
        statement = select(FeishuUserOAuthCredential).where(FeishuUserOAuthCredential.source_id == source_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._db.execute(statement)
        return result.scalar_one_or_none()

    def _require_configuration(
        self,
        *,
        include_secret: bool,
        include_encryption: bool,
        redirect_uri: str | None = None,
    ) -> None:
        required = [self._app_id, self._redirect_uri if redirect_uri is None else redirect_uri]
        if include_secret:
            required.append(self._app_secret)
        if not all(required):
            raise FeishuUserOAuthError("FEISHU_USER_OAUTH_NOT_CONFIGURED", 503, "飞书用户授权尚未配置")
        if include_encryption:
            FeishuTokenCipher.from_environ(self._environ)

    def _redirect_uri_for_mode(self, mode: FeishuOAuthMode) -> str:
        if mode == "redirect":
            return self._redirect_uri
        if mode == "qr":
            if not self._qr_redirect_uri:
                raise FeishuUserOAuthError(
                    "FEISHU_USER_QR_OAUTH_NOT_CONFIGURED",
                    503,
                    "飞书扫码授权回调尚未配置",
                )
            return self._qr_redirect_uri
        raise FeishuUserOAuthError("FEISHU_USER_OAUTH_MODE_INVALID", 400, "不支持的飞书授权方式")

    @staticmethod
    def _required_token_string(payload: Mapping[str, Any], key: str) -> str:
        value = FeishuUserOAuthService._optional_string(payload, key)
        if not value:
            raise FeishuUserOAuthError("FEISHU_USER_OAUTH_FAILED", 401, f"飞书授权响应缺少 {key}")
        return value

    @staticmethod
    def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _required_positive_int(payload: Mapping[str, Any], key: str) -> int:
        value = FeishuUserOAuthService._optional_positive_int(payload, key)
        if value is None:
            raise FeishuUserOAuthError("FEISHU_USER_OAUTH_FAILED", 401, f"飞书授权响应缺少 {key}")
        return value

    @staticmethod
    def _optional_positive_int(payload: Mapping[str, Any], key: str) -> int | None:
        value = payload.get(key)
        if isinstance(value, bool):
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return normalized if normalized > 0 else None

    @staticmethod
    def _hash_state(state: str) -> str:
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    @staticmethod
    def _state_key(state_hash: str) -> str:
        return f"{_STATE_KEY_PREFIX}{state_hash}"


class FeishuSourceUserTokenProvider:
    def __init__(self, source_id: str) -> None:
        self._source_id = source_id

    async def __call__(self, force_refresh: bool) -> str:
        async with pg_manager.get_async_session_context() as session:
            service = FeishuUserOAuthService(db=session)
            try:
                token = await service.get_access_token(self._source_id, force_refresh=force_refresh)
            except FeishuUserOAuthError:
                await session.commit()
                raise
            await session.commit()
            return token


def create_user_authorized_feishu_client(source_id: str) -> FeishuClient:
    return FeishuClient(environ={}, token_provider=FeishuSourceUserTokenProvider(source_id))


__all__ = [
    "FeishuSourceUserTokenProvider",
    "FeishuOAuthMode",
    "FeishuTokenCipher",
    "FeishuUserOAuthError",
    "FeishuUserOAuthService",
    "create_user_authorized_feishu_client",
]
