# LangGraph Coding-Agent 使用指南

版本对应：本指南对应当前仓库 `app/cli.py` 中已提供的命令：`run`、`resume`、`chat`、`logs`。

## 1. 项目简介与能力边界

这是一个本地 CLI Coding-Agent，核心能力是：
- 根据自然语言需求在工作目录中读写代码。
- 通过状态机（LangGraph）分步骤执行：规划、行动、验证、反思、结束。
- 支持会话持久化（SQLite），可查看执行轨迹与失败原因。
- 支持多轮对话：同一个 `session_id` 内连续提出新开发需求。

当前边界：
- 单机、单用户、本地目录执行。
- 工具集为受限白名单（读写文件、搜索、运行测试等）。
- 不覆盖远程并发协作与多租户权限隔离。

## 2. 快速开始（5 分钟）

### 2.1 环境准备

建议 Python 3.11+，并在独立虚拟环境使用。

PowerShell 示例：

```powershell
conda activate coding-agent
python -m pip install -r requirements.txt
```

### 2.2 配置 `.env`

在仓库根目录创建/更新 `.env`：

```env
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=gpt-4o-mini
BASE_URL=https://api.openai.com/v1

MAX_RETRY_STEPS=20
MAX_TOOL_CALLS=80
MAX_CONTEXT_TURNS=8
CONTEXT_SUMMARY_MAX_CHARS=2000
ALLOW_FINISH_WITHOUT_TESTS=true
VERIFY_MODE=auto
TEST_COMMAND=pytest -q
```

说明：`MAX_RETRY_STEPS` 未设置时会回退读取 `MAX_STEPS`。

### 2.3 首次运行（单轮）

```powershell
python -m app.cli run "用 Python 实现一个 LRU Cache，支持 get/put，时间复杂度 O(1)" --cwd D:\desktop\code-A
```

常见输出字段：
- `session_id`：会话 ID。
- `status`：`finished` 或 `failed`。
- `error`：失败原因（若有）。
- `verification_note`：验证阶段说明（若有）。
- `summary`：本轮执行摘要。

## 3. CLI 命令一览

### 3.1 `run`

用途：启动新会话并执行首轮需求，或向指定会话追加首轮任务。

```powershell
python -m app.cli run "<task>" [--cwd <path>] [--session-id <id>] [--yes]
```

参数：
- `task`：任务描述。
- `--cwd`：工作目录。
- `--session-id`：可选，指定已有会话。
- `--yes`：允许风险写入不经人工阻断（谨慎使用）。

### 3.2 `resume`

用途：恢复会话状态继续执行（适合处理中断或挂起场景）。

```powershell
python -m app.cli resume <session_id> [--cwd <path>] [--yes]
```

### 3.3 `chat`

用途：多轮对话入口（推荐日常使用）。

```powershell
python -m app.cli chat [--cwd <path>] [--session-id <id>] [--new] [--yes]
```

会话选择规则：
- `--new`：强制新建会话。
- `--session-id`：连接指定会话。
- 都不传：默认续接最近会话；若不存在则新建。

内置命令：
- `/exit`：退出 chat。
- `/new`：立即新建会话。
- `/session`：显示当前 `session_id`。
- `/session <id>`：切换到指定会话。

### 3.4 `logs`

用途：查看某会话的执行轨迹。

```powershell
python -m app.cli logs [session_id] [--cwd <path>] [--verbose]
```

`--verbose` 会额外输出：
- 预算统计：`retry_attempts/tool_call_count/write_count`
- 验证模式与说明
- turn 列表（每轮状态、请求、工具调用数、错误）

## 4. 多轮会话与上下文机制

核心概念：
- `session`：一个长期上下文容器。
- `turn`：你在 chat 中输入的一条新需求。

每轮执行时：
- 保留会话级上下文（历史摘要 + 最近若干轮）。
- 重置本轮运行时预算与中间状态（避免上一轮污染）。

上下文压缩策略：
- 保留最近 `MAX_CONTEXT_TURNS` 轮完整记录。
- 更早的轮次被折叠进 `conversation_summary`。
- `conversation_summary` 长度受 `CONTEXT_SUMMARY_MAX_CHARS` 限制。

