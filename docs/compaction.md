# 原版压缩与状态记录

`auto-compaction-recovery` 0.2.0 恢复 Claude Code 的原版压缩调度：插件对每次压缩只调用一次 `next(e)`，原样返回结果或错误。原版负责摘要、重试、预计算、取消和历史保存。名称保留，以便已有安装直接升级。

0.1.x 在原版内部重试之外再追加最多三次，并在主窗口空闲时补做压缩。这会放大网络故障的等待时间。**0.2.0 删除了追加重试、空闲定时器和主动压缩调用**，也不额外查询额度或提交“继续”。它不再承诺恢复原版熔断后的压缩；持续的网络错误仍需要处理网络或上游问题。

## 状态含义

插件只记录诊断元数据，按会话 ID、主窗口/子代理、触发类型分别存储，避免多个窗口覆盖同一条 `last-compaction`。记录包括开始时间、耗时、阶段和原版提供的 token 数；不保存提示词、摘要、原始错误或凭据。

- `running`：这次原版压缩仍在执行。
- `prepared`：后台预摘要已准备好，尚未表示它已替换当前上下文。
- `complete`：原版前台压缩返回成功。
- `failed` / `cancelled` / `skipped`：失败、用户取消或原版/hook 跳过。

后台预摘要失败时，提示明确标注“后台预摘要”，不会再暗示前台压缩已经停止。错误只分为连接中断、超时、额度限制和其他请求失败；这些分类不能单独判定问题出在代理还是服务端。关闭插件后，原版仍负责所有压缩。

## 400K 与状态栏

窗口设置继续由原版解析：环境变量、命令行、模型配置和设置的优先级不变。它是自动压缩窗口，不是强制 token 上限。

例如原版 2.1.296 配置 400000 tokens 时，还会预留输出和压缩空间，常见实际触发点约为 367000 tokens。状态栏的 `context 36%` 按模型的 1M 窗口计算；`0% until auto-compact` 表示已接近/到达自动压缩触发点，**不是压缩完成百分比**。

`API_TIMEOUT_MS` 限制请求，不是整次压缩的总时限；原版内部仍可能重试或使用备用摘要方式。插件不另加一个可能留下后台请求的强制取消计时器，也不改变网络超时。

## 安装和已有窗口

需要支持官方 [mods API](https://code.claude.com/docs/en/plugins/mods/overview) 的 Claude Code 2.1.287+。安装器复制本地插件，新启动的 `claude-auto` 加载该副本；已通过插件管理器安装时不会重复加载 inline 副本。

升级源码安装：

```bash
python3 scripts/install.py
```

已经通过本地 marketplace 安装的插件还需要更新缓存。在与目标窗口一致的 `CLAUDE_CONFIG_DIR` 下运行：

```bash
claude plugin marketplace add "$HOME/.local/share/claude-auto"
claude plugin update auto-compaction-recovery@auto-account-tools
```

然后在目标窗口执行 **`/reload-plugins`**。官方[加载说明](https://code.claude.com/docs/en/plugins/mods/overview#install-or-update-a-mod)要求在已有窗口重新加载；只替换文件或修改 `enabledPlugins` 不能证明正在运行的 hooks 已更新。如果该窗口仍在等待压缩，可由用户按 Esc 取消本次等待后重新加载，保留原会话；不要使用 `/clear` 丢弃上下文。

在 `/plugin` 确认版本为 **0.2.0**。也可以在该窗口通过 `/plugin disable auto-compaction-recovery@auto-account-tools` 关闭插件。未通过插件管理器安装时，`CLAUDE_AUTO_COMPACTION_RECOVERY=0 claude-auto` 可关闭启动器的自动加载。

## 验证

```bash
node --test tests/test_compaction.mjs
python3 -m unittest discover -s tests -v
claude plugin validate plugins/compaction-recovery
```

回归覆盖原样传递、失败不追加请求、取消、hook 否决、后台预摘要与前台并行、会话隔离，以及诊断失败不改变原版结果。

2026-10-12 使用原版 2.1.296、独立配置和回环模拟 API 做了对照：400K 窗口下，正常摘要三种版本均为一次请求；持续摘要失败时，原版为三次，0.1.1 为十二次，0.2.0 回到三次。子代理和注入连接重置场景也与原版请求数量一致。故障数据和 token 用量由模拟 API 注入，不消耗真实订阅额度；这验证了插件不会放大重试，不代表真实网络不会失败。
