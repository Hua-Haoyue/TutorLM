# 容器外调度控制面（control-plane）

仓库级开发边界与启动方式见 `../../README.md` 和 `../../docs/architecture.md`。

本目录实现 TutorLM 多实例架构的**容器外**部分：用户身份、持久项目元数据、邀请码准入与
项目分享、固定槽位分配、实例容器生命周期与回收。它是一个独立的 FastAPI 服务，不含任何
文档解析、Prompt 或插件逻辑——那些属于仓库根目录的 `instance-app/`。

与 `instance-app/` 的唯一耦合是 HTTP 契约
（见 `../../contracts/instance-runtime-v1.md`、`instance-runtime-v3.md`）。两侧不共享数据库、
不互相 import 私有模块。

```text
control_plane/
├── config.py        全部 CONTROL_PLANE_* 环境变量与 TOML 配置文件解析与校验
├── config_file.py   TOML/JSON 文件读取与结构解析（只负责把路径变成映射）
├── errors.py        稳定错误码 + HTTP 状态映射
├── security.py      访客令牌生成/哈希/校验
├── invitations.py   系统码/分享码生成、校验与准入判定（道具层，不做调度）
├── sharing.py       分享码导入：停源计算 -> 卷复制 -> 发布独立副本
├── store.py         SQLite：槽位、租约、用户、项目、邀请码、复制操作与事件
├── ports.py         RuntimeAdapter 协议 + InstanceSpec/VolumeSpec/VolumeCopySpec
├── domain/models.py 槽位/租约/项目/邀请码状态与合法迁移、数据目录规则
├── lifecycle.py     create -> start -> ready -> (drain) -> stop -> reset
├── allocator.py     分配、准入、抢占、解析、释放、GC（含活动任务感知）
├── cli.py           serve 与管理命令；管理命令一律走 HTTP，不直接开数据库
├── runtime/fake.py  内存适配器（测试用，精确模拟冷启动与故障）
├── runtime/docker_adapter.py  Docker SDK 实现（含卷复制 helper）
└── api/             FastAPI 路由：health / leases / workspaces / invitations / pages / admin
```

## 职责边界

**控制面负责**：用户身份与项目元数据、邀请码准入与优先级判定、项目分享的卷复制、
访客租约与容量、原子分配、认证网关的判定依据、容器 create/start/ready/stop/remove/reset、
代次管理、重启对账、整体预算。

**控制面不负责**：文档解析、解释/知识生成、Prompt/Harness、实例内的 session 与应用数据。
应用内部仍然有自己的 Bearer 鉴权，控制面不删除它、也不伪造它（受信的会话交换尚未实现）。

**关键不变量**

- 一份镜像多次创建实例；持久项目有独立数据卷，计算回收保留卷，闲置超时才清理。
- 项目数据位于卷内 `users/<owner_uuid>/workspaces/<workspace_uuid>`；应用 session 放在
  卷外临时目录，不随卷复制带走。
- 匿名临时租约仍使用槽位临时卷，回收时删除，与持久项目路径分开。
- 未通过 `GET /health/ready`（200 且 `status=ok`）的实例绝不交给访客；持久模式还须确认
  `workspace_id` 与存储布局声明。
- 任何生命周期失败都进入 `quarantined`，必须人工 `release-quarantine`
  后才能复用；停止失败不允许当作"干净槽位"放回。
- 邀请码只存哈希，明文只在签发响应出现一次；系统码与分享码前缀/哈希域隔离，分享码永不提升优先级。
- 只有声明 v3 `content_only` 布局的项目才能作为分享源；复制先停源计算，失败不发布半成品。
- 卷复制 helper 必须是本机已有镜像，绝不隐式拉取。
- `lease_id` / `slot_id` / `generation` / 应用 `session_id` 是四个不同概念。
- 客户端传入的 `slot_id`、上游地址或邀请码类型/优先级一律忽略。
- 实例容器**不**发布宿主机端口，只在 Docker 内网按容器名访问。
- 控制面只操作带自身 label 的容器与卷（allowlist），不影响其他项目。

