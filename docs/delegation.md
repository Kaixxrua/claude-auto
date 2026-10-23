# 可选跨账号委派

原生 Claude Code 子智能体在同一会话内共享主账号的额度。`account-delegation` 插件增加五个 MCP 工具，用独立的官方 `claude -p` 进程执行明确委派的任务；不会拦截或替换原生 `Agent`。

## 启用

先运行本仓库的安装器，再在已登录的、未停用的账号环境下执行：

```bash
claude plugin marketplace add ~/.local/share/claude-auto
claude plugin install account-delegation@auto-account-tools --scope user
```

已经添加 marketplace 时无需重复添加。已有窗口执行 `/reload-plugins`；如果该版本客户端未重新连接新增 MCP 服务，则重开窗口。通过 `/mcp` 确认 `account-delegation` 的 `accounts` 服务已连接。源码试用可运行 `claude --plugin-dir /path/to/claude-auto/plugins/account-delegation`。自定义安装前缀需将其 `bin` 加入 PATH，以便缓存中的插件找到配套 `claude-delegate`。

也可让任意支持 stdio MCP 的客户端执行 `claude-delegate mcp`。工具来自本机程序，不需要额外 API key 或依赖，不开启 HTTP 监听端口。

## 使用

向主会话明确说明：

> 用跨账号委派工具检查这个工作目录的测试覆盖情况，只读分析，把结论带回来。

| 工具 | 行为 |
| --- | --- |
| `delegate` | 提交 `prompt` 和绝对路径 `cwd`，立即返回 `job_id`，后台选账号并执行 |
| `jobs` | 查看最近 30 个任务的状态、账号和独立会话 ID |
| `result` | 获取最终报告；可用 `wait_seconds: 25` 等待，长报告用 `next_offset` 分页 |
| `resume` | 显式传入续接提示词，对已停止的任务再次选账号，用原独立会话 ID 继续 |
| `cancel` | 取消指定任务，保留已产生的文件修改和会话历史 |

原生客户端可能给工具名增加插件/MCP 前缀。委派工具没有主会话的完整上下文，主会话必须在 `prompt` 中提供目标、约束、已有进度和所需文件路径。返回的会话 ID 不是原生子智能体 ID；请使用这里的 `resume`、`cancel` 管理这些任务。

默认 `mode: read-only`：只开放 `Read`、`Glob`、`Grep`，使用原版 safe mode 关闭插件和 hooks；不开放 shell、文件编辑或 MCP。要执行开发任务，显式选择 `mode: project` 并提供隔离工作树路径。此模式加载现有项目/用户规则，采用 `dontAsk`；需要额外批准而未被现有规则允许的操作会被拒绝，并在结果中显示拒绝数量。它不会自动切到 `acceptEdits` 或绕过权限。计划模式的主会话只应委派只读任务。

两种模式均禁用嵌套 `Agent`、`Task`、`SendMessage` 和外部 MCP。`model` 省略时沿用账号的原版模型配置；`effort` 省略时用策略里的 `effort`，策略也没设就沿用账号设置；默认任务时限为 3600 秒，`max_seconds` 可设为 30–28800。时限只算 Claude 实际运行的时间，选账号、等账号不计；自动续接的几轮累计计算，显式 `resume` 重新计时。

## 账号筛选与恢复

- 排除父会话账号及同身份别名、已禁用账号、未登录账号。不会访问已禁用账号的登录或额度接口。策略设了 `interactive_cap` 时，父会话账号改为限量使用（见下一节）。
- 同时检查 5 小时和周额度。使用不超过 120 秒且身份匹配的完整官方查询；缓存不够新就运行独立 `/usage` 查询。过期重置时间、未知数据、查询错误都不会被当作可用额度。
- 优先尝试记录中剩余额度较多的账号；旧记录只能决定查询顺序，不能授权启动。每次启动前重新检查禁用状态及登录身份。
- 用系统文件锁保证同一账号/同一身份同时只承载一个本工具的任务，工作进程退出自动释放。它无法预留服务端额度，也不会占有或关闭用户已开的其他窗口，因此运行中仍可能碰到限额。
- 任务执行一旦开始，不会从头重放。默认情况下额度耗尽标记 `quota_exhausted`，检查已完成工作后显式 `resume`；打开下面的自动续接后，改为在同一个会话里换账号接着做。续接必须找到唯一且末行完整的历史文件，目标账号必须使用同一共享历史目录。
- 取消只向本任务自己的进程发停止信号；十秒仍未退出才终止它的进程组。取消不会回滚已完成的外部操作。再次续接前会拒绝尚存的原进程，避免同时执行两份。

## 自动化策略（可选）

