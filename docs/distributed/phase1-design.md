# Hermes 分布式改造 · Phase 1 详细设计

> 版本：v1.1（修正稿） ｜ 状态：待确认 ｜ 前置：[Phase 0](phase0-design.md)
>
> 只覆盖 **无状态化底座（目标 A）**：多副本 api_server、会话亲和、transcript 快照、优雅退出。所有能力可选；`cluster.enabled=false` 时与现状完全一致。
>
> 相对 v1.0 的撤回：`cluster.db` / ConfigStore / 归属表、用 LockBackend 替换 `turn_lease`、主路径改 `run_agent_cache.py`、`import_sessions` skip 当迁移、generation 相等才导入。

---

## 1. 目标与非目标

### 目标

1. Gateway 可水平运行多副本（≥2）；同一 `session_key` 由亲和路由钉在同一节点。
2. 节点优雅退出或替换后，**transcript + compression lineage** 可从对象存储恢复到最后一次成功快照。
3. api_server 入口：`claim` → 本节点处理或内部反代到 owner；节点间有共享密钥。
4. 日志携带 `user_id / agent_id / session_id / node_id`（`agent_id = profile`，Phase 1 的 `user_id` 可为空）。
5. 优雅 drain：停新 claim → 等 in-flight → 强制回写 → 释放租约（复用 `gateway/drain_control.py` 语义，另暴露 HTTP）。

### 非目标（本阶段明确不做）

- 不改消息平台长轮询适配器（Phase 5）；**多副本仅用于 api_server**。
- 不引入任务队列（Phase 4）；**cron 仍单节点**（文档化限制）。
- 不做多租户：无 `cluster.db`、无 ConfigStore、无 userId→agentId 表。
- 不替换 SQLite；不把 `turn_lease` 换成 Redis 锁（亲和已保证单节点；进程内 `asyncio.Lock` 保留）。
- 不同步 memory、工作区文件、prompt pin、run 幂等、artifacts（见 Phase 0 清单）。
- 不改 CLI / TUI / Electron 入口。

---

## 2. 总体架构

```
                 ┌─────────────┐
   Client ─────► │  LB / 反代   │  无状态转发，无需 sticky
                 └──────┬──────┘
            ┌───────────┼───────────┐
            ▼           ▼           ▼
      gateway-1    gateway-2    gateway-N     仅 api_server 多副本
      nodeId=A     nodeId=B     nodeId=C      每节点本地 state.db（热缓存）
            └───────────┼───────────┘
              ┌─────────┴──────────┐
              ▼                    ▼
           Redis                 对象存储
        锁/亲和/节点地址          S3 兼容（默认七牛 Kodo）
```

核心思想：会话亲和 + transcript 快照外置。Redis 只存协调元数据；会话内容仍以 SQLite 为节点热路径，仅在迁移/失效时经对象存储交接。

---

## 3. 模块设计

### 3.1 `gateway/coordination/`（Phase 1 支点）

放在 `gateway/` 下，不进顶层包。接口只覆盖本阶段原语。

```
gateway/coordination/
├── __init__.py          # 按当前 profile scope + cluster.* 返回 local 或 redis
├── base.py              # SessionRouter Protocol
├── local.py             # 恒归属本节点；零依赖
├── redis_backend.py     # 可选依赖 redis
├── snapshot_store.py    # S3 兼容存取 + zstd + meta CAS
├── session_sync.py      # acquire/replace/回写编排
└── drain.py             # 停 claim / 等 turn / 回写 / 摘牌（调用现有 drain_control 语义）
```

不设 ConfigStore、cluster_db、也不强制 LockBackend。若实现 redis 锁，仅给快照回写互斥与日后 cron 选主预留，**不**接入 `gateway/turn_lease.py`。

#### SessionRouter

```python
class SessionRouter(Protocol):
    def claim(self, session_key: str, node_id: str, ttl: float) -> Claim: ...
    def lookup(self, session_key: str) -> str | None: ...
    def invalidate(self, session_key: str) -> None: ...
    def stop_new_claims(self) -> None: ...   # drain：本节点拒绝成为新 owner
```

- Redis：`{home}:sess:route:{session_key}` → `{node_id, generation, claimed_at}`，PX = ttl（默认 90s），持有方每 30s 心跳（**asyncio task**，不用后台线程）。
- `claim`：无归属或租约过期 → 本节点接管（`generation += 1`）；他节点未过期 → `is_mine=False`。
- 节点地址：`{home}:node:addr:{node_id}`，与心跳同周期。内部反代校验 `CLUSTER_INTERNAL_TOKEN`；登记地址必须是内网/cluster DNS，防 SSRF。
- **心跳失败 → fail-closed**：停止本地对该 session 的写入，等待租约过期由他节点接管，禁止假装仍持有。

入口：api_server 读到 `session_key`（header 或已绑定会话）后 `claim`：

