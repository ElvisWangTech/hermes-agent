# Hermes 分布式改造 · Phase 5 详细设计

> 版本：v1.2 ｜ 状态：待确认 ｜ 前置：[Phase 1](phase1-design.md) drain + 快照 replace；[Phase 4](phase4-design.md) S0（若该集群跑 cron）。[Phase 3](phase3-design.md) 默认跳过。
>
> 目标一句话：用 K8s 跑多副本 api_server；配置与密钥可挂载；托管 Redis + 对象存储；长轮询按 **平台** 拆 leader；HPA 只对增量会话有意义。
>
> 相对 v1.0 的撤回：emptyDir 即可冷启动全部状态；Phase 1 协议「已保证」空目录；自建 Redis Sentinel 当默认 HA；一个 `leader_polling.py` 覆盖所有平台；canary 5% 能验证已亲和的老会话；滚动升级「1000 请求零 5xx」建立在 90s 猝死窗口上；sandbox 默认进拓扑。

---

## 1. 目标与非目标

### 目标

1. Gateway Deployment 多副本；**配置 / skills 用 ConfigMap 或镜像，密钥用 Secret**，不是靠 Pod 磁盘当源。
2. Redis：**托管实例**（与 Kodo 同级「别自建数据面」）。清单里可附 Redis 样例仅供 kind 开发，生产默认不写自建 Sentinel。
3. 就绪/存活探针、drain（Phase 1 已有 `/admin/drain`）、滚动升级、**仅针对新会话** 的 canary。
4. 长轮询：独立 poller Deployment，**每个平台一种接入**（getUpdates / 长连接 / 改 webhook），不是单一抽象打天下。
5. HPA：CPU + 队列深度；文档化与亲和的张力。

### 非目标

- 不多地域多活。
- 不自建 Operator。
- 不在集群内默认部署 MinIO（生产用 Kodo 等）；kind 测试可用 MinIO。
- 不把 Phase 3 sandbox 画成必选。
- 不声称工作区、本地 memory、未外置的 cron 能随 emptyDir 恢复。

---

## 2. 集群拓扑

```
                    Ingress（无状态）
                         │
          ┌──────────────┼──────────────┐
          ▼                             ▼
   gateway Deploy (HPA)          adapter pollers
   api_server + （可选）worker     每平台独立：副本 1 + 选主
          │                             │
          └──────────┬──────────────────┘
                     ▼
              托管 Redis
              对象存储（Kodo）
              配置：ConfigMap / 镜像
              密钥：Secret → 文件或 env（走现有 secret scope，禁止写进 config.yaml）
```

`state.db` 可用 emptyDir：**仅** Phase 1 已快照的 transcript 能按需拉取。冷启动第一请求延迟含下载+replace+**prompt cache 变冷**，不要写死 < 2s 为验收线；以实测 P95 建基线。

---

## 3. 组件

### 3.1 gateway Deployment

| 项 | 设计 |
|---|---|
| 副本 | 初始 2，HPA 2–10 |
| 镜像 | 与今日 gateway 同一入口；`cluster.enabled=true` |
| 探针 | readiness：进程 + Redis + 对象存储头探 + 本节点能续自己的 node:addr；liveness：进程存活（避免误杀触发迁移风暴） |
| 节点身份 | POD_NAME + 每次启动新 incarnation |
| 存储 | emptyDir 仅 `state.db` 热缓存；**HERMES_HOME 的 config/skills 只读挂载**；`.env` 来自 Secret |
| Profile | 多 profile = 多套 home 挂载或多个 Deployment；禁止一个空目录冒充 multiplex 的全部 home |

### 3.2 Drain 与发布

Phase 1 已规定 drain。K8s：

1. `preStop` 调 `/admin/drain`（内部 token）。
2. `terminationGracePeriodSeconds` > `drain_timeout_sec` + 快照提交/网络重试余量。
3. 滚动：`maxUnavailable: 0`、`maxSurge: 1`；PDB 至少保证 1 个就绪副本（生产建议更高）。

**Canary：** Ingress 权重/header 只影响 **尚未 claim 的新 session_key**。已绑定的会话仍由 SessionRouter 送到 owner（可能是稳定版 Pod）。文档与发布检查单必须写清：灰度指标看新会话错误率，不要用「总流量 5%」解释亲和流量。混跑必须同时满足 fencing 与数据版本兼容：manifest 带 schema_version/min_reader_version，明确 N/N−1 可读写窗口；旧版不能读取的新格式不得在仍可能被旧版接管时发布。fencing 只防失权写入，不能替代版本协商。

**「零 5xx」**：只对 drain 成功的滚动承诺；猝死/Redis 抖动走 Phase 1/4 降级，不得写进同一验收句。

### 3.3 回滚与灾备

