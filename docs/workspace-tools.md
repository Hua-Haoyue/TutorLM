# 工作区模型、代码与 Chat

本文描述当前容器内应用已实现的接口，不改变控制面的租约或实例契约。
下列业务接口都要求 `Authorization: Bearer <app session token>`，仅操作当前工作区。

## 模型偏好

`GET /api/runtime` 返回 `model_features` 能力信息和 `workspace_agent` 开关。
支持的工作流 schema 声明 `model` 参数；解释使用
`input.preferences.model`，Chat、关系梳理、串讲、回讲和 Feynman 使用
`input.model`，结构为 `{"effort":"auto|low|medium|high","web_search":false}`。
省略时使用自动思考强度、关闭联网，不改变旧客户端的请求行为。

可配置强度支持 Gemini 2.5/3、OpenAI 兼容接口的 o1/o3/o4/GPT-5 系列；
原生搜索支持 Gemini 和名称含 search 的兼容模型，最终取决于网关能力。
不支持的显式能力请求返回错误，不静默伪装成功。Gemini 搜索先产生带来源的
背景资料，再传入结构化生成；兼容接口使用原生 `web_search_options`。
思考强度会提高 token 预算，可能增加费用和延迟；不会公开私有思维链。
离线 fake provider 只用于功能测试，不执行真实搜索或模型调用。

## 代码工程

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/code/files` | 文件、语言、版本及静态符号列表 |
| GET | `/api/code/search?query=...` | 跨文件路径、版本、行号和片段 |
| GET | `/api/code/file?path=src/main.py&start_line=1&end_line=200` | 有界读取 |
| PUT | `/api/code/file` | `{path,text,expected_revision}`，创建或版本化保存 |
| POST | `/api/code/import` | multipart `file`，ZIP 或单文件，单文件可附 `path` |

普通文档上传接口也识别代码扩展名。代码文档仍使用现有 TXT 存储和不可变版本，
额外暴露 `code_path`。路径是虚拟项目相对路径，不是主机文件系统路径。
Python 使用 AST 提取函数与类，其他语言使用轻量定义匹配，配合文本检索；
不执行语言服务器、构建或用户代码。索引按文档版本更新并清理已删除文件。

代码解释默认检索整个当前项目；PDF、普通文档默认关闭。
`input.preferences.code_context` 可显式传布尔值覆盖默认行为。
解释的学习数据保留 `code_citations`；联网解释保留 `web_sources`。

安全限制：UTF-8 文本；拒绝绝对路径、遍历、隐藏路径、凭据文件、依赖目录、
符号链接和加密 ZIP。工程最多 200 文件、总计 16 MiB；单次 ZIP 最多
100 文件/400 项，总展开大小 16 MiB，同时受应用单文件和文档配额限制。
归档中出现非法路径、重复路径或已有文件时整批失败，不覆盖已有内容。
读取最多 401 行/24,000 字符，搜索默认返回最多 12 条结果。
整体替换已有节点引用的源码被拒绝，避免破坏来源定位。

## Chat 与审批

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/chat/threads` | 创建当前 app session 的对话 |
| GET | `/api/chat/threads/{id}` | 消息、工具调用状态和修改建议 |
| POST | `/api/runs` | `kind:workspace_chat`，`input:{thread_id,message,model?}` |
| POST | `/api/chat/proposals/{id}` | `{approve:true|false}`，确认或拒绝 |

Chat 使用现有任务排队、取消和 session 配额。对话、建议、Chat 任务的读取与取消
按 app session 隔离，而项目文件和节点继续遵循当前工作区原有访问边界。
刷新或关闭面板不会自动取消任务；停止按钮使用任务取消接口。

助手最多进行八轮模型/工具步骤，工具结果有上下文预算。只允许列出文件、
搜索代码、读取文件或文档、读取节点、按偏好联网搜索。不提供执行代码、
Shell、子进程、SQL、依赖安装、任意 URL 或任意主机路径工具。
导入源码、文档和网页作为不可信资料传入，不作为工具权限依据。

一次回复最多提出五项源码文件、Markdown/TXT 文档、节点或解释修改。`read_nodes`
返回节点及当前解释摘要，传入 `node_id` 可读取更长的单节点内容。
解释编辑新增历史版本，不覆盖原文内容，批准时同时校验节点版本和最新解释 ID。
建议默认不写入；
批准后在事务中重新校验版本并整批应用，冲突时拒绝，重复批准不重复写入。
已有来源节点的文件/文档不得整体替换；PDF/TeX 原件不接受 Chat 改写。
取消或过期任务不发布消息和建议。前端显示工具状态而非模型的私有思维过程。
