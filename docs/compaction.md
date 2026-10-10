# 压缩失败恢复

Claude Code 2.1.296 的原版自动压缩在连续三次失败后，会停止该轮后续尝试；工具循环仍可能继续，因此配置 272K 不代表上下文一定小于 272K。网络、额度、摘要失败和一次较大的工具结果，也会影响压缩时机。不要把状态栏相对模型总窗口的百分比当成相对压缩窗口的百分比。

`auto-compaction-recovery` 使用官方 [mods API](https://code.claude.com/docs/en/plugins/mods/overview)，需要 Claude Code 2.1.287+：

- 主窗口、子代理的自动压缩失败时，在同一个压缩位置额外重试最多三次，分别等待 0.5、1、2 秒。成功后直接继续原任务，不发送“继续”提示。
- 重试成功会显示“压缩已恢复”和前后 token 数；`1/3`、`2/3`、`3/3` 表示追加重试的序号，不是成功通知。等待时间还包含原版摘要请求及其内部重试，不能把上述退避时间当成总耗时。
- 主窗口正常结束一轮后，如果官方读取的输入 token 数仍超过显式配置的压缩窗口，使用官方 `$.session.compact()` 补做一次；空闲检查每 30 秒运行，同一窗口最多每五分钟发起一次补救。
- 使用原版解析的窗口，支持环境变量、命令行和模型配置；不会写死 272K，也不改变用户关闭自动压缩的设置。
- 已确认额度耗尽不追加重试；Esc、API 错误或拒绝结束的回合不触发空闲补救。后台子代理结束不会被当成主窗口空闲。已有 hook 否决保持有效。
- 摘要生成、消息选择和历史持久化仍由原版执行。插件不修改凭据、会话文件或原版二进制，不清空输入框，不终止任务。

这是失败恢复机制，**不是硬 token 上限**。持续的 API 故障仍可能耗尽重试；子代理只有原位重试，官方空闲压缩接口针对主窗口，不能强行压缩正在执行的子代理。重试可能产生额外的摘要请求和用量。未能压缩时保留原上下文并显示失败信息，不伪装成成功。

## 安装与现有窗口

`python3 scripts/install.py` 会同时安装插件。新启动的 `claude-auto` 自动加载本地副本；旧版客户端保留原有行为并提示版本要求。未通过官方插件管理器安装时，可以用 `CLAUDE_AUTO_COMPACTION_RECOVERY=0 claude-auto` 关闭自动加载。

已有 Claude Code 窗口可通过官方插件管理器加载，无需中断工作或重新登录。在与活动窗口一致的 `CLAUDE_CONFIG_DIR` 下运行：

```bash
claude plugin marketplace add "$HOME/.local/share/claude-auto"
claude plugin install auto-compaction-recovery@auto-account-tools --scope user
```

然后在已有窗口中运行 **`/reload-plugins`**。官方[插件加载说明](https://code.claude.com/docs/en/plugins/mods/overview#install-or-update-a-mod)要求这一步；仅替换磁盘文件不等于已运行的窗口加载了修复。在 `/plugin` 查看 active mods 可确认。通过插件管理器安装后，启动器不再加载重复的 inline 副本；需要关闭时使用 `/plugin disable auto-compaction-recovery@auto-account-tools`。

## 验证

```bash
node --test tests/test_compaction.mjs
python3 -m unittest discover -s tests -v
claude plugin validate plugins/compaction-recovery
```

回归测试覆盖失败预算、主窗口和子代理、取消、hook 否决、已耗尽额度、空闲时机、窗口来源和安装兼容性。2026-10-10 还使用原版 2.1.296、独立配置目录、回环模拟 API 进行了客户端实测：未修复时三次摘要失败会打开熔断；修复后第四次摘要成功，主窗口和子代理继续；交互 PTY 中的超窗主窗口在回合结束后完成补压缩。API 用量及失败是注入的测试数据，没有消耗订阅额度；这不代表真实网络永远不会压缩失败。
