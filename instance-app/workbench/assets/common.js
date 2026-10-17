/* TutorLM v2 · shared runtime
   - Icon sprite (inline SVG, no CDN)
   - Toast
   - Lease storage + MockControlPlane (mirrors multi-instance/control-plane contract)
   No backend is required: every call resolves locally with realistic latency. */
(function () {
  'use strict';

  /* ---------- Icons ---------- */
  const ICONS = {
    'file': '<path d="M5 2.5h6l4 4v11H5z"/><path d="M11 2.5v4h4"/>',
    'file-code': '<path d="M5 2.5h6l4 4v11H5z"/><path d="M11 2.5v4h4"/><path d="M8.5 11 7 12.5 8.5 14M11.5 11l1.5 1.5-1.5 1.5"/>',
    'file-pdf': '<path d="M5 2.5h6l4 4v11H5z"/><path d="M11 2.5v4h4"/><path d="M7.5 14.5h5M7.5 11.5h5"/>',
    'folder': '<path d="M2.5 5.5h5l1.5 1.5h8.5v9.5h-15z"/>',
    'folder-plus': '<path d="M2.5 5.5h5l1.5 1.5h8.5v9.5h-15z"/><path d="M10 9.5v4M8 11.5h4"/>',
    'file-plus': '<path d="M5 2.5h6l4 4v11H5z"/><path d="M10 9.5v5M7.5 12h5"/>',
    'upload': '<path d="M10 13V3.5M6.5 7 10 3.5 13.5 7M3.5 13v3.5h13V13"/>',
    'collapse': '<path d="M6 8l4-4 4 4M6 16l4-4 4 4"/>',
    'chev-r': '<path d="M8 5l5 5-5 5"/>',
    'chev-d': '<path d="M5 8l5 5 5-5"/>',
    'chev-ud': '<path d="M7 8l3-3 3 3M7 12l3 3 3-3"/>',
    'search': '<circle cx="9" cy="9" r="5.5"/><path d="M13 13l4 4"/>',
    'cursor': '<path d="M5 3l10 6-4.5 1.5L9 15z"/>',
    'marker': '<path d="M12.5 3.5l4 4-7 7h-4v-4z"/><path d="M3.5 17h7"/>',
    'comment': '<path d="M3.5 4h13v9h-8l-3.5 3v-3h-1.5z"/>',
    'list': '<path d="M7 5.5h10M7 10h10M7 14.5h10M3.5 5.5h.5M3.5 10h.5M3.5 14.5h.5"/>',
    'expand': '<path d="M3.5 8V3.5H8M12 3.5h4.5V8M16.5 12v4.5H12M8 16.5H3.5V12"/>',
    'shrink': '<path d="M8 3.5V8H3.5M16.5 8H12V3.5M12 16.5V12h4.5M3.5 12H8v4.5"/>',
    'download': '<path d="M10 3.5V13M6.5 9.5 10 13l3.5-3.5M3.5 16.5h13"/>',
    'minus': '<path d="M4 10h12"/>',
    'plus': '<path d="M10 4v12M4 10h12"/>',
    'fit': '<path d="M3.5 7V3.5H7M13 3.5h3.5V7M16.5 13v3.5H13M7 16.5H3.5V13"/><rect x="7" y="7" width="6" height="6"/>',
    'layout': '<rect x="3" y="4" width="5" height="4"/><rect x="12" y="4" width="5" height="4"/><rect x="7.5" y="12" width="5" height="4"/><path d="M5.5 8v2h9V8M10 10v2"/>',
    'sparkle': '<path d="M10 3v4M10 13v4M3 10h4M13 10h4M5.5 5.5l2 2M12.5 12.5l2 2M14.5 5.5l-2 2M7.5 12.5l-2 2"/>',
    'copy': '<rect x="6.5" y="6.5" width="10" height="10"/><path d="M3.5 13.5v-10h10"/>',
    'scissors': '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="14" r="2.5"/><path d="M8 7.5 16.5 15M8 12.5 16.5 5"/>',
    'node': '<rect x="3.5" y="5.5" width="13" height="9"/><path d="M6.5 8.5h7M6.5 11.5h4"/>',
    'close': '<path d="M5 5l10 10M15 5 5 15"/>',
    'check': '<path d="M4 10.5l4 4 8-9"/>',
    'bold': '<path d="M6 4h5a3 3 0 0 1 0 6H6zM6 10h6a3 3 0 0 1 0 6H6z"/>',
    'sigma': '<path d="M15 4H5l5 6-5 6h10"/>',
    'code': '<path d="M7 6 3 10l4 4M13 6l4 4-4 4"/>',
    'undo': '<path d="M7 5 3.5 8.5 7 12"/><path d="M3.5 8.5H12a4.5 4.5 0 0 1 0 9H8"/>',
    'redo': '<path d="M13 5l3.5 3.5L13 12"/><path d="M16.5 8.5H8a4.5 4.5 0 0 0 0 9h4"/>',
    'send': '<path d="M3 10h12M11 6l4 4-4 4"/>',
    'trash': '<path d="M4 6h12M8 6V4h4v2M6 6l1 10.5h6L14 6"/>',
    'info': '<rect x="3.5" y="3.5" width="13" height="13"/><path d="M10 9v4.5M10 6.5v.5"/>',
    'mic': '<rect x="7.5" y="3" width="5" height="9"/><path d="M5 10a5 5 0 0 0 10 0M10 15v2.5"/>',
    'narrate': '<path d="M4 4.5h12M4 8.5h12M4 12.5h7"/><path d="M14 12v4.5l3-2.25z"/>',
    'arrow-r': '<path d="M3.5 10h13M12 5.5l4.5 4.5-4.5 4.5"/>',
    'arrow-l': '<path d="M16.5 10h-13M8 5.5 3.5 10 8 14.5"/>',
    'refresh': '<path d="M16 10a6 6 0 1 1-1.8-4.3"/><path d="M16 3.5v3.5h-3.5"/>',
    'panel-l': '<rect x="3" y="4" width="14" height="12"/><path d="M8 4v12"/>',
    'panel-r': '<rect x="3" y="4" width="14" height="12"/><path d="M12 4v12"/>',
    'logout': '<path d="M8 3.5H3.5v13H8M12 6.5 15.5 10 12 13.5M15.5 10H7"/>',
    'clock': '<rect x="3.5" y="3.5" width="13" height="13"/><path d="M10 6.5V10l2.5 2"/>',
    'stop': '<rect x="5.5" y="5.5" width="9" height="9"/>',
    'share': '<circle cx="5.5" cy="10" r="2"/><circle cx="14.5" cy="5" r="2"/><circle cx="14.5" cy="15" r="2"/><path d="M7.3 9.1 12.7 5.9M7.3 10.9 12.7 14.1"/>',
    'sun': '<circle cx="10" cy="10" r="3.5"/><path d="M10 2.5v1.8M10 15.7v1.8M2.5 10h1.8M15.7 10h1.8M4.7 4.7l1.3 1.3M14 14l1.3 1.3M15.3 4.7 14 6M6 14l-1.3 1.3"/>',
    'moon': '<path d="M15.5 11.5A6 6 0 0 1 8.5 4.5a6 6 0 1 0 7 7z"/>',
    'more': '<path d="M5 10h.5M10 10h.5M15 10h.5" stroke-width="2.5"/>',
    'settings': '<circle cx="10" cy="10" r="2.6"/><path d="M10 2.8v2M10 15.2v2M2.8 10h2M15.2 10h2M4.9 4.9l1.4 1.4M13.7 13.7l1.4 1.4M15.1 4.9l-1.4 1.4M6.3 13.7l-1.4 1.4"/>',
    'follow': '<rect x="3" y="3.5" width="6" height="13" rx="1"/><rect x="11" y="3.5" width="6" height="13" rx="1"/><path d="M6 7.5v5M14 7.5v5M5 11.5l1 1 1-1M13 11.5l1 1 1-1"/>',
    'italic': '<path d="M9 4h6M5 16h6M12 4 8 16"/>',
    'chat': '<path d="M3.5 4.5h13v8.5h-7l-3.5 3v-3h-2.5z"/><path d="M7 8.5h6"/>',
    'wand': '<path d="M4 16 13 7M11.5 5.5l3 3"/><path d="M15 2.5v2M17.5 5h-2M15.5 9.5v1.5M9 2.5v1.5"/>',
    'chev-l': '<path d="M12 5 7 10l5 5"/>',
    'pen': '<path d="M13.5 3.5l3 3L7.5 15.5l-4 1 1-4z"/><path d="M11.5 5.5l3 3"/>',
    'brush': '<path d="M11 3.5l5.5 5.5-5 5L6 8.5z"/><path d="M6 8.5 3.5 16.5l8-2.5"/>',
    'highlighter': '<path d="M7 11.5 13.5 4l3 3L9 13.5z"/><path d="M7 11.5 4.5 16h3L9 13.5"/><path d="M11 16.5h5.5"/>',
    'eraser': '<path d="M3.5 12.5 10.5 5.5l6 6-5 5h-4z"/><path d="M7 9l6 6M10.5 16.5h6"/>',
    'globe': '<circle cx="10" cy="10" r="6.5"/><path d="M3.5 10h13M10 3.5c-2.2 2.3-2.2 10.7 0 13M10 3.5c2.2 2.3 2.2 10.7 0 13"/>'
  };

  function injectSprite() {
    if (document.getElementById('tlm-sprite')) return;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.id = 'tlm-sprite';
    svg.setAttribute('aria-hidden', 'true');
    svg.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden';
    svg.innerHTML = Object.entries(ICONS)
      .map(([k, v]) => `<symbol id="i-${k}" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="square">${v}</symbol>`)
      .join('');
    document.body.prepend(svg);
  }
  const icon = (name, cls = '') => `<svg class="ic ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

  /* ---------- Toast ---------- */
  function toast(msg, opts = {}) {
    let host = document.querySelector('.toast-host');
    if (!host) {
      host = document.createElement('div');
      host.className = 'toast-host';
      host.setAttribute('role', 'status');
      host.setAttribute('aria-live', 'polite');
      document.body.appendChild(host);
    }
    const t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    if (opts.action) {
      t.style.pointerEvents = 'auto';
      const b = document.createElement('button');
      b.textContent = opts.action.label;
      b.style.cssText = 'margin-left:14px;background:none;border:0;color:inherit;text-decoration:underline;cursor:pointer;padding:0';
      b.onclick = () => { opts.action.run(); t.remove(); };
      t.appendChild(b);
    }
    host.appendChild(t);
    setTimeout(() => t.remove(), opts.ms || 2400);
  }

  /* ---------- Helpers ---------- */
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const uid = (p = 'id') => p + '_' + Math.random().toString(36).slice(2, 9);
  const fmtClock = (sec) => {
    sec = Math.max(0, Math.floor(sec));
    return String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
  };

  /* ---------- Theme (day / night) ---------- */
  const THEME_KEY = 'tlm.theme';
  const prefersDark = () => { try { return matchMedia('(prefers-color-scheme: dark)').matches; } catch { return false; } };
  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || (prefersDark() ? 'dark' : 'light');
  }
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    return t;
  }
  function setTheme(t) {
    try { localStorage.setItem(THEME_KEY, t); } catch { /* storage disabled */ }
    return applyTheme(t);
  }
  function toggleTheme() { return setTheme(currentTheme() === 'dark' ? 'light' : 'dark'); }
  /* Mode = 'light' | 'dark' | 'system'. 'system' clears the stored choice and
     tracks prefers-color-scheme live (the inline pre-paint script does the same). */
  function themeMode() { try { return localStorage.getItem(THEME_KEY) || 'system'; } catch { return 'system'; } }
  function setThemeMode(m) {
    if (m === 'light' || m === 'dark') return setTheme(m);
    try { localStorage.removeItem(THEME_KEY); } catch { /* storage disabled */ }
    return applyTheme(prefersDark() ? 'dark' : 'light');
  }
  try {
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (themeMode() === 'system') { applyTheme(prefersDark() ? 'dark' : 'light'); syncThemeButton(); }
    });
  } catch { /* old browsers */ }
  const theme = { get: currentTheme, set: setTheme, toggle: toggleTheme, apply: applyTheme, key: THEME_KEY, mode: themeMode, setMode: setThemeMode };

  window.TLM = { icon, injectSprite, toast, sleep, esc, uid, fmtClock, theme };

  /* Wire the day/night toggle wherever a #theme-btn exists (all pages), and
     keep its icon in sync with the active theme. */
  function syncThemeButton() {
    const btn = document.getElementById('theme-btn');
    if (!btn) return;
    const dark = theme.get() === 'dark';
    const use = btn.querySelector('use');
    if (use) use.setAttribute('href', dark ? '#i-sun' : '#i-moon');
    btn.setAttribute('aria-pressed', String(dark));
    btn.title = dark ? '切换到日间' : '切换到夜间';
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', () => { theme.toggle(); syncThemeButton(); });
  }
  function onReady() {
    injectSprite();
    syncThemeButton();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', onReady);
  else onReady();
})();
