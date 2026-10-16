# Instance App API（契约草案，**非已实现**）

> 状态：**当前契约草案，未实现**。本文件描述容器内应用**计划**提供的 HTTP 接口形状，
> 用于前端与后端对齐；不是对已交付能力的声明。字段取自同步工作台前端的真实调用与实体，
> 路径/信封在实现前可调整，破坏性变化须经两侧评审。
> 依据：`instance-app-design.md`、`contracts/instance-runtime-v1/v2/v3.md`、`gateway-lease-v1/v2.md`。
> 本轮扩展 **md/txt/pdf/tex（含 tex 多文件工程）** 的文档、原件、抽取/编译与统一引用端点（§4a–§4e）。
> Reader 与统一 `source_ref` 字段定义**以 [`document-readers.md`](document-readers.md) 为单一真值**，
> 本文件只列端点形状、不重复定义 selector 细节；TeX 安全见 [`tex-compilation-security.md`](tex-compilation-security.md)。

## 通用约定

- 除 `GET /api/runtime`、`GET /health*`、`POST /api/sessions` 外，均需 `Authorization: Bearer <app session token>`。
- 错误信封：`{"error":{"code":string,"message":string,"details"?:object}}`；限流/满载返回 `429/503` + `Retry-After`。
- 业务接口只作用于**当前 env 绑定的 workspace**；不接受客户端提交的 `owner_id`/`workspace_id`。
- 未匹配的 `/api/*` 返回 `404` JSON，用于区分"API 不存在"与前端路由。
- 长 Bearer **不进 URL、不写日志**；原件端点用 `ETag`/`Range`/条件请求（§4c）。

## 1. 运行时发现

`GET /api/runtime`（免鉴权）

```json
{
  "mode": "managed",
  "provider": {"name": "fake", "model": "offline-demo"},
  "plugins": [
    {"name": "explanation", "enabled": true, "workflows": [{"kind": "explanation"}]},
    {"name": "narration", "enabled": true, "workflows": [{"kind": "narration"}]}
  ],
  "limits": {"max_documents_per_session": 50, "max_bytes_per_file": 2097152, "max_runs_per_session": 20}
}
```

前端据 `plugins[].workflows[].kind` 决定是否启用解释/串讲按钮；据 `provider.name` 标识演示模型。

## 2. 会话

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| POST | `/api/sessions` | `{"invite_code"?:string}` | `201 {"access_token":string,"session":{"id","workspace_id","expires_at"}}`；managed 持久模式不强制 `invite_code`（见设计 §3.2） |
| GET | `/api/sessions/me` | — | `{"session":{...},"limits":{...}}` |
| DELETE | `/api/sessions/me` | — | `202`（仅表示已调度清理，不代表数据已删除） |

状态码：无/失效 token → `401`；过期 → `410`；缺失邀请码（demo 模式）→ `401 code=invite_required`。

**隐式会话（planned，默认拒绝）**：`POST /api/sessions` 仅在显式启用应用配置
`TUTORLM_ALLOW_IMPLICIT_SESSION=true`（**planned，默认 `false`**）、且处于持久模式并存在绑定的
workspace env、部署标注为 local 验证时，才允许在无凭据下签发绑定当前 workspace 的会话（设计 §3.2）。
**默认（未开启）= 拒绝**：不接受客户端提交的 owner/workspace 冒充身份，业务 Bearer 保留。
该配置当前**未实现**。

## 3. 项目（每 workspace 单项目）

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| GET | `/api/projects` | — | `{"items":[Project]}`（仅当前项目） |
| GET | `/api/projects/{id}` | — | `Project` |
| PATCH | `/api/projects/{id}` | `{"title"?:string,"description"?:string}` | `Project` |
| DELETE | `/api/projects/{id}` | — | `204`（删除当前项目业务内容；会话在卷外独立） |

```ts
Project = { id, title, description?, workspace_id, created_at, updated_at }
```

> 说明：前端历史上可创建/列举多个项目；单 workspace 模型下适配为**只操作当前项目**，不做跨卷多项目管理。

