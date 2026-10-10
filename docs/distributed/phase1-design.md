# Hermes 分布式改造 · Phase 1 详细设计

> 版本：v1.2 ｜ 状态：待确认 ｜ 前置：[Phase 0](phase0-design.md)
>
> 本轮修订：目标命名为 API 会话历史高可用；Redis 原子提交 manifest，S3 只存不可变 blob；分离 epoch/revision，补冷恢复索引及故障验收。
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
4. 日志携带 `user_id / agent_id / session_id / node_id`（`agent_id = cluster.profile_id`，Phase 1 的 `user_id` 可为空）。
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

核心思想：会话亲和 + transcript 快照外置。Redis 存协调及权威提交头；会话内容仍以 SQLite 为节点热路径，仅在迁移/失效时经对象存储交接。

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
├── snapshot_store.py    # 不可变 blob + zstd；Redis manifest 提交
├── session_sync.py      # acquire/replace/回写编排
└── drain.py             # 停 claim / 等 turn / 回写 / 摘牌（调用现有 drain_control 语义）
```

不设 ConfigStore、cluster_db、也不强制 LockBackend。若实现 redis 锁，仅给快照回写互斥与日后 cron 选主预留，**不**接入 `gateway/turn_lease.py`。

#### SessionRouter

```python
class SessionRouter(Protocol):
    def claim(self, session_key: str, node_id: str, ttl: float) -> Claim: ...
    def lookup(self, session_key: str) -> str | None: ...
    def renew(self, claim: Claim, ttl: float) -> bool: ...
    def release(self, claim: Claim) -> bool: ...  # compare owner/incarnation/epoch
    def stop_new_claims(self) -> None: ...   # drain：本节点拒绝成为新 owner
```

- 每 conversation 分离持久 epoch、带 TTL 的 lease、无 TTL 的 manifest。`claim` 用 Lua 原子检查租约；新接管递增 epoch，返回 lease 与当前 committed manifest。同一 owner 正常续约不递增 epoch。
- lease 保存 `{node_id, incarnation, owner_epoch}`，默认 90s；每 30s asyncio 续约。renew/release/commit 必须原子比较完整身份与有效租约，禁止无条件 `invalidate(session_key)` 删除他人的租约。
- 节点地址按 scope + node_id + incarnation 登记；校验内部 token，限制目标为部署允许的内部地址，拒绝代理环路。
- **心跳失败 → fail-closed**：禁止新 turn、manifest 提交和后续工具派发；尽力取消 in-flight。已执行的外部副作用不可撤销，因此本协议只保证历史提交 fencing，不保证工具副作用 exactly-once。
- Redis 的非回退与单一协调主假设见 Phase 0。故障切换无法确认该假设时停写并进入受控恢复，不以托管 HA 自动等价于安全 fencing。

入口：api_server 读到 `session_key`（header 或已绑定会话）后 `claim`：

- `is_mine=True` → 本地处理（必要时走 §3.3 拉取/replace）。
- `is_mine=False` → **内部反代**到 owner（不用 302）。本阶段保证 chat/completions SSE；传播取消/断线、限定单跳并配置背压。browser-control WebSocket 不提供跨节点迁移，返回明确不支持错误。
- 请求体若已在鉴权时读完，反代转发缓冲后的 body。长 turn 期间心跳续约；代理超时独立配置，不得用 90s 租约当 HTTP 超时。

**主路径是 api_server**，不是 `GatewayAgentCacheMixin`。现码 api_server 每请求重建 `AIAgent`，MemoryManager 停在 `ApiServerMemorySessions`（进程内、**不随快照迁移**）。

进程内 `turn_lease`（按 resolved `session_id` 的 `asyncio.Lock`，含 compression `rebind`）保持不变；恢复和 conversation 解析必须先完成，避免不同别名将同一 transcript claim 到不同节点。

#### Phase 1 端点契约

- 支持 `/v1/chat/completions`：带显式 session_key 的持久 conversation；无 header 的请求保持独立、无续接保证，不把空 key 当全局会话。
- 显式 session_id 必须通过 manifest 验证其所属 conversation。未知或与 header 不一致返回明确 4xx；禁止分别按两个别名 claim 同一 transcript。
- reset/branch 若尚无原子 manifest 更新协议，集群入口明确拒绝；不能静默走本地路径。
- `/v1/runs` 及 events/get/stop/approval、response store、artifacts 本阶段不承诺跨节点可用，集群入口须明确关闭或返回不支持错误。未来启用须补 run_id/response_id → conversation/owner 索引与 owner 丢失语义，不能只反代创建请求。
- 内部代理入口不再次扣限流/配额；认证内部来源并绑定请求 ID、目标 profile 与执行上下文，详见 Phase 2。

### 3.2 快照协议（对象存储）

复用 `hermes_state_portability.py` 的导出能力，但 **禁止**用 `import_sessions` 的 skip-existing 当迁移。需要新 sibling：`import_sessions_replace`（或等价），按 lineage **覆盖**本地行。导出：

```
export_session_lineage(session_id, include_inactive=True)
```

压缩 JSON（不是 SQLite dump）。布局：

```text
s3://<bucket>/hermes/<scope>/conversations/<conversation_id>/
    snapshot.<owner_epoch>.<snapshot_revision>.<sha256>.json.zst
