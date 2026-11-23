// ============================================================
//  ZZ2 Tickets — virtualized ticket table + inline edit + create
//  Data: /api/ticket-records (raw `tickets` rows). Presence: the per-ticket
//  /api/presence endpoint, heartbeated while a row is being edited.
// ============================================================
const AUTH_KEY='zz2_auth_token', ROLE_KEY='zz2_auth_role';
let TOKEN = localStorage.getItem(AUTH_KEY) || '';
let USER_DISPLAY='User';
let PRESENCE_BY_TICKET={};          // ticket _id -> [{display}] other people on that ticket
const RT_CLIENT_ID=(crypto.randomUUID?crypto.randomUUID():String(Date.now())+'-'+Math.random());
const PRESENCE_INTERVAL_MS=10000;
let EDITING=null;                   // {id, num, timer} for the row this tab is editing

const ROW_H=32, HEAD_H=36, OVER=8;
const LINE_H=17, VPAD=10;           // textarea/cell line height + vertical padding
let ROWTOP=[], ROWH=[], TOTAL_H=0;  // per-row offsets/heights for variable-height virtualization
const COLS=[
  {k:'ticket', t:'Ticket #',  w:96,  ro:true, num:true},
  {k:'date',   t:'Date',      w:136, ro:true},
  {k:'tech',   t:'Tech',      w:96},
  {k:'client', t:'Client',    w:160},
  {k:'product',t:'Product',   w:130},
  {k:'vehicle',t:'Vehicle',   w:150},
  {k:'issue',  t:'Issue',     w:240},
  {k:'resolution',t:'Resolution',w:260},
  {k:'status', t:'Status',    w:252, type:'select'},
  {k:'serial', t:'Serial',    w:120},
  {k:'notes',  t:'Notes',     w:240},
];
const TOTAL_W = COLS.reduce((a,c)=>a+c.w,0);
const STATUS_OPTIONS=['','Resolved','W.O.E.','No solution','Waiting for customer feedback',
  'RMA/Return','RMA - ADV REP REQ GROUND','RMA - ADV REP REQ OVERNIGHT','DISTRIBUTOR','Field Destroy'];

const $=(s,r=document)=>r.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=n=>(n==null||isNaN(n))?'—':Number(n).toLocaleString('en-US');
function toast(m,kind){const t=$('#toast');t.textContent=m;t.className='show'+(kind?' '+kind:'');clearTimeout(t._h);t._h=setTimeout(()=>t.className='',3000);}
function ticketNum(t){const d=String(t||'').replace(/\D/g,'');return d?parseInt(d,10):0;}
function statusClass(s){s=(s||'').toLowerCase();
  if(s.indexOf('resolv')>=0)return'st-resolved'; if(s.indexOf('wait')>=0)return'st-open';
  if(s.indexOf('no sol')>=0)return'st-nosol'; if(s.indexOf('rma')>=0)return'st-rma';
  if(s.indexOf('destroy')>=0)return'st-destroy'; if(s.indexOf('distrib')>=0)return'st-distrib'; return'';}

// ---- state ----
let DATA=[];               // all tickets (objects)
let VIEW=[];               // filtered + sorted view
let sort={k:'ticket',dir:-1};
let query='';
let META={generated:null};
let refreshTimer=null;

