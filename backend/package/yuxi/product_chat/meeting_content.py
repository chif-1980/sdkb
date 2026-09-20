"""Render the task section from the saved follow-up data, never from a second editable table."""

import re
from datetime import date

from markdown_it import MarkdownIt


_TASK_HEADING = re.compile(
    r"^(?:[一二三四五六七八九十\d]+[、.．)）]\s*)?(?:行动清单|行动项|待办事项|待办清单)(?:[（(].*[）)])?$"
)


def split_task_section(body: str) -> tuple[str, str]:
    lines = body.splitlines(keepends=True)
    headings = []
    tokens = MarkdownIt().parse(body)
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0:
            title = re.sub(r"[*_`#]", "", tokens[i + 1].content).strip()
            headings.append((token.map[0], int(token.tag[1:]), title))
    spans = []
    for index, (start, level, title) in enumerate(headings):
        if _TASK_HEADING.fullmatch(title):
            end = next((s for s, depth, _ in headings[index + 1 :] if depth <= level), len(lines))
            spans.append((start, end))
    if spans:
        first = spans[0][0]
        removed = {n for start, end in spans for n in range(start, end)}
        return "".join(lines[:first]).rstrip(), "".join(
            line for n, line in enumerate(lines) if n >= first and n not in removed
        ).strip()
    # New/manual narrative without a task heading keeps tasks before questions/analysis.
    insert = next(
        (start for start, _, title in headings if title.startswith(("待确认问题", "业务分析", "企业正式资料"))),
        len(lines),
    )
    return "".join(lines[:insert]).rstrip(), "".join(lines[insert:]).strip()


def task_date(task: dict) -> str:
    value = task.get("dueDate") or ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    if (
        task.get("dueDateEdited")
        or (task.get("delivery") or {}).get("feishuTaskId")
        or task.get("reviewStatus", "PENDING") != "PENDING"
    ):
        return ""
    text = (task.get("dueDateSuggestion") or value).strip()
    text = re.sub(r"^(?:截至|截止(?:到)?)[:：]?\s*", "", text)
    text = re.sub(r"(?:之前|前|截止)$", "", text).strip()
    match = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text) or re.fullmatch(
        r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text
    )
    try:
        return date(*map(int, match.groups())).isoformat() if match else ""
    except ValueError:
        return ""


def meeting_body(result: dict) -> str:
    followup = result.get("followup")
    if not isinstance(followup, dict) or not isinstance(followup.get("tasks"), list):
        return result.get("body", "")
    before, after = split_task_section(result.get("body", ""))
    rows = ["## 待办事项", "", "| 事项与要求 | 负责人 | 期限 | 确认 / 执行 | 依据 |", "| --- | --- | --- | --- | --- |"]

    def cell(value):
        return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "；")

    for task in followup["tasks"]:
        owner = (task.get("assignee") or {}).get("displayName") or "待确认"
        if owner == "待确认" and task.get("assigneeSuggestion"):
            owner += f"（原文：{task['assigneeSuggestion']}）"
        due = task_date(task) or "待确认"
        if due == "待确认" and task.get("dueDateSuggestion"):
            due += f"（原文：{task['dueDateSuggestion']}）"
        review = {"CONFIRMED": "已确认", "IGNORED": "已忽略", "DELIVERY_FAILED": "发送失败"}.get(
            task.get("reviewStatus"), "待确认"
        )
        status = {"OPEN": "待开始", "IN_PROGRESS": "进行中", "DONE": "已完成"}.get(task.get("status"), "待开始")
        state = f"{review} / {status}" if review == "已确认" else review
        if task.get("aiProposal"):
            state += "（有 AI 修改待核对）"
        if (task.get("delivery") or {}).get("pendingUpdate"):
            state += "（待同步飞书）"
        title = task.get("title", "") + (f"；{task['content']}" if task.get("content") else "")
        refs = "".join(f"[{ref}]" for ref in task.get("sourceRefs", [])) or "人工补充 / 未提供"
        rows.append("| " + " | ".join(map(cell, [title, owner, due, state, refs])) + " |")
    section = "\n".join(rows) if followup["tasks"] else "## 待办事项\n\n暂无待办事项。"
    return "\n\n".join(part for part in [before, section, after] if part)
