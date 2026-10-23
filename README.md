# Claude Auto · 额度台与自动续接

[![Tests](https://github.com/Kaixxrua/claude-auto/actions/workflows/test.yml/badge.svg)](https://github.com/Kaixxrua/claude-auto/actions/workflows/test.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![Platform: Linux | Windows](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey.svg)

管理本机多个 Claude Code 订阅账号，在确认额度耗尽后保存上下文、切换可用账号，并恢复原会话。配套网页额度台展示 5 小时 / 7 天用量、重置时间和重置卡。

可选的[跨账号委派工具](docs/delegation.md)可以把明确指定的任务交给有剩余额度的账号，支持查询结果、取消和保留独立会话续接。它不替换原生 `Agent`。

[English](README.en.md) · [安装与配置](docs/configuration.md) · [故障排查](docs/troubleshooting.md) · [安全说明](SECURITY.md)

这是独立社区项目，运行官方 Claude Code 客户端，未附带或修改其程序。仅管理你有权使用的账号；各账号仍受服务端额度、访问权限及服务条款约束。

![额度台界面，使用虚构账号与演示数据](docs/dashboard.png)

*界面示例使用虚构账号、邮箱和额度，不包含实际运行数据。*

## 包含什么

| 组件 | 用途 |
| --- | --- |
| `claude-auto` | 交互式启动器；额度耗尽后切换账号并用原 session ID 续接 |
| `claude-accounts` | 初始化、官方登录、账号停用 / 启用、指定账号运行、检查点恢复 |
| `claude-quota` | 网页额度台；默认地址 `http://127.0.0.1:8794` |

- **保留上下文**：切换前等待工具收尾，备份并校验主会话、子代理历史和恢复元数据。校验不完整时停止切换，保留现场。
- **处理后台任务**：识别已失败子代理与过期的“运行中”记录；支持登记由独立 systemd 用户服务承载的长期监视脚本。
- **合理选择账号**：跳过停用、未登录、重复身份和已知限额账号；完整、近期的官方查询确认额度恢复后，短时间优先选择恢复账号。
- **区分未知与耗尽**：额度台展示数据来源、更新时间、待确认状态；倒计时归零不会直接把旧记录变成“可用”。
- **重置卡**：查询官方余额，核对账号、卡片和有效期后确认使用；不自动消耗卡片，不自动重试结果不明的使用请求。
- **登录冲突恢复**：等待官方刷新锁；针对确切的并发刷新错误最多自动续试两次，用户输入或任务推进会取消等待中的续试。
- **原版压缩**：保持 Claude Code 的原生调度和重试，仅记录主窗口、子代理及后台预摘要的状态，不追加摘要请求；[升级方法及限制](docs/compaction.md)。

## 快速开始

需要 **Linux 或 Windows 10 1809+ / 11、Python 3.10+、已安装的官方 Claude Code，以及交互式终端**（Linux 还需要可用的时区数据库）。核心代码仅依赖 Python 标准库；不需要 Node.js、Docker 或数据库。自动切换需要至少两个不同、已登录的 Claude 订阅账号。macOS 和非交互 `--print` 模式目前不在支持范围；WSL 尚未单独验收。Windows 的差异见下文[在 Windows 上使用](#在-windows-上使用)。

先按[官方说明安装 Claude Code](https://code.claude.com/docs/en/setup)，然后：

```bash
git clone https://github.com/Kaixxrua/claude-auto.git
cd claude-auto
python3 scripts/install.py
export PATH="$HOME/.local/bin:$PATH"

claude-accounts init
claude-accounts setup 2
claude-accounts status

# 在你的工作项目目录里启动
claude-auto
```

`init` 将已有的 `~/.claude` 注册为 `main`，创建独立的 `second` 登录目录。`setup 2` 逐个调用官方登录流程，跳过已登录的位置；需要更多账号可改为 `setup 3`。每次浏览器授权前核对正在登录的身份。

各账号的凭据保持独立；偏好、技能、插件和项目历史通过符号链接共享。因此这些配置**不是账号间的安全隔离边界**。启动器不会复制凭据。账号使用的是官方 [`CLAUDE_CONFIG_DIR` 登录方式](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts)，不支持导入网页 Cookie 或用 API Key 替代订阅登录。

在另一个终端启动额度台：

```bash
claude-quota
```

打开 **<http://127.0.0.1:8794>**。首次采集会逐个查询账号，之后跟随活动会话状态栏；闲置账号可点“刷新额度”查询。重置卡查询是单独操作。

### 在 Windows 上使用

需要官方**原生安装**的 `claude.exe`（[安装说明](https://code.claude.com/docs/en/setup)，默认在 `%USERPROFILE%\.local\bin`）；npm 安装的 `claude.cmd` 不受支持。在 PowerShell 或 Windows Terminal 中：

```powershell
git clone https://github.com/Kaixxrua/claude-auto.git
cd claude-auto
python scripts\install.py
claude-accounts init
claude-accounts setup 2
claude-auto
```

- 安装器把各命令以 `.cmd` 文件放进 `%USERPROFILE%\.local\bin`（官方安装程序通常已把它加入 PATH），并固定使用运行安装器的那个 Python。
- 账号间共享的设置文件需要符号链接：先打开 Windows「开发者模式」，或在管理员终端运行一次 `claude-accounts init` / `add`。共享文件夹在没有该权限时改用目录联接（junction）。
- 启动器通过 ConPTY 代理终端。Windows 没有 SIGTERM，换号时启动器像人一样按两次 Ctrl+C 让原客户端正常退出（输入框里有草稿时再按两次），不会强制结束。
- 官方客户端在 Windows 上按系统时区显示重置时间，额度台据此换算；额度查询同样在 ConPTY 中进行。`--systemd` 仅限 Linux，Windows 上需要时运行 `claude-quota`，或用任务计划程序在登录时启动。

## 常用命令

```bash
# 选择起始账号；原版 Claude 参数放在 -- 后
claude-auto --account second -- --model opus

# 直接运行指定账号，不启用自动切换
claude-accounts use second -- --model opus

# 添加命名账号并完成官方登录
claude-accounts add work
claude-accounts login work

# 停用有问题的账号；正在运行的控制器也会读取此状态
claude-accounts disable main --reason "暂不使用"
claude-accounts enable main

# 只读查看检查点，然后恢复原会话
claude-accounts recovery SESSION_ID
claude-accounts recover SESSION_ID --account second
```

如果所有已登录账号都处于已知等待期，启动器会保留会话并以退出码 `75` 停止，打印恢复命令。它不会无限轮询账号或自动购买、使用重置卡。

## 常驻服务与远程访问

安装可选的 systemd 用户服务文件，再显式启动：

```bash
python3 scripts/install.py --systemd
systemctl --user daemon-reload
systemctl --user enable --now claude-quota-monitor.service
```

服务仍只监听 `127.0.0.1:8794`。在另一台电脑访问时，可以使用 SSH 转发：

```bash
ssh -N -L 8794:127.0.0.1:8794 user@your-server
```

也支持显式 `--bind` 到本机的私有 IPv4 地址，详见[配置说明](docs/configuration.md)。额度台没有登录系统：能访问它的人可以查看账号资料并执行重置卡操作。不要把端口暴露到公网；邮箱遮罩只是显示选项，不是访问控制。

## 兼容性与边界

- 启动器基于官方配置目录、[hooks](https://code.claude.com/docs/en/hooks) 和 `--resume`；初始版本在 Linux、Claude Code **2.1.295** 上做过运行验证，Windows 支持在 Windows 11、原生 Claude Code **2.1.309** 上验证过终端代理、换号续接、钩子与状态栏。没有承诺所有旧版或未来版均兼容。
- 自动切换是交互终端功能。它保留恢复参数和权限限制，拒绝与上下文保护冲突的参数，例如 `--print`、`--no-session-persistence` 和 `--dangerously-skip-permissions`。
- 重置卡使用**未公开、可能变化的 Anthropic OAuth 接口**，属于实验性功能。是否有卡、能否使用均由服务端决定；接口变化时可能无法查询或使用。
- 启动器不读取 `.credentials.json`。额度台的重置卡模块会按需读取现有 OAuth access token，在内存中向固定的 Anthropic 域名发送请求；不会返回给网页、导出或自行刷新令牌。
- 封禁、服务端安全拒绝、普通网络错误和未知错误不会被当成额度耗尽跨账号重试。原版客户端内置的 Artifact 评论监视器等临时能力，不保证能随会话恢复。
- 配置代理时，默认将原版客户端的 `API_TIMEOUT_MS` 设为 `60000`，用户显式设置优先；直连环境不改动。它限制单次等待，重试仍由原版客户端负责。

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
python3 scripts/check_release.py
# 修改前端 JavaScript 时还需 Node.js 做语法检查
node --check dashboard/static/app.js
```

测试使用临时账号目录、伪 CLI、真实 PTY（Windows 上为 ConPTY）和本地 HTTP 服务，不需要真实账号，不请求 Anthropic，不使用真实重置卡。CI 在 Linux 的 Python 3.10、3.12、3.13 和 Windows 的 Python 3.10、3.13 上运行。更多信息见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[Apache License 2.0](LICENSE)。署名与商标说明见 [NOTICE](NOTICE)。
