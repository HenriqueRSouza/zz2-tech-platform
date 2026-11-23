const AUTH_KEY='zz2_auth_token';
let TOKEN = localStorage.getItem(AUTH_KEY) || '';
const HJSON = ()=>({'Content-Type':'application/json','Authorization':'Bearer '+TOKEN});
const $ = s=>document.querySelector(s);
const esc = s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ---- formatters ---- */
function fmtDur(s){ if(s==null) return '—'; s=Math.floor(s);
  const d=Math.floor(s/86400); s-=d*86400; const h=Math.floor(s/3600); s-=h*3600; const m=Math.floor(s/60);
  const p=[]; if(d)p.push(d+'d'); if(h)p.push(h+'h'); p.push(m+'m'); return p.join(' '); }
function fmtKB(kb){ if(kb==null) return '—'; if(kb<1024) return Math.round(kb)+' KB';
  const mb=kb/1024; if(mb<1024) return mb.toFixed(1)+' MB'; return (mb/1024).toFixed(2)+' GB'; }
function fmtBytes(b){ if(b==null) return '—'; return fmtKB(b/1024); }
function fmtTime(e){ if(!e) return '—'; const d=new Date(e*1000); if(isNaN(d)) return '—'; return d.toLocaleString(); }
function ago(e){ if(!e) return '—'; let s=Date.now()/1000-e;
  if(s<0) s=0; if(s<60) return Math.floor(s)+'s ago'; if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago'; return Math.floor(s/86400)+'d ago'; }
function isoNice(v){ if(!v) return '—'; const d=new Date(v); if(isNaN(d)) return esc(v); return d.toLocaleString(); }

function statCard(k,v,sub){ return '<div class="stat"><div class="k">'+esc(k)+'</div><div class="v">'+v+
  '</div>'+(sub?'<div class="sub">'+sub+'</div>':'')+'</div>'; }
function barCard(k,v,sub,pct,cls){ pct=Math.max(0,Math.min(100,pct||0));
  return '<div class="stat"><div class="k">'+esc(k)+'</div><div class="v">'+v+'</div>'+
    (sub?'<div class="sub">'+sub+'</div>':'')+'<div class="bar '+(cls||'')+'"><i style="width:'+pct+'%"></i></div></div>'; }
function tone(pct){ return pct>=90?'bad':pct>=75?'warn':'ok'; }

let LAST=null;
let _cfgBuilt=false;

function render(d){
  LAST=d;
  $('#updated').textContent='updated '+new Date().toLocaleTimeString();

  /* server + host */
  const sv=d.server||{}, h=d.host||{}, st=d.storage||{};
  const memUsedMB = (h.mem_used_kb||0)/1024, memTotMB=(h.mem_total_kb||0)/1024;
  const g1=[
    statCard('Process uptime', fmtDur(sv.process_uptime_sec), 'since '+fmtTime(sv.started)),
    statCard('Python', esc(sv.python||'—'), 'PID '+esc(sv.pid)),
    barCard('CPU load', (h.cpu_pct==null?'—':h.cpu_pct+'<small>%</small>'), 'single core busy', h.cpu_pct||0, tone(h.cpu_pct||0)),
    barCard('System memory', (h.mem_pct||0)+'<small>%</small>',
            Math.round(memUsedMB)+' / '+Math.round(memTotMB)+' MB', h.mem_pct, tone(h.mem_pct)),
    statCard('This process', fmtKB(h.proc_rss_kb), 'resident memory'),
    statCard('Load average', (h.load||[]).map(x=>x.toFixed(2)).join('  ') || '—', '1m · 5m · 15m'),
    barCard('Disk ('+ '/' +')', (st.pct||0)+'<small>%</small>',
            fmtBytes(st.used)+' / '+fmtBytes(st.total), st.pct, tone(st.pct)),
    statCard('Disk free', fmtBytes(st.free), 'available'),
    statCard('System uptime', fmtDur(h.uptime_sec), 'host')
  ];
  $('#g-server').innerHTML=g1.join('');

  /* workers */
  const w=sv.workers||{};
  const worker=(name,ok)=>'<span class="pill '+(ok?'up':'down')+'"><span class="dot"></span>'+name+'</span>';
  $('#g-workers').innerHTML='<div class="card"><div class="hd">Background workers</div>'+
    '<div class="bd" style="padding:13px 14px;display:flex;gap:10px;flex-wrap:wrap">'+
    worker('Nightly scheduler', w['zz2-scheduler'])+
    worker('Ticket poller', w['zz2-poller'])+
    worker('Write-behind queue', w['zz2-write-behind'])+'</div></div>';

  /* store */
  const s=d.store||{};
  const g2=[
    statCard('Tickets in store', (s.ticket_count!=null? s.ticket_count.toLocaleString():'—'),
             fmtKB(s.file_size_kb)+' on disk'),
    statCard('Pending writes', s.pending_writes||0,
             (s.pending_deletes? s.pending_deletes+' deletes queued':'queue empty')),
    statCard('Failed writes', (s.dead_letters? '<span style="color:#dc2626">'+s.dead_letters+'</span>':'0'),
             (s.dead_letters? 'need replay or clear':'none')),
    statCard('Last nightly rebuild', esc(s.last_nightly||'—'), 'full re-pull'),
    statCard('Last reconcile', ago(s.last_reconcile), fmtTime(s.last_reconcile)),
    statCard('Last poll', ago(s.last_poll), (s.poll_added_total||0)+' added since start'),
    statCard('Store generated', isoNice(s.generated), 'snapshot time')
  ];
  $('#g-store').innerHTML=g2.join('');

  /* actions */
  $('#g-actions').innerHTML=[
    '<button class="act brand" onclick="act(\'/api/system/rebuild\',\'Analytics rebuilt\',this)"><span class="ic">&#8635;</span><span>Rebuild store<small>full re-pull from sheet</small></span></button>',
    '<button class="act" onclick="act(\'/api/system/reconcile\',\'Reconciled\',this)"><span class="ic">&#128260;</span><span>Reconcile now<small>sync recent window</small></span></button>',
    '<button class="act" onclick="act(\'/api/system/backup\',\'Backup created\',this)"><span class="ic">&#128190;</span><span>Run backup<small>full snapshot (code + data)</small></span></button>',
    '<button class="act" onclick="act(\'/api/system/sync-database\',\'Catalogue mirrored\',this)"><span class="ic">&#9729;</span><span>Mirror catalogue<small>push kits to the database</small></span></button>'
  ].join('');

  /* config form -- built once so the 12s auto-refresh never clobbers an edit */
  renderConfig(d.config||{});
  if((d.config||{}).auto_resolve_days) $('#ar-days').textContent=d.config.auto_resolve_days;

  /* sessions -- signed tokens issued by this server, not rows in a table.
     There is no session store to enumerate, so rather than render an empty
     table the panel explains how sessions work. */
  const sessionsPanel = $('#g-sessions-note');
  if (sessionsPanel) {
    sessionsPanel.innerHTML =
      '<div class="bd" style="padding:14px">' +
      '<b>Sessions are signed tokens issued by this server.</b>' +
      '<div class="muted" style="margin-top:6px">Password and Microsoft sign-ins ' +
      'both receive a token that is valid for 8 hours and is verified locally on ' +
      'every request. Signing out revokes the token; to cut off an account ' +
      'immediately, disable it on the Users &amp; Roles page.</div></div>';
  }

  /* files */
  const fs=d.data_files||[];
  $('#g-files').innerHTML=fs.map(f=>'<tr><td style="font-family:var(--mono)">'+esc(f.name)+'</td>'+
    '<td>'+(f.size_kb==null?'<span class="muted">missing</span>':fmtKB(f.size_kb))+'</td>'+
    '<td class="muted">'+(f.mtime?fmtTime(f.mtime):'—')+'</td></tr>').join('');

  /* security -- what this process can actually see and be wrong about.
     The old panel reported plaintext passwords, lockouts and failed logins: all
     facts about a local credential store that no longer exists. A panel that
     looked authoritative while reading nothing would be worse than none, so this
     reports configuration posture instead. */
  const sec = d.security || {};
  const secrets = sec.secrets_configured || {};
  const mode = sec.data_dir_mode || {};

  function flag(ok, good, bad) {
    return ok
      ? '<span class="pill up"><span class="dot"></span>' + good + '</span>'
      : '<span class="pill exp">' + bad + '</span>';
  }

  const rows = [
    ['Authentication', '<span class="role-badge">' + esc(sec.auth_provider || 'unknown') + '</span>'],
    ['Token verification', flag(sec.jwt_verification === 'local',
                                'local (no upstream call)', 'remote (per-request call)')],
    ['HTTPS', flag(sec.https_enforced, 'enforced', 'not enforced')],
    ['Rate limiting', flag(sec.rate_limit_enabled, 'on', 'off')],
    ['CORS origins', (sec.cors_origins && sec.cors_origins.length)
        ? esc(sec.cors_origins.join(', '))
        : '<span class="muted">same-origin only</span>'],
    ['Microsoft sign-in', flag(sec.microsoft_sign_in, 'enabled', 'disabled')],
    ['SAML sign-in', flag(sec.saml_sign_in, 'enabled', 'disabled')],
    ['Database URL', flag(secrets.database_url, 'configured', 'missing')],
    ['Session secret', flag(secrets.session_secret, 'configured', 'missing')],
    ['Azure Storage', flag(secrets.azure_storage, 'configured', 'missing')],
    ['Microsoft client secret', flag(secrets.microsoft_client_secret, 'configured', 'missing')],
    ['Apps Script secret', flag(secrets.appscript_secret, 'configured', 'missing')],
    ['Data directory mode', mode.mode
        ? (esc(mode.mode) + ' ' + flag(!mode.world_readable, 'not world-readable',
                                       'world-readable'))
        : '<span class="muted">unknown</span>'],
  ];

  $('#g-security').innerHTML =
    '<div class="card"><div class="hd">Security posture</div>' +
    '<div class="bd" style="padding:0"><table><tbody>' +
    rows.map(function (r) {
      return '<tr><td style="width:220px">' + r[0] + '</td><td>' + r[1] + '</td></tr>';
    }).join('') +
    '</tbody></table></div></div>';

  /* backups */
  const bk=d.backups||{};
  $('#bk-count').textContent='\u00b7 '+(bk.count||0)+' kept';
  const recent=bk.recent||[];
  $('#g-backups').innerHTML = recent.length ? recent.map(b=>'<tr><td style="font-family:var(--mono)">'+esc(b.name)+
    (b.include_media?' <span style="background:#eef0ff;color:#4f46e5;font-size:10px;font-weight:700;padding:1px 6px;border-radius:6px">media</span>':'')+'</td>'+
    '<td class="muted">'+esc(b.label||'\u2014')+'</td>'+
    '<td>'+fmtBytes(b.size)+'</td><td class="muted">'+(b.mtime?fmtTime(b.mtime):'\u2014')+'</td>'+
    '<td style="text-align:right;white-space:nowrap"><button class="mini" onclick="bkFiles(\''+esc(b.name)+'\')">View / restore</button> '+
    '<button class="mini" onclick="bkDownload(\''+esc(b.name)+'\')">&#8595; zip</button></td></tr>').join('')
    : '<tr><td colspan="5" class="muted" style="padding:16px 14px">No backups yet.</td></tr>';

  /* log */
  const lg=d.recent_log||[];
  $('#g-log').innerHTML = lg.length ? lg.map(esc).join('\n') : '<span class="empty">No recent log entries.</span>';

  /* failed-writes badge + button visibility */
  const dl=(d.store||{}).dead_letters||0;
  $('#dead-badge').innerHTML = dl ? '<span style="color:#dc2626;font-weight:700">&middot; '+dl+' need attention</span>' : '<span class="muted" style="font-weight:400">&middot; none</span>';
  $('#dead-replay').style.display = dl ? '' : 'none';
  $('#dead-clear').style.display  = dl ? '' : 'none';
}

/* ---- editable configuration form ---- */
function _cfRow(inner,nm,ds){
  return '<div class="cfg-row"><div class="lbl"><div class="nm">'+nm+'</div>'+
    (ds&&ds.trim()?'<div class="ds">'+ds+'</div>':'')+'</div><div class="ctl">'+inner+'</div></div>';
}
function cfNum(id,nm,ds,val,mn,mx,st,unit){
  return _cfRow('<input type="number" id="'+id+'" value="'+esc(val)+'" min="'+mn+'" max="'+mx+'" step="'+(st||1)+'">'+
    (unit?'<span class="unit">'+unit+'</span>':''),nm,ds);
}
function cfText(id,nm,ds,val){ return _cfRow('<input type="text" id="'+id+'" value="'+esc(val)+'" spellcheck="false">',nm,ds); }
function cfTime(id,nm,ds,val){ return _cfRow('<input type="time" id="'+id+'" value="'+esc(val)+'">',nm,ds); }
function cfSwitch(id,nm,ds,on){
  return _cfRow('<label class="switch"><input type="checkbox" id="'+id+'"'+(on?' checked':'')+'><span class="sl"></span></label>',nm,ds);
}
function cfRO(nm,val){ return '<div class="cfg-row ro"><div class="lbl"><div class="nm">'+nm+'</div></div><div class="ctl">'+esc(val)+'</div></div>'; }
function renderConfig(c){
  if(_cfgBuilt) return;                 // build once; rebuilt only after a save / revert
  _cfgBuilt=true;
  $('#cfg-form').innerHTML=
    '<div class="cfg-card">'+
      '<div class="cfg-grp"><div class="ghd">Auto-resolve</div>'+
        cfSwitch('cf_ar_en','Run nightly','Mark stale tickets Resolved automatically each night.',c.auto_resolve_enabled!==false)+
        cfNum('cf_ar_days','Resolve after','Blank or &ldquo;waiting for feedback&rdquo; tickets older than this get marked Resolved. (e.g. set 14 for two weeks.)',c.auto_resolve_days||7,1,365,1,'days')+
      '</div>'+
      '<div class="cfg-grp"><div class="ghd">Sync &amp; polling</div>'+
        cfNum('cf_poll','Sheet poll interval','How often the server checks the Google Sheet for new rows.',c.poll_interval||30,5,3600,5,'seconds')+
        cfNum('cf_pane','Today pane refresh','How often the Today tickets pane reloads in the browser.',c.pane_refresh_interval||30,2,3600,1,'seconds')+
        cfNum('cf_rec_int','Reconcile interval','How often edits and deletes in the recent window are re-synced.',c.reconcile_interval||300,30,86400,10,'seconds')+
        cfNum('cf_rec_days','Reconcile window','How many recent days are kept sheet-accurate intraday.',c.reconcile_days||2,1,30,1,'days')+
      '</div>'+
      '<div class="cfg-grp"><div class="ghd">Sessions</div>'+
        cfNum('cf_ttl','Session lifetime','How long a sign-in stays valid before re-login is required.',c.session_ttl_days||7,0.5,90,0.5,'days')+
      '</div>'+
      '<div class="cfg-grp"><div class="ghd">Backups</div>'+
        cfNum('cf_bk_keep','Snapshots to keep','Older full backups beyond this count are pruned. Each snapshot includes the app code and data.',c.backup_keep||30,3,365,1,'kept')+
        cfSwitch('cf_bk_media','Include images &amp; manuals','Also back up the images/ and manuals/ folders. Off (default) keeps snapshots small; turn on for a complete archive (much larger).',!!c.backup_include_media)+
      '</div>'+
      '<div class="cfg-grp"><div class="ghd">Work window <span style="font-weight:500;text-transform:none;letter-spacing:0">&mdash; optional</span></div>'+
        cfSwitch('cf_ww_en','Enforce work hours','When on, the poller pauses outside these hours to save Apps Script quota. Off (default) = always polling.',!!c.work_window_enabled)+
        cfTime('cf_ww_start','Start',' ',c.work_start||'09:30')+
        cfTime('cf_ww_end','End',' ',c.work_end||'20:00')+
      '</div>'+
      '<div class="cfg-grp"><div class="ghd">Apps Script backend</div>'+
        cfText('cf_url','Web app URL','The /exec deployment URL the server proxies to. Update this after redeploying the script.',c.webapp_url||'')+
        cfRO('Apps Script secret',c.secret_key_set?'\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 set (configured in the environment)':'(not set)')+
        cfRO('Base directory',c.base_dir||'\u2014')+
      '</div>'+
      '<div class="cfg-foot">'+
        '<button class="btn-save" id="cfg-save" onclick="saveConfig(this)">Save changes</button>'+
        '<button class="btn-ghost" onclick="revertConfig()">Revert</button>'+
        '<span class="hint">Changes apply live &mdash; no restart needed.</span>'+
      '</div>'+
    '</div>';
}
function revertConfig(){ _cfgBuilt=false; load(); }
async function saveConfig(btn){
  const num=id=>{ const v=parseFloat(document.getElementById(id).value); return isNaN(v)?null:v; };
  const payload={
    auto_resolve_enabled: document.getElementById('cf_ar_en').checked,
    auto_resolve_days:    num('cf_ar_days'),
    poll_interval:        num('cf_poll'),
    pane_refresh_interval: num('cf_pane'),
    reconcile_interval:   num('cf_rec_int'),
    reconcile_days:       num('cf_rec_days'),
    session_ttl_days:     num('cf_ttl'),
    backup_keep:          num('cf_bk_keep'),
    backup_include_media: document.getElementById('cf_bk_media').checked,
    work_window_enabled:  document.getElementById('cf_ww_en').checked,
    work_start:           document.getElementById('cf_ww_start').value,
    work_end:             document.getElementById('cf_ww_end').value,
    webapp_url:           document.getElementById('cf_url').value.trim()
  };
  const old=btn.innerHTML; btn.disabled=true; btn.textContent='Saving\u2026';
  try{
    const r=await fetch('/api/system/config',{method:'POST',headers:HJSON(),body:JSON.stringify(payload)});
    const d=await r.json().catch(()=>({}));
    if(r.ok&&d.ok){
      const n=(d.changed||[]).length;
      toast(n? ('Saved '+n+' change'+(n===1?'':'s')+' \u2713') : 'No changes to save');
      _cfgBuilt=false; await load();
    } else toast(d.error||'Save failed',true);
  }catch(e){ toast('Network error',true); }
  finally{ btn.disabled=false; btn.innerHTML=old; }
}
function initSubnav(){
  const links=Array.prototype.slice.call(document.querySelectorAll('.subnav a'));
  const secs=links.map(a=>document.querySelector(a.getAttribute('href')));
  function spy(){
    let i=0;
    for(let k=0;k<secs.length;k++){ if(secs[k] && secs[k].getBoundingClientRect().top<=80) i=k; }
    links.forEach((a,k)=>a.classList.toggle('active',k===i));
  }
  window.addEventListener('scroll',spy,{passive:true}); spy();
}

let _timer=null;
async function load(){
  try{
    const r=await fetch('/api/system/info',{headers:{'Authorization':'Bearer '+TOKEN}});
    if(r.status===403){ location.href='/index.html'; return; }
    if(!r.ok){ toast('Failed to load ('+r.status+')',true); return; }
    render(await r.json());
  }catch(e){ toast('Network error',true); }
}

async function loadDead(btn){
  if(btn){ btn.disabled=true; }
  try{
    const r=await fetch('/api/system/deadletters',{headers:{'Authorization':'Bearer '+TOKEN}});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){ toast(d.error||'Failed to load',true); return; }
    const items=d.items||[];
    if(!items.length){ $('#dead-result').innerHTML='<div class="muted" style="padding:6px 2px">No failed writes. The store and the sheet are in sync.</div>'; return; }
    const rows=items.map(function(it){
      return '<tr><td class="muted" style="white-space:nowrap">'+esc(it.dead_at||'')+'</td>'+
        '<td>'+esc(it.action||'')+'</td>'+
        '<td>'+esc(it.ticketNum||'\u2014')+'</td>'+
        '<td>'+esc(it.client||'\u2014')+'</td>'+
        '<td>'+esc(it.product||'\u2014')+'</td>'+
        '<td class="muted">'+esc(it.reason||'')+'</td>'+
        '<td><button class="mini" onclick="replayDead(\''+esc(it.id)+'\',this)">Replay</button></td></tr>';
    }).join('');
    $('#dead-result').innerHTML='<div style="overflow:auto"><table class="kv" style="width:100%"><thead><tr>'+
      '<th>Failed at</th><th>Action</th><th>Ticket</th><th>Client</th><th>Kit</th><th>Reason</th><th></th></tr></thead>'+
      '<tbody>'+rows+'</tbody></table></div>';
  }catch(e){ toast('Network error',true); }
  finally{ if(btn){ btn.disabled=false; } }
}
async function replayDead(id,btn){
  if(!id && !confirm('Re-queue every failed write to the sheet?')) return;
  if(btn){ btn.disabled=true; }
  try{
    const r=await fetch('/api/system/deadletters/replay',{method:'POST',headers:HJSON(),body:JSON.stringify({id:id||''})});
    const d=await r.json().catch(()=>({}));
    if(r.ok && d.ok){ toast('Re-queued '+(d.replayed||0)+' write'+(d.replayed===1?'':'s')+' \u2713'); loadDead(); load(); }
    else toast(d.error||'Replay failed',true);
  }catch(e){ toast('Network error',true); }
  finally{ if(btn){ btn.disabled=false; } }
}
async function clearDead(btn){
  if(!confirm('Discard all failed writes? A rebuild will still resync from the sheet.')) return;
  if(btn){ btn.disabled=true; }
  try{
    const r=await fetch('/api/system/deadletters/clear',{method:'POST',headers:HJSON(),body:'{}'});
    const d=await r.json().catch(()=>({}));
    if(r.ok && d.ok){ toast('Cleared '+(d.cleared||0)+' \u2713'); $('#dead-result').innerHTML=''; load(); }
    else toast(d.error||'Clear failed',true);
  }catch(e){ toast('Network error',true); }
  finally{ if(btn){ btn.disabled=false; } }
}

