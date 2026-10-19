<p align="center">
  <img src="docs/assets/banner.jpg" alt="TutorLM" width="680"/>
</p>

<p align="center">
  <b>读完，不等于读懂。</b><br/>
  面向长文档学习的可追溯工作台：在原文旁圈画提问、层层追问，把读懂的内容长成一张能回到出处的知识图。
</p>

<p align="center">
  <a href="http://stellaron.link:47823/"><img src="https://img.shields.io/badge/%E5%9C%A8%E7%BA%BF%E4%BD%93%E9%AA%8C-stellaron.link-1d1d1f?style=for-the-badge" alt="在线体验"/></a>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License: MIT"/></a>
  <a href="https://github.com/namenotfound64/TutorLM/actions/workflows/instance-app.yml"><img src="https://github.com/namenotfound64/TutorLM/actions/workflows/instance-app.yml/badge.svg" alt="Instance App CI"/></a>
  <a href="https://github.com/namenotfound64/TutorLM/actions/workflows/runtime-host.yml"><img src="https://github.com/namenotfound64/TutorLM/actions/workflows/runtime-host.yml/badge.svg" alt="Runtime Host CI"/></a>
  <br/>
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/FastAPI-backend-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/Node.js-22+-339933?style=flat-square&logo=node.js&logoColor=white" alt="Node.js 22+"/>
  <img src="https://img.shields.io/badge/Gemini-%C2%B7%20OpenAI%20compatible-4285F4?style=flat-square&logo=googlegemini&logoColor=white" alt="Gemini · OpenAI compatible"/>
  <img src="https://img.shields.io/badge/Docker-multi--instance-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker"/>
</p>

<p align="center">
  <a href="http://stellaron.link:47823/">在线工作台</a> · <a href="#-快速开始">快速开始</a> · <a href="#-功能">功能</a> · <a href="#-多格式阅读">文档格式</a> · <a href="#-接入真实模型">模型接入</a> · <a href="#-测试与质量检查">开发测试</a> · <a href="#-文档入口">文档入口</a>· <a href="#-路演演示">路演演示</a>
</p>

## 🗞️ 最近更新

- **2026-10-18**：解释在卡片里**流式**显示；串讲改为由浅入深的长篇讲解，面板可随时重新打开、放大阅读；开启联网后由模型自己决定是否搜索，解释附带**参考资料**链接。
- **2026-10-18**：自动布局按原文顺序排列，前置概念放在右侧；新卡片自动建立**语义连线**；工作区支持右键重命名和删除。
- **2026-10-18**：编辑器式文档**标签页**和 Code 视图；工作区 Chat 支持多对话、联网检索并列出参考网页；生成期间拖动、翻页卡片不再打断生成。

## 📖 概览

**TutorLM** 让你在一篇难懂的长文旁边，把每一个不懂的地方圈出来问清楚。每个被圈画的概念都会变成一张保存了原文出处的知识卡片，在解释里还能继续圈画、继续追问，最后用一键串讲把所有概念连成一条完整的逻辑链，再用费曼式判断题检验是否真的读懂。

https://github.com/user-attachments/assets/6813d775-f34b-4f83-adbd-158cc1ecb264

### 亮点

- **圈画即提问**：在 Markdown、PDF、TeX 或代码里选中一段文字，解释就从这里开始，卡片随时能回到原文位置。
- **层层追问**：在解释里继续圈画，追问接在父卡片右侧，形成一棵可追溯的理解树。
- **边想边写**：解释在卡片里流式生成，可以选择通俗、严谨、举例、类比、引导式等讲法，每次都先讲清"是什么"。
- **知识自动成图**：自动布局按原文顺序排列卡片，前置概念放在右侧；AI 自动补充语义连线。
- **从基础讲到全局**：一键串讲由浅入深地讲清所有概念及其关系；"优化解释"把子卡片的新理解融回父卡片。
- **费曼检验**：针对逻辑漏洞的判断题，答对才算真的懂。
- **工作区 Chat**：读取当前项目的文档、代码和卡片回答问题，可联网并给出参考来源；任何修改都要你确认。

## ✨ 功能

<table>
<tr>
<td width="50%" valign="top">

**✍️ 圈画，即提问**

在原文中选中不懂的地方，选择讲法后生成解释。节点保存原文引用、文档版本和上下文，随时能定位回原文。

