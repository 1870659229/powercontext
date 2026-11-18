"""TaskBoard 验收测试：目标测试（S4，修改前必须失败）+ 回归测试（R1–R5，必须始终通过）。

运行方式（在 minimax/demo_app 目录）：
    python -m pytest tests -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taskboard import Task, TaskBoard  # noqa: E402


def _board_with_tags() -> TaskBoard:
    board = TaskBoard()
    board.add_task(Task(task_id="t1", title="写规格", tags=("Docs", " design ")))
    board.add_task(Task(task_id="t2", title="修 Bug", tags=("code",)))
    board.add_task(Task(task_id="t3", title="评审", tags=("docs", "review")))
    return board


# ---------- 目标测试：S4 按标签筛选（修改前应失败） ----------


def test_filter_by_tag_returns_matching_tasks_in_order() -> None:
    board = _board_with_tags()
    result = board.filter_by_tag("docs")
    assert [task.task_id for task in result] == ["t1", "t3"]


def test_filter_by_tag_is_exact_not_substring() -> None:
    board = _board_with_tags()
    assert board.filter_by_tag("doc") == ()
    assert board.filter_by_tag("code") != ()


def test_filter_by_tag_normalizes_input_and_handles_blank() -> None:
    board = _board_with_tags()
    assert [task.task_id for task in board.filter_by_tag("  DOCS  ")] == ["t1", "t3"]
    assert board.filter_by_tag("   ") == ()


def test_filter_by_tag_does_not_mutate_board() -> None:
    board = _board_with_tags()
    before = board.list_tasks()
    board.filter_by_tag("docs")
    assert board.list_tasks() == before


# ---------- 回归测试：R1–R5（修改前后都必须通过） ----------


def test_r1_duplicate_task_id_rejected() -> None:
    board = TaskBoard()
    board.add_task(Task(task_id="a", title="first"))
    with pytest.raises(ValueError):
        board.add_task(Task(task_id="a", title="second"))


def test_r2_tags_lowercased_and_order_preserved() -> None:
    board = TaskBoard()
    board.add_task(Task(task_id="x", title="t", tags=("Beta", " alpha ", "")))
    assert board.get_task("x").tags == ("beta", "alpha")


def test_r3_complete_sets_done() -> None:
    board = TaskBoard()
    board.add_task(Task(task_id="c", title="t"))
    updated = board.complete("c")
    assert updated.status == "done"
    assert board.get_task("c").status == "done"


def test_r4_list_tasks_filters_by_status() -> None:
    board = TaskBoard()
    board.add_task(Task(task_id="p", title="todo-task"))
    board.add_task(Task(task_id="q", title="done-task", status="done"))
    assert [task.task_id for task in board.list_tasks(status="done")] == ["q"]
    assert len(board.list_tasks()) == 2


def test_r5_unknown_task_raises_keyerror() -> None:
    board = TaskBoard()
    with pytest.raises(KeyError):
        board.get_task("missing")


def test_r6_invalid_status_rejected() -> None:
    board = TaskBoard()
    with pytest.raises(ValueError):
        board.add_task(Task(task_id="z", title="t", status="archived"))
