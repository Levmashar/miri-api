/* miri-api — account control panel injected into noVNC's vnc.html.
 *
 * A floating overlay on the noVNC page (port 6080). It calls the admin API on
 * port 8000 (CORS is open) with the admin token pasted once. Focused on the
 * task you do in the viewer: log each account in and confirm it. Full config
 * (proxy, soft caps, ordering) lives on the richer /admin page, linked below.
 */
(function () {
  var API = location.protocol + '//' + location.hostname + ':8000';
  function tok() { return localStorage.getItem('miri_admin_token') || ''; }
  function curProv() { return localStorage.getItem('miri_provider') || 'chatgpt'; }
  window.__miriSetProv = function (p) {
    localStorage.setItem('miri_provider', p);
    // Also switch the VIEWER to that provider's first started account, so the
    // window you are looking at matches the tab you selected.
    if (window.__miriLast) {
      var a = (window.__miriLast.accounts || []).filter(function (x) {
        return x.provider === p && x.started && x.tabs_live > 0; })[0];
      if (a) api('/admin/api/accounts/' + a.id + '/focus?tab=0', { method: 'POST' }).catch(function () {});
    }
    load();
  };
  window.__miriFocus = function (id, tab) {
    api('/admin/api/accounts/' + id + '/focus?tab=' + tab, { method: 'POST' })
      .catch(function (e) { alert(e.message); });
  };
  function setTok() {
    var t = prompt('Admin token (ADMIN_TOKEN, or API_TOKEN):', tok());
    if (t !== null) { localStorage.setItem('miri_admin_token', t.trim()); load(); }
  }
  function esc(s){ return (s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({'Content-Type':'application/json','Authorization':'Bearer '+tok()}, opts.headers||{});
    var r = await fetch(API + path, opts);
    if (r.status === 401 || r.status === 403) throw new Error('set the admin token');
    if (!r.ok) { var d; try { d = await r.json(); } catch(e){} throw new Error((d&&d.detail)||('HTTP '+r.status)); }
    return r.status === 204 ? null : r.json();
  }

  // ── Build the panel ──
  var css = document.createElement('style');
  css.textContent = ''
    + '#miri{position:fixed;top:8px;right:8px;z-index:99999;width:320px;font:12px/1.4 system-ui,sans-serif;'
    + 'background:#161b22ee;color:#e6edf3;border:1px solid #30363d;border-radius:8px;backdrop-filter:blur(3px);}'
    + '#miri h3{margin:0;padding:8px 10px;font-size:13px;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:6px;cursor:move;}'
    + '#miri .body{padding:8px 10px;max-height:60vh;overflow:auto;}'
    + '#miri .acct{border-bottom:1px solid #21262d;padding:6px 0;}'
    + '#miri .pill{font-size:10px;padding:1px 7px;border-radius:20px;}'
    + '#miri .active{background:#1a4d2e;color:#7ee2a8}#miri .nearing{background:#4d431a;color:#e6d27a}'
    + '#miri .cooldown{background:#4d3a1a;color:#e6b87a}#miri .failed{background:#5a1e1e;color:#f0a0a0}'
    + '#miri .logged_out{background:#3a3a3a;color:#ccc}#miri .disabled{background:#21262d;color:#8b949e}'
    + '#miri .starting{background:#1a3a4d;color:#7ac6e6}'
    + '#miri button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:2px 7px;cursor:pointer;font-size:11px;margin:2px 2px 0 0;}'
    + '#miri button.primary{background:#238636;border-color:#238636;}'
    + '#miri .muted{color:#8b949e;font-size:10px;}'
    + '#miri a{color:#7ac6e6;}'
    + '#miri .min{display:none;}';
  document.head.appendChild(css);

  var el = document.createElement('div');
  el.id = 'miri';
  el.innerHTML =
    '<h3 id="miri-h">⚙ accounts <span id="miri-sum" class="muted" style="margin-left:auto"></span></h3>'
    + '<div class="body" id="miri-body">…</div>';
  document.body.appendChild(el);

  // draggable
  (function(){ var h=document.getElementById('miri-h'),dx,dy,drag=false;
    h.onmousedown=function(e){drag=true;dx=e.clientX-el.offsetLeft;dy=e.clientY-el.offsetTop;e.preventDefault();};
    document.onmousemove=function(e){ if(drag){el.style.left=(e.clientX-dx)+'px';el.style.top=(e.clientY-dy)+'px';el.style.right='auto';}};
    document.onmouseup=function(){drag=false;};
  })();

  // Build the static shell ONCE, so the proxy textarea isn't clobbered on refresh.
  function ensureShell() {
    var body = document.getElementById('miri-body');
    if (document.getElementById('miri-accts')) return;
    body.innerHTML =
      '<div id="miri-tabs" style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px"></div>'
      + '<div id="miri-accts"></div>'
      + '<div style="margin-top:8px;border-top:1px solid #30363d;padding-top:6px">'
      + '<b>Proxy pool</b> <span class="muted" id="miri-pxc"></span>'
      + '<div class="muted">one per line: scheme://[user:pass@]host:port · account N uses proxy N (wraps)</div>'
      + '<textarea id="miri-px" rows="4" spellcheck="false" style="width:100%;font-family:monospace;font-size:11px;'
      + 'background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:4px"></textarea>'
      + '<button class="primary" onclick="__miriSaveProxies()">Save proxies</button>'
      + '<span class="muted"> restart accounts to apply</span></div>'
      + '<div style="margin-top:8px"><button onclick="__miriSetTok()">Token</button> '
      + '<a href="'+API+'/admin" target="_blank">full admin →</a></div>';
  }

  async function load() {
    var st;
    try { st = await api('/admin/api/status'); }
    catch (e) {
      document.getElementById('miri-body').innerHTML = '<div class="muted">'+esc(e.message)+'</div>'
        + '<button onclick="__miriSetTok()">Set admin token</button>';
      return;
    }
    window.__miriLast = st;
    ensureShell();
    var r = st.resources || {};
    var res = '';
    if (r.available) {
      var cpu = (r.cpu_percent === null || r.cpu_percent === undefined)
        ? '…' : Math.round(r.cpu_percent) + '%';
      res = ' · cpu ' + cpu + ' · ram ' + Math.round(r.mem_percent || 0) + '%';
    }
    document.getElementById('miri-sum').textContent =
      st.tabs_schedulable + '/' + st.tabs_total + ' tabs'
      + (st.saturated ? ' · busy' : '') + res;
    // Provider tabs — switching shows only that provider's accounts.
    var cp = curProv();
    document.getElementById('miri-tabs').innerHTML = (st.providers||[]).map(function(p){
      var on = p.id === cp;
      return '<button onclick="__miriSetProv(\''+p.id+'\')"'
        + (on ? ' style="background:#238636;border-color:#238636"' : '') + '>'
        + esc(p.label) + ' <span class="muted">' + p.schedulable + '/' + p.tabs + '</span></button>';
    }).join('');
    document.getElementById('miri-accts').innerHTML = st.accounts.filter(function(a){
      return a.provider === cp;
    }).map(function(a){
      var u = a.usage || {};
      var btns = [];
      var signedIn = (a.state==='active'||a.state==='nearing'||a.state==='cooldown');
      if (!a.started) btns.push('<button onclick="__miriAct(\''+a.id+'\',\'start\')">Start</button>');
      else btns.push('<button onclick="__miriAct(\''+a.id+'\',\'stop\')">Stop</button>');
      // Log in: opens the provider login page on this account's tab (starts it
      // if needed) so you can sign in right here in the viewer.
      if (!signedIn) btns.push('<button class="primary" onclick="__miriAct(\''+a.id+'\',\'login-open\')">Log in</button>');
      else btns.push('<button onclick="__miriLogout(\''+a.id+'\')">Log out</button>');
      if (a.state === 'logged_out') btns.push('<button class="primary" onclick="__miriAct(\''+a.id+'\',\'login-confirm\')">I\'ve logged in</button>');
      // View this account's window, and jump to a specific tab.
      if (a.started && a.tabs_live > 0) {
        btns.push('<button onclick="__miriFocus(\''+a.id+'\',0)">View</button>');
        for (var ti = 0; ti < a.tabs_live; ti++)
          btns.push('<button onclick="__miriFocus(\''+a.id+'\','+ti+')">t'+(ti+1)+'</button>');
      }
      return '<div class="acct"><b>#'+a.number+' '+esc(a.label||a.id)+'</b> '
        + '<span class="pill '+a.state+'">'+a.state+'</span>'
        + '<div class="muted">'+esc(a.assigned_proxy||'')+' · '+a.tabs_live+' tabs · '
        + (u.requests_total||0)+' reqs · '+(u.limit_hits||0)+' limits'
        + (u.cooldown_remaining_s?(' · cd '+Math.round(u.cooldown_remaining_s/60)+'m'):'')+'</div>'
        + '<div>'+btns.join('')+'</div></div>';
    }).join('') || '<div class="muted">No ' + esc(cp) + ' accounts yet</div>';
    loadProxies();
  }

  var _pxLoaded = false;
  async function loadProxies(){
    if(_pxLoaded) return;
    try{ var p = await api('/admin/api/proxies'); var ta=document.getElementById('miri-px');
         if(ta){ ta.value=p.text; document.getElementById('miri-pxc').textContent='('+p.count+')'; _pxLoaded=true; } }catch(e){}
  }
  window.__miriSaveProxies = async function(){
    var ta=document.getElementById('miri-px');
    try{ var r=await api('/admin/api/proxies',{method:'PUT',body:JSON.stringify({text:ta.value})});
         document.getElementById('miri-pxc').textContent='('+r.count+')';
         alert('Saved '+r.count+' proxies. Restart accounts to apply.'); _pxLoaded=false; load(); }
    catch(e){ alert(e.message); }
  };

  window.__miriSetTok = setTok;
  window.__miriAct = async function(id, verb){
    try { await api('/admin/api/accounts/'+id+'/'+verb, {method:'POST'}); load(); }
    catch(e){ alert(e.message); }
  };
  window.__miriLogout = async function(id){
    if(!confirm('Log out account '+id+'? This clears its cookies — you\'ll need to sign in again.')) return;
    try { await api('/admin/api/accounts/'+id+'/logout', {method:'POST'}); load(); }
    catch(e){ alert(e.message); }
  };

  if (!tok()) setTok();
  load();
  setInterval(load, 4000);
})();
