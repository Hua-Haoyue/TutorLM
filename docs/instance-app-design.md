# 容器内 Instance App 架构设计

> 状态：**设计文档**。`instance-app/backend/` 已落 **foundation 实现**（domain、application/ports、format
> 契约/适配、infrastructure/{sqlite,storage,format,providers}、adapters/http、host、plugins），本地可用
> `python -m backend` 起服务；剩余格式读取/编译与工作台前端接线仍属**设计（未实现）**。
> 本仓库**未**执行真实 Docker、未构建或拉取镜像、未访问远程基础设施、未改动 `contracts/` /
> `runtime-host/` / `sync/` 与既有前端模块。
> 依据：`../contracts/instance-runtime-v1.md`、`v2`、`v3`，`../contracts/gateway-lease-v1.md`、`v2`，
> `architecture.md`、`development.md`、`workspaces.md`，以及 `../instance-app/frontend/` 现有模块
> 与同步工作台前端里真实的字段与调用。
>
> **本轮扩展（设计）**：在原有 md/txt 之上补齐 **pdf / tex（含 tex 多文件工程）** 的文档模型与
> Runtime 基础服务（§14）、浏览器 Reader 与统一引用模型（§15）、格式适配器与受限编译（§16）、
> 上传配额与 GC（§17）。两条细则另立专文，主设计只链接、不复制：
> [`document-readers.md`](document-readers.md)（Reader 框架、统一 reference schema 与各格式 selector）
> 与 [`tex-compilation-security.md`](tex-compilation-security.md)（TeX 编译沙箱与安全抽取）。
> 代码现状：`instance-app/backend/` 已有 foundation 骨架与本地 HTTP/API、SQLite 存储与会话、fake/openai
> 兼容 provider（见 `../instance-app/backend/README.md`）。本文 §14–§17 的 md/pdf/tex 读取、浏览器 Reader、
> 受限编译等仍为**计划**：本仓库**无**安全编译器，编译/抽取的端到端能力**当前未实现**。
> 事实边界：本地 `reference/` 只有容器外控制面的快照，**没有**原容器内 backend 源码或镜像，
> 因此本文只做设计，不声明"恢复/逐行对齐/已验证"任何旧实现。密钥、凭据、数据库、镜像一律不入库。

## 0. 定位

容器内应用只负责**一个 workspace 的业务内容与运行**：文档（md/txt/pdf/tex）、节点图谱、解释/串讲、
任务与模型调用。它**不**承担 Docker 调度、租约、外层用户身份与邀请码/分享实现——那些属于 `runtime-host/`。
文档支持多种格式，但**同一 workspace 仍是单 project**：`tex` 多文件工程是**一个 document revision
下的多 resources**，不是新增控制面 project，也不引入新的外层概念。

```text
Browser -> Nginx -> admission -> Control Plane -> Docker API
              |                                        ^
              +-> selected instance -> Runtime Host ---+  (instance-app/backend/host)
                                             |-> 卷内项目业务内容 (TUTORLM_DATA_DIR)
                                             |-> 卷外会话/凭证   (TUTORLM_SESSION_DATA_DIR)
                                             |-> model provider (env-only key)
```

## 1. 所有权与不变量

| 范围 | 归属 | 明确不承担 |
| --- | --- | --- |
| `instance-app/backend` | 实例内 Host、分层、任务、插件、模型与存储 | Docker 调度、租约数据库、外层用户认证、邀请码/分享 |
| `instance-app/frontend` | 阅读、节点图谱、解释、串讲、浏览器身份接入 | 决定上游容器地址、直连实例端口 |
| `instance-app/deploy` | 应用镜像与前端制品构建 | 外层网关/控制面配置 |
| `runtime-host/*` | 身份、项目元数据、邀请码准入与分享、租约、容量、卷、Docker | Prompt、文档解析、应用 session |

不变量（必须成立，否则视为实现缺陷）：

- 实例容器**不**挂 Docker socket、**不**访问控制面数据库、**不**发布宿主机端口。
- `lease_id` / `slot_id` / `generation` / 应用 `session_id` / `workspace_id` 是不同概念，不可互相替代。
- 一个容器同一时刻只对应**当前一个** `workspace`；同用户多项目由控制面管理，不跨卷访问。
- 项目业务内容只写 `TUTORLM_DATA_DIR`；应用 session/凭证只写 `TUTORLM_SESSION_DATA_DIR`（卷外）。
- 未通过 `/health/ready`（200 且 `status=ok`，持久模式须项目绑定确认）的实例绝不接入访客。
- **原始文件不可变**：上传的 md/txt/pdf/tex（及工程 resources）按 `revision` 存为不可变原件，
  读取、抽取、编译、渲染都不就地改写原件；新内容产生**新 revision**，旧 revision 与其 artifacts/builds 可查。
- 应用**不**挂 Docker socket、不发布端口、不执行任意 shell/用户代码；受限 TeX 编译若在**本仓库**
  无安全执行环境，则默认为 `tex_compile_unavailable`，**不**降级到不安全路径（§16）。

## 2. 分层与目录

保留既有分层边界：`host` / `domain` / `application(ports, services)` / `infrastructure(sqlite, storage,
providers)` / `adapters(http, plugins)` / `plugin_contracts` / `runtime`。
多格式支持**不新增顶层层**，只在既有层内扩展：基础服务（原件/content/range/revision）落在
`application/services` + `infrastructure/storage`，格式适配器落在 `application/format`（纯用例/端口组合），
Reader 是**前端**职责（`frontend/reader`），编译/抽取执行走 `host` 的 RunManager 端口（§16）。

```text
instance-app/
  backend/
    host/            # 组合根与 Host 级关注点
      state.py       #   AppState 与 build_state 组合根（装配唯一入口）
      config.py      #   HostConfig：env 解析与校验（显式字段）
      binding.py     #   workspace 绑定/重绑与 fail closed 判定
      local/         #   local/demo 装配：会话、存储、provider
      runs.py        #   RunManager：任务状态机 + bounded executor
      plugins.py     #   插件注册与生命周期
      health.py      #   /health、/health/ready 声明
    domain/          # 纯领域模型与规则，无 IO
    application/
      ports/         # 抽象端口：repository / provider / clock / id / executor
      services/      # 用例：documents, revisions, content, nodes, graph, explanations, narrations, runs
      format/        # 格式适配器端口：md/pdf/tex 解析、抽取、source-map（实现见 infrastructure/format）
    infrastructure/
      sqlite/        # WAL、UnitOfWork、迁移、repository 实现
      storage/       # 内容文件存储、不可变原件/artifacts、安全路径、range、布局标记
      format/        # md/pdf/tex 适配器实现（纯解析/抽取，不执行用户代码）
      providers/     # fake / openai_compatible
    adapters/
      http/app.py    #   ASGI 应用装配：create_app/build_app（只做 HTTP 组装）
      http/          #   路由、schemas、错误映射
      plugins/       # 静态插件：explanation、narration
    plugin_contracts/# 插件契约：kind / workflow / 输入输出 schema
    runtime/         # 运行模式（local/demo/managed）编排与装配
    tests/           # unit / api / provider_fake / copy_contract / browser / docker_it
    pyproject.toml
    requirements.txt
  frontend/          # 现有租约生命周期 + 邀请码入口（本轮不改）；Reader/工作台为设计（§15）
  deploy/            # Dockerfile、入口脚本、镜像构建设计（见 deploy/README.md）
```

