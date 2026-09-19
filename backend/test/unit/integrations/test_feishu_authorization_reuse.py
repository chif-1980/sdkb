"""Credential sharing must retain identity boundaries and a single refresh token."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from yuxi.integrations.feishu.user_oauth import FeishuUserOAuthService, FeishuTokenCipher, FeishuUserOAuthError
from yuxi.storage.postgres.models_knowledge import FeishuSource, FeishuUserOAuthCredential, FeishuSourceOAuthLink
from yuxi.storage.postgres.models_product import FeishuUserBinding
from yuxi.utils.datetime_utils import utc_now
from test.unit.product_chat.test_product_auth_service import db_session, _add_user  # noqa: F401
from test.unit.integrations.test_feishu_user_oauth import oauth_env

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest.mark.parametrize("case", ["valid", "other_identity", "wrong_tenant", "no_access", "existing"])
async def test_reuse_requires_current_identity_and_live_source_access(db_session, monkeypatch, case):  # noqa: F811
    user = await _add_user(db_session)
    db_session.add(
        FeishuUserBinding(
            user_id=user.id,
            feishu_open_id="ou_me",
            feishu_user_id="me",
            tenant_key="tenant-a",
            display_name="我",
            authorization_status="ACTIVE",
        )
    )
    sources = [
        FeishuSource(source_id=s, name=s, wiki_root_token=s, target_kb_id="kb", credential_env_name="unused")
        for s in ["donor", "target"]
    ]
    db_session.add_all(sources)
    await db_session.flush()
    env = oauth_env()
    cipher = FeishuTokenCipher.from_environ(env)
    credential = FeishuUserOAuthCredential(
        source_id="donor",
        access_token_ciphertext=cipher.encrypt("token"),
        refresh_token_ciphertext=cipher.encrypt("refresh"),
        access_token_expires_at=utc_now() + timedelta(hours=1),
        refresh_token_expires_at=utc_now() + timedelta(days=1),
        feishu_open_id="ou_other" if case == "other_identity" else "ou_me",
        authorization_status="active",
        display_name="我",
    )
    db_session.add(credential)
    await db_session.flush()
    service = FeishuUserOAuthService(db=db_session, environ=env)
    profile = AsyncMock(
        return_value={"open_id": "ou_me", "tenant_key": "wrong" if case == "wrong_tenant" else "tenant-a"}
    )
    validate = AsyncMock(side_effect=FeishuUserOAuthError("DENIED", 403) if case == "no_access" else None)
    monkeypatch.setattr(service, "_fetch_profile", profile)
    monkeypatch.setattr(service, "_validate_source_access", validate)
    if case == "existing":
        db_session.add(FeishuSourceOAuthLink(source_id="target", credential_id=credential.id, linked_by=str(user.id)))
        await db_session.flush()
    if case in {"wrong_tenant", "no_access"}:
        with pytest.raises(FeishuUserOAuthError):
            await service.reuse_authorization("target", user.id)
        assert await db_session.get(FeishuSourceOAuthLink, "target") is None
    elif case in {"other_identity", "existing"}:
        assert not await service.reuse_authorization("target", user.id)
        profile.assert_not_awaited()
    else:
        assert await service.reuse_authorization("target", user.id)
        validate.assert_awaited_once()
        linked = await service._get_credential("target", for_update=True)
        assert linked.id == credential.id
        assert await service.get_access_token("target") == "token"
        assert len(list(await db_session.scalars(select(FeishuUserOAuthCredential)))) == 1
        assert await service._get_credential("target", resolve_link=False) is None
        # Updates/rotation to the shared credential are observed by both sources.
        credential.access_token_ciphertext = cipher.encrypt("rotated-token")
        await db_session.flush()
        assert await service.get_access_token("target") == "rotated-token"
        # Switching the original grant's identity must revoke dependent links.
        monkeypatch.setattr(
            service,
            "_consume_state",
            AsyncMock(
                return_value={
                    "source_id": "donor",
                    "operator_id": str(user.id),
                    "redirect_uri": env["FEISHU_KNOWLEDGE_REDIRECT_URI"],
                }
            ),
        )
        monkeypatch.setattr(
            service,
            "_exchange_code",
            AsyncMock(
                return_value={
                    "access_token": "new-token",
                    "refresh_token": "new-refresh",
                    "expires_in": 7200,
                    "refresh_token_expires_in": 86400,
                }
            ),
        )
        profile.return_value = {"open_id": "ou_new_identity", "name": "另一授权人", "tenant_key": "tenant-a"}
        await service.complete_authorization(code="new-code", state="new-state")
        assert await service._get_credential("target") is None
        assert (await service._get_credential("donor")).feishu_open_id == "ou_new_identity"
