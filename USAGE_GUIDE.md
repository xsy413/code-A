# LangGraph Coding-Agent 使用指南

版本对应：本指南对应当前仓库 `app/cli.py` 中已提供的命令：`run`、`resume`、`chat`、`logs`。

## 1. 项目简介与能力边界

这是一个本地 CLI Coding-Agent，核心能力是：
- 根据自然语言需求在工作目录中读写代码。
- 通过状态机（LangGraph）执行：行动决策、工具批次执行、必要审批、修改验证与收尾。
- 支持会话持久化（SQLite），可查看执行轨迹与失败原因。
- 支持多轮对话：同一个 `session_id` 内连续提出新开发需求。

当前边界：
- 单机、单用户、本地目录执行。
- 文件工具和 Bash/PowerShell 使用统一的 allow / ask / deny 权限策略，未匹配默认 ask。
- 当前没有 OS 级文件与网络隔离；获准脚本和子进程使用当前用户权限。
- 模型服务必须支持原生 tool calling；不回退到文本 JSON 工具协议。
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
CONTEXT_WINDOW=185000
CONTEXT_OUTPUT_RESERVE=25000
CONTEXT_SAFETY_MARGIN=10000
TEST_COMMAND=pytest -q
```

说明：`MAX_RETRY_STEPS` 未设置时会回退读取 `MAX_STEPS`。
`MAX_EXPLORE_STEPS_BEFORE_WRITE` 为兼容旧配置保留读取，但不再触发强制写文件。
`VERIFY_MODE`、`ALLOW_FINISH_WITHOUT_TESTS` 已弃用；显式设置会提示，值不再控制验证或阻止最终回答。`TEST_COMMAND` 仅作模型选择测试时的参考，不自动执行。

### 2.3 首次运行（单轮）

```powershell
python -m app.cli run "用 Python 实现一个 LRU Cache，支持 get/put，时间复杂度 O(1)" --cwd D:\desktop\code-A
```

常见输出字段：
- `session_id`：会话 ID。
- `status`：`finished`、`failed`，或等待审批的 `awaiting_human_confirm`。
- `error`：失败原因（若有）。
- `verification_note`：验证阶段说明（若有）。
- `summary`：模型给出的本轮最终回答；异常停止时为错误说明。

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
- `--yes`：弃用兼容参数，会显示警告，不跳过任何权限审批。

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
- `/resume`：继续当前会话的待批准动作。
- `/revoke`：撤销当前进程中该会话的批准记录。

### 3.4 `logs`

用途：查看某会话的执行轨迹。

```powershell
python -m app.cli logs [session_id] [--cwd <path>] [--verbose]
```

`--verbose` 会额外输出：
- 预算统计：`retry_attempts/tool_call_count/write_count`
- 验证模式与说明
- turn 列表（每轮状态、请求、工具调用数、错误）
- 权限规则命中、执行结果与审批拒绝记录（不包含可复用授权本身）

## 4. 多轮会话与上下文机制

核心概念：
- `session`：一个长期上下文容器。
- `turn`：你在 chat 中输入的一条新需求。

每轮执行时：
- 保留 session 消息历史；未达到 token 上限时，不按轮数或工具结果的新旧程度裁剪。
- 重置本轮运行时预算与中间状态（避免上一轮污染）。

行动阶段使用原生工具调用：模型调用工具时继续执行；正常响应没有工具调用且有非空文字时结束当前 turn。
解释、分析和澄清问题可以不修改文件。本轮结束后，同一个 session 可以继续对话。
文件修改后只自动执行静态检查：路径、文件存在性、删除记录及 Python 源码编译，不导入或执行文件。检查通过、失败和不完整都会以系统执行观察和最新状态传给下一次 act；已触发停止条件时则保存检查结果后确定性收尾，不再调用模型。
测试由模型根据任务风险选择；先读取候选测试和必要配置，看到结果后再决定运行，优先相关文件、目录或用例，不默认运行全量测试。不凭测试文件名判断用途，应注意入口、插件、fixture 及顶层副作用；用户要求不运行测试时应尊重。内容检查只是降低风险，不保证执行无副作用。
内部 `finish` 节点只保存结果，不是模型可调用的工具，也不会再次调用模型生成总结。

### 主执行流程

新请求经过 `intake` 初始化和 `preflight` 工作区可用性检查后，直接进入 `act`。
不再强制生成实施计划，不增加任务分类模型调用，暂未提供计划工具。
预检查不列目录、不发现包或测试入口；需要这些信息时，由模型按当前任务调用工具获取。

```text
intake → preflight → act
act → 工具批次 → execute → 权限判定／审批／逐项执行
execute → 无文件变化 → act
execute → 文件变化 → verify（仅静态检查）→ act
verify → 通过／失败／检查不完整 → act
act → 模型选择的测试 → execute → 修改则静态检查 → act
act → 最终文字 → finish → END
```

所有工具仍经过权限判定与必要审批；拒绝结果返回模型，不作为代码失败诊断。
无法交互审批时保存待执行动作并暂停，使用 `resume` 继续。
shell 失败但留下文件变化时仍进行验证，预算、错误证据和验证限制继续保留。
查询默认只报告结果；修改任务简述改动与实际验证，真实失败和风险不能省略。
旧会话的 `planning / diagnosing / reflecting` 状态恢复为 `acting`；旧计划、诊断、反思字段继续支持加载，但不再影响动作决策。
agent 仍会在 `.agent` 保存状态与日志，项目文件未修改不代表运行期间完全没有磁盘写入。

### 批量工具与恢复

模型一次最多请求 8 个工具，按返回顺序串行执行，不是真正并发。依赖前一个结果的参数应留到下一次响应。
执行前校验整批参数；参数错误整批不执行。调用被拒绝、失败、超时或取消后，剩余项返回 `not_executed`，已完成修改不自动回滚。
整批修改结束后统一静态检查；中途失败的部分修改也检查。测试只能由模型选择，经 shell 工具和正常审批执行；不会追加自动全量测试，测试产生修改也只追加静态检查。
每项请求都计工具预算，包括参数错误、拒绝和跳过项；预算不足整批不执行。审批等待与恢复不重复计数；静态检查不计工具预算。
历史按完整工具批次保留，只有实际请求达到 token 输入上限才压缩旧前缀；assistant 消息只记录一次，每个 tool_call_id 都有对应结果，失败与跳过项也配对。静态检查无论通过或失败都作为系统执行观察，最新静态结果另放入 act 上下文，不因压缩丢失。
正常暂停后 `resume` 从队列位置继续，不重跑已记录完成项。进程意外退出留下 `running` 时，恢复标记 `indeterminate`，停止余项并报告不确定结果，不自动重跑。
变化检查基线只覆盖可扫描的普通工作区文件；外部目标、中断时缺失的基线或扫描失败会标记检查不完整，不承诺崩溃后的严格恰好一次执行。
显式恢复失败会话重置失败恢复窗口，不清除历史证据或重置工具预算；工具预算耗尽后需开启新 turn 或 session。
旧自动测试队列中未启动的项会记录为 `not_executed`，说明策略已调整，不请求审批、不增加失败次数、不退还已消费预算。已完成项保留，`running` 项按不确定执行恢复；队列闭合后检查已有修改。旧文字结论仅为历史证据，不代表当前测试通过。
失败处理总预算累计计算，不因成功调用而清零；完整成功批次只重置连续失败计数。同一失败批次签名连续出现 3 次时确定性停止，不再调用模型重写总结。

上下文压缩策略：
- 默认窗口 185000，预留输出 25000、安全余量 10000，实际输入上限为 150000 token。
- 请求包含 system、完整消息、工具 Schema、工具参数、结果、摘要及运行事实；缓存 token 仍占窗口。
- 达到上限时，将旧完整前缀交给独立配置的压缩模型，保留近期原文预算 30000，压缩后目标为 100000。不拆散批次。
- 未配置压缩服务时继承 act；只保留完整 `<summary>`，核对草稿与独立 reasoning 不显示、不归档。
- 原始消息和脱敏工具结果保留在 SQLite 内部表中；摘要替代旧前缀，不叠加重复摘要。
- 压缩失败进入 `awaiting_context`，保存证据并暂停，配置修正后 `resume`；不声称完成、不重跑工具或重复扣预算。
- `MAX_CONTEXT_TURNS`、`CONTEXT_SUMMARY_MAX_CHARS` 已弃用，不再控制保留范围。

## 5. `.env` 配置项说明

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `OPENAI_API_KEY` | 空 | 模型鉴权 Key |
| `OPENAI_MODEL` | `gpt-4o-mini` | 使用的模型名 |
| `BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `MAX_RETRY_STEPS` | `20`（或回退 `MAX_STEPS`） | 本轮失败处理总预算：失败批次、协议异常、新发生的静态检查失败各计一次 |
| `MAX_TOOL_CALLS` | `80` | 单轮工具请求总预算，模型测试正常计入，静态检查不计入 |
| `CONTEXT_WINDOW` | `185000` | 用户明确配置的总窗口，兼容服务别名不自动猜容量 |
| `CONTEXT_OUTPUT_RESERVE` | `25000` | act 输出预留及生成上限 |
| `CONTEXT_SAFETY_MARGIN` | `10000` | 协议与估算安全余量 |
| `CONTEXT_TARGET_TOKENS` | `100000` | 压缩后完整输入目标，未达目标但低于上限可继续 |
| `CONTEXT_RECENT_TOKENS` | `30000` | 近期完整消息预算，最后完整批次整体保留 |
| `CONTEXT_TOKENIZER` | 空 | 可信本地 tokenizer.json 的绝对路径，不自动下载项目 tokenizer |
| `CONTEXT_ESTIMATE_FACTOR` | `1.20` | 保守估算系数，按服务实际 prompt tokens 向上校准 |
| `COMPACT_MODEL / COMPACT_BASE_URL / COMPACT_API_KEY` | 继承 act | 独立压缩服务 |
| `COMPACT_WINDOW / COMPACT_TOKENIZER` | 继承 act | 压缩服务窗口与本地 tokenizer |
| `COMPACT_SUMMARY_TOKENS` | `8000` | 摘要正文上限，不截取半个摘要 |
| `COMPACT_OUTPUT_TOKENS / COMPACT_TIMEOUT` | `25000 / 180` | 完整生成上限与请求超时（秒） |
| `ALLOW_FINISH_WITHOUT_TESTS` | 兼容读取 | 已弃用并忽略，不阻止模型结束 |
| `VERIFY_MODE` | 兼容读取 | 已弃用并忽略，不再创建自动测试 |
| `TEST_COMMAND` | `pytest -q` | 供模型参考的测试命令，不自动执行 |
| `ALLOWED_COMMANDS` | 兼容读取 | 已弃用，不会转换为自动批准规则 |