关闭 `cluster.enabled` 不是数据回滚。步骤：停外部写入 → drain 所有 owner/worker/poller → 确认 manifest 提交或列明未提交损失 → 将已提交 conversation 导入目标单体 home 并重建绑定 → 恢复 cron/权限配置 → 切入口。恢复过程验证会话数量/lineage/哈希和代表性续聊；未迁移工作区/memory 的限制继续有效。

Redis 回退到旧备份必须先隔离所有旧进程及连接，按 Phase 0 受控恢复；快照孤儿对象不可自动作为新头。记录 RPO/RTO 与恢复演练结果，不把“托管实例可连接”当数据恢复验收。

### 3.4 Redis

生产：云厂商托管 Redis，应用侧 `REDIS_URL`（sentinel/cluster 以厂商 SDK 为准，需要时再扩展 `redis_backend.py`）。

kind：可用单容器 Redis。不要把「1 主 2 从 3 哨兵 StatefulSet」当默认交付物。

### 3.5 长轮询 / 网关型平台（按平台，不要一个文件包打）

问题仍成立：Telegram `getUpdates` 单消费者。

| 平台类型 | Phase 5 做法 |
|---|---|
| Telegram 长轮询 | 独立 Deployment 副本=1，LockBackend **按平台+token** 选主；消息 `XADD` 入 Phase 4 队列（须 queue.enabled） |
| 已支持 HTTP webhook 的平台 | 优先 Ingress → 任意 gateway（比 poller 更贴云原生） |
| Discord/Slack 长连接 | 各自独立选主连接；不要假设与 Telegram 同一循环 |

热备：选主租约过期后另一 Pod 接。平台侧重投/offset 行为要在该平台 adapter 文档写清，90s 空窗可能丢或重复，靠 Phase 4 幂等。

未启用 Phase 4 队列时：poller **不要**多副本部署，保持今日「单 gateway 跑适配器」。

### 3.6 HPA

CPU + 可选队列深度（Prometheus Adapter 仅样例清单）。扩的是 **增量** 请求与新会话；存量会话仍钉在 owner，直到 idle 逐出或 drain。缩容 = 迁移源：`stabilizationWindow` 宜 ≥ 300s，并盯 `session_migrations`。

---

## 4. 配置与清单意向

```yaml
cluster:
  k8s:
    node_id_from: pod_name
    drain_timeout_sec: 120   # 与 Phase 1 同一语义
  pollers:
    telegram: { enabled: false }   # 按平台开关，无全局 pollers.enabled 一把梭
```

清单目录（样例，非核心运行时）：

- `docker/k8s/gateway-deploy.yaml`（Deployment/Service/PDB/HPA）
- `docker/k8s/ingress.yaml`（含 canary 注解说明：只影响新会话）
- `docker/k8s/configmap-secret.example.yaml`
- 可选 `docker/k8s/poller-telegram.yaml`
- kind 用 Redis/MinIO overlay

代码意向：`gateway/admin_api.py` 若 Phase 1 已有 drain/healthz 则本阶段只接线；**按平台** 的 poller 入口放在对应 `gateway/platforms/` sibling，不造万能 `leader_polling.py`。

测试：kind drain 演练可有；不要把「必须连得上公有云 K8s」当单测。

---

## 5. 实施步骤与验收

| 步 | 内容 | 验证 |
|---|---|---|
| S1 | 镜像 + kind：ConfigMap/Secret + 2 replica + 托管或容器 Redis + MinIO | Phase 1 双节点用例仍过 |
| S2 | preStop drain + 滚动 | drain 路径下 transcript 连续；允许 cache 变冷 |
| S3 | 托管 Redis 故障（或模拟） | 按 Phase 2/4 fail_closed / 队列 503，不谎称成功 |
| S4 | （可选）Telegram poller + 队列 | 单消费者；重复消息幂等 |
| S5 | HPA | 扩容接新会话；缩容有 drain |
| S6 | Canary | 新 session_key 按权重进 canary；老会话仍在 owner |

终点验收（诚实版）：

- 单 Pod **drain 后删除**：另一副本接管，transcript 至最后快照。
- 单 Pod **kill -9**：租约到期后接管，另计恢复耗时；可能丢多轮未提交增量。
- 滚动升级走 drain，不承诺猝死级零 5xx。
- emptyDir 重启：仅恢复已快照会话；cron 须 Phase 4 S0；工作区/memory 不在范围。

---

## 6. 风险

| 风险 | 缓解 |
|---|---|
| 缩容迁移风暴 | HPA 窗口、PDB、迁移指标 |
| 把 emptyDir 当成完整 HERMES_HOME | 清单强制拆开 config/secret 挂载；文档禁止「空目录冷启动一切」 |
| Canary 被亲和「吸回」旧版 | 发布说明只统计新会话 |
| 长轮询 poller 单点 | 选主热备；能 webhook 的平台优先 webhook |
| 自建 Sentinel 运维 | 生产不默认交付 |