## 4. 文档（多格式）

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| GET | `/api/projects/{id}/documents` | — | `{"items":[Document]}` |
| POST | `/api/projects/{id}/documents` | `{"title":string,"text":string,"derive_nodes":false}`（**纯文本类**，md/txt） | `{"document":Document}` |
| POST | `/api/projects/{id}/documents/upload` | `multipart/form-data`：`file`（md/txt/pdf/tex 或 **tex zip**）、`derive_nodes=false`、tex 工程 `main`（入口，§4e） | `{"document":Document,"revision":Revision}` |
| GET | `/api/documents/{id}` | — | `{"document":Document}` |
| PATCH | `/api/documents/{id}` | `{"title"?}`（元数据） | `{"document":Document}` |
| POST | `/api/documents/{id}/revisions` | `multipart/form-data`：`file`（**新增不可变 revision**） | `{"document":Document,"revision":Revision}` |
| GET | `/api/documents/{id}/revisions` | — | `{"items":[Revision]}` |
| GET | `/api/documents/{id}/content` | — | `{"text":string}`（**纯文本类全文**；**不**返回 pdf 二进制，§4b） |
| GET | `/api/documents/{id}/source` | —（`Range`/`HEAD`） | 原件字节流（§4c） |
| DELETE | `/api/documents/{id}` | — | `204` |

```ts
Document = { id, project_id, title, source_filename?, format: "md"|"txt"|"pdf"|"tex",
             status: "pending"|"ready"|"failed"|"unsupported",
             current_revision_id, size_bytes, order_index?, created_at, updated_at }
Revision = { id, document_id, revision_no, content_hash, size_bytes,
             status: "ready"|"failed"|"unsupported", created_at }
```

- `format` 由**服务端探测**（signature+extension/mime，设计 §14.2），**不**盲信客户端声明。
- `status` 值域为**草案枚举**：`pending|ready|failed|unsupported`（**待实现冻结**）。pdf/tex 就绪语义以
  抽取/build 为准；无文本层扫描 pdf（§4d）为 `unsupported`，不编造内容。与设计 §4.3 schema 行一致。
- `derive_nodes` 默认 `false`：**不默认生成节点**，节点由人工划词锚定产生。
- **每次上传/替换产生新 revision**（`POST /api/documents/{id}/revisions`）；原件**不可变**，
  `current_revision_id` 指向最新；旧 revision 与 artifacts/builds 仍可查。文件与目录名由服务端生成，
  拒绝穿越、符号链接与非常规文件。

### 4a. 上传/媒体探测与限制

- 单文件上限 `limits.max_bytes_per_file`（超限 `413`）；tex 工程按 §4e 的 resource 数与展开上限。
- 探测冲突（签名 vs 扩展 vs mime）或不可识别 → `400 unsupported_media`；文本类 **UTF-8** 解码失败 → `400 encoding_unsupported`。
- 加密 pdf（有密码）→ `409 pdf_encrypted`；**密码不持久化、不写库、不写日志**（请求内使用后即丢弃）。
- malformed pdf → `422 pdf_malformed`，落 `status=failed`，不生成抽取文本。

### 4b. 文本抽取（md/txt/pdf/tex）

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| POST | `/api/documents/{id}/extract` | `{"revision_id"?:string,"force"?:false}` + 头 `Idempotency-Key` | `202 Run`（`workload:"extract"`） |
| GET | `/api/documents/{id}/extractions` | `?revision_id=&extractor_version=` | `{"items":[Extraction]}` |
| GET | `/api/documents/{id}/text` | `?revision_id=` | `{"revision_id","extractor_version","pages"?:[...]}`（**结构化文本**，非二进制） |

```ts
Extraction = { id, revision_id, extractor_version, status: "pending"|"ready"|"failed"|"unsupported",
               text_hash?, page_count?, error_code?, created_at }
```

- pdf 抽取返回**分页**文本（每页 `page`、文本与规范化偏移）；`extractor_version` 变化 **即失效**，
  引用校验依赖 `revision_id + extractor_version`（设计 §15）。
- 抽取是 **run**（`workload:"extract"`，独立 executor，设计 §5/§16）；取消/超时**不改写**已就绪 revision。
- 无文本层扫描 pdf → `status="unsupported"`（`code=no_text_layer`），**不**用 OCR 编造；OCR 为**可选**、默认关闭。

