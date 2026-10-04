# Hermes 分布式改造 · Phase 1 详细设计

> 版本：v1.0（评审稿） ｜ 状态：待确认 ｜ 对应决策：会话亲和 + 对象存储同步 / HTTP API 优先
>
> 本文档只覆盖 Phase 1（无状态化底座）。所有分布式能力为**可选配置**：未启用时行为与现状完全一致。

---

## 1. 目标与非目标

### 目标
1. Gateway 进程可水平运行多副本（≥2），同一会话由亲和路由固定到同一节点。
2. 节点重启 / 替换后，会话状态可从对象存储恢复，不丢会话。
3. Agent 实例不再假设进程独占：按会话创建、带租约、可逐出。
4. API server（OpenAI 兼容入口）具备 userId→agentId 归属校验。
5. 日志全链路携带 `userId / agentId / sessionId / nodeId`。

### 非目标（本阶段明确不做）
- 不改消息平台长轮询适配器（Telegram 等），Phase 5 处理。
- 不引入任务队列，webhook 异步化推迟到 Phase 4。
- 不做多租户数据模型重构，仅做归属校验（Phase 2 做完整鉴权与配额）。
- 不替换 SQLite 为 PostgreSQL；全部现有 SQLite 防腐工程保留。

---

## 2. 总体架构

```
                 ┌─────────────┐
   Client ─────► │  LB / 反代   │  （无状态转发，无需 sticky）
                 └──────┬──────┘
            ┌───────────┼───────────┐
            ▼           ▼           ▼
      ┌──────────┐┌──────────┐┌──────────┐
      │ gateway-1││ gateway-2││ gateway-N│  api_server 入口
      │ nodeId=A ││ nodeId=B ││ nodeId=C │  每节点本地 state.db（热缓存）
      └────┬─────┘└────┬─────┘└────┬─────┘
           └───────────┼───────────┘
              ┌────────┴────────┐
              ▼                 ▼
        ┌───────────┐    ┌────────────┐
        │   Redis   │    │ 对象存储     │
        │ 锁/亲和/归属│    │ S3 兼容协议   │
        │           │    │ 默认七牛 Kodo │
        └───────────┘    └────────────┘
```

核心思想：**会话亲和（session affinity）+ 状态快照外置**。Redis 只存协调元数据（锁、亲和映射、归属表缓存），不承载会话内容；会话内容仍以 SQLite 形式存在节点本地（热路径零改动），在「会话迁移 / 节点失效」时经对象存储交接。

---

## 3. 模块设计

### 3.1 `coordination/` 模块（全部改造的唯一支点）

新增顶层包，三个小接口，各两个实现。接口刻意收窄，只覆盖 Phase 1 需要的原语。

```
coordination/
├── __init__.py          # get_backend() 工厂，按 config 返回 local 或 redis
├── base.py              # 三个 Protocol/ABC
├── local.py             # 现状行为封装（默认，零依赖）
└── redis_backend.py     # redis 实现（可选依赖 redis>=5）
```

#### 3.1.1 `LockBackend` —— 分布式锁

```python
class LockBackend(Protocol):
    def acquire(self, key: str, ttl: float, *, fencing: bool = True) -> "Lease | None": ...
    # Lease: .token (fencing token, 单调递增), .renew(), .release(), .valid
```

- `local` 实现：进程内 `threading.Lock` + 现有文件锁语义（保持现状）。
- `redis` 实现：`SET key token NX PX ttl` + Lua 释放脚本（比对 token）；fencing token 用 `INCR <key>:fence` 生成，随每次 acquire 单调递增。
- **消费点**：`gateway/turn_lease.py`（会话串行化）、cron tick 锁、会话状态同步的写互斥（§3.3）。

#### 3.1.2 `SessionRouter` —— 会话亲和路由

