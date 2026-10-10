# 安装与配置

## 文件布局

默认用户安装不需要 `sudo`：

| 路径 | 内容 |
| --- | --- |
| `~/.local/bin/claude-accounts` | 账号管理与切换控制器 |
| `~/.local/bin/claude-auto` | 控制器的交互入口 |
| `~/.local/bin/claude-quota` | 额度台入口 |
| `~/.local/share/claude-quota-monitor/` | 额度台 Python 和静态资源 |
| `~/.local/share/doc/claude-auto/` | LICENSE 与 NOTICE |
| `~/.claude-accounts/accounts.json` | 本机账号目录注册表 |
| `~/.claude-accounts/state/` | 额度缓存、运行事件、私有上下文备份与重置卡操作记录 |

`scripts/install.py` 只复制明确的源码清单，不初始化账号、不修改官方 Claude 可执行文件、不启停服务。重复运行会更新程序文件。请在工作会话退出后更新；已经运行的控制器不会自动加载全部新代码，且 hook 子进程会使用磁盘上的文件。

安装到其他目录：

```bash
python3 scripts/install.py --prefix /path/to/local
/path/to/local/bin/claude-accounts init
/path/to/local/bin/claude-quota
```

`--systemd` 仅支持默认 `~/.local` 前缀。自定义路径请根据 `examples/claude-quota-monitor.service` 编写自己的用户服务单元。

从源码直接运行也可以：`./bin/claude-accounts`、`./bin/claude-auto`、`./bin/claude-quota`。

## 账号目录

通过环境变量指定注册表和运行状态的根目录，启动器与额度台均遵守此设置：

```bash
export CLAUDE_ACCOUNTS_HOME="$HOME/.local/state/my-claude-accounts"
claude-accounts init
claude-quota
```

该设置不改变已有 `main` 的 `~/.claude` 路径。不要把状态根目录设为源码仓库；它包含个人数据，不应提交。

额度台也支持 `--root /absolute/path`，优先于环境变量。`--adapter /path/to/claude-accounts` 可以指定配套启动器；`claude-quota` 包装命令会自动选中相邻的版本。

## 额度台参数

```bash
claude-quota --bind 127.0.0.1 --port 8794 --cwd /path/to/trusted-project
```

`--cwd` 默认为启动目录，是执行只读 `/usage` 探测的位置。如果客户端提示首次初始化或目录信任，请先用 `claude-accounts use PROFILE` 在相同目录完成官方交互设置。查询不发送模型提示；不会自动接受信任或权限提示。

局域网访问可显式使用你机器上已有的私有 IPv4 地址，例如：

```bash
claude-quota --bind 192.168.1.5 --port 8794
```

默认只接受与绑定地址、端口匹配的 HTTP Host；回环监听额外允许 `localhost`。写操作还要求同源 Origin 和 JSON 内容类型。拒绝 `0.0.0.0`、公网、IPv6 和任意域名绑定。这些约束用于减少误暴露和跨站请求，不提供用户认证；需要远程访问时优先使用 SSH 转发。

`--once` 会查询后输出本地快照 JSON（**含账号邮箱**），用于本机诊断，不要将输出直接贴到公开 Issue。`--interval` 目前保留用于兼容旧命令；闲置账号不会按此参数周期性执行官方查询。

## 代理与登录

子进程继承当前环境的 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` 及小写形式。配置代理且未显式指定任一超时时，默认设置 `API_TIMEOUT_MS=300000`（5 分钟请求预算）和 `CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS=180000`（3 分钟首响应基准）。原版还会为大请求增加上传时间，并把首次等待和无响应后的重试限制在请求预算以内。

若环境中已设置上述任一变量，启动器保留原值，另一项交由原版解析，避免隐式上限截断显式配置。直连保持原版默认值。已有窗口可在共享用户 `settings.json` 的 `env` 中设置这两个变量，让后续请求使用新配置；已发出的请求保留原来的计时器。systemd 用户服务不一定继承终端环境，按需要在服务 override 中单独配置。

订阅模式会拒绝影响认证来源的环境变量，例如 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_PROFILE` 和云供应商模式开关。在当前终端明确取消这些覆盖后，再使用官方登录流程。

OAuth 刷新由官方客户端负责。启动器等待官方刷新锁，不删除锁、不改写凭据、不导入浏览器 Cookie。

## 重置卡

“查询重置卡”读取账户身份、额度和服务端 grant 信息。“使用”先生成三分钟有效的确认，再在提交时重新核对身份、grant、额度和冷却条件。

模块依赖未公开的接口：

- `GET https://api.anthropic.com/api/oauth/profile`
- `GET https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1`
- `POST https://api.anthropic.com/api/organizations/{organization_id}/reset_rate_limits`

这些路径不是稳定公开 API 承诺。查询失败不会自动改用其他域名；跳转被拒绝。兑换前将一次性操作意图持久化，超时或结果不明时保留“待核对”状态，不会自动重发。手动备注不能授权一次真实兑换。

## 外部长期监视脚本

只有已经由独立 systemd 用户服务运行的脚本，才适合登记为非阻塞监视器。在账号根目录创建私有 `persistent-watchers.json`，数组每项包含：

| 字段 | 含义 |
| --- | --- |
| `cwd` | 脚本工作目录的绝对路径 |
| `argv` | 完整参数数组，第一项为解释器，第二项为脚本路径 |
| `script_sha256` | 当前脚本内容的 SHA-256 |
| `service` | 已运行的 `claude-*.service` 用户服务名称 |
| `log` | 本机日志绝对路径 |

控制器要求参数、工作目录、脚本哈希和 `systemctl --user is-active` 全部匹配；它不会执行会话文本里提供的监视器命令。此功能不能使原版 Artifact 评论监视器自动恢复。

## 卸载

如启用了服务，先停止并禁用它，退出工作会话后删除安装的三个命令、额度台程序目录与用户服务文件。账号目录和 `state/` 不由卸载自动删除，便于恢复工作；确认不再需要后自行处理。这些目录不能放进公共仓库或公开日志。
