"""The /admin control-panel page — one self-contained HTML string (no CDN)."""

ADMIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>miri-api — accounts</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;
         background:#0d1117; color:#e6edf3; }
  header { padding:10px 16px; background:#161b22; border-bottom:1px solid #30363d;
           display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  .stat { color:#8b949e; font-size:12px; }
  .stat b { color:#e6edf3; }
  .wrap { display:flex; gap:14px; padding:14px; align-items:flex-start; flex-wrap:wrap; }
  .col { flex:1 1 460px; min-width:340px; }
  .vnc { flex:1 1 520px; min-width:340px; }
  .vnc iframe { width:100%; height:70vh; border:1px solid #30363d; border-radius:8px; background:#000; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px; margin-bottom:12px; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #21262d; font-size:13px; vertical-align:middle; }
  th { color:#8b949e; font-weight:500; }
  .pill { display:inline-block; padding:1px 8px; border-radius:20px; font-size:11px; font-weight:600; }
  .active{background:#1a4d2e;color:#7ee2a8} .nearing{background:#4d431a;color:#e6d27a}
  .cooldown{background:#4d3a1a;color:#e6b87a} .failed{background:#5a1e1e;color:#f0a0a0}
  .logged_out{background:#3a3a3a;color:#c9c9c9} .disabled{background:#21262d;color:#8b949e}
  .starting{background:#1a3a4d;color:#7ac6e6}
  button { background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px;
           padding:4px 9px; cursor:pointer; font-size:12px; }
  button:hover { background:#30363d; }
  button.primary { background:#238636; border-color:#238636; }
  button.danger { background:#5a1e1e; border-color:#8a2a2a; }
  input,select { background:#0d1117; color:#e6edf3; border:1px solid #30363d; border-radius:6px;
                 padding:5px 7px; font-size:12px; width:100%; }
  label { display:block; font-size:11px; color:#8b949e; margin:6px 0 2px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .muted { color:#8b949e; font-size:11px; }
  .row-actions { display:flex; gap:4px; flex-wrap:wrap; }
  .banner { padding:8px 12px; background:#1a2a3a; border:1px solid #2a4a6a; border-radius:6px; margin:10px 14px; font-size:12px; }
  /* ── Resource bars (CPU / RAM) ── */
  .res { display:flex; gap:14px; flex-wrap:wrap; }
  .res-item { flex:1 1 150px; min-width:140px; }
  .res-head { display:flex; justify-content:space-between; align-items:baseline; font-size:11px; color:#8b949e; }
  .res-head b { color:#e6edf3; font-size:12px; }
  .track { height:7px; margin-top:4px; background:#21262d; border-radius:20px; overflow:hidden; }
  .fill { height:100%; width:0; border-radius:20px; background:#238636; transition:width .4s ease, background .4s ease; }
  .fill.warn { background:#b58407; } .fill.hot { background:#a13a34; }
  /* Edit modal — same look as the Add card, so editing feels like creating. */
  .modal { position:fixed; inset:0; background:#0008; display:none; align-items:center;
           justify-content:center; z-index:1000; }
  .modal.show { display:flex; }
  .modal-card { background:#161b22; border:1px solid #30363d; border-radius:10px;
                padding:16px; width:440px; max-width:calc(100vw - 24px);
                max-height:90vh; overflow:auto; box-shadow:0 12px 40px #000a; }
  .modal-card h2 { font-size:14px; margin:0 0 4px; }
  .modal-actions { margin-top:12px; display:flex; gap:8px; align-items:center; }
  /* ── Logs viewer ── */
  .logwrap { position:fixed; inset:0; background:#0009; display:none; align-items:center; justify-content:center; z-index:1100; backdrop-filter:blur(2px); }
  .logwrap.show { display:flex; }
  .logcard { background:#0d1117; border:1px solid #30363d; border-radius:12px; width:min(1120px,96vw); height:88vh; display:flex; flex-direction:column; box-shadow:0 16px 60px #000b; overflow:hidden; }
  .logcard > header { padding:9px 12px; background:#161b22; border-bottom:1px solid #30363d; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .logcard > header h2 { font-size:14px; margin:0; }
  .chip { background:#21262d; border:1px solid #30363d; border-radius:20px; padding:1px 9px; font-size:11px; color:#8b949e; }
  .logbody { flex:1; overflow:auto; }
  .logtable { width:100%; border-collapse:collapse; font-size:12.5px; }
  .logtable th { position:sticky; top:0; background:#161b22; color:#8b949e; font-weight:500; text-align:left; padding:7px 10px; border-bottom:1px solid #30363d; z-index:1; white-space:nowrap; }
  .logtable td { padding:6px 10px; border-bottom:1px solid #161b22; vertical-align:middle; }
  .logtable tbody tr { cursor:pointer; }
  .logtable tbody tr:hover { background:#161b22; }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .m { font-weight:600; font-size:10.5px; padding:1px 6px; border-radius:5px; letter-spacing:.3px; white-space:nowrap; }
  .m-GET{background:#12283f;color:#79c0ff} .m-POST{background:#12341f;color:#7ee2a8}
  .m-PUT,.m-PATCH{background:#3a2d0a;color:#e3b341} .m-DELETE{background:#3a1414;color:#ff7b72}
  .sc { font-weight:600; font-size:11px; padding:1px 8px; border-radius:20px; white-space:nowrap; }
  .s2{background:#12341f;color:#7ee2a8} .s3{background:#12283f;color:#79c0ff}
  .s4{background:#3a2d0a;color:#e3b341} .s5{background:#3a1414;color:#ff7b72} .s0{background:#21262d;color:#8b949e}
  .tag { font-size:9.5px; color:#c297ff; border:1px solid #3a2d55; border-radius:4px; padding:0 4px; margin-left:5px; vertical-align:middle; }
  .detail-sec { border:1px solid #30363d; border-radius:8px; margin:10px 12px; overflow:hidden; }
  .detail-sec > .hd { display:flex; align-items:center; gap:8px; padding:7px 10px; background:#161b22; border-bottom:1px solid #30363d; font-size:12px; }
  .detail-sec pre { margin:0; padding:10px; max-height:36vh; overflow:auto; font-family:ui-monospace,monospace; font-size:12px; color:#c9d1d9; white-space:pre-wrap; word-break:break-word; }
  .detail-sec details pre { max-height:18vh; background:#0b0f14; }
  .detail-sec summary { list-style:none; padding:5px 10px; font-size:11px; color:#8b949e; cursor:pointer; border-bottom:1px solid #161b22; }
  .kv { font-size:11.5px; color:#8b949e; padding:8px 12px; border-bottom:1px solid #21262d; }
  .kv b { color:#c9d1d9; }
  .linkbtn { background:none; border:none; color:#7ac6e6; cursor:pointer; font-size:12px; padding:2px 4px; }
  .linkbtn:hover { text-decoration:underline; }
</style>
</head>
<body>
<header>
  <h1>miri-api</h1>
  <span class="stat">tabs <b id="s-tabs">–</b> · schedulable <b id="s-sched">–</b>
     · idle <b id="s-idle">–</b> · queue <b id="s-queue">–</b> <b id="s-sat"></b></span>
  <span style="margin-left:auto"></span>
  <button onclick="openLogs()">📋 Logs</button>
  <button onclick="setToken()">Set admin token</button>
  <button onclick="refresh()">Refresh</button>
</header>

<div class="wrap">
  <div class="col">
    <div class="card" style="padding:6px 8px">
      <div id="tabs" style="display:flex;gap:6px;flex-wrap:wrap"></div>
    </div>
    <div class="card" id="res-card">
      <div class="res">
        <div class="res-item">
          <div class="res-head"><span>CPU <span id="res-cpu-sub" class="muted"></span></span><b id="res-cpu-val">–</b></div>
          <div class="track"><div class="fill" id="res-cpu-bar"></div></div>
        </div>
        <div class="res-item">
          <div class="res-head"><span>RAM <span id="res-mem-sub" class="muted"></span></span><b id="res-mem-val">–</b></div>
          <div class="track"><div class="fill" id="res-mem-bar"></div></div>
        </div>
      </div>
      <div class="muted" id="res-note" style="margin-top:6px"></div>
    </div>
    <div class="card">
      <b>Request chat policy</b>
      <div class="muted" style="margin:4px 0 8px">These settings apply to new requests immediately and are persisted across restarts.</div>
      <label style="display:flex;align-items:center;gap:7px;width:auto;color:#e6edf3">
        <input type="checkbox" id="setting-new-chat" style="width:auto" onchange="updateSetting('new_chat_every_request',this.checked)"/>
        Start every request in a new chat
      </label>
      <div class="muted" style="margin:2px 0 7px 23px">Starts each generation in a fresh saved chat.</div>
      <label style="display:flex;align-items:center;gap:7px;width:auto;color:#e6edf3">
        <input type="checkbox" id="setting-temp-chat" style="width:auto" onchange="updateSetting('temporary_chats',this.checked)"/>
        Temporary chats
      </label>
      <div class="muted" style="margin:2px 0 0 23px">Opens a native temporary/incognito chat and leaves it after the response.</div>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>Account</th><th>State</th><th>Tabs</th><th>Usage (est.)</th><th></th></tr></thead>
        <tbody id="accts"></tbody>
      </table>
    </div>

    <div class="card">
      <b>Proxy pool</b> <span class="muted" id="px-count"></span>
      <div class="muted" style="margin:4px 0">One proxy per line: <code>scheme://[user:pass@]host:port</code>
        (http / https / socks5 / socks4). Account <b>N</b> uses proxy
        <b>((N‑1) mod count)+1</b>; with fewer proxies than accounts it wraps back
        to proxy 1. An account's explicit proxy (if set) overrides its pool slot.</div>
      <textarea id="px-text" rows="5" spellcheck="false"
        style="width:100%;font-family:ui-monospace,monospace;font-size:12px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px"
        placeholder="http://user:pass@gate.provider.io:7000&#10;socks5://10.0.0.5:1080"></textarea>
      <div style="margin-top:6px"><button class="primary" onclick="saveProxies()">Save proxy list</button>
        <span class="muted">Restart an account to apply a changed proxy.
        Changing a logged-in account's proxy changes its IP → expect a re-login.</span></div>
    </div>

    <div class="card">
      <b>Add account</b>
      <div class="grid2">
        <div><label>Provider</label><select id="n-prov"></select></div>
        <div><label>Label</label><input id="n-label" placeholder="Work #2"/></div>
        <div><label>Number (0 = auto → proxy slot)</label><input id="n-num" type="number" value="0"/></div>
        <div><label>Tabs (0 = default)</label><input id="n-tabs" type="number" value="0"/></div>
        <div><label>Soft cap / window (0 = off)</label><input id="n-cap" type="number" value="0"/></div>
        <div><label>Order (lower = higher priority)</label><input id="n-order" type="number" value="100"/></div>
        <div style="grid-column:1/3"><label>Proxy override (optional)</label><input id="n-proxy" placeholder="blank = use the proxy pool"/></div>
      </div>
      <div style="margin-top:8px"><button class="primary" onclick="addAccount()">Create</button>
        <span class="muted">ID and timezone are assigned automatically. Then Start it, log in via the viewer, and confirm.</span></div>
    </div>
  </div>

  <div class="vnc">
    <div class="card" style="padding:6px">
      <div class="muted" style="padding:4px 6px">Browser viewer (log in to each account here)</div>
      <iframe id="vncframe" src="about:blank" title="noVNC"></iframe>
      <div class="muted" style="padding:4px 6px" id="vnc-fallback"></div>
    </div>
  </div>
</div>

<!-- Edit account — a real form (like Add), not a chain of prompt() dialogs. -->
<div class="modal" id="editModal">
  <div class="modal-card">
    <h2>Edit account <span class="muted" id="e-id"></span></h2>
    <div class="muted" id="e-sub" style="margin-bottom:6px"></div>
    <div class="grid2">
      <div><label>Provider</label><select id="e-prov"></select></div>
      <div><label>Label</label><input id="e-label"/></div>
      <div><label>Number (proxy slot, 1-based)</label><input id="e-num" type="number"/></div>
      <div><label>Tabs (0 = default)</label><input id="e-tabs" type="number"/></div>
      <div><label>Soft cap / window (0 = off)</label><input id="e-cap" type="number"/></div>
      <div><label>Order (lower = higher priority)</label><input id="e-order" type="number"/></div>
      <div style="grid-column:1/3"><label>Proxy override (blank = use the pool)</label><input id="e-proxy"/></div>
    </div>
    <div class="modal-actions">
      <button class="primary" onclick="saveEdit()">Save</button>
      <button onclick="closeEdit()">Cancel</button>
      <span class="muted" style="margin-left:auto">Restart the account to apply a proxy change.</span>
    </div>
  </div>
</div>

<!-- Logs — every API request, with a per-request detail view. -->
<div class="logwrap" id="logsModal" onclick="if(event.target===this)closeLogs()">
  <div class="logcard">
    <header>
      <h2>Request Log</h2><span class="chip" id="log-count">0</span>
      <input id="log-filter" placeholder="filter — path / model / method / status"
             style="width:auto;flex:1;min-width:160px;max-width:360px" oninput="filterLogs()"/>
      <span style="margin-left:auto"></span>
      <label class="muted" style="display:flex;align-items:center;gap:4px;width:auto">
        <input type="checkbox" id="log-auto" checked style="width:auto"/> auto</label>
      <select id="log-dl-count" style="width:auto" title="How many of the most recent requests to include">
        <option value="25">last 25</option>
        <option value="100" selected>last 100</option>
        <option value="500">last 500</option>
        <option value="2000">last 2000</option>
        <option value="5000">last 5000</option>
      </select>
      <label class="muted" style="display:flex;align-items:center;gap:4px;width:auto"
             title="Also include the tail of every server log file (qwen_client.log, worker_pool.log, …)">
        <input type="checkbox" id="log-dl-files" checked style="width:auto"/> +server logs</label>
      <button id="log-dl" onclick="downloadLogs()" title="Download the selected requests with full bodies">⬇ Download</button>
      <button onclick="loadLogs()">Refresh</button>
      <button class="danger" onclick="clearLogs()">Clear</button>
      <button onclick="closeLogs()">Close</button>
    </header>
    <div class="logbody">
      <table class="logtable">
        <thead><tr>
          <th>Time</th><th>Method</th><th>Endpoint</th><th>Model</th>
          <th>Status</th><th>ms</th><th>Size (req→resp)</th><th></th>
        </tr></thead>
        <tbody id="log-rows"></tbody>
      </table>
      <button id="log-more" onclick="loadLogs(true)" hidden>Load older requests</button>
      <div id="log-warning" class="muted" style="padding:8px"></div>
    </div>
  </div>
</div>

<div class="logwrap" id="logDetailModal" style="z-index:1200" onclick="if(event.target===this)closeLogDetail()">
  <div class="logcard" style="width:min(920px,95vw)">
    <header>
      <button class="linkbtn" onclick="closeLogDetail()">← Back</button>
      <h2 id="ld-title" style="flex:1;font-weight:400"></h2>
      <button onclick="closeLogDetail()">Close</button>
    </header>
    <div class="logbody" id="ld-body"></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let CURRENT_PROVIDER = localStorage.getItem('miri_provider') || 'chatgpt';
function setProvider(p){
  CURRENT_PROVIDER = p; localStorage.setItem('miri_provider', p);
  // Switching provider tabs should also switch what the VIEWER shows: bring the
  // first started account of that provider to the front of the X display.
  focusFirstOf(p); refresh();
}
let LAST_STATUS = null;
async function focusFirstOf(prov){
  try{
    const st = LAST_STATUS || await api('/admin/api/status');
    const a = (st.accounts||[]).find(x=>x.provider===prov && x.started && x.tabs_live>0);
    if(a) await api(`/admin/api/accounts/${a.id}/focus?tab=0`,{method:'POST'});
  }catch(e){}
}
async function focusAcct(id,tab){
  try{ await api(`/admin/api/accounts/${id}/focus?tab=${tab}`,{method:'POST'}); }
  catch(e){ alert(e.message); }
}
// Edit uses the same kind of form as Add — a modal, not a chain of prompts.
let EDIT_ID = null;
function editAcct(id){
  const a = (LAST_STATUS?.accounts||[]).find(x=>x.id===id); if(!a) return;
  EDIT_ID = id;
  const provs = (LAST_STATUS?.providers)||[];
  $('#e-prov').innerHTML = provs.map(p=>`<option value="${p.id}">${esc(p.label)}</option>`).join('');
  $('#e-prov').value = a.provider;
  $('#e-id').textContent = '#'+a.number+' · '+a.id;
  $('#e-sub').textContent = (a.assigned_proxy||'') + ' · timezone: auto';
  $('#e-label').value = a.label||'';
  $('#e-num').value = a.number;
  $('#e-tabs').value = a.tabs;
  $('#e-cap').value = a.soft_cap;
  $('#e-order').value = a.order;
  $('#e-proxy').value = a.proxy_server||'';
  $('#editModal').classList.add('show');
}
function closeEdit(){ EDIT_ID=null; $('#editModal').classList.remove('show'); }
async function saveEdit(){
  if(!EDIT_ID) return;
  const changes = {
    provider: $('#e-prov').value,
    label: $('#e-label').value.trim(),
    number: +$('#e-num').value||0,
    tabs: +$('#e-tabs').value||0,
    soft_cap: +$('#e-cap').value||0,
    order: +$('#e-order').value||0,
    proxy_server: $('#e-proxy').value.trim(),
  };
  try{ await api(`/admin/api/accounts/${EDIT_ID}`,{method:'PATCH',body:JSON.stringify(changes)});
       closeEdit(); refresh(); }
  catch(e){ alert(e.message); }
}
function token(){ return localStorage.getItem('miri_admin_token') || ''; }
function setToken(){ const t = prompt('Admin token (ADMIN_TOKEN, or API_TOKEN):', token());
                     if(t!==null){ localStorage.setItem('miri_admin_token', t.trim()); refresh(); } }
async function api(path, opts={}){
  opts.headers = Object.assign({'Content-Type':'application/json','Authorization':'Bearer '+token()}, opts.headers||{});
  const r = await fetch(path, opts);
  if(r.status===401||r.status===403){ throw new Error('auth: set the admin token'); }
  if(!r.ok){ let d; try{d=await r.json()}catch(e){}; throw new Error((d&&d.detail)||('HTTP '+r.status)); }
  return r.status===204 ? null : r.json();
}
function esc(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

let SETTINGS_PENDING = false;
async function updateSetting(name, value){
  SETTINGS_PENDING = true;
  ['#setting-new-chat','#setting-temp-chat'].forEach(id=>$(id).disabled=true);
  try{
    const r = await api('/admin/api/settings',{method:'PATCH',body:JSON.stringify({[name]:!!value})});
    applySettings(r.settings);
  }catch(e){
    alert(e.message);
    await refresh();
  }finally{
    SETTINGS_PENDING = false;
    ['#setting-new-chat','#setting-temp-chat'].forEach(id=>$(id).disabled=false);
  }
}
function applySettings(s){
  if(!s) return;
  $('#setting-new-chat').checked = !!s.new_chat_every_request;
  $('#setting-temp-chat').checked = !!s.temporary_chats;
}

async function refresh(){
  let st;
  try { st = await api('/admin/api/status'); LAST_STATUS = st; }
  catch(e){ $('#accts').innerHTML = '<tr><td colspan=5 class=muted>'+esc(e.message)+'</td></tr>'; return; }
  // Provider tabs — each provider is its own account list, sharing nothing.
  const provs = st.providers || [];
  $('#tabs').innerHTML = provs.map(p=>{
    const on = p.id===CURRENT_PROVIDER;
    return `<button onclick="setProvider('${p.id}')" style="${on?'background:#238636;border-color:#238636;':''}">`
      + `${esc(p.label)} <span class="muted" style="${on?'color:#dfd':''}">${p.schedulable}/${p.tabs}${p.saturated?' busy':''}</span></button>`;
  }).join('');
  const sel = $('#n-prov');
  if (sel && sel.options.length !== provs.length) {
    sel.innerHTML = provs.map(p=>`<option value="${p.id}">${esc(p.label)}</option>`).join('');
  }
  if (sel) sel.value = CURRENT_PROVIDER;
  renderResources(st.resources);
  if(!SETTINGS_PENDING) applySettings(st.settings);
  $('#s-tabs').textContent = st.tabs_total;
  $('#s-sched').textContent = st.tabs_schedulable;
  $('#s-idle').textContent = st.tabs_idle;
  $('#s-queue').textContent = st.queue_depth;
  $('#s-sat').textContent = st.saturated ? '· SATURATED' : '';
  // VNC iframe
  const vnc = st.vnc_url || (location.protocol+'//'+location.hostname+':6080/vnc.html');
  const f = $('#vncframe');
  if(f.dataset.src !== vnc){ f.dataset.src = vnc; f.src = vnc;
    $('#vnc-fallback').innerHTML = 'If blank: <a style="color:#7ac6e6" target="_blank" href="'+esc(vnc)+'">open the viewer in a new tab</a>'; }
  // accounts
  $('#accts').innerHTML = st.accounts.filter(a=>a.provider===CURRENT_PROVIDER).map(a=>{
    const u = a.usage||{};
    const usage = `${u.requests_in_window??0}/win · ${u.requests_total??0} tot · ${u.limit_hits??0} limits`
      + (u.cooldown_remaining_s ? ` · cd ${Math.round(u.cooldown_remaining_s/60)}m` : '');
    const btns = [];
    const signedIn = (a.state==='active'||a.state==='nearing'||a.state==='cooldown');
    if(!a.started) btns.push(`<button onclick="act('${a.id}','start')">Start</button>`);
    else btns.push(`<button onclick="act('${a.id}','stop')">Stop</button>`);
    // Log in opens the provider's login page on this account's tab (starting it
    // if needed) — sign in via the viewer, then confirm.
    if(!signedIn) btns.push(`<button class="primary" onclick="act('${a.id}','login-open')">Log in</button>`);
    else btns.push(`<button onclick="logout('${a.id}')">Log out</button>`);
    if(a.state==='logged_out') btns.push(`<button class="primary" onclick="act('${a.id}','login-confirm')">I've logged in</button>`);
    if(a.started && a.tabs_live>0){
      btns.push(`<button onclick="focusAcct('${a.id}',0)" title="Show this account in the viewer">View</button>`);
      for(let t=0;t<a.tabs_live;t++) btns.push(`<button onclick="focusAcct('${a.id}',${t})" title="View tab ${t+1}">t${t+1}</button>`);
    }
    btns.push(`<button onclick="editAcct('${a.id}')">Edit</button>`);
    btns.push(`<button onclick="toggleEnabled('${a.id}',${!a.enabled})">${a.enabled?'Disable':'Enable'}</button>`);
    if(a.id!=='default') btns.push(`<button class="danger" onclick="del('${a.id}')">Del</button>`);
    return `<tr>
      <td><b>#${a.number} ${esc(a.label||a.id)}</b>
          <div class="muted">${esc(a.id)}${a.auto_promoted?' · overflow':''}</div>
          <div class="muted">${esc(a.assigned_proxy||'')}</div></td>
      <td><span class="pill ${a.state}">${a.state}</span>${a.last_error?`<div class="muted" title="${esc(a.last_error)}">err</div>`:''}</td>
      <td>${a.tabs_live}</td>
      <td class="muted">${usage}</td>
      <td class="row-actions">${btns.join('')}</td></tr>`;
  }).join('') || '<tr><td colspan=5 class=muted>No '+esc(CURRENT_PROVIDER)+' accounts yet — add one below.</td></tr>';
  loadProxies();
}
// ── CPU / RAM bars ──
// Fed by the same 4s status poll as everything else — no extra round trip.
// In Docker these are the CONTAINER's numbers (cgroup), which is what decides
// whether the browsers get OOM-killed; the host's total would say nothing.
function gib(n){ return (n/1073741824).toFixed(1)+' GiB'; }
function setBar(id, pct){
  const el = $(id);
  el.style.width = Math.max(0, Math.min(100, pct||0))+'%';
  el.className = 'fill' + (pct >= 90 ? ' hot' : pct >= 70 ? ' warn' : '');
}
function renderResources(r){
  const card = $('#res-card');
  if(!r || !r.available){
    // Say why there is no bar instead of drawing a zeroed one.
    $('#res-note').textContent = 'CPU/RAM unavailable here (no cgroup or /proc; '
      + 'outside Docker, pip install psutil).';
    ['#res-cpu-val','#res-mem-val'].forEach(i=>$(i).textContent='–');
    setBar('#res-cpu-bar', 0); setBar('#res-mem-bar', 0);
    $('#res-cpu-sub').textContent = ''; $('#res-mem-sub').textContent = '';
    return;
  }
  const cpu = r.cpu_percent;
  // cpu_percent is a delta between polls, so the first one after a restart has
  // nothing to compare against and comes back null.
  $('#res-cpu-val').textContent = (cpu===null||cpu===undefined) ? '…' : cpu.toFixed(1)+'%';
  $('#res-cpu-sub').textContent = r.cpu_limit ? `of ${r.cpu_limit} core${r.cpu_limit==1?'':'s'}` : '';
  setBar('#res-cpu-bar', cpu||0);

  $('#res-mem-val').textContent = (r.mem_percent??0).toFixed(1)+'%';
  $('#res-mem-sub').textContent = gib(r.mem_used)+' / '+gib(r.mem_total);
  setBar('#res-mem-bar', r.mem_percent||0);

  const parts = [];
  if(r.browser_procs) parts.push(`browsers: ${r.browser_procs} proc${r.browser_procs==1?'':'s'} · ${gib(r.browser_rss)}`);
  parts.push(r.source==='cgroup' ? 'container limits' : r.source==='psutil' ? 'host (psutil)' : 'host');
  $('#res-note').textContent = parts.join(' · ');
}
let _pxLoaded = false;
async function loadProxies(){
  if(_pxLoaded) return;         // don't clobber the textarea while editing
  try{ const p = await api('/admin/api/proxies');
       $('#px-text').value = p.text; $('#px-count').textContent = '('+p.count+')'; _pxLoaded = true;
  }catch(e){}
}
async function saveProxies(){
  try{ const r = await api('/admin/api/proxies',{method:'PUT',body:JSON.stringify({text:$('#px-text').value})});
       $('#px-count').textContent = '('+r.count+')'; alert('Saved '+r.count+' prox(ies). Restart accounts to apply.'); refresh();
  }catch(e){ alert(e.message); }
}
async function act(id,verb){ try{ await api(`/admin/api/accounts/${id}/${verb}`,{method:'POST'}); refresh(); }catch(e){ alert(e.message);} }
async function logout(id){ if(!confirm('Log out '+id+'? Clears its cookies — you\'ll need to sign in again.'))return;
  try{ await api(`/admin/api/accounts/${id}/logout`,{method:'POST'}); refresh(); }catch(e){ alert(e.message);} }
async function del(id){ if(!confirm('Delete account '+id+' and its profile?'))return;
  try{ await api(`/admin/api/accounts/${id}`,{method:'DELETE'}); refresh(); }catch(e){ alert(e.message);} }
async function toggleEnabled(id,en){ try{ await api(`/admin/api/accounts/${id}`,{method:'PATCH',body:JSON.stringify({enabled:en})}); refresh(); }catch(e){ alert(e.message);} }
async function addAccount(){
  // ID + timezone are assigned by the server automatically.
  const body = { provider:($('#n-prov')||{}).value||CURRENT_PROVIDER,
    label:$('#n-label').value.trim(),
    number:+$('#n-num').value||0,
    tabs:+$('#n-tabs').value||0, soft_cap:+$('#n-cap').value||0, order:+$('#n-order').value||100,
    proxy_server:$('#n-proxy').value.trim() };
  try{ await api('/admin/api/accounts',{method:'POST',body:JSON.stringify(body)});
       ['n-label','n-proxy'].forEach(i=>$('#'+i).value='');
       refresh(); }catch(e){ alert(e.message); }
}
// ── Logs viewer ──
let LOGS = [], LOG_TIMER = null, LOG_BODIES = {}, LOG_CURSOR = null;
let LOG_QUERY_TIMER = null, LOG_LOADING = false, LOG_VERSION = 0;
function openLogs(){
  $('#logsModal').classList.add('show'); loadLogs();
  if(LOG_TIMER) clearInterval(LOG_TIMER);
  LOG_TIMER = setInterval(()=>{
    if($('#log-auto').checked && !LOG_LOADING && !$('#logDetailModal').classList.contains('show') && LOGS.length<=500) loadLogs();
  }, 3000);
}
function closeLogs(){ $('#logsModal').classList.remove('show'); if(LOG_TIMER){clearInterval(LOG_TIMER);LOG_TIMER=null;} }
function filterLogs(){
  LOG_VERSION++; clearTimeout(LOG_QUERY_TIMER);
  LOG_QUERY_TIMER=setTimeout(()=>loadLogs(),250);
}
async function loadLogs(older=false){
  if(older && (!LOG_CURSOR || LOG_LOADING)) return;
  const version=++LOG_VERSION;
  LOG_LOADING=true;
  try{
    const query=encodeURIComponent($('#log-filter').value||'');
    const d=await api('/admin/api/logs?limit=500&q='+query+(older?'&before='+LOG_CURSOR:''));
    if(version!==LOG_VERSION) return;
    LOGS=older?[...LOGS,...(d.entries||[])]:d.entries||[];
    LOG_CURSOR=d.next_before;
    $('#log-more').hidden=!LOG_CURSOR;
    $('#log-count').textContent=LOGS.length+' shown / '+d.matched+' matching / '+d.count+' logged';
    $('#log-warning').textContent=d.warning||(LOGS.length>500?'Auto refresh pauses while browsing older requests. Refresh returns to the newest requests.':'');
    renderLogs();
  } catch(e){ if(version===LOG_VERSION) $('#log-warning').textContent=e.message; }
  finally { if(version===LOG_VERSION) LOG_LOADING=false; }
}
function fmtTime(ts){ return new Date(ts*1000).toLocaleTimeString(); }
function fmtSize(n){ if(n==null) return '–'; if(n<1024) return n+' B';
  if(n<1048576) return (n/1024).toFixed(1)+' KB'; return (n/1048576).toFixed(2)+' MB'; }
function methodBadge(m){ return `<span class="m m-${esc(m)}">${esc(m)}</span>`; }
function statusBadge(s){ const c = !s?'s0':(s<300?'s2':s<400?'s3':s<500?'s4':'s5');
  return `<span class="sc ${c}">${s||'—'}</span>`; }
function renderLogs(){
  const f = ($('#log-filter').value||'').toLowerCase();
  const rows = LOGS;
  $('#log-rows').innerHTML = rows.map(e => `
    <tr onclick="openLogDetail(${e.id})">
      <td class="muted mono">${fmtTime(e.ts)}</td>
      <td>${methodBadge(e.method)}</td>
      <td class="mono">${esc(e.path)}${e.streaming?'<span class="tag">stream</span>':''}</td>
      <td class="muted">${esc(e.model||e.provider||'')}</td>
      <td>${statusBadge(e.status)}${e.state==='in_progress'?'<span class="tag">in progress</span>':e.state&&e.state!=='completed'?'<span class="tag">'+esc(e.state)+'</span>':''}</td>
      <td class="muted mono">${e.duration_ms}</td>
      <td class="muted mono">${fmtSize(e.req_size)} → ${fmtSize(e.resp_size)}</td>
      <td><button class="linkbtn" onclick="event.stopPropagation();openLogDetail(${e.id})">Details ›</button></td>
    </tr>`).join('') ||
    `<tr><td colspan=8 class=muted style="padding:16px">${f?'No requests match “'+esc(f)+'”.':'No requests captured yet — make an API call and it will appear here.'}</td></tr>`;
}
function prettyBody(text, ct){
  if(ct && ct.toLowerCase().includes('json')){ try{ return JSON.stringify(JSON.parse(text), null, 2); }catch(e){} }
  return text||'';
}
function logSection(title, ct, size, trunc, headers, body){
  const hdr = Object.entries(headers||{}).map(([k,v])=>k+': '+v).join('\n');
  const bid = 'b'+Math.random().toString(36).slice(2,9);
  const pretty = prettyBody(body, ct); LOG_BODIES[bid] = pretty;
  return `<div class="detail-sec">
    <div class="hd"><b>${title}</b>
      <span class="muted">${esc(ct||'—')} · ${fmtSize(size)}${trunc?' · <span style="color:#e3b341">truncated</span>':''}</span>
      <span style="margin-left:auto"></span>
      <button class="linkbtn" onclick="copyBody('${bid}')">Copy</button></div>
    ${hdr ? `<details><summary>headers (${Object.keys(headers).length})</summary><pre>${esc(hdr)}</pre></details>` : ''}
    <pre>${pretty ? esc(pretty) : '<span class="muted">(empty body)</span>'}</pre>
  </div>`;
}
async function openLogDetail(id){
  $('#logDetailModal').classList.add('show');
  $('#ld-body').innerHTML = '<div class="muted" style="padding:16px">Loading…</div>';
  try{
    const e = await api('/admin/api/logs/'+id);
    LOG_BODIES = {};
    $('#ld-title').innerHTML = `${methodBadge(e.method)} <span class="mono">${esc(e.path)}</span> ${statusBadge(e.status)}`;
    $('#ld-body').innerHTML =
      `<div class="kv">${new Date(e.ts*1000).toLocaleString()} · <b>${e.duration_ms} ms</b>`
      + ` · provider <b>${esc(e.provider||'—')}</b> · model <b>${esc(e.model||'—')}</b>`
      + ` · client ${esc(e.client||'—')}${e.streaming?' · <b>streaming</b>':''}</div>`
      + `<div class="muted">${esc(e.state||'completed')}${e.error?' · '+esc(e.error):''}</div>`
      + logSection('Request', e.req_content_type, e.req_size, e.req_truncated, e.req_headers, e.req_body)
      + logSection('Response', e.resp_content_type, e.resp_size, e.resp_truncated, e.resp_headers, e.resp_body);
  }catch(err){ $('#ld-body').innerHTML = '<div class="muted" style="padding:16px">'+esc(err.message)+'</div>'; }
}
function closeLogDetail(){ $('#logDetailModal').classList.remove('show'); }
function copyBody(bid){ const t = LOG_BODIES[bid]||''; if(navigator.clipboard) navigator.clipboard.writeText(t).catch(()=>{}); }
async function clearLogs(){ if(!confirm('Clear the request log?')) return;
  try{ await api('/admin/api/logs',{method:'DELETE'}); loadLogs(); }catch(e){ alert(e.message); } }

// Download: the export needs the admin token in a header, so it cannot be a
// plain <a href> — fetch it and hand the blob to a synthetic link instead.
async function downloadLogs(){
  const btn = $('#log-dl'), was = btn.textContent;
  btn.disabled = true; btn.textContent = 'Preparing…';
  try{
    const q = encodeURIComponent(($('#log-filter').value||'').trim());
    const url = '/admin/api/logs/export?limit=' + encodeURIComponent($('#log-dl-count').value)
              + '&files=' + ($('#log-dl-files').checked ? '1' : '0') + '&q=' + q;
    const r = await fetch(url, {headers:{'Authorization':'Bearer '+token()}});
    if(r.status===401||r.status===403) throw new Error('auth: set the admin token');
    if(!r.ok){ let d; try{d=await r.json()}catch(e){}; throw new Error((d&&d.detail)||('HTTP '+r.status)); }
    const blob = await r.blob();
    const name = (r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/);
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = href; a.download = name ? name[1] : 'miri-logs.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(href), 10000);
  }catch(e){ alert(e.message); }
  finally{ btn.disabled = false; btn.textContent = was; }
}

if(!token()) setToken();
refresh(); setInterval(refresh, 4000);
</script>
</body>
</html>
"""