- `is_mine=True` → 本地处理（必要时走 §3.3 拉取/replace）。
- `is_mine=False` → **内部反代**到 owner（不用 302：OpenAI SDK 对 POST 重定向不完整）。反代必须覆盖 SSE（chat/completions stream、run events）。WebSocket（browser-control）Phase 1 可拒绝跨节点并返回 503 + `Retry-After`，或文档化为「须打到 owner」。
- 请求体若已在鉴权时读完，反代转发缓冲后的 body。长 turn 期间心跳续约；代理超时独立配置，不得用 90s 租约当 HTTP 超时。

**主路径是 api_server**，不是 `GatewayAgentCacheMixin`。现码 api_server 每请求重建 `AIAgent`，MemoryManager 停在 `ApiServerMemorySessions`（进程内、**不随快照迁移**）。

进程内 `turn_lease`（按 resolved `session_id` 的 `asyncio.Lock`，含 compression `rebind`）保持不变。

### 3.2 快照协议（对象存储）

复用 `hermes_state_portability.py` 的导出能力，但 **禁止**用 `import_sessions` 的 skip-existing 当迁移。需要新 sibling：`import_sessions_replace`（或等价），按 lineage **覆盖**本地行。导出：

```
export_session_lineage(session_id, include_inactive=True)
```

压缩 JSON（不是 SQLite dump）。布局：

```
s3://<bucket>/hermes/{home}/sessions/{tip_session_id}/
    snapshot.{generation}.json.zst
    meta.json     # generation, node_id, exported_at, sha256, lineage_session_ids, message_count
```

先上传不可变 `snapshot.{generation}.json.zst`，再 **CAS 更新** `meta.json`（If-Match ETag / 等价条件写）。禁止原地覆盖单一 `snapshot.db.zst` + 非原子 meta。实施前验证目标厂商（含 Kodo）的条件写语义。

#### fencing（v1.0 读路径是反的，以此为准）

记 Redis 中当前租约为 `G`，快照 meta 为 `Gs`，本地 watermark 为 `Gl`。

1. **claim 成功**（本节点 `G` 为新 generation）后，无论本地是否已有该 session：
   - 拉 meta；若无快照且本地也无 → 空会话，正常新建。
   - 若 `Gs < G`：快照来自前任。若 `Gl < Gs` 或本地无行 → **replace 导入**快照。若 `Gl == Gs` → 本地已是快照内容，可用本地。若 `Gl > Gs` → 本地领先（上次回写失败），用本地并尽快回写。
   - 若 `Gs >= G`：异常（不应出现）；拒绝用快照覆盖，打错误日志。
2. **回写**（turn 结束且 dirty、drain、周期仅对 dirty）：仅当 Redis 中 generation **仍等于** 回写开始时的 `G` 才 CAS `meta.json`；否则丢弃（已被接管）。
3. **故障切换**：租约过期 → 他节点 claim（`G' = G+1`）→ 走第 1 步。丢失窗口 ≤ 距上一次成功回写。
4. 僵尸节点醒来：其 `G` 已不是 Redis 最新值，条件写失败。

**不要**检查 `meta.generation == Redis.generation` 才导入：claim 成功后 Redis 已是新 generation，快照永远是旧值。

#### 回写策略（不要 5 分钟全量所有会话）

- **dirty-bit**：仅当 transcript 相对上次成功快照有变化才写。
- `on_turn_end: true`：turn 成功结束后异步回写该 session（默认）。
- 周期扫描：只扫 dirty，默认 300s 作为漏网兜底，不是全量上传。
- drain / 进程退出：对仍持有的 dirty 会话强制回写。

附录 B 旧「1 万会话 × 5min 全量 = ¥0」不成立：成本是写请求。dirty-bit 是 Phase 1 的一部分，不是 Phase 4 优化。

#### 一致性边界

- 租约内、回写成功：transcript 零丢失。
- 猝死：最多丢未回写增量（典型为最后一 turn，若 `on_turn_end` 已成功则更小）。
- 优雅退出：以 drain 完成回写为准，才允许称「滚动无感知」。
- 导入会重置 routing/handoff/rewind/`last_activity_*`——这是 portability 既有契约，文档化为限制，不在本阶段「修回去」。

### 3.3 日志

`agent/log_context.py`：`contextvars` 持有四元组；api_server 入口设置；`hermes_logging.py` formatter 追加 `[u= a= s= n=]`。缺省字段留空，不伪造。

### 3.4 Drain

现状：`{HERMES_HOME}/.drain_request.json` + watcher。集群：

1. `POST /admin/drain`（节点间 token）或 preStop 调用同一实现。
2. `stop_new_claims`；in-flight turn 等到上限（默认 120s）。
3. dirty 快照回写 + `invalidate`/`release` 本节点路由。
4. 退出。超时强杀走猝死路径（租约过期 + fencing）。

Phase 1 验收必须包含 drain，不要拖到 Phase 5 才有优雅退出。

---

## 4. 配置

