# Instance App Backend

容器内应用后端。**已实现可本地启动的 HTTP Host**：`backend/__main__.py` 用 uvicorn 提供
`backend.adapters.http.app:create_app`（`python -m backend`），装配 `backend.host.state` 组合根
与 `/health`、`/health/ready`、`/api/runtime`、`/api/sessions`、`/api/projects`、`/api/documents`、
`/api/runs`、`/api/sources` 等文档化路由。设计与 API 契约见
[`../../docs/instance-app-design.md`](../../docs/instance-app-design.md) 与
[`../../docs/instance-app-api.md`](../../docs/instance-app-api.md)。

插件直接使用 [Plugin SDK v1](../../docs/plugin-sdk.md)：统一 manifest、生命周期、
`WorkflowInput -> Draft`、请求对象式 ModelProvider 与 Host 原子提交。
内置插件 ID 为 `documents`、`explanations`、`knowledge-narration`，
支持 `explanation`、`narration`、`retelling` 三类任务；没有独立兼容层。

**能力边界（不要当作已交付）**：

- HTTP 层**不提供** `/docs` 与 `/openapi.json`（`docs_url=None`，设计 §6 的有意选择）；
  用 `/health`、`/health/ready`、`/api/runtime` 探查。
- md/txt 抽取与 PDF **结构化门禁**可用；PDF 文本抽取依赖 `requirements.txt` 声明的 `pypdf`
  后端，**未安装时抽取端口 fail closed**（`extractor_unavailable`），绝不把 pdf 二进制当文本解码。
  浏览器端 PDF.js 引擎桥接和自托管资源导出已实现，完整工作台与真实浏览器集成尚未完成。
- TeX 编译在本仓库**无安全执行器**：默认 `tex_compile_unavailable`，路由返回 **503**（见设计 §16
  与 [`../../docs/tex-compilation-security.md`](../../docs/tex-compilation-security.md)）。
- managed 形态的卷初始化、克隆重绑和受控 session 引导已按 [v4](../../contracts/instance-runtime-v4.md)
  接通并通过隔离 Docker 验证；应用会话仍在卷外，业务请求仍需应用 Bearer。
- 完整工作台位于 `../workbench/`，通用模块位于 `../frontend/`，部署时需要一起交付。

## 分层

保留既有分层边界，不 import 外层私有模块，也不读外层数据库：

```text
backend/
  __main__.py         # 入口：uvicorn 提供 adapters.http.app:create_app
  host/               # 组合根与本地装配
    config.py         #   HostConfig：TUTORLM_* 严格解析（唯一配置来源）
    state.py          #   AppState/build_state：装配 store/storage/sessions/runs/plugins/services/health
    services.py       #   供 adapters/http 调用的门面（序列化与用例编排）
    runs.py           #   RunManager：run 状态机 + 各 workload class 的 bounded executor
    health.py         #   HealthService：/health 与 /health/ready 载荷
    binding.py        #   workspace 绑定校验与布局标记落盘（纯判定见 domain/workspace_binding.py）
    storage_probe.py  #   就绪判定所需的存储事实采集（无策略）
    registry.py       #   SDK 注册表、依赖校验与统一工作流执行
    plugins.py        #   内置插件选择
    context.py        #   工作区限定的插件上下文与提交前检查
    errors.py         #   Host 层错误类型
    local/            #   本地装配：FakeProvider / LocalStore / LocalStorage / SessionStore
  domain/             # 纯领域模型与规则（无 IO、无框架）：models, enums, values, run,
                      #   readiness, storage_layout, volume, workspace_binding, paths, hashing, ids, text, errors
  application/
    ports/            # 抽象端口：provider / executor / repositories / unit_of_work / content / artifacts / clock / config
    format/           # 格式端口：media 探测、contracts、extraction、selectors、errors
  infrastructure/
    settings.py       # Settings/ModelSettings：与 HostConfig 平行的一套严格 TUTORLM_* 解析
    sqlite/           # WAL 连接、schema、迁移、UnitOfWork、repository 实现
    storage/          # 不可变原件 / artifacts、安全路径、布局证据采集
    format/           # md/txt/pdf 适配器（纯解析/抽取，不执行用户代码）与 tex_project/tex_compile 校验
    providers/        # fake / openai_compatible（密钥只来自 Settings，不读 env）
  adapters/http/      # 路由、schemas、错误映射、multipart、dependencies（同源，供网关代理）
    routers/          #   health, runtime, sessions, projects, documents, graph, runs, sources
  plugins/            # 静态插件：documents、explanations、knowledge_narration
  plugin_contracts/   # SDK v1：manifest、context、workflow、result、validation、learning
  tests/              # 见下方“测试”
```

