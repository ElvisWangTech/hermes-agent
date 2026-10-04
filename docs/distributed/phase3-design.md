# Hermes 分布式改造 · Phase 3 详细设计（可选增强）

> 版本：v1.0（评审稿） ｜ 状态：待确认 ｜ 前置：无硬依赖，可在 Phase 2 之后任意时点实施
>
> 目标一句话：**把高危工具从 gateway 进程剥离**——Shell、代码执行等工具在独立沙箱服务中运行，消除工具故障/恶意输入导致 gateway 进程逃逸的风险。

---

## 1. 是否实施本阶段的判定标准

| 条件 | 建议 |
|---|---|
| 用户全部可信（自有团队/个人场景）、agent 不处理不可信输入 | **跳过**，原生 Docker 沙箱已够用 |
| 多租户开放、用户可自定义 prompt/技能，工具输入不可信 | **实施** |
| 工具曾造成宿主事故（写坏文件、耗尽磁盘、网络外联失控） | **实施** |

本阶段刻意设计为**可插拔**：实施与否只改变工具执行的传输层，不改变工具的注册与调用契约。

## 2. 目标与非目标

### 目标
1. 工具按风险分级，分级声明在工具注册元数据中。
2. 高危工具（Shell、代码执行、文件写入、浏览器）经 RPC 调用独立沙箱服务，进程/文件系统/网络/资源四维隔离。
3. 沙箱服务无状态、可水平扩展，故障域与 gateway 完全隔离。
4. 资源限制（CPU/内存/时长/输出大小）与输出校验在沙箱侧强制。

### 非目标
- 不做完整的 gVisor/Firecracker 级微虚拟机（Phase 3 用容器隔离 + seccomp/资源限额即可，更强隔离是运维选型而非代码改动）。
- 不改工具对 agent 的 schema 与返回格式（模型侧零感知，保护 prompt 缓存）。
- 低危工具（读文件、搜索、记忆查询）继续进程内执行，不为它们付出 RPC 成本。

## 3. 总体架构

```
gateway 节点                          sandbox 集群
┌──────────────────┐   gRPC/HTTP    ┌───────────────────────┐
│ ToolGateway       │ ────────────► │ sandbox-worker × N     │
│ （客户端，嵌入      │  Execute(req)  │  每请求起一次性容器       │
│  agent 进程）      │ ◄──────────── │  - CPU/mem/时长 cgroup  │
│ 按工具分级路由：    │  Execute(result)│  - 只读根fs + tmpfs    │
│ low → 本地直调     │                │  - 网络策略（默认拒绝外联）│
│ high → RPC        │                │  - 输出截断+校验        │
└──────────────────┘                └───────────────────────┘
```

## 4. 工具分级模型

| 级别 | 定义 | 例子 | 执行位置 |
|---|---|---|---|
| L0 只读 | 不改状态、无外联 | 文件读取、搜索、记忆查询、天气查询 | 进程内直调 |
| L1 低危写 | 限定路径写、可审计 | 记忆写入、笔记、配置读取 | 进程内直调 + 审计日志 |
| L2 高危 | 任意命令/代码执行、网络外联、浏览器 | Shell、Python 执行、browser 工具 | **沙箱 RPC** |
| L3 特权 | 改系统/凭据/发外部消息 | 发送邮件、支付、系统配置 | 沙箱 RPC + 人工审批钩子（预留） |

- 分级落地：`tools/registry` 的注册元数据增加 `risk_level` 字段；存量工具批量标注（内置工具由我们标注，插件工具默认 L2 保守处理，插件可声明降级并自担责任）。
- **对 prompt 缓存零影响**：分级只影响执行路由，工具 schema/描述不变。

## 5. RPC 协议（gRPC，单一方法足够）

```proto
service ToolSandbox {
  rpc Execute(ExecuteRequest) returns (ExecuteResult);
}
message ExecuteRequest {
  string tool_name = 1;
  string call_id = 2;              // 幂等键：重试不产生副作用叠加
  bytes  arguments_json = 3;
  SecurityContext ctx = 4;         // user_id/agent_id/session_id + 允许的权限集
  ResourceLimits limits = 5;       // cpu_ms/mem_mb/wallclock_sec/max_output_bytes
}
message ExecuteResult {
  bytes  output_json = 1;
  string error = 2;                // 分类：timeout | oom | policy_denied | tool_error
  ExecStats stats = 3;             // 实际资源消耗，反哺限额调优
}
```

