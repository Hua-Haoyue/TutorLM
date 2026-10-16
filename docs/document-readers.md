# 文档阅读器设计（Markdown / PDF / TeX 共享）

> 状态：**设计文档，尚未实现**。本轮只落盘设计：未执行真实 Docker、未构建或拉取镜像、
> 未访问远程基础设施、未改动 `contracts/` / `runtime-host/` / `sync/` 与既有前端模块、未提交。
> 未实测任何工具行为。文末官方来源为官方站点链接，其内容由命令行 HTTP 抓取（`curl`/`Invoke-WebRequest`，非浏览器、非可视化核对）比对；**未经本次联网可视化核实，亦未做版本级核对**，不代表本项目已验证。
> 依据：`instance-app-design.md`、`instance-app-api.md`、`../contracts/instance-runtime-v1/v2/v3.md`、
> 以及本文末尾列出的官方一手来源。

## 0. 定位

浏览器内的工作台读取三类内容，**共用一个 PDF 阅读器**，另为 TeX 源码提供独立源视图/编辑器：

| 内容 | 阅读形态 | 归属 |
| --- | --- | --- |
| Markdown（`.md`/`.txt`） | 浏览器内渲染的文本阅读器 | `instance-app/frontend` |
| PDF | 浏览器内 PDF 阅读器（PDF.js 渲染） | `instance-app/frontend` |
| TeX 源码 | 独立 source view / editor | `instance-app/frontend` |
| TeX 编译产物 PDF | **复用同一个 PDF 阅读器** | `instance-app/frontend`（产物由编译执行器产生） |

约束（继承 `instance-app-design.md`）：阅读器只作用于**当前 env 绑定的 workspace/项目**；
不决定上游地址；不直连实例端口；不把文档内容当作身份或授权证据。
文档正文由应用后端提供（`GET /api/documents/{id}/content` 返回全文），前端不自行改写真值。

本文只描述**阅读与引用锚定**；TeX 编译的安全执行另见 `tex-compilation-security.md`。

## 1. 共享阅读器抽象（ReaderAdapter）

`instance-app/frontend` 定义一个适配器接口，Markdown、PDF、TeX 源视图各自实现，工作台只对接口编程：

```ts
interface ReaderAdapter {
  // 身份与生命周期
  open(ref: DocumentRevisionRef): Promise<void>;   // 见 §2，绑定 document_id + revision_id(+artifact)
  close(): Promise<void>;
  abort(reason?: string): void;                    // 中止在途解析/渲染/取数
  clear(): void;                                   // 切文档时清空前一份状态（见 §1 设计要点）

  // 导航与视图
  getOutline(): Promise<OutlineNode[]>;
  search(query: SearchQuery, opts?: SearchOptions): Promise<SearchResult[]>;
  goTo(target: NavigationTarget): Promise<void>;
  setZoom(level: ZoomSpec): void;
  setHighlight(anchors: ReferenceDraft[]): void;   // 依据引用锚点回显

  // 事件（Selection -> ReferenceDraft）
  on(event: 'selection', cb: (sel: SelectionEvent) => void): Unsubscribe;
  on(event: 'reference', cb: (draft: ReferenceDraft) => void): Unsubscribe;
  on(event: 'viewchange', cb: (v: ViewState) => void): Unsubscribe;
  on(event: 'error', cb: (e: ReaderError) => void): Unsubscribe;
}
```

设计要点：

- **事件是唯一出口**：阅读器不直接写节点/图谱，只发出临时的 `ReferenceDraft`；节点由用户在另一流程确认后，
  经 `POST /api/projects/{id}/nodes`（`metadata.source` 存锚点）落库（见 `instance-app-api.md` §5）。
- **取消/中止/清空是一等能力**：`abort` 必须中止在途的 fetch、worker 解析与渲染任务；
  `clear` 在切换文档时同步清空选区、搜索高亮、大纲与缓存，避免旧文档的锚点串到新文档。
- **可访问性**：大纲暴露为可聚焦树（`role=tree`/`treeitem`），页可达性按分页虚拟化顺序暴露，
  键盘可达（翻页、缩放、搜索）；不依赖鼠标选区才可操作。
- **分页/虚拟化**：PDF 天然分页，只渲染视口内页 + 邻页；Markdown 长文按块虚拟化。
  虚拟化不得破坏引用锚点定位（锚点基于数据而非 DOM 节点身份，见 §2）。
- **不需要**承诺“所见即后端字节”：渲染层与服务端提取层可能不同（PDF 见 §4.4）。

