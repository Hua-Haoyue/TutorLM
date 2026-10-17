/* Entry flows for index.html (welcome + boot) and busy.html (503 + Retry-After).
   Every number shown comes from the server; nothing here simulates capacity. */
(function () {
  'use strict';
  const api = RuntimeAPI, { fmtClock, toast } = TLM;
  const $ = (s) => document.querySelector(s);
  const RETRY_KEY = 'tlm.retry-after';
  const isBusyPage = !!$('#retry');

  function paintSlots(el, c) {
    if (!el) return;
    const total = c.slots_total || 0;
    const busy = Math.max(0, total - (c.slots_free ?? c.slots_ready ?? 0));
    const draining = Math.min(busy, (c.slots_draining || 0) + (c.slots_reclaiming || 0));
    el.innerHTML = Array.from({ length: total }, (_, i) =>
      el.id === 'cap-bar'
        ? `<i class="${i < busy ? 'on' : ''}"></i>`
        : `<i class="${i < busy - draining ? '' : i < busy ? 'drain' : 'free'}"></i>`).join('');
    el.style.gridTemplateColumns = `repeat(${Math.max(1, total)}, 1fr)`;
  }
  const setText = (sel, v) => { const el = $(sel); if (el) el.textContent = v; };

  /* Capacity: managed → /control/capacity; standalone → health of this single instance. */
  async function refreshCapacity() {
    try {
      if (await api.detect()) {
        const c = await api.get('/control/capacity', { auth: false });
        setText('#cap-free', c.slots_free); setText('#cap-total', c.slots_total);
        setText('#cap-meta', c.slots_free > 0 ? `${c.leases_active} 位访客正在使用 · 每 10 秒刷新` : '暂时没有空闲工作区，可以稍后再来');
        paintSlots($('#cap-bar'), c); paintSlots($('#slots'), c);
        setText('#d-total', c.slots_total); setText('#d-active', c.leases_active); setText('#d-drain', (c.slots_draining || 0) + (c.slots_reclaiming || 0));
        setText('#deploy-mode', 'MANAGED · 多实例');
        return c;
      }
      const ready = await api.get('/health/ready', { auth: false }).catch((e) => ({ status: e.status === 503 ? 'degraded' : 'error' }));
      setText('#cap-label', '实例状态');
      setText('#cap-free', ready.status === 'ok' ? '就绪' : '繁忙');
      setText('#cap-of', '');
      setText('#cap-meta', ready.provider ? `独立实例 · 模型 ${ready.provider.name}/${ready.provider.model}` : '独立实例');
      const bar = $('#cap-bar'); if (bar) bar.hidden = true;
      setText('#deploy-mode', 'STANDALONE · 单实例');
      return null;
    } catch (e) {
      setText('#cap-meta', '无法读取容量：' + e.message);
      return null;
    }
  }

  if (isBusyPage) busyPage(); else welcomePage();

  /* ---------------- index.html ---------------- */
  async function welcomePage() {
    const boot = $('#boot'), msg = $('#gate-msg'), invite = $('#invite'), start = $('#start');
    const shareInput = $('#share-code-input');
    let needInvite = false, controller = null, leased = false;
    const say = (text, err) => { msg.textContent = text || ''; msg.classList.toggle('err', !!err); };
    $('#restore-login').onclick = async () => {
      const user = $('#restore-user').value.trim(), token = $('#restore-token').value.trim();
      if (!user || !token) { say('请输入用户 UUID 和恢复凭据', true); return; }
      const button = $('#restore-login'); button.disabled = true;
      try { await api.login(user, token); $('#restore-token').value = ''; say('登录已恢复，点击开始试用打开项目'); }
      catch (error) { say(error.message, true); }
      finally { button.disabled = false; }
    };

    // Invite is needed for demo runtimes; behind a control plane the runtime is only reachable after a lease.
    async function probe() {
      try {
        const managed = await api.detect();
        if (managed) {
          needInvite = (await api.accessPolicy()).invitation_only;
          try { const l = await api.currentLease(); showResume(l.lease); } catch { /* no lease yet */ }
        } else {
          const rt = await api.get('/api/runtime', { auth: false });
          needInvite = rt.mode === 'demo' && !api.hasToken();
          if (rt.mode === 'local') say('本地独立实例：无需体验码，数据保存在本机。');
          if (rt.mode === 'demo' && api.hasToken()) {
            try { await api.loadSession(); showResume(null); } catch { needInvite = true; }
          }
        }
      } catch (e) { say('无法连接服务：' + e.message, true); }
      $('#invite-field').hidden = false;
      invite.required = needInvite;
    }
    function showResume(lease) {
      const btn = $('#resume');
      const paint = () => {
        const left = lease ? lease.expires_at - Date.now() / 1000 : api.secondsLeft();
        btn.hidden = left != null && left <= 0;
        setText('#resume-left', left == null ? '—' : fmtClock(left));
      };
      paint(); setInterval(paint, 1000);
    }
    $('#resume').onclick = () => { location.href = 'workspace.html'; };

    const step = (k, state, t) => {
      const li = boot.querySelector(`[data-k="${k}"]`);
      li.className = state;
      li.querySelector('[data-t]').textContent = t ?? (state === 'run' ? '…' : '');
    };
    function openBoot(managed) {
      boot.querySelectorAll('li').forEach((li) => { li.className = ''; li.querySelector('[data-t]').textContent = ''; });
      if (!managed) { step('alloc', 'done', '单实例'); step('start', 'done', '已运行'); }
      setText('#boot-msg', '通常需要几秒钟。');
      boot.hidden = false;
      $('#boot-cancel').focus();
    }
    async function cancel() {
      controller?.abort();
      boot.hidden = true; start.disabled = false;
      if (leased) { try { await api.release(); } catch { /* best effort */ } leased = false; toast('已取消，工作区已释放'); }
    }
    $('#boot-cancel').onclick = cancel;
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !boot.hidden) cancel(); });

    $('#gate').onsubmit = async (e) => {
      e.preventDefault();
      const code = invite.value.trim();
      const shareCode = shareInput ? shareInput.value.trim() : '';
      if (!code && needInvite) { say('请输入邀请码', true); invite.focus(); return; }
      say('');
      start.disabled = true;
      controller = new AbortController();
      let managed;
      try { managed = await api.detect(); }
      catch (error) { start.disabled = false; say(error.message, true); return; }
      openBoot(managed);
      const t0 = performance.now(), secs = () => ((performance.now() - t0) / 1000).toFixed(1) + 's';
      let current = managed ? 'alloc' : 'ready';
      try {
          await api.enter(code, {
            shareCode,
            signal: controller.signal,
            onStep(k, leaseState) {
              if (k !== current) { step(current, 'done', secs()); current = k; }
              if (k === 'start') leased = true;
              step(k, 'run', leaseState === 'pending' ? '启动中' : undefined);
            },
          });
      } catch (err) {
          if (err.name === 'AbortError') return;
            boot.hidden = true; start.disabled = false;
            if (err.status === 503 && managed && current === 'alloc') {
              sessionStorage.setItem(RETRY_KEY, String(err.retryAfter || 15));
              say('槽位已满，请稍后重试；有系统邀请码时可优先分配', true);
              return;
            }
            if (err.status === 401 || err.status === 403) {
              needInvite = true; $('#invite-field').hidden = false;
              say(err.message || '邀请码无效或已失效', true);
              invite.select();
              return;
            }
            if (err.status === 429) { say('同时在线的会话已达上限，请稍后再试', true); return; }
            say(err.message, true);
            return;
      }
      step(current, 'done', secs());
      setText('#boot-msg', '就绪，正在进入…');
      location.href = 'workspace.html';
    };

    await probe();
    const shared = new URLSearchParams(location.search).get('share');
    if (shared && shareInput) { shareInput.value = shared; history.replaceState(null, '', location.pathname); }
    refreshCapacity();
    setInterval(refreshCapacity, 10000);
    // Arriving from busy.html with a reserved lease: continue automatically when no invite is needed.
    if (new URLSearchParams(location.search).has('continue')) {
      if (needInvite) { say('工作区已为你保留，输入体验码继续。'); invite.focus(); }
      else $('#gate').requestSubmit();
    }
  }

  /* ---------------- busy.html ---------------- */
  function busyPage() {
    const retry = $('#retry'), auto = $('#auto');
    let retryAfter = Number(sessionStorage.getItem(RETRY_KEY)) || 15;
    let left = retryAfter, tries = 0, inflight = false;
    const baseTitle = document.title;
    const paintCount = () => {
      setText('#count', fmtClock(left));
      $('#track').style.width = `${100 * (1 - left / retryAfter)}%`;
    };
    const label = () => setText('#count-label', auto.checked ? '距离下次自动重试' : '自动重试已暂停');

    async function attempt() {
      if (inflight) return;
      inflight = true;
      retry.disabled = true; retry.textContent = '正在请求…';
      setText('#count-label', '正在请求工作区');
      tries += 1; setText('#d-tries', `${tries} 次`);
      const stamp = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });
      try {
        if (await api.detect()) { location.href = 'index.html?continue=1'; return; }
        else await api.get('/health/ready', { auth: false });
        setText('#d-last', `${stamp()} · 可用`);
        document.body.classList.add('is-ok');
        document.title = '(可用) ' + baseTitle;
        sessionStorage.removeItem(RETRY_KEY);
        setTimeout(() => { location.href = 'index.html?continue=1'; }, 1200);
      } catch (e) {
        setText('#d-last', `${stamp()} · ${e.status || '网络错误'}`);
        if (e.status && e.status !== 503 && e.status !== 429) toast('无法接入：' + e.message, { ms: 5000 });
        retryAfter = e.retryAfter || retryAfter;
        left = retryAfter;
        inflight = false;
        retry.disabled = false; retry.textContent = '立即重试';
        label(); paintCount(); refreshCapacity();
      }
    }
    retry.onclick = attempt;
    auto.onchange = label;
    setInterval(() => {
      if (!auto.checked || inflight) return;
      left = Math.max(0, left - 1);
      paintCount();
      if (left === 0) attempt();
    }, 1000);
    paintCount(); label(); refreshCapacity();
  }
})();
