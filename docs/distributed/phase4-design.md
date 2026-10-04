# Hermes 分布式改造 · Phase 4 详细设计

> 版本：v1.0（评审稿） ｜ 状态：待确认 ｜ 前置：Phase 1（coordination、Redis）、Phase 2（用量数据）已验收
>
> 目标一句话：**任务可靠性与完整可观测**——异步任务从进程内线程池迁到 Redis 队列（重试/死信/超时），全链路 Trace + 业务 Metrics + 告警，达到生产级运维能力。

---

## 1. 目标与非目标

### 目标
1. Webhook/慢请求异步化：HTTP 快速 ACK，任务进 Redis Streams 队列，规避接入方超时。
2. 队列语义完备：至少一次投递、指数退避重试、死信队列、超时回收。
3. cron 投递从「本地文件 + 崩溃不重试」迁移到统一队列，语义升级但行为兼容。
4. 全链路 Trace：traceId 贯穿 入口→claim→agent turn→工具→模型调用。
5. 业务 Metrics 与告警：请求量、token 消耗、失败率、队列堆积、会话迁移次数。

### 非目标
- 不替换 agent turn 的同步执行模型（交互式请求仍是同步的；只有明确异步的任务进队列）。
- 不引入 Kafka/RabbitMQ——Redis Streams 在本规模足够，复用现有 Redis 零新增组件。
- 不做长任务编排框架（DAG 工作流），仅提供「单任务 + 回调」的朴素编排。

## 2. 为什么现在是队列，而 Phase 1 不是

Phase 1 推迟队列的理由（保护 turn 串行语义）在本阶段通过设计消解：**队列只承载"本来就不保证即时性"的任务**（webhook 事件、cron 投递、异步回调），交互式 turn 仍走 Phase 1 的同步管线 + turn lease。两类流量互不干扰。

## 3. 队列设计（Redis Streams）

### 3.1 拓扑

```
生产者（api_server/webhook 适配器/cron scheduler）
   │  XADD
   ▼
stream: tasks:webhook   tasks:cron   tasks:callback
   │  消费组 workers（每 gateway 节点 N 个 consumer）
   ▼
worker 处理 ──成功──► XACK
   │失败
   ├─ 重试 ≤5 次：XADD 回队（携带 retry_count，延迟 = min(2^n, 300)s，经 ZSET 延迟队列）
   ├─ 超时未 ACK：XPENDING 巡检 → XCLAIM 回收重投
   └─ 超过上限 ──► stream: tasks:dlq（附最后错误与原始负载）
```

### 3.2 消息格式

```json
{
  "task_id": "ulid",
  "type": "webhook.deliver | cron.dispatch | callback.invoke",
  "payload": {...},
  "route": {"session_key": "...", "agent_id": "...", "user_id": "..."},
  "retry_count": 0,
  "trace_id": "...",
  "enqueued_at": 1730000000
}
```

### 3.3 关键语义决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 投递保证 | 至少一次 + 消费端幂等 | Streams 无精确一次；现有 delivery ledger 的"行被 claim 一次"语义可平滑升级为幂等键去重 |
| 会话亲和 | worker 消费后仍走 `SessionRouter.claim`，非本节点则转发 | 队列消费者可能与 session owner 不同节点；复用 Phase 1 机制，不引入二级路由 |
| 延迟重试 | ZSET（score=到期时间）+ 搬运线程 → XADD 回主流 | Streams 原生不支持延迟，ZSET 是标准解法 |
| 顺序性 | 同 session_key 的任务由消费端 claim 后经 turn lease 串行化 | 复用既有串行原语，队列不承诺顺序 |
| 堆积保护 | stream MAXLEN ~ 100k + 堆积告警（见 §5） | 防内存失控 |

### 3.4 cron 迁移

- 现状：`cron/jobs.json` + 本地 tick 锁 + gateway 认领投递。
- 迁移后：tick 锁改走 `LockBackend`（**任一节点可当选 ticker，故障自动切换**）；到点任务 `XADD tasks:cron`；投递走队列而非本地 delivery_queue 文件。
- 兼容：`jobs.json` 格式不变；`cron.enabled_queue=false` 时完全走旧路径。

## 4. 可观测性设计

### 4.1 Trace

- **生成**：入口中间件生成/继承 `trace_id`（W3C traceparent 头透传），contextvars 传播（复用 Phase 1 的 `log_context`）。
- **跨进程**：队列消息携带 `trace_id`；沙箱 RPC（Phase 3）经 ctx 字段透传。
- **导出**：复用现有 `agent/monitoring/otlp_exporter.py` 接 OTLP 后端（Jaeger/Tempo 自选），span 粒度：request → claim → turn → tool_call → model_call。
- **采样**：默认 10%，错误请求 100%。

### 4.2 Metrics（Prometheus 格式，每节点 `/metrics` 端点）