## 2. 引用锚点 schema（共享命名，**v1 草案，待冻结**）

前端引用草稿与后端节点锚点使用**同一套字段名**。命名先固定，不要求每种 selector 字段完全相同。

```ts
DocumentRevisionRef = {
  document_id: string,        // 稳定文档身份，跨克隆不变
  revision_id: string,        // 仅指“哪一份源文本/源字节被读取”——见 §2.1
  artifact_id?: string,       // 仅当引用编译产物（PDF）时存在
  build_id?: string,          // 仅当引用编译产物时存在，标识该次构建——见 §2.1
}

ReferenceDraft = {
  document_id: string,
  revision_id: string,        // 与 ref 一致，必须固定
  artifact_id?: string,       // 产物引用时才有；不与 revision_id 互换
  build_id?: string,          // 产物引用时才有；不与 revision_id 互换
  selector: Selector,         // discriminated union，见 §2.2
  quote: string,              // 选中文本（canonical，见 §3.4 / §4.4）
  prefix: string,             // 选中前文（定长或就近窗口）
  suffix: string,             // 选中后文
  extractor_version?: string, // 产生该锚点的提取器/渲染器版本，便于失效与再锚定
  created_at?: string,
}
```

规则：

- `document_id` / `revision_id` / `artifact_id` / `build_id` **四者语义不同，禁止互换**。
- `revision_id` 永远指内容身份；`artifact_id`/`build_id` 永远指**某次编译产物**。
  一份 revision 可能有多次 build；一次 build 只对应一个 revision。
- 引用 PDF 时必须**同时**给出 `revision_id`（TeX 源版本）与 `artifact_id`/`build_id`（产物身份），
  否则无法判断“这个锚点在旧产物还是新产物上”。
- 不做“偏移量即授权”：锚点只是**内容定位提示**，权限判定永远在服务端按 workspace/session（见 §4.5）。
- **MD 偏移单位不混存**：前端 `ReferenceDraft` 内部可用 `unit:'utf16'`（DOM 原生单位），但**服务器持久化只接受 `'codepoint'`**；API 若接受 `utf16`，必须**强制转换为 codepoint 并显式声明**，不得把两种单位在同一条 anchor 里混存（见 §3.2）。

### 2.1 revision 与 build 的固定选择

| 概念 | 含义 | 何时变化 | 归属字段 |
| --- | --- | --- | --- |
| `revision_id` | 源内容版本（Markdown 文本、TeX 源、PDF 原始字节的某次不可变版本） | 源被编辑/替换 | `revision_id` |
| `artifact_id` | 某次编译产出的 PDF 制品身份 | 每次产出新 PDF | `artifact_id` |
| `build_id` | 某次构建过程/输入集合的身份（可含编译器与包摘要） | 每次构建 | `build_id` |

要求：**固定选择某一维度做引用，不要在实现里混用**。阅读器展示的是 `DocumentRevisionRef` 指向的那一份，
不得在 build 变化时静默把锚点当成仍指向同一页/同一文本。

### 2.2 Selector 联合（按类型，字段可不同）

```ts
OffsetUnit = 'codepoint' | 'utf16'

// 归一化、未旋转 CropBox 坐标（原点 top-left，x→右，y→下，取值 0–1）；见 §4.2
Rect = { x: number, y: number, w: number, h: number }

PdfPageAnchor = {
  page: number,      // 从 1 开始
  rects: Rect[],     // 视觉矩形（高亮回显）
  quads?: Rect[],    // 可选：更精确的四边形（旋转/倾斜文本）
}

Selector =
  | { type: 'text',       start: number, end: number, unit: OffsetUnit }                 // Markdown 正文
  | { type: 'tex-source', start: number, end: number, unit: OffsetUnit, file: string }   // file 必填
  | { type: 'pdf',        pages: PdfPageAnchor[], rotation?: number, cropbox_transform?: number[] }
  | { type: 'whole-document' }
```

- `OffsetUnit = 'codepoint' | 'utf16'`（见 §3.2）。
- **PDF 只有一个坐标系（canonical）**：归一化、未旋转的 CropBox 坐标，原点 **top-left**、**x→右、y→下**，取值 0–1。
  **不再混搭** PDF 物理 user-space points 与归一化两套体系。
- `rotation`（页面顺时针角度）与 `cropbox_transform`（把 canonical 映射到屏幕的变换）**仅用于显示映射**，
  **不参与定位语义**；存储只为回显时复算，读取时不得据其改写 `rects`。