// ============================================================
//  LOAD
// ============================================================
async function recordsApi(method, path, body){
  const r=await ZZ2.api(method, '/api/ticket-records'+path, body);
  let d=null; try{ d=await r.json(); }catch(e){}
  if(r.status===403) throw new Error('forbidden');
  if(!r.ok || !d || d.ok===false) throw new Error((d&&d.error)||('HTTP '+r.status));
  return d;
}
async function loadTickets(){
  const rows=[]; const pageSize=1000;
  for(let offset=0;;offset+=pageSize){
    const d=await recordsApi('GET','?limit='+pageSize+'&offset='+offset);
    const data=d.rows||[];
    rows.push(...data);
    if(data.length<pageSize) break;
  }
  META.generated = new Date().toISOString();
  DATA = rows.map(normalize).filter(t=>t.ticket||t.client||t.date);
}
function normalize(r){
  return {
    _id:r.id==null?null:r.id,
    _raw:r,
    date:String(r.ticket_date||r.date||r.Date||'').trim(),
    ticket:String(r.ticket_number||r.ticket||r['Ticket #']||r.ticketNum||r.number||'').trim(),
    tech:String(r.tech||r.Tech||'').trim(),
    client:String(r.client||r.Client||'').trim(),
    product:String(r.product||r.Product||r.kit||'').trim(),
    vehicle:String(r.vehicle||r.Vehicle||'').trim(),
    issue:String(r.issue||r.Issue||'').trim(),
    resolution:String(r.resolution||r.Resolution||'').trim(),
    status:String(r.status||r.Status||'').trim(),
    serial:String(r.serial||r.Serial||'').trim(),
    notes:String(r.notes||r.Notes||'').trim(),
  };
}

function dayKey(s){
  if(!s) return '';
  s=String(s).trim(); if(!s) return '';
  let m=s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if(m) return m[1]+'-'+('0'+m[2]).slice(-2)+'-'+('0'+m[3]).slice(-2);
  m=s.match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})/);
  if(m){ let y=+m[3]; if(y<100)y+=2000; return y+'-'+('0'+m[1]).slice(-2)+'-'+('0'+m[2]).slice(-2); }
  const d=new Date(s); if(!isNaN(d)) return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2);
  return '';
}

// ============================================================
//  VIEW (filter + sort)
// ============================================================
function applyView(preserveScroll){
  const gw=$('#gridwrap'); const keepTop = preserveScroll? gw.scrollTop : 0;
  const q=query.trim().toLowerCase();
  VIEW = !q ? DATA.slice() : DATA.filter(t=>
    (t.ticket+' '+t.tech+' '+t.client+' '+t.product+' '+t.vehicle+' '+t.issue+' '+
     t.resolution+' '+t.status+' '+t.serial+' '+t.notes).toLowerCase().indexOf(q)>=0);
  const k=sort.k, dir=sort.dir;
  VIEW.sort((a,b)=>{
    let va,vb;
    if(k==='ticket'){ va=ticketNum(a.ticket); vb=ticketNum(b.ticket); }
    else if(k==='date'){ va=dayKey(a.date); vb=dayKey(b.date); }   // normalize mixed date formats (M/D/YYYY vs ISO) to YYYY-MM-DD so the compare is chronological
    else { va=(a[k]||'').toLowerCase(); vb=(b[k]||'').toLowerCase(); }
    if(va<vb)return -1*dir; if(va>vb)return 1*dir; return 0;
  });
  $('#count').textContent = q? (fmt(VIEW.length)+' of '+fmt(DATA.length)+' tickets')
                              : (fmt(DATA.length)+' tickets');
  computeHeights();
  $('#gbody').style.height = TOTAL_H+'px';
  render();
  if(preserveScroll) gw.scrollTop = keepTop;
}

// ============================================================
//  RENDER (virtualized)
// ============================================================
function buildShell(){
  const gw=$('#gridwrap'); gw.classList.remove('empty-state');
  const head = COLS.map(c=>`<div class="h" data-k="${c.k}" style="width:${c.w}px">
      <span>${esc(c.t)}</span><span class="ar" data-ar="${c.k}"></span></div>`).join('');
  gw.innerHTML = `<div class="content" style="width:${TOTAL_W}px">
      <div class="ghead">${head}</div>
      <div class="gbody" id="gbody"></div>
    </div>`;
  // header sort
  gw.querySelectorAll('.ghead .h').forEach(h=>h.onclick=()=>{
    const k=h.dataset.k;
    sort = sort.k===k? {k,dir:-sort.dir} : {k,dir:(k==='ticket'?-1:1)};
    applyView(true); updateSortArrows();
  });
  updateSortArrows();
  gw.addEventListener('scroll', onScroll, {passive:true});
  // delegated commit on edit inputs
  const body=$('#gbody');
  body.addEventListener('change', onCellChange);
  body.addEventListener('focusin', onEditFocus);
  body.addEventListener('focusout', onEditBlur);
  body.addEventListener('keydown', e=>{ if(e.key==='Enter' && !e.shiftKey && e.target.tagName==='TEXTAREA'){ e.preventDefault(); e.target.blur(); } });
}
function updateSortArrows(){
  COLS.forEach(c=>{ const el=document.querySelector(`[data-ar="${c.k}"]`); if(el) el.textContent = sort.k===c.k? (sort.dir<0?'▼':'▲'):''; });
}
let scrollRAF=null;
function onScroll(){ if(scrollRAF) return; scrollRAF=requestAnimationFrame(()=>{scrollRAF=null;render();}); }