```python
class SessionRouter(Protocol):
    def claim(self, session_key: str, node_id: str, ttl: float) -> "Claim": ...
    # Claim: .owner_node, .is_mine, .generation (fencing), .heartbeat(), .release()
    def lookup(self, session_key: str) -> "str | None": ...   # 查询当前归属节点
    def invalidate(self, session_key: str) -> None: ...
```

- Redis key：`sess:route:{session_key}` → `{node_id, generation, claimed_at}`，PX = ttl（默认 90s），持有方每 30s 心跳续期。
- `claim` 语义：无归属或归属租约过期 → 当前节点接管（generation+1）；归属他节点且未过期 → 返回 `is_mine=False`。
- **入口行为**：api_server 收到请求 → `claim(session_key)`：
  - `is_mine=True` → 本地处理；
  - `is_mine=False` → **302/307 重定向或内部反代到 owner 节点**（首选内部反代，客户端无感；节点地址登记在 `node:addr:{node_id}`，TTL 与心跳同周期刷新）。

#### 3.1.3 `ConfigStore` —— Agent 配置与归属

```python
class ConfigStore(Protocol):
    def get_agent_config(self, agent_id: str) -> "AgentConfig | None": ...
    def get_owner(self, agent_id: str) -> "str | None": ...      # userId 归属
    def check_access(self, user_id: str, agent_id: str) -> bool: ...
```

- `local` 实现：读现有 `config.yaml` / profiles（现状）。
- `redis` 实现：读 `agent:cfg:{agent_id}`、`agent:owner:{agent_id}` 哈希；源数据在 DB（§3.4），Redis 作 60s 缓存。

### 3.2 Agent 实例生命周期改造

| 现状 | 改造后 |
|---|---|
| `GatewayAgentCacheMixin` 进程内 LRU（128 上限 / 1h 空闲逐出），turn lease 为 asyncio.Lock | 缓存签名不变；**加一层「租约感知」**：缓存命中前先 `SessionRouter.claim`；逐出 / 租约丢失时触发状态回写（§3.3）；turn lease 改走 `LockBackend`（local 模式下退化为现有 asyncio.Lock） |

- 改造文件：`gateway/run_agent_cache.py`（逐出钩子）、`gateway/turn_lease.py`（锁后端注入）、`gateway/platforms/api_server.py`（入口接入 claim/反代）。
- **不改造**：CLI、TUI、Electron——它们继续走 `local` 后端，行为不变。

### 3.3 会话状态同步（对象存储）

复用现有 `hermes_state_portability.py` 的导入导出能力，按 session 维度做快照。

#### 对象存储选型（国内方案）

**结论：默认七牛云 Kodo，接口层走 S3 兼容协议（boto3），任何 S3 兼容厂商可配置切换。** 选型依据见附录 B。`snapshot_store.py` 不绑定具体厂商，仅要求「S3 兼容 API + 支持条件写/版本号语义」。

#### 存储布局
```
s3://<bucket>/hermes/sessions/{session_id}/
    ├── snapshot.db.zst          # 该 session 的导出快照（zstd 压缩）
    └── meta.json                # {generation, node_id, exported_at, message_count, sha256}
```

#### 协议（关键：与 fencing token 绑定，防脑裂）
1. **获取（acquire-miss）**：节点 `claim` 成功且本地 state.db 无此 session → 从对象存储下载 → 校验 `meta.json.generation == Redis 中的 generation` → 导入本地。
2. **回写（release/evict/周期）**：导出该 session → 上传 `snapshot.db.zst` + `meta.json`（携带当前 fencing generation）→ **条件写**：仅当上传时的 generation 仍是 Redis 中最新值才更新 `meta.json`；否则丢弃（说明已被他节点接管）。
3. **故障切换**：节点死亡 → Redis 租约 90s 过期 → 他节点 claim 成功 → 走第 1 步从最后快照恢复（最多丢失「距上次回写」的增量，由周期回写兜底，默认 5 分钟 + 每次 turn 结束异步回写）。

