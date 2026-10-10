# Hermes 分布式改造 · Phase 0：身份与状态清单

> 版本：v1.2 ｜ 状态：待确认 ｜ 前置：无
>
> 本轮修订：分离本地 scope 与集群身份；明确 conversation manifest、权威元数据及恢复边界。
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
| **profile**（`HERMES_HOME` / `hermes_home_key()`） | 本地隔离单元：配置、密钥、`state.db`、cron、记忆 | `agent_id = cluster.profile_id`；与本地 profile 一一映射，不另造 agents 表 |
| **API principal** | 谁在调 api_server | Phase 1：profile-scoped API_SERVER_KEY；Phase 2：Bearer → user_id |
| **`session_key`**（`X-Hermes-Session-Key` / 内部 routing key） | **亲和路由键** | Redis `sess:route:` 用这个，不是 `session_id` |
| **`session_id`** | transcript 主键；compression 会旋转 | 快照对象键用 **lineage tip 的 `session_id`**，meta 里带 `lineage_session_ids` |
| 会话行上的 `user_id` | 审计/平台 peer，不是鉴权主体 | 不作为 AuthN |

约束：

1. 一个进程可服务多个 profile。`hermes_home_key()` 是解析后的本地绝对路径，只用于进程内 scope/cache；**不是跨节点身份**。每个 home 配置 `cluster.namespace` 与 `cluster.profile_id`，同一逻辑 profile 在所有节点取值相同，不同部署 namespace 必须不同。
2. `get_backend()` / 快照客户端按 **当前 profile scope** 解析，再映射到集群身份；不在 import 时绑死启动 home。Redis/S3/锁按该集群身份分区。
3. `agent_id` 与 profile 一一对应。URL profile 名先通过部署映射解析到 profile_id，不直接以路径、显示名或客户端任意字段决定存储命名空间。
4. `conversation_id` 是 profile 内稳定身份；显式 `session_key` 规范化后映射到它。conversation manifest 持久保存当前 tip、lineage、reset revision 与已提交快照指针；不能靠节点本地 routing 或扫描 S3 猜测。
5. 节点标识由 `node_id` + 每次启动新生成的 `incarnation` 构成。续约、释放、地址登记都比较完整 owner 身份；原名重启不继承旧租约。

---

## 3. 状态清单（源 / 缓存 / 不同步）

**节点本地任何 SQLite、`jobs.json`、进程内 dict 都不是集群源。**

| 数据 | 今日位置 | 集群源 | 节点角色 | 首次外置阶段 |
|---|---|---|---|---|
| 会话 transcript + compression lineage | `state.db` | S3 不可变快照 + Redis 已提交 manifest | 热缓存；claim 后按 committed revision 事务 replace | Phase 1 |
| conversation → 当前 tip、reset revision、快照指针 | 当前依赖本地会话绑定 | Redis manifest（无 TTL） | 导入后重建 API 绑定，旧 live owner 不迁移 | Phase 1 |
| 会话 routing / handoff / rewind / `last_activity_*` | `state.db` + 进程 | **不同步**（portability 导入本就会重置） | 丢了从 transcript 重建，不假装 live ownership 可迁移 | — |
| MemoryManager / Honcho 等 provider 实例 | 进程内（api_server checkout） | **不同步** | 故障切换后 freeze 丢失；远程 memory 后端除外 | 不在 1–5 |
| 本地记忆文件 | `{HERMES_HOME}/memories` | **不同步** | 各节点各自一份，或改用远程 provider | 不在 1–5 |
| `/model` override、prompt pin | session store + 进程 | **不同步**（Phase 1） | 切换节点后可能丢失 pin | 需要时再并进快照 |
| run 幂等、response store、artifacts | 进程 / profile 目录 | **不同步** | Phase 1 不承诺这些端点跨节点可用；需另行 owner 索引才可启用 | 不在 1–5 |
| 工作区文件、终端 cwd、后台进程、浏览器 profile | 节点磁盘 / PTY | **不同步** | Agent 文件工具在多副本下**不保证**跟会话走 | 不在 1–5；Phase 3 若做则靠 environment backend 自带的盘 |
| `config.yaml`、skills | `{HERMES_HOME}` | **部署物**（镜像 / ConfigMap），不是运行时同步 | 所有节点必须等价部署 | Phase 5 挂载 |
| 密钥（`.env`、API keys、云凭证） | `{HERMES_HOME}/.env` + secret scope | **Secret 挂载 / 现有 secret scope** | 禁止写入 `config.yaml`；禁止新发明节点 SQLite 当密钥库 | Phase 2 Redis 管理凭证哈希/授权；Phase 5 K8s Secret |
| cron `jobs.json` + occurrences ledger | `{HERMES_HOME}/cron/` | Phase 4 之前：**单节点才能跑 cron**（文档化限制）；Phase 4：**外置 job store** | `.tick.lock` 不能当集群锁 | Phase 4 |
| 用量（token / 费用） | `sessions` / `session_model_usage` | 现有记账为执行用量来源；Phase 2 Redis 去重结算配额 | 不假定 portability 已导出独立 usage 表；账单/无损聚合另定契约 | Phase 2 |
| 鉴权允许名单（消息平台） | 环境变量 / YAML allowlist | **不动**；与 API 租户授权分流 | `authz_mixin.py` 保持个人 bot 语义 | — |

