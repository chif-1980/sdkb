"""Feishu OAuth and session issuance for the enterprise assistant."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_product import AuthorizationStatus, FeishuUserBinding
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import utc_now_naive

COOKIE_NAME = "enterprise_assistant_session"
STATE_TTL_SECONDS = 300
SESSION_TTL_SECONDS = 8 * 60 * 60

FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_QR_AUTHORIZE_URL = "https://passport.feishu.cn/suite/passport/oauth/authorize"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_PROFILE_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
_STATE_KEY_PREFIX = "enterprise-assistant:oauth-state:"
_DEFAULT_RETURN_PATH = "/chat"


class ProductAuthError(Exception):
    """Stable product-auth failure safe to expose as an error code."""

    def __init__(self, code: str, status_code: int, message: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.status_code = status_code


class ProductAuthService:
    def __init__(
        self,
        *,
        db: AsyncSession,
        redis_client: Any | None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._db = db
        self._redis = redis_client
        self._http_client = http_client
        self._app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        self._app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        self._redirect_uri = os.environ.get("FEISHU_PRODUCT_REDIRECT_URI", "").strip()

    async def create_login_url(self, return_path: str = _DEFAULT_RETURN_PATH) -> str:
        self._require_configuration(include_secret=False)
        state = await self._create_state(return_path)
        query = urlencode(
            {
                "app_id": self._app_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "state": state,
            }
        )
        return f"{FEISHU_AUTHORIZE_URL}?{query}"

    async def create_qr_login_url(self, return_path: str = _DEFAULT_RETURN_PATH) -> str:
        self._require_configuration(include_secret=False)
        state = await self._create_state(return_path)
        query = urlencode(
            {
                "client_id": self._app_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "state": state,
            }
        )
        return f"{FEISHU_QR_AUTHORIZE_URL}?{query}"

    async def complete_callback(self, code: str | None, state: str | None) -> tuple[User, str]:
        await self._consume_state(state)
        self._require_configuration(include_secret=True)
        if not code:
            raise ProductAuthError("FEISHU_OAUTH_FAILED", 401)

        profile = await self._fetch_profile(code)
        user = await self.resolve_bound_user(profile)
        token = AuthUtils.create_access_token(
            {"sub": str(user.id), "token_kind": "enterprise_assistant"},
            expires_delta=timedelta(seconds=SESSION_TTL_SECONDS),
        )
        return user, token

    async def resolve_bound_user(self, profile: dict[str, Any]) -> User:
        open_id = self._profile_string(profile, "open_id")
        feishu_user_id = self._profile_string(profile, "user_id")
        tenant_key = self._profile_string(profile, "tenant_key")
        if not open_id or not tenant_key:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        result = await self._db.execute(
            select(FeishuUserBinding).where(FeishuUserBinding.feishu_open_id == open_id)
        )
        binding = result.scalar_one_or_none()
        if binding is not None:
            return await self._resolve_existing_binding(binding, profile, feishu_user_id, tenant_key)

        if not feishu_user_id:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        result = await self._db.execute(
            select(User).where(User.uid == feishu_user_id, User.is_deleted == 0)
        )
        user = result.scalar_one_or_none()
        if user is None or user.department_id is None:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        result = await self._db.execute(
            select(FeishuUserBinding).where(FeishuUserBinding.user_id == user.id)
        )
        if result.scalar_one_or_none() is not None:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        binding = FeishuUserBinding(
            user_id=user.id,
            feishu_open_id=open_id,
            feishu_user_id=feishu_user_id,
            feishu_union_id=self._profile_string(profile, "union_id"),
            tenant_key=tenant_key,
            display_name=self._profile_string(profile, "name") or user.username,
            avatar_url=self._profile_string(profile, "avatar_url"),
            authorization_status=AuthorizationStatus.ACTIVE,
            last_login_at=utc_now_naive(),
        )
        self._db.add(binding)
        user.last_login = utc_now_naive()
        try:
            await self._db.commit()
        except IntegrityError as exc:
            await self._db.rollback()
            result = await self._db.execute(
                select(FeishuUserBinding).where(FeishuUserBinding.feishu_open_id == open_id)
            )
            concurrent_binding = result.scalar_one_or_none()
            if concurrent_binding is not None:
                return await self._resolve_existing_binding(
                    concurrent_binding,
                    profile,
                    feishu_user_id,
                    tenant_key,
                )
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403) from exc
        return user

    async def _resolve_existing_binding(
        self,
        binding: FeishuUserBinding,
        profile: dict[str, Any],
        feishu_user_id: str | None,
        tenant_key: str,
    ) -> User:
        if binding.authorization_status != AuthorizationStatus.ACTIVE or binding.tenant_key != tenant_key:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)
        if binding.feishu_user_id and binding.feishu_user_id != feishu_user_id:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        result = await self._db.execute(
            select(User).where(User.id == binding.user_id, User.is_deleted == 0)
        )
        user = result.scalar_one_or_none()
        if user is None or user.department_id is None:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        binding.feishu_user_id = binding.feishu_user_id or feishu_user_id
        binding.feishu_union_id = self._profile_string(profile, "union_id")
        binding.display_name = self._profile_string(profile, "name") or binding.display_name
        binding.avatar_url = self._profile_string(profile, "avatar_url")
        binding.last_login_at = utc_now_naive()
        user.last_login = utc_now_naive()
        await self._db.commit()
        return user

    async def _consume_state(self, state: str | None) -> dict[str, Any]:
        if not state:
            raise ProductAuthError("FEISHU_OAUTH_STATE_INVALID", 401)

        state_hash = self._hash_state(state)
        try:
            raw_payload = await self._redis.getdel(self._state_key(state_hash))
        except Exception as exc:
            raise ProductAuthError("AUTH_SERVICE_UNAVAILABLE", 503) from exc
        if not raw_payload:
            raise ProductAuthError("FEISHU_OAUTH_STATE_INVALID", 401)

        try:
            payload = json.loads(raw_payload)
            is_valid = (
                secrets.compare_digest(str(payload["state_hash"]), state_hash)
                and int(payload["expires_at"]) >= int(time.time())
                and payload["return_path"] == self._normalize_return_path(payload["return_path"])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            is_valid = False
        if not is_valid:
            raise ProductAuthError("FEISHU_OAUTH_STATE_INVALID", 401)
        return payload

    async def _create_state(self, return_path: str) -> str:
        normalized_return_path = self._normalize_return_path(return_path)

        for _ in range(3):
            state = secrets.token_urlsafe(32)
            state_hash = self._hash_state(state)
            payload = json.dumps(
                {
                    "state_hash": state_hash,
                    "return_path": normalized_return_path,
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
                raise ProductAuthError("AUTH_SERVICE_UNAVAILABLE", 503) from exc
            if created:
                return state

        raise ProductAuthError("AUTH_SERVICE_UNAVAILABLE", 503)

    async def _fetch_profile(self, code: str) -> dict[str, Any]:
        if self._http_client is not None:
            return await self._fetch_profile_with_client(self._http_client, code)
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            return await self._fetch_profile_with_client(client, code)

    async def _fetch_profile_with_client(self, client: httpx.AsyncClient, code: str) -> dict[str, Any]:
        try:
            token_response = await client.post(
                FEISHU_TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": self._app_id,
                    "client_secret": self._app_secret,
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                },
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            if token_payload.get("code") != 0:
                raise ValueError("Feishu token exchange failed")
            access_token = token_payload.get("access_token")
            if not access_token and isinstance(token_payload.get("data"), dict):
                access_token = token_payload["data"].get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("Feishu access token missing")

            profile_response = await client.get(
                FEISHU_PROFILE_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            profile_response.raise_for_status()
            profile_payload = profile_response.json()
            if profile_payload.get("code") != 0 or not isinstance(profile_payload.get("data"), dict):
                raise ValueError("Feishu profile request failed")
            return profile_payload["data"]
        except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProductAuthError("FEISHU_OAUTH_FAILED", 401) from exc

    def _require_configuration(self, *, include_secret: bool) -> None:
        values = [self._app_id, self._redirect_uri]
        if include_secret:
            values.append(self._app_secret)
        if not all(values):
            raise ProductAuthError("FEISHU_OAUTH_NOT_CONFIGURED", 503)

    @staticmethod
    def _normalize_return_path(return_path: str | None) -> str:
        return _DEFAULT_RETURN_PATH if return_path != _DEFAULT_RETURN_PATH else return_path

    @staticmethod
    def _hash_state(state: str) -> str:
        return hashlib.sha256(state.encode()).hexdigest()

    @staticmethod
    def _state_key(state_hash: str) -> str:
        return f"{_STATE_KEY_PREFIX}{state_hash}"

    @staticmethod
    def _profile_string(profile: dict[str, Any], key: str) -> str | None:
        value = profile.get(key)
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None