## 6. 输出字段释义

### 6.1 主输出字段

- `status`
  - `finished`：模型给出最终回答，本轮已结束；是否完成需求还需结合回答和验证结果判断。
  - `failed`：本轮任务失败。
  - `awaiting_human_confirm`：未执行待批准动作；在交互终端使用 `resume` 继续。
  - `awaiting_context`：上下文无法安全构建或结果持久化失败，本轮暂停；检查配置/存储后恢复。
- `error`：失败原因（例如参数错误、测试失败、预算耗尽）。
- `verification_note`：静态检查的展示摘要，不是测试通过证明。
- `static_check`：`not_run / passed / failed / incomplete`，包含检查文件、检查项目、错误和扫描完整性。
- `test_results`：测试尝试的命令、实际目录、executable、结果和选择范围；状态为 `not_run / passed / failed / interrupted / no_tests / unknown`。
- `change_revision`：本轮记录的文件变化版本；任何后续文件变化都令旧测试结果 `stale`，不分析依赖关系。测试自身产生普通文件变化时也保守标记过期。
- 测试 `scope` 为明确目标、默认选择或未知，不自动推断整个项目通过。npm test、自定义脚本等不透明入口成功只表示命令成功，测试结论为未知。后续成功不删除旧失败证据。
- `summary`：模型原始最终回答，或异常停止时的错误说明。

