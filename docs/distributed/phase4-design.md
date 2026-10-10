# Hermes 分布式改造 · Phase 4 详细设计

> 版本：v1.2 ｜ 状态：待确认 ｜ 前置：[Phase 1](phase1-design.md)；cron 外置是本阶段 **S0**，不是「换一把 Redis 锁」。观测不依赖 Phase 2 租户表。
>
> 目标一句话：把 **本来就允许延迟** 的任务（入站 webhook 投递、cron 触发、出站回调）迁到 Redis Streams；交互式 turn 仍同步。可观测扩展现有 OTLP，而不是新造 coordination 遥测栈。
>
> 相对 v1.0 的撤回：假定 `jobs.json` 仍在本地即可多节点 tick；用 at-least-once 队列直接替换 cron 的 at-most-once；把 `/v1/chat/completions` ACK 化；新建 `coordination/tracing.py` 绕过 `agent/monitoring/otlp_exporter.py`；默认 Prometheus 指标带 user 标签；把 worker 启动堆进 `gateway/run.py` facade。

---

## 1. 目标与非目标

### 目标

1. **S0：cron job store 外置**（Redis 或对象存储备份 + Redis 权威），按 `{scope}` 选主 tick。之后才允许多节点 cron。
2. 入站平台 webhook / 出站投递等延迟任务：HTTP 快 ACK，任务进 Streams。
3. 队列：至少一次、有限重试、死信、超时 XCLAIM；消费端幂等。
4. cron 语义升级后仍 **at-most-once 开火**（先占 slot 再入队；与今日 `next_run_at` + `pending_slot` + occurrences ledger 对齐）。
5. Trace/metrics：接到现有 `monitoring.export.otlp` 与配置门。

### 非目标

- 不把交互式 agent turn 改成队列（OpenAI 兼容同步接口保持）。
- 不引入 Kafka/RabbitMQ。
- 不做 DAG 编排。
- Phase 1 文档化的「cron 单节点」在 S0 完成前仍然有效。

---

## 2. 为什么队列在这一阶段

Phase 1 推迟队列是为了保住 turn 串行。本阶段队列 **只承载本就不保证即时性的工作**。Worker 执行若需要跑 turn，仍走 Phase 1 `claim` + 节点内 `turn_lease`。

两类流量互不干扰。认领失败（不是本节点且反代不可用）→ **任务重入队**，不要静默双跳打爆 owner。

---

## 3. Cron 迁移（先于此，否则 Phase 5 emptyDir 会丢任务定义）

现状（必须遵守的不变量，见 `cron/AGENTS.md`）：

- 每 profile 一份 `jobs.json` + occurrences ledger。
- 每 home 一把 `{home}/cron/.tick.lock`。
- **at-most-once**：tick 先推进 `next_run_at` 再派发；`pending_slot` 崩溃恢复。
- 一个进程顺序 tick 多个 home，in-flight 键是 `(home, job_id)`。

S0 设计：

1. Job 文档与 ledger 的权威副本进 Redis（hash/JSON），key 带 `{scope}:cron:`。可选定期把快照写对象存储防 Redis 误清空。
2. Tick **选主按 home**（`{scope}:lock:cron-tick`），不是全集群一把锁。
3. 到点：Lua 原子校验 leader 身份/租约与 schedule revision，同时推进 next_run_at、登记 occurrence 并 XADD。禁止“先占 slot，再单独入队”的崩溃空窗。相关 key 必须同 hash slot；cron store 与 stream 在此协议下必须位于同一 Redis 原子域。
4. 幂等键 = `(scope, job_id, scheduled_instant)`。worker 用持久 ledger 原子领取执行门，成功领取后才启动 agent；重复投递/XCLAIM 不二次启动。领取后崩溃可能漏执行，恢复标记为 interrupted/unknown，不自动重跑已领取 occurrence。
5. 语义是 **at-most-once agent 启动**，不是任意外部副作用 exactly-once；不得同时承诺崩溃无遗漏和绝不重复。人工重跑须产生显式新 occurrence。未领取的已入队任务可回收；pending 状态与现有 catch-up 窗口需逐项对齐。
6. `cluster.queue.cron_store: redis | local`；`local` = 今日文件路径（默认）。未切 redis 时禁止多节点同时 tick 同一 home。

**不要**只把文件锁换成 `LockBackend` 却把 `jobs.json` 留在 emptyDir。

---

## 4. 队列（Redis Streams）

```
生产者：平台 webhook 适配器 / cron ticker / 出站 webhook
        XADD → stream: {scope}:tasks:{webhook|cron|callback}
消费组：每 gateway 节点 N 个 worker 协程（sibling 模块，不写进 run.py 本体）
        成功 XACK
        失败：≤ max_retries 则 ZSET 延迟后再 XADD；否则 DLQ
        超时：XPENDING → XCLAIM
```

消息需含 `task_id`、`type`、`payload`、`route: {scope, conversation_id, session_key, agent_id, user_id}`、`idempotency_key`、`trace_id`。

| 决策 | 选择 |
|---|---|
| 投递 | 至少一次 + 消费端幂等（复用/扩展 delivery ledger 键） |
| 会话 | worker 先 `claim`；非本节点则 **requeue**（或仅当内部反代健康时转发，并计入回压） |
| 延迟重试 | ZSET + 选主搬运（按 scope，条件续约与释放） |
| 顺序 | 默认不承诺入站顺序；turn_lease 只保串行。需有序的平台按稳定 conversation 分片、带序号并设计缺口/重投处理 |
| 堆积 | 按所有消费组的安全确认范围裁剪，禁止仅 MAXLEN 删除未确认任务；满载拒绝新生产/背压 + 告警 |