<img src="docs/assets/feature-select.jpg" width="100%" alt="圈画提问"/>

</td>
<td width="50%" valign="top">

**🗂️ 每个概念，都是一张卡片**

自动布局按原文顺序排列根节点，前置概念放在右侧，跨层连线绕开卡片；新卡片会自动补充"解释 / 依赖"语义连线。

<img src="docs/assets/feature-cards.jpg" width="100%" alt="知识卡片与自动布局"/>

</td>
</tr>
<tr>
<td width="50%" valign="top">

**⚡ 解释，边想边写**

Gemini 的输出流式写进卡片，Markdown 和公式边写边渲染；开启联网后，模型用到的网页会作为参考资料附在解释下方。

<img src="docs/assets/feature-stream.jpg" width="100%" alt="流式解释"/>

</td>
<td width="50%" valign="top">

**🌳 层层追问，直到读懂**

在解释里继续圈画，追问接在父卡片右侧；翻页切换解释版本，"优化解释"只把新理解合并进相关语句。

<img src="docs/assets/feature-followup.jpg" width="100%" alt="层层追问"/>

</td>
</tr>
<tr>
<td width="50%" valign="top">

**✅ 费曼检验**

针对推理漏洞出一道判断题：答错会解释误区并换题，答对才标记为通过。检查结果不会混进解释。

<img src="docs/assets/feature-feynman.jpg" width="100%" alt="费曼检验"/>

</td>
<td width="50%" valign="top">

**🧵 从基础，讲到全局**

一键串讲先讲清每个根概念是什么，再讲它们的因果与递进，最后连成整体机制。后台生成，面板随时可重新打开并放大阅读。

<img src="docs/assets/feature-narration.jpg" width="100%" alt="串讲"/>

</td>
</tr>
<tr>
<td width="50%" valign="top">

**💬 问它，它会去查证**

工作区 Chat 读取项目里的文档、代码和卡片回答问题，支持多对话；联网时列出可点击的参考网页。提出的修改必须由你确认。

<img src="docs/assets/feature-chat.jpg" width="100%" alt="工作区 Chat"/>

</td>
<td width="50%" valign="top">

**🧑‍💻 公式、代码，都读得懂**

KaTeX 公式、编辑器式代码视图（行号、高亮、补全与缩进）、PDF 原页渲染，用标签页在多个文件之间切换。

<img src="docs/assets/feature-code.jpg" width="100%" alt="代码与公式"/>

</td>
</tr>
</table>

### 📄 多格式阅读

| 格式 | 支持内容 | 边界 |
| --- | --- | --- |
| Markdown / TXT | 上传、版本、搜索、选区、来源定位、表格、列表、代码块、引用和 KaTeX 数学公式；按块原地编辑（回车新段落、方向键在块间移动） | 原始 HTML 不执行，远程图片不加载 |
| PDF | 原件保存、文本抽取、PDF.js 原页渲染、文本层、搜索、选区和高亮 | 扫描 PDF 没有 OCR |
| TeX | 单文件和 ZIP 工程校验、源码 Reader、源码引用 | 安全编译执行器尚未交付，编译返回 503 |
| 代码 | Code 高亮视图、编辑保存、Tab 标识符补全与缩进、ZIP 多文件导入、版本化静态索引和项目内搜索 | 仅 UTF-8 文本，不执行代码；已有来源节点的文件不能整体替换 |

<details>
<summary><b>更多能力</b>：解释风格、原地编辑、持久项目与分享、插件</summary>

#### 解释风格与模型选项

设置中可以选择默认、通俗、严谨、多举例、类比和引导式解释。所有风格都先讲清圈画内容的定义，再讲联系；严谨模式会补出原文省略的推导，通俗和类比模式按追问深度逐层简化。偏好设置提供思考强度和联网搜索开关，按当前 Provider 的能力启用；联网默认关闭，Chat 的联网开关与之独立。

#### 编辑和清理学习卡片

Markdown 视图支持按块原地编辑，编辑会形成新的文档版本，并同步移动可验证的文本来源；已有解释锚定的内容不能直接改写，避免节点悄悄指向错误原文。卡片支持拖动、放大、恢复默认位置和撤销；启用 `card-clear` 插件时，还可以框选删除、清空当前文档并在有效期内撤销。

#### 代码工程