### 6.2 `logs --verbose` 中的关键字段

- `retry_attempts`：本轮失败处理次数；等待审批不计，批次跳过项不重复计。
- `tool_call_count`：本轮工具调用总数。
- `write_count`：本轮记录文件变化的操作次数，包含失败命令留下的部分修改。
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
- `Detected repeated identical failure three times...`

处理建议：
1. 缩小任务粒度（一次只做一件事）。
2. 明确输入输出文件路径与验收标准。
3. 必要时提高预算：`MAX_RETRY_STEPS`、`MAX_TOOL_CALLS`。

### 7.3 `no tests collected`

直接 pytest 命令返回已知的未收集测试退出码时，记录 `no_tests`，不算测试通过。其他运行器不套用 pytest 的规则。
模型可以检查选择范围、读取配置、修复问题或说明限制后结束；没有强制测试门禁。不再使用 `VERIFY_MODE` 或 `ALLOW_FINISH_WITHOUT_TESTS` 改变这一行为。

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

## 11. Shell 工具与权限审批

模型工具为 `inspect_workspace`、`list_files`、`read_file`、`search_text`、`read_tool_result`、`write_file`、`patch_file`、`delete_file`，以及机器上实际安装的 `bash`、`powershell`。旧执行工具仅用于恢复旧待执行记录，不向模型声明。