// ---- variable row heights (wrapping) -----------------------------------
// Format any date string to "Wed Jun 10 2026" (no time), parsing the calendar
// parts directly so there's no timezone shift.
function fmtDate(s){
  s=String(s==null?'':s).trim(); if(!s) return '';
  let Y,M,D,m;
  if(m=s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/)){ Y=+m[1];M=+m[2];D=+m[3]; }
  else if(m=s.match(/^(\d{1,2})[\/.](\d{1,2})[\/.](\d{2,4})/)){ M=+m[1];D=+m[2];Y=+m[3]; if(Y<100)Y+=2000; }
  else { const d=new Date(s); return isNaN(d.getTime())? s : d.toDateString(); }
  const d=new Date(Y,M-1,D); return isNaN(d.getTime())? s : d.toDateString();
}
// What a cell actually shows (date gets formatted; everything else is raw).
function dispVal(col,t){ const v=t[col.k]||''; return col.k==='date'? fmtDate(v) : v; }

let _mctx=null;
function _measCtx(){
  if(_mctx) return _mctx;
  _mctx=document.createElement('canvas').getContext('2d');
  _mctx.font='13px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif';
  return _mctx;
}
// Greedy word-wrap line count for `text` within `availW` px (mirrors how the
// browser wraps), honoring explicit newlines and breaking over-long tokens.
function _linesFor(text, availW){
  text=String(text==null?'':text);
  if(!text || availW<=0) return 1;
  const ctx=_measCtx(); let lines=0;
  const paras=text.split('\n');
  for(let p=0;p<paras.length;p++){
    const words=paras[p].split(' '); let line='';
    for(let wi=0;wi<words.length;wi++){
      let w=words[wi];
      const cand = line ? line+' '+w : w;
      if(ctx.measureText(cand).width<=availW){ line=cand; continue; }
      if(line){ lines++; line=''; }                  // wrap before this word
      // a single word wider than the column breaks across lines
      while(ctx.measureText(w).width>availW && w.length>1){
        let cut=w.length;
        while(cut>1 && ctx.measureText(w.slice(0,cut)).width>availW) cut--;
        lines++; w=w.slice(cut);
      }
      line=w;
    }
    lines++;                                          // final line of paragraph
  }
  return Math.max(1, lines);
}
function computeHeights(){
  const n=VIEW.length; ROWTOP=new Array(n); ROWH=new Array(n);
  let y=0;
  for(let i=0;i<n;i++){
    const t=VIEW[i]; let maxLines=1;
    for(let c=0;c<COLS.length;c++){
      const col=COLS[c];
      if(col.type==='select') continue;              // status is single-line
      const ln=_linesFor(dispVal(col,t), col.w-16);
      if(ln>maxLines) maxLines=ln;
    }
    const h=Math.max(ROW_H, maxLines*LINE_H+VPAD);
    ROWH[i]=h; ROWTOP[i]=y; y+=h;
  }
  TOTAL_H=y;
}
// Largest row index whose bottom edge is still below `yTop` (binary search).
function _firstVisible(yTop){
  let lo=0, hi=ROWTOP.length-1, ans=0;
  while(lo<=hi){ const mid=(lo+hi)>>1;
    if(ROWTOP[mid]+ROWH[mid] > yTop){ ans=mid; hi=mid-1; } else lo=mid+1; }
  return ans;
}
function recomputeAndRender(){ computeHeights(); const b=$('#gbody'); if(b) b.style.height=TOTAL_H+'px'; render(); }

