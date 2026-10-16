# 容器外调度控制面（control-plane）

仓库级开发边界与启动方式见 `../../README.md` 和 `../../docs/architecture.md`。

本目录实现 TutorLM 多实例架构的**容器外**部分：匿名试用租约、固定槽位分配、
实例容器生命周期与回收。它是一个独立的 FastAPI 服务，不含任何文档解析、
Prompt 或插件逻辑——那些属于仓库根目录的 `instance-app/`。

与 `../instance-app` 的唯一耦合是 HTTP 契约
（见 `../../contracts/instance-runtime-v1.md`）。两侧不共享数据库、不互相
import 私有模块。

```text
control_plane/
├── config.py        全部 CONTROL_PLANE_* 环境变量解析与校验
├── errors.py        稳定错误码 + HTTP 状态映射
├── security.py      访客令牌生成/哈希/校验
├── store.py         SQLite：槽位、租约、事件（原子分配在这里）
├── ports.py         RuntimeAdapter 协议 + InstanceSpec/VolumeSpec
├── domain/models.py 槽位/租约/工作负载状态与合法迁移表
├── lifecycle.py     create -> start -> ready -> drain -> stop -> reset
├── allocator.py     分配、解析、释放、GC（含活动任务感知）
├── runtime/fake.py  内存适配器（测试用，精确模拟冷启动与故障）
├── runtime/docker_adapter.py  Docker SDK 实现
└── api/             FastAPI 路由：health / leases / admin
```

## 职责边界

**控制面负责**：访客租约与容量、原子分配、认证网关的判定依据、容器
create/start/ready/stop/remove/reset、代次管理、重启对账、整体预算。

**控制面不负责**：文档解析、解释/知识生成、Prompt/Harness、实例内的
session 与应用数据。应用内部仍然有自己的 Bearer 鉴权，控制面不删除它、
也不伪造它（应用 session 引导是后续独立任务）。

**关键不变量**

- 一份镜像多次创建实例；每个槽位一个**独立数据卷**，回收时必须删除。
- 未通过 `GET /health/ready`（200 且 `status=ok`）的实例绝不交给访客。
- 任何生命周期失败都进入 `quarantined`，必须人工 `release-quarantine`
  后才能复用；停止失败不允许当作"干净槽位"放回。
- `lease_id` / `slot_id` / `generation` / 应用 `session_id` 是四个不同概念。
- 客户端传入的 `slot_id` 或上游地址一律忽略。
- 实例容器**不**发布宿主机端口，只在 Docker 内网按容器名访问。
- 控制面只操作带自身 label 的容器与卷（allowlist），不影响其他项目。

## 快速开始

```bash
# 在仓库根目录执行；脚本设置本地 fake 配置并绑定 loopback。
.\.venv\Scripts\python.exe -m pip install -r runtime-host/control-plane/requirements-dev.txt
.\scripts\dev-runtime-host.ps1
```

无 Docker 时用 `CONTROL_PLANE_ADAPTER=fake` 即可跑起完整流程。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health`、`/control/health` | 控制面存活；不代表实例池可用 |
| GET | `/health/ready` | 控制面就绪；未配置管理令牌时 `degraded` |
| GET | `/control/capacity` | 池容量（`slots_ready/starting/active/...`） |
| POST | `/control/leases` | 申请试用；带有效 Bearer 时**幂等**复用原租约 |
| GET | `/control/leases/current` | 查询当前租约（401 表示无有效凭据） |
| DELETE | `/control/leases/current` | 主动结束试用，进入回收 |
| GET/POST | `/control/admin/*` | 槽位观测、drain、强制回收、解除隔离、对账、GC |

管理接口需要 `X-Admin-Token`；未配置令牌时**一律拒绝**（fail closed）。

`POST /control/leases` 的 `token` 只在响应里出现一次，数据库仅存
`HMAC-SHA256(pepper, token)`。响应不含内网 `upstream`，避免泄漏内部拓扑。

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

- Nginx 网关已实现在 `../gateway/`，部署入口在 `../deploy/`；完整联调仍需应用镜像和前端制品。
- 应用 session 引导：把租约绑定到实例内的应用会话。
- 实例状态探针（`generation`/活动任务数/drain 状态）：在应用补上
  `/internal/status` 之前，空闲回收使用保守探针（视为一直忙），
  因此**不会**自动因空闲而关机，需显式释放或等绝对 TTL。

私有 `.env`、凭据与访客 token 均不入库（见 `.gitignore`）。