上传代码会自动建立文件与符号索引；针对代码节点生成解释时，会检索项目内相关文件并保留路径、行号和版本信息。索引是静态文本与符号检索，不运行构建、语言服务器或项目代码。详细接口与安全边界见 [工作区工具](docs/workspace-tools.md)。

#### 持久项目与分享

接入控制面后可以创建多个持久项目，在工作区列表里右键重命名或删除。分享使用一次性的分享码，接收者得到独立副本，不共享源项目的卷或业务数据；分享码默认有效期为 **7 天**，撤销或过期后不能继续导入。

#### 插件与工作流

应用内置 `documents`、`explanations`、`knowledge-narration` 和 `card-clear` 四个插件，主要工作流包括 `explanation.v1`、`graph_relations.v1`、`narration.v1` 和 `retelling.v1`。插件是随镜像构建的可信代码，不是执行任意用户代码的沙箱。

</details>

## 🏛️ 架构

TutorLM 将浏览器工作台、容器内应用和容器外控制面分开。应用负责文档、节点、解释、任务、模型和存储；控制面负责用户、持久项目、邀请码、计算租约和实例生命周期。两侧通过版本化 HTTP 契约集成，不共享私有模块或数据库。

```text
浏览器工作台 ──▶ Nginx 网关 ──▶ 控制面（用户 · 项目 · 租约 · 槽位）
                     │                    │ 按需创建
                     └──────────────▶ 应用实例（每个工作区独立容器与数据卷）
                                          └─ 文档 · 节点 · 解释 · 任务 · 插件 ──▶ Gemini / OpenAI 兼容模型
```

## 🚀 快速开始

### 1. 准备环境

要求 Python 3.11+、Node.js 22+。在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r instance-app/backend/requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -r runtime-host/control-plane/requirements-dev.txt
Push-Location instance-app/frontend
npm ci
Pop-Location
```

### 2. 启动独立工作台

独立模式不需要 Docker、模型密钥或远程服务，应用使用离线 `fake` provider：

```powershell
.\scripts\dev-instance-app.ps1
```

它会在 `127.0.0.1:8091` 启动应用后端。另开一个终端启动工作台：

```powershell
.\.venv\Scripts\python.exe instance-app/workbench/scripts/dev-server.py --port 8092 --api http://127.0.0.1:8091
```

打开 `http://127.0.0.1:8092/`，输入本地体验码 `TLM-DEV-LOCAL`。这个模式适合验证阅读、节点、解释和前端交互；离线回答不代表真实模型质量。

### 3. 启动控制面开发模式

如果只需要调度、租约和槽位逻辑：

```powershell
.\scripts\dev-runtime-host.ps1
```

控制面默认监听 `127.0.0.1:8090`，使用 fake runtime adapter，不启动真实应用容器。

## 🔌 接入真实模型

| Provider | 说明 |
| --- | --- |
| `fake` | 离线、确定性结果，用于本地开发和回归 |
| `openai_compatible` | OpenAI Chat Completions 兼容接口 |
| `gemini` | Gemini 原生接口，支持流式输出；Gemini 3 支持由模型自行决定的 Google 搜索 |

真实模型配置只应通过服务端环境变量提供：

```text
TUTORLM_PROVIDER=gemini
TUTORLM_MODEL_API_BASE=<provider endpoint>
TUTORLM_MODEL_API_KEY=<server-side secret>
TUTORLM_MODEL_NAME=<provider model name>
TUTORLM_MODEL_TIMEOUT_SECONDS=180
TUTORLM_MODEL_MAX_TOKENS=2048
```

`dev-instance-app.ps1` 会强制使用 `fake` 并清理继承的模型地址和密钥。原生联网支持 Gemini 和明确支持搜索的 OpenAI 兼容模型；可配置思考强度支持 Gemini 2.5/3 和兼容接口的 o1/o3/o4/GPT-5 系列。真实模型请求可能产生费用，模型输出仍需进行内容和数学正确性检查。

## 🗂️ 项目结构

