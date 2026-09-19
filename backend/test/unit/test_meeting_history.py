from yuxi.product_chat.meeting_repository import group_history_rows


def row(id, *, parent=None, url=None, state="completed", title="同名会议"):
    return {
        "id": id,
        "parent_id": parent,
        "title": title,
        "state": state,
        "has_result": state == "completed",
        "urls": [url] if url else [],
        "source_count": 1,
    }


def test_groups_source_and_retry_chain_but_never_title_or_failed_results():
    rows = [
        row("failed", parent="latest", state="failed"),
        row("latest", parent="failed-middle", title="修改过标题"),
        row("failed-middle", parent="original", state="failed"),
        row("same-source", url="https://example.com/meeting/1#position"),
        row("different-source", url="https://example.com/meeting/2"),
        row("text-1"),
        row("text-2"),
        row("original", url="https://example.com/meeting/1"),
    ]
    groups = [[r["id"] for r in group] for group in group_history_rows(rows)]
    assert groups == [["latest", "same-source", "original"], ["different-source"], ["text-1"], ["text-2"]]


def test_does_not_merge_a_link_with_additional_text_or_different_share_tokens():
    with_text = row("mixed", url="https://example.com/meeting/1")
    with_text["source_count"] = 2
    rows = [
        with_text,
        row("link", url="https://example.com/meeting/1"),
        row("token-a", url="https://example.com/share?token=a"),
        row("token-b", url="https://example.com/share?token=b"),
    ]
    assert len(group_history_rows(rows)) == 4
