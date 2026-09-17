import json
import os
from types import SimpleNamespace

import pytest

from yuxi.product_chat.meeting_sources import MeetingSourceError
from yuxi.product_chat.meeting_web_reader import read_public_meeting

HTML = """<!doctype html><html><head><title>新平台项目会议</title></head><body>
<p>平台自动总结：需要继续讨论。</p>
<button onclick="document.getElementById('transcript').hidden=false;this.remove()">查看完整转写</button>
<section id="transcript" hidden>
<p>甲：暂未<b>决定</b>采购。</p><p>乙：负责人还需确认。</p><p>甲：同意继续收集需求。</p>
<p>共3段转写，全部转写已加载</p></section></body></html>"""


class FixtureSession:
    def __init__(self, *args, **kwargs):
        # The browser still uses the production network route and Chromium process.
        self.connector = kwargs["connector"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.connector.close()

    def request(self, method, url, **kwargs):
        assert url.startswith("https://meeting.example.com/")

        class Response:
            status = 200
            headers = {"content-type": "text/html; charset=utf-8"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            @property
            def content(self):
                return self

            async def iter_chunked(self, size):
                yield HTML.encode()

        return Response()


class ReaderModel:
    def __init__(self, end=True):
        self.end = end

    async def call(self, messages):
        data = json.loads(messages[-1]["content"])
        if "lines" in data:
            rows = data["lines"]
            indices = [r["index"] for r in rows if r["text"].startswith(("甲：", "乙："))]
            value = {
                "transcriptIndices": indices,
                "summaryIndices": [],
                "status": "TRANSCRIPT" if indices else "SUMMARY_ONLY",
                "endMarker": "全部转写已加载" if indices and self.end else "",
                "expectedCount": None,
            }
        else:
            value = (
                {"action": "CLICK", "control": data["controls"][0]["index"]}
                if data["controls"]
                else {"action": "SCROLL" if data["moreScroll"] else "FINISH"}
            )
        return SimpleNamespace(content=json.dumps(value, ensure_ascii=False))


@pytest.mark.parametrize("complete", [True, False])
async def test_unknown_platform_uses_real_browser_and_requires_completeness(monkeypatch, complete):
    if not os.path.isfile("/usr/bin/chromium"):
        pytest.skip("Chromium is an optional runtime for generic page tests")
    monkeypatch.setattr("aiohttp.ClientSession", FixtureSession)
    if not complete:
        with pytest.raises(MeetingSourceError) as error:
            await read_public_meeting("https://meeting.example.com/share/new", ReaderModel(end=False))
        assert error.value.code == "PARTIAL"
        return
    source = await read_public_meeting("https://meeting.example.com/share/new", ReaderModel())
    assert [p["text"] for p in source["paragraphs"]] == [
        "甲：暂未决定采购。",
        "乙：负责人还需确认。",
        "甲：同意继续收集需求。",
    ]
    assert source["completenessEvidence"] == "全部转写已加载"
    assert all(p["startMs"] is None for p in source["paragraphs"])


async def test_reader_keeps_mixed_inline_text_and_scrolls_long_static_page(monkeypatch):
    if not os.path.isfile("/usr/bin/chromium"):
        pytest.skip("Chromium is an optional runtime for generic page tests")
    monkeypatch.setattr("aiohttp.ClientSession", FixtureSession)
    monkeypatch.setattr(
        __import__(__name__, fromlist=["HTML"]),
        "HTML",
        """
    <html><body style="min-height:2800px"><div>甲：暂未<b>决定</b>采购。
    <p>乙：负责人还需确认。</p>甲：同意继续收集需求。</div>
    <p>全部转写已加载</p></body></html>""",
    )
    source = await read_public_meeting("https://meeting.example.com/long", ReaderModel())
    assert len(source["paragraphs"]) == 3
    assert source["paragraphs"][0]["text"] == "甲：暂未决定采购。"
