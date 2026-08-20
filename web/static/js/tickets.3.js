// ============================================================
//  ZZ2 Tickets — virtualized Supabase table + inline edit + create
// ============================================================
const AUTH_KEY='zz2_auth_token', ROLE_KEY='zz2_auth_role';
let TOKEN = localStorage.getItem(AUTH_KEY) || '';
let SB=null, SB_USER=null, RT_CHANNEL=null;
let PRESENCE_BY_TICKET={};
const RT_CLIENT_ID=(crypto.randomUUID?crypto.randomUUID():String(Date.now())+'-'+Math.random());

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
async function loadTickets(){
  if(!SB) throw new Error('Supabase is not configured.');
  const rows=[]; const pageSize=1000;
  for(let from=0;;from+=pageSize){
    const {data,error}=await SB.schema('techSupport').from('tickets').select('*')
      .order('ticket_number',{ascending:false}).range(from,from+pageSize-1);
    if(error) throw error;
    rows.push(...(data||[]));
    if(!data || data.length<pageSize) break;
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
    const {data,error}=await SB.schema('techSupport').from('tickets').update(patch)
      .eq('id',t._id).select().single();
    if(error) throw error;
    Object.assign(t,normalize(data));
    cell.classList.remove('saving','dirty');
    if(k==='status' && el.tagName==='SELECT'){ el.className=statusClass(newVal); }
    toast('Ticket '+t.ticket+' updated','ok');
    recomputeAndRender();                 // row may need to grow/shrink to fit
  }catch(err){
    t[k]=oldVal; el.value=oldVal;
    cell.classList.remove('saving','dirty');
    toast('Update failed: '+err.message,'err');
    recomputeAndRender();
  }finally{
    if(RT_CHANNEL) RT_CHANNEL.untrack();
  }
}

function onEditFocus(e){
  const el=e.target, i=+el.dataset.i;
  if(!RT_CHANNEL || isNaN(i) || !VIEW[i]) return;
  const t=VIEW[i];
  RT_CHANNEL.track({client_id:RT_CLIENT_ID,user_id:SB_USER.id,
    display:userDisplay(SB_USER),ticket_id:String(t._id),ticket_number:t.ticket,
    column:el.dataset.k,online_at:new Date().toISOString()});
}
function onEditBlur(){
  setTimeout(()=>{
    const a=document.activeElement;
    if(!a || !a.closest || !a.closest('#gbody')){
      if(RT_CHANNEL) RT_CHANNEL.untrack();
      renderPresenceMarkers();
    }
  },50);
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
    const {data,error}=await SB.schema('techSupport').from('tickets').insert(insert).select().single();
    if(error) throw error;
    const row=normalize(data);
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
  $('#btn-refresh').disabled=true; toast('Refreshing Supabase…');
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
    subscribeRealtime();
  }catch(e){
    showError(e.message==='forbidden'? 'You don\'t have access to ticket data.' : e.message);
  }
}

function userDisplay(user){
  const m=(user&&user.user_metadata)||{};
  return m.display_name||m.full_name||m.name||user.email||'User';
}
function rebuildPresence(){
  const next={};
  const state=RT_CHANNEL?RT_CHANNEL.presenceState():{};
  Object.keys(state).forEach(key=>(state[key]||[]).forEach(p=>{
    if(!p.ticket_id) return;
    (next[String(p.ticket_id)]||(next[String(p.ticket_id)]=[])).push(p);
  }));
  PRESENCE_BY_TICKET=next;
  renderPresenceMarkers();
}
function renderPresenceMarkers(){
  document.querySelectorAll('.grow[data-row-id]').forEach(row=>{
    const editors=PRESENCE_BY_TICKET[String(row.dataset.rowId)]||[];
    row.classList.toggle('is-edited',editors.length>0);
    const host=row.querySelector('.row-presence');
    if(host) host.innerHTML=editors.map(p=>`<span class="presence-chip" title="${esc(p.display)} editando ${esc(p.column||'a linha')}">${esc(p.display)}</span>`).join('');
  });
}
function applyRealtimeChange(payload){
  const row=payload.new&&Object.keys(payload.new).length?payload.new:payload.old;
  if(!row || row.id==null) return;
  const pos=DATA.findIndex(t=>String(t._id)===String(row.id));
  if(payload.eventType==='DELETE'){
    if(pos>=0) DATA.splice(pos,1);
  }else if(pos>=0){
    DATA[pos]=normalize(Object.assign({},DATA[pos]._raw||{},row));
  }else{
    DATA.push(normalize(row));
  }
  if(!isEditing()) applyView(true);
  setMeta();
}
function subscribeRealtime(){
  RT_CHANNEL=SB.channel('techSupport:tickets',{config:{presence:{key:RT_CLIENT_ID}}})
    .on('postgres_changes',{event:'*',schema:'techSupport',table:'tickets'},applyRealtimeChange)
    .on('presence',{event:'sync'},rebuildPresence)
    .on('presence',{event:'join'},rebuildPresence)
    .on('presence',{event:'leave'},rebuildPresence)
    .subscribe(status=>{
      if(status==='CHANNEL_ERROR') toast('Realtime connection unavailable','err');
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
    const cfg=await fetch('/api/config',{cache:'no-store'}).then(r=>r.json());
    if(!window.supabase||!cfg.supabase_url||!cfg.supabase_publishable_key) throw new Error('Supabase is not configured');
    SB=window.supabase.createClient(cfg.supabase_url,cfg.supabase_publishable_key);
    const {data:{session}}=await SB.auth.getSession();
    if(!session){ location.href='/login.html'; return; }
    SB_USER=session.user; TOKEN=session.access_token; localStorage.setItem(AUTH_KEY,TOKEN);
    SB.auth.onAuthStateChange((event,next)=>{
      if(next){TOKEN=next.access_token;localStorage.setItem(AUTH_KEY,TOKEN);}
      else if(event==='SIGNED_OUT'){localStorage.removeItem(AUTH_KEY);location.href='/login.html';}
    });
    const r=await ZZ2.verifySession(TOKEN);
    const d=await r.json();
    if(!d.ok){ localStorage.removeItem(AUTH_KEY); location.href='/login.html'; return; }
    if(!(d.permissions||[]).includes('tickets')){ location.href='/index.html'; return; }
    if(window.zz2BuildNav) zz2BuildNav(d.permissions||[]);
    init();
  }catch(e){ showError(e.message||'Authentication failed.'); }
})();
window.addEventListener('beforeunload',()=>{if(RT_CHANNEL)RT_CHANNEL.untrack();});
