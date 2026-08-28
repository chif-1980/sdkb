from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import OAuth2PasswordRequestForm

from server.routers import auth_router


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Database:
    def __init__(self, user):
        self.results = [_Result(user), _Result(None)]
        self.commit_count = 0

    async def execute(self, _statement):
        return self.results.pop(0)

    async def commit(self):
        self.commit_count += 1


def _user(*, password_matches: bool, locked: bool = False, deleted: bool = False):
    user = SimpleNamespace(
        id=7,
        password_hash="matching-hash" if password_matches else "non-matching-hash",
        is_deleted=deleted,
        login_failed_count=0,
    )
    user.is_login_locked = lambda: locked

    def increment_failed_login():
        user.login_failed_count += 1

    user.increment_failed_login = increment_failed_login
    return user


async def _login_failure(db: _Database) -> HTTPException:
    form = OAuth2PasswordRequestForm(username="candidate", password="wrong-password")
    with pytest.raises(HTTPException) as exc_info:
        await auth_router.login_for_access_token(form_data=form, db=db)
    return exc_info.value


async def test_all_login_failures_return_identical_public_response(monkeypatch: pytest.MonkeyPatch):
    async def no_op_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(auth_router, "log_operation", no_op_log)
    monkeypatch.setattr(
        auth_router.AuthUtils,
        "verify_password",
        staticmethod(lambda stored, _provided: stored == "matching-hash"),
    )

    failures = [
        await _login_failure(_Database(None)),
        await _login_failure(_Database(_user(password_matches=False))),
        await _login_failure(_Database(_user(password_matches=True, locked=True))),
        await _login_failure(_Database(_user(password_matches=True, deleted=True))),
    ]

    assert {(failure.status_code, failure.detail) for failure in failures} == {(401, "登录标识或密码错误")}
    assert {failure.headers["WWW-Authenticate"] for failure in failures} == {"Bearer"}
    assert all("X-Lock-Remaining" not in failure.headers for failure in failures)
