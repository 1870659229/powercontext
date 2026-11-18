"""用 PowerContext 持久化任务状态，支持跨进程重启读取。

用途：题面要求"保存任务进展或验证结果，并在后续运行中读取、使用"。
这里把任务状态写成一条 CAPTURED Source（source_id 固定为 TASK_STATE_SOURCE_ID），
重启后从 journal 读回，供上下文模块输出"已完成/待办/验证结果"。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft

TASK_STATE_SOURCE_ID = "sdd-task-state"
STATE_METADATA_KIND = "task_state"


@dataclass
class TaskState:
    """一次 SDD 任务的进展快照。"""

    task: str = ""
    specs: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "task": self.task,
                "specs": self.specs,
                "completed": self.completed,
                "todo": self.todo,
                "verification": self.verification,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> TaskState:
        data = json.loads(text)
        return cls(
            task=data.get("task", ""),
            specs=list(data.get("specs", [])),
            completed=list(data.get("completed", [])),
            todo=list(data.get("todo", [])),
            verification=dict(data.get("verification", {})),
        )


def _config(db_path: Path) -> BuiltinConfig:
    return BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{db_path}"))


async def _ensure_scope(runtime: Any, scope_id: str | None, idempotency_key: str) -> str:
    if scope_id:
        return scope_id
    scope = await runtime.scopes.create(
        ScopeDraft(title="SDD 上下文任务", summary="spec-driven development task state", idempotency_key=idempotency_key)
    )
    return scope.scope_id


async def save_state(db_path: Path, state: TaskState, scope_id: str | None = None) -> str:
    """写入任务状态，返回 scope_id（后续读取需要）。"""
    config = _config(db_path)
    async with open_builtin_runtime(config) as runtime:
        sid = await _ensure_scope(runtime, scope_id, "sdd-context-scope")
        scoped = runtime.sources.for_scope(sid)
        await scoped.capture(
            CaptureSource(
                source_id=TASK_STATE_SOURCE_ID,
                content=state.to_json(),
                metadata={"kind": STATE_METADATA_KIND},
            )
        )
    return sid


async def load_state(db_path: Path, scope_id: str) -> TaskState | None:
    """重启后读取任务状态；不存在时返回 None。"""
    config = _config(db_path)
    async with open_builtin_contexts(config) as contexts:
        context = await contexts.get(scope_id)
        entries = await context.sources.journal.entries()
        for entry in reversed(entries):
            if entry.source_ref.source_id == TASK_STATE_SOURCE_ID:
                content = getattr(entry.source, "content", None)
                if isinstance(content, str):
                    return TaskState.from_json(content)
    return None


def save_state_sync(db_path: Path, state: TaskState, scope_id: str | None = None) -> str:
    return asyncio.run(save_state(db_path, state, scope_id))


def load_state_sync(db_path: Path, scope_id: str) -> TaskState | None:
    return asyncio.run(load_state(db_path, scope_id))