| 指标 | 类型 | 标签 |
|---|---|---|
| `hermes_requests_total` | counter | node, route, status |
| `hermes_request_latency_seconds` | histogram | node, route |
| `hermes_tokens_total` | counter | user, agent, kind(prompt/completion) |
| `hermes_turn_errors_total` | counter | node, error_class |
| `hermes_queue_depth` | gauge | stream |
| `hermes_queue_retries_total` / `hermes_dlq_total` | counter | stream |
| `hermes_session_migrations_total` | counter | from_node（会话接管次数，观测稳定性） |
| `hermes_snapshot_write_seconds` / `_errors_total` | histogram/counter | node |

### 4.3 告警规则（示例，Prometheus Alertmanager）

| 规则 | 阈值 | 级别 |
|---|---|---|
| 队列堆积 | `queue_depth > 5000` 持续 5min | warning；>20000 critical |
| 死信流入 | `dlq_total` 5min 增量 > 10 | warning |
| 请求失败率 | 5min error ratio > 5% | warning |
| 会话迁移风暴 | `session_migrations` 10min > 50（提示节点抖动） | warning |
| Redis 不可达 | up == 0 | critical（触发 §降级策略） |
| 快照回写失败率 | 10min > 1% | warning（数据持久性风险） |

### 4.4 降级策略（Redis 不可达时）

1. 队列生产者写入失败 → webhook 请求退回同步处理并告警（不丢任务）；
2. cron ticker 无法获锁 → 全集群暂停调度（可接受，恢复后补跑 missed jobs，现有 cron 已有此语义）；
3. Phase 2 限流/配额按 fail_closed（默认）拒绝并告警。

## 5. 新增/修改文件

### 新增（7 个）
| 文件 | 用途 |
|---|---|
| `coordination/queue.py` | Streams 封装：enqueue/ack/retry（ZSET 延迟）/claim-stale/dlq；local 实现退化为进程内队列（保持单体可用） |
| `gateway/task_worker.py` | gateway 内嵌 worker 协程：消费→claim 会话→执行→ack/重试 |
| `gateway/webhook_async.py` | webhook 适配器 ACK 化改造（202 + task_id，可选回调查询） |
| `coordination/tracing.py` | trace 上下文传播 + OTLP span 装配（薄封装现有 exporter） |
| `coordination/metrics.py` | 指标注册与 `/metrics` 端点 |
| `docker/prometheus-alerts.yaml` | 告警规则样例 |
| `tests/coordination/test_queue.py` | 至少一次/重试/死信/超时回收/幂等 用例 |

### 修改（4 个）
| 文件 | 改动 |
|---|---|
| `cron/scheduler.py` | tick 锁走 LockBackend；到点任务入队；保留旧路径开关 |
| `gateway/run.py` | 启动/停止 task_worker；webhook 适配器接 async 层 |
| `agent/monitoring/otlp_exporter.py` | 接 tracing.py 的 span 源 |
| `hermes_cli/config.py` | `cluster.queue.*` / `cluster.observability.*` 配置段 |

## 6. 配置设计

```yaml
cluster:
  queue:
    enabled: false
    max_retries: 5
    retry_base_sec: 2
    retry_max_sec: 300
    claim_stale_sec: 60
    stream_maxlen: 100000
    workers_per_node: 4
  observability:
    metrics_enabled: true
    metrics_port: 8649
    trace_sample_rate: 0.1
    otlp_endpoint: ""            # 空 = 不导出
```

## 7. 实施步骤与验收

| 步 | 内容 | 验证 |
|---|---|---|
| S1 | queue.py + local 实现 + 契约测试 | local 模式回归全绿 |
| S2 | webhook ACK 化 + worker | 慢任务 HTTP ≤200ms ACK，结果经回调可查；重复投递幂等 |
| S3 | 重试/死信/超时回收 | 注入失败 5 次进 dlq；kill consumer 后 XCLAIM 回收 |
| S4 | cron 迁移 | 节点 A 调度中 kill → B 接管 tick；任务不重复执行 |
| S5 | metrics + trace + 告警联调 | Prometheus 出数；错误请求 trace 完整；告警规则触发演练 |

**验收标准**：kill 任一节点，队列任务 60s 内被他节点接管、零丢失；cron 跨节点故障切换无重复执行；`cluster.queue.enabled=false` 时现状行为完全保留。

## 8. 风险与边界

| 风险 | 缓解 |
|---|---|
| Streams 堆积撑爆 Redis 内存 | MAXLEN 截断 + 堆积告警 + dlq 隔离；按 10GB 内存估算可承载千万级消息 |
| 至少一次导致重复副作用 | 幂等键（task_id）消费端去重；文档化给任务作者 |
| 延迟队列搬运单点 | 搬运线程用 LockBackend 选主，故障自动切换 |
| trace/metrics 开销 | 采样 + 异步导出；基准测试开销 <3% |