function render(){
  const gw=$('#gridwrap'), body=$('#gbody'); if(!body) return;
  if(!VIEW.length || !ROWTOP.length){ body.innerHTML=''; return; }
  const top=gw.scrollTop;
  const viewTop=Math.max(0, top-HEAD_H-300);          // gbody coords, with overscan
  const viewBot=top+gw.clientHeight+300;
  let html='';
  for(let i=_firstVisible(viewTop); i<VIEW.length && ROWTOP[i]<=viewBot; i++){
    const t=VIEW[i];
    const editors=PRESENCE_BY_TICKET[String(t._id)]||[];
    html+=`<div class="grow${editors.length?' is-edited':''}" data-row-id="${esc(t._id)}" style="top:${ROWTOP[i]}px;height:${ROWH[i]}px;width:${TOTAL_W}px">`+
      COLS.map(c=>cellHTML(c,t,i)).join('')+`</div>`;
  }
  body.innerHTML=html;
}
function cellHTML(c,t,i){
  const val=t[c.k]||'';
  if(c.ro){
    const presence=c.k==='ticket'?`<span class="row-presence">${presenceHTML(t)}</span>`:'';
    return `<div class="gc ro ${c.num?'num':''}" style="width:${c.w}px"><div class="cv">${esc(dispVal(c,t))}${presence}</div></div>`;
  }
  if(c.type==='select'){
    const opts=STATUS_OPTIONS.map(o=>`<option${o===val?' selected':''}>${esc(o)}</option>`).join('');
    return `<div class="gc" style="width:${c.w}px"><select class="${statusClass(val)}" data-i="${i}" data-k="${c.k}">${opts}</select></div>`;
  }
  return `<div class="gc" style="width:${c.w}px"><textarea rows="1" data-i="${i}" data-k="${c.k}">${esc(val)}</textarea></div>`;
}

function presenceHTML(t){
  return (PRESENCE_BY_TICKET[String(t._id)]||[]).map(p=>
    `<span class="presence-chip" title="${esc(p.display)} editando ${esc(p.column||'a linha')}">${esc(p.display)}</span>`).join('');
}

// ============================================================
//  INLINE EDIT
// ============================================================
async function onCellChange(e){
  const el=e.target; const i=+el.dataset.i, k=el.dataset.k;
  if(isNaN(i)||!k) return;
  const t=VIEW[i]; if(!t) return;
  const newVal=el.value, oldVal=t[k]||'';
  if(newVal===oldVal) return;
  const cell=el.closest('.gc'); cell.classList.add('saving','dirty');
  t[k]=newVal;
  try{
    if(t._id==null) throw new Error('ticket has no id');
    const patch={};
    const columnMap={date:'ticket_date',ticket:'ticket_number'};
    patch[columnMap[k]||k]=newVal||null;
    const d=await recordsApi('PATCH','/'+encodeURIComponent(t._id),patch);
    Object.assign(t,normalize(d.row));
    cell.classList.remove('saving','dirty');
    if(k==='status' && el.tagName==='SELECT'){ el.className=statusClass(newVal); }
    toast('Ticket '+t.ticket+' updated','ok');
    recomputeAndRender();                 // row may need to grow/shrink to fit
  }catch(err){
    t[k]=oldVal; el.value=oldVal;
    cell.classList.remove('saving','dirty');
    toast('Update failed: '+err.message,'err');
    recomputeAndRender();
  }
}

