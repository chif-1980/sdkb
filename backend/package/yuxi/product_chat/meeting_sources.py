"""Public meeting adapters. Only transcript endpoints, never browser credentials."""

from __future__ import annotations

import json
import re
import socket
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver

MAX_SOURCE_BYTES = 20 * 1024 * 1024


class MeetingSourceError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.port not in (None, 80, 443)
        or parsed.username
        or parsed.password
    ):
        raise MeetingSourceError("UNSAFE_ADDRESS", "请提供公开网页链接，不支持凭据链接或非网页端口。")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
        raise MeetingSourceError("UNSAFE_ADDRESS", "会议链接不能指向内部地址。")
    try:
        address = ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise MeetingSourceError("UNSAFE_ADDRESS", "会议链接不能指向内部地址。")
    return url


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await super().resolve(host, port, family)
        if not addresses or any(not ip_address(item["host"]).is_global for item in addresses):
            raise MeetingSourceError("UNSAFE_ADDRESS", "会议链接指向内部地址，无法读取。")
        # These checked addresses are the addresses used by the connector.
        return addresses


async def fetch_json(url: str, body: dict | None = None) -> dict:
    validate_url(url)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicResolver()),
        timeout=aiohttp.ClientTimeout(total=45),
        trust_env=False,
    ) as client:
        for _ in range(4):
            async with client.request("POST" if body else "GET", url, json=body, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = validate_url(urljoin(url, response.headers.get("Location", "")))
                    if response.status == 303:
                        body = None
                    continue
                if response.status in (401, 403):
                    raise MeetingSourceError("ACCESS_REQUIRED", "分享需要登录或访问授权，请更换公开链接或上传文件。")
                if response.status in (404, 410):
                    raise MeetingSourceError("EXPIRED", "分享链接已失效或资料已删除，请更换链接。")
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_SOURCE_BYTES:
                        raise MeetingSourceError("TOO_LARGE", "会议正文超过读取上限，请拆分后上传。")
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError) as exc:
                    raise MeetingSourceError(
                        "PARSE_FAILED", "平台没有返回可读取的正文，请上传文件或粘贴文字。"
                    ) from exc
                if not isinstance(payload, dict):
                    raise MeetingSourceError("PARSE_FAILED", "平台返回格式已变化，请上传文件。")
                return payload
    raise MeetingSourceError("REDIRECT_LIMIT", "分享链接重定向异常，请上传文件。")


def paragraph(text: str, index: int, speaker: str = "未提供", start=None, end=None) -> dict:
    return {"id": f"P{index}", "text": text, "speaker": speaker or "未提供", "startMs": start, "endMs": end}


def parse_buddy(payload: dict, url: str) -> dict:
    if not payload.get("success"):
        raise MeetingSourceError("ACCESS_REQUIRED", "BuddyNote 分享已失效、需要提取码或访问授权，请上传转写文件。")
    data = payload.get("result") or {}
    rows = data.get("messageList") or []
    segments = [
        paragraph(
            str(row.get("message") or "").strip(), i + 1, row.get("speaker"), row.get("startTime"), row.get("endTime")
        )
        for i, row in enumerate(rows)
    ]
    return finalize_source("BuddyNote", data.get("fileName"), url, segments, data.get("summary") or "", len(rows))


def parse_tingwu(payload: dict, url: str) -> dict:
    if str(payload.get("code")) != "0":
        code = str(payload.get("code") or "")
        expired = any(word in code for word in ("InvalidTrans", "Deleted", "Closed", "InvalidShare"))
        raise MeetingSourceError(
            "EXPIRED" if expired else "ACCESS_REQUIRED", "听悟分享失效或需要访问授权，请更换公开链接或上传转写文件。"
        )
    data = payload.get("data") or {}
    try:
        result = json.loads(data["result"]) if isinstance(data.get("result"), str) else data.get("result", {})
        rows = result.get("pg", [])
        identify = json.loads((data.get("tag") or {}).get("identify") or "{}")
    except (ValueError, TypeError) as exc:
        raise MeetingSourceError("PARSE_FAILED", "听悟转写格式无法解析，请上传转写文件。") from exc
    segments = []
    for i, row in enumerate(rows):
        words = row.get("sc") or []
        speaker_id = (identify.get("user_map_info") or {}).get(str(row.get("ui")))
        speaker = (identify.get("user_info", {}).get(speaker_id) or {}).get("name") or "未提供"
        segments.append(
            paragraph(
                "".join(str(word.get("tc") or "") for word in words),
                i + 1,
                speaker,
                min((word["bt"] for word in words if "bt" in word), default=None),
                max((word["et"] for word in words if "et" in word), default=None),
            )
        )
    source = finalize_source("通义听悟", (data.get("tag") or {}).get("showName"), url, segments, "", len(rows))
    duration = float(data.get("duration") or 0) * 1000
    last = max((p["endMs"] or 0 for p in source["paragraphs"]), default=0)
    if any(row.get("partial") for row in rows) or (duration and last < duration - max(60000, duration * 0.03)):
        raise MeetingSourceError("PARTIAL", "听悟仅返回了部分转写，无法作为完整会议分析，请上传完整转写。")
    source["durationMs"] = duration or None
    return source


