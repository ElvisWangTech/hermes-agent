# Hermes 分布式改造 · Phase 2 详细设计

> 版本：v1.2 ｜ 状态：待确认 ｜ 前置：[Phase 0](phase0-design.md)、[Phase 1](phase1-design.md) 已验收
>
> 目标一句话：Bearer 鉴权、稳定 profile 授权、一次准入与 Redis 软配额结算。
>
> 相对 v1.0 的撤回：节点 `cluster.db` 当权威库、自定义 HMAC 当默认鉴权、改 `gateway/authz_mixin.py`、新建 `usage_records` 本地表、密钥「不落环境变量而进 SQLite」、未具备 Redis 健康检查就对公网开放。

---

## 1. 目标与非目标

### 目标

1. OpenAI 兼容：`Authorization: Bearer <api_key>`。
2. 密钥只存哈希；可吊销、可按 profile 作用域绑定；明文仅创建时展示一次。
3. 按 `user_id`（密钥主体）与 `agent_id`（= profile）做 QPS 限流与日/月 token 配额（Redis 计数）。
4. 本阶段只交付 Bearer；HMAC 延后。仅存 key 哈希不能直接验证以原始 key 为 secret 的 HMAC，不保留无实现的签名开关。
5. 用量仍以现有 `session_model_usage` / 会话 token 列为准；配额用 Redis 计数器，不另建本地流水库。

### 非目标

- 不做支付/账单系统。
- 不改消息平台个人 bot 的 allowlist（**不修改** `gateway/authz_mixin.py`）。
- 不做 SSO/OIDC。
- 不把节点 SQLite 当成多副本的用户/密钥源。
- 不实现 HMAC/JWT，不增加空配置位；未来独立提案说明凭证存储、验证及轮换。

### 对公网的前提（本阶段验收的一部分）

- 托管 Redis（或等价 HA）可用。
- `/healthz` 在 Redis 不可达时失败（限流默认 fail_closed）。
- 不要求 Phase 5 的 K8s。

---

## 2. 总体架构

```
Client（OpenAI SDK：Authorization: Bearer）
  │  Authorization: Bearer <api_key>
  ▼
api_server 入口 sibling 中间件（cluster.enabled 时）
  1. AuthN   Bearer key_hash → user_id（显式 legacy/managed）
  2. AuthZ   该 user 是否允许调用本请求的 profile（agent_id）
  3. RateLimit  Redis 令牌桶（user + agent）
  4. Quota      Redis 日/月 token 预检
  ── 进入 Phase 1 的 claim / 反代 ──
  5. 执行节点按 execution_id 去重结算实际增量
```

`cluster.enabled=false`：整链短路，今日 `_check_auth`（单 key）不变。

Agent 核心、工具 schema、消息平台 authz **零改动**。

---

## 3. 数据放哪

权威面在 **Redis**（管理 CLI 写同一权威源）；凭证与 grant 按 namespace 主体/稳定 profile_id 分区。节点缓存不能替代在线状态校验；禁止 `cluster.db` 当源。原子 user+agent 限额与去重结算涉及的 key 必须同 hash slot；Redis Cluster 的热点取舍须先验证。

| Redis key（namespace 主体与 scope 授权/计数分区） | 类型 | 用途 | TTL |
|---|---|---|---|
| `apikey:{key_id}` | hash | `key_hash, user_id, status, scopes` | 无（吊销 DEL） |
| `grant:{user_id}:{agent_id}` | string | 显式授权（缺省：密钥创建时绑定的 profile） | 无 |
| `rl:user:{user_id}` / `rl:agent:{agent_id}` | 令牌桶 Lua | QPS | 60s |
| `quota:u:{user_id}:d:{yyyymmdd}` 等 | INCR | token 累计 | 48h / 62d |


密钥哈希：`sha256` + 服务端 pepper（pepper 在 `.env` / Secret，不在 yaml）。查找：Bearer 明文算 hash，按 `key_id` 前缀（如 `hk_`）定位条目，再 `hmac.compare_digest` 比对。

**兼容今日单 key**：显式 `cluster.auth.mode: legacy | managed`。legacy 使用现有 profile-scoped `API_SERVER_KEY`；managed 只认 Redis key/grant，空表一律拒绝。切换由管理员操作，删除最后一把 key、Redis 恢复或缓存失效不能自动恢复 legacy 认证。

外部入口只做一次限流准入；反代使用认证的内部入口，剥离客户端伪造的内部头，绑定 request_id、principal、目标 profile/conversation。owner 校验授权上下文，不重复扣额度。真正执行节点按唯一 execution_id 读取本次用量增量，以 Lua 原子写去重标记并增加计数；不拿会话累计值直接 INCR。取消、SSE 断开、错误与重试仍按实际执行结算。去重保留期必须覆盖重试和故障恢复窗口。

配额语义：本阶段只交付 **soft** 软额度：请求前原子预检，执行后按实际增量结算，允许已准入的并发请求超发；不承诺超发仅持续秒级。每日/月度按 UTC 窗口，执行开始时确定归属窗口；输入/输出/缓存 token 口径与现有记账明确对齐。跨 profile 的用户额度用 namespace 级 key，agent 额度用 scope 级 key。

硬配额不以“INCR 后拒绝后续请求”冒充 strict。若以后交付 hard 模式，必须原子预留 user+agent 预算、限制最大并发及单 turn 最大开销，再做实际结算/退款、崩溃预留回收；无法界定工具循环最大消耗时拒绝 hard 配置。

