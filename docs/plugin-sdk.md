# Instance App Plugin SDK

容器内插件只使用 `backend.plugin_contracts` 的 SDK v1。Host 直接加载插件并调用工作流，
没有独立的兼容协议或适配层。插件是随镜像构建的可信 Python 代码，不支持用户上传执行，
能力句柄不是 Python 进程安全沙箱。

## 注册与配置

默认插件及依赖顺序：

| 插件 ID | 工作流 ID | 任务 kind |
| --- | --- | --- |
| `documents` | 无（文档基础服务由 Runtime 提供） | 无 |
| `explanations` | `explanation.v1`、`graph_relations.v1` | `explanation`、`graph_relations` |
| `knowledge-narration` | `narration.v1`、`retelling.v1` | `narration`、`retelling` |
| `card-clear` | 无模型工作流；提供文档卡片批量删除与撤销路由 | 无 |

环境变量 `TUTORLM_ENABLED_PLUGINS` 使用逗号分隔的插件 ID，默认值为
`documents,explanations,knowledge-narration,card-clear`。可以仅启用 `documents` 或
`documents,explanations`；未知 ID、缺失或禁用的依赖、循环依赖、重复任务 kind、
缺失工作流实现、Host API 版本不匹配和未知能力均阻止应用装配。

`GET /api/plugins` 免鉴权返回 manifest 描述、依赖顺序、工作流和面板；
`GET /api/runtime` 保留 `plugins[].workflows[].kind` 供浏览器判断能力。
这两个端点不暴露路径或模型密钥。

## 插件入口

插件模块导出 `MANIFEST: PluginManifest` 和 `build()`。插件对象提供：

- `manifest` 与 `workflows: dict[workflow_id, Workflow]`。
- `register_services(host)` 与 `register_routes(router, host)`。
- 异步 `startup(host)` 与 `shutdown(host)`。

生命周期按依赖顺序启动、逆序关闭，启动异常时执行已进入生命周期的插件的清理。
自定义路由固定挂在 `/api/plugins/{plugin_id}/...`，统一要求应用 Bearer session。
生命周期上下文只有插件元数据，没有当前用户和工作区能力句柄。

## 工作流与模型

```python
from backend.plugin_contracts import Draft, WorkflowInput

class ExplanationWorkflow:
    id = 'explanation.v1'
    kind = 'explanation'

    def run(self, context, data: WorkflowInput) -> Draft:
        context.guard()
        entity = context.container.explanations.explain(
            node_id=data.get('node_id'), question=data.get('question'), persist=False,
        )
        context.guard()
        return Draft(entity=entity, result=entity.to_dict(),
                     provider=entity.provider, model=entity.model,
                     artifact_type='explanation')
```

Host 校验有界 JSON 输入、manifest 声明的 schema 与输出 Draft。
schema 支持类型、字段、必填项、未知字段限制、数组、枚举、长度与数值范围；
不支持的 schema 关键词在注册时拒绝，不默默忽略。

每次调用获得限定到当前工作区的 `ScopedContext`：`run`、`cancel`、`valid()`、
`guard()`、`emit()` 与 `container`。按 manifest 能力提供项目、节点、解释、
学习信息和模型服务；不提供公共原始 SQL 连接、存储根路径或凭证。
工作流中的学习信息只读。文档原件、抽取和编译仍由 Runtime 文档服务处理，
声明能力不等于每个能力都有可调用句柄，未提供的句柄明确拒绝访问。

模型实现直接遵守 `application.ports.model_provider.ModelProvider`：

- `capabilities: ProviderCapabilities`。
- `explain(ExplanationRequest) -> str`。
- `narrate(NarrationRequest) -> list[str]`，每个输入节点对应一段。
- `retell(RetellRequest) -> str`，结构化 JSON 由业务工作流验证。

底层 HTTP 模型传输属于 infrastructure，不是另一套插件协议。

### 解释风格

`explanation.v1` 可选输入 `preferences` 为
`{"style":"rigorous","length":"medium","language":"zh"}`。
`style` 支持 `auto`、`plain`、`rigorous`、`example`、`analogy`、`socratic`；
`length` 支持 `short`、`medium`、`long`；`language` 支持 `auto`、`zh`、`en`。
未提供时使用默认风格、中等篇幅与中文默认语言。未知枚举或字段拒绝执行。

- 通俗与类比：L0 用两句定位与定义，L1-L2 简明说明机制，L3+ 使用日常直觉；
  类比模式额外说明映射与边界。仅 L1-L2 允许首次诊断问题。
- 默认：按实际问题与上下文确定深浅，不强制套用分层句数或类比。
- 严谨：明确上下文、前提与结论的逻辑关系，逐步补全被省略的数学推导并列出依据、
  符号与适用条件；缺少前提时明确指出，不虚构证明。深度及简短偏好不省略必要步骤。
- 多举例：提供具体例子的输入、中间过程、结果及适用边界。
- 引导式：以针对性问题与提示引导推理；展开或明确索要答案时给出推理与结论。

偏好、实际深度和服务端生成的策略随解释保存，重试不改变所选策略。
风格约束属于模型指令，不保证模型输出已通过数学正确性验证。
前端依据工作流 schema 发送结构化偏好，旧后端回退到问题末尾的兼容提示。

## 提交与并发

工作流返回 `Draft`，不得直接持久化生成结果。`Draft.precommit` 可补充业务快照检查。
Host 在同一数据库事务中重新检查 session、取消状态、已读取节点的 revision 和
插件的 precommit，再保存领域实体并将任务标为完成；任何一步失败都会回滚实体。
发布知识实体必须声明 `knowledge.write`。

解释与串讲保持现有 HTTP 结果信封 `result.explanation` / `result.narration`。
回讲额外返回 `status`、纳入/阻塞的子节点 ID 和 `reached_root`，并保存学习元数据。
`context.emit()` 记录有界 JSON 的单调序号事件；当前 HTTP 仍通过轮询获取任务状态，
`GET /api/runs/{id}/events` 返回 501，不声称已提供 SSE。

## 学习数据接口

以下接口均需应用 Bearer session，并限定在当前工作区：

| 方法 | 路径 | 写入请求 |
| --- | --- | --- |
| GET / PUT | `/api/learning/profile` | `{"expected_revision":0,"profile":{"education_stage":"undergraduate"}}` |
| GET / PUT | `/api/nodes/{node_id}/learning` | `{"expected_revision":0,"understanding":"resolved","note":""}` |

初始 revision 为 0，写入后递增；陈旧 revision 返回 409。
回讲前必须设置学习资料、解释父节点并至少解释一个直接子节点；
没有可用子节点解释时返回 blocked，有部分或全部解释时分别返回 partial / complete。
生成期间学习资料、树结构或解释快照发生变化时拒绝提交。

MD/PDF/TEX 的存储与 Reader 接口不因本 SDK 调整而改变；
TEX 仍要求独立受限执行器，无安全执行器时编译返回 503。
