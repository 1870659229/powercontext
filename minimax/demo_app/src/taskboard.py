"""TaskBoard: 一个带规格约束的小型任务看板（SDD 演示业务工程，修改前版本）。

本文件是「待修改的业务代码」。规格见 ``minimax/specs/taskboard_spec.md``。
当前实现只满足 S1–S3 与 S5，S4（按标签筛选）尚未实现，因此目标测试应当失败。
"""

from __future__ import annotations

from dataclasses import dataclass

VALID_STATUSES: frozenset[str] = frozenset({"todo", "doing", "done"})
DEFAULT_STATUS = "todo"
DEFAULT_PRIORITY = 3


@dataclass(frozen=True)
class Task:
    """一条任务记录。tags 为小写化后的标签元组，保持声明顺序。"""

    task_id: str
    title: str
    tags: tuple[str, ...] = ()
    status: str = DEFAULT_STATUS
    priority: int = DEFAULT_PRIORITY


class TaskBoard:
    """内存任务看板，按插入顺序保存任务。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def add_task(self, task: Task) -> None:
        if not task.task_id or not task.task_id.strip():
            raise ValueError("task_id must be non-empty")
        if task.task_id in self._tasks:
            raise ValueError(f"duplicate task_id: {task.task_id}")
        if task.status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {task.status}")
        normalized = tuple(tag.strip().lower() for tag in task.tags if tag.strip())
        self._tasks[task.task_id] = Task(
            task_id=task.task_id,
            title=task.title,
            tags=normalized,
            status=task.status,
            priority=task.priority,
        )

    def get_task(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise KeyError(f"unknown task_id: {task_id}") from None

    def list_tasks(self, status: str | None = None) -> tuple[Task, ...]:
        tasks = tuple(self._tasks.values())
        if status is None:
            return tasks
        return tuple(task for task in tasks if task.status == status)

    def complete(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        updated = Task(
            task_id=task.task_id,
            title=task.title,
            tags=task.tags,
            status="done",
            priority=task.priority,
        )
        self._tasks[task_id] = updated
        return updated

    def filter_by_tag(self, tag: str) -> tuple[Task, ...]:
        normalized = tag.strip().lower()
        if not normalized:
            return ()
        return tuple(
            task for task in self._tasks.values() if normalized in task.tags
        )
