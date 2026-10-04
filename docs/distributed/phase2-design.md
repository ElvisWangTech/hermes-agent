# Hermes 分布式改造 · Phase 2 详细设计

> 版本：v1.0（评审稿） ｜ 状态：待确认 ｜ 前置：Phase 1 已验收（coordination 包、cluster.db、api_server 入口管线）
>
> 目标一句话：**让集群可以对公网开放**——完整的多租户鉴权、请求签名、按用户/Agent 维度的配额与限流。

---

## 1. 目标与非目标

### 目标
1. 所有外部请求经统一鉴权（API Key + 可选 JWT），密钥不落环境变量、可吊销、可轮换。
2. 授权模型从「平台 allowlist 环境变量」升级为 DB 表驱动的用户-代理授权关系。
3. 按 userId / agentId 维度的三重管控：速率限流（QPS）、调用配额（日/月 Token 量）、用量统计（计费依据）。
4. 请求签名（HMAC）防重放，供对公网开放的部署形态使用。
5. 审计日志：谁在何时调用了哪个 agent、消耗多少 token、结果如何。

### 非目标
- 不做完整计费/支付系统，只产出计费所需的用量数据。
- 不改消息平台侧的个人 bot 场景（allowlist 路径保留为 `local` 模式行为）。
- 不做 SSO/OIDC 企业登录（Phase 2 只面向 API 消费者；TUI/Web 的 OIDC 已有雏形，不在本阶段扩展）。

---

## 2. 总体架构

```
Client
  │  ① API Key + HMAC 签名头
  ▼
┌────────────────────────────────────────────┐
│ api_server 入口中间件链（新增，按序执行）        │
│  1. AuthN    密钥→userId，签名/时间戳/重放校验   │
│  2. AuthZ    ConfigStore.check_access(u, a)   │
│  3. RateLimit Redis 令牌桶（u 维度 + a 维度）    │
│  4. Quota    Redis 计数器预检（日/月 token 配额）│
│  ── 通过后进入 Phase 1 的 claim/反代管线 ──      │
│  5. Metering 响应后回填实际 token 用量          │
└────────────────────────────────────────────┘
```

设计要点：中间件链全部挂在 api_server 入口，**agent 核心零改动**；`local` 模式下中间件链整体短路（单体用户行为不变）。

---

## 3. 数据模型（cluster.db 新增四张表）

```sql
-- 用户
CREATE TABLE users (
    user_id     TEXT PRIMARY KEY,
    display_name TEXT,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | suspended
    created_at  INTEGER NOT NULL
);

-- API 密钥（只存哈希，明文只在签发时展示一次）
CREATE TABLE api_keys (
    key_id      TEXT PRIMARY KEY,                  -- 前缀可辨识，如 hk_9f3a...
    key_hash    TEXT NOT NULL,                     -- sha256
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    label       TEXT,
    status      TEXT NOT NULL DEFAULT 'active',    -- active | revoked
    expires_at  INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_api_keys_user ON api_keys(user_id);

-- 配额策略（每用户/每 agent 一条；NULL 表示不限制）
CREATE TABLE quotas (
    scope       TEXT NOT NULL,                     -- user | agent
    scope_id    TEXT NOT NULL,                     -- user_id 或 agent_id
    qps_limit   INTEGER,                           -- 速率
    daily_tokens  INTEGER,                         -- 日 token 配额
    monthly_tokens INTEGER,                        -- 月 token 配额
    PRIMARY KEY (scope, scope_id)
);

-- 用量流水（审计 + 计费依据；异步批量落库）
CREATE TABLE usage_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    session_id  TEXT,
    node_id     TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    latency_ms  INTEGER,
    status      TEXT NOT NULL                      -- ok | error | rate_limited | quota_exceeded
);
CREATE INDEX idx_usage_user_ts ON usage_records(user_id, ts);
CREATE INDEX idx_usage_agent_ts ON usage_records(agent_id, ts);
```

- `agents` 表（Phase 1 已建）增加 `owner user_id` 外键约束与 `status` 字段（迁移脚本提供）。
- 授权关系：Phase 1 的 `check_access` 规则升级为——`agents.user_id == 请求 userId`（属主）**或** 存在显式授权记录。授权记录复用 `quotas` 表达共享场景，或按需加 `agent_grants(agent_id, user_id)` 表（多对一共享时启用）。

## 4. Redis 数据结构（实时管控面）

| Key | 类型 | 用途 | TTL |
|---|---|---|---|
| `rl:user:{user_id}` | 令牌桶（Lua） | 用户 QPS 限流 | 60s |
| `rl:agent:{agent_id}` | 令牌桶（Lua） | agent QPS 限流 | 60s |
| `quota:u:{user_id}:d:{yyyymmdd}` | INCR 计数 | 日 token 累计 | 48h |
| `quota:u:{user_id}:m:{yyyymm}` | INCR 计数 | 月 token 累计 | 62 天 |
| `quota:a:{agent_id}:d:{yyyymmdd}` | INCR 计数 | agent 日累计（防单 agent 被刷） | 48h |
| `replay:{key_id}:{nonce}` | SET NX | HMAC 防重放 | 5min（=签名有效期） |
| `cfg:quota:{scope}:{id}` | 哈希缓存 | quotas 表缓存 | 60s |