## 快速开始

```bash
# 在仓库根目录执行；脚本设置本地 fake 配置并绑定 loopback。
.\.venv\Scripts\python.exe -m pip install -r runtime-host/control-plane/requirements-dev.txt
.\scripts\dev-runtime-host.ps1
```

无 Docker 时用 `CONTROL_PLANE_ADAPTER=fake` 即可跑起完整流程。
配置可用 TOML 文件（`CONTROL_PLANE_CONFIG` 或 `--config`）叠加环境变量，见
`../../docs/configuration.md`；CLI 用法见 `../../docs/cli.md`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health`、`/control/health` | 控制面存活；不代表实例池可用 |
| GET | `/health/ready` | 控制面就绪；未配置管理令牌时 `degraded` |
| GET | `/control/capacity` | 池容量（`slots_ready/starting/active/...`） |
| GET | `/control/access-policy` | 公开能力探针：是否需要邀请码、是否支持系统码/分享 |
| POST | `/control/leases` | 申请试用；可带 `workspace_id` 与 `invitation_code`；带有效凭据时**幂等**复用 |
| GET/DELETE | `/control/leases/current` | 查询 / 主动结束当前租约 |
| POST | `/control/leases/{lease_id}/pages*` | 页面存活与关闭通知，全部页面离开后自动释放计算 |
| POST | `/control/users`、`/control/users/session` | 创建 UUID 身份、凭据恢复登录 |
| GET/POST | `/control/workspaces*` | 用户项目管理与显式使用心跳 |
| POST/GET/DELETE | `/control/workspaces/{uuid}/invitations*` | 项目分享码的签发 / 列表 / 撤销 |
| POST | `/control/workspace-imports` | 用分享码导入一个独立副本 |
| GET/POST/DELETE | `/control/admin/invitations*` | 系统码的列表 / 签发 / 撤销 |
| GET/POST | `/control/admin/*` | 槽位观测、drain、强制回收、解除隔离、对账、GC、配置摘要 |

管理接口需要 `X-Admin-Token`；未配置令牌时**一律拒绝**（fail closed），并且公网网关对
`/control/admin/` 直接返回 403。

`POST /control/leases` 的 `token` 只在响应里出现一次，数据库仅存
`HMAC-SHA256(pepper, token)`；邀请码同样只存哈希。响应不含内网 `upstream`，避免泄漏内部拓扑。

## 测试

```bash
# 全部单元测试（fake adapter，不需要 Docker）
python -m pytest tests -q

# 指定模块
python -m pytest tests/test_pool_lifecycle.py -q

# 真实 Docker 集成测试（默认跳过）
CONTROL_PLANE_DOCKER_IT=1 python -m pytest tests/test_docker_integration.py -v
```

真实集成测试需要一个已存在的镜像（默认 `tutorlm-host:0.2.0-single`，可用
`CONTROL_PLANE_IT_IMAGE` 覆盖）和可访问的 Docker daemon；它使用
`cpit-<随机>` 前缀与独立 label，结束时会清理自己创建的容器与卷。

## 尚待实现（不要当成已可用）

- 容器内业务应用与完整工作台尚未实现；`instance-app/backend/` 仍为空目录。
- 受信的应用会话交换：把租约绑定到实例内的应用会话，尚未实现，控制面不伪造应用身份。
- v3 需要应用镜像声明存储布局；未声明的镜像不能作为分享源，分享会返回 `share_source_unsupported`。
- 实例状态探针（`generation`/活动任务数/drain 状态）：在应用补上
  `/internal/status` 之前，空闲回收使用保守探针（视为一直忙），
  因此**不会**自动因空闲而关机，需显式释放或等绝对 TTL。

私有 `.env`、凭据、访客 token 与真实配置文件均不入库（见 `.gitignore`）。
持久项目接口、双重 TTL、邀请码与分享流程见 `../../docs/workspaces.md`。