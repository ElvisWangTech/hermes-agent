# Hermes 分布式改造 · Phase 3 详细设计（默认跳过）

> 版本：v1.1（修正稿） ｜ 状态：待确认 ｜ 前置：无硬依赖
>
> **默认建议：跳过本阶段。** 多租户隔离应复用已有 `tools/environments/`（docker / ssh / modal / daytona / singularity），而不是再做一套「每工具 gRPC + 一次性容器」。
>
> 相对 v1.0 的撤回：ToolGateway、gRPC `Execute`、每请求一次性容器、按 `risk_level` 把 L2 工具全部 RPC 出去、默认插件 L2、把沙箱当 Phase 5 必选组件。

---

## 1. 是否实施

| 条件 | 建议 |
|---|---|
| 自有团队 / 个人、输入可信 | **跳过**。现有 docker 后端 + 审批钩子足够。 |
| 多租户但可接受「会话级容器/远程机」而非「每调用新容器」 | **跳过代码阶段**；运维上强制 `terminal.backend=docker`（或 modal/daytona）。 |
| 现有 backend 无法满足合规，必须额外隔离平面 | 才评估本阶段；且实施形态是 **新 environment provider**，不是工具 RPC 总线。 |

判定为跳过时：Phase 5 清单里 **不要** 默认部署 sandbox Deployment。

---

## 2. 为何不采用 v1.0 RPC 方案

现码已经是「会话级执行环境」，不是无状态函数：

- `tools/environments/base.py`：每命令新进程，但 **cwd / env snapshot 按会话保留**。
- docker / singularity：**bind mount** 看到同一工作区。
- ssh / modal / daytona：`file_sync.py` 同步工作区。
- `terminal(background=true, notify_on_complete=true)`、浏览器 CDP、委托子 agent 都绑在这个模型上。

v1.0「L0 `write_file` 在 gateway 本地、L2 `terminal` 在一次性容器」会拆掉文件连续性；「每请求起容器」会拆掉后台进程与 cwd。Agent 侧不可能「零感知」。Phase 1 又明确 **不同步工作区**，对象存储 mounts 并不存在。

Footprint：新能力应落在 environment 表上的新 provider，或运维强制已有 backend，而不是新核心 RPC 面。

---

## 3. 若跳过：运维约定（仍写进总设计，避免空窗）

多副本 + 不可信租户时：

1. 配置 `terminal.backend` 为 docker（或已有远程 backend），禁止默认 local 解析到 gateway 可写的宿主机盘。
2. 继续用现有审批 / egress / seccomp 相关配置，不在本阶段发明平行策略语言。
3. 文档告知：会话迁移后工作区不跟随（Phase 0）；要持久盘就用 backend 自己的 volume（docker volume、modal 盘等），不要指望 Phase 1 快照。

无需新包、无新工具 schema、prompt 缓存不受影响。

---

## 4. 若仍要做代码：唯一允许的形态

目标：把「高危执行」关进 **已有 Environment ABC** 的一个更硬的 backend，工具注册与 `handle_function_call` 不变。

### 允许

- 新增 `tools/environments/` sibling（例如更严的 gVisor 包装），在 `terminal_tool_backends.py` **表驱动**登记。
- `hermes tools` / `config.yaml` 的 `terminal.backend` 选出它。
- 可选：`risk_level` **仅审计/审批**（不进模型 schema，以免破坏 prompt 缓存）。不作为执行路由分叉。

### 禁止

- 按工具名走 gRPC `ToolSandbox.Execute`。
- 每 tool call 销毁容器导致 cwd/后台进程丢失。
- 默认把第三方插件标 L2 并强制 RPC。
- 为文件工具单独做对象存储 mount 协议（工作区同步不在 1–5 范围；backend 自己解决盘）。

### 验收（仅当实施）

- 同一会话连续 `write_file` + `terminal` 看到同一文件。
- `terminal(background=true)` 仍能完成回调。
- `cluster.enabled=false` 且 `terminal.backend=local` 时与今日完全一致。
- 开关 backend 不改变工具 JSON schema。

---

## 5. 配置（仅当实施）

```yaml
# 跳过 Phase 3 时不要增加新段；用现有 terminal.backend 即可。
terminal:
  backend: docker    # 多租户部署的运维强制项，不是新集群开关
```

不要引入与 `tools/environments` 平行的 `sandbox.enabled` + `sandbox.endpoint`，除非那个 endpoint 是 **environment provider** 的实现细节。

---

## 6. 与其他阶段

- Phase 1：工作区不同步 → 沙箱/backend 磁盘不能假设随会话亲和迁移，除非 backend 用共享 volume。
- Phase 5：跳过则拓扑里没有 sandbox 集群；HPA 只扩 gateway。
- 观测：若 backend 有额外进程，健康检查走现有 Docker/远端探针，不进 `coordination/`。
