# Hermes 分布式改造 · Phase 0：身份与状态清单

> 版本：v1.1 ｜ 状态：设计修正稿 ｜ 前置：无
>
> 本文档是 Phase 1–5 的地基。不写代码、不引入组件。未确认本页之前，不得按旧稿实施任何阶段。

---

## 1. 要解决的两件事（必须分开）

| # | 目标 | 与现状的关系 | 何时做 |
|---|---|---|---|
| A | Gateway **水平多副本**（同一会话亲和到一节点，故障后从快照恢复） | 与现有 Gateway / api_server / SQLite 同构 | Phase 1 起 |
| B | **多租户公网平台**（密钥、配额、账单） | 新产品面，不是把 profile 换个名字 | Phase 2 起，且不阻塞 A |

旧稿把 B 的 `userId/agentId/cluster.db` 焊进 Phase 1，导致后续阶段都建在错误身份上。修正后：Phase 1 只做 A；B 的数据面不进节点 SQLite。

---

## 2. 身份模型

现码已经有完整身份，集群方案必须映射上去，**不另造平行租户模型**。

| 现码概念 | 集群中的角色 | Phase 1–2 映射 |
|---|---|---|
| **profile**（`HERMES_HOME` / `hermes_home_key()`） | 隔离单元：配置、密钥、`state.db`、cron、记忆 | **`agent_id = profile 名`**（`default` 或 `/p/<profile>/` 的名字） |
| **API principal** | 谁在调 api_server | Phase 1：`API_SERVER_KEY`（可按 profile 作用域）；Phase 2：Bearer key → `user_id` |
| **`session_key`**（`X-Hermes-Session-Key` / 内部 routing key） | **亲和路由键** | Redis `sess:route:` 用这个，不是 `session_id` |
| **`session_id`** | transcript 主键；compression 会旋转 | 快照对象键用 **lineage tip 的 `session_id`**，meta 里带 `lineage_session_ids` |
| 会话行上的 `user_id` | 审计/平台 peer，不是鉴权主体 | 不作为 AuthN |

约束：

1. 一个 gateway 进程可服务多个 profile（multiplex）。任何 Redis key、S3 前缀、锁、限流计数都必须带 `hermes_home_key()` 或 `profile`，禁止进程级单例后端读「启动 profile」的库。
2. `get_backend()` / 快照客户端按 **当前 profile scope** 解析，不在 import 时绑死 `HERMES_HOME`。
3. 在引入独立 `agents` 表（若需要，属 Phase 2 之后）之前，禁止出现与 profile 不对齐的 `agent_id`。

---

## 3. 状态清单（源 / 缓存 / 不同步）

**节点本地任何 SQLite、`jobs.json`、进程内 dict 都不是集群源。**

| 数据 | 今日位置 | 集群源 | 节点角色 | 首次外置阶段 |
|---|---|---|---|---|
| 会话 transcript + compression lineage | `state.db` | 对象存储快照 | 热缓存；claim 后按 generation **replace** | Phase 1 |
| 会话 routing / handoff / rewind / `last_activity_*` | `state.db` + 进程 | **不同步**（portability 导入本就会重置） | 丢了从 transcript 重建，不假装 live ownership 可迁移 | — |
| MemoryManager / Honcho 等 provider 实例 | 进程内（api_server checkout） | **不同步** | 故障切换后 freeze 丢失；远程 memory 后端除外 | 不在 1–5 |
| 本地记忆文件 | `{HERMES_HOME}/memories` | **不同步** | 各节点各自一份，或改用远程 provider | 不在 1–5 |
| `/model` override、prompt pin | session store + 进程 | **不同步**（Phase 1） | 切换节点后可能丢失 pin | 需要时再并进快照 |
| run 幂等、response store、artifacts | 进程 / profile 目录 | **不同步** | 客户端应视为最多一次成功 | 不在 1–5 |
| 工作区文件、终端 cwd、后台进程、浏览器 profile | 节点磁盘 / PTY | **不同步** | Agent 文件工具在多副本下**不保证**跟会话走 | 不在 1–5；Phase 3 若做则靠 environment backend 自带的盘 |
| `config.yaml`、skills | `{HERMES_HOME}` | **部署物**（镜像 / ConfigMap），不是运行时同步 | 所有节点必须等价部署 | Phase 5 挂载 |
| 密钥（`.env`、API keys、云凭证） | `{HERMES_HOME}/.env` + secret scope | **Secret 挂载 / 现有 secret scope** | 禁止写入 `config.yaml`；禁止新发明节点 SQLite 当密钥库 | Phase 2 起 Redis 只缓存哈希；Phase 5 K8s Secret |
| cron `jobs.json` + occurrences ledger | `{HERMES_HOME}/cron/` | Phase 4 之前：**单节点才能跑 cron**（文档化限制）；Phase 4：**外置 job store** | `.tick.lock` 不能当集群锁 | Phase 4 |
| 用量（token / 费用） | `sessions` / `session_model_usage` | 仍以现有表为准；跨节点聚合走快照或只读副本 | 不新建 `usage_records` 本地表 | Phase 2 只做 Redis 配额计数 |
| 鉴权允许名单（消息平台） | 环境变量 / YAML allowlist | **不动**；与 API 租户授权分流 | `authz_mixin.py` 保持个人 bot 语义 | — |

