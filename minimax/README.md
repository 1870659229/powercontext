# MiniMax 赛题：规有所依 —— SDD 上下文管理系统

一个围绕**规格驱动开发（Spec-Driven Development）**的工程上下文模块：给定开发任务，
从 seekdb 检索当前有效的规格、相关代码与验证测试，拼成开发上下文交给 MiniMax 完成代码修改，
并用 PowerContext 保存任务进展、支持跨进程重启后复用。

## 一、目录结构

```
minimax/
├── demo_app/                    # 业务工程（待修改对象的宿主）
│   ├── src/taskboard.py         # 任务看板实现（已含 S4：filter_by_tag，即修改后版本）
│   └── tests/test_taskboard.py  # 目标测试(S4) + 回归测试(R1–R6)
├── specs/taskboard_spec.md      # SDD 规格来源（S1–S5 + R1–R5）
├── context_module/
│   ├── indexer.py               # 把规格/代码/测试索引进嵌入式 seekdb
│   ├── context.py               # 按任务检索并构建结构化上下文
│   ├── state.py                 # PowerContext 任务状态读写
│   ├── minimax_client.py        # MiniMax API 调用
│   ├── patcher.py               # 安全应用模型生成的补丁
│   └── cli.py                   # 统一调用入口
├── verify/run_demo.py           # 端到端验证（修改前失败 → 应用补丁 → 修改后通过）
├── records/                     # 技术使用记录
│   ├── minimax_development_log.md  # MiniMax 开发记录（任务/上下文/推理/补丁）
│   ├── patch.py                 # MiniMax 生成的补丁
│   ├── taskboard.patch          # 修改前后代码差异（unified diff）
│   ├── snapshots/               # 修改前 / 修改后代码快照
│   │   ├── taskboard_before.py
│   │   └── taskboard_after.py
│   ├── test_log.md              # 修改前/修改后测试日志（含命令与退出码）
│   └── TECH_NOTE.md             # 一页技术说明
└── data/                        # 运行时数据（seekdb 索引 + PowerContext 数据库，可重建）
```

> **关于代码版本**：`demo_app/src/taskboard.py` 是**修改后**的版本（已实现 S4）。
> 修改前的原始版本保存在 `records/snapshots/taskboard_before.py`，
> 两者差异见 `records/taskboard.patch`。
> `verify/run_demo.py --reset` 会把业务代码还原为修改前状态，
> `--apply` 则重新应用 `records/patch.py` 还原为修改后状态。

## 二、环境与依赖

| 项 | 版本 / 说明 |
|---|---|
| Python | 3.13 |
| powercontext | 1.5.1（PyPI，`pip install powercontext==1.5.1`） |
| pylibseekdb | 嵌入式 seekdb（`pip install pylibseekdb`） |
| pytest | 任意近期版本 |
| MiniMax | 通过 HTTP API 调用，需要 `MINIMAX_API_KEY` |

安装：

```bash
pip install powercontext==1.5.1 pylibseekdb pytest
```

## 三、配置

MiniMax 凭据走环境变量（**不要写进仓库**）：

```bash
# Linux / macOS
export MINIMAX_API_KEY="<your key>"
export MINIMAX_GROUP_ID = "<your group id>"     # 可选
export MINIMAX_MODEL = "MiniMax-M2.7"           # 可选，覆盖默认模型

# Windows PowerShell
$env:MINIMAX_API_KEY = "<your key>"
$env:MINIMAX_GROUP_ID = "<your group id>"     # 可选
$env:MINIMAX_MODEL = "MiniMax-M2.7"           # 可选
```

默认模型为 `MiniMax-M2.7`（通过 `GET https://api.minimaxi.com/v1/models` 可查当前可用模型；
旧的 `MiniMax-Text-01` 已下线）。如需换一个模型，设置 `MINIMAX_MODEL` 即可，无需改代码。

在 `minimax/` 目录下执行所有命令。

## 四、调用入口

> **重要：`pylibseekdb.open()` 是进程级单例**（官方说明：同一进程内第二次调用会被静默忽略），
> 因此需要访问同一个 seekdb 数据目录的连续动作请写在**同一条命令**里。
> `verify/run_demo.py` 就是这样做的：它在一个进程内完成建索引 → 检索 → 状态读回。

```bash
# 1. 初始化 + 检索 + 读状态（一次进程内顺序执行，推荐用法）
python -m context_module.cli init context "实现 TaskBoard 的 filter_by_tag 按标签筛选任务" status

# 2. 只建索引 + 写入 PowerContext 初始任务状态
python -m context_module.cli init

# 3. 输出结构化开发上下文（JSON：规格 / 代码 / 测试 / 进展）
python -m context_module.cli context "实现 TaskBoard 的 filter_by_tag 按标签筛选任务"

# 4. 输出给模型的可读上下文
python -m context_module.cli prompt "实现 TaskBoard 的 filter_by_tag 按标签筛选任务"

# 5. 调用 MiniMax 生成补丁（需 MINIMAX_API_KEY），结果写入 records/
python -m context_module.cli propose "实现 TaskBoard 的 filter_by_tag 按标签筛选任务"

# 6. 读回 PowerContext 中的任务状态（验证重启复用）
python -m context_module.cli status
```

