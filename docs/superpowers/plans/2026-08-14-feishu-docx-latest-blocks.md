# 飞书 Docx 最新块读取修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留 Docx revision 作为素材版本标识，同时让块列表与子块读取始终使用飞书当前版本。

**Architecture:** `FeishuClient` 仍先读取 Docx 元数据和正文，但不再把元数据 revision 传给块接口。内部块分页与递归方法删除 revision 参数，只保留分页参数，从接口边界上保证不会误读受限历史修订。

**Tech Stack:** Python 3.13、httpx、pytest、pytest-asyncio

---

### Task 1: 修复 Docx 块读取参数

**Files:**
- Modify: `backend/test/unit/integrations/test_feishu_client.py`
- Modify: `backend/package/yuxi/integrations/feishu/client.py`

- [ ] **Step 1: 写失败回归测试**

在 `test_get_wiki_document_reads_docx_content_and_nested_block_attachments` 的 MockTransport 中拒绝任何携带 `document_revision_id` 的块请求，并把预期请求参数改为 `None`：

```python
if "/blocks" in request.url.path and request.url.params.get("document_revision_id") is not None:
    return httpx.Response(403, json={"code": 1770032, "msg": "forBidden"})

assert requests == [
    ("/open-apis/docx/v1/documents/doc-token", None, None),
    ("/open-apis/docx/v1/documents/doc-token/raw_content", None, None),
    ("/open-apis/docx/v1/documents/doc-token/blocks", None, None),
    ("/open-apis/docx/v1/documents/doc-token/blocks", "blocks-next", None),
    ("/open-apis/docx/v1/documents/doc-token/blocks/root-1/children", None, None),
    ("/open-apis/docx/v1/documents/doc-token/blocks/container-1/children", None, None),
]
```

- [ ] **Step 2: 运行目标测试确认失败**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations/test_feishu_client.py::test_get_wiki_document_reads_docx_content_and_nested_block_attachments
```

Expected: FAIL，块列表请求因携带 `document_revision_id=42` 收到 `FeishuPermissionError`。

- [ ] **Step 3: 实施最小修复**

删除内部块读取链路的 revision 参数，素材返回值仍使用元数据 revision：

```python
blocks = await self._list_document_blocks(document_id)

async def _list_document_blocks(self, document_id: str) -> list[Mapping[str, Any]]:
    return await self._list_blocks(f"/open-apis/docx/v1/documents/{document_id}/blocks")

async def _list_blocks(self, path: str) -> list[Mapping[str, Any]]:
    params: dict[str, str] = {"page_size": "100"}
```

同时从 `_collect_block_attachments` 和 `_list_block_children` 的签名、调用中删除 revision。

- [ ] **Step 4: 运行目标测试与客户端回归**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations/test_feishu_client.py
uv run --project backend --no-sync --no-dev ruff check backend/package/yuxi/integrations/feishu/client.py backend/test/unit/integrations/test_feishu_client.py
```

Expected: 全部 PASS，Ruff 无错误。

- [ ] **Step 5: 真实只读诊断与提交**

Run:

```bash
backend/.venv/bin/python /tmp/quickdone-kb-feishu-acceptance.xdPBe6/diagnose_feishu.py
git diff --check
git add backend/package/yuxi/integrations/feishu/client.py backend/test/unit/integrations/test_feishu_client.py docs/superpowers/specs/2026-08-13-feishu-knowledge-pipeline-design.md docs/superpowers/plans/2026-08-14-feishu-docx-latest-blocks.md
git commit -m "fix: read latest Feishu document blocks"
```

Expected: Wiki、Docx 元数据、正文、无 revision 的块列表和子节点接口均为 HTTP 200；提交不包含凭据、token 或企业正文。