#### 一致性边界（如实说明）
- 正常路径（租约内）零丢失：所有写都在持有 fencing 租约的节点本地完成。
- 异常路径（节点猝死）丢失窗口 ≤ 周期回写间隔；turn 结束回写可将典型窗口压到秒级。
- fencing generation 保证「僵尸节点」苏醒后无法覆盖新快照。

### 3.4 归属校验与数据表

新增一张表（独立小库 `cluster.db`，不动现有 state.db schema）：

```sql
CREATE TABLE agents (
    agent_id    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,          -- 归属用户
    name        TEXT,
    config_json TEXT NOT NULL,          # model/tools/memory 等配置
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_agents_user ON agents(user_id);
```

- api_server 中间件：`API key → userId`（Phase 1 用静态映射表，Phase 2 换 JWT）→ `ConfigStore.check_access(userId, agentId)` → 拒绝返回 403。
- 现有 `hermes_state_sessions` 的 `user_id` 列写入时与归属表对齐，供审计。

### 3.5 日志埋点

- 新增 `agent/log_context.py`：`contextvars.ContextVar` 持有 `user_id / agent_id / session_id / node_id`；入口中间件设置，`logging.Filter` 注入 record。
- 格式器统一追加 `[u=<userId> a=<agentId> s=<sessionId> n=<nodeId>]`。
- 消耗点少：只改 `hermes_logging.py` 的 formatter 装配处 + api_server 入口。

---

## 4. 配置设计（config.yaml 新增段）

```yaml
cluster:
  enabled: false                 # 默认关闭 = 现状单体行为，所有改动零影响
  node_id: ""                    # 默认 hostname-pid
  redis_url: "redis://localhost:6379/0"
  object_store:
    backend: "s3"                # s3 | redis | none（s3 = 任意 S3 兼容厂商，默认对接七牛 Kodo）
    endpoint: "https://s3.cn-east-1.qiniucs.com"   # Kodo S3 兼容端点（按实际区域）
    bucket: "hermes-sessions"
  affinity:
    lease_ttl_sec: 90
    heartbeat_sec: 30
  snapshot:
    periodic_sec: 300
    on_turn_end: true
```

依赖全部可选化：`redis`、`boto3`、`zstandard` 进 `pyproject.toml` 的 `[project.optional-dependencies] cluster = [...]`，未安装时 `cluster.enabled=true` 直接报错退出并提示。

---

## 5. 实施步骤（每步可独立验证、可回滚）

| 步 | 内容 | 验证 | 回滚 |
|---|---|---|---|
| S1 | 新增 `coordination/` 包 + `local` 实现 + 单测 | `local` 行为与现状逐点对齐 | 删包即可 |
| S2 | turn_lease / agent cache 改走 LockBackend（默认 local） | 现有 gateway 测试全绿 | 配置回退 |
| S3 | redis 实现 + 双进程手动验证 | 两进程争同一 session_key，串行正确 | 同上 |
| S4 | api_server 接入 SessionRouter（claim + 内部反代） | 双节点 curl 同一 session，观测路由与重定向 | 同上 |
| S5 | 快照导出/导入 + fencing 条件写 | 杀节点 A → B 接管 → 会话恢复、A 复活后无法写回 | 同上 |
| S6 | 归属表 + check_access 中间件 | 403/200 矩阵用例 | 同上 |
| S7 | 日志埋点 | 日志行含四元组 | 同上 |

## 6. 验证方案（Phase 1 验收标准）

1. **回归**：`cluster.enabled=false` 时现有测试套件全绿（含 gateway E2E）。
2. **双节点**：docker-compose（2 gateway + redis + minio），同一 session 连续 20 turn 全部落同一节点；跨节点请求被正确反代。
3. **故障切换**：`kill -9` 持有节点 → 90s 内另一节点接管 → 会话历史完整恢复至最后快照 → 僵尸节点复活后写被拒绝（fencing 生效）。
4. **归属**：伪造 userId 访问他人 agentId → 403。
5. **重启不丢会话**：滚动重启两节点，会话无感知。