- 选 gRPC 理由：强类型契约、流式扩展余地（大输出）、连接复用；备选 HTTP/JSON（实现更简单，调试方便）——**默认 gRPC，配置可切 HTTP**。
- 传输安全：内网 mTLS 或共享密钥 HMAC（复用 Phase 2 签名设施）。

## 6. 沙箱执行模型

1. **每请求一次性容器**：镜像预置工具运行时（shell/python/node）；只读根 fs + 限额 tmpfs；默认无网络（白名单域名可配）。
2. **资源限额**：cgroup v2 CPU/内存；wallclock 超时强杀；输出超限截断并标记。
3. **输出校验**：结构化检查（必须是合法 JSON、无控制字符注入）、大小限制、敏感模式扫描（如私钥格式）仅告警不阻断。
4. **工作目录注入**：工具需要的文件经显式 `mounts` 声明（从 gateway 经对象存储中转或直接流式传入），杜绝隐式访问宿主文件系统。
5. **幂等**：`call_id` 去重表（沙箱侧 Redis，TTL 1h），网络重试不重复执行副作用。

## 7. 新增/修改文件

### 新增（6 个）
| 文件 | 用途 |
|---|---|
| `tools/risk_levels.py` | 分级枚举 + 内置工具标注表 |
| `tools/sandbox/client.py` | gateway 侧客户端：按 risk_level 路由 L0/L1 直调 / L2+ RPC |
| `tools/sandbox/proto/` | protobuf 定义与生成代码 |
| `tools/sandbox/server.py` | 沙箱服务：收请求 → 起容器 → 限额执行 → 校验输出 |
| `tools/sandbox/container.py` | 容器生命周期：镜像管理、cgroup、tmpfs、网络策略 |
| `tests/tools/test_sandbox.py` | 逃逸尝试（读写宿主路径/外联/fork bomb）全部被拒的用例 |

### 修改（3 个）
| 文件 | 改动 |
|---|---|
| `tools/registry.py` | 注册元数据加 `risk_level`，默认 L2 |
| `tools/delegate_tool*.py` 等高危工具 | 标注级别；执行点统一走 `sandbox/client` 路由 |
| `hermes_cli/config.py` | `sandbox.*` 配置段（地址、限额默认值、开关） |

## 8. 配置设计

```yaml
sandbox:
  enabled: false                 # false = 全部工具进程内执行（现状）
  endpoint: "sandbox:50051"
  transport: "grpc"              # grpc | http
  defaults:
    wallclock_sec: 60
    mem_mb: 512
    cpu_ms_per_sec: 1000         # 1 核
    max_output_kb: 256
  network:
    default_policy: "deny"
    allowlist: ["api.example.com"]
```

## 9. 实施步骤与验收

| 步 | 内容 | 验证 |
|---|---|---|
| S1 | 分级元数据 + 存量工具标注 | 全工具分级清单评审 |
| S2 | sandbox client/server + 容器执行 | Shell 工具经 RPC 跑通，结果与本地一致 |
| S3 | 限额与超时 | OOM/超时/大输出用例按预期拒绝或截断 |
| S4 | 逃逸测试 | 读宿主 /etc、外联、fork bomb 全部被拒 |
| S5 | 故障隔离演练 | kill 沙箱集群 → gateway 仅 L2 工具报错，其余正常；沙箱恢复后自动可用 |

**验收标准**：agent 侧零感知（同一 prompt 在开关沙箱前后输出等价）；沙箱全挂时 gateway 不崩、不退化其他工具；逃逸测试用例全绿。

## 10. 风险与边界

| 风险 | 缓解 |
|---|---|
| RPC 增加 L2 工具延迟（每次 +50–200ms，含容器冷启） | 容器池预热（pool 默认 2 个热容器）；仍远高于模型调用延迟，占比可忽略 |
| 需要文件交互的工具改造量大 | mounts 声明式注入；逐个工具迁移，灰度开启（按 tool_name 开关） |
| 运维多一个服务 | 无状态、单二进制；docker-compose/K8s 各一份模板 |
