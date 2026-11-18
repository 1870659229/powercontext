"""上下文构建器：按开发任务从 seekdb 检索资料，产出结构化开发上下文。

输出结构对齐题面"上下文模块应能定位"的四类内容：
1. 适用的规格条目
2. 需要修改的代码文件/符号
3. 用于验证功能与兼容性的测试
4. 已完成 / 待办 / 已有验证结果（来自 PowerContext）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .indexer import DocChunk, SeekDBIndex, collect_chunks
from .state import TaskState

SPEC_LIMIT = 4
CODE_LIMIT = 3
TEST_LIMIT = 5


@dataclass
class DevContext:
    """一次任务的结构化开发上下文。"""

    task: str
    specs: list[DocChunk] = field(default_factory=list)
    code: list[DocChunk] = field(default_factory=list)
    tests: list[DocChunk] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "specs": [
                {"id": c.symbol, "source": c.source, "content": c.content} for c in self.specs
            ],
            "code": [
                {"symbol": c.symbol, "source": c.source, "content": c.content} for c in self.code
            ],
            "tests": [
                {"name": c.symbol, "source": c.source, "content": c.content} for c in self.tests
            ],
            "progress": self.progress,
        }

    def to_prompt(self) -> str:
        """渲染成给大模型的可读上下文（供 MiniMax 生成补丁）。"""
        parts = [f"# 开发任务\n{self.task}\n"]

        parts.append("## 必须遵守的规格")
        for chunk in self.specs:
            parts.append(f"### {chunk.symbol}（{chunk.source}）\n{chunk.content}\n")

        parts.append("## 相关代码")
        for chunk in self.code:
            parts.append(f"### {chunk.symbol}（{chunk.source}）\n```python\n{chunk.content}\n```\n")

        parts.append("## 验证测试")
        for chunk in self.tests:
            parts.append(f"### {chunk.symbol}（{chunk.source}）\n```python\n{chunk.content}\n```\n")

        progress = self.progress
        parts.append("## 任务进展")
        parts.append(f"- 已完成：{progress.get('completed', [])}")
        parts.append(f"- 待办：{progress.get('todo', [])}")
        parts.append(f"- 已有验证结果：{progress.get('verification', {})}")
        return "\n".join(parts)


class ContextBuilder:
    """用 seekdb 索引构造开发上下文。"""

    def __init__(self, project_root: Path, data_dir: Path) -> None:
        self._project_root = project_root
        self._index = SeekDBIndex(data_dir)

    def reindex(self) -> int:
        chunks = collect_chunks(self._project_root)
        with self._index:
            return self._index.reindex(chunks)

    def build(self, task: str, state: TaskState | None = None) -> DevContext:
        """按任务检索规格/代码/测试，并附上任务进展。"""
        with self._index:
            specs = [chunk for chunk, _ in self._index.search(task, kind="spec", limit=SPEC_LIMIT)]
            code = [chunk for chunk, _ in self._index.search(task, kind="code", limit=CODE_LIMIT)]
            tests = [chunk for chunk, _ in self._index.search(task, kind="test", limit=TEST_LIMIT)]
        progress: dict[str, Any] = {
            "completed": state.completed if state else [],
            "todo": state.todo if state else [],
            "verification": state.verification if state else {},
        }
        return DevContext(task=task, specs=specs, code=code, tests=tests, progress=progress)