async function bkFiles(name){
  document.getElementById('bk-modal').classList.add('open');
  document.getElementById('bk-modal-title').textContent=name;
  const body=document.getElementById('bk-modal-body'); body.innerHTML='<div class="muted">Loading\u2026</div>';
  try{
    const r=await fetch('/api/system/backup-info?name='+encodeURIComponent(name),{headers:{'Authorization':'Bearer '+TOKEN}});
    const d=await r.json().catch(()=>({}));
    if(!r.ok||!d.ok){ body.innerHTML='<div class="muted">Could not load this snapshot.</div>'; return; }
    const m=d.manifest||{}; const files=m.files||[];
    let h='<div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">'+
      '<button class="btn-save" onclick="bkRestore(\''+esc(name)+'\',\'all\')">Restore everything</button>'+
      '<button class="btn-ghost" onclick="bkDownload(\''+esc(name)+'\')">Download .zip</button>'+
      '<span class="hint">'+files.length+' files \u00b7 '+fmtBytes(m.total_bytes||0)+(m.label?(' \u00b7 '+esc(m.label)):'')+'</span></div>'+
      '<div class="hint" style="margin-bottom:10px">Restoring code (server.py, *.html) takes effect on the next page load; server.py needs a restart. Your current files are snapshotted first, so a restore is itself reversible.</div>'+
      '<div style="max-height:42vh;overflow:auto"><table style="width:100%"><tbody>'+
      files.map(f=>'<tr><td style="font-family:var(--mono);font-size:12px">'+esc(f.path)+'</td>'+
        '<td class="muted" style="text-align:right;white-space:nowrap">'+fmtBytes(f.size)+'</td>'+
        '<td style="text-align:right"><button class="mini" onclick="bkRestore(\''+esc(name)+'\',[\''+esc(f.path)+'\'])">Restore</button></td></tr>').join('')+
      '</tbody></table></div>';
    body.innerHTML=h;
  }catch(e){ body.innerHTML='<div class="muted">Network error.</div>'; }
}
function closeBk(){ document.getElementById('bk-modal').classList.remove('open'); }
async function bkRestore(name,files){
  const what = (files==='all') ? 'ALL files from this snapshot' : (Array.isArray(files)?files.join(', '):files);
  if(!confirm('Restore '+what+'?\nYour current files are backed up first, so this is reversible.')) return;
  try{
    const r=await fetch('/api/system/restore',{method:'POST',headers:HJSON(),body:JSON.stringify({name:name,files:files})});
    const d=await r.json().catch(()=>({}));
    if(r.ok&&d.ok){ const c=(d.result||{}).count||0; toast('Restored '+c+' file'+(c===1?'':'s')+' \u2713'); closeBk(); load(); }
    else toast(d.error||'Restore failed',true);
  }catch(e){ toast('Network error',true); }
}
async function bkDownload(name){
  toast('Preparing download\u2026');
  try{
    const r=await fetch('/api/system/backup-download?name='+encodeURIComponent(name),{headers:{'Authorization':'Bearer '+TOKEN}});
    if(!r.ok){ toast('Download failed',true); return; }
    const blob=await r.blob();
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
    a.download='zz2-backup-'+name+'.zip'; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(a.href),2000); toast('Downloaded \u2713');
  }catch(e){ toast('Network error',true); }
}