function onEditFocus(e){
  const el=e.target, i=+el.dataset.i;
  if(isNaN(i) || !VIEW[i]) return;
  const t=VIEW[i];
  if(!t.ticket || t._id==null) return;
  if(EDITING && String(EDITING.id)===String(t._id)) return;
  presenceLeave();
  EDITING={id:t._id, num:t.ticket, timer:null};
  presenceBeat(EDITING);
  EDITING.timer=setInterval(()=>presenceBeat(EDITING),PRESENCE_INTERVAL_MS);
}
function onEditBlur(){
  setTimeout(()=>{
    const a=document.activeElement;
    if(!a || !a.closest || !a.closest('#gbody')){
      presenceLeave();
      renderPresenceMarkers();
    }
  },50);
}
function presenceUrl(num){ return '/api/presence/tickets/'+encodeURIComponent(num); }
// Heartbeat for the row being edited. The reply lists everyone on that ticket
// (here or in the Kit & Ticket Tool), shown as chips on the row.
async function presenceBeat(ed){
  if(EDITING!==ed) return;
  try{
    const r=await fetch(presenceUrl(ed.num),{method:'POST',cache:'no-store',headers:ZZ2.jsonHeaders(),
      body:JSON.stringify({client_id:RT_CLIENT_ID,name:USER_DISPLAY})});
    if(!r.ok) return;
    const d=await r.json();
    if(EDITING!==ed) return;
    PRESENCE_BY_TICKET[String(ed.id)]=(d.viewers||[])
      .filter(v=>v && v.client_id!==RT_CLIENT_ID && v.name)
      .map(v=>({display:v.name}));
    renderPresenceMarkers();
  }catch(e){ /* best-effort */ }
}
// sendBeacon cannot send DELETE or the Authorization header, so leave with a
// keepalive fetch; the server also expires a viewer 30 s after its last beat.
function presenceLeave(){
  const ed=EDITING; if(!ed) return;
  EDITING=null;
  if(ed.timer) clearInterval(ed.timer);
  delete PRESENCE_BY_TICKET[String(ed.id)];
  const url=presenceUrl(ed.num)+'?client_id='+encodeURIComponent(RT_CLIENT_ID);
  const opts={method:'DELETE',headers:ZZ2.authHeaders()};
  try{ fetch(url,Object.assign({keepalive:true},opts)).catch(()=>{}); }
  catch(e){ try{ fetch(url,opts).catch(()=>{}); }catch(e2){} }
}

// ============================================================
//  NEW TICKET
// ============================================================
function openNew(){ $('#overlay').classList.add('show'); $('#n-msg').textContent=''; $('#n-tech').focus(); }
function closeNew(){ $('#overlay').classList.remove('show');
  ['tech','client','product','vehicle','serial','issue','resolution','notes'].forEach(f=>$('#n-'+f).value='');
  $('#n-status').value=''; }
async function saveNew(){
  const g=f=>$('#n-'+f).value;
  const payload={tech:g('tech'),client:g('client'),product:g('product'),
    vehicle:g('vehicle'),serial:g('serial'),status:g('status'),issue:g('issue'),
    resolution:g('resolution'),notes:g('notes')};
  if(!payload.tech && !payload.client && !payload.product){
    $('#n-msg').textContent='Add at least a tech, client, or product.'; $('#n-msg').className='msg err'; return; }
  $('#n-save').disabled=true; $('#n-msg').className='msg'; $('#n-msg').textContent='Creating…';
  try{
    const ticketNumber=nextTicketNumber();
    const insert={ticket_date:new Date().toISOString().slice(0,10),ticket_number:ticketNumber,
      tech:payload.tech||null,client:payload.client||null,product:payload.product||null,
      vehicle:payload.vehicle||null,issue:payload.issue||null,resolution:payload.resolution||null,
      status:payload.status||null,serial:payload.serial||null,notes:payload.notes||null};
    const d=await recordsApi('POST','',insert);
    const row=normalize(d.row);
    if(!DATA.some(t=>String(t._id)===String(row._id))) DATA.push(row);
    closeNew(); applyView(false); $('#gridwrap').scrollTop=0;
    toast('Created '+row.ticket,'ok');
  }catch(err){
    $('#n-msg').className='msg err'; $('#n-msg').textContent='Failed: '+err.message;
  }finally{ $('#n-save').disabled=false; }
}

