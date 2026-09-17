"""Opt-in full-source acceptance, through the deployed HTTP API and actual worker/model."""

import os

import pytest

from test.integration.api.test_meeting_lifecycle import meeting_clients, submit, wait_terminal  # noqa: F401

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


@pytest.mark.parametrize(
    "platform,url,count,last_end",
    [
        ("通义听悟", "https://tingwu.aliyun.com/doc/transcripts/p7g395w7m55w9z65?sl=1", 388, 10050246),
        ("BuddyNote", "https://bncloud.ieasetek.com/share/ZPmqLtXMyiy", 1280, 5608010),
    ],
)
async def test_real_long_meeting_full_coverage(meeting_clients, platform, url, count, last_end):  # noqa: F811
    if os.getenv("MEETING_LIVE_SOURCES") != "1":
        pytest.skip("Set MEETING_LIVE_SOURCES=1 to call the real meeting platforms and configured model")
    owner, _ = meeting_clients
    cid, mid = await submit(owner, "@会议纪要 " + url)
    record = await wait_terminal(owner, mid, seconds=1800)
    assert record["state"] == "completed", record.get("error")
    source = record["sources"][0]
    assert source["platform"] == platform
    assert len(source["paragraphs"]) == count
    assert source["paragraphs"][-1]["endMs"] == last_end
    coverage = record["result"]["coverage"]
    assert coverage["paragraphs"] == count
    assert coverage["processed"] == coverage["total"] and coverage["total"] > 1
    assert record["result"]["selectedHistoryIds"] == []
    assert (await owner.get(f"/api/chat/meetings/{mid}/export?version={record['version']}")).status_code == 200
    restored = (await owner.get(f"/api/chat/conversations/{cid}")).json()["messages"]
    assert restored[-1]["meeting"]["result"]["body"] == record["result"]["body"]