### 4c. 原件下载（range/HEAD/条件请求）

`GET /api/documents/{id}/source?revision_id=`（省略 `revision_id` 用 `current_revision_id`）：

- 返回原件字节（md/txt/pdf/tex 原文件或其 zip）；`Accept-Ranges: bytes`、`Content-Length`、
  `ETag`（由 `content_hash` 派生）、`Last-Modified`。
- `Range: bytes=...` → `206` + `Content-Range`；越界 `416`；`If-None-Match` 命中 → `304`。
- `HEAD` 仅返回头部（不含 body）。
- `Content-Disposition: attachment`（默认）或 `inline`（受控预览）；**不**返回 JSON 信封包装二进制。
- 与 `/text`、`/content` **分离**：原件端点**不**做抽取，抽取端点**不**返回二进制。

### 4d. 编译与 build（tex）

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| POST | `/api/documents/{id}/compile` | `{"revision_id":string,"main":string}` + 头 `Idempotency-Key` | `202 Run`（`workload:"tex_compile"`）；**无安全 executor 时** `503 code=tex_compile_unavailable`（**不**入队、**不** fallback） |
| GET | `/api/documents/{id}/builds` | `?revision_id=` | `{"items":[Build]}` |
| GET | `/api/documents/{id}/builds/{build_id}` | — | `Build` |

```ts
Build = { id, revision_id, status: "queued"|"running"|"completed"|"failed"|"cancelled"|"interrupted",
          artifact_id?, engine_version?, source_map?: object | null,   // 可选：取决于真实 compiler
          error?: { code, message }, created_at, updated_at }
```

- **`tex_compile_unavailable` 为默认**：本仓库未实现安全编译沙箱，编译端点直接 `503`（**不入队**、**不**回退同 App subprocess，
  **不**做 unsafe fallback）；细则见 [`tex-compilation-security.md`](tex-compilation-security.md) §6。
- `source_map` **可选**，仅在真实 compiler 提供时填充；**不虚构** TeX-source → PDF 位置映射（设计 §15/§16）。
- `partial`/`failed` build **保留旧的成功 artifacts**；重编译**不覆盖**已有成功 build；新 build 独立 `build_id`。

### 4e. tex 多文件工程入口

- **upload** 接受 `.tex`（单文件）或 **`.zip`**（工程包）。zip 走**安全解压**（绝对路径/`..`/symlink/hardlink/
  zip bomb/大小与数量与深度上限；细则见 [`tex-compilation-security.md`](tex-compilation-security.md)）。
- 必须显式指定 `main`（工程入口，如 `main.tex`）并**校验存在**；缺失/歧义 → `422 tex_main_invalid`。
- 工程 resources（其它 `.tex`、`.sty`、`.bib`、本地图片等）登记为 **`document_resources`**（设计 §4.3），
  仍是**同一个 document 的一个 revision**；**不新增控制面 project**，不引入新外层概念。
- 禁止网络 fetch、任意 shell 与用户代码；本地图片只从工程内相对路径解析，越界即拒绝。

## 5. 节点与图谱

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| POST | `/api/projects/{id}/nodes` | `{"document_id","title","kind","content","parent_id"?,"metadata":{...}}` | `Node` |
| PATCH | `/api/nodes/{id}` | `{"title"?,"kind"?,"content"?,"metadata"?,"revision"?}` | `Node` |
| DELETE | `/api/nodes/{id}` | — | `204`（子节点提升为顶层，不级联删子树） |
| GET | `/api/projects/{id}/graph` | — | `{"nodes":[Node],"edges":[Edge]}` |
| POST | `/api/projects/{id}/graph/edges` | `{"source_node_id","target_node_id","relation","metadata"?}` | `Edge` |
| DELETE | `/api/projects/{id}/graph/edges/{edge_id}` | — | `204` |

```ts
Node = { id, project_id, document_id, title, kind, summary?, content?, order_index?, parent_id?,
         metadata: { source?: SourceRef | { fromNode }, status: "learning"|"mastered", position?: {x,y} },
         revision, created_at, updated_at }
Edge = { id, project_id, source_node_id, target_node_id, relation, metadata?, created_at }
```

