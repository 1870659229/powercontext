"""端到端验证：修改前失败 → 应用 MiniMax 补丁 → 修改后通过。

用法：
    python verify/run_demo.py            # 只验证"修改前失败"和上下文输出
    python verify/run_demo.py --apply    # 额外应用 records/patch.py 中的补丁并复验
    python verify/run_demo.py --reset    # 从备份恢复业务代码初始状态

退出码：0 全部符合预期；1 有断言失败。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo_app"
TARGET_SOURCE = DEMO / "src" / "taskboard.py"
PATCH_FILE = ROOT / "records" / "patch.py"
BEFORE_SNAPSHOT = ROOT / "records" / "snapshots" / "taskboard_before.py"

sys.path.insert(0, str(ROOT))

from context_module.patcher import PatchError, append_method, backup_file, restore_file  # noqa: E402

TARGET_TESTS = [
    "test_filter_by_tag_returns_matching_tasks_in_order",
    "test_filter_by_tag_is_exact_not_substring",
    "test_filter_by_tag_normalizes_input_and_handles_blank",
    "test_filter_by_tag_does_not_mutate_board",
]
REGRESSION_TESTS = [
    "test_r1_duplicate_task_id_rejected",
    "test_r2_tags_lowercased_and_order_preserved",
    "test_r3_complete_sets_done",
    "test_r4_list_tasks_filters_by_status",
    "test_r5_unknown_task_raises_keyerror",
    "test_r6_invalid_status_rejected",
]


def run_pytest(node_ids: list[str]) -> tuple[int, str]:
    args = [sys.executable, "-m", "pytest", "-q", *[f"tests/test_taskboard.py::{n}" for n in node_ids]]
    result = subprocess.run(args, cwd=DEMO, capture_output=True, text=True, encoding="utf-8")
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _capture(command, task: str) -> str:
    """在当前进程内执行一个 CLI 动作并捕获其标准输出。

    pylibseekdb 的 ``open()`` 是进程级单例：同一数据目录在一个进程里只能打开
    一次，子进程方式在 Windows 上会因上一个实例尚未释放而打不开同一目录。
    因此这里在同进程内调用，直接取函数的返回值。
    """
    import argparse

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = command(argparse.Namespace(task=task))
    if code != 0:
        raise RuntimeError(f"命令执行失败（exit={code}）：{buffer.getvalue()}")
    return buffer.getvalue()


def summary_line(output: str) -> str:
    for line in reversed(output.strip().splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            return line.strip()
    return output.strip().splitlines()[-1] if output.strip() else ""


def check(condition: bool, label: str, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""))
    return condition


def restore_before() -> str:
    """把业务代码还原为「修改前」版本，返回还原方式说明。

    以仓库里的「修改前」快照为准（它是提交物中的权威事实源）；
    没有快照时才退回本地 .bak 备份。
    """
    if BEFORE_SNAPSHOT.exists():
        TARGET_SOURCE.write_text(
            BEFORE_SNAPSHOT.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
        return f"已从快照 {BEFORE_SNAPSHOT.relative_to(ROOT)} 恢复 taskboard.py"
    restore_file(TARGET_SOURCE)
    return "已从备份恢复 taskboard.py"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="应用 records/patch.py 补丁")
    parser.add_argument("--reset", action="store_true", help="从备份恢复")
    args = parser.parse_args()

    if args.reset:
        try:
            print(restore_before())
            return 0
        except PatchError as error:
            print(f"恢复失败：{error}")
            return 1

    # 每次验证都从「修改前」版本出发，保证评审拿到任何版本都能复现完整流程。
    if args.apply:
        try:
            print("[INFO] " + restore_before())
        except PatchError as error:
            print(f"[FAIL] 无法还原到修改前状态：{error}")
            return 1

    ok = True

    # 1. 修改前：目标测试必须失败
    code, output = run_pytest(TARGET_TESTS)
    ok &= check(code != 0, "修改前目标测试失败", summary_line(output))

    # 2. 修改前：回归测试必须通过
    code, output = run_pytest(REGRESSION_TESTS)
    ok &= check(code == 0, "修改前回归测试通过", summary_line(output))

    # 3. 初始化索引与任务状态（同一进程内，随后复用同一 seekdb 实例）
    from context_module.cli import cmd_context, cmd_init, cmd_status  # noqa: PLC0415

    init_out = _capture(cmd_init, "")
    ok &= check('"indexed_chunks"' in init_out, "索引初始化", init_out.strip()[:60])

    # 4. 上下文模块能输出四类内容
    #    同进程内调用：pylibseekdb 的 open() 是进程级单例，子进程方式在
    #    Windows 上会因为上一个实例尚未退出而无法再次打开同一数据目录。
    context = json.loads(_capture(cmd_context, "实现 TaskBoard 的 filter_by_tag 按标签筛选任务"))
    ok &= check([s["id"] for s in context["specs"]][:1] == ["S4"], "上下文首条规格为 S4")
    ok &= check(any(t["name"].startswith("test_filter_by_tag") for t in context["tests"]), "上下文包含目标测试")
    ok &= check(bool(context["progress"]["todo"]), "上下文包含待办事项")

    # 5. 任务状态可读（PowerContext 持久化在独立的 SQLite 中，与 seekdb 实例无关）
    state = json.loads(_capture(cmd_status, ""))
    ok &= check("filter_by_tag" in json.dumps(state, ensure_ascii=False), "状态重启后可读")

    if args.apply:
        if not PATCH_FILE.exists():
            print(f"[FAIL] 补丁文件不存在：{PATCH_FILE}")
            return 1
        backup_file(TARGET_SOURCE)
        try:
            name = append_method(TARGET_SOURCE, PATCH_FILE.read_text(encoding="utf-8"))
        except PatchError as error:
            print(f"[FAIL] 补丁应用失败：{error}")
            return 1
        print(f"[INFO] 已应用方法 {name}")

        code, output = run_pytest(TARGET_TESTS)
        ok &= check(code == 0, "修改后目标测试通过", summary_line(output))
        code, output = run_pytest(REGRESSION_TESTS)
        ok &= check(code == 0, "修改后回归测试通过", summary_line(output))

    print("\n=== 结论：" + ("全部符合预期" if ok else "存在不符合项") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
