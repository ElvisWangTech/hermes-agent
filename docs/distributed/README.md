# Hermes 分布式改造 · 设计文档索引

> 目标：Gateway 可水平扩展；多用户隔离在后续阶段按现有 **profile** 模型叠加，不另起炉灶。
> 核心原则：所有分布式能力可选；`cluster.enabled=false` 时与单体现状完全一致。
>
> **先读 [Phase 0](phase0-design.md)。** 身份与「源 / 缓存 / 不同步」未确认前，不要实施 Phase 1–5。

---

## 架构决策

| 决策点 | 结论 | 状态 |
|---|---|---|
| 状态存储 | 会话亲和（Redis 租约 + fencing）+ 对象存储快照；SQLite 仅节点热缓存 | 方向确认；**协议以 Phase 1 v1.1 为准**（旧稿 generation 比较与 skip-import 作废） |
| 对象存储 | S3 兼容协议（boto3）；默认对接七牛 Kodo，可换任意兼容端点 | 确认（不绑厂商 SDK） |
| 服务入口 | HTTP API 优先（现有 OpenAI 兼容 api_server）；长轮询平台延后 | 确认 |
| 身份 | `agent_id = profile`；亲和键 = `session_key`；快照键 = session lineage | 见 Phase 0 |
| 协调层 | `gateway/coordination/`：Phase 1 只放 SessionRouter + 快照。不含 ConfigStore / 本地 `cluster.db` | 修正：旧「三接口 + cluster.db」撤回 |
| Redis HA | 对公网或多副本生产之前使用**托管 Redis**；不把自建 Sentinel 当 Phase 5 才补的前提 | 修正 |
| 共享状态 | 节点 SQLite / `jobs.json` / 进程 dict **不是**集群源 | 见 Phase 0 清单 |

---

## 阶段文档

| 阶段 | 文档 | 内容 | 状态 |
|---|---|---|---|
| Phase 0 | [phase0-design.md](phase0-design.md) | 身份映射、状态清单、键前缀、加载器 | 设计修正稿 |
| Phase 1 | [phase1-design.md](phase1-design.md) | 亲和路由、快照协议、内部反代、drain、日志四元组 | 设计修正稿 |
| Phase 2 | [phase2-design.md](phase2-design.md) | Bearer 鉴权、Redis 限流/配额；不改消息平台 authz | 设计修正稿 |
| Phase 3 | [phase3-design.md](phase3-design.md) | **默认跳过**；若做则强制现有 terminal backend | 设计修正稿（可跳过） |
| Phase 4 | [phase4-design.md](phase4-design.md) | 外置 cron store 后上队列；观测接现有 OTLP | 设计修正稿 |
| Phase 5 | [phase5-design.md](phase5-design.md) | K8s、配置/密钥挂载、按平台 poller、HPA | 设计修正稿 |

## 阶段依赖

```
Phase 0（身份 + 状态清单）
   └──► Phase 1（亲和 + 快照 + drain）
          ├──► Phase 2（API 鉴权 / 限流）──┐
          ├──► Phase 3（可选，默认跳过）    ├──► Phase 5（K8s）
          └──► Phase 4（cron 外置 + 队列 + 观测）─┘
```

Phase 2「对公网开放」的前提：托管 Redis 可用 + `/healthz` 能反映 Redis/对象存储，而不是等 Phase 5。

---

## 贯穿性约定

1. **可回滚**：每阶段默认关闭；阶段内 S1–Sn 可独立验证。
2. **组件**：只引入 Redis（协调 / 计数 / 队列）与对象存储（快照）。权威数据不放节点 SQLite。
3. **Profile 作用域**：一个进程多个 home；Redis/S3/锁按 `hermes_home_key()` 分区。Gateway 与 CLI 两套 config 加载器都要解析 `cluster.*`。
4. **Prompt 缓存**：改动停在入口与协调层，不改工具 schema。会话迁移后允许缓存变冷。
5. **OpenAI 兼容**：默认鉴权保持 `Authorization: Bearer`。
6. **观测**：扩展现有 `monitoring.export.otlp`；带 user 的用量标签须配置门 / opt-in。
7. **代码形状**：新逻辑进 sibling 或 `gateway/coordination/`，不往 `run.py` / `api_server.py` facade 堆。依赖带上界。
8. **只改设计直至确认**：本文档树是实施前置；确认前不落地代码。