`context` 命令的输出示例（真实运行结果）：

```json
{
  "task": "实现 TaskBoard 的 filter_by_tag 按标签筛选任务",
  "specs": [{"id": "S4", "source": "specs/taskboard_spec.md", "content": "..."}],
  "code": [{"symbol": "TaskBoard", "source": "demo_app/src/taskboard.py", "content": "..."}],
  "tests": [{"name": "test_filter_by_tag_returns_matching_tasks_in_order", "...": "..."}],
  "progress": {"completed": ["..."], "todo": ["实现 filter_by_tag", "运行目标测试与回归测试"], "verification": {}}
}
```

### 平台差异说明

`pylibseekdb` 目前通过启动本地 seekdb runtime 进程提供嵌入式访问（官方说明：真正的进程内嵌模式尚未发布）。
在 **Linux** 上实例关闭后可再次打开同一数据目录；在 **Windows** 上存在官方已知缺陷
（seekdb-bindings issue #61：`close()` 早于 `seekdb.exe` 退出，数据目录仍被占用），
导致同一目录在**另一个进程**里可能无法再次打开。

本作品不依赖跨进程复用同一个 seekdb 数据目录：
`verify/run_demo.py` 在单个进程内完成全部需要访问索引的步骤，
任务状态则由 PowerContext 持久化在独立的 SQLite 中，与 seekdb 实例无关。

## 五、验证样例

```bash
# 完整流程：还原到「修改前」→ 验证修改前失败 → 建索引 → 检索 → 状态读回
#          → 应用 MiniMax 补丁 → 验证修改后通过
python verify/run_demo.py --apply

# 只验证「修改前」状态与上下文输出（不应用补丁）
python verify/run_demo.py

# 单独把业务代码还原为「修改前」版本
python verify/run_demo.py --reset
```

> `--apply` 会先把 `demo_app/src/taskboard.py` 还原为「修改前」版本
> （依据 `records/snapshots/taskboard_before.py`），再应用补丁并复验，
> 因此无论评审拿到的是哪个版本的代码，都能复现完整流程。
> 该脚本在一个进程内完成全部步骤（`pylibseekdb.open()` 是进程级单例）。

预期结果：

| 阶段 | 目标测试（S4） | 回归测试（R1–R6） |
|---|---|---|
| 修改前 | **4 failed**（`filter_by_tag` 不存在） | 6 passed |
| 应用补丁后 | **4 passed** | 6 passed |

## 六、已知限制

- 上下文检索基于关键词命中（seekdb 存词元、应用层打分），资料量小（17 个片段）时精度足够；
  资料规模显著变大时应改为向量检索（seekdb 支持向量索引，本作品未启用以免引入 embedding 依赖）。
- MiniMax 补丁应用采用"追加方法"的保守策略：若目标方法已存在会拒绝重复写入，需先 `--reset`。
- `data/` 目录可随时删除重建（`python -m context_module.cli init`），不含不可再生信息。

## 七、提交材料对照

| 题面要求 | 对应位置 |
| --- | --- |
| 完整源码（上下文模块 / Agent 接入 / 配套程序） | `context_module/`、`demo_app/`、`verify/` |
| 运行说明（依赖版本、配置步骤、数据初始化、调用入口、测试命令） | 本文件第二、三、四、五节 |
| 验证样例（任务描述、有效规格、初始数据、操作步骤、预期结果、重置方法） | `specs/taskboard_spec.md` + 本文件第五节 |
| 代码与测试（修改前后快照、差异、新增功能测试、回归测试、运行日志） | `records/snapshots/`、`records/taskboard.patch`、`demo_app/tests/`、`records/test_log.md` |
| 技术使用记录（MiniMax 开发记录、数据库查询记录、PowerContext 状态写入与复用记录） | `records/minimax_development_log.md`、`records/test_log.md`、`records/TECH_NOTE.md` |
| 一页技术说明（模块结构、上下文选择方式、三项技术分工） | `records/TECH_NOTE.md` |

### 六项必做项的落点

| 必做项 | 落点与证据 |
| --- | --- |
| 能够运行 | `pip install powercontext==1.5.1 pylibseekdb pytest` → `python -m context_module.cli init` → `python verify/run_demo.py` |
| MiniMax 实际参与 | `records/minimax_development_log.md`（本次任务 + 提供的上下文 + 推理 + 补丁），补丁与 `records/taskboard.patch` 一致 |
| 上下文正确 | `python -m context_module.cli context "<任务>"` 输出规格/代码/测试来源；改 `specs/` 后重新 `init` 即反映更新内容 |
| 数据库实际参与 | `context_module/indexer.py` 写入嵌入式 seekdb，`context.py` 真实执行 `SELECT ... FROM documents` |
| 状态能够复用 | `context_module/state.py` 用 PowerContext CAPTURED Source 存进度，`cli status` 跨进程读回 |
| 代码修改有效 | `records/test_log.md`：修改前 4 failed / 回归 6 passed，修改后 4 passed / 回归 6 passed |
