# Hermes 分布式改造 · Phase 5 详细设计

> 版本：v1.0（评审稿） ｜ 状态：待确认 ｜ 前置：Phase 1/2/4 已验收（Phase 3 可选）
>
> 目标一句话：**完整云原生高可用集群**——K8s 部署、Redis 高可用、基于指标的自动扩缩容、优雅退出与灰度发布，并解决长轮询适配器的多副本问题。

---

## 1. 目标与非目标

### 目标
1. 全组件容器化并落地 K8s：gateway（多副本）、Redis（Sentinel 高可用）、sandbox（若启用 Phase 3）。
2. HPA 自动扩缩容：CPU + 自定义指标（队列堆积）双维度。
3. 发布能力：就绪/存活探针、优雅退出（改造现有 drain 机制）、滚动升级与灰度。
4. Telegram 等长轮询适配器多副本化：单 leader 轮询 + 队列分发。
5. 有状态面收敛：节点本地 state.db 仅作热缓存，空目录可冷启动——这是 HPA 自由伸缩的前提。

### 非目标
- 不做多地域多活（单地域多可用区足够；对象存储已跨区域可恢复，DR 走预案）。
- 不自建 K8s _operator_：用原生 Deployment/StatefulSet/HPA/PDB 即可。
- 对象存储用七牛 Kodo 云服务，不在集群内部署 MinIO。

## 2. 集群拓扑

```
                 ┌──────── Ingress（无状态转发） ────────┐
                 │                                       │
        ┌────────▼─────────┐                    ┌────────▼────────┐
        │ gateway Deploy    │  HPA 2–10 副本     │ adapter-pollers │
        │ （api_server+worker）│                  │ Deploy 1 副本     │
        └────────┬─────────┘                    │ （长轮询→入队）    │
                 │                              └────────┬────────┘
        ┌────────▼─────────┐                    ┌────────▼────────┐
        │ Redis Sentinel    │  1 主 2 从 + 3 哨兵 │ tasks:webhook   │
        │ StatefulSet       │ ◄──────────────────┘                 │
        └──────────────────┘
        对象存储：七牛 Kodo（云服务）   观测：Prometheus + OTLP 后端
```

## 3. 组件设计

### 3.1 gateway Deployment

| 项 | 设计 |
|---|---|
| 副本 | 初始 2，HPA 控制 2–10 |
| 镜像 | 单镜像含 api_server + task_worker（Phase 4）；`cluster.enabled=true` |
| 探针 | readiness：`/healthz` 检查 Redis 连通 + 自身 claim 能力；liveness：进程存活即可（避免误杀导致会话迁移） |
| 资源 | request 500m/1Gi，limit 2/4Gi（按压测调整） |
| 存储 | **emptyDir**——state.db 仅是热缓存；冷启动从 Kodo 按需拉取（Phase 1 协议已保证） |
| node_id | `POD_NAME`（Downward API 注入），Pod 重建自动生成新身份，旧租约 90s 过期被接管 |

### 3.2 优雅退出（改造现有 drain 机制）

现状 drain 是写本地 `.drain_request.json` 文件。集群化后：

1. K8s 删除 Pod → `preStop` 钩子调用本节点 `/admin/drain`；
2. 节点停止接受**新会话** claim（claim 返回 `is_mine=False` 并主动从路由表摘牌），进行中的 turn 等待完成（上限 120s）；
3. 全部缓存会话执行快照回写（fencing 条件写）；
4. 退出。`terminationGracePeriodSeconds: 150` 兜底；超时强杀也无妨——租约过期 + fencing 协议兜底，最坏丢失未回写窗口。

**会话迁移成本实测目标**：滚动升级期间单次请求额外延迟 < 2s（新节点 claim + 拉快照 + 导入）。

### 3.3 Redis Sentinel（StatefulSet）

- 1 主 + 2 从 + 3 sentinel；客户端（`redis_backend.py`）改 Sentinel 感知连接（redis-py 原生支持）。
- 故障切换 RTO ≈ 10–30s；期间按 Phase 4 降级策略运行（队列生产者退回同步、限流 fail_closed）。
- 数据面风险重估：Redis 丢失未持久化的路由/锁信息**不丢会话数据**（快照在 Kodo），AOF everysec 足够。

### 3.4 长轮询适配器多副本化

问题：Telegram getUpdates 是单消费者语义，多副本会互相抢 offset。

方案（二选一，默认 A）：

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A. 单 leader 轮询 + 队列分发（默认）** | adapter-pollers 独立 Deployment（1 副本 + leader election 经 LockBackend），拿到消息 `XADD tasks:webhook`，由任意 gateway 消费 | 改动小、复用 Phase 4 队列；副本固定为 1+热备 |
| B. 改 webhook 接入 | Telegram 改为 setWebhook → Ingress → 任意 gateway | 最云原生，但依赖公网回调域名/证书，且部分平台（微信）不支持；逐平台迁移 |

