# Hermes 分布式改造 · 设计索引

> 版本：v3.0 ｜ 状态：待确认 ｜ 本轮只修订设计文档，不落代码。
>
> 相对 v2.0：去掉 shared 拓扑，只做 sandbox；范围收敛为 OpenAI 兼容 API 与 `apps/pocket`；快照改为暂停后拷贝，去掉 SQLite 导出/替换导入；Cube 适配器作为独立包。

---

## 范围

| 项 | 结论 |
|---|---|
| 入口 | OpenAI 兼容 API（Bearer）、`apps/pocket` 网页端 |
| 不在范围 | 消息平台（Telegram 等）、CLI、TUI、桌面端、仪表盘 `web/`、shared 多副本 |
| 威胁模型 | 存在不可信外部租户 |
| 隔离单元 | 一个 profile = 一台 Cube microVM。Pocket 中一个角色即一个 profile |
| 执行位置 | Agent 与所有工具都在 VM 内，网关不运行租户代码 |

## 架构

```text
浏览器 Pocket / API 客户端
   │ 设备凭证 / API key
   ▼
控制面（网关，无状态，可多副本）
   鉴权 · 授权 · 限流 · 软配额 · profile → 实例路由 · HTTP/SSE/WebSocket 代理
   │
   ├─ 权威存储：凭证、连接码、角色与主人绑定、实例租约、快照清单、cron 索引
   ├─ Sandbox Manager：创建 / 暂停 / 恢复 / 销毁，挂载、网络、密钥
   └─ 节点代理：运行在 Cube 节点上，负责快照拷贝与恢复
          ▼
   Cube microVM（每 profile 一台，内部是单 profile 的 Hermes）
     /root/.hermes         ← 节点目录（配置、会话、记忆、私有 Skill、日程）
     /workspace            ← 节点目录（工作区）
     /opt/common-skills    ← 公共 Skill，只读

对象存储（S3 兼容）：不可变快照
```

## 已确认的决策

| 决策 | 结论 |
|---|---|
| 威胁模型 | 存在不可信外部租户 |
| 拓扑 | 只做 sandbox |
| DR 快照范围 | 包含工作区 |
| cron | 到点唤醒 VM（控制面索引 + 唤醒），不让 VM 永不 pause |
| Cube 适配器 | 独立包。核心只定义 provider 接口，不含 Cube 代码 |
| 范围 | 只做 API 与 `apps/pocket` |

## 本轮采用的默认值（待确认）

1. **首个部署为单节点。** 控制面权威存储用 SQLite，与 `docs/pocket/design.md` 第一阶段一致。出现多控制面副本时再换托管 Redis 或数据库。
2. **快照在 VM 暂停时拷贝节点目录。** 避免 SQLite/WAL 在运行中拷贝不一致。代价是暂停时间，需实测。RPO = 最近一次成功快照。
3. **工作区纳入快照，但设大小上限。** 上限值待定。
4. **快照保留期、角色销毁后的备份删除时限，待定。**

## 阶段文档

| 阶段 | 文档 | 内容 |
|---|---|---|
| Phase 0 | [phase0-design.md](phase0-design.md) | 身份、状态归属、密钥、配置 |
| Phase 1 | [phase1-design.md](phase1-design.md) | 暂停拷贝快照、清单提交、实例租约与 fencing、恢复 |
| Phase 2 | [phase2-design.md](phase2-design.md) | API 密钥、连接码与设备凭证、授权、限流、软配额 |
| Phase 3 | [phase3-design.md](phase3-design.md) | 沙箱提供者接口、Cube 独立包、生命周期、路由、网络、唤醒 |
| Phase 4 | [phase4-design.md](phase4-design.md) | 日程到点唤醒、未听结果、观测 |
| Phase 5 | [phase5-design.md](phase5-design.md) | 部署、模板升级、保留与删除、回滚 |

## 依赖

```text
Phase 0（身份与归属）
  └─► Phase 1（快照与 fencing）
        ├─► Phase 2（凭证、授权、配额）
        └─► Phase 3（实例生命周期与路由）
              ├─► Phase 4（日程唤醒）
              └─► Phase 5（部署与升级）
```

## Pocket 对应关系

| Pocket 需求（`docs/pocket/design.md`） | 落在哪里 |
|---|---|
| 一个连接码对应一个独立角色（profile） | Phase 0 身份；Phase 2 连接码 |
| 同一角色多设备，设备凭证可单独撤销 | Phase 2 |
| 主人绑定、角色名字、头像、连接码状态 | Phase 0 权威存储（不放 VM 内，随实例重建保留） |
| `/api/ws` JSON-RPC、音频、历史查询 | Phase 3 路由代理（需支持 WebSocket 与 SSE） |
| 冷启动唤醒、"正在唤醒"状态 | Phase 3 |
| 日程、到点说出、未听结果、浏览器通知 | Phase 4 |
| 管理员签发连接码、撤销设备、停用授权、人工销毁 | Phase 2（管理员凭证）；`apps/pocket-admin` 调用控制面 |
| 销毁角色时删除的数据范围 | Phase 0 删除范围；Phase 5 保留期 |
| 第二阶段声纹与 Cubism 动态模型 | 不在本目录范围 |

`apps/pocket` 客户端基本不需要改协议：它只看到同一套 `/api/ws` 与 `/api/pocket/*`，profile 由网关从凭证解析。需要改的是连接时的唤醒状态显示。

## 明确不做

- shared 多副本拓扑（亲和路由、内部反代到节点）。
- 消息平台 webhook 入站、出站回调队列。API 与网页入口都是同步的。
- 外置 cron job store、按 home 选主。
- Redis Cluster 槽位、完整键前缀表。
- HPA、canary 细则、告警样例。
- 多地域、Operator。

## 待实测假设

| 假设 | 影响 | 验证方式 |
|---|---|---|
| 暂停后拷贝目录是一致的（SQLite WAL 已 checkpoint） | 快照正确性 | 暂停/拷贝/恢复循环，校验 sqlite 完整性 |
| pause/resume 可靠（Cube #953 已修复） | 整体前提 | 反复 pause/resume，记录失败率 |
| 暂停与恢复耗时可接受 | 快照频率、唤醒体验 | 按工作区大小实测 P95 |
| 同一宿主目录不会被两台运行中 VM 挂载 | 单 writer | 尝试双挂载，确认被阻止 |
| 出站策略能精确屏蔽内网与元数据地址 | 租户隔离 | 从 VM 探测 |
| Cube 能转发 WebSocket 与 SSE | 网页端与 API 流式输出 | 端到端测试 |