- `kind` ∈ `section|concept|example|exercise|summary`；`relation` ∈ `parent_of|explains|supports|depends_on|prerequisite_of|contradicts|example_of|summarizes|derived_from|references|related_to`。
- `metadata.source` 保存划词锚点：**统一用 `SourceRef`**（见 §6，顶层字段与 [`document-readers.md`](document-readers.md) 共用）
  或 `{ fromNode }` 表示由某节点派生；**不**再使用平铺的 `quote/block/label`。`status` 为学习状态；`position` 为画布坐标。
- 并发：`PATCH` 携带 `revision`，不匹配返回 `409`。

## 6. 统一引用（source ref）

节点锚点、解释/串讲引用与用户划词共享**同一** `SourceRef`。**顶层字段名**与
[`document-readers.md`](document-readers.md) 共用；**selector 的规范定义以本节为准**（该专文中较早草稿的
`pdf-user`/`normalized-0-1` 写法一律被本文的单一 canon 取代），**同一字段不出现两套**。

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| POST | `/api/sources/resolve` | `{"refs":[SourceRef], "budget"?:{max_chars?,max_tokens?}}` | `{"items":[ResolvedRef]}` |

```ts
SourceRef = {
  document_id, revision_id,
  artifact_id?, build_id?,        // 仅引用编译产物（compiled-pdf）时；与 revision_id 语义不同、不可互换
  selector: Selector,             // 判别联合，见下
  quote, prefix?, suffix?,        // canonical quote + 定长上下文窗口
  extractor_version?
}
Selector =
  | { type: 'text',        start, end, unit: 'codepoint' }               // md：codepoint（DOM utf16 需先换算）
  | { type: 'pdf',         pages: [{ page, rects? | quads? }],           // 1-based；多页
                           coordinate_space: 'normalized-cropbox-unrotated-top-left' }
  | { type: 'tex-source',  file, start, end, unit: 'codepoint' }         // file 必填：canonical 相对路径
  | { type: 'whole-document' }                                           // 草稿：无可靠 text-quote 能力时的显式占位
ResolvedRef = { ref: SourceRef, text: string, citation: { document_id, revision_id, page?, file?, span? },
                resolved: boolean, reason?: "rev_gone" | "artifact_stale" | "unauthorized" | "expired" }
```

- 顶层字段与 [`document-readers.md`](document-readers.md) **对齐、只有一套**；selector 的**规范结论**：
  md 持久化一律 **codepoint**（**不**混存 utf16）；pdf **只定义** `normalized-cropbox-unrotated-top-left`
  这一个 canon 坐标（未旋转 CropBox、原点左上、x 右 y 下、`[0,1]`；**不**同时用 pdf-user 物理点，
  `rotation` 仅视图变换、不改写存储坐标）；tex-source 的 **`file` 必填**为工程内 canonical 相对路径，
  与 compiled-pdf 分开、source map **可选非必造**。
- **校验（不静默漂移）**：`resolve` 必须验证 ref 绑定的 `revision_id` **仍存在且未删除**、
  `artifact_id`/`build_id` **未过期**、`extractor_version` **未失效**、请求方**有权限**；
  越权/已删/过期 → `410`/`404`/`409`（或逐条 `resolved:false` + `reason`），**绝不**改绑定到最新 revision。
- pdf 的 PDF.js 排序 offset **不能直接信任**：抽取端返回的 canonical quote 由后端校验，`resolve` 以
  `revision_id + extractor_version` 为准。
- 业务（explain/narration）消费 `ResolvedRef` 组装 context，按 `budget` 截断；**不**渲染、**不**管理文件生命周期。

## 7. 解释与串讲（历史）

| 方法 | 路径 | 响应 |
| --- | --- | --- |
| GET | `/api/projects/{id}/explanations` | `{"items":[Explanation]}` |
| GET | `/api/projects/{id}/narrations` | `{"items":[Narration]}` |

```ts
Explanation = { node_id, question?, answer, provider, model, created_at }
NarrationSegment = { node_id, title, text, duration_seconds }
Narration = { id, title, segments: [NarrationSegment],
              segment_count,               // == segments.length（服务端派生，非独立真值）
              total_duration_seconds,      // == Σ segments[].duration_seconds（服务端派生）
              provider, model, created_at }
```

