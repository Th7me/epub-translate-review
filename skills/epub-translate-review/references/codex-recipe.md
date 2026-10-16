# Codex 配置与模型核验

## 默认模型分工

主助手使用 `gpt-6.1-sol`，推理强度 `high`；初译子代理使用 `gpt-5.6-luna`，推理强度 `max`。这记录本工作流的实践配方，不意味着所有账号都可调用，也不作为不同模型质量或成本的比较证据。用户可以明确覆盖配方。

主助手负责资料库、样稿与标准、任务分配、完整回源审校、疑点裁定、术语合并、封装及交付。Luna 负责分配范围的初译和自查，只写自己初稿；自查不代替主审。

## 配置方式

`agents/openai.yaml` 提供技能入口和默认提示，不强制运行模型。主助手由 Codex 当前会话的模型选择或配置决定；skill 不得假称自己已切换模型，也不擅自修改用户全局配置。

在支持当前配置字段的 Codex 中，可将以下设置合并到专用翻译项目的 `.codex/config.toml`，保留已有设置。新会话中核对生效结果；已有会话的显式模型选择和自定义代理配置可能影响结果。

```toml
model = "gpt-6.1-sol"
model_reasoning_effort = "high"

[agents]
enabled = true
default_subagent_model = "gpt-5.6-luna"
default_subagent_reasoning_effort = "max"
max_concurrent_threads_per_session = 2
```

默认子代理设置影响该项目后续创建的代理。若只希望固定翻译角色，使用自定义 `epub_translator` 角色，并在其 TOML 中同时设置 `model = "gpt-5.6-luna"` 与 `model_reasoning_effort = "max"`；该角色文件的配置可优先于默认/显式调用设置。GitHub 分享包的 `examples/codex/` 附有完整示例。不要使用已固定其他模型的角色，再假定文字提示能改写它。

若宿主提供直接的模型/推理强度调用参数，也可显式传递这两个值；以当前实际工具 schema 为准，不臆造参数或通过未授权外部 API 绕行。委派时提供源文、已批准标准、邻近上下文和唯一可写范围，勿依赖未传给子代理的主会话历史。

## 执行前后记录

- 开始时确认主模型、初译模型、推理强度、可用代理机制与本轮范围。记录预期配置；实际运行以可取得的配置加载结果、调用元数据或日志核对，不能用模型自报身份作证明。
- 可验证到模型但验证不到强度时分别记录，不把未知标为已确认；不匹配时先说明差异，不悄悄以继承模型继续并声称配方相同。
- 模型不可用或额度不足时保存已完成产物与剩余范围。没有用户授权的替代配置时，停止依赖该模型的新初译，继续可完成的已有稿审校与整理。
- 并发 2 只是起始示例，按额度、篇幅与主审速度调整。批次长度不固定，主审未完成的积压不能标为已完成。

配置字段与角色机制参见 [OpenAI Docs：Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)，技能安装与入口参见 [OpenAI Docs：Build skills](https://learn.chatgpt.com/docs/build-skills)。账号可用模型与具体客户端支持范围需在运行时确认。
