# Hermes 分布式改造 · Phase 2 详细设计

> 版本：v1.1（修正稿） ｜ 状态：待确认 ｜ 前置：[Phase 0](phase0-design.md)、[Phase 1](phase1-design.md) 已验收
>
> 目标一句话：api_server 可按 **Bearer 密钥** 识别调用方，并按 profile（`agent_id`）做 Redis 限流与 token 配额。
>
> 相对 v1.0 的撤回：节点 `cluster.db` 当权威库、自定义 HMAC 当默认鉴权、改 `gateway/authz_mixin.py`、新建 `usage_records` 本地表、密钥「不落环境变量而进 SQLite」、未具备 Redis 健康检查就对公网开放。

---

## 1. 目标与非目标

### 目标

1. 默认鉴权保持 OpenAI 兼容：`Authorization: Bearer <key>`。
2. 密钥只存哈希；可吊销、可按 profile 作用域绑定；明文仅创建时展示一次。
3. 按 `user_id`（密钥主体）与 `agent_id`（= profile）做 QPS 限流与日/月 token 配额（Redis 计数）。
4. HMAC 签名（时间戳 + nonce）为 **可选**，仅 `cluster.auth.require_signature: true` 时强制（公网加强，不是默认客户端路径）。
5. 用量仍以现有 `session_model_usage` / 会话 token 列为准；配额用 Redis 计数器，不另建本地流水库。

### 非目标

- 不做支付/账单系统。
- 不改消息平台个人 bot 的 allowlist（**不修改** `gateway/authz_mixin.py`）。
- 不做 SSO/OIDC。
- 不把节点 SQLite 当成多副本的用户/密钥源。
- JWT 仅预留配置位，本阶段不实现签发。

### 对公网的前提（本阶段验收的一部分）

- 托管 Redis（或等价 HA）可用。
- `/healthz` 在 Redis 不可达时失败（限流默认 fail_closed）。
- 不要求 Phase 5 的 K8s。

---

## 2. 总体架构

```
Client（OpenAI SDK：Authorization: Bearer）
  │  可选：X-Hermes-Timestamp / Nonce / Signature
  ▼
api_server 入口 sibling 中间件（cluster.enabled 时）
  1. AuthN   Bearer → key_hash 查找 → user_id；可选 HMAC
  2. AuthZ   该 user 是否允许调用本请求的 profile（agent_id）
  3. RateLimit  Redis 令牌桶（user + agent）
  4. Quota      Redis 日/月 token 预检
  ── 进入 Phase 1 的 claim / 反代 ──
  5. 响应后按实际 token INCRBY 配额计数
```

`cluster.enabled=false`：整链短路，今日 `_check_auth`（单 key）不变。

Agent 核心、工具 schema、消息平台 authz **零改动**。

---

## 3. 数据放哪

权威面在 **Redis**（及管理 CLI 写入 Redis 的同一 hash）。节点进程内存 60s 缓存可有；**禁止** `cluster.db` 当源。

| Redis key（均带 `{home}:` 前缀，见 Phase 0） | 类型 | 用途 | TTL |
|---|---|---|---|
| `apikey:{key_id}` | hash | `key_hash, user_id, status, scopes` | 无（吊销 DEL） |
| `grant:{user_id}:{agent_id}` | string | 显式授权（缺省：密钥创建时绑定的 profile） | 无 |
| `rl:user:{user_id}` / `rl:agent:{agent_id}` | 令牌桶 Lua | QPS | 60s |
| `quota:u:{user_id}:d:{yyyymmdd}` 等 | INCR | token 累计 | 48h / 62d |
| `replay:{key_id}:{nonce}` | SET NX | 仅签名模式防重放 | 5min |

密钥哈希：`sha256` + 服务端 pepper（pepper 在 `.env` / Secret，不在 yaml）。查找：Bearer 明文算 hash，按 `key_id` 前缀（如 `hk_`）定位条目，再 `hmac.compare_digest` 比对。

**兼容今日单 key**：未配置任何 `apikey:*` 时，行为等于现有 `API_SERVER_KEY`（profile-scoped secret）。管理 CLI 写入第一条集群密钥后，才走表驱动。