- `segment_count` 与 `total_duration_seconds` 为**服务端派生**：前者等于 `segments.length`，后者等于
  各段 `duration_seconds` 之和。两者与 `segments` 必须一致，前端不得自行重算或覆盖。
- 串讲段序即 `segments` 数组顺序（按用户选择的 `node_ids` 讲述顺序），每段绑定一个 `node_id`。

## 8. 任务（runs）

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| POST | `/api/runs` | `{"kind":"explanation"|"narration","input":object}` + 头 `Idempotency-Key` | `202 Run` |
| GET | `/api/runs` | — | `{"items":[Run]}`（可用于重载后续跑） |
| GET | `/api/runs/{id}` | — | `Run` |
| POST | `/api/runs/{id}/cancel` | — | `202`（取消后不 commit；结果丢弃） |
| GET | `/api/runs/{id}/events` | — | **可选** SSE：事件带单调 `seq`，支持 `after_seq`/`Last-Event-ID` 重放 |

```ts
Run = { id, kind: "explanation"|"narration",
        state: "queued"|"running"|"completed"|"failed"|"cancelled"|"interrupted",
        progress?: number, input?: object,
        result?: { explanation: Explanation }   // kind == "explanation"
               | { narration: Narration },      // kind == "narration"
        error?: { code, message }, created_at, updated_at }
```

- `POST /api/runs` 入参：
  - `explanation` → `{node_id, question?}`；结果 `result = {explanation: Explanation}`。
  - `narration` → `{project_id, node_ids: [...], title?}`（`node_ids` 上限 10）；
    结果 `result = {narration: Narration}`（含 `segments` 及派生 `segment_count`/`total_duration_seconds`）。
- 终态：`completed|failed|cancelled|interrupted`；仅 `completed` 时 `result` 存在。
  **前端当前用轮询**（`GET /api/runs/{id}`，间隔约 250ms 起步、自适应放大到约 1s）直到进入终态；
  SSE 为**可选后期增量**，非必需依赖。
- 幂等键（头 `Idempotency-Key`）作用域为 workspace 身份；同 key 不同输入 → `409`。
- 容器 hardstop/抢占后进行中 run 置 `interrupted`，**不重放付费调用**。

## 9. 健康

| 方法 | 路径 | 响应 |
| --- | --- | --- |
| GET | `/health` | `{"status":"ok"}`（进程存活） |
| GET | `/health/ready` | 非持久：`{"status":"ok","mode":string}`；持久须含 `{"storage_mode":"persistent","workspace_id":"<UUID>"["storage_layout":"content_only"]}` |

持久就绪字段与控制面校验见设计 §8；未声明 `content_only` 时不返回该字段（按 `legacy_root` 处理）。

## 10. 与前端既有调用的差异（必须适配）

| 前端现有调用 | 归属变化 | 目标 |
| --- | --- | --- |
| `POST /api/projects` / `DELETE /api/projects/{id}` | 应用内不再做多项目 CRUD | 单 workspace 单项目，操作当前项目 |
| `POST /api/projects/{id}/share` | 属**控制面** | 改调 `POST /control/workspaces/{uuid}/invitations` |
| `POST /api/shares/{code}/redeem` | 属**控制面** | 改调 `POST /control/workspace-imports` |
| demo 下强制体验码 | managed 持久模式**默认仍要求凭据** | 默认拒绝隐式会话；仅当 `TUTORLM_ALLOW_IMPLICIT_SESSION=true`（planned）时过渡 |

> 应用**不**实现 `/api/projects/{id}/share` 或 `/api/shares/{code}/redeem`，以免触碰 Docker 或外层数据库。
> **克隆导入（业务端到端）当前尚未 ready**：容器内 rebind 依赖控制面注入
> `TUTORLM_WORKSPACE_SOURCE_ID` / `TUTORLM_WORKSPACE_COPY_ID`（**planned，当前未注入**），
> 缺标识时副本 **fail closed**（设计 §4.5）；解锁需控制面注入 + 版本化契约 ADR。这是**实施前置**，
> 不是当前功能。