**Host 的职责**（且仅有这些）：

| 职责 | 说明 |
| --- | --- |
| HTTP 组装 | 装配路由、中间件、错误信封、内容类型与安全响应头 |
| 会话能力与配额 | 应用 Bearer 的签发/校验、上限计数（与租约无关） |
| RunManager | 任务入队、状态机、并发上限、取消、关停收尾 |
| 插件生命周期 | 注册、启用/禁用、`kind`/`workflow` 发现 |
| 存储装配 | 打开业务库与会话库、校验布局、绑定当前 workspace |

**Host 不得**：挂 Docker socket、读租约数据库、实现邀请码/分享、伪造外层身份、
把外层 Cookie/恢复凭据转成应用身份、决定上游地址。

## 3. 身份与数据流（关键缺口分阶段）

三层身份互不等同：外层用户身份（控制面 SQLite）／计算租约（HttpOnly Cookie）／应用会话（Bearer）。
控制面**当前不注入 bootstrap 凭据、不转发外层用户 Cookie，只在网关保留 `Authorization`**。

### 3.1 当前已实现（阶段 0，作为基线）

- 网关按租约准入后把 `/api/*`、`/health*` 代理到实例，剥离外层 Cookie 与 `X-User-ID`/`X-User-Token`。
- 应用保留自己的 Bearer：`POST /api/sessions {invite_code}` 换取 `access_token`。
- 因此"邀请码门 + 应用体验码"会形成二次输入，且直连实例端口可绕过租约——本设计不消除这两点。

### 3.2 阶段 A（**显式 opt-in 过渡**，仅本地受控部署验证；不宣称 production 安全）

阶段 A **不是默认行为**，也不是仅凭 env 就签会话。它由一个**新增的应用配置**显式开启：

- **`TUTORLM_ALLOW_IMPLICIT_SESSION`**（应用配置，**planned，默认 `false`**）：
  只有**显式设为真**、且同时满足"持久模式（`TUTORLM_PERSISTENT_WORKSPACE=true`）+
  存在绑定的 workspace env（`TUTORLM_WORKSPACE_ID` / `TUTORLM_OWNER_ID`）+
  部署明确标注为 local 验证"时，应用才允许在没有 `invite_code` 时签发一个
  **绑定当前 workspace 的应用会话**。
- **默认（未开启该开关）行为 = 拒绝隐式会话**：`POST /api/sessions` 在无凭据时返回未授权，
  与"未完成 trusted bootstrap"一致；业务 **Bearer 保留**，不取消应用鉴权。
- **不把"拓扑可检测"当作安全保证**：应用**不**声明自己"能检测到是否被绕过网关"，
  也不以"应该只在 Docker 内网可达"作为鉴权前提。隔离是**部署约束**，不是应用可验证的安全属性。
- 绑定来源只能是**控制面注入的 env**，**绝不**接受客户端提交的 owner/workspace 头或字段。
- 该会话的 token 只存会话库（卷外），并仍然区分"应用会话 vs 租约"。
- **适用范围严格限定**为本地受控部署 + 显式开关：这是**弱鉴权过渡**，
  不构成 production 安全边界；**不得**将阶段 A 描述为已具备 production 级可信身份。

### 3.3 阶段 B（**正式下一契约 ADR，推荐方案**；当前未实现）

**推荐决策**：受信身份应通过**正式的下一个版本化契约 ADR** 落地，而**不是**把阶段 A 的 env 弱鉴权
当作默认长期方案。ADR 必须与 `contracts/` 变更一并由两侧评审。

- 由外层 admission 签发**短期、强绑定**的 bootstrap token：绑定 `audience` + `workspace_id` +
  `generation`（必要时含 `lease_id`），用**每 instance 隔离的 secret** 校验，有效期很短且一次性。
- 应用用它建立应用会话，替换"无凭据自动签会话"；token 不经浏览器持久化，不被 client 伪造。
- 该能力当前**不存在**：控制面不会代应用签发 session。本文不假实现、不把 ADR 当作已有接口；
  在 ADR 冻结前，managed 形态仍**保留 Bearer**，不取消应用鉴权。

### 3.4 邀请码与分享的边界

- **应用内不重复实现**邀请码/分享：`/api/projects/{id}/share`、`/api/shares/{code}/redeem` 不再由 app 提供。
- 分享签发/导入属控制面（`POST /control/workspaces/{uuid}/invitations`、`POST /control/workspace-imports`）；
  前端必须改为调用控制面端点，**不**让 app 触达 Docker 或外层数据库。
- 应用自身体验码（`TUTORLM_INVITE_CODE`）在 standalone/demo 形态保留；managed 形态**默认仍要求凭据**，
  仅当显式开启 `TUTORLM_ALLOW_IMPLICIT_SESSION`（planned，默认 `false`）时才走阶段 A 过渡。

## 4. 数据布局与 schema

### 4.1 卷内（`TUTORLM_DATA_DIR`，唯一业务内容根）

```text
<TUTORLM_DATA_DIR>/
  business.sqlite3          # 业务 + run history（WAL）
  business.sqlite3-wal
  business.sqlite3-shm
  sources/<document_id>/<revision_id>/   # 不可变原件（md/txt/pdf/tex 及 tex 工程 resources）
    main.<ext> / main.tex                # 规范文件名，由服务端生成，不用用户文件名
    resources/...                         # tex 多文件工程的其它 .tex/.sty/.bib/本地图片等
  artifacts/<document_id>/<revision_id>/ # 派生只读产物（抽取文本、编译 PDF、build 日志）
```

要点（不变量）：`sources/.../<revision_id>/` 一经写入即**不可变**；新内容写**新 revision 目录**，
不覆盖旧目录。`artifacts/` 只放**可再生的派生结果**，删除后可重建，原件仍在 `sources/`。
文件名、目录名全部由服务端按 id 生成，**绝不**使用用户文件名或路径（§10）。

### 4.2 卷外（`TUTORLM_SESSION_DATA_DIR`，如 `/tmp/tutorlm-session/sNN`）

```text
<TUTORLM_SESSION_DATA_DIR>/
  sessions.sqlite3          # 会话注册、token 哈希、过期、workspace 绑定、配额计数
```

- **应用 session / 凭证绝不写卷内**：分享/克隆按目录整树复制，卷内任何 session 都会变成接收者身份。
- 卷复制与 GC 不得删除持久项目；容器 hardstop 后卷内数据仍在。

### 4.3 业务库 schema（草案，字段对齐前端实体）