## 7. 风险与边界

| 风险 | 评估 | 缓解 |
|---|---|---|
| 快照导出/导入的性能（大 session） | 中 | zstd 压缩 + 增量回写留作 Phase 4 优化；先限制单 session 导出大小告警 |
| Redis 单点 | 低（Phase 1） | 租约过期只会导致恢复回退到快照，不丢已回写数据；Phase 5 上 Redis HA |
| 内部反代带来双跳延迟 | 低 | 命中率由亲和保证；后续可在 LB 层读 Redis 做 sticky 消跳 |
| 长轮询平台（Telegram）多副本重复消费 | 存在 | Phase 1 文档化限制：多副本仅用于 API 入口，平台适配器仍单副本运行 |
| portability 导出是否覆盖全部会话状态 | 需验证 | S5 步骤先做覆盖性审计（compression 血缘、rewind、ledger），缺什么补什么 |

---

## 8. 工作量估算

约 **新增 8–10 个文件、修改 6–8 个文件**，核心代码量预估 1500–2000 行（含测试）。S1–S3 为纯增量无风险；S4–S5 是关键路径。

确认本文档后，我将按 S1→S7 顺序实施，每步完成后单独汇报验证结果。

---

## 附录 A：Phase 1 文件清单

### A.1 新增文件（10 个）

| 文件 | 用途 |
|---|---|
| `coordination/__init__.py` | 包入口与 `get_backend()` 工厂：读 `config.yaml` 的 `cluster.*` 段，返回 local 或 redis 实现；未启用集群时恒返回 local |
| `coordination/base.py` | 三个 Protocol 接口定义：`LockBackend`（分布式锁 + fencing token）、`SessionRouter`（会话亲和 claim/lookup/心跳）、`ConfigStore`（agent 配置与 userId 归属查询） |
| `coordination/local.py` | local 实现（默认）：锁退化为进程内锁，路由恒归属本节点，配置读现有 config.yaml/profiles——封装**现状行为**，保证未启用集群时零差异 |
| `coordination/redis_backend.py` | redis 实现：锁用 `SET NX PX` + Lua 比对 token 释放 + `INCR` 生成 fencing；路由表、节点地址登记（`node:addr:*`）、归属缓存（60s TTL） |
| `coordination/lease.py` | `Lease` / `Claim` 数据类：token、generation、后台心跳续期线程、释放回调；被 turn_lease 与 agent cache 消费 |
| `coordination/snapshot_store.py` | 对象存取层：session 快照上传/下载（boto3 兼容 S3/MinIO）、zstd 压缩、`meta.json` 的 fencing 条件写（防僵尸节点覆盖） |
| `coordination/session_sync.py` | 同步协议编排：acquire-miss 时「下载→校验 generation→导入本地」；release/逐出/周期触发「导出→条件写回写」；调用 `hermes_state_portability` 做导入导出 |
| `coordination/cluster_db.py` | 独立小库 `cluster.db` 的 schema 与 DAO：`agents` 表（agent_id/user_id/config_json）的增删查；与 state.db 完全解耦 |
| `agent/log_context.py` | contextvars 持有 `user_id/agent_id/session_id/node_id` + `logging.Filter` 注入；入口中间件 set，formatter 消费 |
| `tests/coordination/test_*.py` | 接口契约测试（local/redis 跑同一套用例）、fencing 防脑裂用例、快照往返一致性用例、归属 403 矩阵 |

### A.2 修改文件（7 个）