```

Redis manifest 保存 `schema_version, min_reader_version, conversation_id, reset_revision, tip_session_id, lineage_session_ids, owner_epoch, snapshot_revision, object_key, sha256, exported_at, message_count`。对象存储不再维护第二个权威 `meta.json`。

#### 提交与恢复协议（取代 v1.0/v1.1 generation 比较）

1. **claim** 原子取得新 owner epoch `E` 与已提交 manifest `M`。首次无 manifest 可新建；有 tombstone/reset 状态必须尊重，不复活旧对象。
2. **恢复**：校验 manifest 版本和哈希，下载已提交对象，在本地单事务 replace 全 lineage、归档标志与必要关联行；导入后重建 conversation 绑定。完整本地 watermark 必须匹配 committed revision + 哈希且无 dirty 才可免下载。未提交本地分叉隔离保存，不作为新 owner 的权威历史。
3. **一致导出**：同 conversation 串行回写；在 turn 锁或一致读事务下获取固定内容及 dirty sequence。revision 在 conversation 内单调递增，epoch 与 revision 含义分开；禁止用 epoch 判断内容新旧。
4. **上传**：以 epoch/revision/hash 唯一命名不可变 blob；失败不发布指针。校验目标 S3 兼容端点的写成功、读可见性、校验和及不可变写能力；不依赖跨 Redis/S3 的伪 CAS。
5. **唯一提交点**：Redis Lua 在同一原子操作内验证有效 lease、owner/incarnation/epoch、预期 manifest revision/reset revision，再发布新 manifest。租约已失效或 base revision 已变则拒绝。上传后、提交前崩溃只留下孤儿 blob，不构成成功快照。
6. **清 dirty**：只清本次导出所覆盖的 sequence；导出后新增修改仍 dirty。失败保留 dirty 并有界重试，不让旧任务清掉新修改。
7. **接管**：新 owner 只恢复 committed manifest。A 在上传/提交之间被 B 接管后，A 的 Lua 提交必须失败；不允许先 GET 租约再无条件写另一系统。

所有参与一次 Lua 的 key 必须同 Redis Cluster hash slot；实现前明确 key 编码。Redis epoch/manifest 不可回退是协议硬前提，不满足时不能通过本协议验收。导入失败不得开放该 conversation；旧缓存不得作为隐式降级源。

快照 schema 明确 lineage segments、message active/compacted flags、关联记录的替换范围与删除规则。`session_model_usage` 不是现有 portability 的自动完整导出项；若需要其迁移，须显式扩展 payload 并验证，不能仅凭 session 行宣称账单无损。

#### 回写策略（不要 5 分钟全量所有会话）

- **dirty-bit**：仅当 transcript 相对上次成功快照有变化才写。
- `on_turn_end: true`：turn 成功结束后异步回写该 session（默认）。
- 周期扫描：只扫 dirty，默认 300s 作为漏网兜底，不是全量上传。
- drain / 进程退出：对仍持有的 dirty 会话强制回写。

附录 B 旧「1 万会话 × 5min 全量 = ¥0」不成立：成本是写请求。dirty-bit 是 Phase 1 的一部分，不是 Phase 4 优化。

#### 一致性边界

- 成功快照 = blob 可读取且 Redis manifest 已原子提交；仅上传成功不算。
- 猝死恢复至最后 committed revision。异步回写积压/失败时可能丢多轮，不承诺“最多最后一 turn”；记录 snapshot lag、dirty turn 数与最老 dirty 年龄。
- 默认响应不等待快照提交，因此客户端收到成功不等于历史已耐久。需更强承诺时另设等待提交模式；SSE 已输出内容不能事后撤回。
- 优雅退出只有全部 dirty 提交成功、in-flight 清零、条件释放完成才算 drain 成功。依赖不可用或超时返回未完成，不假称无损。
- routing/handoff/rewind/活动状态仍不迁移，但稳定 conversation 绑定必须依据 manifest 重建。
- 基础指标在 Phase 1 即交付：租约续约失败、proxy 错误、snapshot lag/失败、冷恢复 P95、迁移与 drain 未完成数；沿用现有监控配置门，不能等队列阶段才观测。

### 3.3 日志

`agent/log_context.py`：`contextvars` 持有四元组；api_server 入口设置；`hermes_logging.py` formatter 追加 `[u= a= s= n=]`。缺省字段留空，不伪造。

### 3.4 Drain

现状：`{HERMES_HOME}/.drain_request.json` + watcher。集群：

1. `POST /admin/drain`（节点间 token）或 preStop 调用同一实现。
2. `stop_new_claims`；in-flight turn 等到上限（默认 120s）。
3. readiness 摘牌；拒绝新 turn（含已亲和会话的新请求），但继续续约以保护 in-flight。dirty 快照提交后按 owner/incarnation/epoch 条件 release。
4. 返回 drain 状态与未提交会话数。所有步骤完成后退出；超时/依赖故障为 incomplete，强杀走猝死路径。snapshot 上传和提交时间计入 drain 预算。

Phase 1 验收必须包含 drain，不要拖到 Phase 5 才有优雅退出。

---

## 4. 配置

```yaml
cluster:
  enabled: false
  namespace: ""                   # enabled 时必填；部署稳定身份
  profile_id: ""                  # 每 home 映射；跨节点一致
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
| S4 | 快照 lineage 导出 + replace 导入 + 原子 manifest + dirty sequence | 杀 A → B 恢复至最后快照；A 复活不能覆盖；回流 A 不吃陈旧本地 | 同上 |
| S5 | drain HTTP + 与 drain_control 对齐 | 优雅退出后下一节点无缺口 transcript | 同上 |
| S6 | 日志四元组 | 集群请求日志含 s=/n= | 同上 |

