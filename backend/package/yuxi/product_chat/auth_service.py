"""Feishu OAuth and session issuance for the enterprise assistant."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.integrations.feishu import FeishuClient, FeishuClientError, FeishuNotFoundError
from yuxi.storage.postgres.models_business import Department, User
from yuxi.storage.postgres.models_product import (
    AuthorizationStatus,
    FeishuDepartmentBinding,
    FeishuUserBinding,
    FeishuUserDepartmentMembership,
)
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
_FEISHU_ROOT_DEPARTMENT_ID = "0"
_FEISHU_ROOT_DEPARTMENT_NAME = "企业根部门"


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    display_name: str
    departments: tuple[tuple[str, str], ...]


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
        directory_client: FeishuClient | None = None,
    ):
        self._db = db
        self._redis = redis_client
        self._http_client = http_client
        self._directory_client = directory_client
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
        if not open_id or not feishu_user_id or not tenant_key:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        result = await self._db.execute(select(FeishuUserBinding).where(FeishuUserBinding.feishu_open_id == open_id))
        binding = result.scalar_one_or_none()
        identity = await self._resolve_directory_identity(
            feishu_user_id=feishu_user_id,
            open_id=open_id,
            fallback_name=self._profile_string(profile, "name") or feishu_user_id,
        )
        if binding is not None:
            return await self._resolve_existing_binding(
                binding,
                profile,
                feishu_user_id,
                tenant_key,
                identity,
            )

        result = await self._db.execute(select(User).where(User.uid == feishu_user_id))
        user = result.scalar_one_or_none()
        if user is not None and user.is_deleted != 0:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        try:
            if user is None:
                user = User(
                    username=await self._available_username(identity.display_name, feishu_user_id),
                    uid=feishu_user_id,
                    avatar=self._profile_string(profile, "avatar_url"),
                    password_hash=AuthUtils.hash_password(secrets.token_urlsafe(32)),
                    role="user",
                )
                self._db.add(user)
                await self._db.flush()
            else:
                result = await self._db.execute(select(FeishuUserBinding).where(FeishuUserBinding.user_id == user.id))
                if result.scalar_one_or_none() is not None:
                    raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

            binding = FeishuUserBinding(
                user_id=user.id,
                feishu_open_id=open_id,
                feishu_user_id=feishu_user_id,
                feishu_union_id=self._profile_string(profile, "union_id"),
                tenant_key=tenant_key,
                display_name=identity.display_name,
                avatar_url=self._profile_string(profile, "avatar_url"),
                authorization_status=AuthorizationStatus.ACTIVE,
                last_login_at=utc_now_naive(),
            )
            self._db.add(binding)
            await self._sync_user(user, binding, profile, tenant_key, identity)
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
                    identity,
                )
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403) from exc
        return user

    async def _resolve_existing_binding(
        self,
        binding: FeishuUserBinding,
        profile: dict[str, Any],
        feishu_user_id: str | None,
        tenant_key: str,
        identity: _DirectoryIdentity,
    ) -> User:
        if binding.authorization_status != AuthorizationStatus.ACTIVE or binding.tenant_key != tenant_key:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)
        if binding.feishu_user_id and binding.feishu_user_id != feishu_user_id:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        result = await self._db.execute(select(User).where(User.id == binding.user_id, User.is_deleted == 0))
        user = result.scalar_one_or_none()
        if user is None:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

        binding.feishu_user_id = binding.feishu_user_id or feishu_user_id
        binding.feishu_union_id = self._profile_string(profile, "union_id")
        await self._sync_user(user, binding, profile, tenant_key, identity)
        await self._db.commit()
        return user

    async def _sync_user(
        self,
        user: User,
        binding: FeishuUserBinding,
        profile: dict[str, Any],
        tenant_key: str,
        identity: _DirectoryIdentity,
    ) -> None:
        user.username = await self._available_username(identity.display_name, user.uid, user_id=user.id)
        user.avatar = self._profile_string(profile, "avatar_url")
        user.last_login = utc_now_naive()
        binding.display_name = identity.display_name
        binding.avatar_url = user.avatar
        binding.last_login_at = utc_now_naive()
        await self._sync_departments(user, tenant_key, identity.departments)

    async def _sync_departments(
        self,
        user: User,
        tenant_key: str,
        departments: tuple[tuple[str, str], ...],
    ) -> None:
        department_bindings: list[FeishuDepartmentBinding] = []
        for feishu_department_id, display_name in departments:
            result = await self._db.execute(
                select(FeishuDepartmentBinding).where(
                    FeishuDepartmentBinding.tenant_key == tenant_key,
                    FeishuDepartmentBinding.feishu_department_id == feishu_department_id,
                )
            )
            department_binding = result.scalar_one_or_none()
            if department_binding is None:
                department = await self._resolve_local_department(
                    tenant_key,
                    feishu_department_id,
                    display_name,
                )
                department_binding = FeishuDepartmentBinding(
                    tenant_key=tenant_key,
                    feishu_department_id=feishu_department_id,
                    department_id=department.id,
                    display_name=display_name,
                )
                self._db.add(department_binding)
                await self._db.flush()
            else:
                department_binding.display_name = display_name
            department_bindings.append(department_binding)

        await self._db.execute(
            delete(FeishuUserDepartmentMembership).where(FeishuUserDepartmentMembership.user_id == user.id)
        )
        for position, department_binding in enumerate(department_bindings):
            self._db.add(
                FeishuUserDepartmentMembership(
                    user_id=user.id,
                    department_binding_id=department_binding.id,
                    position=position,
                )
            )
        primary_binding = next(
            (binding for binding in department_bindings if binding.feishu_department_id != _FEISHU_ROOT_DEPARTMENT_ID),
            department_bindings[0],
        )
        user.department_id = primary_binding.department_id

    async def _resolve_local_department(
        self,
        tenant_key: str,
        feishu_department_id: str,
        display_name: str,
    ) -> Department:
        result = await self._db.execute(select(Department).where(Department.name == display_name))
        department = result.scalar_one_or_none()
        if department is not None:
            mapped = await self._db.scalar(
                select(FeishuDepartmentBinding.id).where(FeishuDepartmentBinding.department_id == department.id)
            )
            if mapped is None:
                return department

        suffix = hashlib.sha256(f"{tenant_key}\0{feishu_department_id}".encode()).hexdigest()[:8]
        unique_name = f"{display_name[:39]} ({suffix})"
        existing = await self._db.scalar(select(Department).where(Department.name == unique_name))
        if existing is not None:
            return existing

        department = Department(name=unique_name)
        self._db.add(department)
        await self._db.flush()
        return department

    async def _available_username(
        self,
        display_name: str,
        feishu_user_id: str,
        *,
        user_id: int | None = None,
    ) -> str:
        query = select(User.id).where(User.username == display_name)
        if user_id is not None:
            query = query.where(User.id != user_id)
        if await self._db.scalar(query) is None:
            return display_name
        suffix = hashlib.sha256(feishu_user_id.encode()).hexdigest()[:8]
        return f"{display_name} ({suffix})"

    async def _resolve_directory_identity(
        self,
        *,
        feishu_user_id: str,
        open_id: str,
        fallback_name: str,
    ) -> _DirectoryIdentity:
        owns_client = self._directory_client is None
        directory_client = self._directory_client or FeishuClient()
        try:
            employee = await directory_client.get_employee(feishu_user_id)
            employee_user_id = self._profile_string(employee, "user_id")
            employee_open_id = self._profile_string(employee, "open_id")
            if employee_user_id != feishu_user_id or (employee_open_id and employee_open_id != open_id):
                raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

            status = employee.get("status")
            if not isinstance(status, dict):
                raise ProductAuthError("FEISHU_DIRECTORY_UNAVAILABLE", 503)
            blocked = any(status.get(key) is True for key in ("is_frozen", "is_resigned", "is_exited", "is_unjoin"))
            if status.get("is_activated") is not True or blocked:
                raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

            raw_department_ids = employee.get("department_ids")
            if not isinstance(raw_department_ids, list):
                raise ProductAuthError("FEISHU_DIRECTORY_UNAVAILABLE", 503)
            department_ids = tuple(
                dict.fromkeys(value.strip() for value in raw_department_ids if isinstance(value, str) and value.strip())
            )
            if not department_ids:
                raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)

            departments: list[tuple[str, str]] = []
            for department_id in department_ids:
                department = await directory_client.get_department(department_id)
                returned_id = self._profile_string(
                    department,
                    "open_department_id",
                ) or self._profile_string(department, "department_id")
                display_name = self._profile_string(department, "name")
                if returned_id and returned_id != department_id:
                    raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403)
                if not display_name and department_id == _FEISHU_ROOT_DEPARTMENT_ID:
                    display_name = _FEISHU_ROOT_DEPARTMENT_NAME
                if not display_name:
                    raise ProductAuthError("FEISHU_DIRECTORY_UNAVAILABLE", 503)
                departments.append((department_id, display_name))

            return _DirectoryIdentity(
                display_name=self._profile_string(employee, "name") or fallback_name,
                departments=tuple(departments),
            )
        except ProductAuthError:
            raise
        except FeishuNotFoundError as exc:
            raise ProductAuthError("IDENTITY_MAPPING_REQUIRED", 403) from exc
        except (FeishuClientError, ValueError) as exc:
            raise ProductAuthError("FEISHU_DIRECTORY_UNAVAILABLE", 503) from exc
        finally:
            if owns_client:
                await directory_client.aclose()

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
