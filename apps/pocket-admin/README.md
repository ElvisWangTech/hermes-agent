# Pocket Admin

独立管理项目，不包含 Pocket 客户端，不由 `hermes serve` 挂载。独立构建、进程和监听端口；通过同一个 `HERMES_HOME` 内的 `pocket-auth.sqlite` 与客户端服务共享单机授权状态。

从仓库根目录执行。首次使用先构建本地隔离环境和前端资源：

```sh
./build-local.sh
npm run build --workspace @hermes/pocket-admin
```

之后使用统一启动脚本，脚本会自动 source `.build/activate.sh`，客户端、Hermes 后端和 Admin 共用 `.build/hermes-home`：

```sh
./scripts/pocket.sh setup-admin  # 首次设置或重置 Admin 密码
./scripts/pocket.sh backend      # Hermes API，http://127.0.0.1:9119/
./scripts/pocket.sh admin        # Pocket Admin，http://127.0.0.1:9122/
./scripts/pocket.sh client       # Pocket 客户端，http://127.0.0.1:5176/pocket/
```

三个常驻命令分别在终端窗口中运行。访问 `http://127.0.0.1:9122/`，输入初始化时设置的管理员密码。初始化通过终端隐藏输入读取密码，要求至少 12 个字符；不在聊天、命令参数或环境变量里传密码。摘要保存在 `.build/hermes-home/pocket-admin-auth.json`，权限 0600。重复执行初始化可修改密码，之后重启服务使旧会话失效。没有配置密码时服务拒绝启动，没有默认密码。

客户端另行运行 `hermes serve`，使用不同端口。管理服务没有兑换、聊天、模型、文件或终端 API；客户端没有连接码签发、管理员设备撤销和全部授权停用 API。登录会话最多一小时，使用 HttpOnly、SameSite=Strict cookie，退出立即撤销会话；服务重启需重新登录。五次登录尝试/分钟的全局上限适用于单管理员首版，公网还应加反向代理限速。

远程部署必须在独立受控域名以 HTTPS 提供，启用 `--secure-cookies`，反向代理不得把管理员 API 转发到客户端入口。Cookie 不按端口隔离，仅拆端口不是浏览器凭证隔离；生产环境必须用不同主机名，不能在同一域名下部署不可信客户端内容。默认仅本机 HTTP 用于开发。代理须保留原始 Host 和 HTTPS scheme，服务不接受跨源写请求，不启用 CORS。

本版只能由管理员签发连接码：可选择已有独立命名 profile，也可在签发时创建新的 profile。新 profile 会在创建时复制 default 的主模型和上下文长度设置，但不会复制 `.env`、API 密钥或消息渠道；创建后配置完全独立。管理员可在“Profile 配置”页面修改每个独立 profile 的模型设置。列表不包含码明文或设备令牌。暂不实现用户注册、申请、审批、账号角色分配、审计或集群会话存储。未来账号系统接入后，用户提交申请，签发仍由授权流程控制，不直接放开现有管理 API。单机 SQLite 可以跨本机进程共享，但不能作为跨机器数据库。

Profile 配置还包含 STT/TTS 的基础 provider、模型、声音、语言和识别开关。新 profile 一次性复制这些安全字段，不复制语音密钥或自定义执行命令。主模型 provider 使用下拉，模型名称既可选择目录候选，也可手动填写；候选来自目标 profile 的 Hermes 模型目录。新选择仍需通过 Hermes 模型校验，原模型/provider 不变时不重新校验目录，避免影响其他设置的保存。

API 凭据在同一页面独立保存：选择对应的凭据槽位，输入新 key，点击保存。输入为空不会提交；替换后输入框清空。页面只显示目标 profile 的 `.env` 槽位已配置/未配置，不返回原 key；删除需勾选确认后单独提交。槽位来自 Hermes provider 注册表和语音凭据白名单，不允许任意环境变量写入。OpenAI 语音可使用 `VOICE_TOOLS_OPENAI_KEY`，否则使用 `OPENAI_API_KEY`；相同 provider 的语音和模型可能共用同一个槽位。

Pocket 未配置 Edge voice 时默认使用中文普通话 `zh-CN-XiaoxiaoNeural`（晓晓）。Edge voice 使用下拉选择，提供普通话、台湾中文、粤语及英文常用声音。已有明确保存的声音（包括列表之外的声音）保持不变；其他 TTS provider 的声音字段不受影响。新 profile 的安全语音快照保留显式字段，不因等于 Hermes 默认值而丢失。

凭据写入使用 Hermes 统一生命周期，处理 `.env`、旧配置镜像、凭据池和来源抑制。标准 Chat Completions 客户端沿用已有轮次边界刷新，key 更新在下一轮采用，无需重启，进行中的轮次保留原凭据快照。此保证不扩展到 OAuth、非 Chat Completions 传输、临时 failover 或显式自定义路由；删除文件凭据也不承诺立即取消在途请求或清除已存活客户端。已有外部 secret source/独立凭据池可能继续提供凭据，因此“未配置”只表示该 `.env` 槽位为空。部署本次代码更新需重新启动 Admin；日常保存支持范围内的 key 不需要重启服务。