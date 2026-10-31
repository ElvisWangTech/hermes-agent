# Hermes 分布式改造 · Phase 0：身份与状态归属

> 版本：v3.0 ｜ 状态：待确认 ｜ 前置：无
>
> 范围：sandbox 拓扑；入口为 OpenAI 兼容 API 与 `apps/pocket`。

---

## 1. 身份

| 身份 | 含义 | 来源 |
|---|---|---|
| `namespace` | 部署命名空间，部署内固定 | `cluster.namespace` |
| `profile_id` | 租户键。Pocket 中即一个角色 | 控制面分配，稳定不变 |
| `instance_id` | 一台 VM 的身份，`profile_id` 与 `incarnation` 组合 | 控制面生成 |
| `conversation_id` | profile 内稳定的会话身份 | 由 `session_key` 映射 |
| `session_key` | 客户端会话键 | 请求头或 Pocket 本地会话 |
| `device_id` | Pocket 设备凭证 | 连接码兑换时生成 |
| `api_key_id` | OpenAI API 密钥 | 管理员创建 |

规则：

1. `hermes_home_key()`（本地路径）只用于进程内作用域，不作为集群身份。
2. 客户端只能提供不透明凭证与会话 ID。profile 一律由网关从凭证解析。客户端伪造 profile 或会话 ID 必须拒绝。
3. `instance_id` 的 `incarnation` 每次创建新生成。旧实例的任何写入、提交都按 `instance_id` 校验，不能靠名字判断。

## 2. 状态归属

| 数据 | 存放位置 | 进快照 | 删除角色时 |
|---|---|---|---|
| 会话与 compression lineage（`state.db`） | 节点目录，VM 挂载到 `/root/.hermes` | 是 | 删除 |
| 记忆、私有 Skill、`config.yaml`（去掉密钥） | 同上 | 是 | 删除 |
| 日程定义与未听结果 | 同上（VM 内 cron 存储） | 是 | 删除 |
| 工作区文件 | 节点目录，VM 挂载到 `/workspace` | 是（有大小上限） | 删除 |
| 角色名字、头像 ID、主人绑定 | 控制面权威存储 | 否 | 删除，保留审计记录 |
| 连接码、设备凭证、API 密钥（仅哈希） | 控制面权威存储 | 否 | 撤销 |
| 实例租约、快照清单 | 控制面权威存储 | 否 | 保留到快照过期 |
| cron 下次运行索引 | 控制面权威存储 | 否 | 删除 |
| 模型密钥与其他租户凭证 | 启动时注入 VM 环境变量 | 否，不落盘 | 随实例销毁 |
| 用量计数 | 控制面 | 否 | 按保留期 |
| 路由、活动回合、运行中任务 | 进程内 | 否 | — |

为什么角色名字、主人绑定放在控制面而不是 VM 内：它们是授权关系。VM 重建或快照恢复时必须保留，且不能被 VM 内代码修改。

## 3. "不丢会话"的验收定义

最后一次成功快照中的会话、记忆、私有 Skill、配置、工作区、日程，在新实例中可恢复。快照之后的修改允许丢失，并在日志中记录丢失范围。

## 4. 配置

```yaml
cluster:
  enabled: false                 # false = 单体，现有行为不变
  namespace: ""
  object_store:
    backend: none                # s3 | none
    endpoint: ""
    bucket: ""
    region: us-east-1
    addressing_style: path
  lease:
    ttl_sec: 90
  snapshot:
    interval_sec: 600            # 周期快照：仅在有写入且距上次快照超过该时长时
    max_size_mb: 2048            # 超限拒绝快照，待实测后定
  drain_timeout_sec: 120
```

- 行为配置写入 `config.yaml`。密钥只有 `OBJECT_STORE_ACCESS_KEY` 与 `OBJECT_STORE_SECRET_KEY`，写入 `.env` 或 Secret，不写入 `config.yaml`、快照或挂载目录。
- 控制面的权威数据与 Pocket 授权共用 `pocket-auth.sqlite`（见 `hermes_cli/dashboard_auth/pocket_store.py::AUTHORITY_FILENAME`）。
- 启用集群但缺对象存储配置、密钥或 boto3 时启动失败，不允许半开。

可选依赖组（单节点首版不需要 Redis）：

```text
boto3>=1.34,<2
zstandard>=0.22,<0.24
```

## 5. 对象布局

```text
控制面权威存储（单节点：SQLite；多副本：托管 Redis 或数据库）
  profile:{profile_id}                     角色元数据、主人绑定
  apikey:{api_key_id}                      哈希、profile、状态
  device:{device_id}                       哈希、profile、状态
  instance:{profile_id}                    当前实例、incarnation、租约
  manifest:{profile_id}                    当前快照版本指针

对象存储（不可变）
  s3://{bucket}/hermes/{namespace}/{profile_id}/snapshots/{revision:012d}.{sha256}.tar.zst
```

## 6. 本阶段不做

- 不改 SQLite 为 PostgreSQL。
- 不把消息平台的允许名单改为多租户表。
- 不改 OpenAI 兼容入口的 Bearer 鉴权方式。