| 表 | 关键列 | 说明 |
| --- | --- | --- |
| `projects` | id, workspace_id, title, description, created_at, updated_at | 每 workspace 恰好一行，`workspace_id` 唯一 |
| `documents` | id, project_id, title, source_filename, format, status, current_revision_id, order_index, created_at, updated_at | `format` ∈ `md\|txt\|pdf\|tex`（**草案枚举**）；`current_revision_id` 指向最新 revision；`status` 值域 `pending\|ready\|failed\|unsupported`（见 API §4） |
| `document_revisions` | id, document_id, revision_no, content_hash, size_bytes, status, created_at | **版本化原件**：每次上传/替换生成一行；`content_hash` 为原件摘要；原件不可变（§14.1） |
| `document_resources` | id, revision_id, logical_path, kind, content_hash, size_bytes | tex 多文件工程的 resources（`.tex/.sty/.bib/本地图片` 等）；`logical_path` 为工程内相对路径，非磁盘路径 |
| `nodes` | id, project_id, document_id, title, kind, summary, content, order_index, parent_id, metadata_json, revision, created_at, updated_at | `metadata={source{quote,block,label|fromNode},status,position}`；`revision` 用于并发 |
| `edges` | id, project_id, source_node_id, target_node_id, relation, metadata_json, created_at | 前端 `{source_node_id,target_node_id,relation,metadata}` |
| `explanations` | id, project_id, node_id, question, answer, provider, model, created_at | 前端 `Explanation` |
| `narrations` | id, project_id, title, segment_count, total_duration_seconds, provider, model, created_at | 前端 `Narration` |
| `narration_segments` | id, narration_id, order_index, node_id, title, text, duration_seconds | 段序 |
| `runs` | id, project_id, kind, state, input_json, result_json, error_code, error_message, idempotency_key, created_at, updated_at | run history 可留卷内，**不留 token** |
| `run_events` | run_id, seq, type, payload_json, created_at | SSE 可选事件的单调 `seq` |
| `document_extractions` | id, revision_id, extractor_version, status, text_hash, page_count?, error_code?, created_at | 抽取结果元数据；正文文本落 `artifacts/.../<revision_id>/extracted.*`，**不**在库里存 pdf 二进制 |
| `document_artifacts` | id, revision_id, kind, path, content_hash, size_bytes, created_at | 派生产物（抽取文本、编译 PDF、build 日志）；`kind` ∈ `extracted_text\|compiled_pdf\|build_log\|...` |
| `document_builds` | id, revision_id, status, source_map?, artifact_id?, engine_version?, created_at, updated_at | 编译 build 状态/ID；`source_map` **可选**，取决于真实 compiler 能力（§16） |
| `workspace_binding` | current_workspace_id, origin_workspace_id, owner_id, copy_id?, updated_at | **访问上下文**，与业务内容解耦；克隆时只重绑这一行（§4.5） |

规则：

- SQLite 开 **WAL**；写操作走 **UnitOfWork**（显式事务边界），repository 不裸开事务。
- **模型调用在事务外**：读输入 → 提交/关闭事务 → 调 provider → 重新开事务 → 校验并写入。
- 写入前**校验**：workspace 绑定未变、session 仍有效、run 仍 `running` 且未被取消；否则丢弃结果。
- `nodes.revision` 乐观并发：`PATCH` 携带旧 revision，不匹配返回 `409`；层级用 `parent_id`，
  删除父节点时子节点**提升为顶层**（`ON DELETE SET NULL`），不级联删子树。
- 文件/DB 一致性：删除文档在同一 UoW 内删子行 + `sources/` 文件；提供孤儿扫描，但不在请求路径阻塞。

### 4.4 布局兼容与分享安全

- 应用**必须真正消费 v3**：就绪声明回 v3 要求的全部字段，且只有**确认卷内无 session/凭证**时才声明 `content_only`。
- 旧 `legacy_root`（`TUTORLM_WORKSPACE_SUBDIRECTORY="."`）与全新 v3 子目录要**区分**：
  无法证明隔离的项目一律按 legacy 处理（不返回 `storage_layout`），**不无条件宣布 share safe**。
- 控制面据此：源项目未声明 `content_only` 时拒绝签发分享码（`409 share_source_unsupported`）。

### 4.5 克隆后的身份重绑定（rebind）

克隆得到**新 workspace UUID + 新卷**，但卷内业务库仍带**源 workspace_id**。核心是把两类身份**分离**：

- **业务 stable ID（内容身份，跨克隆不变）**：`project_id`、`document_id`、`node_id`、`edge_id`、
  `explanation_id`、`narration_id`，以及内部引用（`node.document_id`、`edge.source_node_id`、
  `narration_segments.node_id`）。克隆**原样保留**，否则引用会断、接收者会看到损坏图谱。
- **workspace access context（访问上下文，克隆后重绑）**：`workspace_id`、`owner` 绑定、存储布局。
  存于独立的 `workspace_binding` 行，与业务内容解耦；克隆时**只改这一处**。

**确定验证算法**（启动时执行，全部输入取自控制面注入，绝不取客户端）：

1. 读 env：`ws_dst = TUTORLM_WORKSPACE_ID`、`owner_dst = TUTORLM_OWNER_ID`，
   以及控制面注入的**迁移/血缘标识** `TUTORLM_WORKSPACE_SOURCE_ID`（可选）与 `TUTORLM_WORKSPACE_COPY_ID`（可选）。
2. 打开业务库，读 `workspace_binding`：`current_workspace_id`、`origin_workspace_id`、`owner_id`。
3. **相同**：`current_workspace_id == ws_dst` → 校验 `owner_id == owner_dst`；
   一致则正常打开（幂等），不一致则 fail closed（见第 5 步）。
4. **不同**（疑似克隆）：仅当同时满足以下**可信条件**才重绑——
   - 存在控制面注入的迁移标识，且 `origin_workspace_id == TUTORLM_WORKSPACE_SOURCE_ID`，
   - 且目标一致 `ws_dst`（如提供 `TUTORLM_WORKSPACE_COPY_ID` 则要求与库内记录匹配）；
   - 满足 → 在**一个事务**内把 `workspace_binding` 更新为新 UUID + 新 owner，
     写入 `workspace_rebound` 事件（旧/新 + 标识 id）。幂等、可重入；**业务内容与 stable ID 不动**。
5. **否则**（无迁移标识、标识不匹配、或 owner 冲突）→ **fail closed**：
   **不**盲写 owner、**不**把源内容当作目标 workspace 提供，就绪非 `ok` 并给出可操作原因。
   这既防止"错误挂载被静默当成目标项目"，也防止"把他人内容写成自己的"。

规则：**不得**仅因 UUID 不同就拒绝接收者的合法副本（第 4 步）；也**不得**在没有可信标识时盲写归属（第 5 步）。

**当前实施前置（不是现有功能事实）**：`TUTORLM_WORKSPACE_SOURCE_ID` 与 `TUTORLM_WORKSPACE_COPY_ID`
**控制面尚未注入**。因此：

- **分享克隆的容器内业务端到端当前尚未 ready**：缺少可信迁移标识时，第 4 步重绑条件无法成立，
  副本按第 5 步 **fail closed**（就绪非 `ok`），这是**预期**行为，不是 bug。
- 解锁条件：**控制面注入上述标识 + 版本化契约 ADR 冻结重绑语义**。在两者完成前，
  **不得**宣称分享→克隆在容器内可端到端工作，也**不得**用"读源 UUID 猜"或"直接盲写 owner"绕过。
- 会话在卷外，克隆天然不带旧会话：接收者得到全新会话。

## 5. 任务、RunManager 与事件

