"""MiniMax 调用封装：把开发上下文交给模型，拿回代码补丁。

设计：
- 只用标准库 urllib 调用，避免额外 SDK 依赖（评审环境更易复现）。
- 提供 ``available()`` 检测环境变量；没有 Key 时 CLI 会明确报错而不是伪造结果。
- 每次调用的请求/响应摘要写入开发记录，供人工评审核对。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MINIMAX_API_URL = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
DEFAULT_MODEL = "MiniMax-M2.7"
ENV_KEY = "MINIMAX_API_KEY"
ENV_GROUP = "MINIMAX_GROUP_ID"
ENV_MODEL = "MINIMAX_MODEL"
REQUEST_TIMEOUT_SECONDS = 120

SYSTEM_PROMPT = """你是软件开发助手。你会收到一个开发任务、必须遵守的规格、相关代码和验证测试。
请只输出一个 JSON 对象，格式：
{"reasoning": "简述你如何满足规格", "patch": "要替换进目标文件的新代码块（完整方法或类定义）", "target_symbol": "被修改的符号名"}
不要输出 markdown 代码围栏之外的任何解释。"""


@dataclass
class MinimaxResult:
    """一次 MiniMax 调用的结果。"""

    reasoning: str
    patch: str
    target_symbol: str
    raw: str


def api_key() -> str | None:
    value = os.environ.get(ENV_KEY)
    return value.strip() if value and value.strip() else None


def group_id() -> str | None:
    value = os.environ.get(ENV_GROUP)
    return value.strip() if value and value.strip() else None


def available() -> bool:
    return api_key() is not None


def model_name(default: str = DEFAULT_MODEL) -> str:
    """Resolve the chat model, allowing an override via ``MINIMAX_MODEL``."""
    value = os.environ.get(ENV_MODEL)
    return value.strip() if value and value.strip() else default


def _extract_json(text: str) -> dict:
    """从模型输出里抽出第一个 JSON 对象（容忍代码围栏）。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model output contains no JSON object")
    return json.loads(cleaned[start : end + 1])


def complete_context(context_prompt: str, model: str | None = None) -> MinimaxResult:
    """把上下文发给 MiniMax，返回结构化结果。"""
    key = api_key()
    if key is None:
        raise RuntimeError(f"{ENV_KEY} is not set; cannot call MiniMax")

    payload = {
        "model": model_name() if model is None else model,
        "messages": [
            {"role": "system", "name": "MiniMax AI", "content": SYSTEM_PROMPT},
            {"role": "user", "name": "user", "content": context_prompt},
        ],
        "temperature": 0.1,
    }
    url = MINIMAX_API_URL
    gid = group_id()
    if gid:
        url = f"{url}?GroupId={gid}"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"MiniMax HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"MiniMax network error: {error.reason}") from error

    choices = body.get("choices") or []
    if not choices:
        message = body.get("base_resp", {}).get("status_msg", "empty choices")
        raise RuntimeError(f"MiniMax returned no choices: {message}")
    content = choices[0].get("message", {}).get("content", "")
    parsed = _extract_json(content)
    return MinimaxResult(
        reasoning=str(parsed.get("reasoning", "")),
        patch=str(parsed.get("patch", "")),
        target_symbol=str(parsed.get("target_symbol", "")),
        raw=content,
    )


def log_exchange(records_dir: Path, task: str, context_prompt: str, result: MinimaxResult) -> Path:
    """把一次交互写入开发记录（用于评审核对 MiniMax 实际参与）。"""
    records_dir.mkdir(parents=True, exist_ok=True)
    target = records_dir / "minimax_development_log.md"
    header = "" if target.exists() else "# MiniMax 开发记录\n\n"
    entry = (
        f"## 任务：{task}\n\n"
        f"### 提供的上下文（节选前 1200 字）\n```\n{context_prompt[:1200]}\n```\n\n"
        f"### 模型推理\n{result.reasoning}\n\n"
        f"### 生成补丁\n```python\n{result.patch}\n```\n\n"
        f"### 目标符号\n`{result.target_symbol}`\n\n---\n\n"
    )
    with target.open("a", encoding="utf-8") as handle:
        handle.write(header + entry)
    return target