配额语义：请求前预检（计数 ≥ 配额 → 429）；响应后按实际 token `INCRBY`。默认乐观（允许秒级超发）；`cluster.quota.mode: strict` 在 INCR 后若超限则拒绝后续请求。

Redis 宕机：默认 **fail_closed**（503），可配 `fail_open`（仅内网）。对公网禁止 fail_open。

---

## 4. 鉴权规范

**默认（必须能被 OpenAI 官方客户端使用）：**

```
Authorization: Bearer hk_...
```

**可选签名（require_signature=true）：**

```
X-Hermes-Timestamp: <unix 秒，± signature_ttl_sec>
X-Hermes-Nonce: <16 字节 hex>
X-Hermes-Signature: hex(HMAC_SHA256(secret, method\npath\ntimestamp\nnonce\nsha256(body)))
```

AuthZ：请求 URL 的 `/p/<profile>/` 或默认 profile = `agent_id`。允许当且仅当 grant 存在，或密钥的 `scopes` 含该 profile。无 grant → 403。吊销：`DEL apikey:{id}`，立即全网生效（不靠 60s 缓存当唯一手段；进程缓存必须可被主动失效或 TTL ≤ 5s）。

---

## 5. 用量

- **不要**新建 `usage_records` SQLite。
- Token 数从现有 turn/session 记账读取，回填 Redis 配额。
- 若需要跨节点账单导出：只读聚合 `session_model_usage`（随 Phase 1 快照走的会话行）或离线任务扫对象存储；本阶段不交付计费管道。
- Metrics 带 `user` 标签：遵守仓库 opt-in；默认指标只用 route/status/node（详见 Phase 4）。

---

## 6. 文件意向

新增（均非 facade）：

| 意向路径 | 用途 |
|---|---|
| `gateway/coordination/rate_limiter.py` | local 进程内桶 / Redis Lua |
| `gateway/coordination/quota.py` | 预检与 INCRBY |
| `gateway/platforms/api_server_cluster_auth.py` | Bearer + 可选 HMAC；AuthZ grant |
| `hermes_cli/keys_admin.py` | `hermes keys create/revoke/list`、`hermes quota set`（写 Redis） |
| 契约测试 | 签名可选、重放、429、吊销立即 401、Bearer 无签名通过 |

修改：api_server **sibling** 挂中间件链；`gateway/config*.py` + CLI config 的 `cluster.auth.*` / `ratelimit.*` / `quota.*`。

**不修改** `gateway/authz_mixin.py`。

---

## 7. 配置

```yaml
cluster:
  auth:
    require_signature: false     # 公网可 true；默认 false 以保持 SDK 兼容
    signature_ttl_sec: 300
    jwt_enabled: false           # 预留
  ratelimit:
    default_user_qps: 10
    default_agent_qps: 20
    fail_closed: true
  quota:
    mode: "optimistic"
    default_daily_tokens: 0      # 0 = 不限
```

Pepper、Redis URL、密钥明文不出现在 yaml。

---

## 8. 实施步骤与验收

| 步 | 内容 | 验证 |
|---|---|---|
| S1 | Redis 密钥/grant 结构 + 管理 CLI | create/revoke/list |
| S2 | Bearer AuthN + 单 key 回退 | 无集群密钥时旧 `API_SERVER_KEY` 仍工作；错误 Bearer 401 |
| S3 | AuthZ grant | 属主 200 / 未授权 403 / 吊销立即 401 |
| S4 | 令牌桶 | 突发超 QPS → 429 |
| S5 | 配额预检 + 回填 | 用尽 429；与 turn 实际 token 一致（合同：同量级，非快照笔数） |
| S6 | `cluster.enabled=false` 回归 | 现有测试全绿 |
| S7 | Redis 宕机 fail_closed | 503；内网可测 fail_open |

验收：双节点鉴权一致（计数在 Redis）；OpenAI 兼容客户端仅带 Bearer 可调 `/v1/chat/completions`；消息平台 bot 路径行为不变。

---

## 9. 风险

| 风险 | 缓解 |
|---|---|
| 乐观配额超发 | 文档化；strict 可选 |
| Redis 宕机 | 对公网 fail_closed；健康检查 |
| HMAC 提高接入成本 | 默认关闭 |
| 进程缓存导致吊销延迟 | 短 TTL 或吊销 pub/sub；不以 60s 为承诺 |