- 状态机：`queued → running → completed | failed | cancelled | interrupted`（`cancelled/interrupted/failed` 为终态）。
- **bounded ThreadExecutor**：固定工作线程上限 + 有界队列；队列满时快速失败（不无限排队、不拖垮容器）。
- **取消后不 commit**：取消即置 `cancelled` 并丢弃在途模型结果；已返回的 provider 结果也不落库。
- **硬停止/重启不重放付费调用**：容器 hardstop 或被抢占后，进行中 run 置 `interrupted`；
  重启**不自动重试** provider 调用（避免重复计费）。
- 幂等：`POST /api/runs` 带 `Idempotency-Key`，**作用域是 workspace 身份**（同 workspace 同 key 幂等），
  与应用 session 区分；同 key 不同输入 → `409`。
- 事件：`/api/runs/{id}/events` 为**可选** SSE，事件带单调 `seq`，支持 `Last-Event-ID`/`after_seq` 断点重放。
  **当前前端是轮询（约 250ms→1s 自适应），不依赖 SSE delta**；SSE 作为增量能力，不替代轮询。
- **workload class 分离**：run 带 `workload`（如 `model` / `extract` / `tex_compile`）。**编译不占用模型队列**：
  各 workload class 有**独立 executor 与资源额度**，避免长尾编译阻塞模型。抽取/编译的**版本快照/幂等/
  stale completion 不覆盖新 version**，`partial`/`failed` **保留旧成功产物**（§16）。

## 6. API 分组（契约草案，非已实现）

分组前缀 `/api`，除运行时发现外均需应用 Bearer；业务接口只作用于 env 绑定的当前 workspace。

| 分组 | 方法/路径 | 说明 |
| --- | --- | --- |
| 运行时发现 | `GET /api/runtime` | 返回 `mode`(local/demo/managed)、`provider{name,model}`、`plugins[{enabled,workflows[{kind}]}]`、`limits` |
| 会话 | `POST /api/sessions`、`GET /api/sessions/me`、`DELETE /api/sessions/me` | 签发/校验/注销应用会话；managed 持久模式按阶段 A 免重复体验码 |
| 项目 | `GET /api/projects`、`GET/PATCH/DELETE /api/projects/{id}` | 每 workspace 单项目；返回当前项目，不跨卷枚举 |
| 文档 | `GET/POST /api/projects/{id}/documents`、`POST .../documents/upload`、`GET /api/documents/{id}`、`GET /api/documents/{id}/content`（**纯文本类**全文 `text`）、`GET /api/documents/{id}/source`（原件 range）、`GET /api/documents/{id}/revisions`、`DELETE /api/documents/{id}` | md/txt/pdf/tex 上传（§14.2）；原件与抽取**分离端点**；`content` 仅对 md/txt 返回全文，**不**返回 pdf 二进制 |
| 抽取/构建 | `POST /api/documents/{id}/extract`、`GET /api/documents/{id}/extractions`、`POST /api/documents/{id}/compile`、`GET /api/documents/{id}/builds` | 抽取为 **run**（§5/§16）轮询终态；无文本层 → `unsupported`；无安全 executor 时 `compile` 直接 `503 tex_compile_unavailable`（不入队、不 fallback） |
| 引用 | `POST /api/sources/resolve` | 统一 `source ref` → resolved content（§14.4/§15），校验 revision/artifact/build 与权限，供 explain/narration context |
| 节点 | `POST /api/projects/{id}/nodes`、`PATCH /api/nodes/{id}`、`DELETE /api/nodes/{id}` | 人工划词锚定；`metadata.source` 存锚点 |
| 图谱 | `GET /api/projects/{id}/graph`、`POST /api/projects/{id}/graph/edges`、`DELETE /api/projects/{id}/graph/edges/{eid}` | 层级 + 关系边 |
| 解释 | `GET /api/projects/{id}/explanations` | 历史解释（`explanation` run 结果落库） |
| 串讲 | `GET /api/projects/{id}/narrations` | 历史串讲（`narration` run 结果落库） |
| 任务 | `POST /api/runs`、`GET /api/runs`、`GET /api/runs/{id}`、`POST /api/runs/{id}/cancel`、`GET /api/runs/{id}/events`（SSE 可选） | `kind: explanation|narration`；`follow` 轮询到终态 |
| 健康 | `GET /health`、`GET /health/ready` | 见 §8 |
| 兜底 | `GET /api/{unmatched:path}` | 统一 `404` JSON，区分"API 不存在"与 SPA 路由 |

请求/响应信封（与前端一致）：错误体 `{error:{code,message,details?}}`，`Retry-After` 头用于 429/503。
`POST /api/projects/{id}/documents` 入参 `{title, text, derive_nodes:false}`（**默认不生成节点**）；
上传为 `multipart/form-data`：`file` + `derive_nodes`。`run` 结果：`explanation` → `{explanation}`，
`narration` → `{narration}`。

**前端 API 差异（必须显式适配，不重复实现）**：

| 前端现状 | 目标 | 处理 |
| --- | --- | --- |
| `POST /api/projects`（创建项目）、`DELETE /api/projects/{id}` | 单 workspace 单 project | 适配为返回/操作当前项目，不做跨卷项目 CRUD |
| `GET /api/projects` 列表 | 每容器仅当前一个 | 只返回当前项目 |
| `POST /api/projects/{id}/share` | 分享属控制面 | 改调控制面 `POST /control/workspaces/{uuid}/invitations` |
| `POST /api/shares/{code}/redeem` | 导入属控制面 | 改调 `POST /control/workspace-imports` |
| demo 下必填体验码 | managed 持久模式免重复体验码 | 前端按 `runtime.mode`/`managed` 走阶段 A |

## 7. 插件与模型 Provider

- 插件契约在 `plugin_contracts/`：`kind`、`workflow`、输入/输出 schema、`enabled`。
  静态插件 `explanation`、`narration` 只做编排（取输入 → 调 provider → 校验 → 返回），不含模型逻辑。
- Provider 经端口抽象，实现两个：
  - `fake`：确定性离线输出，用于测试与演示，响应中明确标识 `provider.name=fake`；
  - `openai_compatible`：`MODEL_API_BASE` + `MODEL_API_KEY` + `MODEL_NAME`，兼容 OpenAI 风格接口。
- **模型密钥只从 env 读取**（`TUTORLM_MODEL_API_KEY`），**不写项目卷、不复制 `.env`、不落库、不入镜像**。
- 前端通过 `/api/runtime` 的 `plugins[].workflows[].kind` 发现能力，据此启用/禁用"解释/串讲"按钮。

## 8. 就绪与健康

`GET /health`：进程存活（容器内存活不等于就绪）。

`GET /health/ready`：

- 非持久模式：返回 `{"status":"ok","mode":"demo"}`（及 provider 概要）。
- 持久模式（`TUTORLM_PERSISTENT_WORKSPACE=true`）**必须**返回 v2/v3 全部字段：

```json
{"status":"ok","storage_mode":"persistent","workspace_id":"<UUID>","storage_layout":"content_only"}
```

- `workspace_id` 必须与控制面注入的 `TUTORLM_WORKSPACE_ID` 一致，否则控制面拒绝接入。
- `storage_layout` **只在已验证卷内无 session/凭证时才返回 `content_only`**；否则不返回（控制面按 `legacy_root` 记录）。
- 已初始化（`storage_initialized=1`）的卷缺失时必须报错，不得静默生成空卷冒充原项目。
- 就绪须真正验证：业务库可打开/可迁移、数据根可写、会话目录可写；仅当这些通过才返回 `ok`。
- 提供者/插件不健康时返回 `503`/`degraded`，绝不在降级时声明 `ok`。