Shell 接收 `command`、可选 `cwd` 和 `timeout_s`（默认 60 秒，范围 1-600）。每次独立启动，不保留上次变量和目录，不支持后台任务。Windows 使用 Git Bash 与 PowerShell，不调用 WSL。

审批输入 `once`、`session` 或 `reject`；未知操作和高影响动作不提供 `session`。普通文件的 session 批准覆盖整个工作区的创建与修改，但不包含删除、敏感配置或 shell 写入。测试/构建/lint 批准绑定具体入口和参数；源码、测试修改后可复用，执行配置、依赖、锁文件或入口变化后重新询问。

批准仅在当前 CLI 进程内、当前 session 生效。退出后即使恢复同一个 session 也需重新批准。无交互输入时暂停并保存动作，不默认批准；模型测试遵守相同规则。已等待审批的会话须先 `/resume`，或 `/new` 开始新会话。

用户权限配置只读取 `~/.coding-agent/permissions.json`，不读取仓库内授权文件。版本 1 的示例见 `docs/permissions.example.json`。`rules` 可按 tool、精确 argv 前缀、绝对路径 glob 和 workspace 限定；`overrides` 按 ID 显式替换内置等级。deny 优先于 ask，ask 优先于 allow；空文件、格式错误和未知字段都停止执行。权限配置与受保护 agent 状态不得由模型改写。

`D-08`、`D-09` 的自增权和状态/审计保护不能降级；试图降级会使配置无效。进程启动时从当前可信启动环境登记本地程序路径，工作区同名程序不获自动信任。用 `python -m app.cli permissions --cwd <path>` 查看规则，或追加 `--shell bash --command "git status"` 只分析而不执行命令。

普通 shell 成功不代表测试通过。文件变化会触发验证；失败命令留下的修改也记录。shell 快照排除依赖、缓存和构建输出，最多扫描 10000 个文件，单文件超过 8 MiB 标记不完整；这不是任意 shell 修改的完整审计或备份。工作区外文件修改不覆盖在普通工作区备份内。

目录内容查询或 Git 内容查询无法可靠排除敏感文件时会保守地 ask；已知凭据读取仍 deny。完整默认名单与安全边界见 `docs/tool-permissions-design.md`。

## 12. 工具分页与上下文观察

`python -m app.cli context <session_id> --cwd <workspace>` 或聊天 `/context` 显示最近输入估算、实际 prompt 用量、阈值、压缩记录与归档完整性。`logs --verbose` 区分本轮 act 与 session 的 act/compact 累计用量。

默认展示：文件/归档 20000 Unicode 字符、2000 行；目录 20000 字符、500 路径；搜索 20000 字符、200 匹配（前后各最多 3 行）；工作区概览 2000 字符、50 条目；写入/补丁/删除 2000 字符；Shell 两通道共 20000 字符，保留首尾，省略标记也计入额度。元数据独立保留，不对结构化 JSON 截取半段。

`read_file` 行号两端包含且从 1 开始，`column_start` 为 Unicode 字符列。续页使用 `output_meta.next` 和 `expected_version`；文件改变会明确返回版本冲突，不拼接两个版本。`list_files/search_text` 续页重复原查询并带 cursor，读取既有结果而不再次搜索。

`read_tool_result(result_id, stream='stdout', line_start=1, line_end?, column_start?)` 只读当前 session 已完成、脱敏结果，stream 仅 stdout/stderr。来源文件重新检查路径权限，用户明确的 ask/deny 仍优先。归档源码是历史证据，不更新当前文件阅读证据；普通文件和 Shell 不能直接访问 `.agent` 归档。

工具额度可用 `CONTEXT_<工具名大写>_CHARS / LINES / ITEMS` 调整相应维度，Shell 另有 HEAD。单结果原始正文最多 20 MiB、session 最多 100 MiB（`CONTEXT_RESULT_BYTES / CONTEXT_SESSION_BYTES`）；Shell 每通道有界捕获 10 MiB，持续排空并保留尾部。额度不足保留可用首尾，标记 `archive_complete=false` 和可读取范围，缺失内容明确不可恢复，不删除旧证据。

完整配置示例见 `docs/context.env.example`。本地 tokenizer 优先，其次匹配已知模型；未知别名使用 o200k_base 估算完整序列化输入，加每消息 32、每请求 256 的协议余量，再乘保守系数。估算不等于服务计数，实际 prompt 用量只向上校准。更小窗口必须显式配置，并同步降低目标、近期预算和输出预留。

这是上下文与验证策略，不是 OS 隔离，也不能保证批准的脚本不会读取外部资源或联网。
