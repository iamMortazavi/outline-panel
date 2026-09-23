/* Outline Panel — the dashboard.

   Plain script, no build step: the panel is installed by `install.sh` onto a
   bare VPS, and a toolchain there is a toolchain to break. Layout and colour
   live in organic.css; this file only decides *what* is on screen.

   One rule runs through all of it: the server enforces every permission on its
   own. Hiding a control here spares someone a button that would 403 — it is
   never the boundary. */
'use strict';

/* ================================================================ helpers */
const $ = s => document.querySelector(s);
const root = $('#root'), modalRoot = $('#modalRoot');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tx = (s, v) => esc(t(s, v));        // translated *and* escaped, for markup
// localStorage throws in some private modes; a preference that cannot be read
// must not take the whole dashboard down with it.
const store = {
  get(k, d){ try { const v = localStorage.getItem(k); return v == null ? d : v; } catch(_) { return d; } },
  set(k, v){ try { localStorage.setItem(k, v); } catch(_) { /* not persisted */ } },
};
// One id per purchase *intent*, not per call. The server replays the first
// answer for a repeat, so a create/renew whose response was lost can be sent
// again without charging twice — but only if the retry carries the same id,
// which is why it is minted where the user decides, not where fetch runs.
const idemKey = () => (crypto.randomUUID ? crypto.randomUUID()
  : String(Date.now()) + Math.random().toString(16).slice(2));

