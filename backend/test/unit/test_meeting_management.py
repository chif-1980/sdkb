from copy import deepcopy

from yuxi.product_chat.meeting_management import item_key, merge_followups


def test_rerun_preserves_human_changes_and_external_task_identity():
    extracted = {"id": "task-1", "title": "核对产品", "sourceRefs": ["S1-P2"], "status": "OPEN"}
    saved = {
        **extracted,
        "extractionKey": item_key(extracted),
        "title": "核对具体产品规格",
        "status": "DONE",
        "reviewStatus": "CONFIRMED",
        "assignee": {"feishuUserId": "u1"},
        "delivery": {"feishuTaskId": "external-1"},
    }
    before = deepcopy(saved)
    result = merge_followups(
        [saved], [extracted, {"id": "task-1", "title": "新增事项", "sourceRefs": []}], run_id="MT-new-run"
    )
    assert len(result) == 2
    assert result[0] == before
    assert result[1]["id"] != "task-1"
    assert saved == before


def test_rerun_never_reopens_maintainer_decision():
    saved = {
        "id": "knowledge-1",
        "title": "价格说明",
        "status": "COVERED",
        "decisionReason": "已核对",
        "comparison": "旧比较",
    }
    incoming = {**saved, "status": "PENDING_MAINTAINER", "comparison": "新的正式知识证据"}
    result = merge_followups([saved], [incoming], run_id="MT-2")
    assert result[0]["status"] == "COVERED"
    assert result[0]["decisionReason"] == "已核对"
    assert result[0]["comparison"] == "新的正式知识证据"


async def test_feishu_completion_readback_skips_unsent_local_changes(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from yuxi.product_chat import meeting_task_sync as sync

    client = SimpleNamespace(
        get_employee=AsyncMock(return_value={"open_id": "open-1"}),
        get_task=AsyncMock(return_value={"completed_at": "1700000000000"}),
        aclose=AsyncMock(),
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(feishu_user_id="u1", feishu_open_id="open-1")))
    record = SimpleNamespace(
        result={
            "followup": {
                "tasks": [
                    {"id": "first", "status": "OPEN", "delivery": {"feishuTaskId": "remote-1"}},
                    {
                        "id": "second",
                        "status": "IN_PROGRESS",
                        "delivery": {"feishuTaskId": "remote-2", "pendingUpdate": True},
                    },
                ]
            }
        }
    )
    save = AsyncMock()
    monkeypatch.setattr(sync, "FeishuClient", lambda: client)
    monkeypatch.setattr(sync, "MeetingRepository", lambda _: SimpleNamespace(save_result=save))
    monkeypatch.setattr(
        sync,
        "MeetingManagement",
        lambda _: SimpleNamespace(
            attach=AsyncMock(return_value=SimpleNamespace(id="m1")),
            event=lambda *args: None,
        ),
    )
    # 未发送的本地修改计入 skipped，不参与读取，也不再当成 checked。
    assert await sync.sync_tasks(db, record, 1) == {"checked": 1, "failed": 0, "skipped": 1, "conflicts": 0}
    client.get_task.assert_awaited_once_with("remote-1")
    tasks = save.await_args.args[1]["followup"]["tasks"]
    assert tasks[0]["status"] == "DONE"
    assert tasks[1]["status"] == "IN_PROGRESS"
    assert record.result["followup"]["tasks"][0]["status"] == "OPEN"


async def test_unchanged_feishu_poll_does_not_create_a_revision(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from yuxi.product_chat import meeting_task_sync as sync

    client = SimpleNamespace(
        get_employee=AsyncMock(return_value={"open_id": "open-1"}),
        get_task=AsyncMock(return_value={"completed_at": "0"}),
        aclose=AsyncMock(),
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(feishu_user_id="u1", feishu_open_id="open-1")),
        execute=AsyncMock(),
    )
    record = SimpleNamespace(
        version=7,
        result={
            "followup": {
                "tasks": [
                    {
                        "id": "first",
                        "status": "OPEN",
                        "delivery": {
                            "feishuTaskId": "remote-1",
                            "syncStatus": "SYNCED",
                            # 0.6.2 起排期比对结果也参与状态判定，这里预置与本次读取一致的结论。
                            "scheduleComparison": {
                                "status": "UNVERIFIED",
                                "error": "飞书未返回完整的负责人信息",
                            },
                        },
                    },
                ]
            }
        },
    )
    save = AsyncMock()
    monkeypatch.setattr(sync, "FeishuClient", lambda: client)
    monkeypatch.setattr(sync, "MeetingRepository", lambda _: SimpleNamespace(save_result=save))
    monkeypatch.setattr(
        sync,
        "MeetingManagement",
        lambda _: SimpleNamespace(
            attach=AsyncMock(return_value=SimpleNamespace(id="m1")),
            event=lambda *args: None,
        ),
    )
    assert await sync.sync_tasks(db, record, 1) == {"checked": 1, "failed": 0, "skipped": 0, "conflicts": 0}
    save.assert_not_awaited()
    assert record.version == 7
    assert record.result["followup"]["tasks"][0]["delivery"]["lastSyncedAt"]
    db.execute.assert_awaited_once()


async def test_first_poll_that_discovers_a_schedule_difference_creates_one_revision(monkeypatch):
    """排期首次比对出差异时属于业务状态变化，必须留痕；重复轮询不再新增修订。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from yuxi.product_chat import meeting_task_sync as sync

    remote = {
        "completed_at": "0",
        "members": [{"id": "u9", "role": "assignee", "name": "李工"}],
        "due": {"timestamp": "1767225600000", "is_all_day": True},
    }
    client = SimpleNamespace(
        get_employee=AsyncMock(return_value={"open_id": "open-1"}),
        get_task=AsyncMock(return_value=remote),
        aclose=AsyncMock(),
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(feishu_user_id="u1", feishu_open_id="open-1")),
        execute=AsyncMock(),
    )
    record = SimpleNamespace(
        version=3,
        result={
            "followup": {
                "tasks": [
                    {
                        "id": "first",
                        "status": "OPEN",
                        "dueDate": "2026-01-03",
                        "assignee": {"feishuUserId": "u1", "displayName": "王工"},
                        "delivery": {"feishuTaskId": "remote-1", "syncStatus": "SYNCED"},
                    },
                ]
            }
        },
    )
    save = AsyncMock()

    async def save_result(_record, result, **_kwargs):
        # 真实仓储会把新快照写回记录，这里复现该行为，才能验证第二次轮询无状态变化。
        _record.result = result

    save.side_effect = save_result
    monkeypatch.setattr(sync, "FeishuClient", lambda: client)
    monkeypatch.setattr(sync, "MeetingRepository", lambda _: SimpleNamespace(save_result=save))
    monkeypatch.setattr(
        sync,
        "MeetingManagement",
        lambda _: SimpleNamespace(
            attach=AsyncMock(return_value=SimpleNamespace(id="m1")),
            event=lambda *args: None,
        ),
    )

    assert await sync.sync_tasks(db, record, 1) == {"checked": 1, "failed": 0, "skipped": 0, "conflicts": 1}
    save.assert_awaited_once()
    comparison = save.await_args.args[1]["followup"]["tasks"][0]["delivery"]["scheduleComparison"]
    assert comparison["status"] == "DIFFERENT"
    assert comparison["differences"] == ["assignee", "dueDate"]

    # 同样的远端结果再次轮询不应再产生修订。
    save.reset_mock()
    assert await sync.sync_tasks(db, record, 1) == {"checked": 1, "failed": 0, "skipped": 0, "conflicts": 1}
    save.assert_not_awaited()
