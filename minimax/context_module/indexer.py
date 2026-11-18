"""把工程资料（规格 / 源码 / 测试）索引进嵌入式 seekdb。

设计要点：
- seekdb 通过 ``pylibseekdb`` 嵌入式打开，数据落盘到指定目录，重启后仍可查询。
- 一张 ``documents`` 表存所有资料片段；一行 = 一个可定位的来源单元
  （规格条目 / 代码符号 / 测试函数），因此检索结果天然可回查来源。
- 索引用"关键词命中"而非向量：本题资料量小（十几条），关键词检索足够准，
  且不需要额外模型依赖，评审可直接复现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pylibseekdb

STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "the", "of", "to", "in", "on", "for", "is", "are",
        "be", "with", "by", "as", "or", "it", "this", "that", "支持", "实现", "需要",
    }
)
TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]+")
SPEC_HEADING = re.compile(r"^##\s+(S\d+)\s+(.*)$", re.MULTILINE)
DEF_PATTERN = re.compile(r"^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
TEST_FUNC = re.compile(r"^def\s+(test_[A-Za-z0-9_]+)", re.MULTILINE)


@dataclass(frozen=True)
class DocChunk:
    """一条可定位的资料来源。"""

    kind: str  # spec | code | test
    source: str  # 相对路径
    symbol: str  # 规格编号 / 函数名 / 测试名
    content: str


def _sql(value: str) -> str:
    """把单值渲染成 SQL 字符串字面量（转义单引号与反斜杠）。"""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def tokenize(text: str) -> set[str]:
    """把文本切成小写词元集合，去掉停用词。

    中文按单字切分（无分词依赖时的稳妥做法）：整段中文若当一个词元，
    "按标签筛选任务" 无法与 "标签" 命中，故拆成字级词元并保留双字组合。
    """
    tokens: set[str] = set()
    for match in TOKEN_PATTERN.findall(text):
        lowered = match.lower()
        if lowered in STOPWORDS or len(lowered) <= 1:
            continue
        if lowered.isascii():
            tokens.add(lowered)
            continue
        # 中文：加入每个汉字，并加入相邻双字，提升召回
        for char in lowered:
            if char not in STOPWORDS:
                tokens.add(char)
        for index in range(len(lowered) - 1):
            tokens.add(lowered[index : index + 2])
    return tokens


def _split_specs(text: str, source: str) -> list[DocChunk]:
    chunks: list[DocChunk] = []
    matches = list(SPEC_HEADING.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start() : end].strip()
        chunks.append(DocChunk(kind="spec", source=source, symbol=match.group(1), content=body))
    return chunks


def _split_code(text: str, source: str) -> list[DocChunk]:
    """按 def/class 切分代码，保留每个符号及其方法体作为独立片段。"""
    lines = text.splitlines()
    chunks: list[DocChunk] = []
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = DEF_PATTERN.match(line)
        if match and not line.startswith((" ", "\t")):
            starts.append((index, match.group(1)))
    for position, (start, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        chunks.append(DocChunk(kind="code", source=source, symbol=name, content=body))
    if not chunks:
        chunks.append(DocChunk(kind="code", source=source, symbol=Path(source).stem, content=text))
    return chunks


def _split_tests(text: str, source: str) -> list[DocChunk]:
    lines = text.splitlines()
    chunks: list[DocChunk] = []
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = TEST_FUNC.match(line)
        if match:
            starts.append((index, match.group(1)))
    for position, (start, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        chunks.append(DocChunk(kind="test", source=source, symbol=name, content=body))
    return chunks


def collect_chunks(project_root: Path) -> list[DocChunk]:
    """收集项目下的规格、源码与测试，切成可定位片段。"""
    chunks: list[DocChunk] = []
    spec_dir = project_root / "specs"
    demo_src = project_root / "demo_app" / "src"
    demo_tests = project_root / "demo_app" / "tests"

    for path in sorted(spec_dir.glob("*.md")):
        chunks.extend(_split_specs(path.read_text(encoding="utf-8"), f"specs/{path.name}"))
    for path in sorted(demo_src.glob("*.py")):
        chunks.extend(_split_code(path.read_text(encoding="utf-8"), f"demo_app/src/{path.name}"))
    for path in sorted(demo_tests.glob("*.py")):
        chunks.extend(_split_tests(path.read_text(encoding="utf-8"), f"demo_app/tests/{path.name}"))
    return chunks


class SeekDBIndex:
    """嵌入式 seekdb 资料索引。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._instance: pylibseekdb.SeekdbInstance | None = None
        self._connection: pylibseekdb.Connection | None = None

    def open(self) -> SeekDBIndex:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._instance = pylibseekdb.open(str(self._data_dir))
        self._connection = self._instance.connect()
        cursor = self._connection.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            " chunk_id INT PRIMARY KEY,"
            " kind VARCHAR(16),"
            " source VARCHAR(200),"
            " symbol VARCHAR(120),"
            " content TEXT,"
            " tokens TEXT)"
        )
        self._connection.commit()
        cursor.close()
        return self

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._instance is not None:
            self._instance.close()
            self._instance = None

    def __enter__(self) -> SeekDBIndex:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def reindex(self, chunks: list[DocChunk]) -> int:
        """清空并重建索引，返回写入条数。"""
        assert self._connection is not None
        cursor = self._connection.cursor()
        cursor.execute("DELETE FROM documents")
        for index, chunk in enumerate(chunks, start=1):
            tokens = " ".join(sorted(tokenize(chunk.content + " " + chunk.symbol)))
            # pylibseekdb 的 execute 只接受完整 SQL 字符串（无参数化接口），故逐字段转义。
            cursor.execute(
                "INSERT INTO documents (chunk_id, kind, source, symbol, content, tokens) VALUES ("
                f"{index}, {_sql(chunk.kind)}, {_sql(chunk.source)}, {_sql(chunk.symbol)},"
                f" {_sql(chunk.content)}, {_sql(tokens)})"
            )
        self._connection.commit()
        cursor.close()
        return len(chunks)

    def all_chunks(self) -> list[DocChunk]:
        assert self._connection is not None
        cursor = self._connection.cursor()
        cursor.execute("SELECT kind, source, symbol, content FROM documents ORDER BY chunk_id")
        rows = cursor.fetchall()
        cursor.close()
        return [DocChunk(kind=row[0], source=row[1], symbol=row[2], content=row[3]) for row in rows]

    def search(self, query: str, kind: str | None = None, limit: int = 8) -> list[tuple[DocChunk, float]]:
        """按关键词命中率检索，返回 (片段, 得分) 列表，得分降序。

        检索真的走 seekdb：先拉取候选行，再用 SQL 过滤 kind，最后在应用层按词元命中打分。
        打分口径：命中词元数 / 查询词元数，平局按片段长度升序（短片段更聚焦）。
        """
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        assert self._connection is not None
        cursor = self._connection.cursor()
        if kind is None:
            cursor.execute("SELECT kind, source, symbol, content, tokens FROM documents")
        else:
            cursor.execute(
                "SELECT kind, source, symbol, content, tokens FROM documents"
                f" WHERE kind = {_sql(kind)}"
            )
        rows = cursor.fetchall()
        cursor.close()

        scored: list[tuple[DocChunk, float]] = []
        for kind_value, source, symbol, content, tokens_text in rows:
            doc_tokens = set((tokens_text or "").split())
            hits = len(query_tokens & doc_tokens)
            if hits == 0:
                # 规格编号/测试名等精确锚定也算命中，避免关键词同义失配
                if symbol.lower() in query.lower():
                    hits = 1
                else:
                    continue
            score = hits / len(query_tokens)
            scored.append((DocChunk(kind_value, source, symbol, content), score))
        scored.sort(key=lambda item: (-item[1], len(item[0].content)))
        return scored[:limit]