## 5. `.env` 配置项说明

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `OPENAI_API_KEY` | 空 | 模型鉴权 Key |
| `OPENAI_MODEL` | `gpt-4o-mini` | 使用的模型名 |
| `BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `MAX_RETRY_STEPS` | `20`（或回退 `MAX_STEPS`） | 失败反思重试上限 |
| `MAX_TOOL_CALLS` | `80` | 单轮工具调用软上限 |
| `MAX_CONTEXT_TURNS` | `8` | 保留最近完整轮次数 |
| `CONTEXT_SUMMARY_MAX_CHARS` | `2000` | 会话摘要最大字符数 |
| `ALLOW_FINISH_WITHOUT_TESTS` | `true` | 无测试场景是否允许收敛 |
| `VERIFY_MODE` | `auto` | 验证策略，`auto` / `required` |
| `TEST_COMMAND` | `pytest -q` | 验证阶段测试命令 |
| `ALLOWED_COMMANDS` | `pytest,python,pip,uv` | `run_command` 工具允许的可执行程序（逗号分隔） |

## 6. 输出字段释义

### 6.1 主输出字段

- `status`
  - `finished`：本轮任务已完成。
  - `failed`：本轮任务失败。
- `error`：失败原因（例如参数错误、测试失败、预算耗尽）。
- `verification_note`：验证结论说明（例如 tests 通过、无 tests 收敛）。
- `summary`：当前轮执行总结。

### 6.2 `logs --verbose` 中的关键字段

- `retry_attempts`：本轮失败反思次数。
- `tool_call_count`：本轮工具调用总数。
- `write_count`：本轮成功写文件次数。
- `turns`：会话中每轮请求与结果摘要。

## 7. 常见问题与排障

### 7.1 `Permission denied: D:\desktop\code-A`

高概率不是系统目录完全不可写，而是模型生成了无效写入路径（例如空路径、目录路径）。

排查步骤：
1. 用 `logs --verbose` 看该轮 `error` 与 turn 信息。
2. 在下一轮明确要求“写入具体文件路径”，例如：
   - `请写入 tests/lru_cache_test.py`
3. 避免模糊表达，如“写到 tests 文件夹里”但不提供明确文件名。

推荐提示词：
```text
目标：实现 LRU Cache
写入文件：tests/lru_cache.py
测试文件：tests/test_lru_cache.py
验收：至少包含一个 assert 验证 get/put 行为
```

### 7.2 `MAX_RETRY_STEPS` 耗尽或重复失败短路

现象：
- `Reached MAX_RETRY_STEPS=...`
- `Detected repeated identical failure twice...`

处理建议：
1. 缩小任务粒度（一次只做一件事）。
2. 明确输入输出文件路径与验收标准。
3. 必要时提高预算：`MAX_RETRY_STEPS`、`MAX_TOOL_CALLS`。

### 7.3 `no tests collected`

在 `VERIFY_MODE=auto` 且 `ALLOW_FINISH_WITHOUT_TESTS=true` 时，可能自动收敛并给出说明。

如果你希望强制测试门禁：
```env
VERIFY_MODE=required
```

### 7.4 如何降低失败率

建议每轮需求包含三段：
1. 目标功能（做什么）
2. 明确路径（写到哪些文件）
3. 验收断言（如何判断完成）

模板：
```text
请完成以下任务：
1) 目标：...
2) 写入文件：src/a.py, tests/test_a.py
3) 验收：运行 TEST_COMMAND 后通过，且包含至少一个断言
```

## 8. 完整多轮示例

### 第 1 轮：实现功能

```powershell
python -m app.cli chat --cwd D:\desktop\code-A
```

输入：
```text
实现 LRU Cache，写入 tests/lru_cache.py，要求 get/put 都是 O(1)
```

### 第 2 轮：补测试

输入：
```text
在 tests/test_lru_cache.py 增加至少 3 个断言，覆盖淘汰策略
```

### 第 3 轮：修复失败

输入：
```text
如果测试失败，请最小改动修复并解释根因
```

### 查看会话轨迹

```powershell
python -m app.cli logs <session_id> --cwd D:\desktop\code-A --verbose
```

## 9. 最佳实践

- 尽量指定“文件路径 + 验收标准”，不要只给抽象目标。
- 优先在 `chat` 中连续推进同一主题，保持上下文连续。
- 大任务拆成多轮：实现 -> 测试 -> 修复 -> 重构。
- 遇到失败先看 `logs --verbose`，再补充更具体需求。

## 10. 变更同步清单（维护指南）

当你修改以下内容时，请同步更新本文件：
- 新增/删除 CLI 命令或参数（更新第 3 章）。
- 新增/调整 `.env` 配置（更新第 5 章）。
- 修改会话/上下文策略（更新第 4 章）。
- 修改错误处理或预算策略（更新第 6、7 章）。
- 新增常见故障（更新第 7 章 FAQ）。

---

如果你希望，我可以继续再生成一个更短的 `QUICKSTART.md`（只保留最核心命令与 FAQ）。
