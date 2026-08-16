from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

import yuxi.storage.postgres.manager as postgres_manager_module
from yuxi.storage.postgres.manager import PostgresManager


class _FailingSession:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.events: list[str] = []

    async def commit(self) -> None:
        self.events.append("commit")
        raise self.error

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def close(self) -> None:
        self.events.append("close")


@pytest.mark.asyncio
async def test_session_context_does_not_log_integrity_error_parameters(monkeypatch: pytest.MonkeyPatch):
    secret_content = "private-question-and-answer-marker"
    secret_url = "https://tenant.feishu.cn/wiki/private-url-marker"
    error = IntegrityError(
        "INSERT INTO product_messages (content, source_url) VALUES (:content, :source_url)",
        {"content": secret_content, "source_url": secret_url},
        RuntimeError("constraint failed"),
    )
    session = _FailingSession(error)
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.AsyncSession = lambda: session
    logged_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(manager, "initialize", lambda: None)
    monkeypatch.setattr(
        postgres_manager_module.logger,
        "error",
        lambda message, *args, **kwargs: logged_calls.append((message, args, kwargs)),
    )

    with pytest.raises(IntegrityError):
        async with manager.get_async_session_context():
            pass

    logged_output = repr(logged_calls)
    assert secret_content not in logged_output
    assert secret_url not in logged_output
    assert session.events == ["commit", "rollback", "close"]