### 执行与恢复边界

- 长任务定期续执行心跳/可见性期限。仅“pending 超过 60s”不能认定 owner 已死；回收先验证执行状态，cron 仍受不可重领执行门约束。
- 失败消息转延迟 ZSET/DLQ 与原消息 XACK 原子化；延迟搬运 XADD + ZREM 原子化，相关 key 同 slot。幂等键跨重试保持不变，attempt 增加且有最大总年龄。
- 延迟搬运锁按 scope，带 incarnation/epoch 条件续约与释放。认领失败指数退避、jitter 与 owner 级回压，避免不断 requeue 占满流。
- 入站 webhook 只有 XADD 被确认后才 ACK；恢复承诺仍受 Redis 持久化 RPO 限制。Redis 故障切换不能宣称队列无损，见 Phase 0。
- 容量阈值拒绝新任务并返回可重试错误；未 ACK 的正文不能因达到 stream_maxlen 被静默裁剪。DLQ 提供查询与受控重放，ledger 保留期覆盖重试/平台重投窗口。

### Webhook 要分流

| 种类 | 本阶段怎么做 |
|---|---|
| OpenAI `/v1/chat/completions`（同步） | **不入队** |
| 平台 **入站** webhook（若该平台已是 HTTP 推送） | 校验后 202 + XADD；执行走 worker+claim |
| `hermes webhook` **出站** | 可入队；重试对投递 URL |
| Telegram getUpdates 等长轮询 | **仍不在本阶段**（Phase 5 poller） |

---

## 5. 可观测性

- Trace：入口生成/继承 W3C `traceparent`；contextvars 传播（可与 Phase 1 `log_context` 同上下文）。**导出走 `agent/monitoring/otlp_exporter.py` 已有 `monitoring.export.otlp`。** 不新建平行 exporter。
- 采样：沿用/扩展现有 monitoring 配置；错误可 100%。
- Metrics：每节点 scrape 或 OTLP metrics（已有 optional extra）。默认标签：`node, route, status, stream`。**`user` / 用量归因标签默认关闭**，须配置门（仓库「无 opt-in 不上外发用量」）。
- 建议指标：请求量与延迟、错误分类、`queue_depth`、重试/DLQ、`session_migrations`、快照回写错误。告警样例只放 `docker/` 或 `docs/`，不进核心 Python 包当「别人的产品」。

### Redis 不可达时

1. 队列生产者失败 → 对 **允许同步降级** 的路径改为同步并告警（例如出站 webhook）；入站 202 类则 503，避免谎称已入队。
2. cron 选主失败 → 该 home 本轮不 tick（与今日抢不到文件锁类似）；恢复后按 catch-up 窗口补，不默默丢 slot。
3. Phase 2 限流保持 fail_closed。

---

## 6. 文件意向

| 意向 | 用途 |
|---|---|
| `gateway/coordination/queue.py` | Streams 封装；local 退化进程内队列 |
| `gateway/coordination/cron_store.py` | S0：按 home 的 Redis job store |
| `gateway/task_worker.py`（或 `run_task_worker.py` sibling） | 消费循环 |
| `cron/` siblings | 切 store、占 slot、幂等键；**最小改** scheduler facade |
| 扩展 `otlp_exporter.py` / monitoring 配置 | span 源与指标门 |
| `docker/prometheus-alerts.yaml`（样例） | 非核心依赖 |
| 测试 | 至少一次、DLQ、XCLAIM、cron 双节点不双火、local 回归 |

配置加载：gateway raw YAML + CLI defaults 同步。

---

## 7. 配置

```yaml
cluster:
  queue:
    enabled: false
    cron_store: local            # local | redis；redis 才允许多节点 tick 同一 home
    max_retries: 5
    retry_base_sec: 2
    retry_max_sec: 300
    claim_stale_sec: 60          # 须配执行心跳
    stream_maxlen: 100000        # 容量阈值，禁止裁剪未确认消息
    workers_per_node: 4
  observability:
    metrics_enabled: false       # 须显式开；默认不带 user 标签
    metrics_include_user: false
    # trace 继续用 monitoring.export.otlp.*
```

---

## 8. 实施步骤与验收

| 步 | 内容 | 验证 |
|---|---|---|
| S0 | cron Redis store + 按 home 选主 + 幂等开火 | 两节点同一 home 不双跑；杀 ticker 后 catch-up 符合现窗口 |
| S1 | queue local 实现 + 契约测试 | `queue.enabled=false` 全绿 |
| S2 | 出站/入站 webhook 入队（不含 chat completions） | 快 ACK；重复投递幂等 |
| S3 | 重试 / DLQ / XCLAIM | 注入失败进 DLQ；杀 consumer 后回收 |
| S4 | cron 任务经队列执行（S0 之后） | 故障切换无第二次 agent 跑同一 `scheduled_instant` |
| S5 | OTLP/metrics 配置门 | 默认无 user 标签；开启后错误请求有 trace |

验收：`cluster.queue.enabled=false` 且 `cron_store=local` 等于今日行为；S0 未完成不得宣称 cron 多活。

---

## 9. 风险

| 风险 | 缓解 |
|---|---|
| 队列重复开火 cron | 幂等键与 ledger 对齐；测试双投 |
| Streams 撑爆内存 | 安全裁剪 + 背压 + 告警 |
| worker 反代风暴 | 认领失败 requeue + 回压 |
| 观测变相收集用户用量 | 默认关；配置门 |
