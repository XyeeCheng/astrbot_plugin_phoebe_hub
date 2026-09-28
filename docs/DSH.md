# DeepSeek Harness 接入

## 当前实现

AstrBot插件 → 带认证的HTTP Bridge → 一次性Python worker → 官方DSH SDK/runtime → 最终文本 → Hub短句出口。

SDK与runtime固定 `0.1.5rc1`，参考的官方源码提交为 `4878cdabd87d4041bdaff61d04c966883b9fd07a`。启动检查安装版本，不自动漂移。

采用 `sdk-minimal` 独立配置树，并显式禁用 `persistent-bash`、`persistent-pwsh`、额外会话日志上传及插件清单上传，沙箱策略设为只读。仅注册本项目 `phoebe_time` 工具。官方示例原配置带终端，不能去掉这些覆盖后直接暴露给群聊。

每次请求使用独立临时DSH home/session，结束删除；对话历史由Hub提供。这样避免重复保存一份长期聊天和删除时两边不同步，代价是每次启动runtime的开销。模型名、Key、base URL在服务端配置，不接受群消息修改。

Bridge `/health` 只代表服务存活；工具真正可调用由 `tests/smoke_dsh.py` 使用实际runtime验证。该测试通过本地模拟模型发起工具调用，检查模型只看到 `phoebe_time`、取得真实时间，再产出回答；不是实际DeepSeek线上请求。

## 后续加入DSH插件

DSH原生插件和AstrBot插件不是同一种安装包。1.0.0固定加载内置时间插件，不提供群聊安装命令或任意插件自动加载。

开发接入流程：

1. 确认插件适配固定DSH版本、无需Web侧边栏，并检查host代码和依赖。
2. 在独立Bridge镜像中安装依赖，修改 `worker.patch_text()` 加载受控插件行；不要在群聊中接收文件路径或任意patch。
3. 给新增工具定义只读范围、参数约束、超时及取消行为。需要文件修改、发送消息的插件另做权限流程，不混入普通群聊配置。
4. 更新 `smoke_dsh.py` 的工具清单断言，加入真实插件调用的本地测试；再次核对终端和管理工具没有出现。
5. 实际模型/接口和指定群验证通过后，再固定新版本发布。

内置 `clock.mjs` 使用公开 `ToolDefinition` 注册接口，没有裸导入DSH工具包；SDK独立运行时不会向外部ESM模块暴露内嵌npm依赖，这是实测后的兼容处理。

标准MCP工具也可以接入，但必须新增明确的DSH MCP配置并单独测试。既有AstrBot工具当前不经Bridge暴露；使用native模式可继续调用已允许的Tavily/HLTV工具。

## 参考资料

- [DSH 官方Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md)
- [DSH sdk-minimal配置](https://github.com/deepseek-ai/deepseek-harness/blob/4878cdabd87d4041bdaff61d04c966883b9fd07a/packages/bundle/sdk-minimal/cordis.patch.yml)
- [公开工具注册接口](https://github.com/deepseek-ai/deepseek-harness/blob/4878cdabd87d4041bdaff61d04c966883b9fd07a/packages/core/tools/src/index.ts)
