# 一页技术说明

## 问题

AI 开发助手修改代码时，需要知道：当前该遵守哪些规格、该改哪些代码、怎么验证。
资料分散、规格演进、任务中断会导致遗漏约束或使用过时信息。

## 模块结构

```
任务描述 ──▶ ContextBuilder ──▶ DevContext ──▶ MiniMax ──▶ 补丁 ──▶ 测试
                 │                  │
                 ▼                  ▼
            seekdb 索引        PowerContext 状态
       （规格/代码/测试）      （已完成/待办/验证结果）
```

| 组件 | 职责 |
|---|---|
| `indexer.py` | 把规格按编号(S1..S5)、代码按符号(def/class)、测试按函数切成可定位片段，写入嵌入式 seekdb 的 `documents` 表（含词元列） |
| `context.py` | 按任务描述检索三类资料，加上 PowerContext 里的进展，产出 `DevContext`（可 JSON 化，也可渲染成 prompt） |
| `state.py` | 用 PowerContext 的 CAPTURED Source 保存任务状态，`scope_id` 落一个小文件，重启后按 scope 读回 |
| `minimax_client.py` | 标准库 HTTP 调用 MiniMax，强制模型输出 JSON（reasoning/patch/target_symbol），并把交互写入 `records/` |
| `patcher.py` | 把补丁方法安全追加到目标类，先备份、拒绝重复方法 |
| `verify/run_demo.py` | 端到端复现"修改前失败 → 应用补丁 → 修改后通过" |

## 上下文选择方式

1. **切分**：规格按 `## S\d+` 标题切、代码按顶层 `def/class` 切、测试按 `def test_` 切。
2. **索引**：每个片段连同其词元集合写入 seekdb（中文按字+双字切分，英文按标识符切分）。
3. **检索**：任务描述分词后与片段词元求交集，按"命中率"排序；跨 kind 分别取 Top-K（规格 4 / 代码 3 / 测试 5）。
4. **组装**：拼成 `DevContext`，规格在前（约束优先），代码居中，测试在后，进展收尾。

检索真的走数据库：`SELECT ... FROM documents [WHERE kind=...]`，不是内存字典。

## 三项技术的分工

| 技术 | 承担什么 | 证据 |
|---|---|---|
| **MiniMax** | 接收上下文，生成满足 S4 的 `filter_by_tag` 补丁 | `records/minimax_development_log.md`（任务+上下文+推理+补丁）、`records/patch.py` |
| **seekdb** | 持久化规格/代码/测试片段与词元，供真实查询构造上下文 | `data/seekdb/`（可重建）、`indexer.py`、`context` 命令输出 |
| **PowerContext** | 保存任务进展（已完成/待办/验证结果），重启后读取并进入上下文 | `state.py`、`status` 命令跨进程输出 |

## 为什么这样设计

- **规格编号锚定**：S1–S5 / R1–R5 让"哪些要求必须遵守"可被逐条核对，而不是靠模型自由发挥。
- **可回查来源**：每个片段带 `source` + `symbol`，上下文输出能定位到"哪个文件的哪个符号"。
- **保守补丁**：只追加方法、先备份、拒绝重复，避免模型输出直接破坏业务代码。
- **状态最小化**：只存进展摘要，不存完整会话，符合题面"不要求恢复完整 Agent 会话"。
