# 测试日志

测试命令（Windows，Python 3.13，工作目录 `minimax/demo_app`）：

```
python -m pytest tests -q
```

同一组测试在代码修改前后各执行一次，测试集完全一致，未删改任何断言或预期结果。

## 修改前（`filter_by_tag` 未实现，代码快照见 `records/snapshots/taskboard_before.py`）
```
$ python -m pytest tests -q

tests\test_taskboard.py:51: AttributeError
=========================== short test summary info ===========================
FAILED tests/test_taskboard.py::test_filter_by_tag_returns_matching_tasks_in_order
FAILED tests/test_taskboard.py::test_filter_by_tag_is_exact_not_substring - A...
FAILED tests/test_taskboard.py::test_filter_by_tag_normalizes_input_and_handles_blank
FAILED tests/test_taskboard.py::test_filter_by_tag_does_not_mutate_board - At...
4 failed, 6 passed in 0.10s
```

失败原因是目标行为尚未实现（`AttributeError: 'TaskBoard' object has no attribute 'filter_by_tag'`），
不是缺依赖或环境错误；同批执行的 6 个回归测试全部通过。

## 修改后（应用 MiniMax 生成的补丁，代码快照见 `records/snapshots/taskboard_after.py`）
```
$ python -m pytest tests -q
..........                                                               [100%]
10 passed in 0.03s
```

目标测试单独执行：
```
$ python -m pytest tests/test_taskboard.py::test_filter_by_tag_returns_matching_tasks_in_order \
    tests/test_taskboard.py::test_filter_by_tag_is_exact_not_substring \
    tests/test_taskboard.py::test_filter_by_tag_normalizes_input_and_handles_blank \
    tests/test_taskboard.py::test_filter_by_tag_does_not_mutate_board -q
....                                                                     [100%]
4 passed in 0.02s
```

## 前后对照

| 阶段 | 目标测试（S4） | 回归测试（R1–R6） | 全量 |
| --- | --- | --- | --- |
| 修改前 | 4 failed | 6 passed | 4 failed, 6 passed |
| 修改后 | 4 passed | 6 passed | 10 passed |

## 端到端脚本

```
$ python verify/run_demo.py
[PASS] 修改前目标测试失败 -- 4 failed in 0.11s
[PASS] 修改前回归测试通过 -- 6 passed in 0.02s
[PASS] 上下文首条规格为 S4
[PASS] 上下文包含目标测试
[PASS] 上下文包含待办事项
[PASS] 状态重启后可读

=== 结论：全部符合预期 ===

$ python verify/run_demo.py --apply
[PASS] 修改前目标测试失败 -- 4 failed in 0.10s
[PASS] 修改前回归测试通过 -- 6 passed in 0.02s
[PASS] 上下文首条规格为 S4
[PASS] 上下文包含目标测试
[PASS] 上下文包含待办事项
[PASS] 状态重启后可读
[INFO] 已应用方法 filter_by_tag
[PASS] 修改后目标测试通过 -- 4 passed in 0.03s
[PASS] 修改后回归测试通过 -- 6 passed in 0.02s

=== 结论：全部符合预期 ===
```