`host/local/` 与 `infrastructure/providers|sqlite|storage` 目前**并存**：`host/local` 是当前
`build_state` 实际装配的本地实现，`infrastructure/*` 是承接同一端口契约的落地实现。两者都不得
import 外层私有模块，也不得读外层数据库。`host/` 不含 `app.py`，也没有 `runtime/`、
`adapters/plugins/` 包：HTTP 组装在 `adapters/http/app.py`，运行模式编排在 `host/state.py`。

## Host 的边界

Host **负责**：HTTP 组装、应用会话能力与配额、RunManager（任务状态机 + bounded ThreadExecutor）、
插件生命周期、存储与会话装配、`/health` 与 `/health/ready`。

Host **不得**：挂 Docker socket、读租约数据库、实现邀请码/分享、伪造或转换外层身份、
决定上游地址。外层用户 Cookie 与恢复凭据不会转发到应用。

## 关键约束

- 项目业务内容写 `TUTORLM_DATA_DIR`；应用 session/凭证只写卷外 `TUTORLM_SESSION_DATA_DIR`。
- SQLite 开 WAL，写操作走 UnitOfWork；**模型调用在事务外**，写入前校验 workspace/session/run 状态。
- 任务状态机 `queued → running → completed|failed|cancelled|interrupted`；取消后不 commit；
  硬停止/重启不重放付费调用。
- **workload class 分离**：`model` / `extract` / `tex_compile` 各有独立 executor 与额度；编译不占模型队列。
  抽取/编译的版本快照、幂等与 stale completion **不覆盖新 version**；`partial`/`failed` **保留旧成功产物**。
- **不可变原件**：上传/替换写 `sources/<document_id>/<revision_id>/`，不可就地改写；新内容 = 新 revision；
  原件下载（range/HEAD）与文本抽取是**分离端点**，`document/content` 只对文本类返回 `text`，不返回 pdf 二进制。
- 业务消费统一 `source_ref`（§15）解析出的文本，不渲染、不管理文件生命周期；refs 绑定具体 revision/artifact/build，不静默跟新。
- 模型密钥只来自 env，不写项目卷、不入库、不入镜像、不复制 `.env`。
- 业务接口只作用于 env 绑定的当前 workspace；绝不信任客户端提交的 owner/workspace。
- 后端**不**执行任意 shell/用户代码，**不**内置 TeX 编译器；编译走受限任务（见专文），无安全执行器时不可用。

## 依赖与本地运行

运行时依赖见 [`requirements.txt`](requirements.txt)（FastAPI/Starlette/Pydantic/uvicorn + pypdf），
开发测试依赖见 [`requirements-dev.txt`](requirements-dev.txt)（追加 pytest/httpx）。
**先装依赖再运行**，否则 `python -m backend` 会因缺少 uvicorn/FastAPI 直接退出：

```powershell
# 仓库根目录
.\.venv\Scripts\python.exe -m pip install -r instance-app/backend/requirements-dev.txt
```

本地启动（真实后端，非 fake 控制面；loopback、fake provider、无 Docker、无付费模型调用）：

```powershell
# 仓库根目录；默认 127.0.0.1:8091，数据/会话写在 var/instance-app/（git-ignored）
.\scripts\dev-instance-app.ps1
.\scripts\dev-instance-app.ps1 -Help            # 参数与安全默认值
.\scripts\dev-instance-app.ps1 -AllowImplicitSession   # 显式开启隐式本地会话（scratch 工作区）
```

脚本细节与安全默认值见仓库根 [`README.md`](../../README.md) 的“本地开发”一节。
应用**不提供** `/docs`；启动后访问 `http://127.0.0.1:8091/health`、
`/health/ready`、`/api/runtime`。demo 模式用打印出的体验码换应用会话：
`POST /api/sessions {"invite_code":"<code>"}` → `access_token`。

## 测试

```powershell
# 仓库根目录；backend 测试独立进程运行（避免与控制面 tests 包冲突）
$env:PYTHONPATH = "instance-app"
.\.venv\Scripts\python.exe -m pytest instance-app/backend/tests -q

# 静态未用导入检查与边界检查
.\.venv\Scripts\python.exe -m pyflakes instance-app/backend
.\.venv\Scripts\python.exe scripts/check-boundaries.py
```

前端模块单独用 Node 运行：`node --test instance-app/frontend/tests/*.test.js`。
CI 见仓库根 [`.github/workflows/instance-app.yml`](../../.github/workflows/instance-app.yml)：
backend（pip + pyflakes + pytest + 边界检查）与 frontend（`npm ci` + node）分进程运行。

真实 Docker 集成测试**显式 opt-in**（设置对应环境开关后运行），使用独立前缀与 label 并自清理。
普通开发用 fake runtime/provider，不需要 Docker 与模型密钥。
