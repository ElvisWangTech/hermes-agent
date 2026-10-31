# Hermes 分布式改造 · Phase 3：沙箱提供者与路由

> 版本：v3.0 ｜ 状态：待确认 ｜ 前置：[Phase 0](phase0-design.md)、[Phase 1](phase1-design.md)、[Phase 2](phase2-design.md)

---

## 1. 组件

| 组件 | 位置 | 职责 |
|---|---|---|
| 网关 | 控制面 | 准入（Phase 2）、profile → 实例路由、HTTP/SSE/WebSocket 代理 |
| Sandbox Manager | 控制面 | 实例生命周期、租约、挂载与网络参数、密钥注入 |
| 节点代理 | 每个 Cube 节点 | 准备目录、暂停/恢复时拷贝快照、恢复目录 |
| Provider 接口 | 核心仓库 | 只定义 `create / pause / resume / destroy / status / endpoint` |
| Cube provider | **独立包**（仓库外） | 调用 Cube SDK 实现接口，版本与 Cube 兼容矩阵单独维护 |
| 开发 provider | 核心仓库 | 本地进程，无隔离，启动时拒绝在非开发环境使用 |

核心仓库不包含 Cube 代码。这符合 `docs/distributed/README.md` 的 rubric：第三方产品的连接器不进入核心树。

## 2. 实例生命周期

| 操作 | 步骤 |
|---|---|
| create | 节点代理准备目录（属主为租户 UID，权限 0700）→ 若无 `config.yaml`，从默认 home 复制初始内容 → 注入密钥 → 创建 VM → 启动 api_server → 健康检查 |
| pause | 快照（Phase 1）→ 暂停 VM |
| resume | 恢复 VM → 健康检查 |
| destroy | 快照 → 停止路由 → 销毁 VM → 释放租约 |

规则：

1. 健康检查失败时不自动销毁，保留现场供诊断。
2. resume 失败时，按 Phase 1 恢复流程重建实例。
3. 初始化只在 `config.yaml` 不存在时执行。已初始化的 home 不覆盖。空目录不能遮蔽镜像内的初始内容。

## 3. 唤醒

- 请求到达暂停的 profile 时，网关保留该请求，触发 resume，等待健康检查通过后转发。
- 等待上限可配置。超过上限返回"正在唤醒"（HTTP 202 或等价状态），由客户端重试。
- Pocket 客户端在收到该状态时显示"正在唤醒"，不显示为连接失败。
- 日程到点唤醒见 Phase 4。

## 4. 路由

- 网关按 `profile_id` 找到当前实例，代理到 VM 的 api_server。
- HTTP、SSE 流式输出、WebSocket（`/api/ws`）都要支持。WebSocket 升级前完成鉴权（Phase 2）。
- 网关到 VM 使用**实例级内部密钥**。VM 只接受带有效内部密钥的请求。
- 网关向 VM 传递已验证的主体信息（`profile_id`、`device_id` 或 `api_key_id`），由内部密钥签名。VM 不接收终端用户的原始凭证。

## 5. 挂载

- 宿主目录路径由服务端根据 `profile_id` 派生，客户端不能指定。
- `profile_id` 必须匹配白名单格式，拒绝路径分隔符与 `..`。
- VM 内 Hermes 以非 root 用户运行。
- `/opt/common-skills` 只读挂载。同名私有 Skill 优先。

## 6. 网络

- 默认拒绝出站。
- 放行：模型端点（显式 CIDR 或域名）、模板声明的外部服务。
- 屏蔽：内网网段（`10/8`、`172.16/12`、`192.168/16`）、云元数据地址、控制面、节点代理、Redis 或数据库、对象存储内网地址、其他 VM。

## 7. 密钥

- 模型密钥与其他租户凭证在启动时注入 VM 环境变量。
- 不写入挂载目录，不进入快照。
- 轮换 = 重启实例。

## 8. 资源与空闲

- 模板声明 CPU、内存、磁盘上限，按 profile 可覆盖。
- 空闲超过 `idle_pause_sec`（默认 900s）后自动 pause。
- VM 内 terminal 使用 local backend。隔离由 VM 提供。

## 9. 验收

1. **隔离**：租户 A 的 VM 无法访问控制面、节点代理、数据库、对象存储内网地址、其他 VM 或内网段。路径穿越和符号链接攻击被拒绝。
2. **挂载**：空目录不遮蔽镜像初始内容；已初始化的 home 不被覆盖。
3. **单 writer**：创建新实例前旧实例已停止。
4. **密钥**：VM 文件系统与快照中搜不到明文密钥。
5. **流式**：SSE 与 WebSocket 经网关代理后输出完整，客户端断开能传播到 VM。
6. **唤醒**：暂停的 profile 收到请求后，唤醒完成并返回；唤醒耗时有记录。
7. **工具连续性**：同一会话中 `write_file` 与 `terminal` 看到同一文件，后台任务能完成回调。
8. **提供者边界**：核心仓库的代码中不出现 Cube SDK 的导入。

## 10. 待确认

- Cube provider 包的名称与仓库位置。
- 目标 Cube 版本的 pause/resume 可靠性（Cube #953）。
- 出站策略能否精确到 CIDR 与域名。

## 11. 实施步骤

| 步 | 内容 | 验证 |
|---|---|---|
| S1 | Provider 接口与开发 provider | 生命周期循环 |
| S2 | 节点代理的目录准备与初始化 | 空目录不遮蔽镜像 |
| S3 | 网关路由（HTTP、SSE、WebSocket） | 流式输出完整 |
| S4 | 唤醒与"正在唤醒"状态 | 唤醒耗时记录 |
| S5 | 网络与密钥（Cube 独立包） | 内网探测失败，无明文落盘 |
| S6 | 隔离验收 | 第 9 节全部通过 |