def finalize_source(platform, title, url, segments, summary, expected) -> dict:
    if not segments or not any(p["text"].strip() for p in segments):
        raise MeetingSourceError(
            "SUMMARY_ONLY" if summary else "EMPTY",
            "仅有平台总结，没有原始转写。请上传完整正文。" if summary else "未读取到会议正文，请上传文件或粘贴文字。",
        )
    if any(not p["text"].strip() for p in segments) or len(segments) != expected:
        raise MeetingSourceError("PARTIAL", "部分转写段落为空，无法确认全文完整，请上传完整转写。")
    return {
        "platform": platform,
        "title": title or "未提供",
        "url": url,
        "paragraphs": segments,
        "platformSummary": summary,
        "completeness": "COMPLETE",
        "paragraphCount": expected,
        "durationMs": max((p["endMs"] or 0 for p in segments), default=0) or None,
    }


async def read_meeting_link(url: str, *, model=None, on_progress=None) -> dict:
    validate_url(url)
    parsed = urlsplit(url)
    if parsed.hostname == "bncloud.ieasetek.com":
        match = re.fullmatch(r"/share/([A-Za-z0-9]+)/*", parsed.path)
        if match:
            return parse_buddy(await fetch_json(f"https://bncloud.ieasetek.com/pocket-note/web/share/{match[1]}"), url)
    elif parsed.hostname == "tingwu.aliyun.com":
        match = re.fullmatch(r"/doc/transcripts/([A-Za-z0-9]+)/*", parsed.path)
        if match:
            source = parse_tingwu(
                await fetch_json(
                    "https://tingwu.aliyun.com/api/trans/getTransResult?c=web",
                    {
                        "action": "getTransResult",
                        "version": "1.0",
                        "transId": match[1],
                    },
                ),
                url,
            )
            try:
                summary = await fetch_json(
                    "https://tingwu.aliyun.com/api/lab/getAllLabInfo?c=web",
                    {
                        "action": "getAllLabInfo",
                        "version": "1.0",
                        "transId": match[1],
                        "content": ["labSummaryInfo"],
                    },
                )
                if str(summary.get("code")) == "0":
                    cards = (summary.get("data") or {}).get("labCardsMap", {}).get("labSummaryInfo", [])
                    source["platformSummary"] = "\n".join(
                        f"{value.get('title', '')}\n{value.get('value', '')}"
                        for card in cards
                        for content in card.get("contents", [])
                        for value in content.get("contentValues", [])
                    )
            except (aiohttp.ClientError, TimeoutError, MeetingSourceError):
                source["summaryNotice"] = "平台总结未能读取，分析依据为原始转写。"
            return source
    if model is None:
        from yuxi.config import config
        from yuxi.models import select_model

        model = select_model(config.default_model)
    from yuxi.product_chat.meeting_web_reader import read_public_meeting

    return await read_public_meeting(url, model, on_progress=on_progress)


def read_text(text: str, title: str = "粘贴的会议资料") -> dict:
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    return finalize_source(
        "文字资料", title, None, [paragraph(line, i + 1) for i, line in enumerate(rows)], "", len(rows)
    )


def read_attachment(path: str, name: str) -> dict:
    file = Path(path)
    if file.stat().st_size > MAX_SOURCE_BYTES:
        raise MeetingSourceError("TOO_LARGE", "文件超过 20 MB，请缩小文件后重试。")
    suffix = Path(name).suffix.lower()
    if suffix in (".txt", ".md", ".markdown"):
        try:
            text = file.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = file.read_text(encoding="gb18030")
    elif suffix == ".docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(file)
        text = "\n".join(
            item.text
            if isinstance(item, Paragraph)
            else "\n".join(" | ".join(cell.text for cell in row.cells) for row in item.rows)
            for item in doc.iter_inner_content()
            if isinstance(item, (Paragraph, Table))
        )
    elif suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(file)
        if reader.is_encrypted:
            raise MeetingSourceError("ACCESS_REQUIRED", "PDF 已加密，请上传可读取的文件。")
        pages = [page.extract_text() or "" for page in reader.pages]
        if any(not page.strip() for page in pages):
            raise MeetingSourceError("PARTIAL", "PDF 含无法提取文字的页面，首版不支持图片转写，请上传完整文字版。")
        text = "\n".join(pages)
    else:
        raise MeetingSourceError("UNSUPPORTED_FILE", "请上传 TXT、Markdown、DOCX 或可提取文字的 PDF。")
    return read_text(text, name)
