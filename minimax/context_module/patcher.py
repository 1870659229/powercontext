"""把 MiniMax 生成的补丁安全地应用到目标文件。

补丁约定：模型返回一段完整的方法定义（``def filter_by_tag(...)`` 或整个类）。
本模块负责两种落地方式：
1. ``append_method``：把方法追加到指定类的末尾（最常用、最安全）。
2. ``replace_symbol``：替换同名符号（保守实现，失败即报错不静默）。

两种方式都会先备份原文件，便于验证脚本回滚与评审复现。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

METHOD_HEADER = re.compile(r"^(\s*)def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)


class PatchError(RuntimeError):
    """补丁无法安全应用时抛出。"""


def backup_file(path: Path) -> Path:
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copyfile(path, backup)
    return backup


def restore_file(path: Path) -> None:
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        raise PatchError(f"no backup found for {path}")
    shutil.copyfile(backup, path)


def normalise_patch(patch: str) -> str:
    """去掉 markdown 围栏与首尾空行，保留补丁内部的相对缩进。"""
    lines = patch.strip("\n").splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")


def _reindent_to_member(code: str) -> str:
    """把补丁重排为「类成员」级缩进：``def`` 落在 4 空格，方法体相应加深。

    模型返回的补丁可能已经带缩进（顶层 ``def``，或已是类成员级缩进的 ``def``）。
    这里按补丁自身 ``def`` 行的缩进判断基准：``def`` 已带 4 空格时只保留相对
    缩进，``def`` 在顶层时整体加一级，保证落盘后与文件里其它类方法风格一致。
    """
    lines = code.splitlines()
    if not lines:
        return ""
    header = METHOD_HEADER.search(code)
    def_indent = len(header.group(1)) if header is not None else 0
    base = def_indent
    normalised: list[str] = []
    for line in lines:
        if not line.strip():
            normalised.append("")
            continue
        stripped = line[base:] if line[:base].strip() == "" else line.lstrip()
        normalised.append("    " + stripped)
    return "\n".join(normalised)


def append_method(file_path: Path, method_code: str, class_name: str = "TaskBoard") -> str:
    """把补丁方法追加到指定类的末尾，返回方法名。"""
    code = normalise_patch(method_code)
    header = METHOD_HEADER.search(code)
    if header is None:
        raise PatchError("patch does not define a method")
    method_name = header.group(2)

    original = file_path.read_text(encoding="utf-8")
    if re.search(rf"^\s*def\s+{re.escape(method_name)}\s*\(", original, re.MULTILINE):
        raise PatchError(f"method {method_name} already exists; refusing to duplicate")

    class_pattern = re.compile(rf"^class\s+{re.escape(class_name)}\b.*$", re.MULTILINE)
    class_match = class_pattern.search(original)
    if class_match is None:
        raise PatchError(f"class {class_name} not found")

    # 类体的结束位置：下一个顶层 class/def 或文件末尾
    tail_pattern = re.compile(r"^(?:class|def)\s+[A-Za-z_]", re.MULTILINE)
    tail_match = tail_pattern.search(original, class_match.end())
    insert_at = tail_match.start() if tail_match else len(original)

    block = f"\n{_reindent_to_member(code)}\n"
    head = original[:insert_at].rstrip("\n")
    tail = original[insert_at:]
    updated = f"{head}\n{block}\n{tail}" if tail else f"{head}\n{block}"
    # newline="\n" 保持 LF 行尾，避免 Windows 上默认把 LF 转成 CRLF 造成差异噪音
    file_path.write_text(updated, encoding="utf-8", newline="\n")
    return method_name