- **多页选择必须用 `pages` 数组**：单页即 `pages` 长度为 1；禁止用单个 `page` 字段描述一次选择。
- **`tex-source.file` 必填**：即使是工程 main 文件也要写明路径，不设 optional。
- **命名一致但字段不必对齐**：`text` 用偏移，`pdf` 用 `pages[]`，`tex-source` 用偏移 + `file`。

## 3. Markdown 阅读器

### 3.1 渲染与信任

- 后端提供**全文文本**（`GET /api/documents/{id}/content`，不得用 `text_excerpt` 替代），阅读器渲染。
- **禁止 raw HTML**：Markdown 中的 HTML 一律**转义为纯文本**或**严格消毒**后再入 DOM。
  CommonMark 规范本身允许 raw HTML 透传（见来源），因此**必须显式关闭或消毒**，不能依赖解析器默认行为。
  允许的方案：关闭 raw HTML 渲染；或经允许列表消毒（如 DOMPurify）后再插入。
- **数学**：数学渲染同样视为不可信输入，禁止启用会注入脚本/`<iframe>`/事件处理器的路径；
  公式渲染失败降级为纯文本，不静默执行。
- **本地图片**：图片引用默认**不自动加载**；仅允许经服务端授权的同源资源路径
  （`document_id`/`revision_id` 绑定），拒绝外链与 `..` 穿越；加载需显式权限或显式用户动作。
- **链接**：正文外链默认不可自动导航（点击需用户确认），并阻止 `javascript:`/`data:` 等危险 scheme。

### 3.2 偏移语义（Unicode 与 UTF-16）

Markdown 文本有两套并行偏移，必须**显式标注单位**，禁止假设二者相等：

| 层 | 单位 | 说明 |
| --- | --- | --- |
| 后端 canonical | **Unicode 码点（scalar value）** | 对解码后的 UTF-8 文本按码点计偏移 |
| 浏览器 DOM/JS | **UTF-16 code unit** | `String.prototype.length`、`Range`/`Selection` 偏移均按 UTF-16 code unit（见来源） |

结论：

- 后端 selector 的 `unit` 默认 `'codepoint'`；阅读器在 DOM 侧使用 `'utf16'`。
- 前端必须做**码点 ↔ UTF-16 转换**再对齐：BMP 外字符（如 emoji）计 **1 码点 / 2 code unit**，
  组合字符与代理对要按码点边界处理，避免半代理切分。
- **前端可发 `utf16`，服务端只持久化 `codepoint`**：`ReferenceDraft` 在浏览器内可以用 DOM 原生单位 `'utf16'`；但落库（`metadata.source`）**只存 `'codepoint'`**。若 API 接受 `utf16`，必须在入口**强制转换并声明**（转成 codepoint 后再存），**不得**同一条 anchor 混存两种单位。
- 每个持久化 selector **必须显式带 `unit`**（持久层恒为 `'codepoint'`）；比较/拼接前先归一化到同一单位。
- 前端字符串比较注意 `DOMString` 可含未配对代理；对文本处理应使用 scalar value 语义（见 WHATWG WebIDL）。

### 3.3 选择事件

`on('selection')` 产出 `{ document_id, revision_id, selector:{type:'text',start,end,unit}, quote, prefix, suffix }`；
`start/end` 由阅读器统一为**同一单位**（建议 canonical = 码点），`quote` 取自该区间的解码文本。

### 3.4 canonical quote

`quote` 使用规范化后的文本（换行统一、去掉渲染期装饰）；`prefix/suffix` 取定长窗口。
服务端落库前应能**用 quote 复核**定位，防止仅凭偏移在文本变化后错位。

## 4. PDF 阅读器（PDF.js）

### 4.1 加载与渲染

- 使用浏览器内 PDF.js；渲染层（canvas + text layer）与服务端提取层**各自独立**。
- 允许的加载参数（依据官方 `DocumentInitParameters`，见来源）：
  - `cMapUrl` / `standardFontDataUrl` / `wasmUrl` / `iccUrl`：**指向本地已打包资源**，不由远端拉取。
  - `disableStream` / `disableAutoFetch`：按需关闭流式与预取，控制取数行为。
  - `rangeChunkSize`：限制 range 请求块大小。
  - `maxImageSize`：限制渲染像素总量，防超大图拖垮内存。
  - `enableXfa`：默认 `false`（官方默认即为 `false`），不渲染 XFA 动态表单。
  - `isOffscreenCanvasSupported` / `useWasm`：按部署能力显式设定。
