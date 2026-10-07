# Bash / PowerShell 工具权限设计与默认等级名单

调查日期：2026-10-05。

本文的三级策略已接入本项目执行器。命令等级表是我们的默认策略，不是 Claude Code、Gemini CLI 或 Copilot 的完整内置名单。命令示例用于解释匹配条件，不能直接转换成无条件的前缀通配规则。

首版边界：仅前台命令、仅用户级配置、批准仅当前 CLI 进程内有效。普通文件的创建/修改可批准覆盖整个工作区；删除、敏感配置和 shell 写入不继承。目录内容读取或 Git 内容查询无法可靠排除敏感文件时保守 ask，未登记参数也 ask。

用户已选定：测试、构建和 lint 首次 ask；批准后允许在当前 session 内复用。会话授权不自动持久化到以后启动的会话。

## 1. 官方产品中值得采用的设计

- Claude Code 使用 allow、ask、deny，按 deny、ask、allow 的顺序评估。Bash 复合命令逐个检查子命令；PowerShell 会解析 AST、归一化常见别名，匹配不区分大小写。项目授予的权限与工作区信任关联。[Claude Code 权限文档](https://code.claude.com/docs/en/permissions)
- Gemini CLI 使用带优先级的规则，决策为 allow、ask_user、deny；非交互情况下 ask_user 不执行。重定向默认额外询问。[Gemini CLI Policy Engine](https://geminicli.com/docs/reference/policy-engine/)
- VS Code Copilot 也逐个检查复合命令，但其自动批准配置中的 false 代表需要批准，不是禁止执行。文档明确指出命令分析是尽力而为，需要隔离措施约束实际访问。[VS Code 审批文档](https://code.visualstudio.com/docs/agents/run/approvals)
- 权限与沙箱是两层机制。Claude Code 的 shell 沙箱覆盖子进程，支持 macOS、Linux、WSL2；官方文档指出原生 Windows 的命令不受该沙箱保护。[Claude Code 沙箱文档](https://code.claude.com/docs/en/sandboxing)

本项目采用限制优先的规则顺序，不采用数值优先级让 allow 覆盖 deny。

## 2. 决策规则

| 等级 | 行为 | 用户能否在此次执行提示中放行 |
|---|---|---|
| allow | 自动执行，仍受路径、参数、执行环境限制 | 不需要提示 |
| ask | 展示命令与影响，获得用户批准后执行 | 可以；按类别选择单次或当前会话 |
| deny | 不执行，向模型返回命中规则和原因 | 不可以；需要用户在权限配置管理中主动调整 |
| 未匹配 | 等同 ask | 可以；未知命令默认只提供单次批准 |

评估顺序固定为：deny -> ask -> allow -> 默认 ask。

显式 ask 不会因为另一个更具体的 allow 而失效。会话批准是对某条 ask 决策的授权记录，并不把它改成具有更高优先级的 allow。标记为每次确认的规则不能使用会话批准。

不要配置 ask(Bash(*)) 或 ask(PowerShell(*)) 来实现默认 ask，它会遮蔽所有 allow。默认等级应是独立配置项。

整个复合命令取最严格的子命令、重定向目标和路径访问等级：任一 deny 就拒绝整条；无 deny 但任一 ask 就确认整条；全部 allow 才自动执行。执行前完成分析，不能先运行前半条再发现后半条被禁止。

如果无法可靠解析动态命令、脚本或参数，结果为 ask，而不是猜测它只读。已经识别出的 deny 不因解析其他部分失败而降为 ask。

## 3. allow：默认自动执行名单

下面的文件操作必须限于已授权工作区内的普通文件，排除凭据、权限配置和其他受保护路径。程序需要来自已登记的可信安装路径，不能因为名字相同就信任项目中同名脚本。未列出的参数默认 ask。

### 3.1 Bash

| ID | 命令/操作 | 自动执行条件 |
|---|---|---|
| A-B01 | pwd | 只输出当前工作目录 |
| A-B02 | ls | 仅列目录；目标位于工作区内 |
| A-B03 | cat | 仅读取已解析的普通文件；没有写重定向 |
| A-B04 | head、tail | 读取普通文件；tail 不带后台运行或持续跟踪选项 |
| A-B05 | rg、grep | 本地搜索；逐个核对文件目标；禁止 preprocessor 等执行外部程序的选项 |
| A-B06 | find | 仅白名单的遍历、筛选、输出参数，如 -name、-type、-maxdepth、-print；不含 -exec、-execdir、-delete、-fprint |
| A-B07 | wc | 统计工作区内文件的行数、字节数 |
| A-B08 | stat、du | 读取工作区内文件属性或大小 |
| A-B09 | diff、cmp | 比较普通文件；不执行外部比较器、不写输出文件 |
| A-B10 | sort、uniq、cut、tr | 只处理普通文件或已获准管道数据；不含输出文件、子命令或动态执行 |
| A-B11 | sha256sum、sha512sum | 对工作区内非敏感文件计算摘要 |
| A-B12 | echo、printf | 仅输出可静态理解的文本；变量、命令替换和重定向分别审核 |
| A-B13 | date、whoami | 只读取本机时间或当前用户身份；date 不包含设置时间的选项 |
| A-B14 | command -v、type | 仅查询命令位置或类型；不是执行包装器 |
| A-B15 | cd | 目标及符号链接解析结果均在工作区内；仅影响本次 shell 调用 |

不将 sed、awk、perl 自动视为只读工具；它们包含程序执行或写入能力，默认 ask。

### 3.2 PowerShell

命令需要限定为可信内置 cmdlet 或登记的可信模块，不允许项目同名函数、别名或模块冒充。文件参数必须解析为 FileSystem provider 的工作区路径。

| ID | 命令/操作 | 自动执行条件 |
|---|---|---|
| A-P01 | Get-Location | 查询 FileSystem 工作目录 |
| A-P02 | Get-ChildItem | 仅列工作区文件；排除 Env:、Registry、UNC 路径 |
| A-P03 | Get-Item、Test-Path | 查询普通文件或目录是否存在、属性 |
| A-P04 | Get-Content | 读取工作区非敏感文件；不含持续跟踪参数 |
| A-P05 | Select-String | 仅本地文件文本搜索；核对所有目标路径 |
| A-P06 | Get-FileHash | 对工作区内非敏感普通文件计算摘要 |
| A-P07 | Get-Date | 查询时间，不含 Set-Date 等修改命令 |
| A-P08 | Get-Command | 仅查询已加载、可信命令；禁止搜索时自动加载项目模块 |
| A-P09 | Get-Process | 只返回进程名、PID、CPU 等普通元数据，不扩展到凭据、内存或环境内容 |
| A-P10 | Get-Service | 仅查看本机服务状态；不得使用远程查询或修改 cmdlet |
| A-P11 | Set-Location | 仅进入工作区内目录；不操作其他 provider |
| A-P12 | Select-Object、Measure-Object | 处理已批准输入；禁止计算属性等脚本块 |
| A-P13 | Sort-Object、Group-Object、Compare-Object | 仅静态属性处理；脚本块和自定义表达式 ask |
| A-P14 | Format-Table、Format-List、Out-String | 输出普通显示内容；不带脚本表达式和额外文件写入 |
| A-P15 | Write-Output、Write-Host | 仅输出可静态理解的文本，不含额外执行表达式 |

常见别名需要先归一化，如 gci/ls/dir -> Get-ChildItem，gc/cat/type -> Get-Content，rm/del/ri -> Remove-Item。别名解析不明时 ask；不能通过临时 Set-Alias 获得已有 cmdlet 的 allow 权限。

### 3.3 两个 shell 共用的 Git 与版本查询

| ID | 命令/操作 | 自动执行条件 |
|---|---|---|
| A-G01 | git status | 仅状态查询；由执行器固定关闭 pager、fsmonitor 和可选索引写入 |
| A-G02 | git diff | 固定禁止 ext-diff、textconv；禁止输出文件和修改配置参数；受保护文件内容不得输出 |
| A-G03 | git log | 仅提交元数据；不包含 patch、文件内容、输出文件或外部执行选项 |
| A-G04 | git show | 只查看非敏感提交/文件；禁止 ext-diff、textconv 和输出文件；受保护历史路径同样检查 |
| A-G05 | git rev-parse、git ls-files | 仅登记的查询参数，如 --show-toplevel、--verify HEAD 或普通索引文件列表 |
| A-G06 | git branch --list、git tag --list | 仅查询；不得创建、删除、覆盖分支或标签 |
| A-G07 | git ls-tree、git describe | 仅登记的本地查询形态 |
| A-G08 | 可信程序的精确版本命令 | 如 git --version、python --version、node --version；逐个登记，不配置 * --version |

Git 的 pager、hooks、textconv、fsmonitor、-c、自定义 executable 或全局配置都可能引入额外行为。不能简单放行 git *，也不能把程序所有 --help 形态自动放行。

pytest --version、npm 脚本帮助或未登记解释器模块的版本查询，仍按执行项目代码或加载插件处理，为 ask。

## 4. ask：必须获得批准的名单

### 4.1 可在当前会话复用的 ask

用户明确选择当前会话批准后，下列操作可以复用限定授权；默认选项仍是单次。批准范围必须展示 shell、工作区、程序、入口和允许参数，不保存成 python *、npm *、git * 等宽泛规则。

| ID | 操作 | Bash / PowerShell 示例 | 会话复用边界 |
|---|---|---|---|
| Q-S01 | Python 测试 | pytest -q；python -m pytest | 当前项目的测试入口和批准的参数/测试目录 |
| Q-S02 | Node 测试 | npm test；pnpm test；yarn test | 当前项目已确认的脚本定义；参数单独限定 |
| Q-S03 | 其他测试入口 | cargo test；go test；dotnet test；mvn test；gradle test | 限定项目与测试命令；依赖获取单独审批 |
| Q-S04 | 构建 | npm run build；cargo build；dotnet build；make build | 已确认的构建入口，不自动涵盖 publish/install/deploy |
| Q-S05 | lint / 类型检查 | ruff check；eslint；tsc --noEmit；mypy | 禁止把 --fix、--output、任意插件参数纳入只检查授权 |
| Q-S06 | 格式化或自动修复 | ruff format；black；eslint --fix | 批准具体命令及可写文件范围；不能扩大至受保护配置 |
| Q-S07 | 本地开发服务 | npm run dev；python -m http.server | 指定项目、端口、入口；默认仅 loopback；后台生命周期单独展示 |
| Q-S08 | 已审阅的项目脚本 | python scripts/check.py；node scripts/check.js；./scripts/check.sh；& ./scripts/check.ps1 | 指定入口；入口内容改变后重新确认 |
| Q-S09 | 结构化文件工具修改普通代码/测试/文档 | write_file、patch_file | 批准指定文件或目录范围；不包含删除、凭据或权限配置；shell 写入不继承此授权 |

测试、构建和 lint 都会执行项目代码或加载配置/插件，不能描述为纯只读操作。会话测试授权明确包含正常的源码和测试修改；否则每次修复代码都会重复提示。执行入口、脚本定义、依赖清单、锁文件、插件/hook 配置或 executable 变更时，授权失效。

构建可能联网下载依赖，会话构建批准不自动授予网络访问。原生 Windows 没有 OS 沙箱时，提示应明确说明程序及其子进程使用当前用户权限。

### 4.2 默认只提供单次批准的 ask

| ID | 操作 | Bash 示例 | PowerShell 示例/补充 |
|---|---|---|---|
| Q-O01 | 执行任意内联代码 | python -c、node -e、ruby -e、perl -e | ScriptBlock::Create、任意 .NET 方法、Add-Type、动态脚本块 |
| Q-O02 | 启动嵌套 shell / 动态执行 | bash -c、sh -c、eval、source、. script.sh | pwsh -Command、powershell -File、Invoke-Expression、& $command、点加载脚本 |
| Q-O03 | 安装、卸载或更新依赖 | pip install、uv sync、npm install、npx、pnpm dlx、cargo install | Install-Module、Install-Package、winget install；可能联网、执行安装钩子 |
| Q-O04 | 新建、写入、复制、移动文件 | mkdir、cp、mv、touch、tee、sed -i、重定向 | New-Item、Set-Content、Add-Content、Out-File、Copy-Item、Move-Item、Rename-Item |
| Q-O05 | 删除普通工作区文件 | rm、rmdir、find -delete | Remove-Item；展示解析后的全部目标；根目录删除走 deny |
| Q-O06 | 清理明确的构建输出目录 | rm -rf dist；rm -rf .pytest_cache | Remove-Item dist -Recurse；目标必须位于工作区且不是根目录/受保护目录 |
| Q-O07 | 写 Git 索引或本地历史 | git add、commit、merge、rebase、cherry-pick、stash | 同左；可能运行 hooks；不能用全局 git allow 授权 |
| Q-O08 | 创建或切换分支/工作树 | git switch、checkout、branch <name>、worktree add/remove | 同左；会影响工作树或文件 |
| Q-O09 | 丢弃修改或重写历史 | git reset --hard、restore、clean -fdx、branch -D、filter-repo | 同左；展示范围及会丢失的内容；每次确认 |
| Q-O10 | Git 网络操作 | git fetch、pull、clone、push、push --force-with-lease/--force | 同左；展示远端、分支、方向；每次确认 |
| Q-O11 | HTTP 请求和下载 | curl、wget | Invoke-WebRequest、Invoke-RestMethod；展示目标域名、方法和上传来源 |
| Q-O12 | 远程执行与传输 | ssh、scp、rsync | Enter-PSSession、Invoke-Command、New-PSSession；展示主机与命令 |
| Q-O13 | 修改非保护配置 | git config --local 普通键；编辑 pyproject.toml/package.json | 修改项目配置、依赖清单、锁文件；权限配置写入走 deny |
| Q-O14 | 压缩、解压 | tar、unzip、zip | Expand-Archive、Compress-Archive；核对源、目标及路径穿越；不能自动批准归档工具 |
| Q-O15 | 进程和后台任务 | kill 指定 PID、nohup、命令末尾 & | Stop-Process 指定 PID、Start-Process、Start-Job；只处理可确认归属的进程 |
| Q-O16 | 容器与虚拟环境执行 | docker run/exec/build/compose；podman；wsl 执行命令 | 同左；内层执行不靠 docker 前缀推断安全 |
| Q-O17 | 发布、部署和外部变更 | npm publish、twine upload、gh pr create、terraform apply、kubectl apply | 云 CLI 写操作、生产迁移；每次确认目标环境 |
| Q-O18 | 包管理、云或数据库查询 | npm view、pip index、aws、az、gcloud、psql、mysql | 即使查询也可能联网、读取凭据或访问外部业务数据 |
| Q-O19 | 工作区外普通文件访问 | cat ../other-project/file、cd ../other-project | Get-Content/Set-Location 指向未授权目录；可由用户另行授予路径范围 |
| Q-O20 | UNC / 网络共享 | //server/share、/mnt 下外部挂载 | \\server\share；即使只读也可能产生网络和身份认证 |
| Q-O21 | 调整运行环境 | export PATH、env FOO=... command、改变 executable 搜索路径 | 修改 $env:PATH、Set-Alias、Import-Module；不能复用原 executable 的授权 |
| Q-O22 | 带外部执行/写入参数的查询工具 | find -exec、rg --pre、sort -o、git -c、自定义 pager | 带脚本块的管道、计算属性；对子命令继续检查 deny |
| Q-O23 | 凭据以外的系统信息与状态 | systemctl status、mount 查询、系统配置读取 | Get-ComputerInfo、注册表查询、完整系统诊断；无明确 allow 就 ask |
| Q-O24 | 无法静态确定的命令 | 动态程序名、复杂展开、未支持的语法 | 参数 splatting、动态命令名、不可确认的别名、provider 或脚本调用 |

未知程序、未知子命令和未登记参数均由默认 ask 覆盖，不需要维护一份永远穷举不完的 ask 名单。

## 5. deny：默认禁止名单

deny 针对动作及目标，不是简单禁止某个程序。普通 rm/Remove-Item 为 ask；删除系统根目录或工作区根目录才是 deny。用户可以在 agent 之外主动调整权限配置，但不能在某次命令提示中绕过 deny。

| ID | 禁止动作 | Bash 示例/目标 | PowerShell / Windows 示例/目标 |
|---|---|---|---|
| D-01 | 删除系统、用户或项目根目录 | 对 /、/home、用户 home、整个工作区执行递归删除 | 对 C:\、D:\、Windows、Users、用户目录、整个工作区执行递归删除 |
| D-02 | 格式化、擦除或破坏磁盘 | mkfs；写块设备的 dd；wipefs | format；diskpart clean；Clear-Disk；Format-Volume |
| D-03 | 删除/改写重要系统文件 | /etc、/boot、系统程序安装目录 | Windows/System32、启动配置、系统注册表配置 |
| D-04 | 读取或导出真实凭据 | .env、私钥、云凭据、认证 token 文件、密码库 | 同左；Credential Manager 凭据、浏览器登录数据、私钥证书 |
| D-05 | 批量输出环境变量或明显的 secret 变量 | env、printenv 全量；输出 TOKEN/PASSWORD/SECRET 等变量 | Get-ChildItem Env:；输出 API_KEY/TOKEN/PASSWORD 等变量 |
| D-06 | 上传受保护文件/变量 | curl --data-binary @.env；传输私钥或凭据目录 | 将凭据文件/变量作为请求体或上传内容 |
| D-07 | 下载后直接交给解释器执行 | curl URL \| bash；wget 输出直接送给 sh | Invoke-WebRequest 内容直接交给 Invoke-Expression/IEX |
| D-08 | 修改权限策略以给自己增权 | 改写本项目权限文件、agent 用户权限配置 | 同左；改写审批记录或受保护权限配置 |
| D-09 | 破坏 agent 的审计和状态 | 修改/删除受保护审批记录、审计数据、状态数据库 | 同左；agent 自身受控持久化不通过模型工具调用完成 |
| D-10 | 写入自动执行入口 | 修改 shell 启动配置、设置任意自动执行 Git hook | 修改 PowerShell profile、启动文件夹或自动执行配置 |
| D-11 | 提权 | sudo、su、doas | runas；Start-Process -Verb RunAs；系统提权入口 |
| D-12 | 关闭系统安全措施 | 修改防火墙、安全服务、验证机制 | 关闭 Defender/防火墙/UAC；永久放宽执行策略 |
| D-13 | 持久化系统任务或服务 | crontab 写入；创建系统服务 | Register-ScheduledTask；schtasks 创建；New-Service |
| D-14 | 任意代码型容器扩大主机权限 | docker --privileged；挂载整个 /、凭据目录或主机管理 socket | 挂载整个系统盘、用户 home、凭据目录、主机管理接口 |
| D-15 | 大范围停止进程/系统关机 | kill -9 -1；fork bomb；shutdown/reboot | Stop-Process 不加区分批量结束；Stop-Computer；Restart-Computer |
| D-16 | 编码隐藏执行内容 | 用解码器隐藏后直接执行的 shell 程序 | powershell/pwsh -EncodedCommand；解码内容直接送给 IEX |
| D-17 | 修改全局账户、权限和远程登录 | useradd/userdel；系统认证配置；大范围 chmod/chown | net user 管理；账户创建删除；系统 ACL/远程登录策略变更 |
| D-18 | 明确的大范围外部数据销毁 | 清空生产数据库、删除整个生产集群或业务存储 | 对明确生产资源执行同类全量销毁 |

这是一份默认阻止策略；不声称通过字符串或 AST 能识别任意脚本内部的所有上述行为。对于执行内容不透明的脚本，只能先 ask。真正阻止已经获准脚本访问 secret 或系统目录，需要限制其实际执行环境。

## 6. 路径、参数和输出规则

路径检查适用于 shell 与现有文件工具，不允许通过换一种工具绕过同一文件保护。

| 资源/行为 | 默认等级 | 说明 |
|---|---|---|
| 工作区普通源码、测试、文档读取 | allow | 读取前解析路径与链接；搜索时排除受保护路径 |
| .env.example、.env.sample、示例配置 | allow 读取 | 明确登记为模板且不含真实凭据，不用 .env* 一刀切 |
| .env、.env.local、.env.production 等真实环境文件 | deny 读取/搜索/输出；ask 创建或修改 | 修改不得先把原凭据内容回传模型；不能放行整个文件读取 |
| ~/.ssh 私钥、~/.aws/credentials、认证配置 | deny | known_hosts、公共配置等非凭据文件可单独登记 ask |
| secrets 目录、明确的私钥/凭据文件 | deny 读取/输出 | 按完整路径和内容用途登记；不能把所有 .pem 都当私钥，公开证书可单独分类 |
| 权限配置、审批记录、受保护 agent 状态 | deny 模型写入 | 内部受控持久化与备份是系统功能，不授予模型改写权限 |
| .git/hooks、可自动执行的 profile | deny 写入 | Git 元数据查询可 allow，但不能直接覆盖自动执行入口 |
| AGENTS.md、普通项目配置、依赖和锁文件 | ask 写入 | 改动执行相关配置可能使会话批准失效 |
| 工作区普通文件写入、移动和删除 | ask，直到存在明确授权规则 | 不因已有 cwd 校验就自动放行 |
| 未授权工作区外普通路径 | ask | 显式新增允许目录后才按新的边界评估 |
| 系统目录、磁盘根、用户目录整体修改 | deny | 单个非敏感的工作区外用户文件可 ask，不把用户目录所有操作一概禁止 |
| UNC 路径、网络共享、远程 provider | ask | 本地文件只读规则不能自动批准网络访问 |
| 重定向 >、>>、tee、Out-File | 至少 ask | 对解析后的写入目标另行评估；目标命中 deny 则整条 deny |
| 纯文件描述符合并、/dev/null | 不单独升级等级 | 例如 2>&1；仍检查其余命令和输入/输出目标 |
| 输入重定向、here-doc、here-string | 检查输入内容和接收程序 | 普通数据不自动 deny；送入解释器属于执行，按 ask/deny 规则判断 |
| 命令替换 $()、反引号、脚本块 | 递归评估；解析不了 ask | 外层 echo/Write-Output 的 allow 不覆盖内部执行 |

所有文件名通配结果都要检查；展开可能产生新的参数时不能按原字符串放行。读取受保护文件的 deny 也应覆盖 git show 的历史文件目标和文本搜索工具。

日志只记录决策、规则 ID、经过脱敏的命令和路径。不要将 API key、密码或原始 secret 文件内容写进审批日志、提示界面或模型上下文。

## 7. 会话批准如何复用

批准记录绑定 session_id、shell 类型、解析后的 executable、工作区、命令/入口、允许参数和策略版本。内联代码批准还绑定完整代码摘要；不能把 python -c 的一次批准扩大为任意 Python 代码执行。

| 情况 | 是否复用 |
|---|---|
| 同 session、同 shell、同项目，重复获准的 pytest 入口 | 可以 |
| 对正常源码/测试作修改后重跑同一个获准测试入口 | 可以；批准提示明确包含这种预期改动 |
| 增加未获准参数、测试范围或新入口 | 不可以；再次 ask |
| Bash 改为 PowerShell，或切换 executable/cwd | 不可以 |
| 项目脚本入口、依赖、锁文件、插件或 hooks 配置变更 | 不可以 |
| 用户新增 deny 或 ask 每次确认规则 | 立即重新评估，旧批准不能覆盖 |
| 新 session；从持久化状态 resume 到同一 session | 新 session 不复用；进程退出后批准失效，恢复同一 session 也重新确认；仅审计持久化 |
| git push、发布、外部写操作、危险删除 | 每次 ask，不复用 |
| 未知命令或分析失败 | 默认单次批准；不生成整程序的会话 allow |

审批界面至少显示：shell、完整命令、实际 cwd、可解析的读写目标、网络目标、匹配规则/原因，以及单次和当前会话两种选项。测试会话批准明确说明会执行项目代码及后续修改后的测试代码。

用户拒绝后向模型返回结构化拒绝结果，不执行任何命令；模型可以调整方案或解释阻碍。非交互、输入取消或无法显示批准界面时，ask 不执行，返回需要批准的结果，不能默认为 yes。

原有 --yes 不能作为跳过 shell ask 或 deny 的通道；如保留该参数，需要明确限定用途并在统一权限评估之后使用。

## 8. 实现时需要遵守的边界

1. 模型工具命名沿用现有小写风格：bash、powershell。输入至少含 command、可选 cwd 和 timeout_s；权限等级由程序判断，不接受模型自报 allow。
2. Bash 与 PowerShell 的 executable 由系统配置选择，不能让模型替换。Bash 用无 profile 的非交互执行，PowerShell 用 -NoProfile -NonInteractive，避免自动加载用户启动脚本。根据已安装后端暴露工具。
3. Windows 原生 shell 的可用性与 WSL/Git Bash 的路径模型要分开处理，不能把 /mnt/d、/d 和 D:\ 当作普通字符串等价。当前进程 PATH 可找到 pwsh 和 powershell，未发现 bash；这不证明机器上没有 WSL 或 Git Bash。
4. 不用 shlex.split 或字符串分号切割实现 shell 权限解析。Bash 使用成熟的语法解析器，PowerShell 可使用官方 Parser.ParseInput 获取 AST、tokens 和解析错误。[Microsoft PowerShell Parser 文档](https://learn.microsoft.com/es-mx/dotnet/api/system.management.automation.language.parser.parseinput?view=powershellsdk-7.5.0)
5. 规范化 executable、大小写、常见别名、参数形态和路径，但规范化不能执行被检查的命令，也不能先展开会运行代码的表达式。
6. 解析管道、条件链、命令替换、子 shell、循环体、脚本块、重定向和包装器。分析覆盖不了的结构走 ask。对声明为 allow 的参数集合实行正向校验。
7. 搜索/读取工具、write/patch/delete、旧 run_command/python_probe 和模型选择的测试都走同一权限策略。verify 仅做不执行项目代码的静态检查，不自动运行测试，也不提供免审批执行通道。
8. 权限评估应返回 decision、rule_id、reason、批准复用范围和解析后的影响。现有 human_confirm 节点可扩展承接 ask；拒绝、deny 和执行失败仍需保留完整 tool_call_id 配对。
9. 配置使用默认 default=ask 和 allow/ask/deny 规则集合。项目提供的授权先由用户信任；模型写入的新规则不能立即给自己提权。用户/项目多个来源的 deny 都不能被更低限制来源覆盖。
10. 本项目目前只校验 cwd 和 executable 白名单，并没有 OS 级 shell 文件/网络隔离。实现权限引擎时必须如实报告隔离状态；执行获准解释器或脚本不能被宣传为限制在工作区内。
11. 子进程环境只继承明确需要的变量，不默认继承 agent 的 OPENAI_API_KEY 等凭据。构建/测试需要的业务凭据另行授权并避免输出；环境脱敏不能代替文件访问隔离。

本文不建议第一版就承诺无提示地运行任意测试/构建。按用户选择，先实行会话内的受限批准复用，再独立建设能约束子进程的执行环境。

## 9. 权限判定验收示例

以下等级为没有额外用户授权、执行路径可信且路径已解析时的结果。表中命令仅为测试输入，不应在真实机器上执行高危示例。

| 输入/场景 | 预期 | 原因 |
|---|---|---|
| bash: git status --short | allow | 已登记的本地状态查询 |
| powershell: Get-Content ./app/agent.py | allow | 工作区普通源码读取 |
| powershell: gci ./app | allow | 归一化为 Get-ChildItem 后符合文件查询规则 |
| bash: git status && pytest -q | ask | 第二个子命令首次执行项目测试 |
| 当前 session 已批准 pytest -q 后重复上一条 | allow 执行 | ask 子命令的批准记录有效，其余子命令 allow |
| 同一批准后改用 pytest -q --某个未登记参数 | ask | 批准不覆盖新增参数 |
| bash: git status; rm -rf ./dist | ask | 有文件删除，不继承 git status 的 allow |
| bash: git status; rm -rf <工作区根目录> | deny | 任一子命令命中根目录删除规则 |
| powershell: Get-ChildItem; Remove-Item <工作区根目录> -Recurse | deny | 别名、链式执行均不能绕过目标规则 |
| bash: cat .env | deny | 真实凭据文件读取 |
| powershell: Get-Content .env.example | allow | 登记为无凭据模板 |
| bash: rg pattern ./app --pre some-program | ask | 搜索中执行外部程序 |
| bash: find . -name '*.py' -print | allow | 已登记的普通文件遍历；无需把输出当参数执行 |
| bash: find . -exec some-program {} \; | ask | 显式执行外部程序；内部已知 deny 仍可提升为 deny |
| bash: git diff --output=result.txt | ask | 查询命令带文件写入参数 |
| powershell: Get-ChildItem Env: | deny | 批量环境变量暴露 |
| powershell: Write-Output $(Remove-Item <工作区根目录> -Recurse) | deny | 递归检查子表达式 |
| powershell: Invoke-WebRequest URL | ask | 联网；URI/上传范围需批准 |
| powershell: Invoke-WebRequest URL 的内容送给 IEX | deny | 下载后直接执行 |
| bash: python -c '<任意代码>' | ask | 不因 python 程序名而自动批准 |
| bash: npm run build | ask；批准后可会话复用 | 执行项目构建脚本 |
| bash: 未知命令 --version | ask | 不用宽泛版本匹配放行未知程序 |
| powershell: & $dynamicCommand | ask | 无法确认执行程序 |
| 权限配置增加了 deny，旧 session 有对应测试批准 | deny | 即时限制优先于批准缓存 |
| 模型选择的测试遇到首次 ask | 等待批准，不执行 | 没有测试专用免审批通道 |

## 10. 调查结论与实施范围

建议将默认自动能力限制为已理解的本地查询，项目代码执行采用本 session 的受限批准，高影响外部写入每次询问，明确禁止凭据暴露、根目录破坏和 agent 自行修改权限。

已接入统一权限评估、shell 后端、人工确认、自动静态检查和 SQLite 权限审计。模型只看到文件工具及已安装的 bash/powershell，测试由模型选择；旧自动测试队列恢复时闭合而不启动。此策略不提供 OS 隔离或保证测试没有副作用。旧执行工具适配器继续用于恢复历史记录。权限配置示例见 permissions.example.json。

首版不提供 OS 沙箱、后台任务或任意 shell 修改的备份。复杂脚本只能 ask；经批准的代码仍可访问当前用户有权访问的文件和网络。静态识别出的 deny 会阻止整条命令，但不能保证识别所有动态程序内部行为。

## 11. 分页归档权限

新增只读 `read_tool_result`，默认允许读取当前 session 已完成、脱敏的结果；显式用户 ask/deny 优先。工具只接受结果 ID、stdout/stderr 和行列范围，不接受文件路径、SQL 或其他 session ID。来源文件重新检查当前路径权限，目录/搜索归档按当前策略检查来源路径；凭据或受保护状态不得通过归档入口读取。普通工具仍不能直接读取或修改 `.agent` 数据库。

分页 cursor 绑定原查询、实际目录和结果版本，不重新执行原命令。归档不足时明确标记缺失，恢复不重跑原工具。压缩服务 API key 同样登记脱敏；tokenizer、预算及压缩服务配置由用户设置，模型不能通过归档入口修改。
