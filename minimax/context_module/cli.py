"""SDD 上下文模块统一调用入口。

命令：
    python -m context_module.cli init                 # 建 seekdb 索引 + 初始化 PowerContext 状态
    python -m context_module.cli context "任务描述"    # 输出结构化开发上下文（JSON）
    python -m context_module.cli prompt "任务描述"     # 输出给模型的可读上下文
    python -m context_module.cli propose "任务描述"    # 调用 MiniMax 生成补丁（需 MINIMAX_API_KEY）
    python -m context_module.cli status                # 读回 PowerContext 中的任务状态（验证重启复用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_module.context import ContextBuilder
from context_module.minimax_client import available, complete_context, log_exchange
from context_module.state import TASK_STATE_SOURCE_ID, TaskState, load_state_sync, save_state_sync

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
SCOPE_FILE = DATA_DIR / "scope_id.txt"


def _scope_id() -> str | None:
    if SCOPE_FILE.exists():
        value = SCOPE_FILE.read_text(encoding="utf-8").strip()
        return value or None
    return None


def _remember_scope(scope_id: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCOPE_FILE.write_text(scope_id, encoding="utf-8")


def cmd_init(_: argparse.Namespace) -> int:
    builder = ContextBuilder(PROJECT_ROOT, DATA_DIR / "seekdb")
    count = builder.reindex()
    state = TaskState(
        task="实现 TaskBoard.filter_by_tag 按标签筛选",
        specs=["S4"],
        completed=["搭建业务工程与测试", "建立 seekdb 资料索引"],
        todo=["实现 filter_by_tag", "运行目标测试与回归测试"],
        verification={},
    )
    scope_id = save_state_sync(DATA_DIR / "powercontext.db", state, _scope_id())
    _remember_scope(scope_id)
    print(json.dumps({"indexed_chunks": count, "scope_id": scope_id}, ensure_ascii=False))
    return 0


def _load_task_state() -> TaskState | None:
    scope = _scope_id()
    if scope is None:
        return None
    return load_state_sync(DATA_DIR / "powercontext.db", scope)


def cmd_context(args: argparse.Namespace) -> int:
    builder = ContextBuilder(PROJECT_ROOT, DATA_DIR / "seekdb")
    context = builder.build(args.task, _load_task_state())
    print(json.dumps(context.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    builder = ContextBuilder(PROJECT_ROOT, DATA_DIR / "seekdb")
    context = builder.build(args.task, _load_task_state())
    print(context.to_prompt())
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    if not available():
        print("ERROR: 未检测到 MINIMAX_API_KEY，无法调用 MiniMax。请先配置环境变量。", file=sys.stderr)
        return 2
    builder = ContextBuilder(PROJECT_ROOT, DATA_DIR / "seekdb")
    context = builder.build(args.task, _load_task_state())
    result = complete_context(context.to_prompt())
    log_path = log_exchange(PROJECT_ROOT / "records", args.task, context.to_prompt(), result)
    print(json.dumps({"target_symbol": result.target_symbol, "patch": result.patch, "log": str(log_path)}, ensure_ascii=False, indent=2))
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    state = _load_task_state()
    if state is None:
        print(json.dumps({"state": None}, ensure_ascii=False))
        return 0
    print(json.dumps(json.loads(state.to_json()), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="context_module", description="SDD 上下文管理模块")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="建索引并初始化状态").set_defaults(func=cmd_init)
    sub.add_parser("status", help="读回任务状态").set_defaults(func=cmd_status)

    for name, func, help_text in (
        ("context", cmd_context, "输出结构化上下文 JSON"),
        ("prompt", cmd_prompt, "输出给模型的可读上下文"),
        ("propose", cmd_propose, "调用 MiniMax 生成补丁"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("task", help="开发任务描述")
        child.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