## 9. 容器与镜像构建设计（不实际构建）

### 9.1 运行契约（由控制面注入，应用消费）

| 变量 | 说明 |
| --- | --- |
| `TUTORLM_DATA_DIR` | 当前项目数据根（新项目 `挂载点/users/<owner>/workspaces/<ws>`；旧项目为挂载点） |
| `TUTORLM_WORKSPACE_SUBDIRECTORY` | 相对挂载点子目录（旧项目 `.`） |
| `TUTORLM_SESSION_DATA_DIR` | **卷外** session/凭证目录（如 `/tmp/tutorlm-session/sNN`） |
| `TUTORLM_WORKSPACE_ID` / `TUTORLM_OWNER_ID` | 当前项目/所属用户 UUID |
| `TUTORLM_PERSISTENT_WORKSPACE` | `true` |
| `TUTORLM_RUN_MODE` / `TUTORLM_INVITE_CODE` / `TUTORLM_PROVIDER` / `TUTORLM_MODEL_*` | 运行模式、体验码、provider 与模型配置 |
| `TUTORLM_ALLOW_IMPLICIT_SESSION` | **planned（应用配置，默认 `false`）**：显式开启阶段 A 隐式会话（§3.2）。控制面是否透传待定；未开启时无凭据即拒绝 |
| `TUTORLM_WORKSPACE_SOURCE_ID` | **planned（当前控制面未注入）**：克隆源 workspace UUID，用于 rebind 可信校验（§4.5） |
| `TUTORLM_WORKSPACE_COPY_ID` | **planned（当前控制面未注入）**：复制 id，用于 rebind 幂等与匹配（§4.5） |

> 注意：控制面**不**注入 bootstrap 凭据；卷外的 `/tmp` 是控制面为容器设置的**容器内 tmpfs**（见 §9.3），
> 由控制面在创建容器时挂载，而非应用自行创建。
> 标注 **planned** 的键**当前不存在**：在控制面注入与契约冻结前，应用按"未提供"处理
> （隐式会话默认拒绝、克隆副本 fail closed），不得当作已具备能力。

### 9.2 Dockerfile（多阶段，设计）