Discord/Slack 等 WebSocket 网关型适配器同理走方案 A（单连接 + 分发）。

### 3.5 HPA

```yaml
metrics:
- type: Resource      # CPU 70%
- type: Pods
  pods:
    metric: hermes_queue_depth_ratio   # 当前深度 / 单 worker 吞吐基准
    target: { type: AverageValue, averageValue: "50" }
```

- 队列深度指标由 Phase 4 的 `/metrics` 提供，经 Prometheus Adapter 暴露。
- 扩容触发后新 Pod 冷启动 < 60s（镜像预热 + 无状态启动）；缩容走 §3.2 优雅退出。
- 注意：**缩容是会话迁移的主要来源**，配合 `hermes_session_migrations_total` 告警观测迁移风暴。

## 4. 发布策略

| 项 | 设计 |
|---|---|
| 滚动升级 | maxSurge 1 / maxUnavailable 0（配合优雅退出，会话零中断目标） |
| 灰度 | 单独 `gateway-canary` Deployment（1 副本，镜像新版本），Ingress 按 header/权重分流 5% 流量；观测 30min 指标后全量 |
| 回滚 | Deployment revision 回滚 + 镜像不可变 tag；fencing 保证新旧版本混跑期间会话安全 |
| PDB | minAvailable 1（防止维护操作同时杀全部副本） |

## 5. 配置与清单

### 新增文件（K8s 清单 + 代码）

| 文件 | 用途 |
|---|---|
| `docker/k8s/gateway-deploy.yaml` | Deployment + Service + PDB + HPA |
| `docker/k8s/redis-sentinel.yaml` | StatefulSet + sentinel 配置 |
| `docker/k8s/adapter-pollers.yaml` | 长轮询 poller Deployment |
| `docker/k8s/ingress.yaml` | Ingress + 灰度注解 |
| `docker/k8s/prometheus-adapter.yaml` | 自定义指标管线 |
| `gateway/leader_polling.py` | 适配器轮询循环的 leader 化改造（LockBackend 选主 + 入队） |
| `gateway/admin_api.py` | `/healthz` `/admin/drain` 端点 |
| `tests/e2e/test_k8s_drain.py` | kind 集群内 drain/接管演练 |

### 配置段

```yaml
cluster:
  k8s:
    node_id_from: "pod_name"
    drain_timeout_sec: 120
  redis:
    sentinel_hosts: "redis-0.redis:26379,..."
    master_name: "hermes"
  pollers:
    enabled: true               # gateway 内嵌长轮询关闭，由独立 poller 承担
```

## 6. 实施步骤与验收

| 步 | 内容 | 验证 |
|---|---|---|
| S1 | 镜像 + 清单 + kind 本地集群起全量 | 2 副本通过 Phase 1 全部验收用例 |
| S2 | drain 改造 + 滚动升级 | 升级期间 1000 请求零 5xx；会话迁移延迟达标 |
| S3 | Sentinel 切换演练 | kill 主 → 30s 内恢复；期间按降级策略运行不丢任务 |
| S4 | poller 拆分 | Telegram 消息经 poller→队列→任意 gateway 消费，单副本语义保持 |
| S5 | HPA 压测 | 负载拉升自动扩容、回落自动缩容；缩容无会话错误 |
| S6 | 灰度演练 | canary 分流/回滚全流程 |

**验收标准（项目终点）**：单 Pod 故障 60s 内无感接管；Redis 主切换 30s 内恢复且零数据丢失；滚动升级与缩容对在线会话零 5xx；队列积压驱动扩容实测有效。

## 7. 风险与边界

| 风险 | 缓解 |
|---|---|
| 缩容/升级引发会话迁移风暴 | 迁移指标告警；HPA 缩容 stabilizationWindow ≥ 300s；drain 串行化（PDB） |
| emptyDir 冷启动拉快照延迟 | 首请求同步拉取 + 异步预热；实测目标 < 2s |
| Sentinel 切换期写入失败 | Phase 4 降级策略已覆盖；AOF everysec 丢数据 ≤1s 且不涉及会话本体 |
| 长轮询 poller 单副本是可用性短板 | leader 选举 + 热备，故障 90s 内切换；平台侧消息有重投机制兜底 |
| 会话亲和 + HPA 的天然张力（扩容不能分摊存量会话） | 文档化：HPA 主要应对**增量**会话；存量均衡靠空闲逐出后自然再分布 |