/* Lucide geometry at the design's 2.75 stroke. */
const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  link: '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
  qr: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><path d="M14 14h3M20 14v3M14 20h3"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  warn: '<path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  down: '<path d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 6.6 19l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3 12.6H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 6"/>',
  shield: '<path d="M12 3l8 4v5c0 4.5-3 7.5-8 9-5-1.5-8-4.5-8-9V7z"/>',
  keyb: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M8 13h8"/>',
  out: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  server: '<rect x="3" y="4" width="18" height="8" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 8h.01M7 17h.01"/>',
  filter: '<path d="M3 4h18M6 12h12M10 20h4"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
};
const ic = (name, size = 16) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
const tick = `<svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 6.5l2.5 2.5L10 3"/></svg>`;
const caret = `<svg width="11" height="11" viewBox="0 0 12 8" fill="none" aria-hidden="true" style="opacity:.6;flex:none"><path d="M1 1l5 5 5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;

/* The one fetch. `quiet` is for background polls: they must not toast a 403
   every five seconds at an admin who was never granted what they poll. */
async function api(path, o = {}) {
  const { idem, quiet, ...rest } = o;
  let r;
  try {
    r = await fetch(path, { credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(idem ? { 'Idempotency-Key': idem } : {}) }, ...rest });
  } catch (_) {
    throw Object.assign(new Error(t('Network error — check your connection.')), { code: 'net' });
  }
  let d = null;
  try { d = await r.json(); } catch (_) { /* empty or non-JSON body */ }
  const err = () => Object.assign(new Error(d ? I18N.tErr(d) : ('HTTP ' + r.status)),
    { code: d && d.code, status: r.status });
  // An expired or revoked session anywhere — polls included — goes back to
  // the sign-in screen once, instead of every panel quietly emptying.
  if (r.status === 401) {
    if (S.screen !== 'login') { stopPolling(); showLogin(); }
    throw err();
  }
  if (!r.ok) {
    const e = err();
    if (r.status === 403 && !quiet) toast(e.message, 1);
    throw e;
  }
  return d;
}

let toastT;
function toast(msg, bad) {
  const el = $('#toast');
  el.className = bad ? 'bad' : '';
  el.innerHTML = '<span class="td"></span><span>' + esc(msg) + '</span>';
  el.style.display = 'flex';
  el.setAttribute('role', bad ? 'alert' : 'status');
  clearTimeout(toastT);
  toastT = setTimeout(() => { el.style.display = 'none'; }, bad ? 4200 : 2400);
}
// Every destructive step goes through here, translated — this is the one
// sentence in the whole UI that must be understood before clicking.
const ask = (s, v) => confirm(t(s, v));

const locale = () => (I18N.lang === 'fa' ? 'fa-IR' : 'en-GB');
// A figure with a Latin unit inside a Persian sentence gets reordered by the
// bidi algorithm — "320 GB" came out "GB 320", "31.7 GB of 100 GB" as a
// jumble. An isolate keeps each figure one left-to-right unit; in an LTR page
// it is a no-op, so only RTL pays for the invisible characters.
const iso = s => (I18N.isRTL() ? '\u2066' + s + '\u2069' : s);
function rawBytes(n) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; let i = 0, x = Number(n);
  while (Math.abs(x) >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return (i >= 2 && x < 100 ? x.toFixed(1) : Math.round(x)) + ' ' + u[i];
}
const fmtBytes = n => (n == null ? '—' : iso(rawBytes(n)));
// "/s" inside the same isolate, or RTL puts it in front: "s/6.6 MB".
const fmtRate = n => iso(rawBytes(n || 0) + '/s');
const fmtDate = ts => ts ? new Date(ts * 1000).toLocaleDateString(locale(),
  { year: 'numeric', month: 'short', day: 'numeric' }) : '—';
const fmtDateTime = ts => ts ? new Date(ts * 1000).toLocaleString(locale(),
  { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
function fmtAgo(sec) {
  if (sec == null) return t('never');
  if (sec < 90) return t('just now');
  const m = Math.floor(sec / 60); if (m < 60) return t('{n}m ago', { n: m });
  const h = Math.floor(m / 60); if (h < 48) return t('{n}h ago', { n: h });
  return t('{n}d ago', { n: Math.floor(h / 24) });
}
function fmtDur(sec) {
  if (!sec) return '0h';
  const h = Math.floor(sec / 3600);
  return h < 48 ? h + t('h') : Math.floor(h / 24) + t('d');
}
const now = () => Date.now() / 1000;
const fmtMoney = n => iso(Number(n || 0).toLocaleString('en-US') + ' ' + (S.currency || 'T'));
function flag(cc) {
  if (!cc || cc.length !== 2) return '🌐';
  return String.fromCodePoint(...[...cc.toUpperCase()].map(c => 0x1F1E6 + c.charCodeAt(0) - 65));
}
let _rn;
function countryName(cc) {
  if (!cc || cc.length !== 2) return cc || t('Unknown');
  try { _rn = _rn || new Intl.DisplayNames([I18N.lang], { type: 'region' }); return _rn.of(cc.toUpperCase()) || cc; }
  catch (_) { return cc; }
}
// A clean ss:// with the key's name as its label — Outline's own URL carries a
// /?outline=1 path some clients choke on.
function tagUrl(k) {
  const u = k.accessUrl || '', m = u.match(/^(ss:\/\/[^@]+@[^\/?#]+)/);
  return (m ? m[1] : u.split(/[\/?#]/)[0]) + (k.name ? '#' + encodeURIComponent(k.name.replace(/\s+/g, '')) : '');
}
const srv = id => S.servers.find(x => x.id === id);
const srvName = id => (srv(id) || {}).name || id;

/* =================================================================== state */
const S = {
  screen: 'login', route: 'users', servers: [], keys: [], errors: [], stats: null, me: null,
  admins: [], health: null, audit: null, currency: 'T',
  srvFilter: 'all', statusFilter: 'all', adminFilter: 'all',
  sort: store.get('oc_sort', 'recent'), search: '', selected: {}, srvMenuOpen: false,
  bwSamples: [], bannerDismissed: false, mobile: false,
};
const can = c => !!(S.me && S.me.caps.includes(c));
const isOwner = () => !!(S.me && S.me.isOwner);
const onCredit = () => !!(S.me && S.me.creditEnabled);

// Routes, and who may see each. The hash carries the route so reload and the
// back button keep your place — the old panel always reopened on the list.
const ROUTES = {
  users:    { label: 'Users',        ok: () => can('keys.view') },
  servers:  { label: 'Servers',      ok: () => true },
  admins:   { label: 'Admins',       ok: isOwner, parent: 'settings' },
  activity: { label: 'Activity log', ok: isOwner },
  settings: { label: 'Settings',     ok: () => true },
  bot:      { label: 'Telegram bot', ok: () => can('bot.manage'), parent: 'settings' },
  packages: { label: 'Packages',     ok: isOwner, parent: 'settings' },
};
const defaultRoute = () => (can('keys.view') ? 'users' : 'servers');
function routeFromHash() {
  const r = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
  return ROUTES[r] && ROUTES[r].ok() ? r : defaultRoute();
}
function go(route) {
  if (!ROUTES[route] || !ROUTES[route].ok()) route = defaultRoute();
  S.srvMenuOpen = false;
  if (S.route !== route) { S.search = ''; S.selected = {}; }
  S.route = route;
  const h = '#/' + route;
  if (location.hash !== h) history.pushState(null, '', h);
  renderApp();
  window.scrollTo(0, 0);
}
addEventListener('popstate', () => { if (S.screen === 'app') { S.route = routeFromHash(); S.search = ''; renderApp(); } });

I18N.apply();
const nextLang = () => I18N.languages.find(x => x.id !== I18N.lang);
function toggleLang() {
  const l = nextLang(); if (!l) return;
  I18N.setLang(l.id);
  _rn = null;
  S.screen === 'login' ? showLogin() : renderApp();
}

/* ==================================================================== auth */
async function checkAuth() {
  try { await api('/api/me', { quiet: true }); await enterApp(); }
  catch (_) { showLogin(); }
}
function showLogin(needTotp) {
  S.screen = 'login'; stopPolling(); closeModal();
  document.body.classList.remove('is-mobile');
  root.innerHTML = `<div class="login">
    <span class="blob" style="width:420px;height:420px;background:var(--color-accent-200);top:-160px;inset-inline-end:-120px"></span>
    <span class="blob" style="width:300px;height:300px;background:var(--color-accent-2-200);bottom:-120px;inset-inline-start:-90px"></span>
    <div class="login-box">
      <div class="brand"><span class="brand-mark">O</span><span class="brand-name">Outline</span></div>
      <h1>${tx('Welcome back.')}</h1>
      <p class="sub">${tx('Sign in to manage your servers and keys.')}</p>
      <div class="field"><label for="un">${tx('Username')}</label>
        <input id="un" class="input" value="admin" autocomplete="username" autocapitalize="off" spellcheck="false"></div>
      <div class="field"><label for="pw">${tx('Password')}</label>
        <input id="pw" class="input" type="password" placeholder="••••••••••" autocomplete="current-password"></div>
      <div id="totpWrap" class="field ${needTotp ? '' : 'hidden'}"><label for="totp">${tx('Two-factor code')}</label>
        <input id="totp" class="input" inputmode="numeric" autocomplete="one-time-code" placeholder="000000"></div>
      <div id="loginErr" class="err" role="alert"></div>
      <button class="btn btn-primary btn-block btn-lg" id="loginBtn">${tx('Sign in →')}</button>
      <button id="langsw" class="btn btn-ghost" style="align-self:center;margin-top:var(--space-3)">${esc(nextLang().label)}</button>
    </div></div>`;
  $('#langsw').onclick = toggleLang;
  $('#loginBtn').onclick = doLogin;
  $('#un').addEventListener('keydown', e => { if (e.key === 'Enter') $('#pw').focus(); });
  $('#pw').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  $('#totp').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  (needTotp ? $('#totp') : $('#pw')).focus();
}
async function doLogin() {
  const un = $('#un').value.trim(), pw = $('#pw').value, code = $('#totp').value.trim(), b = $('#loginBtn');
  $('#loginErr').textContent = ''; b.disabled = true; b.innerHTML = '<span class="spin"></span>';
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ username: un, password: pw, totp: code || null }) });
    await enterApp();
  } catch (e) {
    b.disabled = false; b.textContent = t('Sign in →');
    // By code, not by message: the message is translated, and matching the
    // English sentence meant a Persian-speaking admin with 2FA never got the
    // code field at all.
    if (e.code === 'auth.totp_required') {
      $('#totpWrap').classList.remove('hidden'); $('#totp').focus();
      $('#loginErr').textContent = t('Enter your two-factor code.');
    } else {
      $('#loginErr').textContent = e.message;
    }
  }
}
async function signOut() { try { await api('/api/logout', { method: 'POST' }); } catch (_) {} showLogin(); }

/* ==================================================================== data */
async function loadAll() {
  // /me and servers are critical; stats and keys are best-effort, so one slow
  // server cannot blank the dashboard — and an admin without keys.view (whose
  // /api/keys would 403) still gets a working shell rather than an error.
  const me = await api('/api/me');
  S.me = me;                       // re-read every load: caps can change mid-session
  const want = can('keys.view');
  const [sv, st, ks] = await Promise.allSettled([
    api('/api/servers'),
    want ? api('/api/stats', { quiet: true }) : Promise.resolve(null),
    want ? api('/api/keys', { quiet: true }) : Promise.resolve({ keys: [], errors: [] }),
  ]);
  if (sv.status !== 'fulfilled') throw sv.reason || new Error(t('Failed to load servers'));
  S.servers = sv.value.servers || [];
  if (st.status === 'fulfilled' && st.value) { S.stats = st.value; pushBw(); }
  if (ks.status === 'fulfilled') { S.keys = ks.value.keys || []; S.errors = ks.value.errors || []; }
  else { S.keys = []; S.errors = []; }
  pruneSelection();
}
let _keysBusy = false, _statsBusy = false;
async function refreshKeys() {
  if (_keysBusy || !can('keys.view')) return; _keysBusy = true;
  try { const d = await api('/api/keys', { quiet: true }); S.keys = d.keys || []; S.errors = d.errors || []; pruneSelection(); }
  catch (_) {} finally { _keysBusy = false; }
}
async function refreshStats() {
  if (_statsBusy || !can('keys.view')) return; _statsBusy = true;
  try { S.stats = await api('/api/stats', { quiet: true }); pushBw(); } catch (_) {} finally { _statsBusy = false; }
}
// A selection that outlives its keys (deleted elsewhere) must not linger and
// then be acted on.
function pruneSelection() {
  const live = new Set(S.keys.map(selKey));
  Object.keys(S.selected).forEach(s => { if (!live.has(s)) delete S.selected[s]; });
}
let _lastBwTs = null;
function pushBw() {
  if (!(S.stats && S.stats.available)) return;
  const ts = S.stats.bwTs, v = S.stats.bwCurrent || 0;
  // Outline refreshes bandwidth about once a minute; only a genuinely new
  // sample becomes a point, or the line is a flat run of duplicates.
  if (ts != null) { if (ts === _lastBwTs) return; _lastBwTs = ts; }
  else if (S.bwSamples.length && S.bwSamples[S.bwSamples.length - 1] === v) return;
  S.bwSamples.push(v); if (S.bwSamples.length > 60) S.bwSamples.shift();
}

// Poll-side change detection: the DOM is touched only when the data behind it
// moved, so a five-second tick with nothing new costs no re-render.
let pollT = null, _sigStats = '', _sigKeys = '';
const statsSig = () => JSON.stringify(S.stats || 0);
const keysSig = () => S.keys.map(k => [k.serverId, k.id, k.used, k.limit || 0, k.disabled ? 1 : 0,
  k.pending ? 1 : 0, k.lastSeen || 0, k.expiry || 0, k.name, k.peakDevices || 0, k.ownerAdminId || 0].join(':')).join('|');
function stopPolling() { if (pollT) { clearInterval(pollT); pollT = null; } }
function startPolling() {
  stopPolling();
  _sigStats = statsSig(); _sigKeys = keysSig();
  let n = 0;
  pollT = setInterval(async () => {
    if (document.hidden || S.screen !== 'app') return;   // don't fan out to every server while hidden
    await refreshStats();
    const ss = statsSig();
    const statsMoved = ss !== _sigStats; _sigStats = ss;
    let keysMoved = false;
    if (++n % 6 === 0) { await refreshKeys(); const ks = keysSig(); keysMoved = ks !== _sigKeys; _sigKeys = ks; }
    if (!statsMoved && !keysMoved) return;
    if (modalRoot.innerHTML) { if (keysMoved) renderNav(); return; }   // never repaint under an open sheet
    if (S.route === 'users') { renderKpis(); if (keysMoved) renderList(); }
    else if (S.route === 'servers') renderPage();
    if (keysMoved) renderNav();
  }, 5000);
}
async function enterApp() {
  S.screen = 'app';
  root.innerHTML = '<div style="min-height:100dvh;display:grid;place-items:center"><span class="spin"></span></div>';
  try { await loadAll(); }
  catch (e) {
    if (e.status === 401) return;               // api() already went back to sign-in
    root.innerHTML = `<div class="login"><div class="login-box" style="text-align:center;align-items:center">
      <div class="empty" style="width:100%"><span class="blobmark">!</span><h3>${tx("Couldn't load the dashboard")}</h3>
      <p>${esc(e.message || t('Network error — check your connection.'))}</p>
      <button class="btn btn-primary" id="retry">${tx('Retry')}</button></div></div></div>`;
    $('#retry').onclick = () => enterApp();
    return;
  }
  // Currency is an owner knob; resellers just see the default label.
  if (isOwner()) api('/api/settings/panel', { quiet: true }).then(d => { S.currency = (d.values || {}).currency || 'T'; renderKpis(); }).catch(() => {});
  S.route = routeFromHash();
  if (location.hash !== '#/' + S.route) history.replaceState(null, '', '#/' + S.route);
  renderApp();
  startPolling();
  loadOwnerExtras();
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && S.screen === 'app') refreshStats().then(() => { if (S.route === 'users') renderKpis(); });
});

/* ================================================================ derived */
const selKey = k => k.serverId + '/' + k.id;
// Outline numbers keys per server, so "15" names a different customer on each
// one: every lookup carries the server id as well.
const keyOf = d => S.keys.find(k => k.serverId === d.sid && String(k.id) === String(d.id))
  // No server id on the element: fall back to the id alone. This used to call
  // itself, which on any element without data-sid never returned.
  || (d.sid ? null : S.keys.find(k => String(k.id) === String(d.id)));
function selectedKeys() {
  return Object.keys(S.selected).filter(s => S.selected[s]).map(s => {
    const i = s.indexOf('/');
    return S.keys.find(k => k.serverId === s.slice(0, i) && String(k.id) === s.slice(i + 1));
  }).filter(Boolean);
}
const isOnline = k => !k.disabled && !k.pending && k.lastSeen && (now() - k.lastSeen) < 300;
const agoSec = k => k.lastSeen ? Math.max(0, Math.floor(now() - k.lastSeen)) : null;
const pctOf = k => k.limit ? Math.min(100, Math.round(k.used / k.limit * 100)) : 0;
const WARN_WINDOW = 5 * 86400;
// One predicate for "needs attention", so the headline number and the list it
// filters to can never disagree: near the limit, or out of time.
const needsAttention = k => !k.pending && ((k.limit && k.used / k.limit >= 0.9)
  || (k.expiry && k.expiry - now() < WARN_WINDOW));
function statusOf(k) {
  if (k.pending) return { key: 'pending', label: t('Pending') };
  if (k.disabled) return { key: 'disabled', label: t('Disabled') };
  if (isOnline(k)) return { key: 'online', label: t('Online') };
  return { key: 'active', label: t('Active') };
}
function barFor(k) {
  const w = k.limit ? Math.max(pctOf(k), k.used ? 2 : 0) : 6;   // unlimited: a sliver, never "full"
  const col = k.disabled ? 'var(--color-neutral-400)'
    : (k.limit && pctOf(k) >= 90) ? 'var(--color-accent-600)'
    : !k.limit ? 'var(--color-accent-2-400)'
    : isOnline(k) ? 'var(--color-accent-2-500)' : 'var(--color-accent-400)';
  return `<span style="width:${w}%;background:${col}"></span>`;
}
function usageText(k) {
  if (k.pending && !k.used) return t('nothing used yet');
  return k.limit ? t('{used} of {limit} used', { used: fmtBytes(k.used), limit: fmtBytes(k.limit) })
                 : t('{used} · unlimited', { used: fmtBytes(k.used) });
}
function expiryOf(k) {
  const n = now();
  if (k.pending) return { short: t('starts on use'), date: t('{n}-day term', { n: k.durationDays || 0 }), urgent: false };
  if (!k.expiry) return { short: t('no expiry'), date: '—', urgent: false };
  const left = k.expiry - n;
  if (left <= 0) return { short: t('expired'), date: fmtDate(k.expiry), urgent: true };
  const d = Math.floor(left / 86400);
  return { short: d >= 1 ? t('{n}d left', { n: d }) : t('{n}h left', { n: Math.max(1, Math.floor(left / 3600)) }),
           date: fmtDate(k.expiry), urgent: left < WARN_WINDOW };
}
const lastText = k => k.pending ? '—' : (isOnline(k) ? t('now') : fmtAgo(agoSec(k)));

function scoped() {
  return S.keys.filter(k => (S.srvFilter === 'all' || k.serverId === S.srvFilter)
    && (S.adminFilter === 'all' || (S.adminFilter === 'owner' ? k.ownerAdminId == null
        : String(k.ownerAdminId) === String(S.adminFilter))));
}
function adminFilterName() {
  if (S.adminFilter === 'all') return null;
  if (S.adminFilter === 'owner') return (S.me && S.me.username) || 'admin';
  const a = S.admins.find(x => String(x.id) === String(S.adminFilter));
  return a ? a.username : '#' + S.adminFilter;
}
const STATUS_TEST = {
  all: () => true, online: isOnline, active: k => !k.disabled && !k.pending,
  pending: k => k.pending, disabled: k => k.disabled, attention: needsAttention,
};
function filteredKeys() {
  const q = S.search.trim().toLowerCase();
  const list = scoped().filter(STATUS_TEST[S.statusFilter] || STATUS_TEST.all)
    .filter(k => !q || (k.name || '').toLowerCase().includes(q) || String(k.id).includes(q)
      || (k.serverName || '').toLowerCase().includes(q) || (k.ownerName || '').toLowerCase().includes(q));
  const m = S.sort;
  return list.slice().sort((a, b) => {
    if (m === 'name') return (a.name || '').localeCompare(b.name || '');
    if (m === 'usage') return (b.used || 0) - (a.used || 0);
    if (m === 'expiry') return (a.expiry || Infinity) - (b.expiry || Infinity);
    if (m === 'devices') return (b.peakDevices || 0) - (a.peakDevices || 0);
    if (m === 'online') return (isOnline(b) - isOnline(a)) || ((b.lastSeen || 0) - (a.lastSeen || 0));
    if (m === 'lastseen') return (b.lastSeen || 0) - (a.lastSeen || 0);
    return (b.createdTs || 0) - (a.createdTs || 0) || ((parseInt(b.id) || 0) - (parseInt(a.id) || 0));
  });
}

/* =================================================================== shell */
// The design's own breakpoint: below 900px the stage is narrower than a laptop
// and the phone layout is the honest one.
const mq = matchMedia('(max-width: 899px)');
mq.addEventListener('change', () => { if (S.screen === 'app') renderApp(); });

function navCounts() {
  return { users: scoped().length, servers: S.servers.length, admins: S.admins.length || null };
}
function renderApp() {
  S.mobile = mq.matches;
  document.body.classList.toggle('is-mobile', S.mobile);
  if (!ROUTES[S.route] || !ROUTES[S.route].ok()) S.route = defaultRoute();
  root.innerHTML = S.mobile ? mobileShell() : desktopShell();
  renderNav(); renderTopbar(); renderPage();
}
function desktopShell() {
  return `<div class="shell">
    <nav class="sidebar" aria-label="${tx('Main')}">
      <div class="brand"><span class="brand-mark">O</span><span class="brand-name">Outline</span></div>
      <div id="nav" style="display:contents"></div>
      <div id="sideCard"></div>
      <button class="btn btn-secondary" data-act="signout" style="justify-content:flex-start;font-size:13.5px">${tx('Sign out')} · ${esc((S.me || {}).username || '')}</button>
    </nav>
    <main class="main" id="main">
      <header class="topbar" id="topbar"></header>
      <div id="page"></div>
    </main></div>`;
}
function mobileShell() {
  return `<div class="m-shell">
    <header class="m-head" id="topbar"></header>
    <main class="m-body" id="main"><div id="page"></div></main>
    <nav class="tabbar" aria-label="${tx('Main')}"><div class="tabs" id="nav"></div></nav>
  </div>`;
}
function renderNav() {
  const el = $('#nav'); if (!el) return;
  const c = navCounts();
  const cur = ROUTES[S.route].parent && S.mobile ? ROUTES[S.route].parent : S.route;
  if (S.mobile) {
    const tabs = ['users', 'servers', isOwner() ? 'activity' : null, 'settings'].filter(r => r && ROUTES[r].ok());
    el.innerHTML = tabs.map(r => `<button class="tab ${cur === r ? 'on' : ''}" data-act="go" data-id="${r}" ${cur === r ? 'aria-current="page"' : ''}>${tx(r === 'activity' ? 'Activity' : ROUTES[r].label)}</button>`).join('');
    return;
  }
  const item = (r, count) => ROUTES[r].ok() ? `<button class="nav-item ${S.route === r ? 'on' : ''}" data-act="go" data-id="${r}" ${S.route === r ? 'aria-current="page"' : ''}>
      <span class="nd"></span><span class="nl">${tx(ROUTES[r].label)}</span><span class="nc num">${count == null ? '' : count}</span></button>` : '';
  const operate = item('users', c.users) + item('servers', c.servers) + item('admins', c.admins) + item('activity');
  const configure = item('settings') + item('bot') + item('packages', S.packagesCount);
  el.innerHTML = (operate ? `<div class="nav-group"><div class="kicker">${tx('Operate')}</div>${operate}</div>` : '')
    + (configure ? `<div class="nav-group"><div class="kicker">${tx('Configure')}</div>${configure}</div>` : '');
  renderSideCard();
}
// Something true and useful where the design's mock has a scheduler card: a
// reseller's balance, or whether every server is answering.
function renderSideCard() {
  const el = $('#sideCard'); if (!el) return;
  if (onCredit()) {
    const c = S.me.credit || 0;
    el.innerHTML = `<div class="side-card ${c > 0 ? '' : 'warn'}"><span class="kicker ${c > 0 ? 'sage' : 'accent'}">${tx('My credit')}</span>
      <span class="big num">${esc(fmtMoney(c))}</span>
      <button class="btn btn-ghost btn-sm" data-act="myledger" style="align-self:flex-start;padding-inline:0">${tx('Statement')} ›</button></div>`;
    return;
  }
  const up = S.servers.filter(s => s.reachable).length, all = S.servers.length;
  if (!all) { el.innerHTML = ''; return; }
  const down = S.servers.filter(s => !s.reachable).map(s => s.name);
  el.innerHTML = `<div class="side-card ${down.length ? 'warn' : ''}"><span class="kicker ${down.length ? 'accent' : 'sage'}">${tx('Servers')}</span>
    <span class="big">${tx('{up} of {all} reachable', { up, all })}</span>
    <span class="small">${down.length ? tx('Not answering: {names}', { names: down.join(', ') }) : tx('Expiry, quotas and alerts run in the background.')}</span></div>`;
}
const SEARCH_HINT = { users: 'Search keys, servers, admins', servers: 'Search servers', activity: 'Search the log',
  settings: 'Search settings', admins: 'Search admins', packages: 'Search packages', bot: 'Search settings' };
function srvPickerHtml() {
  const sf = srv(S.srvFilter);
  const label = S.srvFilter === 'all' ? t('All servers') : (sf ? sf.name : t('All servers'));
  const down = S.srvFilter !== 'all' && sf && !sf.reachable;
  const count = S.srvFilter === 'all' ? S.servers.length : '';
  let menu = '';
  if (S.srvMenuOpen) {
    const it = (id, name, sub, dotDown, n) => `<button class="menu-item ${S.srvFilter === id ? 'on' : ''}" data-act="srvpick" data-id="${esc(id)}">
      <span class="dot ${dotDown ? 'down' : ''}"></span><span class="mi-t"><span class="mi-n" style="display:block">${esc(name)}</span>${sub ? `<span class="mi-s" style="display:block">${esc(sub)}</span>` : ''}</span><span class="num" style="font-size:12px;color:var(--color-neutral-600)">${n}</span></button>`;
    menu = `<div class="menu" role="menu">${it('all', t('All servers'), '', false, S.keys.length)}<div class="menu-sep"></div>`
      + S.servers.map(s => it(s.id, s.name, (s.host || '') + (s.reachable ? '' : ' · ' + t('unreachable')), !s.reachable,
        S.keys.filter(k => k.serverId === s.id).length)).join('') + '</div>';
  }
  return `<div style="position:relative;flex:none" id="srvwrap">
    <button class="pill-btn" data-act="srvmenu" aria-haspopup="menu" aria-expanded="${S.srvMenuOpen}">
      <span class="dot ${down ? 'down' : ''}"></span><span class="lbl">${esc(label)}</span>${count ? `<span class="cnt num">${count}</span>` : ''}${caret}</button>${menu}</div>`;
}
function renderTopbar() {
  const el = $('#topbar'); if (!el) return;
  const hint = t(SEARCH_HINT[S.route] || 'Search');
  const search = `<div class="search">${ic('search', 17)}<input id="search" class="input" data-inp="search" value="${esc(S.search)}" placeholder="${esc(hint)}" aria-label="${esc(hint)}">${S.mobile ? '' : '<kbd data-act="palette" title="⌘K">⌘K</kbd>'}</div>`;
  const newBtn = can('keys.create') && S.servers.length;
  if (S.mobile) {
    el.innerHTML = `<div class="row1"><span class="brand-mark">O</span><span class="brand-name">Outline</span>
      ${S.servers.length > 1 ? srvPickerHtml() : ''}
      ${newBtn ? `<button class="btn btn-primary btn-new" data-act="create" aria-label="${tx('New key')}">${ic('plus', 20)}</button>` : ''}</div>${search}`;
    return;
  }
  el.innerHTML = `${S.servers.length ? srvPickerHtml() : ''}${search}<span class="spacer"></span>
    ${S.route === 'users' && can('keys.view') ? `<button class="btn btn-secondary" data-act="csv">${tx('Export CSV')}</button>` : ''}
    ${newBtn ? `<button class="btn btn-primary" data-act="create" style="padding-inline:var(--space-4)">${ic('plus', 16)}${tx('New key')}</button>` : ''}`;
}
function renderPage() {
  const el = $('#page'); if (!el) return;
  const fn = { users: pageUsers, servers: pageServers, admins: pageAdmins, activity: pageActivity,
               settings: pageSettings, bot: pageBot, packages: pagePackages }[S.route];
  fn(el);
}
// The page heading, with a way back up on the phone where the sidebar is gone.
function pageHead(title, sub, extra) {
  const parent = S.mobile && ROUTES[S.route].parent;
  return `<div class="page-head ${extra ? 'row' : ''}"><div>
    ${parent ? `<button class="btn btn-ghost btn-sm" data-act="go" data-id="${parent}" style="padding-inline:0;margin-bottom:var(--space-2)">${ic('back', 16)}${tx(ROUTES[parent].label)}</button>` : ''}
    <h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ''}</div>${extra || ''}</div>`;
}
const emptyBox = (title, body, action) => `<div class="empty"><span class="blobmark">O</span><h3>${esc(title)}</h3>${body ? `<p>${esc(body)}</p>` : ''}${action || ''}</div>`;
const matches = (...parts) => { const q = S.search.trim().toLowerCase(); return !q || parts.some(p => String(p || '').toLowerCase().includes(q)); };

/* ============================================================ users page */
function pageUsers(el) {
  if (!can('keys.view')) { el.innerHTML = emptyBox(t('Users'), t('You have not been given access to see users.')); return; }
  el.innerHTML = `<div class="page">
    <div id="uhead"></div><div class="kpis" id="kpis"></div>
    <div class="toolbar" id="utool"></div><div id="banner"></div><div id="selbar"></div>
    <div id="list"></div><div class="foot-note" id="ufoot"></div></div>`;
  renderKpis(); renderList();
}
function headSentence(sc) {
  const n = sc.length, m = new Set(sc.map(k => k.serverId)).size || S.servers.length, a = sc.filter(needsAttention).length;
  if (!S.servers.length) return t('Connect an Outline server to start handing out keys.');
  if (!n) return t('No keys yet — create the first one.');
  const first = t(n === 1 ? 'One key across {m} servers.' : '{n} keys across {m} servers.', { n, m });
  return first + ' ' + (a ? t(a === 1 ? 'One needs a look before the week is out.' : '{a} need a look before the week is out.', { a })
                          : t('Nothing needs your attention.'));
}
function renderKpis() {
  const box = $('#kpis'); if (!box) return;
  const sc = scoped(), st = S.stats;
  const head = $('#uhead');
  if (head) head.innerHTML = pageHead(t('Your people'), esc(headSentence(sc)));
  const cnt = {
    all: sc.length, online: sc.filter(isOnline).length, active: sc.filter(STATUS_TEST.active).length,
    attention: sc.filter(needsAttention).length,
  };
  const card = (o) => {
    const tag = o.filter ? 'button' : 'div';
    const on = o.filter && o.filter !== 'all' && S.statusFilter === o.filter;
    return `<${tag} class="kpi ${o.tone || ''} ${on ? 'on' : ''}" ${o.filter ? `data-act="kpi" data-id="${o.filter}" aria-pressed="${on}"` : ''}>
      <span class="kl">${o.live ? '<span class="pulse"></span>' : ''}${esc(o.label)}</span>
      <span class="kv">${esc(o.val)}</span><span class="ks">${esc(o.sub)}</span></${tag}>`;
  };
  const avail = st && st.available;
  const credit = onCredit() ? { label: t('My credit'), val: fmtMoney(S.me.credit), tone: S.me.credit > 0 ? 'live' : 'attention',
    sub: t('what you can still sell') } : null;
  const list = [
    credit,
    { label: t('Total keys'), val: String(cnt.all), sub: t('across {m} servers', { m: S.srvFilter === 'all' ? S.servers.length : 1 }), filter: 'all' },
    { label: t('Online now'), val: String(cnt.online), sub: t('in the last 5 minutes'), tone: 'live', live: cnt.online > 0, filter: 'online' },
    { label: t('Active'), val: String(cnt.active), sub: t('not pending or disabled'), filter: 'active', desk: true },
    { label: t('Needs attention'), val: String(cnt.attention), sub: t('near the limit or out of time'), tone: 'attention', filter: 'attention' },
    { label: t('Transfer · 30d'), val: avail ? fmtBytes(st.dataBytes) : '—',
      sub: avail ? t('{v} right now', { v: fmtRate(st.bwCurrent) }) : t('metrics sharing is off') },
  ].filter(Boolean);
  // The phone gets a 2×2: the design drops "Active" there, and a reseller's
  // balance displaces the transfer figure, which is the owner's number anyway.
  const shown = S.mobile ? list.filter(k => !k.desk).slice(0, 4) : list;
  box.innerHTML = shown.map(card).join('');
}
function renderList() {
  renderToolbar(); renderBanner(); renderSelbar();
  const el = $('#list'); if (!el) return;
  const list = filteredKeys(), sc = scoped();
  const foot = $('#ufoot');
  if (foot) foot.textContent = (S.search || S.statusFilter !== 'all')
    ? t('{n} of {m} keys', { n: list.length, m: sc.length })
    : t(sc.length === 1 ? '1 key in total' : '{n} keys in total', { n: sc.length })
      + (S.mobile ? '' : ' · ' + t('click a row for the whole record, ⌘K to jump anywhere'));
  if (!S.servers.length) {
    el.innerHTML = emptyBox(t('No servers yet.'), t('Add an Outline server and its keys show up here.'),
      can('servers.manage') ? `<button class="btn btn-primary" data-act="addserver">${tx('Add your first server')}</button>` : '');
    return;
  }
  if (!list.length) {
    el.innerHTML = emptyBox(S.search ? t('Nothing matches “{q}”', { q: S.search }) : t('No keys here yet.'),
      S.statusFilter !== 'all' ? t('Try another filter.') : '',
      S.statusFilter !== 'all' || S.search ? `<button class="btn btn-secondary" data-act="clearfilters">${tx('Clear filters')}</button>`
        : (can('keys.create') ? `<button class="btn btn-primary" data-act="create">${ic('plus', 16)}${tx('New key')}</button>` : ''));
    return;
  }
  el.innerHTML = S.mobile ? cardList(list) : tableList(list);
}
function renderToolbar() {
  const tb = $('#utool'); if (!tb) return;
  const sc = scoped();
  const c = id => sc.filter(STATUS_TEST[id]).length;
  const pills = [['all', 'All'], ['online', 'Online'], ['active', 'Active'], ['pending', 'Pending'], ['disabled', 'Disabled']]
    .map(([id, l]) => `<button class="seg-btn ${S.statusFilter === id ? 'on' : ''}" data-act="statusf" data-id="${id}" aria-pressed="${S.statusFilter === id}">${tx(l)}<span class="c">${c(id)}</span></button>`).join('');
  const af = adminFilterName();
  const sorts = [['recent', 'Newest first'], ['name', 'Name A–Z'], ['usage', 'Most usage'], ['expiry', 'Expiring soon'],
    ['devices', 'Most devices'], ['online', 'Online first'], ['lastseen', 'Last seen']];
  tb.innerHTML = `${af ? `<button class="chip sel" data-act="adminfilter" data-id="all">${tx("{name}'s users", { name: af })} ✕</button>` : ''}
    ${S.statusFilter === 'attention' ? `<button class="chip sel" data-act="statusf" data-id="all">${tx('Needs attention')} ✕</button>` : ''}
    <div class="segs" role="group" aria-label="${tx('Status')}">${pills}</div><span class="spacer"></span>
    <select class="input" data-inp="sort" aria-label="${tx('Sort')}">${sorts.map(([v, l]) => `<option value="${v}" ${S.sort === v ? 'selected' : ''}>${tx(l)}</option>`).join('')}</select>`;
}
function renderBanner() {
  const b = $('#banner'); if (!b) return;
  const down = S.errors.filter(e => S.srvFilter === 'all' || e.serverId === S.srvFilter);
  if (!down.length || S.bannerDismissed) { b.innerHTML = ''; return; }
  b.innerHTML = `<div class="banner" role="status">${ic('warn', 18)}<span>${tx('Couldn’t reach {names} — its keys are hidden until it answers again.',
    { names: down.map(e => e.serverName || t('a server')).join(', ') })}</span><button class="x" data-act="dismissbanner" aria-label="${tx('Close')}">×</button></div>`;
}
function renderSelbar() {
  const el = $('#selbar'); if (!el) return;
  const picked = selectedKeys();
  if (!picked.length) { el.innerHTML = ''; return; }
  const b = (op, label, cls = 'btn-secondary') => `<button class="btn ${cls} btn-sm" data-act="bulk" data-op="${op}">${tx(label)}</button>`;
  el.innerHTML = `<div class="selbar"><span class="n">${tx('{n} selected', { n: picked.length })}<span class="bulkp num"></span></span><span class="spacer"></span>
    ${S.servers.length > 1 && can('keys.edit') ? b('servers', 'Servers…') : ''}
    ${b('links', 'Copy links')}
    ${can('keys.edit') && !onCredit() ? b('extend', 'Extend +30d') : ''}
    ${can('keys.edit') ? b('disable', 'Disable') : ''}
    ${isOwner() ? b('transfer', 'Transfer to…') : ''}
    ${can('keys.delete') ? b('delete', 'Delete', 'btn-primary') : ''}
    ${b('clear', 'Clear', 'btn-ghost')}</div>`;
}
function tableList(list) {
  const allOn = list.length && list.every(k => S.selected[selKey(k)]);
  const head = `<div class="krow head" role="row"><button class="chk ${allOn ? 'on' : ''}" data-act="selall" aria-label="${tx('Select all')}" aria-pressed="${!!allOn}">${tick}</button>
    <span>${tx('Key')}</span><span>${tx('Status')}</span><span>${tx('Data')}</span><span class="c-dev">${tx('Devices')}</span>
    <span class="c-last">${tx('Last seen')}</span><span style="text-align:end">${tx('Expiry')}</span><span class="c-act"></span></div>`;
  const rows = list.map(k => {
    const s = statusOf(k), e = expiryOf(k), on = !!S.selected[selKey(k)], id = esc(k.id), sid = esc(k.serverId);
    return `<div class="krow ${on ? 'sel' : ''} ${k.disabled ? 'off' : ''}" role="row" data-act="detail" data-id="${id}" data-sid="${sid}">
      <button class="chk ${on ? 'on' : ''}" data-act="sel" data-id="${id}" data-sid="${sid}" data-stop="1" aria-label="${tx('Select')}" aria-pressed="${on}">${tick}</button>
      <div style="min-width:0"><div class="kname">${esc(k.name)}</div>
        <div class="kmeta"><span class="num">#${id}</span><span>·</span><span style="overflow:hidden;text-overflow:ellipsis">${esc(srvName(k.serverId))}</span>${isOwner() && k.ownerAdminId != null && k.ownerName ? `<span class="own">${esc(k.ownerName)}</span>` : ''}</div></div>
      <div><span class="tag st-${s.key}"><span class="dot"></span>${esc(s.label)}</span></div>
      <div style="min-width:0"><div class="bar">${barFor(k)}</div><div class="kusage num">${esc(usageText(k))}</div></div>
      <div class="c-dev">${k.peakDevices != null ? k.peakDevices : '—'}${(k.peakDevices || 0) >= 5 ? `<span class="tag tag-accent" style="font-size:10px;padding:2px 8px" title="${tx('Possible shared account')}">${tx('shared?')}</span>` : ''}</div>
      <div class="c-last ${isOnline(k) ? 'live' : ''}">${esc(lastText(k))}</div>
      <div class="c-exp"><div class="e1 ${e.urgent ? 'urgent' : ''}">${esc(e.short)}</div><div class="e2">${esc(e.date)}</div></div>
      <div class="c-act"><button class="icon-btn" data-act="copylink" data-id="${id}" data-sid="${sid}" data-stop="1" title="${tx('Copy customer link')}" aria-label="${tx('Copy customer link')}">${ic('link', 15)}</button>
        <button class="icon-btn" data-act="share" data-id="${id}" data-sid="${sid}" data-stop="1" title="${tx('QR code')}" aria-label="${tx('QR code')}">${ic('qr', 15)}</button></div>
    </div>`;
  }).join('');
  return `<div class="keys" role="table">${head}${rows}</div>`;
}
function cardList(list) {
  return `<div class="kcards">` + list.map(k => {
    const s = statusOf(k), e = expiryOf(k), on = !!S.selected[selKey(k)], id = esc(k.id), sid = esc(k.serverId);
    const meta = ['#' + k.id, srvName(k.serverId), k.peakDevices ? t('{n} devices', { n: k.peakDevices }) : '',
      isOwner() && k.ownerAdminId != null ? k.ownerName : ''].filter(Boolean).join(' · ');
    return `<div class="kcard ${on ? 'sel' : ''} ${k.disabled ? 'off' : ''}" data-act="detail" data-id="${id}" data-sid="${sid}">
      <div class="top"><div style="flex:1;min-width:0"><div class="nm">${esc(k.name)}</div><div class="mt">${esc(meta)}</div></div>
        <span class="tag st-${s.key}"><span class="dot"></span>${esc(s.label)}</span></div>
      <div class="bar">${barFor(k)}</div>
      <div class="ln"><span class="u num">${esc(usageText(k))}</span><span class="c-exp" style="text-align:end"><span class="e1 ${e.urgent ? 'urgent' : ''}">${esc(e.short)}</span></span></div>
      <div class="acts"><button class="btn btn-secondary" data-act="copylink" data-id="${id}" data-sid="${sid}" data-stop="1">${tx('Copy')}</button>
        <button class="btn btn-secondary" data-act="share" data-id="${id}" data-sid="${sid}" data-stop="1">${tx('QR')}</button>
        <button class="btn btn-primary" data-act="detail" data-id="${id}" data-sid="${sid}" data-stop="1">${tx('Open')}</button></div></div>`;
  }).join('') + '</div>';
}

/* ========================================================== servers page */
function sparkPaths(samples) {
  const sm = samples.length > 1 ? samples : [0, 0];
  const mn = Math.min(...sm), mx = Math.max(...sm), rng = (mx - mn) || 1, W = 280, H = 58, n = sm.length;
  const pts = sm.map((v, i) => [(i / Math.max(1, n - 1)) * W, H - 4 - ((v - mn) / rng) * (H - 10)]);
  const line = 'M' + pts.map(p => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' L');
  return { line, area: line + ` L${W} ${H} L0 ${H} Z` };
}
async function loadHealth() {
  try { S.health = await api('/api/servers/health?days=7', { quiet: true }); } catch (_) { S.health = null; }
  if (S.route === 'servers' && !modalRoot.innerHTML) renderPage();
}
function pageServers(el) {
  const st = S.stats || {};
  const per = id => (st.perServer || []).find(p => p.id === id) || {};
  const hl = id => ((S.health || {}).servers || []).find(h => h.id === id);
  const list = S.servers.filter(s => matches(s.name, s.host));
  const cards = list.map(s => {
    const ks = S.keys.filter(k => k.serverId === s.id), h = hl(s.id), p = per(s.id);
    return `<div class="srv-card">
      <div class="hd"><span class="dot ${s.reachable ? '' : 'down'}" style="width:10px;height:10px"></span><span class="nm">${esc(s.name)}</span>
        ${s.version ? `<span class="tag tag-neutral num">v${esc(s.version)}</span>` : ''}</div>
      <div class="host"><span class="ltr">${esc(s.host || '')}</span>${s.reachable ? '' : ' · ' + tx('unreachable')}</div>
      ${can('keys.view') ? `<div class="stat3">
        <div><div class="kicker">${tx('Keys')}</div><div class="sv">${ks.length}</div></div>
        <div><div class="kicker">${tx('Online')}</div><div class="sv sage">${ks.filter(isOnline).length}</div></div>
        <div><div class="kicker">${tx('30d')}</div><div class="sv">${p.available ? esc(fmtBytes(p.dataBytes)) : '—'}</div></div></div>` : ''}
      ${h && h.probes ? `<div class="health"><span><b class="num">${h.uptimePct == null ? '—' : h.uptimePct + '%'}</b> ${tx('up · 7 days')}</span>
        ${h.avgLatencyMs != null ? `<span><b class="num">${h.avgLatencyMs} ms</b> ${tx('average')}</span>` : ''}
        ${h.failingNow ? `<span style="color:var(--danger)"><b style="color:inherit">${h.failingNow}</b> ${tx('failed checks in a row')}</span>` : ''}</div>` : ''}
      <div class="acts"><button class="btn btn-secondary btn-sm" data-act="srvdetail" data-id="${esc(s.id)}">${tx(can('servers.manage') ? 'Server settings' : 'Details')}</button>
        ${can('keys.view') ? `<button class="btn btn-ghost btn-sm" data-act="srvfilter" data-id="${esc(s.id)}">${tx('See its keys')}</button>` : ''}</div>
    </div>`;
  }).join('');
  const add = can('servers.manage') ? `<button class="btn btn-primary" data-act="addserver">${ic('plus', 16)}${tx('Add a server')}</button>` : '';
  el.innerHTML = `<div class="page">${pageHead(t('Servers'), S.servers.length ? tx('Every Outline server this panel manages, and how it is holding up.') : '', add)}
    ${S.servers.length ? (cards ? `<div class="grid-cards">${cards}</div>` : emptyBox(t('Nothing matches “{q}”', { q: S.search }), ''))
      : emptyBox(t('No servers yet.'), t('Paste the API URL from Outline Manager to connect one.'), add)}
    ${can('keys.view') && S.servers.length ? bandwidthCard() : ''}</div>`;
  if (!S.health) loadHealth();
}
function bandwidthCard() {
  const st = S.stats;
  if (!st || !st.available) {
    return `<div class="card pad-lg"><span class="kicker accent">${tx('Live bandwidth')}</span>
      <p class="desc"><b style="color:var(--color-text)">${tx('Advanced stats are off.')}</b> ${tx('Enable metrics sharing for a server (Server settings) to see live bandwidth, online users and ISP locations.')}</p></div>`;
  }
  const sp = sparkPaths(S.bwSamples);
  const locs = (st.locations || []).map(l => ({ cc: l.location || '', asn: l.asn, org: l.asOrg,
    bytes: (l.dataTransferred || {}).bytes || 0 })).filter(l => l.bytes > 0).sort((a, b) => b.bytes - a.bytes).slice(0, 8);
  const max = locs.length ? locs[0].bytes : 1;
  return `<div class="card pad-lg" style="gap:var(--space-3)"><span class="kicker accent">${tx('Live bandwidth')}</span>
    <div class="bw-big"><span class="v num">${esc(fmtBytes(st.bwCurrent))}</span><span class="s">${tx('per second · peak {p}', { p: fmtRate(st.bwPeak) })}</span></div>
    <svg class="spark" viewBox="0 0 280 60" preserveAspectRatio="none" aria-hidden="true"><path d="${sp.area}" fill="var(--color-accent-200)"/>
      <path d="${sp.line}" fill="none" stroke="var(--color-accent)" stroke-width="2.75" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>
    ${S.bwSamples.length < 4 ? `<p class="hint" style="margin:0">${tx('Collecting samples — the line fills in over the next few minutes.')}</p>` : ''}
    <div class="health"><span>${tx('Tunnel time')} <b class="num">${esc(fmtDur(st.tunnelSec))}</b></span><span>${tx('Transfer · 30d')} <b class="num">${esc(fmtBytes(st.dataBytes))}</b></span></div>
    ${locs.length ? `<span class="kicker" style="margin-top:var(--space-2)">${tx('Connections by network · ISP')}</span><div class="isps">${locs.map(l => {
      const org = l.org || (l.asn ? 'AS' + l.asn : countryName(l.cc));
      return `<div class="isp" title="${esc(countryName(l.cc) + (l.asn ? ' · AS' + l.asn : ''))}"><span class="fl">${flag(l.cc)}</span>
        <div style="flex:1;min-width:0"><div class="o">${esc(org)}</div><div class="bar xs" style="margin-top:4px"><span style="width:${Math.max(4, Math.round(l.bytes / max * 100))}%"></span></div></div>
        <span class="b num">${esc(fmtBytes(l.bytes))}</span></div>`;
    }).join('')}</div>` : ''}</div>`;
}

/* =========================================================== admins page */
async function loadAdmins() {
  if (!isOwner()) return;
  try { const d = await api('/api/admins', { quiet: true }); S.admins = d.admins; S.adminCaps = d.caps; S.adminServers = d.servers; }
  catch (_) { /* the page says so when it renders */ }
}
async function pageAdmins(el) {
  el.innerHTML = `<div class="page">${pageHead(t('Admins'), tx('Give someone a login limited to certain servers and rights. The owner always keeps full access.'),
    `<button class="btn btn-primary" data-act="adminadd">${ic('plus', 16)}${tx('Add an admin')}</button>`)}
    <div id="alist"><span class="spin"></span></div></div>`;
  await loadAdmins();
  const box = $('#alist'); if (!box || S.route !== 'admins') return;
  const count = a => S.keys.filter(k => a.isOwner ? k.ownerAdminId == null : k.ownerAdminId === a.id).length;
  const rows = S.admins.filter(a => matches(a.username)).map(a => {
    const n = count(a);
    const where = a.isOwner ? t('All servers') : a.servers.map(srvName).join(', ');
    return `<div class="srv-card" style="gap:var(--space-2)">
      <div class="hd"><span class="dot ${a.disabled ? 'down' : ''}"></span><span class="nm">${esc(a.username)}</span>
        ${a.isOwner ? `<span class="tag tag-accent-2">${tx('Owner')}</span>` : ''}${a.disabled ? `<span class="tag tag-accent">${tx('Disabled')}</span>` : ''}</div>
      <div class="host">${esc(where)}${!a.isOwner ? ' · ' + tx(a.caps.length === 1 ? '1 right' : '{n} rights', { n: a.caps.length }) : ''}</div>
      <div class="stat3">
        <div><div class="kicker">${tx('Users')}</div><div class="sv">${n}</div></div>
        <div style="grid-column:span 2"><div class="kicker">${tx('Credit')}</div><div class="sv ${a.creditEnabled ? (a.credit > 0 ? 'sage' : '') : ''}" style="font-size:20px;line-height:1.6">${a.creditEnabled ? esc(fmtMoney(a.credit)) : tx('No credit limit')}</div></div></div>
      <div class="acts"><button class="btn btn-secondary btn-sm" data-act="adminfilter" data-id="${a.isOwner ? 'owner' : a.id}">${tx('See their users')}</button>
        ${a.creditEnabled ? `<button class="btn btn-secondary btn-sm" data-act="adminledger" data-id="${a.id}" data-name="${esc(a.username)}">${tx('Statement')}</button>` : ''}
        ${a.isOwner ? '' : `<button class="btn btn-ghost btn-sm" data-act="adminedit" data-id="${a.id}">${tx('Manage')}</button>`}</div></div>`;
  }).join('');
  box.outerHTML = `<div class="grid-cards" id="alist">${rows || emptyBox(t('Nothing matches “{q}”', { q: S.search }), '')}</div>`;
  renderNav();
}

/* ========================================================= activity page */
// "POST /api/servers/{sid}/keys" reads better as "Created a user".
const VERBS = [[/POST .*\/keys$/, 'Created a user'], [/DELETE .*\/keys\/.*/, 'Deleted a user'],
  [/POST .*\/extend/, 'Extended a user'], [/PUT .*\/limit/, 'Changed a data limit'], [/PUT .*\/monthly/, 'Changed a monthly quota'],
  [/PUT .*\/name$/, 'Renamed'], [/POST .*\/disable/, 'Disabled a user'], [/POST .*\/enable/, 'Enabled a user'],
  [/POST .*\/reset/, 'Reset usage'], [/POST .*\/rotate/, 'Issued a new config'], [/PUT .*\/owner/, 'Transferred a user'],
  [/bulk-servers/, 'Moved users between servers'], [/\/sub\//, 'Changed a subscription'],
  [/POST \/api\/login/, 'Signed in'], [/POST \/api\/logout/, 'Signed out'],
  [/\/api\/admins/, 'Admin management'], [/\/api\/packages/, 'Package management'],
  [/\/api\/servers/, 'Server management'], [/\/api\/restore/, 'Restored a backup'],
  [/\/api\/snapshots/, 'Backup snapshot'], [/\/api\/settings/, 'Changed settings'], [/\/api\/me\//, 'Changed own account']];
const auditLabel = a => { for (const [re, s] of VERBS) if (re.test(a)) return t(s); return a; };
async function loadAudit(more) {
  const a = S.audit || (S.audit = { rows: [], before: null, done: false });
  if (a.busy || (more && !a.before)) return; a.busy = true;
  try {
    const d = await api('/api/audit?limit=50' + (more && a.before ? '&before_id=' + a.before : ''), { quiet: true });
    a.rows = more ? a.rows.concat(d.entries) : d.entries; a.before = d.nextBeforeId; a.err = null;
  } catch (e) { a.err = e.message; }
  a.busy = false; a.loaded = true;
  if (S.route === 'activity' && !modalRoot.innerHTML) renderPage();
}
function pageActivity(el) {
  const a = S.audit;
  if (!a || !a.loaded) { el.innerHTML = `<div class="page">${pageHead(t('Activity'), tx('Every change made through the panel. Passwords and tokens are never stored.'))}<span class="spin"></span></div>`; loadAudit(); return; }
  const rows = a.rows.filter(e => matches(auditLabel(e.action), e.actor_name, e.target, e.ip));
  const item = e => {
    const who = e.actor_name || t('not signed in'), bad = (e.status || 0) >= 400;
    const init = who === 'scheduler' ? '⏱' : who.slice(0, 2).toUpperCase();
    return `<div class="feed-item"><span class="avatar ${bad ? 'bad' : ''}">${esc(init)}</span><div style="flex:1;min-width:0">
      <div class="w">${esc(auditLabel(e.action))}${e.target ? ` <span class="tg mono">${esc(e.target)}</span>` : ''}</div>
      <div class="m">${esc(who)} · ${esc(fmtDateTime(e.ts))}${bad ? ' · ' + esc(e.status) : ''}${e.ip ? ' · <span class="mono">' + esc(e.ip) + '</span>' : ''}</div>
      ${e.detail ? `<div class="d">${esc(e.detail.slice(0, 180))}</div>` : ''}</div></div>`;
  };
  el.innerHTML = `<div class="page">${pageHead(t('Activity'), tx('Every change made through the panel. Passwords and tokens are never stored.'),
      `<button class="btn btn-secondary btn-sm" data-act="auditreload">${tx('Refresh')}</button>`)}
    ${a.err ? `<p class="err">${esc(a.err)}</p>` : ''}
    <div class="feed">${rows.map(item).join('') || emptyBox(S.search ? t('Nothing matches “{q}”', { q: S.search }) : t('Nothing recorded yet.'), '')}
    ${a.before ? `<button class="btn btn-secondary" data-act="auditmore" style="align-self:center">${a.busy ? '<span class="spin"></span>' : tx('Load more')}</button>` : ''}</div></div>`;
}

/* ========================================================= settings page */
function settingGroups() {
  const r = (act, title, sub, extra = '') => ({ act, title: t(title), sub: typeof sub === 'function' ? sub() : t(sub), extra });
  const g = [
    { title: t('Servers'), rows: [r('go:servers', 'Servers', () => t(S.servers.length === 1 ? '1 connected' : '{n} connected', { n: S.servers.length })),
      can('servers.manage') ? r('addserver', 'Add a server', 'Paste an API URL or access config') : null] },
    { title: t('Access'), rows: [
      isOwner() ? r('go:admins', 'Admins', 'Sub-admins, the servers they see, and their rights') : null,
      isOwner() ? r('security', 'Security', 'Panel password and two-factor') : r('account', 'My account', 'Your password and two-factor'),
      isOwner() ? r('profile', 'Customer links', 'The short domain handed to customers') : null] },
    { title: t('Selling'), rows: [
      isOwner() ? r('go:packages', 'Packages', 'What admins on credit may sell') : null,
      onCredit() ? r('myledger', 'My statement', 'Every movement of your credit') : null,
      isOwner() ? r('knobs', 'Currency & thresholds', 'Alerts, intervals, caches and the currency label') : null] },
    { title: t('Automation'), rows: [can('bot.manage') ? r('go:bot', 'Telegram bot', 'The bot and the Mini App inside Telegram') : null] },
    { title: t('Data'), rows: [
      isOwner() ? r('go:activity', 'Activity log', 'Who did what, and when') : null,
      isOwner() ? r('backup', 'Backup & restore', 'A JSON snapshot of everything, and scheduled copies') : null] },
    { title: t('Preferences'), rows: [
      r('lang', 'Language', () => nextLang().label),
      S.mobile ? null : r('shortcuts', 'Keyboard shortcuts', '⌘K, /, N and ?'),
      r('signout', 'Sign out', () => t('Signed in as {name}', { name: (S.me || {}).username || '' }))] },
  ];
  return g.map(x => ({ ...x, rows: x.rows.filter(Boolean).filter(y => matches(y.title, y.sub)) })).filter(x => x.rows.length);
}
function pageSettings(el) {
  const groups = settingGroups();
  el.innerHTML = `<div class="page settings">${pageHead(t('Settings'), tx('Everything the panel stores lives here — no dialog stacked on a dialog.'))}
    ${groups.map(g => `<section class="set-group"><div class="kicker accent">${esc(g.title)}</div><div class="set-grid">
      ${g.rows.map(r => `<button class="set-card" data-act="${r.act.startsWith('go:') ? 'go' : r.act}" ${r.act.startsWith('go:') ? `data-id="${r.act.slice(3)}"` : ''}>
        <div style="flex:1;min-width:0"><div class="t">${esc(r.title)}</div><div class="s">${esc(r.sub)}</div></div><span class="chev">›</span></button>`).join('')}
    </div></section>`).join('') || emptyBox(t('Nothing matches “{q}”', { q: S.search }), '')}</div>`;
}

/* ============================================================== bot page */
async function pageBot(el) {
  el.innerHTML = `<div class="page">${pageHead(t('Telegram bot'), tx('Paste your bot token from @BotFather and the numeric admin IDs. The bot runs inside the panel — no separate process.'))}
    <div class="inline-panel" style="display:flex;flex-direction:column;gap:var(--space-4)">
      <div id="botStatus" class="banner" style="background:var(--color-bg)"><span class="spin"></span></div>
      <div class="field"><label for="bt">${tx('Bot token')}</label><input id="bt" class="input" autocomplete="off" placeholder="${tx('123456:ABC… (blank = keep current)')}"></div>
      <div class="field"><label for="bi">${tx('Admin Telegram IDs (comma separated)')}</label><input id="bi" class="input" inputmode="numeric" placeholder="${tx('e.g. 11111111, 22222222')}"><div class="hint">${tx('Send /id to your bot to find an ID.')}</div></div>
      <div class="field"><label for="bw">${tx('Mini App URL (public HTTPS base)')}</label><input id="bw" class="input" placeholder="https://panel.example.com"><div class="hint">${tx('Opens a Telegram Web App from the bot at <base>/tma. Must be HTTPS.')}</div></div>
      <div class="row-line"><div><div style="font-weight:700">${tx('Enabled')}</div><div class="hint" style="margin:0">${tx('Turn the bot on or off.')}</div></div><div class="toggle on" id="be" role="switch" tabindex="0" aria-checked="true"><div class="tk"></div><div class="kn"></div></div></div>
      <div id="boterr" class="err"></div>
      <div class="btn-row"><button class="btn btn-secondary" id="btest">${tx('Test token')}</button><button class="btn btn-primary" id="bsave">${tx('Save & apply')}</button></div>
    </div></div>`;
  let enabled = true; const tg = $('#be');
  const setTg = v => { enabled = v; tg.classList.toggle('on', v); tg.setAttribute('aria-checked', v); };
  tg.onclick = () => setTg(!enabled);
  tg.onkeydown = e => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); setTg(!enabled); } };
  const paint = b => {
    $('#botStatus').innerHTML = b.running ? `<span class="dot"></span><span><b>${tx('Running')}</b> ${tx('as')} <span class="mono">@${esc(b.username || 'bot')}</span></span>`
      : `<span class="dot down"></span><span><b>${tx(b.configured ? 'Configured' : 'Not configured')}</b> ${tx(b.configured ? 'but stopped.' : 'yet.')}</span>`;
  };
  try {
    const b = await api('/api/settings/bot');
    if (S.route !== 'bot') return;
    paint(b); $('#bi').value = (b.adminIds || []).join(', '); $('#bw').value = b.webappUrl || ''; setTg(b.enabled);
  } catch (e) { $('#botStatus').textContent = e.message; }
  $('#btest').onclick = async () => {
    const tok = $('#bt').value.trim(), er = $('#boterr'); er.textContent = '';
    if (!tok) { er.textContent = t('Enter a token to test.'); return; }
    try { const r = await api('/api/settings/bot/test', { method: 'POST', body: JSON.stringify({ token: tok }) }); toast(t('Valid · @{name}', { name: r.username })); }
    catch (e) { er.textContent = e.message; }
  };
  $('#bsave').onclick = async () => {
    const er = $('#boterr'), b = $('#bsave'); er.textContent = ''; busy(b, true);
    try {
      const r = await api('/api/settings/bot', { method: 'PUT', body: JSON.stringify({ token: $('#bt').value.trim() || null,
        adminIds: $('#bi').value.trim(), enabled, webappUrl: $('#bw').value.trim() }) });
      paint(r); $('#bt').value = ''; toast(t('Saved'));
    } catch (e) { er.textContent = e.message; }
    busy(b, false, t('Save & apply'));
  };
}

/* ========================================================= packages page */
const pkgSize = p => p.gb == null ? t('Unlimited data') : t('{n} GB', { n: p.gb });
const pkgTerm = p => p.days ? t('{n} days', { n: p.days }) : t('No expiry');
async function pagePackages(el) {
  el.innerHTML = `<div class="page">${pageHead(t('Packages'), tx("What your admins may sell. They pay the price out of their credit; each admin's discount applies on top."),
    `<button class="btn btn-primary" data-act="pkgadd">${ic('plus', 16)}${tx('Add a package')}</button>`)}<div id="plist"><span class="spin"></span></div></div>`;
  let d; try { d = await api('/api/packages'); } catch (e) { $('#plist').innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  if (S.route !== 'packages') return;
  S.packagesCount = d.packages.length; renderNav();
  const rows = d.packages.filter(p => matches(p.name)).map(p => `<button class="set-card" data-act="pkgedit" data-id="${p.id}">
    <div style="flex:1;min-width:0"><div class="t">${esc(p.name)}</div><div class="s">${esc(pkgSize(p))} · ${esc(pkgTerm(p))}${p.monthlyGb ? ' · ' + tx('{n} GB/month', { n: p.monthlyGb }) : ''}</div></div>
    <span class="tag tag-accent num" style="font-size:13px">${esc(fmtMoney(p.basePrice))}</span><span class="chev">›</span></button>`).join('');
  $('#plist').outerHTML = rows ? `<div class="set-grid" id="plist">${rows}</div>`
    : emptyBox(t('No packages yet.'), t('Admins on credit cannot sell until you add one.'));
}

/* ================================================================ overlays */
let _returnFocus = null;
function _mount(cls, panelCls, inner, label) {
  if (!modalRoot.innerHTML) _returnFocus = document.activeElement;
  modalRoot.innerHTML = `<div class="overlay ${cls}"><div class="${panelCls}" role="dialog" aria-modal="true" aria-label="${esc(label || '')}">
    ${S.mobile ? '<div class="grip" aria-hidden="true"></div>' : ''}${inner}</div></div>`;
  const ov = modalRoot.firstElementChild;
  // Focus the first field so the keyboard lands somewhere useful — but not on
  // the phone, where it would throw the keyboard up over half the sheet.
  if (!S.mobile) setTimeout(() => { const f = ov.querySelector('input:not([type=checkbox]):not([type=radio]):not([disabled]),select,textarea'); if (f) f.focus(); }, 30);
  return ov;
}
const openSheet = (inner, opts = {}) => _mount(opts.top ? 'top' : 'center', 'sheet' + (opts.wide ? ' wide' : ''), inner, opts.label);
const openDrawer = (inner, label) => _mount('right', 'drawer', inner, label);
function closeModal() {
  if (!modalRoot.innerHTML) return;
  modalRoot.innerHTML = '';
  if (_returnFocus && document.contains(_returnFocus)) _returnFocus.focus();
  _returnFocus = null;
}
// A sheet's header: kicker, title, close.
const shHead = (kicker, title, sub) => `<div class="sh-head"><div>${kicker ? `<div class="kicker accent">${esc(kicker)}</div>` : ''}
  <h2>${esc(title)}</h2>${sub ? `<div class="sh-meta">${sub}</div>` : ''}</div>
  <button class="btn btn-secondary btn-icon" data-act="close" aria-label="${tx('Close')}">×</button></div>`;
function busy(b, on, label) {
  if (!b) return;
  b.disabled = on;
  if (on) { b.dataset.label = b.innerHTML; b.innerHTML = '<span class="spin"></span>'; }
  else b.innerHTML = label != null ? esc(label) : (b.dataset.label || '');
}
const setSheet = (ov, html) => { const s = ov.querySelector('.sheet,.drawer'); s.innerHTML = (S.mobile ? '<div class="grip" aria-hidden="true"></div>' : '') + html; return s; };
async function afterChange(full) {
  if (full) await loadAll(); else await refreshKeys();
  _sigKeys = keysSig();
  if (S.screen !== 'app') return;
  if (full) renderApp(); else { renderNav(); if (S.route === 'users') { renderKpis(); renderList(); } else renderPage(); }
}

function renderQR(box, text) {
  if (!box) return;
  if (typeof QRCode === 'undefined') { box.parentElement.innerHTML = '<p class="desc">' + tx('QR unavailable offline.') + '</p>'; return; }
  new QRCode(box, { text, width: 196, height: 196, colorDark: '#201e1d', colorLight: '#ffffff', correctLevel: QRCode.CorrectLevel.M });
}

/* ------------------------------------------------------------ key drawer */
function modalDetail(k) {
  const s = statusOf(k), e = expiryOf(k), p = pctOf(k), id = esc(k.id), sid = esc(k.serverId);
  const edit = can('keys.edit'), del = can('keys.delete');
  const validity = k.pending ? t('{n}-day term · starts on first connection', { n: k.durationDays || 0 })
    : !k.expiry ? t('No expiry') : k.expiry <= now() ? t('Expired {date}', { date: fmtDate(k.expiry) })
    : t('{left} · expires {date}', { left: e.short, date: fmtDate(k.expiry) });
  const stat = (l, v, cls) => `<div class="stat"><div class="kicker" style="font-size:11px;color:var(--color-neutral-600)">${tx(l)}</div><div class="v ${cls || ''}">${esc(v)}</div></div>`;
  const b = (act, label, cls = 'btn-secondary') => `<button class="btn ${cls}" data-act="${act}" data-id="${id}" data-sid="${sid}">${tx(label)}</button>`;
  openDrawer(`<div class="sh-head"><div><div class="kicker accent">${tx('Access key')}</div><h2>${esc(k.name)}</h2>
      <div class="sh-meta"><span class="num">#${id}</span> · ${esc(srvName(k.serverId))}${k.createdTs ? ' · ' + tx('created {date}', { date: fmtDate(k.createdTs) }) : ''}${isOwner() && k.ownerAdminId != null && k.ownerName ? ' · ' + esc(k.ownerName) : ''}</div></div>
      <button class="btn btn-secondary btn-icon" data-act="close" aria-label="${tx('Close')}">×</button></div>
    <div style="display:flex;gap:var(--space-2);align-items:center;flex-wrap:wrap"><span class="tag st-${s.key}" style="padding:5px 12px"><span class="dot"></span>${esc(s.label)}</span>
      <span style="font-size:13px;color:var(--color-neutral-700)">${esc(validity)}</span></div>
    <div class="card" style="gap:var(--space-2)"><div class="row-line"><span class="kicker">${tx('Data usage')}</span>
      <span class="num" style="font-size:14px;font-weight:600">${esc(k.limit ? t('{used} of {limit}', { used: fmtBytes(k.used), limit: fmtBytes(k.limit) }) : fmtBytes(k.used) + ' · ' + t('unlimited'))}</span></div>
      <div class="bar lg">${barFor(k)}</div>
      <div class="row-line" style="font-size:12.5px;color:var(--color-neutral-700)"><span>${esc(k.limit ? t('{p}% of the limit', { p }) : t('No data limit'))}</span>
        <span>${k.monthlyBytes ? esc(t('resets monthly · {q}', { q: fmtBytes(k.monthlyBytes) })) : ''}</span></div></div>
    <div class="statgrid">${stat('Last seen', lastText(k), isOnline(k) ? 'sage' : '')}${stat('Peak devices', k.peakDevices != null ? String(k.peakDevices) : '—', (k.peakDevices || 0) >= 5 ? 'warn' : '')}
      ${stat('Tunnel time', fmtDur(k.tunnelSec || 0))}${stat(k.monthlyBytes ? 'Monthly quota' : 'Validity', k.monthlyBytes ? fmtBytes(k.monthlyBytes) : (k.pending ? t('{n}d on use', { n: k.durationDays || 0 }) : e.short))}</div>
    <div class="sec"><span class="kicker">${tx('Hand out')}</span><div class="btn-row">${b('copylink', 'Copy link')}${b('copy', 'Copy config')}${b('share', 'QR code')}</div></div>
    ${edit ? `<div class="sec"><span class="kicker">${tx('Change')}</span><div class="btn-row">${b('manage', 'Edit key', 'btn-primary')}
      ${onCredit() ? b('manage', 'Renew') : b('extend30', 'Extend +30d')}${k.monthlyBytes && !onCredit() ? b('resetkey', 'Reset usage') : ''}</div></div>` : ''}
    ${edit || del ? `<div class="danger-zone"><div style="flex:1;min-width:170px"><div class="t">${tx('No going back')}</div>
      <div class="s">${tx('Disabling keeps the record. Deleting removes the key from the server.')}</div></div>
      ${edit ? b('toggle', k.disabled ? 'Enable' : 'Disable') : ''}${del ? b('delkey', 'Delete', 'btn-primary') : ''}</div>` : ''}`, k.name);
}

/* -------------------------------------------------------------- edit key */
function modalManage(k) {
  const credit = onCredit(), owner = isOwner(), multi = S.servers.length > 1;
  const gb = b => b ? +(b / 1073741824).toFixed(2) : '';
  const e = expiryOf(k);
  const validityNow = k.pending ? t('{n}-day term, starts on first connection', { n: k.durationDays || 0 })
    : !k.expiry ? t('No expiry') : t('{left} · expires {date}', { left: e.short, date: fmtDate(k.expiry) });
  const chips = [-30, -7, 7, 30, 90, 365];
  const ov = openSheet(`${shHead(t('Key #{id}', { id: k.id }) + (multi ? ' · ' + srvName(k.serverId) : ''), t('Edit key'))}
    <div class="form-grid">
      <div class="field full"><label for="mn">${tx('Name')}</label><input id="mn" class="input" value="${esc(k.name)}" maxlength="80"></div>
      ${credit ? '' : `<div class="field"><label for="ml">${tx('Data limit · GB')}</label><input id="ml" class="input" type="number" min="0" step="any" value="${gb(k.limit)}" placeholder="${tx('Unlimited')}"></div>
      <div class="field"><label for="mq">${tx('Monthly quota · GB')}</label><input id="mq" class="input" type="number" min="0" step="any" value="${gb(k.monthlyBytes)}" placeholder="${tx('Off')}"></div>`}
      ${owner ? `<div class="field full"><label for="mow">${tx('Belongs to')}</label><select id="mow" class="input"><option value="">${esc((S.me || {}).username || 'admin')} (${tx('you')})</option></select></div>` : ''}
    </div>
    ${credit ? `<div class="sec"><span class="kicker">${tx('Renew')}</span><div id="mrn"><span class="spin"></span></div></div>`
      : `<div class="sec"><span style="font-size:13px;color:var(--color-neutral-700)">${tx('Validity — {v}', { v: validityNow })}</span>
      <div class="btn-row" style="gap:var(--space-2)">${chips.map(d => `<button class="chip" data-days="${d}">${d > 0 ? '+' : '−'}${esc(Math.abs(d) === 365 ? t('1 year') : t('{n} days', { n: Math.abs(d) }))}</button>`).join('')}</div>
      <div class="preview" id="mvp">${tx('Pick a chip to extend or shorten.')}</div></div>`}
    <div class="sec"><span class="kicker">${tx('Config')}</span><div class="btn-row" style="justify-content:flex-start">
      <button class="btn btn-secondary btn-sm" data-act="config" data-id="${esc(k.id)}" data-sid="${esc(k.serverId)}">${tx('Dynamic JSON')}</button>
      <button class="btn btn-secondary btn-sm" id="msub">${tx('Subscription link')}</button>
      <button class="btn btn-secondary btn-sm" id="mrot">${tx('Issue new config')}</button></div></div>
    <div id="me" class="err" role="alert"></div>
    <div class="btn-row" style="align-items:center">${can('keys.delete') ? `<button class="btn btn-secondary" id="mdel" style="flex:none">${tx('Delete key')}</button>` : ''}
      <span class="spacer"></span><button class="btn btn-secondary" data-act="close" style="flex:none">${tx('Cancel')}</button>
      <button class="btn btn-primary" id="msave" style="flex:none;padding-inline:var(--space-6)">${tx('Save changes')}</button></div>`, { label: t('Edit key') });
  const kb = '/api/servers/' + k.serverId + '/keys/' + k.id;
  if (owner) ownerPicker(ov, k);
  if (credit) renewPicker(ov, k, kb);
  let days = 0;
  const daysIdem = idemKey();   // days stack up on a double-send even with no money involved
  ov.querySelectorAll('[data-days]').forEach(c => c.onclick = () => {
    const d = +c.dataset.days, same = days === d;
    days = same ? 0 : d;
    ov.querySelectorAll('[data-days]').forEach(x => x.classList.toggle('sel', !same && x === c));
    const p = ov.querySelector('#mvp');
    if (!days) { p.textContent = t('Pick a chip to extend or shorten.'); return; }
    const sign = (days > 0 ? '+' : '') + days + t('d');
    if (k.pending) p.textContent = t('Applying {s} → a {n}-day term', { s: sign, n: Math.max(1, (k.durationDays || 0) + days) });
    else { const base = Math.max(k.expiry || 0, now()); p.textContent = t('Applying {s} → expires {date}', { s: sign, date: fmtDate(Math.max(now(), base + days * 86400)) }); }
  });
  ov.querySelector('#msub').onclick = async () => { try { openSubSheet(k, await api(kb + '/sub', { method: 'POST' })); } catch (e) { ov.querySelector('#me').textContent = e.message; } };
  ov.querySelector('#mrot').onclick = async () => {
    if (!ask('Give “{name}” a new config? Their link stays the same, and the data they have already used is carried over.', { name: k.name })) return;
    const b = ov.querySelector('#mrot'); busy(b, true);
    try {
      const r = await api(kb + '/rotate', { method: 'POST' });
      toast(t('New config issued')); await afterChange(true);
      const nk = S.keys.find(x => x.serverId === k.serverId && String(x.id) === String(r.id));
      if (nk) modalShare(nk, true); else closeModal();
    } catch (e) { ov.querySelector('#me').textContent = e.message; busy(b, false); }
  };
  const del = ov.querySelector('#mdel');
  if (del) del.onclick = () => deleteKey(k);
  ov.querySelector('#msave').onclick = async () => {
    const er = ov.querySelector('#me'), name = ov.querySelector('#mn').value.trim(); er.textContent = '';
    if (!name) { er.textContent = t('Name cannot be empty.'); return; }
    const num = id => { const el = ov.querySelector(id); return el ? (parseFloat(el.value || '0') || 0) : null; };
    const lg = num('#ml'), mg = num('#mq');
    const b = ov.querySelector('#msave'); busy(b, true);
    try {
      if (name !== k.name) await api(kb + '/name', { method: 'PUT', body: JSON.stringify({ name }) });
      // Only what actually changed. This always wrote the limit, which the
      // server refuses for an admin on credit — so a reseller could not even
      // rename a customer.
      if (lg != null && lg !== (+gb(k.limit) || 0)) await api(kb + '/limit', { method: 'PUT', body: JSON.stringify({ limit_gb: lg }) });
      if (mg != null && mg !== (+gb(k.monthlyBytes) || 0)) await api(kb + '/monthly', { method: 'PUT', body: JSON.stringify({ monthly_gb: mg }) });
      const ow = ov.querySelector('#mow');
      if (ow && ow.value !== String(k.ownerAdminId == null ? '' : k.ownerAdminId))
        await api(kb + '/owner', { method: 'PUT', body: JSON.stringify({ admin_id: ow.value === '' ? null : +ow.value }) });
      if (days) await api(kb + '/extend', { method: 'POST', idem: daysIdem, body: JSON.stringify({ days }) });
      toast(t('Changes saved')); closeModal(); await afterChange(false);
    } catch (e) { er.textContent = e.message; busy(b, false); }
  };
}
async function ownerPicker(ov, k) {
  const sel = ov.querySelector('#mow');
  if (!S.admins.length) await loadAdmins();
  S.admins.filter(a => !a.isOwner && a.servers.includes(k.serverId)).forEach(a => {
    const o = document.createElement('option'); o.value = a.id;
    o.textContent = a.username + (a.disabled ? ' · ' + t('disabled') : ''); sel.appendChild(o);
  });
  sel.value = k.ownerAdminId == null ? '' : String(k.ownerAdminId);
}
function pkgRow(p, name) {
  return `<label class="pkg ${p.affordable ? '' : 'no'}"><input type="radio" name="${name}" value="${p.id}" ${p.affordable ? '' : 'disabled'}>
    <div style="flex:1;min-width:0"><div style="font-weight:700">${esc(p.name)}</div><div class="hint" style="margin:0">${esc(pkgSize(p))} · ${esc(pkgTerm(p))}</div></div>
    <span class="num" style="font-weight:700;color:${p.affordable ? 'var(--color-accent-700)' : 'var(--danger)'}">${esc(fmtMoney(p.price))}</span></label>`;
}
async function renewPicker(ov, k, kb) {
  const box = ov.querySelector('#mrn');
  let d; try { d = await api('/api/packages'); } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  box.innerHTML = `<p class="desc" style="margin-bottom:var(--space-2)">${tx("Renewing adds the package's time and data. You have {c}.", { c: fmtMoney(d.credit) })}</p>
    ${d.packages.map(p => pkgRow(p, 'mpk')).join('') || `<p class="desc">${tx('No packages available.')}</p>`}
    <button class="btn btn-secondary btn-block" id="mrgo">${tx('Renew with this package')}</button>`;
  const renewIdem = idemKey();   // one renewal, however many attempts
  box.querySelector('#mrgo').onclick = async () => {
    const pick = box.querySelector('input[name=mpk]:checked'), er = ov.querySelector('#me'); er.textContent = '';
    if (!pick) { er.textContent = t('Pick a package.'); return; }
    const b = box.querySelector('#mrgo'); busy(b, true);
    try { await api(kb + '/extend', { method: 'POST', idem: renewIdem, body: JSON.stringify({ package_id: +pick.value }) });
      toast(t('Renewed')); closeModal(); await afterChange(true); }
    catch (e) { er.textContent = e.message; busy(b, false); }
  };
}
async function deleteKey(k) {
  if (!ask('Delete “{name}”? This cannot be undone.', { name: k.name })) return;
  try { await api('/api/servers/' + k.serverId + '/keys/' + k.id, { method: 'DELETE' });
    delete S.selected[selKey(k)]; toast(t('Key deleted')); closeModal(); await afterChange(false); }
  catch (e) { toast(e.message, 1); }
}

/* ---------------------------------------------------------------- create */
// Servers this customer gets a config on. All reachable ones ticked by
// default, so a new customer has failover unless someone narrows it; the
// first becomes the primary and the rest are mirrored under one link.
function srvPicker() {
  if (S.servers.length < 2) return '';
  return `<div class="field"><label>${tx('Servers for this user')}</label><div class="btn-row" style="gap:var(--space-2)">${S.servers.map(sv => `
    <label class="chip" style="cursor:${sv.reachable ? 'pointer' : 'not-allowed'};opacity:${sv.reachable ? 1 : .45}">
      <input type="checkbox" class="csrv" value="${esc(sv.id)}" ${sv.reachable ? 'checked' : 'disabled'}>${esc(sv.name)}${sv.reachable ? '' : ' · ' + tx('offline')}</label>`).join('')}</div>
    <div class="hint">${tx('One link, a config on each. The full allowance applies on every server.')}</div></div>`;
}
const pickedServers = ov => {
  const boxes = [...ov.querySelectorAll('.csrv')];
  if (!boxes.length) { const f = S.servers.find(s => s.reachable) || S.servers[0]; return f ? [f.id] : []; }
  return boxes.filter(x => x.checked).map(x => x.value);
};
const startToggle = () => `<div class="row-line"><div><div style="font-weight:700">${tx('Start countdown now')}</div>
  <div class="hint" style="margin:0" id="csd">${tx("Off: the clock starts on the user's first connection.")}</div></div>
  <div class="toggle" id="cst" role="switch" tabindex="0" aria-checked="false"><div class="tk"></div><div class="kn"></div></div></div>`;
function wireStart(ov) {
  let on = false; const tg = ov.querySelector('#cst');
  const flip = () => { on = !on; tg.classList.toggle('on', on); tg.setAttribute('aria-checked', on);
    ov.querySelector('#csd').textContent = on ? t('On: the clock starts the moment the key is created.') : t("Off: the clock starts on the user's first connection."); };
  tg.onclick = flip; tg.onkeydown = e => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); flip(); } };
  return () => on;
}
function modalCreate() {
  if (!S.servers.length) { toast(t('Add a server first.'), 1); return; }
  return onCredit() ? modalCreateBuy() : modalCreateFree();
}
async function submitCreate(ov, body, idem, b, label) {
  const picked = pickedServers(ov), er = ov.querySelector('#ce'); er.textContent = '';
  if (!picked.length) { er.textContent = t('Pick at least one server.'); return; }
  if (!body.name) { er.textContent = t('Please enter a name.'); return; }
  busy(b, true);
  try {
    const sid = picked[0];
    const k = await api('/api/servers/' + sid + '/keys', { method: 'POST', idem, body: JSON.stringify({ ...body, extra_servers: picked.slice(1) }) });
    if (k.serverErrors && k.serverErrors.length) toast(t('Created, but some servers failed') + ': ' + k.serverErrors.map(e => srvName(e.id)).join(', '), 1);
    await afterChange(onCredit());
    modalShare({ id: k.id, serverId: sid, name: body.name, accessUrl: k.accessUrl, profileUrl: k.profileUrl }, true);
  } catch (e) { er.textContent = e.message; busy(b, false, label); }
}
function modalCreateFree() {
  const idem = idemKey();   // one id for this dialog, however many times Create is pressed
  const chip = (id, v, l) => `<button class="chip" data-chip="${id}" data-v="${v}">${tx(l)}</button>`;
  const ov = openSheet(`${shHead(t('New key'), t('Create a new key'))}<p class="desc">${tx('Generate one Outline access key for a single user.')}</p>
    ${srvPicker()}
    <div class="field"><label for="cn">${tx('Name')}</label><input id="cn" class="input" maxlength="100" placeholder="${tx('e.g. Mohsen — iPhone')}"></div>
    <div class="form-grid">
      <div class="field"><label for="cl">${tx('Data limit · GB')}</label><input id="cl" class="input" type="number" min="0" step="any" placeholder="${tx('Unlimited')}">
        <div class="btn-row" style="gap:6px;margin-top:var(--space-2)">${chip('cl', 10, '10 GB')}${chip('cl', 50, '50 GB')}${chip('cl', 100, '100 GB')}${chip('cl', 0, 'Unlimited')}</div></div>
      <div class="field"><label for="cd">${tx('Valid for · days')}</label><input id="cd" class="input" type="number" min="0" placeholder="${tx('No expiry')}">
        <div class="btn-row" style="gap:6px;margin-top:var(--space-2)">${chip('cd', 30, '30 days')}${chip('cd', 90, '90 days')}${chip('cd', 365, '1 year')}${chip('cd', 0, 'No expiry')}</div></div>
    </div>
    <div class="field"><label for="cm">${tx('Monthly quota · GB')}</label><input id="cm" class="input" type="number" min="0" step="any" placeholder="${tx('Off')}"><div class="hint">${tx('Refreshes the allowance every billing cycle.')}</div></div>
    ${startToggle()}
    <div id="ce" class="err" role="alert"></div>
    <div class="btn-row end"><button class="btn btn-secondary" data-act="close">${tx('Cancel')}</button><button class="btn btn-primary" id="cgo" style="padding-inline:var(--space-6)">${tx('Create key')}</button></div>`, { label: t('New key') });
  const startNow = wireStart(ov);
  const num = id => parseFloat(ov.querySelector(id).value || '0') || 0;
  ov.querySelector('#cgo').onclick = () => submitCreate(ov, { name: ov.querySelector('#cn').value.trim(), limit_gb: num('#cl'),
    days: parseInt(ov.querySelector('#cd').value || '0', 10) || 0, monthly_gb: num('#cm'), start_now: startNow() }, idem, ov.querySelector('#cgo'), t('Create key'));
  ov.querySelector('#cn').addEventListener('keydown', e => { if (e.key === 'Enter') ov.querySelector('#cgo').click(); });
}
async function modalCreateBuy() {
  const idem = idemKey();   // this dialog is one purchase, however many attempts
  const ov = openSheet(`${shHead(t('New key'), t('New user'))}<span class="spin"></span>`, { label: t('New user') });
  let d; try { d = await api('/api/packages'); } catch (e) { setSheet(ov, shHead(t('New key'), t('New user')) + `<p class="err">${esc(e.message)}</p>`); return; }
  S.me.credit = d.credit;
  setSheet(ov, `${shHead(t('New key'), t('New user'))}
    <p class="desc">${tx('Pick a package. Its price comes out of your credit — you have {c}.', { c: fmtMoney(d.credit) })}${d.discountPct ? ' ' + tx('Your discount: {p}%.', { p: d.discountPct }) : ''}</p>
    ${srvPicker()}
    <div class="field"><label for="cn">${tx('Name')}</label><input id="cn" class="input" maxlength="100" placeholder="${tx('e.g. Mohsen — iPhone')}"></div>
    <div class="field"><label>${tx('Package')}</label>${d.packages.map(p => pkgRow(p, 'cpk')).join('') || `<p class="desc">${tx('No packages are available yet. Ask the owner to add one.')}</p>`}</div>
    ${startToggle()}
    <div id="ce" class="err" role="alert"></div>
    <div class="btn-row end"><button class="btn btn-secondary" data-act="close">${tx('Cancel')}</button><button class="btn btn-primary" id="cgo" style="padding-inline:var(--space-6)">${tx('Create user')}</button></div>`);
  const startNow = wireStart(ov);
  ov.querySelector('#cgo').onclick = () => {
    const pick = ov.querySelector('input[name=cpk]:checked');
    if (!pick) { ov.querySelector('#ce').textContent = t('Pick a package.'); return; }
    submitCreate(ov, { name: ov.querySelector('#cn').value.trim(), package_id: +pick.value, start_now: startNow() }, idem, ov.querySelector('#cgo'), t('Create user'));
  };
  if (!S.mobile) ov.querySelector('#cn').focus();
}

/* --------------------------------------------------- share / sub / config */
function modalShare(k, fresh) {
  const url = tagUrl(k);
  const link = k.profileUrl ? new URL(k.profileUrl, location.origin).href : '';
  const ov = openSheet(`${shHead(fresh ? t('Key created') : t('Access key'), k.name)}
    <p class="desc">${tx('Scan with the Outline app, or copy the link to share it.')}</p>
    <div class="qr"><div id="qr"></div></div>
    <div class="codebox">${esc(url)}</div>
    <div class="btn-row">${link ? `<button class="btn btn-secondary" data-act="copytext" data-text="${esc(link)}">${tx('Copy customer link')}</button>` : ''}
      <button class="btn btn-primary" data-act="copytext" data-text="${esc(url)}">${tx('Copy config')}</button></div>`, { label: k.name });
  renderQR(ov.querySelector('#qr'), url);
}
function openSubSheet(k, info) {
  const link = info.profileUrl || (location.origin + info.path), multi = S.servers.length > 1;
  const ov = openSheet(`${shHead(t('Subscription'), k.name)}
    <p class="desc">${tx('One auto-updating link. Clients show data and expiry, and refresh it on their own.')}</p>
    <div class="qr"><div id="sq"></div></div><div class="codebox">${esc(link)}</div>
    ${multi ? `<div class="sec"><span class="kicker">${tx('Servers in this subscription')}</span><div class="btn-row" style="gap:var(--space-2)">${info.servers.map(s =>
      `<button class="chip ${s.included ? 'sel' : ''}" data-subsrv="${esc(s.id)}" aria-pressed="${s.included}">${s.included ? '✓ ' : ''}${esc(s.name)}</button>`).join('')}</div>
      <div class="hint">${tx("Include another server's config in this one link — handy for failover.")}</div></div>` : ''}
    <button class="btn btn-primary btn-block" data-act="copytext" data-text="${esc(link)}">${tx('Copy link')}</button>`, { label: t('Subscription') });
  renderQR(ov.querySelector('#sq'), link);
  ov.querySelectorAll('[data-subsrv]').forEach(c => c.onclick = async () => {
    const tid = c.dataset.subsrv, inc = c.classList.contains('sel'); busy(c, true);
    try { const r = await api('/api/sub/' + info.token + '/servers/' + tid, { method: inc ? 'DELETE' : 'POST' });
      toast(inc ? t('Server removed') : t('Server added')); await refreshKeys(); openSubSheet(k, r); }
    catch (e) { toast(e.message, 1); busy(c, false); }
  });
}
function modalConfig(k) {
  let cfg = null;
  try {
    const m = (k.accessUrl || '').match(/^ss:\/\/([^@]+)@([^:\/?#]+):(\d+)/);
    if (m) { let ui = m[1].replace(/-/g, '+').replace(/_/g, '/'); while (ui.length % 4) ui += '=';
      const dec = atob(decodeURIComponent(ui)), i = dec.indexOf(':');
      cfg = { server: m[2], server_port: +m[3], password: dec.slice(i + 1), method: dec.slice(0, i) }; }
  } catch (_) { /* not a base64 userinfo */ }
  if (!cfg) { toast(t('Could not decode this key'), 1); return; }
  const json = JSON.stringify(cfg, null, 2);
  openSheet(`${shHead(t('Dynamic-key config'), k.name)}
    <p class="desc">${tx('Host this JSON at a URL and hand users a key that points to it — then you can repoint the server without redistributing keys.')}</p>
    <div class="codebox">${esc(json)}</div>
    <div class="btn-row"><button class="btn btn-secondary" data-act="manage" data-id="${esc(k.id)}" data-sid="${esc(k.serverId)}">${tx('Back')}</button>
      <button class="btn btn-primary" data-act="copytext" data-text="${esc(json)}">${tx('Copy JSON')}</button></div>`);
}

/* ============================================================== servers */
function modalAddServer() {
  const ov = openSheet(`${shHead(t('Servers'), t('Add a server'))}
    <p class="desc">${tx('Paste the API URL, or the full Outline Manager access config (JSON with apiUrl and certSha256).')}</p>
    <div class="field"><label for="an">${tx('Label')}</label><input id="an" class="input" placeholder="${tx('e.g. Sweden · Hetzner')}" maxlength="80"></div>
    <div class="field"><label for="au">${tx('API URL or access config')}</label><textarea id="au" class="input" placeholder='https://1.2.3.4:1234/xxxx — or — {"apiUrl":"…","certSha256":"…"}'></textarea></div>
    <div id="ae" class="err" role="alert"></div>
    <div class="btn-row end"><button class="btn btn-secondary" data-act="close">${tx('Cancel')}</button><button class="btn btn-primary" id="ago">${tx('Connect & add')}</button></div>`);
  ov.querySelector('#ago').onclick = async () => {
    const name = ov.querySelector('#an').value.trim(), apiUrl = ov.querySelector('#au').value.trim(), er = ov.querySelector('#ae');
    er.textContent = '';
    if (!name) { er.textContent = t('Enter a label.'); return; }
    if (!apiUrl) { er.textContent = t('Paste the API URL.'); return; }
    const b = ov.querySelector('#ago'); busy(b, true);
    try { await api('/api/servers', { method: 'POST', body: JSON.stringify({ name, apiUrl }) });
      toast(t('Server connected')); closeModal(); S.health = null; await afterChange(true); }
    catch (e) { er.textContent = e.message; busy(b, false); }
  };
}
async function modalServerDetail(s) {
  const ov = openSheet(`${shHead(t('Server'), s.name)}<span class="spin"></span>`, { label: s.name });
  let st; try { st = await api('/api/servers/' + s.id + '/settings'); }
  catch (e) { setSheet(ov, shHead(t('Server'), s.name) + `<p class="err">${esc(e.message)}</p>`); return; }
  const gl = st.globalLimit ? +(st.globalLimit / 1073741824).toFixed(2) : '';
  const manage = can('servers.manage');
  setSheet(ov, `${shHead(t('Server'), s.name, `<span class="mono">${esc(st.host || '')}</span>${st.version ? ' · Outline v' + esc(st.version) : ''}`)}
    ${manage ? `<div class="form-grid">
      <div class="field"><label for="dl">${tx('Label (shown in this panel)')}</label><input id="dl" class="input" value="${esc(s.name)}" maxlength="80"></div>
      <div class="field"><label for="dn">${tx('Outline server name')}</label><input id="dn" class="input" value="${esc(st.name || '')}" maxlength="80"></div>
      <div class="field full"><label for="dg">${tx('Default data limit for every key · GB')}</label><input id="dg" class="input" type="number" min="0" step="any" value="${gl}" placeholder="${tx('No global limit')}"></div></div>
    <div class="row-line"><div><div style="font-weight:700">${tx('Metrics sharing')}</div><div class="hint" style="margin:0">${tx('Live bandwidth, online users and ISP locations.')}</div></div>
      <div class="toggle ${st.metricsEnabled ? 'on' : ''}" id="dm" role="switch" tabindex="0" aria-checked="${!!st.metricsEnabled}"><div class="tk"></div><div class="kn"></div></div></div>
    <div id="de" class="err" role="alert"></div>
    <div class="btn-row" style="align-items:center"><button class="btn btn-secondary" id="drm" style="flex:none">${tx('Remove')}</button><span class="spacer"></span>
      <button class="btn btn-secondary" data-act="close" style="flex:none">${tx('Cancel')}</button><button class="btn btn-primary" id="dsave" style="flex:none">${tx('Save')}</button></div>`
    : `<div class="statgrid"><div class="stat"><div class="kicker">${tx('Outline server name')}</div><div class="v" style="font-size:18px">${esc(st.name || '—')}</div></div>
      <div class="stat"><div class="kicker">${tx('Metrics sharing')}</div><div class="v" style="font-size:18px">${tx(st.metricsEnabled ? 'On' : 'Off')}</div></div></div>
      <p class="desc">${tx('You can see this server but not change it.')}</p>`}`);
  if (!manage) return;
  let metrics = !!st.metricsEnabled; const tg = ov.querySelector('#dm');
  const flip = async () => {
    metrics = !metrics; tg.classList.toggle('on', metrics); tg.setAttribute('aria-checked', metrics);
    try { await api('/api/servers/' + s.id + '/settings/metrics', { method: 'PUT', body: JSON.stringify({ enabled: metrics }) });
      toast(metrics ? t('Metrics enabled') : t('Metrics disabled')); }
    catch (e) { toast(e.message, 1); metrics = !metrics; tg.classList.toggle('on', metrics); tg.setAttribute('aria-checked', metrics); }
  };
  tg.onclick = flip; tg.onkeydown = e => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); flip(); } };
  ov.querySelector('#dsave').onclick = async () => {
    const label = ov.querySelector('#dl').value.trim(), oname = ov.querySelector('#dn').value.trim();
    const lg = parseFloat(ov.querySelector('#dg').value || '0') || 0, er = ov.querySelector('#de'), b = ov.querySelector('#dsave');
    er.textContent = ''; busy(b, true);
    try {
      if (label && label !== s.name) await api('/api/servers/' + s.id, { method: 'PUT', body: JSON.stringify({ name: label }) });
      if (oname && oname !== st.name) await api('/api/servers/' + s.id + '/settings/name', { method: 'PUT', body: JSON.stringify({ name: oname }) });
      if (lg !== (+gl || 0)) await api('/api/servers/' + s.id + '/settings/global-limit', { method: 'PUT', body: JSON.stringify({ limit_gb: lg }) });
      toast(t('Saved')); closeModal(); await afterChange(true);
    } catch (e) { er.textContent = e.message; busy(b, false); }
  };
  ov.querySelector('#drm').onclick = async () => {
    if (!ask('Remove “{name}” from this panel? (The Outline server itself is not deleted.)', { name: s.name })) return;
    try { await api('/api/servers/' + s.id, { method: 'DELETE' }); toast(t('Server removed'));
      if (S.srvFilter === s.id) S.srvFilter = 'all'; closeModal(); S.health = null; await afterChange(true); }
    catch (e) { toast(e.message, 1); }
  };
}

/* ========================================================= account & 2FA */
/* One enrolment, on /api/me/2fa/*: the owner's Security sheet and a
   reseller's account sheet are the same job, and two copies is how one ends
   up saying "on" while the other says "off". */
function render2fa(box) {
  if (!box) return;
  const draw = () => api('/api/me/2fa').then(s => {
    if (s.enabled) {
      box.innerHTML = `<p class="desc">${tx('Two-factor is on.')}</p>
        <div class="field"><label for="tpw">${tx('Password to turn it off')}</label><input id="tpw" class="input" type="password" autocomplete="current-password"></div>
        <div id="tferr" class="err" role="alert"></div><button class="btn btn-danger btn-block" id="tdis">${tx('Turn off 2FA')}</button>`;
      box.querySelector('#tdis').onclick = async () => {
        try { await api('/api/me/2fa/disable', { method: 'POST', body: JSON.stringify({ password: box.querySelector('#tpw').value }) }); toast(t('2FA disabled')); draw(); }
        catch (e) { box.querySelector('#tferr').textContent = e.message; }
      };
      return;
    }
    box.innerHTML = `<p class="desc">${tx('A code from your phone on top of your password. Recommended for anyone who sells.')}</p>
      <button class="btn btn-secondary btn-block" id="tstart">${tx(s.pending ? 'Finish setting up 2FA' : 'Set up 2FA')}</button>`;
    box.querySelector('#tstart').onclick = async () => {
      let r; try { r = await api('/api/me/2fa/start', { method: 'POST' }); } catch (e) { return toast(e.message, 1); }
      box.innerHTML = `<p class="desc">${tx('Scan with Google Authenticator or Authy, then enter the 6-digit code.')}</p>
        <div class="qr"><div id="tq"></div></div><div class="codebox" style="text-align:center">${esc(r.secret)}</div>
        <div class="field"><label for="tcode">${tx('6-digit code')}</label><input id="tcode" class="input" inputmode="numeric" autocomplete="one-time-code" placeholder="000000"></div>
        <div id="tferr" class="err" role="alert"></div><button class="btn btn-primary btn-block" id="tconf">${tx('Turn on 2FA')}</button>`;
      renderQR(box.querySelector('#tq'), r.uri);
      const go = async () => {
        try { await api('/api/me/2fa/enable', { method: 'POST', body: JSON.stringify({ code: box.querySelector('#tcode').value.trim() }) }); toast(t('2FA enabled')); draw(); }
        catch (e) { box.querySelector('#tferr').textContent = e.message; }
      };
      box.querySelector('#tconf').onclick = go;
      box.querySelector('#tcode').addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
      box.querySelector('#tcode').focus();
    };
  }).catch(() => { box.innerHTML = `<p class="desc">${tx('Could not load 2FA status.')}</p>`; });
  box.style.display = 'flex'; box.style.flexDirection = 'column'; box.style.gap = 'var(--space-3)';
  draw();
}
function modalSecurity() {
  const ov = openSheet(`${shHead(t('Settings'), t('Security'))}
    <span class="kicker">${tx('Username and password')}</span>
    <div class="field"><label for="pu">${tx('Username')}</label><input id="pu" class="input" value="${esc((S.me || {}).username || '')}" autocapitalize="off" spellcheck="false" autocomplete="username"></div>
    <div class="form-grid"><div class="field"><label for="pc">${tx('Current password')}</label><input id="pc" class="input" type="password" autocomplete="current-password"></div>
      <div class="field"><label for="pn">${tx('New password')}</label><input id="pn" class="input" type="password" autocomplete="new-password" placeholder="${tx('Leave blank to keep')}"></div></div>
    <div id="pwerr" class="err" role="alert"></div><button class="btn btn-primary btn-block" id="pwsave">${tx('Save changes')}</button>
    <span class="kicker" style="margin-top:var(--space-4)">${tx('Two-factor authentication')}</span><div id="tfa"><span class="spin"></span></div>`, { label: t('Security') });
  ov.querySelector('#pwsave').onclick = async () => {
    const cur = ov.querySelector('#pc').value, nw = ov.querySelector('#pn').value, un = ov.querySelector('#pu').value.trim(), er = ov.querySelector('#pwerr');
    er.textContent = '';
    if (!cur) { er.textContent = t('Enter your current password.'); return; }
    if (nw && nw.length < 6) { er.textContent = t('New password must be at least 6 characters.'); return; }
    const body = { current: cur };
    if (nw) body.new = nw;
    if (un && un !== (S.me && S.me.username)) body.username = un;
    if (!body.new && !body.username) { er.textContent = t('Nothing to change.'); return; }
    const b = ov.querySelector('#pwsave'); busy(b, true);
    try { const r = await api('/api/settings/password', { method: 'POST', body: JSON.stringify(body) });
      S.me.username = r.username; toast(body.username ? t('Account updated — sign in as “{name}” next time', { name: r.username }) : t('Password updated'));
      ov.querySelector('#pc').value = ''; ov.querySelector('#pn').value = ''; }
    catch (e) { er.textContent = e.message; }
    busy(b, false, t('Save changes'));
  };
  render2fa(ov.querySelector('#tfa'));
}
function modalAccount() {
  const ov = openSheet(`${shHead(t('Settings'), t('My account'), tx('Signed in as {name}', { name: (S.me || {}).username || '' }))}
    <div class="form-grid"><div class="field"><label for="ac">${tx('Current password')}</label><input id="ac" class="input" type="password" autocomplete="current-password"></div>
      <div class="field"><label for="an2">${tx('New password')}</label><input id="an2" class="input" type="password" autocomplete="new-password"></div></div>
    <div id="aerr" class="err" role="alert"></div><button class="btn btn-primary btn-block" id="asave">${tx('Change password')}</button>
    <span class="kicker" style="margin-top:var(--space-4)">${tx('Two-factor authentication')}</span><div id="tfa"><span class="spin"></span></div>`, { label: t('My account') });
  render2fa(ov.querySelector('#tfa'));
  ov.querySelector('#asave').onclick = async () => {
    const cur = ov.querySelector('#ac').value, nw = ov.querySelector('#an2').value, er = ov.querySelector('#aerr'); er.textContent = '';
    if (!cur) { er.textContent = t('Enter your current password.'); return; }
    if (nw.length < 6) { er.textContent = t('New password must be at least 6 characters.'); return; }
    const b = ov.querySelector('#asave'); busy(b, true);
    try { await api('/api/me/password', { method: 'POST', body: JSON.stringify({ current: cur, new: nw }) }); toast(t('Password changed')); closeModal(); }
    catch (e) { er.textContent = e.message; busy(b, false, t('Change password')); }
  };
}

/* ================================================= knobs, links, backup */
/* Knob labels come from the server's spec and are looked up by key, so the
   server stays the source of truth for what a knob *is* and a knob added
   later is readable before anyone translates it. */
function knobText(spec, which) { const k = 'knob.' + spec.key + '.' + which, v = t(k); return v === k ? (spec[which] || '') : v; }
async function modalKnobs() {
  const ov = openSheet(`${shHead(t('Settings'), t('Currency & thresholds'))}<span class="spin"></span>`, { wide: true });
  let d; try { d = await api('/api/settings/panel'); } catch (e) { setSheet(ov, shHead(t('Settings'), t('Currency & thresholds')) + `<p class="err">${esc(e.message)}</p>`); return; }
  const field = s => `<div class="field"><label for="k_${esc(s.key)}">${esc(knobText(s, 'label'))}${s.type === 'str' ? '' : ` <span class="num" style="font-weight:400;color:var(--color-neutral-600)">${s.min}–${s.max}</span>`}</label>
    <input id="k_${esc(s.key)}" class="input knob" data-key="${esc(s.key)}" data-type="${esc(s.type)}" ${s.type === 'str' ? '' : `type="number" min="${s.min}" max="${s.max}"`} value="${esc(d.values[s.key])}">
    ${s.help ? `<div class="hint">${esc(knobText(s, 'help'))}</div>` : ''}</div>`;
  setSheet(ov, `${shHead(t('Settings'), t('Currency & thresholds'))}<p class="desc">${tx('Everything the panel runs on. Changes apply immediately — no restart.')}</p>
    <div class="form-grid">${d.spec.map(field).join('')}</div><div id="kerr" class="err" role="alert"></div>
    <div class="btn-row"><button class="btn btn-secondary" id="kreset">${tx('Reset all to defaults')}</button><button class="btn btn-primary" id="ksave">${tx('Save')}</button></div>`);
  ov.querySelector('#ksave').onclick = async () => {
    const body = {};
    ov.querySelectorAll('.knob').forEach(i => { body[i.dataset.key] = i.dataset.type === 'str' ? i.value.trim() : Number(i.value); });
    const er = ov.querySelector('#kerr'), b = ov.querySelector('#ksave'); er.textContent = ''; busy(b, true);
    try { const r = await api('/api/settings/panel', { method: 'PUT', body: JSON.stringify(body) });
      S.currency = r.values.currency || S.currency; toast(t('Settings saved')); closeModal(); if (S.route === 'users') renderKpis(); }
    catch (e) { er.textContent = e.message; busy(b, false, t('Save')); }
  };
  ov.querySelector('#kreset').onclick = async () => {
    if (!ask('Reset every panel setting to its default?')) return;
    try { await api('/api/settings/panel/reset', { method: 'POST' }); toast(t('Back to defaults')); modalKnobs(); }
    catch (e) { ov.querySelector('#kerr').textContent = e.message; }
  };
}
/* When set, that hostname serves the customer page and nothing else — the
   dashboard and /api 404 there, so a forwarded link never carries the panel's
   address with it. */
async function modalProfile() {
  const ov = openSheet(`${shHead(t('Settings'), t('Customer links'))}<span class="spin"></span>`);
  let d; try { d = await api('/api/settings/profile'); } catch (e) { setSheet(ov, shHead(t('Settings'), t('Customer links')) + `<p class="err">${esc(e.message)}</p>`); return; }
  setSheet(ov, `${shHead(t('Settings'), t('Customer links'))}
    <p class="desc">${tx('Point a subdomain here and customers get a short link like star.example.com/230-x7k2. It keeps working when you issue them a new config.')}</p>
    <div class="field"><label for="pfu">${tx('Customer profile site')}</label><input id="pfu" class="input" placeholder="https://star.example.com" value="${esc(d.baseUrl || '')}">
      <div class="hint">${tx('Leave empty to serve everything on this one domain. Point the DNS at this server first.')}</div></div>
    <div id="pferr" class="err" role="alert"></div>
    <div class="btn-row end"><button class="btn btn-secondary" data-act="close">${tx('Cancel')}</button><button class="btn btn-primary" id="pfsave">${tx('Save')}</button></div>`);
  ov.querySelector('#pfsave').onclick = async () => {
    const er = ov.querySelector('#pferr'), b = ov.querySelector('#pfsave'); er.textContent = ''; busy(b, true);
    try { await api('/api/settings/profile', { method: 'PUT', body: JSON.stringify({ baseUrl: ov.querySelector('#pfu').value.trim() }) });
      toast(t('Settings saved')); closeModal(); await afterChange(false); }
    catch (e) { er.textContent = e.message; busy(b, false, t('Save')); }
  };
}
// Backup: the one-click export, the scheduled on-disk snapshots (which the
// server has been taking all along with nothing in the panel to show them),
// and restore.
async function modalBackup() {
  const ov = openSheet(`${shHead(t('Settings'), t('Backup & restore'))}
    <p class="desc">${tx('Download a JSON snapshot of every server, key and setting. Keep it somewhere safe.')}</p>
    <button class="btn btn-primary btn-block" id="bkdl">${ic('down', 16)}${tx('Download backup')}</button>
    <span class="kicker" style="margin-top:var(--space-2)">${tx('Scheduled copies on this server')}</span><div id="snaps"><span class="spin"></span></div>
    <span class="kicker" style="margin-top:var(--space-2)">${tx('Restore')}</span>
    <p class="desc">${tx('Restoring replaces all current data.')}</p>
    <input id="bkfile" class="input" type="file" accept="application/json,.json">
    <div id="bkerr" class="err" role="alert"></div><button class="btn btn-danger btn-block" id="bkres">${tx('Restore from file')}</button>`, { wide: true, label: t('Backup & restore') });
  ov.querySelector('#bkdl').onclick = async () => {
    try { const r = await fetch('/api/backup', { credentials: 'same-origin' }); if (!r.ok) throw new Error(t('Download failed'));
      saveBlob(await r.blob(), 'outline-panel-backup-' + new Date().toISOString().slice(0, 10) + '.json'); toast(t('Backup downloaded')); }
    catch (e) { toast(e.message, 1); }
  };
  const snaps = async () => {
    const box = ov.querySelector('#snaps'); if (!box) return;
    let d; try { d = await api('/api/snapshots'); } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
    box.innerHTML = `<p class="hint" style="margin:0 0 var(--space-2)">${esc(d.enabled ? t('Every {h}h, keeping the newest {k}. Last: {last}.', { h: d.everyHours, k: d.keep, last: d.lastTs ? fmtDateTime(d.lastTs) : t('never') }) : t('Scheduled backups are off.'))}</p>
      ${d.snapshots.slice(0, 8).map(s => `<div class="srow" style="cursor:default"><div style="flex:1;min-width:0"><div class="mono" style="font-size:13px;overflow:hidden;text-overflow:ellipsis">${esc(s.name)}</div>
        <div class="hint" style="margin:0">${esc(fmtDateTime(s.ts))} · ${esc(fmtBytes(s.bytes))}</div></div>
        <button class="icon-btn" data-snap="dl" data-name="${esc(s.name)}" title="${tx('Download')}" aria-label="${tx('Download')}">${ic('down', 15)}</button>
        <button class="icon-btn" data-snap="rm" data-name="${esc(s.name)}" title="${tx('Delete')}" aria-label="${tx('Delete')}">×</button></div>`).join('')}
      <button class="btn btn-secondary btn-block" id="snapnow">${tx('Take one now')}</button>`;
    box.querySelector('#snapnow').onclick = async e => { busy(e.currentTarget, true);
      try { await api('/api/snapshots', { method: 'POST' }); toast(t('Snapshot written')); } catch (er) { toast(er.message, 1); } snaps(); };
    box.querySelectorAll('[data-snap]').forEach(b => b.onclick = async () => {
      const n = b.dataset.name;
      if (b.dataset.snap === 'dl') {
        try { const r = await fetch('/api/snapshots/' + encodeURIComponent(n), { credentials: 'same-origin' }); if (!r.ok) throw new Error(t('Download failed')); saveBlob(await r.blob(), n); }
        catch (e) { toast(e.message, 1); }
      } else if (ask('Delete the snapshot {name}?', { name: n })) {
        try { await api('/api/snapshots/' + encodeURIComponent(n), { method: 'DELETE' }); snaps(); } catch (e) { toast(e.message, 1); }
      }
    });
  };
  snaps();
  ov.querySelector('#bkres').onclick = async () => {
    const f = ov.querySelector('#bkfile').files[0], er = ov.querySelector('#bkerr'); er.textContent = '';
    if (!f) { er.textContent = t('Choose a backup file first.'); return; }
    if (!ask('Restore will REPLACE all current data. Continue?')) return;
    const b = ov.querySelector('#bkres'); busy(b, true);
    try { const data = JSON.parse(await f.text());
      const r = await api('/api/restore', { method: 'POST', body: JSON.stringify(data) });
      toast(t('Restored {s} servers and {k} keys', { s: r.servers, k: r.keys })); closeModal(); S.health = null; S.audit = null; await afterChange(true); }
    catch (e) { er.textContent = e instanceof SyntaxError ? t('Invalid backup file') : e.message; busy(b, false, t('Restore from file')); }
  };
}
function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ================================================================= ledger */
const LEDGER_LABEL = { topup: 'Credit added', purchase: 'Sold', reversal: 'Refunded (sale failed)', adjust: 'Correction' };
async function modalLedger(adminId, who) {
  const url = adminId ? '/api/admins/' + adminId + '/ledger' : '/api/me/ledger';
  const ov = openSheet(`${shHead(t('Statement'), who || t('My credit'))}<span class="spin"></span>`, { wide: true });
  let d; try { d = await api(url); } catch (e) { setSheet(ov, shHead(t('Statement'), who || '') + `<p class="err">${esc(e.message)}</p>`); return; }
  setSheet(ov, `${shHead(t('Statement'), who || t('My credit'))}<p class="desc">${tx('Every movement of credit, newest first.')}</p>
    <div>${d.entries.map(e => { const up = e.delta > 0; return `<div class="srow" style="cursor:default"><span class="sdot" style="background:${up ? 'var(--live)' : 'var(--color-neutral-400)'}"></span>
      <div style="flex:1;min-width:0"><div style="font-weight:700">${tx(LEDGER_LABEL[e.reason] || e.reason)}${e.package_name ? ' · ' + esc(e.package_name) : ''}</div>
        <div class="hint" style="margin:0">${esc(fmtDate(e.created_ts))}${e.key_id ? ' · ' + tx('user #{id}', { id: e.key_id }) : ''}${e.note ? ' · ' + esc(e.note) : ''}</div></div>
      <div style="text-align:end"><div class="num" style="font-weight:700;color:${up ? 'var(--ok)' : 'var(--color-text)'}">${up ? '+' : ''}${esc(fmtMoney(e.delta))}</div>
        <div class="hint num" style="margin:0">${esc(fmtMoney(e.balance_after))}</div></div></div>`; }).join('') || `<p class="desc">${tx('Nothing yet.')}</p>`}</div>
    ${adminId ? `<button class="btn btn-secondary btn-block" data-act="adminedit" data-id="${adminId}">${tx('Back')}</button>` : ''}`);
}

/* ================================================================= admins */
const CAP_LABELS = { 'keys.view': 'See users & stats', 'keys.create': 'Create users', 'keys.edit': 'Edit users',
  'keys.delete': 'Delete users', 'servers.manage': 'Manage servers', 'bot.manage': 'Telegram bot' };
function adminForm(a) {
  const servers = S.adminServers || S.servers.map(s => ({ id: s.id, name: s.name }));
  const caps = S.adminCaps || Object.keys(CAP_LABELS);
  const box = (cls, v, on, label) => `<label class="chip" style="justify-content:flex-start"><input type="checkbox" class="${cls}" value="${esc(v)}" ${on ? 'checked' : ''}>${esc(label)}</label>`;
  return `<div class="form-grid">
    <div class="field"><label for="aun">${tx('Username')}</label><input id="aun" class="input" ${a ? `value="${esc(a.username)}" disabled` : `placeholder="${tx('e.g. sara')}"`} autocapitalize="off" spellcheck="false"></div>
    <div class="field"><label for="apw">${tx(a ? 'New password (blank keeps it)' : 'Password')}</label><input id="apw" class="input" type="password" placeholder="${tx('At least 6 characters')}" autocomplete="new-password"></div></div>
    <div class="field"><label>${tx('Servers they can use')}</label><div class="btn-row" style="gap:var(--space-2)">${servers.map(s => box('asrv', s.id, a && a.servers.includes(s.id), s.name)).join('') || `<span class="desc">${tx('No servers yet.')}</span>`}</div></div>
    <div class="field"><label>${tx('What they can do')}</label><div class="btn-row" style="gap:var(--space-2)">${caps.map(c => box('acap', c, a && a.caps.includes(c), t(CAP_LABELS[c] || c))).join('')}</div></div>
    <label class="srow" style="margin:0"><input type="checkbox" id="acr" ${a && a.creditEnabled ? 'checked' : ''}><div style="flex:1"><div style="font-weight:700">${tx('Sell from packages, on credit')}</div><div class="hint" style="margin:0">${tx('Off: they create users freely.')}</div></div></label>
    <div class="form-grid">
      <div class="field"><label for="adsc">${tx('Discount %')}</label><input id="adsc" class="input" type="number" min="0" max="100" value="${a ? a.discountPct : 0}"></div>
      ${a ? `<div class="field"><label>${tx('Balance')}</label><div class="input num" style="display:flex;align-items:center;color:var(--color-accent-700);font-weight:700">${esc(fmtMoney(a.credit))}</div></div>`
          : `<div class="field"><label for="acrd">${tx('Opening credit')}</label><input id="acrd" class="input" type="number" min="0" placeholder="0"></div>`}
      <div class="field full"><label for="atg">${tx('Telegram ID (optional)')}</label><input id="atg" class="input" type="number" value="${a && a.telegramId ? a.telegramId : ''}" placeholder="${tx('They get it by sending /id to the bot')}">
        <div class="hint">${tx('Links their Telegram to this account, so the bot and Mini App give them exactly these rights.')}</div></div></div>
    ${a ? `<div class="field"><label for="atop">${tx('Add or correct credit')}</label><div class="btn-row"><input id="atop" class="input" type="number" style="flex:1" placeholder="${tx('e.g. 500000 or -50000')}"><button class="btn btn-secondary" id="atopgo" style="flex:none">${tx('Apply')}</button></div>
      <div class="hint">${tx('Every change is recorded.')} <a href="#" id="aledger">${tx('See statement')}</a></div></div>
      <label class="srow" style="margin:0"><input type="checkbox" id="adis" ${a.disabled ? 'checked' : ''}><div style="flex:1;font-weight:700">${tx('Disabled (signs them out immediately)')}</div></label>` : ''}
    <div id="aerr" class="err" role="alert"></div>`;
}
const picked = (ov, cls) => [...ov.querySelectorAll('.' + cls)].filter(x => x.checked).map(x => x.value);
async function modalAdminAdd() {
  if (!S.adminServers) await loadAdmins();
  const ov = openSheet(`${shHead(t('Admins'), t('Add an admin'))}<p class="desc">${tx('They sign in with this username and password.')}</p>${adminForm(null)}
    <div class="btn-row end"><button class="btn btn-secondary" data-act="close">${tx('Cancel')}</button><button class="btn btn-primary" id="ago">${tx('Create admin')}</button></div>`, { wide: true });
  ov.querySelector('#ago').onclick = async () => {
    const er = ov.querySelector('#aerr'); er.textContent = '';
    const body = { username: ov.querySelector('#aun').value.trim(), password: ov.querySelector('#apw').value,
      servers: picked(ov, 'asrv'), caps: picked(ov, 'acap'), credit_enabled: ov.querySelector('#acr').checked,
      discount_pct: parseInt(ov.querySelector('#adsc').value || '0', 10) || 0,
      credit: parseInt(ov.querySelector('#acrd').value || '0', 10) || 0,
      telegram_id: parseInt(ov.querySelector('#atg').value || '0', 10) || null };
    if (!body.username) { er.textContent = t('Enter a username.'); return; }
    if (body.password.length < 6) { er.textContent = t('Password must be at least 6 characters.'); return; }
    if (!body.servers.length) { er.textContent = t('Pick at least one server.'); return; }
    const b = ov.querySelector('#ago'); busy(b, true);
    try { await api('/api/admins', { method: 'POST', body: JSON.stringify(body) }); toast(t('Admin created')); closeModal(); await loadAdmins(); if (S.route === 'admins') renderPage(); renderNav(); }
    catch (e) { er.textContent = e.message; busy(b, false); }
  };
}
async function modalAdminEdit(id) {
  await loadAdmins();
  const a = S.admins.find(x => String(x.id) === String(id));
  if (!a) { closeModal(); return; }
  const ov = openSheet(`${shHead(t('Admins'), a.username, tx('Changes apply immediately, on their next request.'))}${adminForm(a)}
    <div class="btn-row" style="align-items:center"><button class="btn btn-secondary" id="adel" style="flex:none">${tx('Delete admin')}</button><span class="spacer"></span>
      <button class="btn btn-secondary" data-act="close" style="flex:none">${tx('Cancel')}</button><button class="btn btn-primary" id="ago" style="flex:none">${tx('Save changes')}</button></div>`, { wide: true });
  ov.querySelector('#ago').onclick = async () => {
    const er = ov.querySelector('#aerr'); er.textContent = '';
    const pw = ov.querySelector('#apw').value;
    const body = { servers: picked(ov, 'asrv'), caps: picked(ov, 'acap'), disabled: ov.querySelector('#adis').checked,
      credit_enabled: ov.querySelector('#acr').checked, discount_pct: parseInt(ov.querySelector('#adsc').value || '0', 10) || 0,
      telegram_id: parseInt(ov.querySelector('#atg').value || '0', 10) || null };
    if (pw) { if (pw.length < 6) { er.textContent = t('Password must be at least 6 characters.'); return; } body.password = pw; }
    if (!body.servers.length) { er.textContent = t('Pick at least one server.'); return; }
    const b = ov.querySelector('#ago'); busy(b, true);
    try { await api('/api/admins/' + a.id, { method: 'PUT', body: JSON.stringify(body) }); toast(t('Saved')); closeModal(); await loadAdmins(); if (S.route === 'admins') renderPage(); }
    catch (e) { er.textContent = e.message; busy(b, false); }
  };
  ov.querySelector('#atopgo').onclick = async () => {
    const d2 = parseInt(ov.querySelector('#atop').value || '0', 10) || 0, er = ov.querySelector('#aerr'); er.textContent = '';
    if (!d2) { er.textContent = t('Enter an amount to add (or a negative one to correct).'); return; }
    try { await api('/api/admins/' + a.id + '/credit', { method: 'POST', body: JSON.stringify({ delta: d2, note: '' }) }); toast(t('Credit updated')); modalAdminEdit(a.id); }
    catch (e) { er.textContent = e.message; }
  };
  ov.querySelector('#aledger').onclick = e => { e.preventDefault(); modalLedger(a.id, a.username); };
  ov.querySelector('#adel').onclick = async () => {
    if (!ask('Delete “{name}”? They are signed out immediately, and their customers move to you.', { name: a.username })) return;
    try { await api('/api/admins/' + a.id, { method: 'DELETE' }); toast(t('Admin deleted')); closeModal(); if (S.adminFilter === String(a.id)) S.adminFilter = 'all'; await loadAdmins(); await afterChange(false); }
    catch (e) { toast(e.message, 1); }
  };
}

/* =============================================================== packages */
function pkgForm(p) {
  return `<div class="field"><label for="pn">${tx('Name')}</label><input id="pn" class="input" maxlength="60" value="${p ? esc(p.name) : ''}" placeholder="${tx('e.g. 5 GB · 1 month')}"></div>
    <div class="form-grid">
      <div class="field"><label for="pg">${tx('Data · GB')}</label><input id="pg" class="input" type="number" min="0" step="0.5" value="${p && p.gb != null ? p.gb : ''}" placeholder="${tx('0 = unlimited')}"></div>
      <div class="field"><label for="pd">${tx('Days')}</label><input id="pd" class="input" type="number" min="0" value="${p && p.days ? p.days : ''}" placeholder="${tx('0 = no expiry')}"></div>
      <div class="field"><label for="pm">${tx('Monthly quota · GB')}</label><input id="pm" class="input" type="number" min="0" step="0.5" value="${p && p.monthlyGb ? p.monthlyGb : ''}" placeholder="${tx('Off')}"></div>
      <div class="field"><label for="pp">${tx('Price')}</label><input id="pp" class="input" type="number" min="0" value="${p ? p.basePrice : ''}" placeholder="${tx('e.g. 30000')}"></div></div>
    <div id="perr" class="err" role="alert"></div>`;
}
function pkgBody(ov) {
  const num = id => parseFloat(ov.querySelector(id).value || '0') || 0;
  return { name: ov.querySelector('#pn').value.trim(), gb: num('#pg') || null, days: parseInt(ov.querySelector('#pd').value || '0', 10) || null,
    monthly_gb: num('#pm') || null, price: parseInt(ov.querySelector('#pp').value || '0', 10) || 0 };
}
function modalPackageAdd() {
  const ov = openSheet(`${shHead(t('Packages'), t('Add a package'))}<p class="desc">${tx('Leave data or days at 0 for unlimited.')}</p>${pkgForm(null)}
    <div class="btn-row end"><button class="btn btn-secondary" data-act="close">${tx('Cancel')}</button><button class="btn btn-primary" id="pgo">${tx('Create package')}</button></div>`);
  ov.querySelector('#pgo').onclick = async () => {
    const body = pkgBody(ov), er = ov.querySelector('#perr'); er.textContent = '';
    if (!body.name) { er.textContent = t('Enter a name.'); return; }
    const b = ov.querySelector('#pgo'); busy(b, true);
    try { await api('/api/packages', { method: 'POST', body: JSON.stringify(body) }); toast(t('Package added')); closeModal(); if (S.route === 'packages') renderPage(); else go('packages'); }
    catch (e) { er.textContent = e.message; busy(b, false); }
  };
}
async function modalPackageEdit(id) {
  let d; try { d = await api('/api/packages'); } catch (e) { toast(e.message, 1); return; }
  const p = d.packages.find(x => String(x.id) === String(id)); if (!p) return;
  const ov = openSheet(`${shHead(t('Packages'), p.name, tx('Editing the price does not change what was already sold.'))}${pkgForm(p)}
    <div class="btn-row" style="align-items:center"><button class="btn btn-secondary" id="pdel" style="flex:none">${tx('Delete package')}</button><span class="spacer"></span>
      <button class="btn btn-secondary" data-act="close" style="flex:none">${tx('Cancel')}</button><button class="btn btn-primary" id="pgo" style="flex:none">${tx('Save changes')}</button></div>`);
  ov.querySelector('#pgo').onclick = async () => {
    const body = pkgBody(ov), er = ov.querySelector('#perr'); er.textContent = '';
    if (!body.name) { er.textContent = t('Enter a name.'); return; }
    const b = ov.querySelector('#pgo'); busy(b, true);
    try { await api('/api/packages/' + p.id, { method: 'PUT', body: JSON.stringify(body) }); toast(t('Saved')); closeModal(); renderPage(); }
    catch (e) { er.textContent = e.message; busy(b, false); }
  };
  ov.querySelector('#pdel').onclick = async () => {
    if (!ask('Delete “{name}”? Past sales keep their history.', { name: p.name })) return;
    try { await api('/api/packages/' + p.id, { method: 'DELETE' }); toast(t('Package deleted')); closeModal(); renderPage(); }
    catch (e) { toast(e.message, 1); }
  };
}

/* ================================================================ actions */
async function copyText(text, okMsg) {
  try { await navigator.clipboard.writeText(text); toast(okMsg || t('Copied')); }
  catch (_) { prompt(t('Copy:'), text); }   // no clipboard (plain http, old browser)
}
/* The customer's page, not the raw config. profileUrl is absolute when a
   profile host is set and a bare path otherwise; a reseller pasting "/sub/x"
   into a chat has pasted nothing useful, so resolve it first. */
function copyProfile(k) {
  if (!k) return;
  if (!k.profileUrl) { toast(t('This key has no customer link yet'), 1); return; }
  copyText(new URL(k.profileUrl, location.origin).href, t('Customer link copied'));
}
const copyKey = k => { if (k) copyText(tagUrl(k), t('Config copied')); };
async function keyAction(k, path, okMsg, opts = {}) {
  try { await api('/api/servers/' + k.serverId + '/keys/' + k.id + path, { method: 'POST', ...opts }); toast(okMsg); closeModal(); await afterChange(false); }
  catch (e) { toast(e.message, 1); }
}

async function bulk(op) {
  if (op === 'transfer') return bulkTransfer();
  if (op === 'servers') return bulkServers();
  if (op === 'links') return copySelectedLinks();
  if (op === 'clear') { S.selected = {}; renderList(); return; }
  const list = selectedKeys(); if (!list.length) return;
  if (op === 'delete' && !ask('Delete {n} keys? This cannot be undone.', { n: list.length })) return;
  if (op === 'disable' && !ask('Disable {n} keys?', { n: list.length })) return;
  // A batch runs one request per user; say where it is, and name what broke
  // once at the end rather than one toast per failure.
  let n = 0; const bad = [];
  for (const k of list) {
    const kb = '/api/servers/' + k.serverId + '/keys/' + k.id;
    const p = $('#selbar .bulkp'); if (p) p.textContent = ' · ' + (++n) + '/' + list.length;
    try {
      if (op === 'extend') await api(kb + '/extend', { method: 'POST', idem: idemKey(), body: JSON.stringify({ days: 30 }) });
      else if (op === 'disable') await api(kb + '/disable', { method: 'POST' });
      else if (op === 'delete') await api(kb, { method: 'DELETE', quiet: true });
    } catch (e) { bad.push(k.name + ': ' + e.message); }
  }
  S.selected = {};
  toast(bad.length ? t('{n} failed', { n: bad.length }) + ' — ' + bad[0] : t('Done'), bad.length ? 1 : 0);
  await afterChange(false);
}
async function copySelectedLinks() {
  const list = selectedKeys(); if (!list.length) return;
  const rows = list.filter(k => k.profileUrl).map(k => k.name + '\t' + new URL(k.profileUrl, location.origin).href);
  if (!rows.length) { toast(t('None of these have a customer link yet'), 1); return; }
  const missing = list.length - rows.length;
  copyText(rows.join('\n'), t('{n} links copied', { n: rows.length }) + (missing ? ' · ' + t('{n} without a link', { n: missing }) : ''));
}
async function bulkTransfer() {
  const list = selectedKeys(); if (!list.length) return;
  // Only admins who can reach every server in the selection can take it all.
  const sids = [...new Set(list.map(k => k.serverId))];
  await loadAdmins();
  const targets = S.admins.filter(a => !a.isOwner && sids.every(s => a.servers.includes(s)));
  const ov = openSheet(`${shHead(t('Transfer'), t('{n} selected', { n: list.length }))}
    <p class="desc">${tx("They move onto that admin's page and off everyone else's.")}${targets.length ? '' : ' ' + tx('No sub-admin has access to every server in this selection.')}</p>
    <div class="field"><label for="btsel">${tx('Belongs to')}</label><select id="btsel" class="input"><option value="">${esc((S.me || {}).username || 'admin')} (${tx('you')})</option>
      ${targets.map(a => `<option value="${a.id}">${esc(a.username)}</option>`).join('')}</select></div>
    <div class="btn-row end"><button class="btn btn-secondary" data-act="close">${tx('Cancel')}</button><button class="btn btn-primary" id="btgo">${tx('Transfer')}</button></div>`);
  ov.querySelector('#btgo').onclick = async () => {
    const v = ov.querySelector('#btsel').value, b = ov.querySelector('#btgo'); busy(b, true);
    let ok = 0, bad = 0;
    for (const k of list) {
      try { await api('/api/servers/' + k.serverId + '/keys/' + k.id + '/owner', { method: 'PUT', body: JSON.stringify({ admin_id: v === '' ? null : +v }) }); ok++; }
      catch (_) { bad++; }
    }
    S.selected = {}; closeModal();
    toast(bad ? t('{ok} moved, {bad} failed', { ok, bad }) : t('{n} transferred', { n: ok }), bad ? 1 : 0);
    await afterChange(false);
  };
}
/* Put a whole selection on a server, or take it off — one call per server,
   not per customer. */
function bulkServers() {
  const list = selectedKeys(); if (!list.length) return;
  const toks = new Set(list.map(k => k.subToken).filter(Boolean));
  const on = sv => S.keys.filter(k => k.serverId === sv && k.subToken && toks.has(k.subToken)).length;
  const ov = openSheet(`${shHead(t('Servers'), t('Servers for the selection'))}
    <p class="desc">${tx('One link per customer, a config on each server you add. The full allowance applies on every server.')}</p>
    ${S.servers.map(sv => { const c = on(sv.id), all = c >= list.length;
      return `<div class="srow" style="cursor:default;flex-wrap:wrap"><span class="dot ${sv.reachable ? '' : 'down'}"></span>
        <div style="flex:1;min-width:140px"><div style="font-weight:700">${esc(sv.name)}</div><div class="hint num" style="margin:0">${tx('{c} of {n} selected are on it', { c, n: list.length })}${sv.reachable ? '' : ' · ' + tx('unreachable')}</div></div>
        <button class="btn btn-secondary btn-sm" data-bulksrv="add" data-sv="${esc(sv.id)}" ${all || !sv.reachable ? 'disabled' : ''}>${tx('Add all')}</button>
        <button class="btn btn-ghost btn-sm" data-bulksrv="remove" data-sv="${esc(sv.id)}" ${c ? '' : 'disabled'}>${tx('Remove all')}</button></div>`; }).join('')}
    <div id="bserr" class="err" role="alert"></div>`, { wide: true });
  ov.querySelectorAll('[data-bulksrv]').forEach(btn => btn.onclick = async () => {
    const action = btn.dataset.bulksrv, sv = btn.dataset.sv;
    if (action === 'remove' && !ask('Take {n} customers off “{name}”? Their other servers keep working.', { n: list.length, name: srvName(sv) })) return;
    ov.querySelectorAll('[data-bulksrv]').forEach(b => { b.disabled = true; }); busy(btn, true);
    try {
      const r = await api('/api/servers/' + sv + '/bulk-servers', { method: 'POST',
        body: JSON.stringify({ action, keys: list.map(k => ({ server_id: k.serverId, key_id: String(k.id) })) }) });
      toast(t(action === 'add' ? '{n} added' : '{n} removed', { n: r.done.length }) + (r.failed.length ? ' · ' + t('{n} failed', { n: r.failed.length }) : ''), r.failed.length ? 1 : 0);
      closeModal(); await afterChange(true);
    } catch (e) { ov.querySelector('#bserr').textContent = e.message; bulkServers(); }
  });
}

/* RFC 4180: quote everything and double the quotes inside, or a user called
   Ali, "VIP" shifts every column after it. customer_link is included — it is
   the page a reseller already sends each customer by hand; accessUrl is the
   key material itself and stays out. */
const csvCell = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
function exportCsv() {
  const rows = filteredKeys();
  if (!rows.length) { toast(t('Nothing to export'), 1); return; }
  const gb = b => b == null ? '' : (b / 1073741824).toFixed(2);
  const iso = s => s ? new Date(s * 1000).toISOString().slice(0, 10) : '';
  const head = ['id', 'name', 'server', 'owner', 'customer_link', 'used_gb', 'limit_gb', 'monthly_gb', 'status', 'created', 'expiry', 'last_seen'];
  const body = rows.map(k => [k.id, k.name, k.serverName, k.ownerName || '', k.profileUrl ? new URL(k.profileUrl, location.origin).href : '',
    gb(k.used), k.limit == null ? 'unlimited' : gb(k.limit), k.monthlyBytes ? gb(k.monthlyBytes) : '',
    k.disabled ? 'disabled' : (k.pending ? 'pending' : 'active'), iso(k.createdTs), iso(k.expiry), iso(k.lastSeen)]);
  const csv = [head, ...body].map(r => r.map(csvCell).join(',')).join('\r\n');
  const who = adminFilterName();
  // BOM, so Excel opens Persian names as UTF-8 instead of mojibake.
  saveBlob(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' }),
    'users' + (who ? '-' + who.replace(/[^A-Za-z0-9._-]/g, '') : '') + '-' + new Date().toISOString().slice(0, 10) + '.csv');
  toast(t(rows.length === 1 ? '1 user exported' : '{n} users exported', { n: rows.length }));
}

/* ======================================================== event delegation */
document.addEventListener('click', e => {
  // Close the server menu on any click outside it.
  if (S.srvMenuOpen && !e.target.closest('#srvwrap')) { S.srvMenuOpen = false; renderTopbar(); }
  // `el`, not `t`: this used to be `const t`, which shadowed the translator for
  // the whole switch — every t('…') below then threw *after* its request had
  // succeeded, so Copy popped a prompt and Delete reported an error.
  const el = e.target.closest('[data-act]'); if (!el) return;
  const a = el.dataset.act, d = el.dataset;
  if (d.stop) e.stopPropagation();
  if (el.tagName === 'A') e.preventDefault();
  const k = () => keyOf(d);
  switch (a) {
    case 'close': closeModal(); break;
    case 'go': closeModal(); go(d.id); break;
    case 'create': closeModal(); modalCreate(); break;
    case 'signout': signOut(); break;
    case 'csv': exportCsv(); break;
    case 'palette': openPalette(); break;
    case 'shortcuts': openShortcuts(); break;
    case 'lang': toggleLang(); break;
    case 'srvmenu': S.srvMenuOpen = !S.srvMenuOpen; renderTopbar(); break;
    case 'srvpick': S.srvFilter = d.id; S.srvMenuOpen = false; S.selected = {}; renderTopbar(); renderNav(); if (S.route === 'users') { renderKpis(); renderList(); } else renderPage(); break;
    case 'srvfilter': S.srvFilter = d.id; go('users'); break;
    case 'statusf': S.statusFilter = d.id; renderKpis(); renderList(); break;
    case 'kpi': S.statusFilter = (S.statusFilter === d.id && d.id !== 'all') ? 'all' : d.id; renderKpis(); renderList(); break;
    case 'clearfilters': S.statusFilter = 'all'; S.search = ''; renderTopbar(); renderKpis(); renderList(); break;
    case 'adminfilter': S.adminFilter = d.id; S.statusFilter = 'all'; S.selected = {}; go('users'); break;
    case 'dismissbanner': S.bannerDismissed = true; renderBanner(); break;
    case 'selall': { const list = filteredKeys(), all = list.length && list.every(x => S.selected[selKey(x)]);
      list.forEach(x => { if (all) delete S.selected[selKey(x)]; else S.selected[selKey(x)] = true; }); renderList(); break; }
    case 'sel': { const s = d.sid + '/' + d.id; if (S.selected[s]) delete S.selected[s]; else S.selected[s] = true; renderList(); break; }
    case 'bulk': bulk(d.op); break;
    case 'copy': copyKey(k()); break;
    case 'copylink': copyProfile(k()); break;
    case 'copytext': copyText(d.text); break;
    case 'share': { const x = k(); if (x) modalShare(x); break; }
    case 'detail': { const x = k(); if (x) modalDetail(x); break; }
    case 'manage': { const x = k(); if (x) modalManage(x); break; }
    case 'config': { const x = k(); if (x) modalConfig(x); break; }
    case 'toggle': { const x = k(); if (x) keyAction(x, x.disabled ? '/enable' : '/disable', x.disabled ? t('Key enabled') : t('Key disabled')); break; }
    case 'extend30': { const x = k(); if (x) keyAction(x, '/extend', t('Extended +30d'), { idem: idemKey(), body: JSON.stringify({ days: 30 }) }); break; }
    case 'resetkey': { const x = k(); if (x && ask('Reset usage for “{name}”?', { name: x.name })) keyAction(x, '/reset', t('Usage reset')); break; }
    case 'delkey': { const x = k(); if (x) deleteKey(x); break; }
    case 'addserver': closeModal(); modalAddServer(); break;
    case 'srvdetail': { const s = srv(d.id); if (s) modalServerDetail(s); break; }
    case 'security': modalSecurity(); break;
    case 'account': modalAccount(); break;
    case 'knobs': modalKnobs(); break;
    case 'profile': modalProfile(); break;
    case 'backup': modalBackup(); break;
    case 'myledger': modalLedger(null, (S.me || {}).username); break;
    case 'adminledger': modalLedger(d.id, d.name); break;
    case 'adminadd': modalAdminAdd(); break;
    case 'adminedit': modalAdminEdit(d.id); break;
    case 'pkgadd': modalPackageAdd(); break;
    case 'pkgedit': modalPackageEdit(d.id); break;
    case 'auditmore': loadAudit(true); break;
    case 'auditreload': S.audit = null; renderPage(); break;
  }
});
// Backdrop click closes; a click that started inside the sheet (a text
// selection dragged out) must not.
let _downOnBackdrop = false;
modalRoot.addEventListener('mousedown', e => { _downOnBackdrop = e.target.classList.contains('overlay'); });
modalRoot.addEventListener('click', e => { if (e.target.classList.contains('overlay') && _downOnBackdrop) closeModal(); });
// Chips that fill an input (create sheet presets).
document.addEventListener('click', e => {
  const c = e.target.closest('[data-chip]'); if (!c) return;
  const inp = document.getElementById(c.dataset.chip); if (inp) inp.value = c.dataset.v;
  c.parentElement.querySelectorAll('[data-chip]').forEach(x => x.classList.toggle('sel', x === c));
});
let _searchT;
document.addEventListener('input', e => {
  if (e.target.dataset.inp !== 'search') return;
  S.search = e.target.value;
  clearTimeout(_searchT);
  // Debounced: every keystroke used to rebuild the whole list.
  _searchT = setTimeout(() => { if (S.route === 'users') renderList(); else renderPage(); }, 120);
});
document.addEventListener('change', e => {
  if (e.target.dataset.inp === 'sort') { S.sort = e.target.value; store.set('oc_sort', S.sort); renderList(); }
});

/* ======================================================= command palette */
function paletteCommands() {
  const c = [
    { need: 'keys.create', label: t('New key'), hint: 'N', icon: 'plus', kw: 'create add', run: modalCreate },
    { label: t('Search'), hint: '/', icon: 'search', kw: 'find', run: () => setTimeout(() => $('#search') && $('#search').focus(), 60) },
    ...Object.keys(ROUTES).filter(r => ROUTES[r].ok()).map(r => ({ label: t('Go to {page}', { page: t(ROUTES[r].label) }), icon: 'filter', kw: 'go page ' + r, run: () => go(r) })),
    { need: 'servers.manage', label: t('Add a server'), icon: 'server', kw: 'server', run: modalAddServer },
    { need: 'owner', label: t('Security & two-factor'), icon: 'shield', kw: '2fa password', run: modalSecurity },
    { need: 'owner', label: t('Backup & restore'), icon: 'down', kw: 'export import', run: modalBackup },
    { label: t('Keyboard shortcuts'), hint: '?', icon: 'keyb', kw: 'help keys', run: openShortcuts },
    { label: t('Language') + ' · ' + nextLang().label, icon: 'filter', kw: 'language زبان fa en', run: toggleLang },
    { label: t('Sign out'), icon: 'out', kw: 'logout', run: signOut },
  ].filter(x => !x.need || (x.need === 'owner' ? isOwner() : can(x.need)));
  if (can('keys.view')) {
    c.push({ label: t('Show all servers'), icon: 'filter', kw: 'filter', run: () => { S.srvFilter = 'all'; go('users'); renderTopbar(); } });
    S.servers.forEach(s => c.push({ label: t('Filter · {name}', { name: s.name }), icon: 'filter', kw: 'filter server', run: () => { S.srvFilter = s.id; go('users'); renderTopbar(); renderList(); } }));
    [['online', 'Online'], ['attention', 'Needs attention'], ['pending', 'Pending'], ['disabled', 'Disabled'], ['all', 'All']].forEach(([v, l]) =>
      c.push({ label: t('Status · {s}', { s: t(l) }), icon: 'filter', kw: 'filter status', run: () => { S.statusFilter = v; go('users'); renderKpis(); renderList(); } }));
  }
  return c;
}
function openPalette() {
  if (S.screen !== 'app') return;
  const cmds = paletteCommands(); let idx = 0, list = cmds;
  const ov = openSheet(`<div class="search" style="max-width:none;flex:none">${ic('search', 17)}<input id="cmdq" class="input" autocomplete="off" placeholder="${tx('Type a command…')}" aria-label="${tx('Type a command…')}"></div>
    <div id="cmdlist" role="listbox" style="max-height:52vh;overflow:auto;margin:0 calc(var(--space-2) * -1)"></div>`, { top: true, label: t('Command palette') });
  const q = ov.querySelector('#cmdq'), box = ov.querySelector('#cmdlist');
  const draw = () => {
    const ql = q.value.trim().toLowerCase();
    list = cmds.filter(c => !ql || c.label.toLowerCase().includes(ql) || (c.kw || '').includes(ql));
    idx = Math.min(idx, Math.max(0, list.length - 1));
    box.innerHTML = list.length ? list.map((c, i) => `<div class="cmd-item ${i === idx ? 'on' : ''}" role="option" aria-selected="${i === idx}" data-i="${i}"><span class="ci">${ic(c.icon, 16)}</span><span>${esc(c.label)}</span>${c.hint ? `<span class="hint">${esc(c.hint)}</span>` : ''}</div>`).join('')
      : `<p class="desc" style="text-align:center;padding:var(--space-6)">${tx('No matching commands')}</p>`;
  };
  const run = i => { const c = list[i]; if (c) { closeModal(); c.run(); } };
  q.addEventListener('input', () => { idx = 0; draw(); });
  q.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); idx = Math.min(idx + 1, list.length - 1); draw(); box.children[idx] && box.children[idx].scrollIntoView({ block: 'nearest' }); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); idx = Math.max(idx - 1, 0); draw(); box.children[idx] && box.children[idx].scrollIntoView({ block: 'nearest' }); }
    else if (e.key === 'Enter') { e.preventDefault(); run(idx); }
  });
  box.addEventListener('mousemove', e => { const it = e.target.closest('[data-i]'); if (it && +it.dataset.i !== idx) { idx = +it.dataset.i; draw(); } });
  box.addEventListener('click', e => { const it = e.target.closest('[data-i]'); if (it) run(+it.dataset.i); });
  draw(); setTimeout(() => q.focus(), 30);
}
function openShortcuts() {
  const row = (keys, d) => `<div class="row-line" style="padding:var(--space-2) 0;border-bottom:1px solid var(--color-divider)"><span>${tx(d)}</span><span style="display:flex;gap:4px">${keys.split('+').map(x => '<kbd>' + esc(x) + '</kbd>').join('')}</span></div>`;
  openSheet(`${shHead('', t('Keyboard shortcuts'))}<div>${row('⌘+K', 'Command palette')}${row('/', 'Focus search')}${row('N', 'New key')}${row('?', 'This help')}${row('Esc', 'Close / clear selection')}</div>`);
}
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault(); if ($('#cmdq')) closeModal(); else { closeModal(); openPalette(); } return;
  }
  if (e.key === 'Escape') {
    if (modalRoot.innerHTML) { closeModal(); return; }
    if (S.srvMenuOpen) { S.srvMenuOpen = false; renderTopbar(); return; }
    if (Object.keys(S.selected).length) { S.selected = {}; renderList(); }
    return;
  }
  const typing = /INPUT|TEXTAREA|SELECT/.test((e.target && e.target.tagName) || '') || (e.target && e.target.isContentEditable);
  if (typing || modalRoot.innerHTML || S.screen !== 'app' || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === '/') { e.preventDefault(); $('#search') && $('#search').focus(); }
  else if ((e.key === 'n' || e.key === 'N') && can('keys.create')) { e.preventDefault(); modalCreate(); }
  else if (e.key === '?') { e.preventDefault(); openShortcuts(); }
});

/* Owner-only extras that feed the chrome (admin and package counts in the
   sidebar), fetched after the first paint so they never hold it up. */
async function loadOwnerExtras() {
  if (!isOwner()) return;
  await loadAdmins();
  try { const d = await api('/api/packages', { quiet: true }); S.packagesCount = d.packages.length; } catch (_) {}
  if (S.screen === 'app') renderNav();
}
checkAuth();
