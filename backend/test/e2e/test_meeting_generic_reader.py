import os

import pytest

from test.unit.test_meeting_web_reader import FixtureSession
from yuxi.product_chat.meeting_web_reader import read_public_meeting

pytestmark = pytest.mark.e2e


async def test_generic_reader_with_configured_model(monkeypatch):
    if os.getenv("MEETING_LIVE_READER") != "1":
        pytest.skip("Optional real-model reader check requires MEETING_LIVE_READER=1")
    from yuxi.config import config
    from yuxi.models import select_model
    from yuxi.models.providers.cache import model_cache
    from yuxi.models.providers.service import get_all_model_providers
    from yuxi.storage.postgres.manager import pg_manager

    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        model_cache.rebuild(await get_all_model_providers(db))
    monkeypatch.setattr("aiohttp.ClientSession", FixtureSession)
    source = await read_public_meeting(
        "https://meeting.example.com/share/new",
        select_model(os.getenv("MEETING_TEST_MODEL") or config.default_model),
    )
    assert len(source["paragraphs"]) == 3
    assert "暂未决定采购" in source["paragraphs"][0]["text"]
    assert source["completeness"] == "COMPLETE"
    await pg_manager.close()
