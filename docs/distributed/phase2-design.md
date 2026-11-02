# Hermes 分布式改造 · Phase 2：凭证、授权与配额

> 版本：v3.0 ｜ 状态：待确认 ｜ 前置：[Phase 0](phase0-design.md)、[Phase 1](phase1-design.md)
>
> 目标：API 密钥、Pocket 连接码与设备凭证、profile 授权、限流、软配额。

---

## 1. 凭证

### 1.1 API 密钥（OpenAI 兼容）

- `Authorization: Bearer <api_key>`。
- 每把密钥绑定**一个** profile。
- 只存哈希（`sha256` + 服务端 pepper，pepper 在 `.env` 或 Secret）。明文只在创建时展示一次。
- 可吊销。吊销后新请求立即 401。已在执行的请求不自动撤销。

### 1.2 连接码与设备凭证（Pocket）

- 管理员为一个 profile 签发连接码。连接码单次有效，有过期时间，只存哈希。
- 客户端兑换连接码，得到设备凭证 `device_id` 与 token。兑换是原子操作，同一码只能成功一次。
- 一个设备凭证只绑定一个 profile。同一角色可签发多个连接码，供多台设备兑换。
- 设备可单独撤销；角色可整体停用（撤销全部设备与未用连接码）。停用不删除角色数据。
- 设备凭证不能访问管理接口。管理接口使用独立的管理员凭证。

### 1.3 主人绑定

- 主人绑定属于授权关系，存控制面权威存储（见 Phase 0），不放 VM 内。
- Pocket 的认主流程由客户端发起，控制面原子写入唯一主人绑定。重复提交不创建第二个主人。

## 2. 授权

每个请求：

1. 凭证 → `profile_id`（由控制面解析）。
2. 请求中若带 profile 或会话 ID，必须属于该 profile，否则拒绝。
3. 路由到该 profile 的实例（Phase 3）。

HTTP、SSE、WebSocket 使用同一规则。WebSocket 升级前完成鉴权，之后每条 JSON-RPC 消息仍校验会话归属。

网关剥离客户端伪造的内部头。

## 3. 限流与配额

- 限流：按凭证和 profile 各设令牌桶。
- 配额：**软配额**。请求前检查日/月 token 余额，执行后按实际用量结算。允许并发请求超发，超发时间有限。
- 用量来源：VM 在每次 turn 结束时回报本次增量，附带 `execution_id`。控制面按 `execution_id` 去重，同一次执行只计一次。取消、断开、错误仍按实际用量结算。
- 不使用会话累计值直接累加。
- 窗口按 UTC 日与月。

硬配额不在本阶段。

## 4. 故障

- 权威存储不可用：拒绝新请求（503），不做降级放行。
- 实例不可用：返回"正在唤醒"（Phase 3），不把请求转给其他 profile 的实例。

## 5. 管理

- 管理员后台是独立服务 `apps/pocket-admin`，有自己的登录（单独的管理员密码、短时会话）。它调用控制面的管理接口，使用管理员凭证。
- 管理员能力：创建连接码、撤销连接码、撤销设备、停用角色授权、人工销毁角色。
- 设备凭证和 API 密钥无法访问管理接口。
- 远程访问必须走 HTTPS。

## 6. 配置

```yaml
cluster:
  ratelimit:
    default_user_qps: 10
    default_profile_qps: 20
  quota:
    default_daily_tokens: 0      # 0 = 不限
```

密钥、pepper 和 token 明文不写入 YAML。

## 7. 验收

1. 官方 OpenAI SDK 仅带 Bearer 即可使用。
2. 吊销后新请求 401；已执行的请求不受影响。
3. 连接码并发兑换只成功一次。
4. 设备 A 的凭证访问 profile B 的任何接口，一律拒绝（包括伪造会话 ID）。
5. 单设备撤销不影响同角色其他设备；角色停用撤销全部设备。
6. 同一次执行的用量只计一次；取消与错误仍结算。
7. 设备凭证访问管理接口被拒绝。
8. 权威存储不可用时返回 503。

## 8. Pocket 接入边界

Pocket 的多设备、连接码、主人认证与单机阶段的设计见 [Pocket 多客户端与集群兼容](../pocket/design.md#多客户端与集群兼容2026-10-12-补充待实现)。

- 本阶段不确定 Pocket 凭证的具体格式，只要求它满足 1.2 的约束。
- 声纹验证不在本阶段。文字通道和设备凭证不能视为声纹验证通过。

## 11. 实现状态（2026-10-31）

`gateway/coordination/access.py`，表 `cp_api_keys`、`cp_usage` 位于 `pocket-auth.sqlite`。

- API 密钥：仅存 HMAC-SHA256 摘要（pepper 由调用方提供，应来自 secret scope）；吊销即时生效。
- 设备凭证：复用 `PocketAuthority.verify`，通过回调注入，不引入 dashboard 依赖。
- 授权：`Principal` 绑定单一 profile；`authorize` 拒绝跨 profile。
- 限流：凭证与 profile 两个令牌桶（进程内）。
- 软配额：`preflight` 读取当日（UTC）已记账总量；`record_usage` 以 `execution_id` 去重。

未实现：

- 限流与配额的计数不跨进程共享，多副本前需换成托管存储。
- 用量上报入口：VM 侧回报 `execution_id` 与 token 数的接口尚未定义，当前只能由调用方直接调用 `record_usage`。
- `ratelimit.*` 与 `quota.*` 配置键尚未进入 `DEFAULT_CONFIG`，当前由构造参数传入。
- 设计中的 `hk_` 密钥管理 CLI（`hermes keys create/revoke/list`）未实现。
