/**
 * 邀请码入口组件：可直接挂载到任意容器的同源 DOM 表单。
 *
 * 职责只有“取得计算租约”这一件事：
 *   1. 用 GET /control/access-policy 读取 invitation_only（每次提交前重新校验）；
 *   2. 收集邀请码：分享码走导入，其他码原样交给服务端判定；
 *   3. 用 POST /control/leases 申请租约，成功后清空输入并回调 onLease。
 *
 * 它不安装租约生命周期模块（避免重复登记页面）；取得租约后由调用方在 onLease
 * 里决定如何接入。组件不读取、不记录、不持久化任何令牌或邀请码文本。
 *
 * 分享导入的 workspace 只在本次挂载的内存缓存里保留（按 code + title 作键），
 * 导入成功后若租约失败，重试会复用该副本而不会重复克隆来源项目。
 */

const SHARE_PREFIX = 'TLM-SHARE-';
const POLICY_URL = '/control/access-policy';
const LEASE_URL = '/control/leases';
const IMPORT_URL = '/control/workspace-imports';
// 与后端 invitation_code 字段的约定上限对齐。
const CODE_MAX_LENGTH = 256;
const IDEMPOTENCY_HEADER = 'Idempotency-Key';

const TEXT = {
  label: '邀请码',
  placeholder: 'TLM-SHARE-xxxx 或 TLM-SYS-xxxx',
  submit: '进入工作台',
  retry: '重试',
  loading: '正在加载访问策略…',
  hint: '分享码会导入为你的独立项目：导入前源项目计算会停止，源内容不受影响，但进行中的任务可能被打断。',
  open: '当前可留空直接试用；如需导入分享项目，请填入邀请码。',
  required: '当前仅限邀请使用，请填入邀请码。',
  importing: '正在导入分享项目…',
  leasing: '正在申请计算资源…',
  done: '已取得计算资源。',
  requiredError: '请输入邀请码。',
  policyError: '暂时无法确认访问策略，请稍后重试。',
  importError: '导入分享项目失败，请稍后重试。',
  genericError: '请求失败，请稍后重试。',
};

// 多挂载时保证 DOM id 唯一，避免 label/aria-describedby 串线。
let mountSequence = 0;

function generateUUID(env) {
  // 只用注入环境（默认为 globalThis）的 Web Crypto。没有 Web Crypto 时明确报错，
  // 绝不退回 Math.random —— 不可靠的去重键会造成重复克隆。
  const cryptoObj = env && env.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID();
  }
  if (cryptoObj && typeof cryptoObj.getRandomValues === 'function') {
    const bytes = cryptoObj.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  const error = new TypeError('This environment lacks Web Crypto (crypto.randomUUID / crypto.getRandomValues); an Idempotency-Key cannot be generated');
  error.local = true;
  throw error;
}

/**
 * 挂载邀请码表单。
 *
 * @param {Element} container 承载表单的容器，会追加一个 <form>。
 * @param {object} [options]
 * @param {string} [options.workspaceId] 已有项目 UUID；仅表示“打开该项目”，不是邀请授权证据。
 * @param {object} [options.environment] 依赖注入点（fetch/document/crypto/console 等），测试用。
 * @param {string} [options.importTitle] 分享导入时可选的项目标题。
 * @param {(state: object) => void} [options.onReady] 策略加载/刷新结束时回调。
 * @param {(response: object) => void} [options.onLease] 租约申请成功后回调。
 * @param {(workspace: object) => void} [options.onImported] 分享导入副本就绪后立即回调（可能早于租约结果）。
 * @param {(error: Error, context: string) => void} [options.onError] 消费方回调自身抛错时回调；未提供则 console.error。
 * @returns {{ready: Promise<object>, refreshPolicy: () => Promise<object>, dispose: () => void}}
 */