验收时「不丢会话」= **transcript + lineage 恢复到最后一次成功快照**。不是工作区、不是 memory、不是 cron、不是 live 路由状态。

---

## 4. 键与前缀（全程遵守）

设 `scope = encode(cluster.namespace, cluster.profile_id)`，编码必须无歧义，不包含本地路径。下文 `{scope}` 表示隔离前缀；实现 Redis Cluster 时还须按原子操作范围设计真实 hash tag，不能假定所有带同前缀的 key 自动同 slot。

```text
Redis:
  {scope}:conversation:{conversation_id}:epoch     # 不随租约过期删除
  {scope}:conversation:{conversation_id}:lease     # owner + epoch，带 TTL
  {scope}:conversation:{conversation_id}:manifest  # 已提交 tip/revision/blob 指针，无 TTL
  {scope}:node:addr:{node_id}:{incarnation}
  {scope}:cron:...
  {scope}:quota:...
  <namespace>:principal:...                       # 跨 profile 主体与全局配额

S3:
  s3://<bucket>/hermes/<scope>/conversations/<conversation_id>/
    snapshot.<owner_epoch>.<snapshot_revision>.<sha256>.json.zst
```

快照内容在 S3；**唯一在线提交头在 Redis manifest**。不再双写 S3 `meta.json` 当第二权威头。S3 孤儿对象可在保留窗口后回收；灾备导出 manifest，但恢复时必须停写和隔离旧 owner，不能扫描最高对象版本自动认定提交。

`node_id` 默认 `POD_NAME` 或 `hostname`；`incarnation` 为随机启动标识而非 pid。地址登记必须是集群内可达地址；内部反代认证走现有 secret scope。

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
          ├──► Phase 2  Bearer + Redis 限流/软配额（公网前完成持久性、健康与隔离验收）
          ├──► Phase 3  默认跳过；若做 = 强制已有 terminal backend（docker 等），不上工具 RPC
          └──► Phase 4  外置 cron store 之后再上队列；观测接现有 OTLP
                 └──► Phase 5  K8s：托管 Redis + Kodo + 配置/密钥挂载；poller 按平台拆
```

组件约束仍是「只引入 Redis + 对象存储」，但含义补全为：

- Redis：协调、epoch/manifest，以及 Phase 2+ 权限/计数、Phase 4 job store/ledger/队列；**已是权威元数据面**，不是可随意清空的缓存。不存 transcript 或密钥明文。
- 对象存储：不可变会话快照及可选灾备导出。
- **不**把节点 `cluster.db` 当多副本权威库。
- 生产 Redis 必须明确持久化、`noeviction`、备份、容量隔离、恢复流程与 RPO/RTO。队列膨胀不能驱逐 epoch、manifest、权限或 ledger。
- **协议前提：已确认的 epoch/manifest 不回退，所有 owner 使用同一有效协调主。** 托管 HA/异步复制本身不保证该前提；生产前验证厂商保证。无法保证时，禁止宣称严格 fencing：须停写、隔离旧 owner 后受控恢复，或另行评估更强协调存储。
- Redis/对象存储灾备恢复必须一致，恢复旧备份前 fencing 所有旧进程；仅切新 Redis URL 不足以隔离持有旧连接的 owner。

---

## 7. 明确不做（贯穿所有阶段）

- 不替换 SQLite → PostgreSQL；`hermes_state_*.py` 防腐工程保留。新增的是 **replace/upsert 导入** sibling，不是改 facade 神文件。
- 不把消息平台 allowlist 升级成「表驱动多租户」（个人 bot 继续今日路径）。
- 不破坏 OpenAI 兼容入口的默认鉴权方式（Bearer）。
- 不把 prompt 缓存「跨节点热迁移」写进验收；亲和命中则缓存仍热，迁移/故障则允许变冷。
- 不在无用户 opt-in / 配置门的情况下外发带 user 标签的用量归因。
- 不往 `gateway/run.py`、`api_server.py`、`scheduler.py` facade 无限制堆逻辑：新行为进 sibling / `gateway/coordination/`。