`~/.claude-accounts/state/delegation/policy.json` 决定账号不够、任务中途停下时怎么办。每次决定前都会重读，改了对正在跑的任务也生效。文件不存在时全部关闭，行为同上一节。

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `wait_for_account` | `false` | 没有可用账号时等待（状态 `waiting`），每 `retry_seconds`（60）秒再选一次，已知的重置时间会提前下一次检查；最多等 `max_wait_seconds`（86400）秒，之后才标 `blocked` |
| `share_when_short` | `false` | 没有空闲账号时，和别的委派任务共用一个仍有额度的账号，挑任务最少的；任务记 `shared: true` |
| `share_wait_seconds` | `0` | 有空闲账号会在这么多秒内重置时，先等它，不共用（需要 `wait_for_account`） |
| `avoid_interactive_accounts` | `false` | 不选正开着交互窗口（由 `claude-accounts` 启动）的账号 |
| `auto_continue` | `false` | 因额度、登录或订阅错误、服务端错误、进程意外退出停下时，在同一个会话里换账号接着做，最多 `max_continues`（8）次；刚停下的账号 `avoid_seconds`（900）秒内不再选，服务端错误按 `transient_seconds`（30）秒起指数退避 |
| `enforce_model` | `false` | 任务指定了 `claude-*` 模型时，出现别的模型的回复就立刻停下（`model_mismatch`），再按自动续接换号，续接提示要求先撤销那一轮的改动 |
| `effort` | 不设 | 委派会话的思考强度（`low`、`medium`、`high`、`xhigh`、`max`），以 `--effort` 传给客户端。任务自己传了 `effort` 就用任务的；两边都没设，沿用账号设置 |
| `interactive_cap` | `0` | 主会话也在用的账号（任务的父账号，以及开着交互窗口的账号）留给委派任务用到这个百分比。别的账号都不行时才用，同一时间只给一个任务用，不共用。开工要求五小时和本周的用量都不超过它减 10；跑的时候任一窗口的读数到了它，就停下、换号续做（停止原因 `reserve`）。`0` 时这些账号照旧不用 |
| `continue_prompt`、`why` | 英文 | 续接提示的模板（必须含 `{why}`）和各类停止原因的说明，可以换成别的语言 |

自动续接用 `continue_prompt` 告诉会话上一轮停在中途，先核对改过的文件、启动的进程和写了一半的输出，再做完剩下的部分。还没有历史记录的任务（第一轮就没跑起来）重新发送原任务。续接次数用完、历史记录损坏时停下，并在 `auto_stop` 里写明原因，等人处理。

## 监督服务

工作进程本身负责续接。旧版工作进程留下的 `quota_exhausted`、崩掉的工作进程、重启后的机器，由监督服务接上：

```bash
cp examples/claude-delegate-supervisor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-delegate-supervisor.service
```

它每 30 秒查一遍最近 24 小时的任务，只接上没有活着的工作进程或 Claude 进程、没有取消标记、没有 `auto_stop` 的任务。`KillMode=process` 让它启动的工作进程在服务重启时留下来。心跳写在 `supervisor.json`，额度台据此显示服务在不在。

## 任务结束时叫醒会话

`claude-delegate wait` 会一直等，直到发起任务的会话有事要处理才退出：任务完成，或者停下了而且没有东西会接着做（取消、超时、等不到账号、`auto_stop`，或者停下 10 分钟后监督服务仍没接上）。它按 `CLAUDE_CODE_SESSION_ID` 认会话；没记下会话的旧任务，按工作目录认。

- 在会话里用后台任务挂上：`claude-delegate wait --cwd <项目目录>`。它退出时，客户端会叫醒模型。
- 想让每个会话自动挂上，就装钩子：`claude-delegate wake-hook install`。它在各账号的 `settings.json` 里加一个 `SessionStart` 和 `Stop` 的 `asyncRewake` 钩子：会话一开始、每轮结束都挂一个等待程序，有消息时以退出码 2 叫醒模型。指向同一个文件的符号链接只改一次。`--project <目录>` 改为只装进那个项目的 `.claude/settings.local.json`；`status`、`uninstall` 查看、撤销。
- 同一个客户端、同一个会话只留一个等待程序。客户端退出时，它跟着退出。在 print 模式的会话里（包括委派任务自己的会话），它立刻返回。
- 每条通知只发一次。发出去以后客户端退出了（比如换账号重开），下一个等待程序会查会话记录，没收到的就再发一次。会话第一次挂等待程序时，之前已经结束的任务只记下，不通知。

状态、任务提示、完整客户端输出和最终报告保存在 `~/.claude-accounts/state/delegation/`，目录 0700、文件 0600。没有凭据复制或读取，认证和刷新均由官方客户端处理。此目录包含任务私有内容，不能公开或提交；`jobs` 只返回概要，`result` 返回最终报告，不把完整轨迹或登录身份作为工具结果。

选择账号和成功写出报告分别验证；模拟测试不代表真实上游可用。真实测试应报告客户端版本、任务使用的账号、独立会话 ID 和续接结果，避免把原生 `Agent` 的同账号行为误称为跨账号成功。