async function act(path,label,btn){
  const old = btn ? btn.innerHTML : '';
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="ic">&#8987;</span><span>Working&hellip;</span>'; }
  try{
    const r=await fetch(path,{method:'POST',headers:HJSON(),body:'{}'});
    const d=await r.json().catch(()=>({}));
    if(r.ok && d.ok!==false){
      let extra='';
      if(d.count!=null) extra=' ('+d.count+' tickets)';
      else if(d.removed!=null) extra=' ('+d.removed+')';
      toast(label+extra+' ✓');
      load();
    } else toast(d.error||(label+' failed'),true);
  }catch(e){ toast('Network error',true); }
  finally{ if(btn){ btn.disabled=false; btn.innerHTML=old; } }
}

async function arPreview(btn){
  const old=btn.innerHTML; btn.disabled=true; btn.innerHTML='<span class="ic">&#8987;</span><span>Checking&hellip;</span>';
  try{
    const r=await fetch('/api/system/autoresolve',{method:'POST',headers:HJSON(),body:JSON.stringify({apply:false})});
    const d=await r.json().catch(()=>({}));
    if(!r.ok||d.error){ toast(d.error||'Preview failed',true); return; }
    document.getElementById('ar-days').textContent = d.days||7;
    renderAR(d,false);
    document.getElementById('ar-apply').style.display = (d.candidates>0)?'flex':'none';
  }catch(e){ toast('Network error',true); }
  finally{ btn.disabled=false; btn.innerHTML=old; }
}
async function arApply(btn){
  if(!confirm('Mark these tickets Resolved in the Google Sheet?\nA backup is taken first, then it runs to completion automatically.')) return;
  btn.disabled=true;
  let total=0, first=true, guard=0, stuck=false;
  try{
    while(guard++ < 1000){
      btn.innerHTML='<span class="ic">&#8987;</span><span>Resolving&hellip;'+(total?(' '+total):'')+'</span>';
      const r=await fetch('/api/system/autoresolve',{method:'POST',headers:HJSON(),body:JSON.stringify({apply:true,cont:!first})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok||(d.error&&!(d.applied))){ toast(d.error||'Apply failed',true); break; }
      first=false;
      total += (d.applied||0);
      const remaining=d.remaining||0;
      renderARProgress(total, remaining);
      if((d.done!==false) && remaining<=0){
        document.getElementById('ar-apply').style.display='none';
        toast('Resolved '+total+' tickets \u2713');
        break;
      }
      if((d.applied||0)===0){ stuck=true; toast('Stopped \u2014 '+remaining+' left, no progress',true); break; }
    }
    load();
  }catch(e){ toast('Network error \u2014 progress is saved, click to resume',true); }
  finally{
    btn.disabled=false;
    if(document.getElementById('ar-apply').style.display!=='none')
      btn.innerHTML='<span class="ic">&#10003;</span><span>'+(stuck?'Retry':'Resume')+'<small>finish remaining</small></span>';
  }
}
function renderARProgress(total, remaining){
  const box=document.getElementById('ar-result');
  const cand=total+remaining, pct=cand?Math.round(100*total/cand):100;
  const head = remaining>0 ? ('Resolving&hellip; '+total+' done, '+remaining+' to go') : ('Resolved '+total+' ticket'+(total===1?'':'s')+'.');
  box.innerHTML='<div class="card"><div class="bd" style="padding:12px 14px">'+
    '<div style="font-weight:600;margin-bottom:8px">'+head+'</div>'+
    '<div style="height:8px;background:var(--line);border-radius:4px;overflow:hidden"><div style="height:100%;width:'+pct+'%;background:var(--brand,#4a7cff);transition:width .3s"></div></div>'+
    '</div></div>';
}
function renderAR(d,applied){
  const box=document.getElementById('ar-result');
  const n=applied?(d.applied||0):(d.candidates||0);
  if(!n){ box.innerHTML='<span class="muted">No stale tickets older than '+(d.days||7)+' days &mdash; nothing to do.</span>'; return; }
  let head;
  if(applied){
    const rem=d.remaining||0;
    head = (d.done===false && rem>0)
      ? ('Resolved '+n+' so far &mdash; '+rem+' still to go (click Continue).')
      : ('Resolved '+n+' ticket'+(n===1?'':'s')+'.');
  } else {
    head = n+' ticket'+(n===1?'':'s')+' would be resolved';
  }
  let h='<div style="font-weight:600;margin-bottom:4px">'+head+'</div>';
  const sm=d.samples||[];
  if(sm.length){
    h+='<table style="margin-top:6px"><thead><tr><th>Ticket</th><th>Date</th><th>Status was</th></tr></thead><tbody>'+
      sm.map(x=>'<tr><td>'+esc(x.ticket)+'</td><td class="muted">'+esc(x.date)+'</td><td class="muted">'+esc(x.was)+'</td></tr>').join('')+'</tbody></table>';
    if(n>sm.length) h+='<div class="muted" style="margin-top:6px">&hellip;and '+(n-sm.length)+' more.</div>';
  }
  box.innerHTML='<div class="card"><div class="bd" style="padding:12px 14px">'+h+'</div></div>';
}

function signOut(){
  ZZ2.logoutRequest().catch(()=>{});
  localStorage.removeItem(AUTH_KEY); location.href='/login.html';
}

let _toastT=null;
function toast(msg,bad){ const t=$('#toast'); t.textContent=msg; t.className='show'+(bad?' bad':'');
  clearTimeout(_toastT); _toastT=setTimeout(()=>t.className='',2600); }

(async function guard(){
  if(!TOKEN){ location.href='/login.html'; return; }
  try{
    const r=await ZZ2.verifySession(TOKEN);
    const d=await r.json();
    if(!d.ok){ localStorage.removeItem(AUTH_KEY); location.href='/login.html'; return; }
    const perms=d.permissions||[];
    if(window.zz2BuildNav) zz2BuildNav(perms);
    if(perms.indexOf('system_settings')<0){ location.href='/index.html'; return; }
    $('#who').textContent=(d.display||'')+' · '+((d.role||'').replace('_',' '));
    $('#app').style.display='block';
    await load();
    initSubnav();
    _timer=setInterval(load, 12000);
  }catch(e){
    document.body.insertAdjacentHTML('beforeend','<div class="err">Could not reach the server.</div>');
  }
})();