**令牌桶实现**：Lua 脚本原子完成「按速率注水 → 扣减 → 返回剩余/拒绝」，单 Redis 往返。

**配额语义**：请求前预检（计数器 ≥ 配额 → 429），响应后按**实际 token** `INCRBY` 回填。预检是乐观策略，允许秒级少量超发；需要严格不超额时可对 `INCRBY` 结果判断并拒绝后续请求——默认乐观，严格模式走配置。

## 5. 鉴权与签名规范

```
请求头：
  X-Hermes-Key:      hk_9f3a...
  X-Hermes-Timestamp: 1730000000        # 秒级，有效期 ±300s
  X-Hermes-Nonce:    <16 字节随机 hex>   # 防重放
  X-Hermes-Signature: hex(HMAC_SHA256(secret, method\npath\ntimestamp\nnonce\nsha256(body)))
```

- 兼容模式：`Authorization: Bearer <key>` 无签名——仅在内网/可信网关后允许，配置项 `cluster.auth.require_signature: true` 可强制全量签名。
- 密钥查询：Redis 缓存 `apikey:{key_id}` → `{key_hash, user_id, status}`（60s TTL），吊销后最坏 60s 生效；管理端吊销操作同时主动 `DEL` 缓存做到秒级生效。

## 6. 新增/修改文件

### 新增（7 个）
| 文件 | 用途 |
|---|---|
| `coordination/rate_limiter.py` | `RateLimiter` 接口 + local（进程内桶）/ redis（Lua 令牌桶）实现 |
| `coordination/quota.py` | 配额预检与用量回填；乐观/严格两种模式 |
| `gateway/authn_middleware.py` | API Key 解析、HMAC 签名与时间戳/重放校验 |
| `gateway/authz_db.py` | 表驱动授权（升级 Phase 1 的 check_access 实现） |
| `coordination/metering.py` | 用量采集：响应后提取 token 数 → Redis 计数 + 批量落 usage_records |
| `hermes_cli/keys_admin.py` | 管理 CLI：`hermes keys create/revoke/list`、`hermes quota set`（密钥明文仅创建时输出一次） |
| `tests/coordination/test_authz_rate.py` | 签名/重放/限流/配额/吊销生效时延 用例 |

### 修改（4 个）
| 文件 | 改动 |
|---|---|
| `gateway/platforms/api_server.py` | 入口挂中间件链（AuthN→AuthZ→RateLimit→Quota→claim→…→Metering） |
| `coordination/cluster_db.py` | 四张新表 schema + 迁移 |
| `gateway/authz_mixin.py` | allowlist 路径标注为 `local` 模式专用，集群模式走 authz_db |
| `hermes_cli/config.py` | `cluster.auth.*` / `cluster.ratelimit.*` 配置段 |

## 7. 配置设计

```yaml
cluster:
  auth:
    require_signature: false     # 公网部署必须 true
    signature_ttl_sec: 300
    jwt_enabled: false           # 预留，签发方配置后置
  ratelimit:
    default_user_qps: 10
    default_agent_qps: 20
  quota:
    mode: "optimistic"           # optimistic | strict
    default_daily_tokens: 0      # 0 = 不限
```

## 8. 实施步骤与验收

| 步 | 内容 | 验证 |
|---|---|---|
| S1 | cluster.db 四表 + 管理 CLI | keys create/revoke/list 全流程 |
| S2 | AuthN 中间件（含签名/重放） | 伪造签名/过期时间戳/重放 nonce → 401 |
| S3 | authz_db 表驱动授权 | 属主 200 / 未授权 403 / 吊销 key 60s 内 401 |
| S4 | Redis 令牌桶限流 | 突发 2×QPS → 超出部分 429，1s 后恢复 |
| S5 | 配额预检 + 回填 + metering 落库 | 配额用尽 → 429；usage_records 与实际消耗一致（±1%） |
| S6 | local 模式回归 | `cluster.enabled=false` 现有测试全绿 |

**验收标准**：双节点对公网形态演练——任意节点入口鉴权一致（限流/配额计数是 Redis 全局值，天然跨节点一致）；吊销密钥 ≤60s 全网生效；usage_records 可支撑按用户/月聚合出账单数据。

## 9. 风险与边界

| 风险 | 缓解 |
|---|---|
| 乐观配额允许少量超发 | 文档化语义；strict 模式可选；超额部分在下一请求拒绝 |
| Redis 宕机导致限流/配额失效 | 降级策略配置：`fail_open`（放行，记告警）或 `fail_closed`（503）；默认 fail_closed |
| usage_records 写入放大 | metering 批量异步落库（每 100 条或 5s 一刷）；该表只做追加，可定期归档 |
| 签名对客户端有接入成本 | 提供 Python/JS SDK 片段；内网部署可关签名 |