```yaml
cluster:
  enabled: false
  node_id: ""                      # 空 = hostname 或 POD_NAME
  # redis_url 走 .env REDIS_URL，不写进 yaml
  object_store:
    backend: "s3"                  # s3 | none
    endpoint: "https://s3.cn-east-1.qiniucs.com"
    bucket: "hermes-sessions"
    # AccessKey/Secret 走 .env
  affinity:
    lease_ttl_sec: 90
    heartbeat_sec: 30
  snapshot:
    on_turn_end: true
    dirty_periodic_sec: 300
  drain_timeout_sec: 120
  proxy:
    timeout_sec: 600               # 长 turn；与租约无关
```

`cluster.enabled=true` 但缺 redis/boto3/token → 启动失败。

---

## 5. 实施步骤（确认文档后再写代码）

| 步 | 内容 | 验证 | 回滚 |
|---|---|---|---|
| S1 | `gateway/coordination/` + local SessionRouter + 契约测试 | local 恒 is_mine | 删包 |
| S2 | redis SessionRouter + 心跳 fail-closed；**不改** turn_lease | 双进程争同一 session_key | 配置回退 |
| S3 | api_server claim + 内部反代（含 SSE）+ 节点 token | 双节点同一 session 落同一 owner；跨节点无感 | 同上 |
| S4 | 快照 lineage 导出 + replace 导入 + fencing CAS + dirty-bit | 杀 A → B 恢复至最后快照；A 复活不能覆盖；回流 A 不吃陈旧本地 | 同上 |
| S5 | drain HTTP + 与 drain_control 对齐 | 优雅退出后下一节点无缺口 transcript | 同上 |
| S6 | 日志四元组 | 集群请求日志含 s=/n= | 同上 |

Gateway **和** CLI 两边加载 `cluster.*`。

---

## 6. 验收

1. `cluster.enabled=false`：现有测试（含 gateway E2E）全绿。
2. 双节点 compose（2 gateway + redis + minio）：同一 `session_key` 连续 20 turn 落同一节点；跨节点请求被反代。
3. **优雅退出**：drain 后滚动重启，transcript 连续（允许 prompt cache 变冷）。
4. **猝死**：`kill -9` → 90s 内他节点接管 → 历史恢复至最后成功快照 → 僵尸写被拒。不称为「无感知」。
5. 回流：A→B→A 后 transcript 含 B 期间的 turn（replace，不是 skip）。
6. 明确不验：memory freeze、工作区文件、cron、Telegram 多副本。

---

## 7. 风险

| 风险 | 评估 | 缓解 |
|---|---|---|
| 大 session 导出 | 中 | zstd；超限告警；不在本阶段做增量 chunk |
| Redis 单点 | 生产前用托管 Redis | 租约丢失只回退到快照；不丢已回写 |
| 内部反代双跳 / SSE | 中 | 亲和保证命中率；SSE 测过再标 S3 完成 |
| Kodo 条件写不完整 | 中 | S5 用 MinIO 验协议；生产前对目标 endpoint 做 CAS 探测 |
| api_server 不走 agent cache | — | 文档化；不要改错文件 |
| 写请求费用 | 中 | dirty-bit；禁止全会话周期全量 PUT |

---

## 8. 工作量（修正）

关键路径是 S3–S5（反代 + replace 导入 + fencing），不是「1500 行含测试」。按小几千行 + compose 双节点验收估，确认后再拆 PR。

---

## 附录 A：文件意向（实施时以实际 sibling 为准，此处约束范围）

### 新增（约 8）

`gateway/coordination/` 下 §3.1 所列；`agent/log_context.py`；`tests/coordination/` 或 `tests/gateway/coordination/`；`docker/docker-compose.cluster.yml`（2 gateway + redis + minio）。

**需要动 hermes_state**：新增 replace/upsert 导入 sibling（不是改 portability facade 行为让日常 import 变成覆盖）。这与 v1.0「hermes_state 全系列不动」冲突——以本页为准，否则回流必丢数据。

### 修改

| 区域 | 改动 |
|---|---|
| api_server **siblings**（入口中间件，避免继续膨胀 facade） | claim + 反代 + log context |
| `hermes_logging.py` | 四元组 |
| `gateway/config*.py` + `hermes_cli/config*.py` | `cluster.*` |
| `gateway/drain_control.py` 或 sibling | HTTP drain 接到现有文件语义 |
| `pyproject.toml` | optional-dependencies `cluster`（带上界） |

### 明确不动

CLI/TUI/Electron、cron、长轮询适配器、`turn_lease.py` 锁实现、`authz_mixin.py`、消息平台 allowlist、`state.db` schema。

---

## 附录 B：对象存储（修正成本叙事）

选型仍是 **S3 兼容 + 默认可配 Kodo**，接口不绑厂商。场景以写请求为主、读极少。

v1.0 用「10GB 存储免费 → ¥0/月」忽略了写次数：若对 1 万会话每 5 分钟全量 PUT，月写量是千万级，请求费才是主成本。Phase 1 用 dirty-bit + turn 结束回写后，费用与「有变更的 turn 数」成正比，而不是会话数 × 288。

`object_store.backend=s3` + `endpoint` 可切换 OBS/TOS/COS。不把 Redis 当快照后端（大对象会撑爆协调 Redis）。