- **禁用活动内容与附件执行**：不执行 PDF 内嵌 JavaScript、不自动打开附件、不自动跳转外链
  （链接目标需用户显式确认）；渲染注解时选择不包含交互表单的渲染模式
  （官方 `annotationMode` 有 `DISABLE`/`ENABLE`/`ENABLE_FORMS` 等取值，见来源）。

### 4.2 选择模型：逻辑文本 + 视觉四边形

PDF 没有天然“字符偏移”，选择由两部分组成：

- **逻辑文本选择**：按阅读顺序的连续文本（跨行/跨块拼接），用于生成 `quote`。
- **视觉四边形**：每行/每段的 `Rect[]`，用于高亮与回显。

**唯一 canonical 坐标系**（存储与比较都用它，不并存第二套）：

| 项 | 约定 |
| --- | --- |
| 基准 | **未旋转的 CropBox**（不是 MediaBox、不是旋转后的页面） |
| 归一化 | x、y、w、h 均落在 **0–1**，即除以 CropBox 宽/高；与缩放、DPR 无关 |
| 原点 | **top-left**（左上角） |
| 轴方向 | **x 向右、y 向下**（固定，不随旋转翻转） |
| 页号 | **从 1 开始**（官方 `getPage(pageNumber)`：首页为 1） |
| 多页 | 用 `pages: [{page, rects, quads?}, ...]`，见 §2.2 |

`rotation` 与 `cropbox_transform` **只做显示映射**（把 canonical 映射到渲染后的屏幕），
**不参与定位语义**：读取锚点时不得据它们改写 `rects`。渲染侧需要的官方原始量仅用于换算：

- `view`（页面可见区 `[x1,y1,x2,y2]`，user space）与 `getViewport` 返回的 `PageViewport` 变换，用于屏幕↔canonical 换算；
- `rotate`（页面顺时针角度，官方 `PDFPageProxy.rotate`）、`userUnit`（1/72 英寸单位）仅作换算输入。

要求：**不得只用 bbox 唯一定位**。同一页可能有多处相同 bbox 文本；定位必须以
`pages[] + quote + prefix/suffix + extractor_version` 组合，并承认“仅粗略回显”。

### 4.3 多页选择与搜索

- **多页选择**：允许跨页选择，但每页各存一组 `rects` 与页号；引用时按页展开。
- **搜索硬页限**：搜索必须设**最大页数上限**与超时；超限返回部分结果并标注 `truncated`，
  不静默全量扫描大文档（避免长任务卡死/内存膨胀）。搜索应可取消（`abort`）。

### 4.4 排序不一致与 canonical quote 复核

- 阅读器的**文本排序**（用户看到/选中的顺序）与服务端**提取顺序**（`getTextContent` 等）
  **不保证给出可互换的偏移映射**——注意这与 §3.2 的 UTF-16 问题**是两个不同问题**：
  §3.2 是同一字符串的**单位换算**（码点↔UTF-16，可精确换算），这里是**顺序/切口可能不同**（不可靠换算）。
  官方 `getTextContent` 提供按阅读顺序的 `items`，但与本项目后端提取器版本可能不同。
- 因此：**不承诺 textOrder ↔ 偏移一一对应**；跨层引用以 `quote + prefix/suffix` 为 canonical，
  经服务端复核定位；偏移仅作提示。
- 记录 `extractor_version`（渲染/提取任一来源的版本），版本变化时标记锚点需**再锚定**。

### 4.5 扫描版与 OCR

- **扫描版 PDF 不得假装有文本**：若某页无可提取文本，`quote` 为空，选择/搜索返回“无文本层”，
  不得伪造 offset 或猜测内容。
- **OCR 后升级**：OCR 结果作为**独立 revision** 引入（新 `revision_id`，`extractor_version` 标注 OCR），
  与原扫描件锚点分开；旧锚点通过显式**再锚定**流程迁移，不原地改写。

### 4.6 传输/内存/安全

- **取数**：优先 `fetch` + 同源鉴权；若用 `Blob`/`ArrayBuffer`，注意官方提示
  `TypedArray` 会**转移所有权**到 worker，主线程不再持有，避免内存翻倍；用完释放并 `cleanup`。
- **ETag / Range**：可利用 `ETag` 与 Range 做增量与缓存；**任何 token 都不得写进 URL**
  （避免日志/历史泄漏），鉴权走头部；PDF 数据不落地为可长期引用的公共 URL。
