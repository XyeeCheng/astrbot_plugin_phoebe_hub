# Ubuntu 部署

## 一、先装普通模式

1. 在**菲比**的 AstrBot 管理页从本仓库 URL 安装插件，保存配置并重载。
2. 默认 `engine=native`，`provider_id` 留空，复用菲比当前模型；默认傲娇程度为3。
3. 首次在指定测试群 @菲比说一句正常话，再用“菲比 好感度”“菲比 系统状态”检查；本项目不会自动向群发送测试。
4. 需要限定范围时，把“系统状态”显示的完整会话标识填入 `allowed_sessions`。
5. 若安装 Proactive Chat，最多等15秒后检查 `主动聊天适配：active`。原插件的目标、调度、免打扰设置仍由其自身管理；Hub只适配输出。

本插件不覆盖旧人格、不改原游戏库和赛事订阅。更改配置后重载插件，`False`、`0`和空Token按实际保存值生效。

## 二、可选部署 DSH

普通模式不需要本节。Bridge 仅提供文本 DSH 对话和内置时间工具，运行在单独容器，不另行登录QQ。

在独立工作目录获取本仓库，不放入奶龙或其他机器人的目录。下方仅操作本项目服务。

```bash
git clone https://github.com/XyeeCheng/astrbot_plugin_phoebe_hub.git
cd astrbot_plugin_phoebe_hub/deploy
cp .env.example .env
```

检查菲比容器所在的实际 Docker 网络，填入 `.env` 的 `PHOEBE_NETWORK`；同时填写你已确认可用的 `DSH_MODEL`。不是 Docker 内运行 AstrBot 时，请自行将服务端口限制在回环地址后连接，不使用下面的内网主机名。

```bash
docker inspect phoebe-astrbot --format '{{json .NetworkSettings.Networks}}'
sudo python3 init_secrets.py
```

`init_secrets.py` 隐藏输入模型Key，生成Bridge随机口令，不输出密钥、不覆盖已有文件。以sudo执行时将凭据文件所有者设为容器UID 10001。不要把 `.env` 或 `secrets/` 上传到GitHub。

```bash
docker compose --env-file .env -f compose.yaml config --quiet
docker compose --env-file .env -f compose.yaml up -d --build
```

Compose复用现有网络，不发布公网端口，不挂载宿主机目录或Docker socket。资源限制为起始配置：单并发、768MB、1CPU；生产机器资源需现场核对，不宣称该值适合所有主机。

在菲比插件配置填写：

```json
{
  "engine": "dsh",
  "dsh_url": "http://phoebe-hub-dsh:8099",
  "dsh_token": "在本机填写 secrets/hub_token.txt 中的值",
  "native_fallback": true
}
```

`dsh_token` 是Bridge访问口令；模型Key只在DSH容器凭据文件。两者不能互换。默认每日最多200次Bridge请求（UTC日界），失败尝试同样占额度，单次超时会清理worker及runtime进程组。

## 三、上线验收

管理员在目标测试群逐项检查：

- 普通对话只有一条、正文一到两句；故意要求长回复也被压缩。
- 两个群友分别查好感度，不串记录；重载后分数保留。
- “妈妈”触发，“我妈妈来接我”不触发；道歉后恢复。
- 主动消息适配状态为active，真实主动消息仍为一条短文本。
- `a一把`题目/答案、弗一把、赛事查询仍完整。
- DSH启用后查询北京时间，实际工具返回正确；断开DSH服务后回退仅一条。

健康进程、离线测试、真实API成功、群内收到分别记录；没有真实Key/目标群测试时，后两项保持未验证。

## 四、停用与回滚

- 在AstrBot后台停用Hub：恢复原LLM对话流程，主动聊天方法包装会解除；原人格与旧插件配置没有被改写。
- 只停可选DSH：在本仓库 `deploy` 目录执行 `docker compose --env-file .env -f compose.yaml stop dsh`；不要停整个服务器Docker。
- Hub数据在框架插件数据目录内的 `hub.sqlite3`；不要删除整个 `plugin_data`。
- 可用管理员命令创建在线备份。恢复数据库前停用Hub；建议恢复到单独目录检查，之后处理现有删除标记再替换，见[数据文档](DATA.md)。

升级时同时保留配置、数据库与原插件版本。切换人格ID会建立独立关系分区，旧数据保留到明确删除。
