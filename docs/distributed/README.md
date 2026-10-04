# Hermes 分布式改造 · 设计文档索引

> 目标：多用户专属 Agent、用户隔离、可水平扩展，渐进式交付，每阶段产出可验证版本。
> 核心原则：**最小改动**——所有分布式能力可选配置，`cluster.enabled=false` 时行为与单体现状完全一致。

## 关键架构决策（已确认）

| 决策点 | 结论 |
|---|---|
| 状态存储路线 | 会话亲和（Redis 租约 + fencing）+ 对象存储快照；SQLite 保留为节点热缓存 |
| 对象存储 | 七牛云 Kodo（S3 兼容协议，boto3 直连，可配置切换任意 S3 兼容厂商） |
| 服务入口 | HTTP API 优先（现有 OpenAI 兼容 api_server）；消息平台长轮询延后至 Phase 5 |
| 协调层 | `coordination/` 包三接口（LockBackend / SessionRouter / ConfigStore），local + redis 双实现 |

## 阶段文档

| 阶段 | 文档 | 内容 | 状态 |
|---|---|---|---|
| Phase 1 | [phase1-design.md](phase1-design.md) | 无状态化底座：协调层抽象、会话亲和、快照同步（fencing 防脑裂）、归属校验、日志埋点 | 待确认 |
| Phase 2 | [phase2-design.md](phase2-design.md) | 安全与租户管控：API Key/HMAC 签名、表驱动授权、Redis 令牌桶限流、token 配额与用量审计 | 待确认 |
| Phase 3 | [phase3-design.md](phase3-design.md) | 工具安全解耦（可选）：工具分级、高危工具 RPC 沙箱、资源限额与逃逸防护 | 待确认（可跳过） |
| Phase 4 | [phase4-design.md](phase4-design.md) | 可靠性与可观测：Redis Streams 队列（重试/死信/超时）、cron 迁移、Trace/Metrics/告警 | 待确认 |
| Phase 5 | [phase5-design.md](phase5-design.md) | 云原生：K8s 清单、Redis Sentinel、HPA、优雅退出/灰度、长轮询适配器 leader 化 | 待确认 |

## 阶段依赖

```
Phase 1（底座）
   ├──► Phase 2（安全）────────┐
   ├──► Phase 3（沙箱，可选）   ├──► Phase 5（云原生）
   └──► Phase 4（队列/观测）───┘
```

## 贯穿性约定

1. **可回滚**：每个阶段默认配置关闭，单体用户零影响；每阶段内部分 S1–Sn 步，每步独立验证。
2. **组件最小化**：全程只引入两个外部组件——Redis（协调 + 队列）与对象存储（快照）。
3. **prompt 缓存与核心 invariant 不受破坏**：所有改动在入口管线与协调层，agent 核心与工具 schema 不变。