- **多标签页**：每个标签页独立加载，不共享可变全局状态；关闭一个标签页不得影响其他页。
- **CSP**：限制 `script-src`/`worker-src`/`frame-src`/`connect-src`/`img-src` 为**本地允许列表**；
  PDF 阅读器与 worker 资源必须为本地打包资源（见来源 CSP3）。
- **依赖与许可**：`pdfjs-dist` 等第三方库**固定版本**、锁定自托管资源；许可与版本在实现阶段单独补录，
  本文不预先罗列版本号（不声称已实测）。

## 5. TeX 源码视图

- **独立 source view / editor**：展示并编辑 TeX **源码**，与 PDF 阅读器分离。
- 源码锚点用 `selector:{type:'tex-source', start, end, unit, file}`；`file` **必填**（工程 main 文件也写明路径），同样显式标注偏移单位（§3.2），持久层恒为 `'codepoint'`。
- 编译产物 PDF 复用 §4 的 PDF 阅读器；引用时**同时**带 `revision_id`（源）与 `artifact_id`/`build_id`（产物）。
- 编译的安全执行与产物可信性见 `tex-compilation-security.md`；阅读器只消费经校验的产物。

## 6. 验收（设计判据，未执行）

| 用例 | 期望 |
| --- | --- |
| Markdown raw HTML | 注入 `<script>`/事件处理器被转义或消毒，不执行 |
| Markdown 数学/图片 | 危险渲染被拒；外链图片默认不加载 |
| 码点/UTF-16 | emoji（BMP 外）跨层锚点往返一致，无半代理切分 |
| PDF 旋转/裁剪 | 归一化坐标在不同缩放/旋转/DRP 下回显稳定 |
| 多页选择/搜索 | 选择以 `pages[]` 表达、逐页展开；搜索超出页限返回 `truncated` 并可取消 |
| MD 偏移持久化 | 前端可发 `utf16`，落库恒为 `codepoint`，同条 anchor 不混存；无半代理切分 |
| TeX 锚点 file | 缺 `file` 的 `tex-source` selector 被拒（必填） |
| 扫描版 PDF | 无文本时明确“无文本层”，不伪造锚点 |
| 切换文档 | `abort`+`clear` 后旧锚点不串入新文档 |
| 引用稳定性 | 同一 `revision_id`（及产物同一 `build_id`）下锚点可复核定位 |
| 不把偏移当授权 | 伪造锚点不能越权访问他 workspace 文档 |

## 7. 事实边界与官方来源

- 本文为**设计**，对应代码尚未实现；未实测任何库/引擎行为；未改动其他文件。
- 相对链接：`instance-app-design.md`、`instance-app-api.md`、`tex-compilation-security.md`、
  `../contracts/instance-runtime-v3.md`。

官方一手来源（纯链接）：

- PDF.js API（`getPage` 首页为 1、`rotate`、`userUnit`、`view`、`getViewport`、`getTextContent`、`RenderTask.cancel`）：
  https://mozilla.github.io/pdf.js/api/draft/module-pdfjsLib-PDFPageProxy.html
- PDF.js `DocumentInitParameters`（`cMapUrl`/`standardFontDataUrl`/`wasmUrl`/`iccUrl`/`enableXfa`/`maxImageSize`/`disableAutoFetch`）：
  https://mozilla.github.io/pdf.js/api/draft/module-pdfjsLib.html
- PDF.js `PDFDocumentProxy`（`numPages`、`getOutline`、`getPageLabels`）：
  https://mozilla.github.io/pdf.js/api/draft/module-pdfjsLib-PDFDocumentProxy.html
- PDF.js `PDFDocumentLoadingTask.destroy` 与 `RenderTask.cancel`：
  https://mozilla.github.io/pdf.js/api/draft/module-pdfjsLib-PDFDocumentLoadingTask.html
  https://mozilla.github.io/pdf.js/api/draft/module-pdfjsLib-RenderTask.html
- PDF.js 部署为网页：https://github.com/mozilla/pdf.js/wiki/Setup-PDF.js-in-a-website
- CommonMark 规范（raw HTML 透传）：https://spec.commonmark.org/0.31.2/#raw-html
- DOMPurify：https://github.com/cure53/DOMPurify
- OWASP XSS 预防：https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html
- WHATWG WebIDL（`DOMString` 与 `USVString`、scalar value 语义）：https://webidl.spec.whatwg.org/
- WHATWG DOM（偏移以 code unit 表达）：https://dom.spec.whatwg.org/
- MDN `String.prototype.length`（UTF-16 code unit）：https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/String/length
- W3C CSP3（`worker-src`/`script-src` 等）：https://www.w3.org/TR/CSP3/