验收时「不丢会话」= **transcript + lineage 恢复到最后一次成功快照**。不是工作区、不是 memory、不是 cron、不是 live 路由状态。

---

## 4. 键与前缀（全程遵守）

设 `home = hermes_home_key()`（稳定字符串，不是瞬时 `HERMES_HOME` 环境变量）。

```
Redis:
  {home}:sess:route:{session_key}
  {home}:node:addr:{node_id}
  {home}:lock:{name}
  {home}:rl:user:{user_id}          # Phase 2
  {home}:quota:...                  # Phase 2

S3:
  s3://<bucket>/hermes/{home}/sessions/{session_id}/
    meta.json
    snapshot.{generation}.json.zst
```

`node_id` 默认 `POD_NAME` 或 `hostname`（**不要**拼 pid：重启应表现为新节点即可，pid 只增加噪音）。地址登记必须是集群内可达地址，并带节点间共享密钥（Phase 1 内部反代用）。

---

## 5. 配置与加载器

- 行为：`config.yaml` 的 `cluster.*`（及后续 `sandbox.*` 仅当 Phase 3 启用）。
- 密钥：`.env` / secret scope（`REDIS_URL`、对象存储 AccessKey、节点间 `CLUSTER_INTERNAL_TOKEN`）。
- Gateway 读 **raw YAML**（`gateway/config.py` / `gateway/config_loader.py`），不只改 `hermes_cli/config.py`。两边都要认识 `cluster.*`，缺一边就是静默默认值。
- `cluster.enabled=false`（默认）：所有集群代码短路为今日单体行为；CLI / TUI / Electron / 长轮询适配器不走 Redis/S3。

可选依赖组（均须 **带上界**，符合仓库 pinning 政策）：

```
cluster = [
  "redis>=5,<7",
  "boto3>=1.34,<2",
  "zstandard>=0.22,<0.24",
]
```

启用集群但未安装 extra → 启动失败并提示，禁止半开。

---

## 6. 阶段依赖（修正后）

```
Phase 0  本文（身份 + 状态清单）
   └──► Phase 1  亲和 + 快照协议 + drain + 日志四元组
          ├──► Phase 2  Bearer 鉴权 + Redis 限流/配额（不对公网直到 Redis 可用且有基础健康指标）
          ├──► Phase 3  默认跳过；若做 = 强制已有 terminal backend（docker 等），不上工具 RPC
          └──► Phase 4  外置 cron store 之后再上队列；观测接现有 OTLP
                 └──► Phase 5  K8s：托管 Redis + Kodo + 配置/密钥挂载；poller 按平台拆
```

组件约束仍是「只引入 Redis + 对象存储」，但含义补全为：

- Redis：协调与（Phase 2+）计数/队列，**不是**会话内容、**不是**密钥明文。
- 对象存储：会话快照（及 Phase 4 起可选的 cron 备份）。
- **不**把节点 `cluster.db` 当多副本权威库。
- 对象存储用托管 S3 兼容服务时，Redis 也优先托管，不把 HA 拖到最后再自建 Sentinel。

---

## 7. 明确不做（贯穿所有阶段）

- 不替换 SQLite → PostgreSQL；`hermes_state_*.py` 防腐工程保留。新增的是 **replace/upsert 导入** sibling，不是改 facade 神文件。
- 不把消息平台 allowlist 升级成「表驱动多租户」（个人 bot 继续今日路径）。
- 不破坏 OpenAI 兼容入口的默认鉴权方式（Bearer）。
- 不把 prompt 缓存「跨节点热迁移」写进验收；亲和命中则缓存仍热，迁移/故障则允许变冷。
- 不在无用户 opt-in / 配置门的情况下外发带 user 标签的用量归因。
- 不往 `gateway/run.py`、`api_server.py`、`scheduler.py` facade 无限制堆逻辑：新行为进 sibling / `gateway/coordination/`。