function nextTicketNumber(){
  let max=0, width=4, prefix='TKT-';
  DATA.forEach(t=>{const m=String(t.ticket||'').match(/^(.*?)(\d+)$/);if(m){const n=+m[2];if(n>max){max=n;width=m[2].length;prefix=m[1];}}});
  return prefix+String(max+1).padStart(width,'0');
}

// ============================================================
//  REFRESH
// ============================================================
function isEditing(){ const a=document.activeElement; return a && (a.tagName==='TEXTAREA'||a.tagName==='SELECT') && a.closest('#gbody'); }
async function hardRefresh(){
  $('#btn-refresh').disabled=true; toast('Refreshing…');
  try{
    await loadTickets(); applyView(true); setMeta(); toast('Up to date','ok');
  }catch(e){ toast('Refresh failed','err'); }
  finally{ $('#btn-refresh').disabled=false; }
}
function setMeta(){
  const g=META.generated?new Date(META.generated):null;
  $('#meta').innerHTML=`<b>${fmt(DATA.length)}</b> tickets`+
    (g&&!isNaN(g)?` · synced ${g.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})}`:'');
}

// ============================================================
//  INIT + AUTH GUARD (any logged-in role)
// ============================================================
function showError(msg){
  $('#gridwrap').className='gridwrap empty-state';
  $('#gridwrap').innerHTML=`<div class="err"><div>
    <div style="font-size:15px;color:var(--ink);font-weight:600;margin-bottom:6px">Couldn't load tickets</div>
    <div>${esc(msg)}</div>
    <div style="margin-top:10px"><button class="btn accent" onclick="location.reload()">Retry</button></div>
  </div></div>`;
}
async function init(){
  try{
    await loadTickets();
    buildShell();
    $('#toolbar').style.display='flex';
    applyView(false);
    setMeta();
  }catch(e){
    showError(e.message==='forbidden'? 'You don\'t have access to ticket data.' : e.message);
  }
}

function renderPresenceMarkers(){
  document.querySelectorAll('.grow[data-row-id]').forEach(row=>{
    const editors=PRESENCE_BY_TICKET[String(row.dataset.rowId)]||[];
    row.classList.toggle('is-edited',editors.length>0);
    const host=row.querySelector('.row-presence');
    if(host) host.innerHTML=editors.map(p=>`<span class="presence-chip" title="${esc(p.display)} editando ${esc(p.column||'a linha')}">${esc(p.display)}</span>`).join('');
  });
}
// wire controls
$('#btn-new').onclick=openNew;
$('#n-cancel').onclick=closeNew;
$('#n-save').onclick=saveNew;
$('#btn-refresh').onclick=hardRefresh;
$('#overlay').addEventListener('click',e=>{ if(e.target.id==='overlay') closeNew(); });
let qTimer=null;
$('#q').addEventListener('input',e=>{ query=e.target.value; clearTimeout(qTimer);
  qTimer=setTimeout(()=>{ $('#gridwrap').scrollTop=0; applyView(false); },180); });
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeNew(); });
window.addEventListener('resize',()=>{ if($('#gbody')) render(); });

(async function guard(){
  try{
    if(!TOKEN){ location.href='/login.html'; return; }
    const r=await ZZ2.verifySession(TOKEN);
    const d=await r.json();
    if(!d.ok){ localStorage.removeItem(AUTH_KEY); location.href='/login.html'; return; }
    if(!(d.permissions||[]).includes('tickets')){ location.href='/index.html'; return; }
    USER_DISPLAY=d.display||d.email||(d.user&&(d.user.display||d.user.email))||'User';
    if(window.zz2BuildNav) zz2BuildNav(d.permissions||[]);
    init();
  }catch(e){ showError(e.message||'Authentication failed.'); }
})();
window.addEventListener('beforeunload',presenceLeave);
window.addEventListener('pagehide',presenceLeave);