```text
runtime-host/
  control-plane/       用户、项目、邀请码、租约、容量和 Docker 生命周期
  gateway/             Nginx 同源入口和受控转发
  deploy/              控制面与网关部署模板
instance-app/
  backend/             应用 API、Host、插件、任务、格式适配和存储
  frontend/            PDF.js、Reader、租约生命周期和通用浏览器模块
  workbench/           TutorLM 工作台页面、画布、阅读区和本地开发服务器
  deploy/              应用镜像构建入口
contracts/             外层与应用之间的版本化协议
docs/                  架构、配置、API、Reader 和开发说明
scripts/               本地启动和边界检查
```

`runtime-host/` 和 `instance-app/` 是两个开发边界。`instance-app/backend/host` 属于应用容器内部，不是外层控制面。

## 🧪 测试与质量检查

控制面和应用后端应在不同 pytest 进程中运行：

```powershell
.\.venv\Scripts\python.exe -m pytest runtime-host/control-plane/tests -q
.\.venv\Scripts\python.exe -m pytest instance-app/backend/tests -q
node --test instance-app/frontend/tests/*.test.js
node --test instance-app/workbench/scripts/test-access.cjs instance-app/workbench/scripts/test-workspace-view.cjs instance-app/workbench/scripts/test-markdown.cjs instance-app/workbench/scripts/test-explanation-preferences.cjs instance-app/workbench/scripts/test-graph-layout.cjs
.\.venv\Scripts\python.exe scripts/check-boundaries.py
```

浏览器测试需要使用隔离的本地后端，不要对已有用户数据或生产服务运行测试。真实 Docker 集成测试必须显式开启，并使用隔离的本地资源。

## ⚠️ 当前限制

- 扫描 PDF OCR 尚未提供。
- TeX 安全编译执行器尚未提供，编译请求不会回退到不受限 subprocess。
- 流式输出目前只覆盖 Gemini 的卡片解释；任务接口仍是轮询，不提供 HTTP SSE。
- 公共工作台的 TLS、Cookie 安全属性、容量规划和长期运行需要在实际部署环境中单独验收。
- 真实模型调用可能产生费用；仓库测试默认不调用远程模型。
- 关系梳理和布局只能在当前文档范围内工作；复杂或含环的图无法保证完全没有交叉。
- Markdown 原地编辑以文本块为边界；已绑定解释的内容和不可验证的跨版本来源不会被强行重写。

## 🚢 部署说明

控制面动态创建应用实例，Compose 不会静态列出所有用户容器。部署时需要同源提供工作台页面、Reader 模块、PDF.js 脚本、worker 和字体资源。应用镜像负责后端和通用静态资源，完整工作台页面由外层网关按部署方案提供。

配置优先级为：内置默认值 → TOML 文件 → `CONTROL_PLANE_*` 环境变量 → 显式代码覆盖。详细配置见 [docs/configuration.md](docs/configuration.md)。

不要把 `.env`、真实配置、凭据、数据库、卷、日志、镜像归档或 `reference/` 提交到 Git。

## 📚 文档入口

| 主题 | 文档 |
| --- | --- |
| 已实现能力与当前限制 | [docs/feature-status.md](docs/feature-status.md) |
| 工作台启动与浏览器测试 | [instance-app/workbench/README.md](instance-app/workbench/README.md) |
| 架构与责任边界 | [docs/architecture.md](docs/architecture.md) |
| 技术栈与第三方资源 | [docs/technical-resources.md](docs/technical-resources.md) |
| 应用设计与 API | [docs/instance-app-design.md](docs/instance-app-design.md)、[docs/instance-app-api.md](docs/instance-app-api.md) |
| 插件 SDK | [docs/plugin-sdk.md](docs/plugin-sdk.md) |
| 文档 Reader | [docs/document-readers.md](docs/document-readers.md) |
| 工作区、邀请与分享 | [docs/workspaces.md](docs/workspaces.md) |
| 控制面配置与 CLI | [docs/configuration.md](docs/configuration.md)、[docs/cli.md](docs/cli.md) |
| 多人协作流程 | [docs/development.md](docs/development.md) |
| 运行契约 | [contracts/README.md](contracts/README.md) |

## 🎬 路演演示

https://github.com/user-attachments/assets/b56c4b39-8312-4962-95c2-d1e1fc0f42ea

## 📄 许可证

项目采用 [MIT License](LICENSE)。随仓库提供的第三方前端资源保留各自许可证，例如 [KaTeX](instance-app/workbench/assets/katex/LICENSE) 和 [markdown-it](instance-app/workbench/assets/markdown-it/LICENSE)。
