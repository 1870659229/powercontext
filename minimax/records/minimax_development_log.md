# MiniMax 开发记录

## 任务：实现 TaskBoard 的 filter_by_tag 按标签筛选任务

### 提供的上下文（节选前 1200 字）
```
# 开发任务
实现 TaskBoard 的 filter_by_tag 按标签筛选任务

## 必须遵守的规格
### S4（specs/taskboard_spec.md）
## S4 按标签筛选（本任务要实现的功能）

- 新增方法 `filter_by_tag(tag: str) -> tuple[Task, ...]`。
- `tag` 在匹配前去除首尾空白并转小写；空标签（去空白后为空）返回**空元组**。
- 只返回**包含该标签**的任务，标签匹配为**精确匹配**（不做子串、不做前缀匹配）。
- 返回结果**保持任务插入顺序**，且不改变看板内部状态。

### S5（specs/taskboard_spec.md）
## S5 查询语义

- `get_task(task_id)` 查不到时必须抛 `KeyError`。
- `list_tasks(status=None)` 返回全部任务；给定 `status` 时只返回该状态的任务。
- 所有查询返回**新元组**，不暴露内部可变容器。

## 兼容性约束（回归要求）

以下行为在本次修改中必须保持不变，对应回归测试：

- **R1**：重复 `task_id` 仍抛 `ValueError`。
- **R2**：标签仍小写化并保持顺序。
- **R3**：`complete` 仍正确置 `done`。
- **R4**：`list_tasks` 按状态过滤仍正确。
- **R5**：`get_task` 未知 ID 仍抛 `KeyError`。

### S2（specs/taskboard_spec.md）
## S2 标签规范化

- 任务可携带零到多个标签 `tags`。
- 写入时每个标签去除首尾空白，空白标签丢弃，统一转小写后保存。
- 标签**保持声明顺序**，不排序、不去重以外不做其他变换。

### S1（specs/taskboard_spec.md）
## S1 任务身份

- 每条任务有全局唯一、非空、非纯空白的 `task_id`。
- 重复 `task_id` 写入必须失败（`ValueError`）。

## 相关代码
### Task（demo_app/src/taskboard.py）
```python
class Task:
    """一条任务记录。tags 为小写化后的标签元组，保持声明顺序。"""

    task_id: str
    title: str
    tags: tuple[str, ...] = ()
    status: str = DEFAULT_STATUS
    priority: int = DEFAULT_PRIORITY
```

### TaskBoard（demo_app/src/taskboar
```

### 模型推理
在 TaskBoard 中新增 filter_by_tag 方法：先去除查询标签首尾空白并转小写，空标签直接返回空元组；随后遍历按插入顺序保存的任务，仅选择 tags 中包含精确匹配标签的任务。方法只读取内部状态，并返回新元组，不修改看板内容，同时保持现有查询、标签规范化及重复 ID 等回归行为不变。

### 生成补丁
```python
    def filter_by_tag(self, tag: str) -> tuple[Task, ...]:
        normalized = tag.strip().lower()
        if not normalized:
            return ()
        return tuple(
            task for task in self._tasks.values() if normalized in task.tags
        )

```

### 目标符号
`TaskBoard.filter_by_tag`

---