export function mountInvitationEntry(container, {
  workspaceId = null,
  environment = globalThis,
  importTitle = null,
  onReady = () => {},
  onLease = () => {},
  onImported = () => {},
  onError = null,
} = {}) {
  if (!container || typeof container.appendChild !== 'function') {
    throw new TypeError('mountInvitationEntry requires a container element');
  }
  const env = environment;
  const doc = env.document || globalThis.document;
  if (!doc || typeof doc.createElement !== 'function') {
    throw new TypeError('mountInvitationEntry requires a document');
  }
  const AbortControllerImpl = env.AbortController || globalThis.AbortController;

  const targetWorkspace = typeof workspaceId === 'string' && workspaceId.trim()
    ? workspaceId.trim()
    : (workspaceId == null ? null : String(workspaceId));

  // 单个 controller 服务于本次挂载的全部请求：同一时刻只有一个在途请求（busy 闸门），
  // dispose 时统一中止，避免旧响应在卸载后回写 DOM。
  const controller = AbortControllerImpl ? new AbortControllerImpl() : null;

  const uid = `tutorlm-invitation-${++mountSequence}`;
  const inputId = `${uid}-code`;
  const hintId = `${uid}-hint`;
  const statusId = `${uid}-status`;
  const errorId = `${uid}-error`;

  let disposed = false;
  let busy = false;
  // null 表示策略未知：表单保持 disabled，绝不当作 invitation_only=false 放行。
  let invitationOnly = null;
  // 本挂载内由成功分享导入换来的 trial grant 对应的工作区。
  // 只有它（或将来服务端显式 grant 字段）能免除邀请码；opts.workspaceId 不能。
  let grantedWorkspaceId = null;

  // 分享导入副本的进程内缓存，键为 code + title；不落盘、不跨挂载共享。
  const importedWorkspaces = new Map();
  let importKey = null;
  let importKeyFor = null;

  const form = doc.createElement('form');
  form.className = 'tutorlm-invitation';
  form.setAttribute('novalidate', '');

  const field = doc.createElement('div');
  field.className = 'tutorlm-invitation__field';

  const label = doc.createElement('label');
  label.setAttribute('for', inputId);
  label.textContent = TEXT.label;

  const input = doc.createElement('input');
  input.id = inputId;
  input.name = 'invitation_code';
  input.type = 'text';
  input.autocomplete = 'off';
  input.spellcheck = false;
  input.placeholder = TEXT.placeholder;
  input.maxLength = CODE_MAX_LENGTH;
  input.setAttribute('maxlength', String(CODE_MAX_LENGTH));
  input.setAttribute('aria-describedby', `${hintId} ${statusId} ${errorId}`);

  const button = doc.createElement('button');
  button.type = 'submit';
  button.textContent = TEXT.submit;

  const retryButton = doc.createElement('button');
  retryButton.type = 'button';
  retryButton.className = 'tutorlm-invitation__retry';
  retryButton.textContent = TEXT.retry;
  retryButton.hidden = true;

  const hint = doc.createElement('p');
  hint.id = hintId;
  hint.className = 'tutorlm-invitation__hint';
  hint.textContent = TEXT.hint;
  hint.title = TEXT.hint;

  const status = doc.createElement('p');
  status.id = statusId;
  status.className = 'tutorlm-invitation__status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');

  const error = doc.createElement('p');
  error.id = errorId;
  error.className = 'tutorlm-invitation__error';
  error.setAttribute('role', 'alert');

  field.appendChild(label);
  field.appendChild(input);
  field.appendChild(button);
  form.appendChild(field);
  form.appendChild(hint);
  form.appendChild(status);
  form.appendChild(error);
  form.appendChild(retryButton);

  function setStatus(message) {
    status.textContent = message;
  }

  function setError(message) {
    error.textContent = message;
  }

  function updateControls() {
    // 邀请码输入框始终可见；这里只切换可用性，不做隐藏。
    const enabled = !disposed && !busy && invitationOnly !== null;
    input.disabled = !enabled;
    button.disabled = !enabled;
    // 仅在“邀请模式且尚无 trial grant”时必填；workspaceId 不构成授权。
    input.required = invitationOnly === true && grantedWorkspaceId === null;
    input.setAttribute('aria-required', input.required ? 'true' : 'false');
    form.setAttribute('aria-busy', busy ? 'true' : 'false');
    retryButton.disabled = disposed || busy;
  }

  function setBusy(value) {
    busy = value;
    updateControls();
  }

  function reportCallbackError(reason, context) {
    if (typeof onError === 'function') {
      try {
        onError(reason, context);
        return;
      } catch {
        /* fall through to console */
      }
    }
    const consoleObj = env.console || (typeof globalThis !== 'undefined' ? globalThis.console : undefined);
    if (consoleObj && typeof consoleObj.error === 'function') {
      consoleObj.error(`[invitation-entry] ${context} callback threw`, reason);
    }
  }

  // 消费方回调抛错不会破坏表单状态，也不会被误报成 API 失败；错误交给 onError/console。
  function notify(callback, argument, context) {
    try {
      return callback(argument);
    } catch (reason) {
      reportCallbackError(reason, context);
      return undefined;
    }
  }

  async function request(method, url, body, extraHeaders) {
    const headers = { Accept: 'application/json', ...(extraHeaders || {}) };
    const options = { method, credentials: 'same-origin', headers };
    if (controller) options.signal = controller.signal;
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await env.fetch(url, options);
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const envelope = payload && payload.error ? payload.error : null;
      const failure = new Error((envelope && envelope.message) || TEXT.genericError);
      failure.status = response.status;
      failure.code = envelope && envelope.code;
      throw failure;
    }
    return payload || {};
  }

  function readPolicyOnly(policy) {
    return policy && typeof policy.invitation_only === 'boolean' ? policy.invitation_only : null;
  }

  async function fetchPolicy() {
    try {
      const policy = await request('GET', POLICY_URL);
      const only = readPolicyOnly(policy);
      if (only === null) {
        // 成功的 HTTP 响应但没有布尔策略：仍然不放行。
        return { ok: false, invitationOnly: null, enabled: false, error: new Error(TEXT.policyError) };
      }
      invitationOnly = only;
      return { ok: true, invitationOnly: only, enabled: true };
    } catch (failure) {
      // 请求失败同样不放行（no fail-open）。
      return { ok: false, invitationOnly: null, enabled: false, error: failure };
    }
  }

  function applyPolicyResult(result) {
    if (disposed) return;
    if (result.ok) {
      setStatus(result.invitationOnly ? TEXT.required : TEXT.open);
      setError('');
      retryButton.hidden = true;
    } else {
      setStatus('');
      setError(TEXT.policyError);
      retryButton.hidden = false;
    }
    updateControls();
  }

  async function resolveImport(code) {
    const title = typeof importTitle === 'string' ? importTitle.trim() : '';
    const key = `${code}\u0000${title}`;
    const cached = importedWorkspaces.get(key);
    if (cached) return { workspace_id: cached.workspace_id, workspace: cached.workspace, fresh: false };

    // 同一个导入 attempt 复用同一个 Idempotency-Key，直到成功为止。
    if (importKeyFor !== key) {
      importKeyFor = key;
      importKey = generateUUID(env);   // 缺 Web Crypto 时抛出明确的 unsupported 错误，见 generateUUID
    }
    const body = { invitation_code: code };
    if (title) body.title = title;
    const payload = await request('POST', IMPORT_URL, body, { [IDEMPOTENCY_HEADER]: importKey });
    const workspace = (payload && payload.workspace) || payload || {};
    const importedId = workspace.workspace_id || workspace.id;
    if (!importedId) {
      const failure = new Error(TEXT.importError);
      failure.status = 502;
      throw failure;
    }
    const entry = { workspace_id: importedId, workspace };
    importedWorkspaces.set(key, entry);
    // 分享导入成功即取得该工作区的 trial grant：之后即使清空邀请码也能重试。
    grantedWorkspaceId = importedId;
    importKey = null;
    importKeyFor = null;
    if (!disposed) updateControls();
    return { ...entry, fresh: true };
  }

  async function submitFlow() {
    if (disposed || busy) return;
    const code = typeof input.value === 'string' ? input.value.trim() : '';

    // 每次提交前重新校验策略：期间策略可能收紧或放松。
    setBusy(true);
    const policy = await fetchPolicy();
    applyPolicyResult(policy);
    if (disposed) return;
    if (!policy.ok) {
      setBusy(false);
      return;
    }
    // 邀请模式下只有“已有 trial grant”才能免码；targetWorkspace 不构成授权。
    const codeRequired = policy.invitationOnly === true && grantedWorkspaceId === null;
    if (codeRequired && !code) {
      setBusy(false);
      setError(TEXT.requiredError);
      if (typeof input.focus === 'function') input.focus();
      return;
    }
    setError('');

    let lease = null;
    try {
      // 已取得的 trial grant（成功导入的副本）允许在无码重试时复用。
      let requestWorkspace = targetWorkspace || grantedWorkspaceId;
      let invitationCode;
      if (code.startsWith(SHARE_PREFIX)) {
        // 只有前缀用于路由；服务端仍会校验真实邀请码类型。
        setStatus(TEXT.importing);
        const result = await resolveImport(code);
        requestWorkspace = result.workspace_id;
        // 只在真正新建副本时通知一次；复用缓存副本的重试不再重复回调，
        // 但即便随后的租约失败，调用方也已经知道副本已建立。
        if (result.fresh) notify(onImported, result.workspace, 'onImported');
      } else if (code) {
        // 系统码或其他码原样上传，优先级完全由服务端判定。
        invitationCode = code;
      }
      setStatus(TEXT.leasing);
      const body = {};
      if (requestWorkspace) body.workspace_id = requestWorkspace;
      if (invitationCode) body.invitation_code = invitationCode;
      lease = await request('POST', LEASE_URL, body);
      input.value = '';
      setStatus(TEXT.done);
    } catch (failure) {
      if (disposed) return;
      // 本地错误（无 Web Crypto 等）直接说明原因；服务端错误用其后端 message。
      setError(failure && failure.status ? (failure.message || TEXT.genericError)
        : (failure && failure.local ? failure.message : TEXT.genericError));
    } finally {
      if (!disposed) setBusy(false);
    }

    if (disposed || !lease) return;
    // 回调抛错不改变“租约已取得”的事实，也不写回 API 错误文案。
    notify(onLease, lease, 'onLease');
  }

  async function refreshPolicy() {
    if (disposed || busy) {
      return { ok: false, invitationOnly, enabled: false, error: new Error(TEXT.policyError) };
    }
    setBusy(true);
    const result = await fetchPolicy();
    if (disposed) return { ...result, enabled: false };
    applyPolicyResult(result);
    setBusy(false);
    notify(onReady, result, 'onReady');
    return { ...result, enabled: result.ok };
  }

  function handleSubmit(event) {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    // 内部错误已写入 UI；这里兜底消费 Promise，避免未处理的 rejection。
    submitFlow().catch(() => {});
  }

  function handleRetry() {
    refreshPolicy().catch(() => {});
  }

  container.appendChild(form);
  form.addEventListener('submit', handleSubmit);
  retryButton.addEventListener('click', handleRetry);

  setStatus(TEXT.loading);
  updateControls();

  const ready = refreshPolicy();

  return {
    ready,
    refreshPolicy,
    dispose() {
      if (disposed) return;
      disposed = true;
      if (controller) {
        try {
          controller.abort();
        } catch {
          /* ignore */
        }
      }
      if (typeof form.removeEventListener === 'function') form.removeEventListener('submit', handleSubmit);
      if (typeof retryButton.removeEventListener === 'function') retryButton.removeEventListener('click', handleRetry);
      try {
        if (form.parentNode && typeof form.parentNode.removeChild === 'function') {
          form.parentNode.removeChild(form);
        } else {
          container.removeChild(form);
        }
      } catch {
        /* already detached */
      }
    },
  };
}