Gateway **和** CLI 两边加载 `cluster.*`。

---

## 6. 验收

1. `cluster.enabled=false`：现有测试（含 gateway E2E）全绿。
2. 双节点 compose（2 gateway + redis + minio）：同一 `session_key` 连续 20 turn 落同一节点；跨节点请求被反代。
3. **优雅退出**：drain 后滚动重启，transcript 连续（允许 prompt cache 变冷）。
4. **猝死**：`kill -9` → 租约到期后接管（另计恢复耗时） → 历史恢复至最后成功快照 → 僵尸写被拒。不称为「无感知」。
5. 回流：A→B→A 后 transcript 含 B 期间的 turn（replace，不是 skip）。
6. 同 epoch 连续多次快照产生不同 revision；旧回写不能覆盖新 manifest 或清新 dirty。
7. 暂停 A 于上传与 Lua 提交之间，B 接管后恢复 A：A 提交失败。
8. compression 后空 state.db，仅带 session_key 冷恢复；不同挂载路径 A→B→A 仍读同一 manifest。
9. Redis 主从切换/恢复备份：验证 epoch/manifest 非回退保证，或验证停写与旧 owner 隔离流程；不能把普通重启当等价测试。
10. 对象存储失败、SSE 取消、drain 超时、显式 session_id/header 不匹配、未支持端点均有契约测试。
11. 明确不验：memory freeze、工作区文件、cron、Telegram 多副本；不宣称工具副作用 exactly-once。

---

## 7. 风险

| 风险 | 评估 | 缓解 |
|---|---|---|
| 大 session 导出 | 中 | zstd；超限告警；不在本阶段做增量 chunk |
| Redis 单点 | 生产前用托管 Redis | 验证非回退假设；受控恢复，不能默认宣称零丢失 |
| 内部反代双跳 / SSE | 中 | 亲和保证命中率；SSE 测过再标 S3 完成 |
| Kodo 条件写不完整 | 中 | S5 用 MinIO 验协议；生产前验证 endpoint 不可变写/哈希/读可见性 |
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