| 文件 | 改动内容 | 改动量 |
|---|---|---|
| `gateway/turn_lease.py` | turn lease 从 asyncio.Lock 改为经 `LockBackend` 获取（local 模式行为不变）；持锁期间挂心跳 | 小 |
| `gateway/run_agent_cache.py` | agent 缓存命中前加 `SessionRouter.claim`；逐出 / 租约丢失时触发 `session_sync` 回写钩子 | 中 |
| `gateway/platforms/api_server.py` | 入口管线插入三段：API key→userId 解析 → `ConfigStore.check_access` 归属校验（403）→ `claim` 亲和判定（非本节点则内部反代到 owner） | 中 |
| `hermes_logging.py` | formatter 装配处追加四元组字段（消费 `log_context`） | 小 |
| `hermes_cli/config.py` | 解析 `cluster.*` 配置段并校验（启用集群但缺 redis/boto3 依赖时明确报错退出） | 小 |
| `pyproject.toml` | 新增可选依赖组 `cluster = ["redis>=5", "boto3", "zstandard"]` | 小 |
| `docker-compose.yml`（或新增 `docker/docker-compose.cluster.yml`） | Phase 1 验证环境：2×gateway + redis + minio | 小 |

### A.3 明确不动的部分

CLI / TUI / Electron 入口、cron、全部平台长轮询适配器、`hermes_state_*.py` 全系列（SQLite 工程原样保留）、`state.db` schema。

---

## 附录 B：国内对象存储选型比较（2026-10 定价）

场景特征：快照为小对象（均值 50KB–2MB，zstd 压缩）、周期性写入（每会话 5min）、读极少（仅故障切换/迁移）、同云内网传输为主。该场景下**存储费与请求费是唯一有效成本项，外网流量几乎不发生**。

| 厂商 | 标准存储（元/GB/月） | 写请求（元/万次） | 读请求（元/万次） | 免费额度 | S3 兼容 | 备注 |
|---|---|---|---|---|---|---|
| **七牛云 Kodo** | **0.098–0.115** | 0.01 | 0.01 | **每月 10GB 存储 + 10 万次写 + 100 万次读（持续发放）** | 是 | 外网流出 0.26 元/GB 为全网最低档 |
| 华为云 OBS | 0.099 | 0.01 | 0.01 | 无持续额度 | 是 | 存储底价最低之一 |
| 火山引擎 TOS | 0.099 | 0.016 | 0.016 | 无持续额度 | 是 | 请求费略高 |
| 腾讯云 COS | 0.118 | 0.01 | 0.01 | 50GB × 6 个月（新用户一次性） | 是 | 微信生态场景才有加成 |
| 阿里云 OSS | 0.12 | 0.01 | 0.001 | 无持续额度 | 是 | 读请求费最低，但本场景读极少 |

**估算（1 万会话 × 1MB ≈ 10GB，日回写约 290 万次）**：

- 七牛 Kodo：存储 10GB 在免费额度内 → **¥0/月**；超出后约 ¥1.0–1.2/月。
- 华为 OBS / 火山 TOS：约 ¥1.0 + 请求费 ¥3–5 ≈ **¥4–6/月**。
- 阿里 OSS / 腾讯 COS：约 ¥1.2 + 请求费 ≈ **¥4–6/月**。

**结论：选七牛 Kodo。** 三条理由：
1. **持续有效的每月免费额度**（10GB + 百万级请求）完整覆盖 Phase 1 乃至中期规模，成本为 ¥0；其他厂商的免费额度要么没有、要么是新用户一次性。
2. 单价本身处于国内最低档（0.098–0.115 元/GB/月），超出免费额度后与底价厂商持平。
3. S3 兼容 API，`snapshot_store.py` 用 boto3 即可对接，保留切换任意厂商的能力；项目方与七牛生态的既有关系也降低了接入与排障成本。

**风险说明**：选型不锁定厂商——`object_store.backend=s3` + `endpoint` 配置项即可切换华为 OBS / 火山 TOS；若未来要压掉对象存储组件，可切 `backend=redis`（快照存 Redis，中小规模适用，见 §3.3 协议不变）。