```dockerfile
# syntax: 设计示意，不实际构建
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# 依赖锁定安装；不安装构建期密钥；不复制 .env
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

FROM base AS app
# 非 root：app 用户 uid/gid 10001（与控制面卷复制 helper 交出的属主一致）
RUN groupadd -g 10001 app && useradd -u 10001 -g app -m -s /usr/sbin/nologin app
WORKDIR /app
COPY backend /app
COPY frontend /app/static          # 前端制品（网关只读挂载同一份）
RUN mkdir -p /data /tmp/tutorlm-session && chown -R 10001:10001 /data /tmp/tutorlm-session
USER 10001:10001
EXPOSE 8000                        # 仅容器内网，控制面不发布宿主机端口
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2).status==200 else 1)"
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "uvicorn", "backend.adapters.http.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

要点：镜像**不含** `.env`/密钥/测试数据；`USER 10001` 非 root；`EXPOSE 8000` 仅供 Docker 内网；
`HEALTHCHECK` 打 `/health/ready`；`STOPSIGNAL SIGTERM` 配合优雅关停。

> **入口口径**：本节 `ENTRYPOINT` 与真实
> [`../instance-app/deploy/Dockerfile`](../instance-app/deploy/Dockerfile) 的 `ENTRYPOINT` **完全一致**
> （`backend.adapters.http.app:create_app` + `--factory`，监听 `0.0.0.0:8000`）。本地入口 `python -m backend`
> （`backend/__main__.py`）导入**同一**工厂 `backend.adapters.http.app.create_app`，与镜像入口**指向一致**。
> `backend.host.app` 模块**不存在**（Host 组合根在 `backend.host.state`，HTTP 装配在 `backend.adapters.http.app`），
> 实现与测试均不再引用它；两条入口都可解析。
> 本节其余行仍是**版式示意**，以 `deploy/Dockerfile` 为准（真实文件以**仓库根**为构建上下文，
> `COPY instance-app/backend /app/backend` 并设 `PYTHONPATH=/app`，入口因此能解析 `backend.*`）。
> 本仓库**未执行真实 Docker**（未构建、未拉取、未运行镜像），以上一致性与可解析性均为**静态比对**结论，
> **不是**运行验证。

### 9.3 根文件系统、tmpfs 与资源（控制面已按此加固）

控制面创建实例时的加固参数（`docker_adapter.py`，v3 已实现侧）：

- `read_only=true`（根文件系统只读）；`cap_drop=["ALL"]`；`security_opt=["no-new-privileges:true"]`；`init=true`。
- `tmpfs={'/tmp': 'size=<tmpfs_size>,mode=1777'}`：**应用会话目录 `TUTORLM_SESSION_DATA_DIR` 位于该 tmpfs 内**，
  因此卷外、不落盘、不随卷复制。
- 资源：`mem_limit`（默认 `2g`）、`nano_cpus`（默认 `2.0`）、`pids_limit`（默认 `256`）。
- 具名卷挂载到 `instance_mount_path`（默认 `/data`）；**无 Docker socket、无宿主机端口**。
- `STOPSIGNAL`/`stop_grace_seconds`（默认 30s）：停止是硬性计算期限，不等于无损任务迁移。

### 9.4 非 root（10001）写入权初始化问题（必须闭环）

**问题**：控制面为持久项目**新建具名卷**时，卷根目录属 **root**；容器以 uid `10001` 运行，
首次写入 `TUTORLM_DATA_DIR`（尤其是新子目录 `users/<owner>/workspaces/<ws>`）会 `EACCES`，
表现为"功能 bug"。

**现状（`runtime-host` 侧，事实）**：现有的 `docker_adapter.py` 卷**复制** helper（`COPY_OWNER_UID=10001`，
`user=0:0` + `cap_add=["CHOWN"]`）服务于**分享/克隆的源→目标复制**，它假定源目录已存在并整树拷贝。
**它不等于、也不能直接套用于"新建空卷初始化"**：新持久卷需要的是先在空卷里 `mkdir` 规范子目录再交出属主，
而不是复制任何源内容。

**重要澄清（当前未实现）**：**面向新建持久卷的属主初始化 helper 在当前控制面尚未实现**。
因此下列流程是**v3 前置条件的接口设计**，不是已具备的能力；在它落地前，新持久卷首次写入仍会失败。

**确定的初始化顺序（外层实现，列为 v3 前置）**：

1. **ensure / create volume**：确保目标 workspace 具名卷存在（新建时卷根属 root）。
2. **mkdir canonical dir + chown 10001 + verify**：用一次性 helper（root + `CAP_CHOWN`）在卷内创建规范子目录
   `users/<owner>/workspaces/<ws>`，把该子树 `chmod`/`lchown` 交给 `10001:10001`，并**回读校验**属主确为 `10001`。
3. **create instance**：按 §9.3 的加固参数创建实例容器。
4. **ready**：实例 `/health/ready` 通过（含数据根可写性探测）后才接客。

**约束**：

- 新空卷**不得**直接复用"源→目标复制" helper（它面向已有源内容；空卷上无内容可复制）。
- 初始化失败**不得**把该 workspace 标记为已初始化。**当前控制面在部分路径上"先 create 容器再
  `mark_workspace_initialized=1`"，这存在把未真正可写的卷当成已初始化的风险**，**需后续实现调整**
  （先确认属主/可写性与就绪，再落 `storage_initialized=1`）。这是**待办的实现修正**，本轮不改代码。
- **不使用 root 应用入口**：镜像保持 `USER 10001`；`chown` 只由外层 helper 完成，应用内不做 `chown`
  （无 `CAP_CHOWN`），也不以 root 启动绕过（与只读根 + `no-new-privileges` 冲突）。
- 应用侧只做"数据根可写性探测"作为就绪前置：不可写则就绪非 `ok` 并给出可操作原因，
  **不**降级写到容器可写层或 `/tmp`。

> **结论（推荐决策）**：非 root 不能凭空可写。采用**面向新建卷的**一次性 helper（root + `CAP_CHOWN`）
> 在 **`ensure/create volume → mkdir + chown 10001 + verify → create instance → ready`** 顺序中交出属主，
> 并列为 **v3 前置条件**。该 helper **当前未实现**；且需修正"create 后即 `mark_workspace_initialized`"
> 的现有顺序风险。这些均为**双方需在契约中固定**的接口点。

## 10. 安全

| 面 | 要求 |
| --- | --- |
| Markdown XSS | 渲染 Markdown 走白名单：转义 HTML，禁用 raw HTML/脚本；前端一律 `textContent`，不用 `innerHTML` 注入服务器字符串 |
| 上传路径穿越 | 保存文件名/**目录名由服务端生成 id**，不使用用户文件名/路径；media 探测见 §14.2（signature+ext+mime，不盲信） |
| Symlink | 打开/读取文档时拒绝符号链接与非常规文件；路径解析后必须仍在 `TUTORLM_DATA_DIR` 或 `TUTORLM_SESSION_DATA_DIR` 之下 |
| TeX zip 解压 | 拒绝绝对路径/`..`/symlink/hardlink/zip bomb；限制大小/数量/深度；**禁止**网络 fetch 与任意 shell/用户代码；`main` 入口必须显式指定并校验（§17） |
| 编译沙箱 | 编译**不**与 model 共享 execution environment/secrets；清 env **不是**安全沙箱；src 只读最小副本、artifact 出口校验；无安全 executor 时默认 `tex_compile_unavailable`（§16） |
| 二进制 content | `document/content` 只对文本类返回 `text`，**不**以 utf-8 伪装返回 pdf 二进制；原件/抽取**分离端点**（§14.3） |
| 长 Bearer | 不把长 Bearer 放 URL；不把 token 写入日志；原件端点用 ETag/Range/条件请求（§14.3） |
| 越权 | 业务接口只作用于 env 绑定的 workspace/session；`owner`/`workspace` 仅取自 env，绝不取自客户端 |
| 上传限制 | 文件大小/数量上限（前端 `limits.max_bytes_per_file` 对齐），超限 413；整包读取上限与网关一致 |
| 密钥 | 模型密钥仅 env；不入卷、不入库、不入镜像、不复制 `.env` |
| 客户端头 | 不信任任何客户端提供的身份头；网关已剥离外层 Cookie/`X-User-ID`/`X-User-Token`，应用也不得引入等价后门 |
| 错误信封 | 统一 JSON 错误，避免堆栈泄漏；不把内部路径/凭据回显给浏览器 |

## 11. 测试分层

| 层 | 范围 | 是否需 Docker |
| --- | --- | --- |
| unit | domain 规则、状态机、revision 并发、路径校验、配额、schemas | 否 |
| API | 路由契约、错误信封、分页、404 兜底、鉴权与越权 | 否 |
| format | md 解析偏移、pdf 抽取（text/scanned/malformed/encrypted-password）、tex zip 安全解压、媒体探测 | 否 |
| refs | `source_ref` resolve：revision/artifact/build 校验、过期/越权/已删拒绝、context budget | 否 |
| provider_fake | `fake` provider 确定性输出、schema 校验、超时/失败路径 | 否 |
| copy-contract | v3 数据子目录/卷外会话/就绪声明字段；**不实际复制卷**，只校验契约形状 | 否 |
| browser | 同源工作台流程（上传→Reader 划词→节点→解释/串讲→轮询）；Reader MD/PDF open/select/search/navigate/highlight | 否（本地实例），需浏览器 |
| docker_it | 真实 Docker 集成：卷属主、tmpfs 会话、只读根、停止信号、就绪握手 | **是，显式 opt-in** |

- 真实 Docker 测试沿用外层约定：**仅当显式环境开关（如 `CONTROL_PLANE_DOCKER_IT=1` / 应用侧等价开关）设置后运行**，
  并使用独立前缀与 label，结束后清理自己创建的资源。
- 控制面回归与本仓库边界检查（`python scripts/check-boundaries.py`、控制面测试）在任何相关改动后照常运行。
- **多格式 fixtures**（实现后）：`md`、`pdf text`、`scanned pdf`（无文本层 → `unsupported`）、
  `malformed pdf`、`encrypted pdf`（**密码不持久化**）、`tex bad imports`、`timeout`、`zipbomb`；
  以及 range/HEAD/auth、revision/ref 过期、recompile 无 source map 等断言。

## 12. 里程碑（可验收）

| 里程碑 | 交付 | 验收判据 |
| --- | --- | --- |
| M0 骨架 | `backend/` 分层与 `adapters/http/app.py` 起服务 | `GET /health` 200；`/api/{unmatched}` 404 JSON |
| M1 存储与会话 | WAL + UoW、业务库 schema、卷外会话库 | 单元测试通过；数据根可写性探测生效（不可写时 ready 非 ok） |
| M2 就绪握手 | `/health/ready` 返回 v2/v3 字段 + `storage_layout` 判定 | copy-contract 测试通过；legacy 与 content_only 区分正确 |
| M3 文档与图谱 | 文档 CRUD/上传、节点/边 CRUD、正文全文 | API 测试通过；XSS/路径穿越/符号链接用例拒绝 |
| M4 任务与 provider | RunManager + bounded executor、`fake`/`openai_compatible`、取消/幂等 | provider_fake 通过；取消后不 commit；重启 run 变 `interrupted` |
| M5 前端接入 | 工作台适配（单 workspace/单 project、分享改控制面） | browser 测试通过；无重复 `/api/shares` 实现 |
| M6 集成 | 真实 Docker opt-in 集成 + 卷属主初始化 | docker_it 显式开启后通过；非 root 属主闭环（依赖 v3 新卷 helper 前置） |
| M7 分享克隆 | 控制面注入 `SOURCE_ID`/`COPY_ID` + 契约 ADR 后的 rebind | 副本重绑通过；缺标识时 fail closed 有测试覆盖 |
| M8 多格式文档 | format 探测、不可变 revision、原件 range、content/source 分离、抽取端点 | API 测试通过；md/pdf/text/scanned-pdf/malformed/encrypted-pdf fixtures 行为正确；scanned 无文本层 → `unsupported` |
| M9 Reader 与引用 | 前端 Reader（MD/PDF 统一 open/select/search/navigate/highlight）+ `source_ref` resolve | reader 测试通过；md codepoint/UTF-16 换算、pdf canonical quote 后端校验、`revision_id+extractor_version` 过期拒绝 |
| M10 受限编译 | TeX zip 安全解压 + 编译 run（独立 executor） | 无安全 executor 时 **`tex_compile_unavailable`**；zipbomb/绝对路径/`..`/symlink/hardlink/bad imports/timeout 用例拒绝；partial/failed **保留旧成功产物**；recompile 不产生 source map 时**如实为空** |

## 13. 推荐决策

本节给出**建议结论**（设计立场），而非把开放问题原样交回；凡涉及 `contracts/` 的仍需两侧评审后冻结。

1. **身份 bootstrap —— 采用阶段 B 作为正式方向**：以"外层 admission 签发短期、强绑定
   audience/workspace/generation 的 bootstrap token + 每 instance 隔离 secret"为**推荐的下一契约 ADR**，
   正式提出并双方评审。**不以阶段 A 的 env 弱鉴权作为默认长期方案**；默认**拒绝隐式会话**（业务 Bearer 保留），
   阶段 A 仅作为**显式 opt-in 的本地受控部署过渡**（`TUTORLM_ALLOW_IMPLICIT_SESSION=true`，planned，默认 `false`），
   且**不宣称 production 安全**、**不以"拓扑可检测"作假保证**（§3.2/§3.3）。
2. **新卷属主初始化 —— 采用外层 helper（uid 10001，当前未实现）**：由控制面一次性 helper（root +
   `CAP_CHOWN`）按 `ensure/create volume → mkdir canonical dir + chown 10001 + verify → create instance → ready`
   顺序交出属主，**列为 v3 前置条件**；镜像保持非 root、不采用 root 入口脚本，并修正"create 后即
   `mark_workspace_initialized`"的顺序风险（§9.4）。
3. **克隆 rebind —— 采用确定验证算法（当前未 ready）**：分离业务 stable ID（跨克隆保留）与 workspace access
   context（克隆后重绑）；仅凭**控制面注入的迁移标识 + 匹配校验**才重绑，缺标识或 owner 冲突时 **fail closed**，
   **不**因 UUID 不同拒绝合法副本、也**不**盲写 owner。因 `TUTORLM_WORKSPACE_SOURCE_ID`/`_COPY_ID`
   **当前控制面未注入**，分享→克隆的业务端到端**当前尚未 ready**；解锁需控制面注入 + 版本化契约 ADR（§4.5）。
4. **事件通道 —— 轮询为默认，SSE 为可选后期增量**：默认保留前端轮询（约 250ms→1s）；
   `/api/runs/{id}/events` 的 SSE（`seq`/`after_seq`）作为**后期可选**能力，非必需依赖（§5）。
5. **前端适配 —— 需前端团队确认**：`POST /api/projects` / `DELETE /api/projects/{id}` 收敛为单项目，
   `share` / `redeem` 改调控制面端点（§3.4、API 契约 §9）。这是**明确的适配要求**，不是应用侧的开放设计。
6. **TeX 编译 —— 默认关闭、独立 executor**：本仓库**无**安全编译执行环境时，编译默认
   **`tex_compile_unavailable`**，**不**回退同 App subprocess（清 env 不是沙箱）。安全沙箱的创建者与生命周期
   属**部署基础设施**，不是用户控制面或 App 业务职责；同 Pod/sidecar 也必须保持 FS/proc/cap 边界。
   细则见 [`tex-compilation-security.md`](tex-compilation-security.md)。
7. **pdf 与 tex 引用 —— 保守且可验**：pdf 用**唯一** canon 坐标 `normalized-cropbox-unrotated-top-left`
   （多页 `pages[]`；不混用 pdf-user 物理点）与后端校验的 canonical quotes（不信任 PDF.js 排序 offset），
   依赖 `revision_id + extractor_version`；tex 的 `tex-source`（`file` 必填 canonical 相对路径）与
   compiled-pdf 分开，source map **可选、非必造**。**不伪造**无法提供的 text-quote 能力，降级为显式 `whole-document`。

## 14. App Runtime 基础服务（多格式）

本节定义 md/txt/pdf/tex **共用**的 App Runtime 基础服务。格式特有的解析/渲染细节放在 §15/§16 与专文。

### 14.1 不可变原件、revision 与 hash

- 每个 document 持有**不可变原件**：写入 `sources/<document_id>/<revision_id>/`（§4.1），带 `content_hash`
  （原件字节摘要）与 `revision_no`。**替换/编辑一律生成新 revision**，不就地改写旧原件。
- `documents.current_revision_id` 只指向最新 revision；旧 revision 与旧 artifacts/builds **仍可查**
  （除非被显式清理，§17）。引用（§15）绑定到**具体 revision**，不静默跟随最新。
- tex 多文件工程是**一个 revision 下的多 resources**（`main.tex` + `resources/`），
  仍是单 workspace 单 project 内的**一个 document**，不新增控制面 project 概念。

### 14.2 媒体探测（signature + extension/mime，不盲信任何单一来源）

- 上传时同时取**内容签名**（magic bytes）、**扩展名**与客户端声明的 `Content-Type`，三者交叉判定；
  **不盲信**客户端 mime 或扩展名。三者冲突或签名无法识别 → 拒绝为 `400 unsupported_media`，
  **不猜测**、**不**按扩展名强行当文本读。
- 判定结果写入 `documents.format`（`md|txt|pdf|tex`）。pdf 以 `%PDF-` 签名、tex 以工程入口与
  `resources` 形状判定；仅当签名/形状与声明一致才落库为对应 format。
- 文本类（md/txt）统一按 **UTF-8** 解码，解码失败 → `400 encoding_unsupported`，不做有损替换。

### 14.3 原始 range 读取与 content disposition

- **原件下载**与**文本抽取**是**分离端点**（API §4）：原端点按字节返回原件（含 `Content-Length`），
  抽取端点返回结构化文本；**不得**用文本端点返回 pdf 二进制，或用 utf-8 编码伪造 pdf 内容。
- 原端点支持 **Range**（`bytes=`）与 `HEAD`：返回 `206`/`Content-Range`，含 `Accept-Ranges: bytes`；
  越界 `416`。带 `ETag`（基于 `content_hash`）与 `Last-Modified`，支持条件请求（`If-None-Match` → `304`）。
- `Content-Disposition: attachment`（原件）或 `inline`（受控预览）；不把长 Bearer 放 URL，
  也不把 token 写进日志（§10/API 通用约定）。

### 14.4 source refs 与统一 content 供业务消费

- 业务（explain/narration）消费的是**引用（source ref）+ 解析后的内容**，**不**直接渲染、**不**管理文件生命周期：
统一引用模型见 §15，`resolve` 返回带 citation 的文本片段，供 workflow 组装 context。
- `resolve(source_ref)` 必须校验：ref 绑定的 **revision 仍存在且未删除**、**artifact/build 未过期**、
  请求方对当前 workspace 有权限；越权/已删/过期 revision → 拒绝（`410`/`404`/`409`），
  绝不静默换到最新 revision。context 组装按 `budget` 截断，引用保持可追溯。
- `document/content`（文本类）保持**纯文本 md 兼容**：返回 `{"text": ...}`；pdf/tex **不**用该端点返回二进制。

## 15. Reader 框架与统一引用模型（链接专文）

浏览器 Reader 提供 **MD/PDF 统一**的 `open/select/search/navigate/highlight` 与 **reference id**；
tex 用 **source editor + 编译出的 PDF** **共用**同一个 Reader。Reader 是**前端**职责（`frontend/reader`），
后端只提供原文/抽取/build 数据与 `source ref` 端点。

**统一 reference schema**（顶层字段与 [`document-readers.md`](document-readers.md) 对齐，两处**只有一套**定义）：

```text
source_ref = {
  document_id, revision_id,
  artifact_id?, build_id?,             // 仅引用编译产物（compiled-pdf）时；与 revision_id 语义不同、不可互换
  selector: text | pdf | tex-source,   // 判别联合（见下）
  quote, prefix?, suffix?,             // canonical quote + 定长上下文窗口
  extractor_version?
}
```

三种 selector 的字段（此处给**规范结论**，Reader 框架与选择事件细节在专文展开）：

- **md `text`**：`{type:'text', start, end, unit:'codepoint'}`。后端 canonical 偏移一律 **Unicode code point**；
  浏览器 DOM 侧偏移是 **UTF-16 code unit**，**持久化前必须换算为 code point**，**不得**两种单位混存；
  `unit` 字段**必须**存在，比较/拼接先归一到同一单位。
- **pdf `pdf`**：`{type:'pdf', pages:[{page, rects?|quads?}], coordinate_space:'normalized-cropbox-unrotated-top-left'}`。
  `page` **从 1 开始**，支持**多页**（每页各一组 rects/quads）。**只定义这一个 canon 坐标**：
  归一化到 **未旋转的 CropBox**、原点左上、**x 向右、y 向下**，范围 `[0,1]`；**不**同时使用 `pdf-user` 物理点。
  页面 `rotation` **只作视图变换**（渲染/回显时应用），**不改写**存储的规范坐标。
- **tex-source `tex-source`**：`{type:'tex-source', file, start, end, unit:'codepoint'}`；**`file` 必填**，
  为工程内 **canonical 相对路径**（如 `main.tex`、`sections/intro.tex`），**不**是磁盘绝对路径。
  该 selector 与 **compiled-pdf 的 `pdf` selector 分开**；两者映射为 **source map，可选**（§16），**不虚构**。
- **`whole-document`（草稿）**：当某格式/目标**暂不具备可靠 text-quote 能力**时，允许 `{type:'whole-document'}`
  作为**显式**占位；此时**不伪造**任何偏移或 quote。

校验规则：

- pdf 的 canonical quote **必须后端校验**；浏览器 PDF.js 的分页/排序与后端抽取可能不同，
  其 offset **不能直接信任**；定位以 `page + rects/quads + quote + prefix/suffix + revision_id + extractor_version` 组合，
  并承认"仅粗略回显"。
- `artifact_id`/`build_id` 与 `revision_id` **语义不同、禁止互换**：一份 revision 可有多次 build；
  引用产物 PDF 时**同时**给出 `revision_id` 与 `artifact_id`/`build_id`。**不得**在 build 变化时静默把锚点当成仍指向同一页。

> 顶层字段名与专文共用；**pdf / tex-source selector 的规范定义以本节与 API §6 为准**。
> 专文中较早草稿若出现 `page_space:'pdf-user'` 或 `coordinate_space:'normalized-0-1'` 等写法，
> 一律由本文的单一 canon（`pages[]` + `normalized-cropbox-unrotated-top-left`）取代。

## 16. 格式适配器与受限编译（链接专文）

格式适配器经 `application/format` 端口接入（§2），实现见 `infrastructure/format`：

| format | 适配器职责 | 明确不做 |
| --- | --- | --- |
| md | Markdown 解析（白名单渲染、偏移/节点锚点） | 执行脚本、原始 HTML 注入 |
| pdf | 文本抽取（分页/offset/quotes） | 伪造偏移；对扫描无文本页**编造**内容 |
| tex | 受限编译 → PDF（经 RunManager） | 网络 fetch、任意 shell/用户代码、读取卷外文件 |

- **OCR 可选**：仅当显式启用且部署具备条件时才做；扫描件无文本层时**明确 `unsupported`**，
  返回可操作原因，**不编造**文本。
- **文本能力如实标注**：当适配器/Reader 暂不具备可靠 text-quote 能力（如无文本层或未接入抽取）时，
  **不伪造**偏移或 quote，按 §15 的显式 `whole-document` 降级处理。
- 编译/抽取与模型调用**共用 RunManager**（§5），但**workload class 独立**：TeX 编译有独立 executor 与
  资源额度，**不**占用模型队列长尾。取消/超时/版本快照/幂等/stale completion **不覆盖新 version**；
  `partial`/`failed` build **保留旧的成功产物**。
- **安全与默认**：`scripts.check-boundaries`、本仓库**当前未实现**安全编译执行环境。因此**默认**
  `tex_compile_unavailable`，**不**回退到同 App subprocess（清 env **不等于**安全沙箱）。编译沙箱的
  创建者与生命周期、网络 `none`、文件隔离、低权、资源 bounded 等要求见
  [`tex-compilation-security.md`](tex-compilation-security.md)（主设计不复制）。

## 17. 上传/配额与 GC

- 上传/workspace 配额：按项目**持久**计量（文件数/字节/tex 工程 resource 数）；临时租约沿用匿名/临时策略
  （见 `contracts/instance-runtime-v1.md`）；超限 `413`/`429`。
- **项目持久 4h 由外层清理**（控制面按闲置回收，见 `architecture.md`）；**应用 session GC 不删原件**：
  session 在卷外，GC 只清会话，**绝不**删除 `sources/`、`artifacts/` 或业务库（§4.2）。
- tex zip 上传按**安全解压**处理（绝对路径/`..`/symlink/hardlink/zip bomb/大小与数量与深度上限），
  **禁止**网络 fetch 与任意 shell/用户代码；multi-file 工程必须显式指定 `main` 入口并校验存在。
  细则见 [`tex-compilation-security.md`](tex-compilation-security.md)。

## 18. 事实边界与自查声明

- 本文为**设计**并含 **foundation 现状**说明；多格式读取/编译与工作台前端接线尚未实现。未执行真实
  Docker、未构建/拉取镜像、未访问远程基础设施。
- 本地 `reference/` 只含容器外控制面快照，**无**原容器内 backend 源码/镜像，故未做"恢复/逐行验证"声明。
- 引用契约：`contracts/instance-runtime-v1/v2/v3.md`、`gateway-lease-v1/v2.md`；
  引用外层事实：`runtime-host/control-plane/control_plane/{lifecycle.py,domain/models.py,runtime/docker_adapter.py}`。
- §14–§17 为**多格式设计**：md/pdf/tex 的端到端读流程、抽取与编译**均未实现**；本仓库**无**安全编译
  executor，TeX 编译**默认 `tex_compile_unavailable`**，**不**声称任何"已支持/已安全"结论。
- 未改动 `contracts/`、`runtime-host/`、`sync/` 及既有前端模块；未提交任何内容。