Redis 宕机：默认 **fail_closed**（503），可配 `fail_open`（仅内网）。对公网禁止 fail_open。

---

## 4. 鉴权规范

**默认（必须能被 OpenAI 官方客户端使用）：**

```
Authorization: Bearer <api_key>
```

**HMAC 延后：** 未来若需要请求签名，须另定可验证凭证（不能假定仅凭 Bearer 哈希恢复签名 secret）、重放窗口与内部代理签名语义。本阶段没有 `require_signature` 配置。

AuthZ：URL profile 经部署映射解析为稳定 `profile_id = agent_id`；grant/scopes 使用该身份，不直接把显示名当 agent_id。无授权 → 403。

吊销：Redis 原子变更凭证状态/版本。新请求在线检查权威状态，不以进程缓存/pubsub 作为唯一依据，承诺吊销完成后新准入返回 401；已准入执行不自动撤销。若未来允许离线短缓存，必须明确最长吊销延迟，不能同时宣称“立即全网生效”。

### Pocket 接入兼容边界（2026-10-12）

Pocket 的单机设计同时覆盖不同 profile 的客户端隔离，以及同 profile 的多设备共享，详见 [Pocket 多客户端与集群兼容](../pocket/design.md#多客户端与集群兼容2026-10-12-补充待实现)。后续集群接入须沿用 namespace 下的稳定 profile 身份及权威授权原则，不使用节点路径、显示名或服务级 token 作为角色授权。

每个设备凭证只授权一个 profile，同角色可有多把独立可撤销凭证；连接码单次兑换、主人绑定及角色状态需要共享事务权威源。Pocket 的 WebSocket、音频、元数据和历史入口均须纳入授权及 owner 路由，不能因本阶段 OpenAI API 已支持 Bearer 就宣称 Pocket 同样安全。长连接的撤销后订阅终止、确认请求首答原子提交、同会话单活动回合及迁移重连须另行集成验收；遵守本节“新准入吊销、已准入执行不自动撤销”的区别，角色销毁的执行停止属于显式生命周期操作。

本补充是兼容约束，不扩展当前 Phase 2 的交付声明，也不提前确定 Pocket 凭证格式、Redis key 布局或把本地单机数据库提升为集群权威库。

---

## 5. 用量

- **不要**新建 `usage_records` SQLite。
- Token 数从现有执行记账读取本次增量，按 execution_id 原子去重结算；崩溃未结算需恢复流程或标记 unknown，不从累计 session 值猜测。
- `session_model_usage` 是独立用量数据，不能假定随 portability 的 session 行自动完整导出。本阶段不交付账单/无损计费管道；若需跨节点聚合，应先显式定义导出 payload 与唯一执行增量契约，不能仅扫会话快照宣称费用完整。
- Metrics 带 `user` 标签：遵守仓库 opt-in；默认指标只用 route/status/node（详见 Phase 4）。

---

## 6. 文件意向

新增（均非 facade）：

| 意向路径 | 用途 |
|---|---|
| `gateway/coordination/rate_limiter.py` | local 进程内桶 / Redis Lua |
| `gateway/coordination/quota.py` | 预检与 INCRBY |
| `gateway/platforms/api_server_cluster_auth.py` | Bearer、显式模式、grant、内部代理上下文 |
| `hermes_cli/keys_admin.py` | `hermes keys create/revoke/list`、`hermes quota set`（写 Redis） |
| 契约测试 | Bearer 兼容、显式模式、429、吊销、反代去重结算 |

修改：api_server **sibling** 挂中间件链；`gateway/config*.py` + CLI config 的 `cluster.auth.*` / `ratelimit.*` / `quota.*`。

**不修改** `gateway/authz_mixin.py`。

---

## 7. 配置

```yaml
cluster:
  auth:
    mode: legacy                # legacy | managed；显式切换，不以 key 表为空回退
  ratelimit:
    default_user_qps: 10
    default_agent_qps: 20
    fail_closed: true
  quota:
    mode: "soft"                # 本阶段不提供 hard/strict
    default_daily_tokens: 0      # 0 = 不限
```

Pepper、Redis URL、密钥明文不出现在 yaml。

---

## 8. 实施步骤与验收

| 步 | 内容 | 验证 |
|---|---|---|
| S1 | Redis 密钥/grant 结构 + 管理 CLI | create/revoke/list |
| S2 | Bearer + 显式认证模式 | legacy 兼容；managed 空表拒绝；撤销最后 key 不回退 |
| S3 | AuthZ grant | 属主 200 / 未授权 403 / 吊销立即 401 |
| S4 | 令牌桶 | 突发超 QPS → 429 |
| S5 | soft 预检与去重结算 | execution 增量只计一次；本地与反代计数一致；取消/错误仍结算 |
| S6 | `cluster.enabled=false` 回归 | 现有测试全绿 |
| S7 | Redis 宕机 fail_closed | 503；内网可测 fail_open |

验收：双节点计数一致；官方客户端仅带 Bearer；跨 profile 用户配额共享；消息平台行为不变。计量持久性受 Redis RPO 约束。

---

## 9. 风险

| 风险 | 缓解 |
|---|---|
| 软配额超发 | 并发与单 turn 上限；不冒充 strict |
| Redis 宕机 | 对公网 fail_closed；健康检查 |
| 认证模式误回退 | 显式 mode；空表不降级 |
| 进程缓存导致吊销延迟 | 在线权威状态校验；pub/sub 只作加速，不作为正确性保证 |
