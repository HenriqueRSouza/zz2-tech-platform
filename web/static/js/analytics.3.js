/* ============================================================
   ZZ2 Analytics — tabbed, lazy-rendered.
   Only the active tab's charts are built, so first paint is fast.
   ============================================================ */
const AUTH_KEY='zz2_auth_token';
let TOKEN=localStorage.getItem(AUTH_KEY)||'';
const authHeaders=()=>({'Authorization':'Bearer '+TOKEN});
const DATA_URLS=['/api/analytics'];
const REBUILD_URL='/api/analytics/rebuild';
const WAITING_AUTORESOLVE_DAYS=14;

const $=(s,r=document)=>r.querySelector(s);
const $$=(s,r=document)=>[...r.querySelectorAll(s)];
const fmt=n=>(n==null||isNaN(n))?'—':Number(n).toLocaleString('en-US');
const pct=(a,b)=>b?Math.round(a/b*1000)/10:0;
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const DAY=86400000;
function startOfDay(d){const x=new Date(d);x.setHours(0,0,0,0);return x;}
const today=startOfDay(new Date());
function toast(m,e){const t=$('#toast');t.textContent=m;t.className='show'+(e?' err':'');clearTimeout(t._h);t._h=setTimeout(()=>t.className='',3200);}
function pick(o,ns){for(const n of ns){if(o[n]!=null&&o[n]!=='')return o[n];}return '';}
function parseDate(v){
  if(!v)return null; if(v instanceof Date)return isNaN(v)?null:startOfDay(v);
  const s=String(v).trim(); if(!s)return null;
  let m=s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/); if(m)return startOfDay(new Date(+m[1],+m[2]-1,+m[3]));
  m=s.match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})/); if(m){let y=+m[3];if(y<100)y+=2000;return startOfDay(new Date(y,+m[1]-1,+m[2]));}
  const d=new Date(s); return isNaN(d)?null:startOfDay(d);
}

/* ---- status taxonomy ---- */
function statusCanon(raw){
  const s=String(raw||'').trim().toLowerCase();
  if(!s)return 'open';
  if(s.indexOf('resolv')>=0)return 'resolved';
  if(s.indexOf('wait')>=0)return 'open';
  if(s.indexOf('no sol')>=0||s.indexOf('nosol')>=0)return 'nosol';
  if(s.indexOf('rma')>=0)return 'rma';
  if(s.indexOf('destroy')>=0)return 'destroy';
  if(s.indexOf('distrib')>=0)return 'distrib';
  if(s==='w.o.e.'||s==='woe'||s.indexOf('w.o.e')>=0)return 'woe';
  return 'other';
}
const STATUS_LABEL={resolved:'Resolved',open:'Open / Waiting',nosol:'No solution',rma:'RMA',destroy:'Field Destroy',distrib:'Distributor',woe:'W.O.E.',other:'Other'};
const STATUS_COLOR={resolved:'#16a34a',open:'#f59e0b',nosol:'#dc2626',rma:'#7c3aed',destroy:'#b91c1c',distrib:'#0891b2',woe:'#64748b',other:'#9ca3af'};
function isResolved(t){ if(t.canon==='resolved')return true; if(t.canon==='open'&&t.ageDays!=null&&t.ageDays>WAITING_AUTORESOLVE_DAYS)return true; return false; }
function isOpen(t){ if(t.canon!=='open')return false; if(t.ageDays!=null&&t.ageDays>WAITING_AUTORESOLVE_DAYS)return false; return true; }
function isFail(t){ return t.canon==='nosol'||t.canon==='rma'||t.canon==='destroy'; }

/* ---- normalize ---- */
function normalize(rows){
  return rows.map(r=>{
    const date=parseDate(pick(r,['date','Date','DATE']));
    const t={ date, dateStr:date?date.toISOString().slice(0,10):'', ageDays:date?Math.round((today-date)/DAY):null,
      ticket:String(pick(r,['ticket','ticket_no','Ticket #','number'])||''),
      tech:String(pick(r,['tech','technician','Tech'])||'').trim(),
      client:String(pick(r,['client','customer','Client'])||'').trim(),
      product:String(pick(r,['product','kit','Product','Kit'])||'').trim(),
      vehicle:String(pick(r,['vehicle','Vehicle'])||'').trim(),
      issue:String(pick(r,['issue','Issue'])||'').trim(),
      resolution:String(pick(r,['resolution','Resolution'])||'').trim(),
      status:String(pick(r,['status','Status'])||'').trim(),
      serial:String(pick(r,['serial','Serial'])||'').trim(),
      notes:String(pick(r,['notes','Notes'])||'').trim() };
    t.vehicleRaw=t.vehicle; t.clientRaw=t.client; t.techRaw=t.tech;
    t.canon=statusCanon(t.status);
    return t;
  });
}

/* ---- consolidation (typos + 2-digit years), bucketed for speed ---- */
function lev(a,b){const m=a.length,n=b.length;if(!m)return n;if(!n)return m;let p=[];for(let j=0;j<=n;j++)p[j]=j;for(let i=1;i<=m;i++){const c=[i];for(let j=1;j<=n;j++)c[j]=Math.min(p[j]+1,c[j-1]+1,p[j-1]+(a[i-1]===b[j-1]?0:1));p=c;}return p[n];}
const YY=new Date().getFullYear()%100;
function expandYears(s){ if(!/[a-z]/i.test(s))return s; return s.replace(/(^|[\s(\/-])'?(\d{2})(?=[\s)\/-]|$)/g,(m,pre,d)=>{const n=+d;return pre+(n<=YY+1?2000+n:1900+n);}); }
function cleanLabel(s){return (s||'').replace(/\s+/g,' ').trim();}
function normKey(s){return cleanLabel(s).toLowerCase().replace(/[.,]/g,'');}
function tokenSetKey(s){return normKey(s).split(' ').filter(Boolean).sort().join(' ');}
function numSig(s){return (String(s).match(/\d+/g)||[]).sort().join(',');}
function modelSig(s){return normKey(s).split(' ').filter(w=>w&&/^[a-z]+$/.test(w)&&w.length<=4).sort().join(',');}  // short letter codes: RDX, MDX, GLE, RX...
function caseScore(s){let sc=0;for(const w of String(s).split(/\s+/)){if(!/[a-z]/i.test(w))continue;if(/^[A-Z][a-z]/.test(w))sc+=2;else if(/^[A-Z]+$/.test(w)&&w.length<=4)sc+=1;else if(/^[A-Z0-9]+$/.test(w))sc-=1;}return sc;}
function mergeOk(a,b){
  if(a===b)return true;
  if(numSig(a)!==numSig(b))return false;        // different year / trim number
  if(modelSig(a)!==modelSig(b))return false;     // different short model code (RDX vs MDX) — a 1-letter change here is a real model, not a typo
  if(tokenSetKey(a)===tokenSetKey(b))return true; // same words, different order
  const ml=Math.max(a.length,b.length);
  if(ml<5)return false;
  const d=lev(a,b);return d<=2&&(1-d/ml)>=0.85;   // typo in a longer word
}
function consolidate(values,opts){
  opts=opts||{};
  const items=[];
  for(const v of values){let e=cleanLabel(v);if(!e)continue;if(opts.expandYears)e=expandYears(e);items.push({orig:v,exp:e,key:normKey(e)});}
  const freq=new Map(),disp=new Map();
  for(const it of items){freq.set(it.key,(freq.get(it.key)||0)+1);const d=disp.get(it.key)||{};d[it.exp]=(d[it.exp]||0)+1;disp.set(it.key,d);}
  const keyDisplay=new Map();
  for(const [k,d] of disp) keyDisplay.set(k,Object.keys(d).sort((a,b)=>(d[b]-d[a])||(caseScore(b)-caseScore(a)))[0]);
  const keys=[...freq.keys()].sort((a,b)=>freq.get(b)-freq.get(a));
  const canonOfKey=new Map(),canons=[],buckets=new Map();
  const bk=k=>numSig(k)+'|'+((k.replace(/[^a-z]/g,'')[0])||'');
  for(const k of keys){
    let hit=null;
    if(opts.fuzzy){const cand=buckets.get(bk(k))||[];for(const c of cand){if(mergeOk(k,c)){hit=c;break;}}}
    if(hit){canonOfKey.set(k,hit);}
    else{canonOfKey.set(k,k);canons.push(k);const b=bk(k);if(!buckets.has(b))buckets.set(b,[]);buckets.get(b).push(k);}
  }
  const map=new Map(),merges=[],seen=new Set();
  for(const it of items){const label=keyDisplay.get(canonOfKey.get(it.key));map.set(it.orig,label);
    if(cleanLabel(it.orig)!==label){const s=it.orig+'>>'+label;if(!seen.has(s)){seen.add(s);merges.push([it.orig,label]);}}}
  return {map,merges,before:freq.size,after:canons.length};
}

/* ---- aggregation helpers ---- */
const monthKey=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;
const monthLbl=k=>{const[y,m]=k.split('-');return new Date(y,m-1,1).toLocaleString('en-US',{month:'short',year:'2-digit'});};
function weekKey(d){const x=new Date(d);const day=(x.getDay()+6)%7;x.setDate(x.getDate()-day);return x.toISOString().slice(0,10);}
const DOW=['Mon','Tue','Wed','Thu','Fri','Sat','Sun']; const dowIdx=d=>(d.getDay()+6)%7;
function countBy(arr,f){const m=new Map();for(const x of arr){const k=f(x);if(k==null||k==='')continue;m.set(k,(m.get(k)||0)+1);}return m;}
function topN(m,n){return [...m.entries()].sort((a,b)=>b[1]-a[1]).slice(0,n);}
const STOP=new Set(('the a an and or of to in on for with at by from is are was be it this that no not have has had will would can could should do does did i we you they them us our your their as but if then so just got get out up off into over under na n/a none nil ok okay yes also been being more most some any all each per via vs etc re').split(/\s+/));
function keywords(arr,field,n){const m=new Map();for(const x of arr){const t=(x[field]||'').toLowerCase();if(!t)continue;for(let w of t.split(/[^a-z0-9]+/)){w=w.trim();if(w.length<3||STOP.has(w)||/^\d+$/.test(w))continue;m.set(w,(m.get(w)||0)+1);}}return topN(m,n);}
function bigrams(arr,field,n){const m=new Map();for(const x of arr){const tk=(x[field]||'').toLowerCase().split(/[^a-z0-9]+/).filter(w=>w.length>=3&&!STOP.has(w)&&!/^\d+$/.test(w));for(let i=0;i<tk.length-1;i++){const g=tk[i]+' '+tk[i+1];m.set(g,(m.get(g)||0)+1);}}return [...m.entries()].filter(([_,c])=>c>=2).sort((a,b)=>b[1]-a[1]).slice(0,n);}
function topResolution(arr){const m=new Map();for(const t of arr){const r=cleanLabel(t.resolution);if(!r)continue;const o=m.get(r.toLowerCase())||{disp:r,n:0};o.n++;m.set(r.toLowerCase(),o);}const v=[...m.values()].sort((a,b)=>b.n-a.n);return v[0]||null;}

/* ---- vehicle make / year inference --------------------------------------
   Makes are INFERRED from explicit words or model codes, but model codes stay
   distinct: MDX and RDX both resolve to make "Acura" yet remain separate models
   (no fuzzy 1-letter merging). Year is parsed from a 2- or 4-digit leading token. */
const _CURYY=new Date().getFullYear()%100;
function vehYear(raw){
  const v=String(raw||''); let m=v.match(/\b(19\d{2}|20\d{2})\b/);
  let y; if(m){y=+m[1];} else { m=v.match(/^\s*'?(\d{2})\b/); if(!m)return null; y=+m[1]; y=(y<=_CURYY+1)?2000+y:1900+y; }
  if(y<1990||y>new Date().getFullYear()+1)return null; return y;
}
const _MAKE_WORDS=[
  [/\bmercedes\b|\bmerc\b|\bbenz\b|\bmb\b|\bm\.b\b|\bamg\b/i,'Mercedes'],[/\bbmw\b/i,'BMW'],[/\baudi\b/i,'Audi'],
  [/\blexus\b/i,'Lexus'],[/\bacura\b/i,'Acura'],[/\binfinit[iy]\b/i,'Infiniti'],[/\btoyota\b/i,'Toyota'],
  [/\bhonda\b/i,'Honda'],[/\bporsche\b/i,'Porsche'],[/\bford\b/i,'Ford'],[/\bchev(y|rolet)?\b/i,'Chevrolet'],
  [/\bgmc\b/i,'GMC'],[/\bcadillac\b|\bcaddy\b/i,'Cadillac'],[/\bbuick\b/i,'Buick'],[/\bjeep\b/i,'Jeep'],
  [/\bram\b/i,'Ram'],[/\bdodge\b/i,'Dodge'],[/\bchrysler\b/i,'Chrysler'],[/\bnissan\b/i,'Nissan'],
  [/\bsubaru\b/i,'Subaru'],[/\bmazda\b/i,'Mazda'],[/\bvolkswagen\b|\bvw\b/i,'Volkswagen'],[/\bvolvo\b/i,'Volvo'],
  [/\bjaguar\b|\bjag\b/i,'Jaguar'],[/\bland ?rover\b|\brange ?rover\b|\brr\b|evoque|velar|defender|discovery/i,'Land Rover'],
  [/\bmini\b|cooper/i,'Mini'],[/\btesla\b/i,'Tesla'],[/\brolls|royce|wraith|ghost|phantom|cullinan/i,'Rolls-Royce'],
  [/\bbentley\b|bentayga/i,'Bentley'],[/\bmaserati\b|ghibli|levante|quattroporte/i,'Maserati'],
  [/\blambo(rghini)?\b|aventador|huracan|urus|gallardo/i,'Lamborghini'],[/\bferrari\b/i,'Ferrari'],
  [/\bgenesis\b/i,'Genesis'],[/\bhyundai\b/i,'Hyundai'],[/\bkia\b/i,'Kia'],
];
const _MODEL_MAKE=[
  [/^(911|cayenne|macan|panamera|boxster|cayman|taycan|carr?er+a)/i,'Porsche'],
  [/^(rx|gx|lx|nx|ux|is|es|ls|lc|rc|gs|ct)\d{0,3}$/i,'Lexus'],
  [/^(mdx|rdx|tlx|ilx|rlx|tsx|tl|rl|zdx|nsx|integra|cdx)$/i,'Acura'],
  [/^(qx\d{2}|qx|q\d{2}|g\d{2}|fx\d{2}|jx\d{2}|ex\d{2})$/i,'Infiniti'],
  [/^(x[1-7]|m[1-8]|z[34]|i[3-8]|\d{3}[a-z]{0,3}|[1-8](\sseries)?)$/i,'BMW'],
  [/^(a[1-8]|s[3-8]|q[1-8]|rs\d?|sq\d|tt|r8|e-?tron)$/i,'Audi'],
  [/^(glc|gle|gls|gla|glb|glk|gl|cla|cls|slk|slc|sl|maybach|[cesg]\d{2,3}|ml\d{3}|ntg\d?)/i,'Mercedes'],
  [/^(f-?\d{3}|f150|f250|f350|explorer|escape|expedition|mustang|bronco|edge|fusion|focus|ranger|maverick)/i,'Ford'],
  [/^(silverado|corvette|tahoe|suburban|camaro|malibu|equinox|colorado|blazer|traverse|impala|cruze)/i,'Chevrolet'],
  [/^(sierra|yukon|acadia|canyon|terrain|denali)/i,'GMC'],
  [/^(escalade|cts|ats|xt[4-6]|srx|ct[4-6]|xts|sts)/i,'Cadillac'],
  [/^(highlander|tundra|tacoma|camry|corolla|rav-?4?|4runner|sequoia|avalon|sienna|prius|land ?cruiser|supra|venza)/i,'Toyota'],
  [/^(odyssey|accord|civic|cr-?v|pilot|ridgeline|hr-?v|passport|fit|insight)/i,'Honda'],
  [/^(wrangler|cherokee|gladiator|compass|renegade|grand)/i,'Jeep'],
  [/^(1500|2500|3500|promaster)/i,'Ram'],
  [/^(altima|maxima|rogue|pathfinder|murano|titan|frontier|armada|sentra|370z|gt-?r)/i,'Nissan'],
  [/^(xe|xf|xj|f-?pace|f-?type|e-?pace|i-?pace)/i,'Jaguar'],
];
const _VEHCACHE=new Map();
function vehMake(raw){
  const v=String(raw||'').trim(); if(!v)return 'Unknown';
  if(_VEHCACHE.has(v))return _VEHCACHE.get(v);
  if(/^[\?\-\.]+$|^(na|n\/a|none|unknown|tbd)$/i.test(v)){_VEHCACHE.set(v,'Unknown');return 'Unknown';}
  let make='';
  for(const [re,nm] of _MAKE_WORDS){ if(re.test(v)){make=nm;break;} }
  if(!make){
    const rest=v.replace(/^\s*'?(\d{2,4})\b[\s\-]*/,'').trim();
    const tok=(rest.split(/[\s\-\/]+/)[0]||'');
    for(const [re,nm] of _MODEL_MAKE){ if(re.test(tok)||re.test(rest)){make=nm;break;} }
  }
  if(!make)make='Other';
  _VEHCACHE.set(v,make);return make;
}
function vehModel(raw){ let v=String(raw||'').trim(); if(!v)return ''; return v.replace(/^\s*'?(\d{2,4})\b[\s\-]*/,'').trim().toUpperCase(); }

/* ---- monthly outcome rates (for trend lines + YoY) ---- */
function monthlyRates(f){
  const m=new Map();
  for(const t of f){ if(!t.date)continue; const k=monthKey(t.date);
    let o=m.get(k); if(!o){o={k,total:0,res:0,rma:0,nosol:0};m.set(k,o);}
    o.total++; if(isResolved(t))o.res++; if(t.canon==='rma')o.rma++; if(t.canon==='nosol'||t.canon==='destroy')o.nosol++; }
  return [...m.values()].sort((a,b)=>a.k<b.k?-1:1);
}

/* ============================================================ NEW: quality trends + funnel */
function renderQualityTrends(f){
  const mr=monthlyRates(f).filter(o=>o.total>=3);
  const labels=mr.map(o=>monthLbl(o.k));
  chart('qtrend',{type:'line',data:{labels,datasets:[
    {label:'Resolved %',data:mr.map(o=>pct(o.res,o.total)),borderColor:'#16a34a',backgroundColor:'rgba(22,163,74,.08)',tension:.3,borderWidth:2,pointRadius:0,fill:true},
    {label:'RMA %',data:mr.map(o=>pct(o.rma,o.total)),borderColor:'#7c3aed',tension:.3,borderWidth:2,pointRadius:0},
    {label:'No-solution %',data:mr.map(o=>pct(o.nosol,o.total)),borderColor:'#dc2626',tension:.3,borderWidth:2,pointRadius:0},
  ]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{position:'top',labels:{boxWidth:12,font:{size:11},padding:10}}},scales:{x:{grid:{display:false},ticks:{...TICK,maxRotation:0,autoSkip:true,maxTicksLimit:12}},y:{beginAtZero:true,max:100,grid:GRID,ticks:{...TICK,callback:v=>v+'%'}}}}});
  const all=monthlyRates(f), last=all.slice(-12), prev=all.slice(-24,-12);
  const agg=arr=>{const t=arr.reduce((x,o)=>x+o.total,0);return{res:pct(arr.reduce((x,o)=>x+o.res,0),t),rma:pct(arr.reduce((x,o)=>x+o.rma,0),t)};};
  if(last.length>=6&&prev.length>=6){const a=agg(last),b=agg(prev);
    const dRes=Math.round((a.res-b.res)*10)/10, dRma=Math.round((a.rma-b.rma)*10)/10;
    const cRes=dRes>=0?'green':'red', cRma=dRma<=0?'green':'red';
    $('#qtrend-note').innerHTML=`Last 12 months vs the 12 before — resolution <b style="color:var(--${cRes})">${dRes>=0?'\u25B2':'\u25BC'} ${Math.abs(dRes)} pts</b> (${a.res}% vs ${b.res}%), RMA <b style="color:var(--${cRma})">${dRma<=0?'\u25BC':'\u25B2'} ${Math.abs(dRma)} pts</b> (${a.rma}% vs ${b.rma}%).`;
  } else $('#qtrend-note').textContent='Not enough history in this filter for a year-over-year comparison.';
}
function renderFunnel(f){
  const order=['resolved','open','woe','distrib','nosol','rma','destroy','other'];
  const m=countBy(f,t=>t.canon), total=f.length||1;
  const segs=order.filter(k=>m.get(k)).map(k=>({k,n:m.get(k),p:m.get(k)/total*100}));
  const bar='<div class="ob">'+segs.map(x=>`<div class="ob-seg" style="width:${x.p}%;background:${STATUS_COLOR[x.k]}" title="${STATUS_LABEL[x.k]}: ${fmt(x.n)} (${Math.round(x.p)}%)"></div>`).join('')+'</div>';
  const leg='<div class="ob-leg">'+segs.map(x=>`<span class="ob-li"><i style="background:${STATUS_COLOR[x.k]}"></i>${STATUS_LABEL[x.k]} <b>${fmt(x.n)}</b> <span class="note">${Math.round(x.p*10)/10}%</span></span>`).join('')+'</div>';
  const res=f.filter(isResolved).length, rma=(m.get('rma')||0), nos=(m.get('nosol')||0)+(m.get('destroy')||0);
  const head=`<div class="ob-head"><div><div class="ob-big" style="color:var(--green)">${pct(res,total)}%</div><div class="note">resolved</div></div><div><div class="ob-big" style="color:var(--purple)">${pct(rma,total)}%</div><div class="note">RMA / return</div></div><div><div class="ob-big" style="color:var(--red)">${pct(nos,total)}%</div><div class="note">no-solution</div></div></div>`;
  $('#funnel').innerHTML=head+bar+leg;
}

/* ============================================================ NEW: kit Pareto + drill */
function renderKitPareto(f){
  const m=countBy(f.filter(t=>t.product),t=>t.product);
  const sorted=[...m.entries()].sort((a,b)=>b[1]-a[1]);
  const top=sorted.slice(0,25), grand=sorted.reduce((x,e)=>x+e[1],0)||1;
  let cum=0; const cumPct=top.map(e=>{cum+=e[1];return Math.round(cum/grand*1000)/10;});
  KIT_PARETO_LABELS=top.map(e=>e[0]);
  chart('kit-pareto',{data:{labels:KIT_PARETO_LABELS,datasets:[
    {type:'bar',label:'Tickets',data:top.map(e=>e[1]),backgroundColor:'#fcd34d',borderRadius:4,yAxisID:'y',order:2},
    {type:'line',label:'Cumulative %',data:cumPct,borderColor:'#6366f1',borderWidth:2,pointRadius:2,yAxisID:'y1',tension:.2,order:1}
  ]},options:{responsive:true,maintainAspectRatio:false,onClick:(e,el)=>{if(el&&el.length)openKitDrill(KIT_PARETO_LABELS[el[0].index]);},plugins:{legend:{position:'top',labels:{boxWidth:12,font:{size:11}}},tooltip:{callbacks:{afterBody:it=>'Cumulative '+cumPct[it[0].dataIndex]+'% \u00b7 click to drill'}}},scales:{x:{grid:{display:false},ticks:{...TICK,maxRotation:60,minRotation:45,font:{size:9}}},y:{beginAtZero:true,grid:GRID,ticks:TICK},y1:{position:'right',beginAtZero:true,max:100,grid:{display:false},ticks:{...TICK,callback:v=>v+'%'}}}}});
  let run=0,n50=0,n80=0; for(let i=0;i<sorted.length;i++){run+=sorted[i][1];if(!n50&&run/grand>=0.5)n50=i+1;if(!n80&&run/grand>=0.8){n80=i+1;break;}}
  const shownPct=cumPct.length?cumPct[cumPct.length-1]:0;
  $('#kit-pareto-note').innerHTML=`The top <b>${n50}</b> kits cover half of all tickets; it takes <b>${n80}</b> of ${fmt(sorted.length)} kits to reach 80% &mdash; a long tail, not an 80/20. These top ${top.length} cover ${shownPct}%. Click any bar to drill into that kit's tickets.`;
}
function openDrill(title,tickets){
  const rows=[...tickets].sort((a,b)=>(b.date?+b.date:0)-(a.date?+a.date:0)).slice(0,400).map(t=>`<tr><td class="note" style="white-space:nowrap">${t.dateStr||'\u2014'}</td><td style="white-space:nowrap">${esc(t.ticket)}</td><td>${esc(t.client||'\u2014')}</td><td>${esc(t.vehicle||'\u2014')}</td><td>${esc((t.issue||'').slice(0,90))}</td><td><span class="badge" style="background:${STATUS_COLOR[t.canon]}22;color:${STATUS_COLOR[t.canon]}">${STATUS_LABEL[t.canon]}</span></td></tr>`).join('');
  $('#drill-title').innerHTML=esc(title);
  $('#drill-sub').innerHTML=`${fmt(tickets.length)} ticket${tickets.length===1?'':'s'}`+(tickets.length>400?' \u00b7 showing 400 most recent':'');
  $('#drill-body').innerHTML=`<table><thead><tr><th>Date</th><th>Ticket</th><th>Client</th><th>Vehicle</th><th>Issue</th><th>Status</th></tr></thead><tbody>${rows||'<tr><td colspan="6" class="empty">No tickets.</td></tr>'}</tbody></table>`;
  $('#drill').classList.add('show');
}
function openKitDrill(k){ openDrill('Kit \u00b7 '+k, CUR.filter(t=>t.product===k)); }
function openClientDrill(c){ openDrill('Account \u00b7 '+c, CUR.filter(t=>t.client===c)); }
function openMakeDrill(mk){ openDrill('Make \u00b7 '+mk, CUR.filter(t=>(t.make||'Other')===mk)); }
function closeDrill(){ const d=$('#drill'); if(d)d.classList.remove('show'); }
function wireDrill(){
  const x=$('#drill-x'); if(x)x.onclick=closeDrill;
  const d=$('#drill'); if(d)d.onclick=e=>{if(e.target.id==='drill')closeDrill();};
  if(!window._drillDocWired){window._drillDocWired=true;
    document.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('a.drill');if(!a)return;
      if(a.dataset.kit)openKitDrill(a.dataset.kit);else if(a.dataset.client)openClientDrill(a.dataset.client);else if(a.dataset.make)openMakeDrill(a.dataset.make);});
    document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDrill();});}
}

/* ============================================================ NEW: account health */
function renderAccountHealth(f){
  const first=new Map();
  for(const t of f){ if(!t.client||!t.date)continue; const mk=monthKey(t.date); const c=first.get(t.client); if(c==null||mk<c)first.set(t.client,mk); }
  const months=new Map();
  for(const t of f){ if(!t.client||!t.date)continue; const mk=monthKey(t.date); let o=months.get(mk); if(!o){o={k:mk,nw:0,rt:0};months.set(mk,o);} if(first.get(t.client)===mk)o.nw++; else o.rt++; }
  const ms=[...months.values()].sort((a,b)=>a.k<b.k?-1:1);
  chart('acct-trend',{type:'bar',data:{labels:ms.map(o=>monthLbl(o.k)),datasets:[
    {label:'New clients',data:ms.map(o=>o.nw),backgroundColor:'#6366f1',stack:'s'},
    {label:'Returning',data:ms.map(o=>o.rt),backgroundColor:'#a5b4fc',stack:'s'}
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'top',labels:{boxWidth:12,font:{size:11}}}},scales:{x:{stacked:true,grid:{display:false},ticks:{...TICK,maxRotation:0,autoSkip:true,maxTicksLimit:12}},y:{stacked:true,beginAtZero:true,grid:GRID,ticks:TICK}}}});
  const cn=[...new Set(f.map(t=>t.client).filter(Boolean))];
  const rows=cn.map(c=>{const cs=f.filter(t=>t.client===c);const fail=cs.filter(isFail).length;return{client:c,total:cs.length,fail,rate:pct(fail,cs.length)};}).filter(r=>r.total>=4&&r.fail>0).sort((a,b)=>b.rate-a.rate||b.fail-a.fail).slice(0,20);
  if(rows.length) rankedTable('#acct-risk',rows,[
    {key:'client',label:'Account',bar:true,render:r=>`<a class="drill" data-client="${esc(r.client)}">${esc(r.client)}</a>`},
    {key:'total',label:'Tickets',num:true,render:r=>fmt(r.total)},
    {key:'fail',label:'RMA/NoSol',num:true,render:r=>fmt(r.fail)},
    {key:'rate',label:'Fail %',num:true,render:r=>`<span class="pct">${r.rate}%</span>`},
  ],'rate'); else $('#acct-risk').innerHTML='<div class="empty">No accounts with 4+ tickets and a failure here.</div>';
  const dist=f.filter(t=>t.canon==='distrib'||/distrib/i.test(t.status)).length;
  $('#acct-seg').innerHTML=`${fmt(cn.length)} clients \u00b7 <b>${fmt(dist)}</b> distributor-channel \u00b7 <b>${fmt(f.length-dist)}</b> direct`;
}

/* ============================================================ NEW: vehicles tab */
function renderVehiclesTab(f){
  const fv=f.filter(t=>t.vehicle);
  const mkAll=new Map();
  for(const t of fv){const mk=t.make||'Other';let o=mkAll.get(mk);if(!o){o={make:mk,total:0,fail:0};mkAll.set(mk,o);}o.total++;if(isFail(t))o.fail++;}
  const makes=[...mkAll.values()].sort((a,b)=>b.total-a.total);
  const topMk=makes.slice(0,14);
  chart('veh-make',{type:'bar',data:{labels:topMk.map(m=>m.make),datasets:[{data:topMk.map(m=>m.total),backgroundColor:'#5eead4',borderRadius:5}]},options:hbar({onClick:(e,el)=>{if(el&&el.length)openMakeDrill(topMk[el[0].index].make);}})});
  rankedTable('#veh-make-tbl',makes.map(m=>({make:m.make,total:m.total,fail:m.fail,failRate:pct(m.fail,m.total)})),[
    {key:'make',label:'Make',bar:true,render:r=>`<a class="drill" data-make="${esc(r.make)}">${esc(r.make)}</a>`},
    {key:'total',label:'Tickets',num:true,render:r=>fmt(r.total)},
    {key:'fail',label:'Fail load',num:true,render:r=>fmt(r.fail)},
    {key:'failRate',label:'Fail %',num:true,render:r=>`<span class="pct">${r.failRate}%</span>`},
  ],'total');
  const yr=new Map();
  for(const t of fv){if(t.year==null)continue;let o=yr.get(t.year);if(!o){o={year:t.year,total:0,fail:0};yr.set(t.year,o);}o.total++;if(isFail(t))o.fail++;}
  const years=[...yr.values()].filter(o=>o.total>=5).sort((a,b)=>a.year-b.year);
  chart('veh-year',{data:{labels:years.map(o=>o.year),datasets:[
    {type:'bar',label:'Tickets',data:years.map(o=>o.total),backgroundColor:'#c7d2fe',yAxisID:'y',borderRadius:4,order:2},
    {type:'line',label:'Fail %',data:years.map(o=>pct(o.fail,o.total)),borderColor:'#dc2626',borderWidth:2,pointRadius:2,yAxisID:'y1',tension:.25,order:1}
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'top',labels:{boxWidth:12,font:{size:11}}}},scales:{x:{grid:{display:false},ticks:TICK},y:{beginAtZero:true,grid:GRID,ticks:TICK},y1:{position:'right',beginAtZero:true,max:100,grid:{display:false},ticks:{...TICK,callback:v=>v+'%'}}}}});
  renderVehMatrix(fv);
  const vtop=topN(countBy(fv,t=>t.vehicle),14);
  chart('veh-vol',{type:'bar',data:{labels:vtop.map(r=>r[0]),datasets:[{data:vtop.map(r=>r[1]),backgroundColor:'#5eead4',borderRadius:5}]},options:hbar()});
  const vn=[...new Set(fv.map(t=>t.vehicle))];
  const vrows=vn.map(v=>{const vs=fv.filter(t=>t.vehicle===v);const tk=topN(countBy(vs.filter(t=>t.product),t=>t.product),1)[0];
    return {vehicle:v,total:vs.length,fail:vs.filter(isFail).length,failRate:pct(vs.filter(isFail).length,vs.length),kit:tk?tk[0]:'\u2014'};}).filter(r=>r.total>=3);
  rankedTable('#veh-table',vrows,[
    {key:'vehicle',label:'Vehicle',bar:true},
    {key:'total',label:'Tickets',num:true,render:r=>fmt(r.total)},
    {key:'fail',label:'Fail load',num:true,render:r=>fmt(r.fail)},
    {key:'failRate',label:'Fail %',num:true,render:r=>`<span class="pct">${r.failRate}%</span>`},
    {key:'kit',label:'Top kit',render:r=>esc(r.kit)},
  ],'total');
}
function renderVehMatrix(fv){
  const ff=fv.filter(t=>t.product);
  const topMakes=topN(countBy(ff,t=>t.make||'Other'),8).map(x=>x[0]);
  const topKits=topN(countBy(ff,t=>t.product),12).map(x=>x[0]);
  let html='<table class="heat"><thead><tr><th>Kit \\ Make</th>'+topMakes.map(m=>`<th class="num">${esc(m)}</th>`).join('')+'</tr></thead><tbody>';
  for(const kit of topKits){
    html+=`<tr><td class="lab">${esc(kit)}</td>`;
    for(const mk of topMakes){
      const cell=ff.filter(t=>t.product===kit&&(t.make||'Other')===mk);
      const tot=cell.length, fail=cell.filter(isFail).length, fr=tot?fail/tot:0;
      const bg=tot?`rgba(220,38,38,${Math.min(.82,fr*0.85+(fail?0.12:0.02))})`:'#fff';
      html+=`<td class="h" style="background:${bg}" title="${esc(kit)} \u00b7 ${esc(mk)}: ${fail}/${tot} fail">${tot?(fail||'\u00b7'):''}</td>`;
    }
    html+='</tr>';
  }
  $('#veh-matrix').innerHTML=html+'</tbody></table>';
}

/* ============================================================ accounts tab (clients + health) */
function renderAccountsTab(f){
  const cn=[...new Set(f.map(t=>t.client).filter(Boolean))];
  const crows=cn.map(name=>{const cs=f.filter(t=>t.client===name);const last=cs.map(t=>t.date).filter(Boolean).sort((a,b)=>b-a)[0];
    return {client:name,total:cs.length,open:cs.filter(isOpen).length,rma:cs.filter(t=>t.canon==='rma').length,last:last?last.toISOString().slice(0,10):'\u2014'};});
  rankedTable('#cli-table',crows,[
    {key:'client',label:'Client',bar:true,render:r=>`<a class="drill" data-client="${esc(r.client)}">${esc(r.client)}</a>`},
    {key:'total',label:'Tickets',num:true,render:r=>fmt(r.total)},
    {key:'open',label:'Open',num:true,render:r=>fmt(r.open)},
    {key:'rma',label:'RMA',num:true,render:r=>fmt(r.rma)},
    {key:'last',label:'Last',render:r=>r.last},
  ],'total');
  $('#cli-note').textContent=`${fmt(cn.length)} clients \u00b7 ${fmt(crows.filter(r=>r.total>1).length)} repeat.`;
  const g=new Map();
  for(const t of f){if(!t.client||!t.product)continue;const k=t.client+' \u00b7 '+t.product;const o=g.get(k)||{combo:k,total:0,open:0,rma:0};o.total++;if(isOpen(t))o.open++;if(isFail(t))o.rma++;g.set(k,o);}
  const chronic=[...g.values()].filter(o=>o.total>=3);
  if(chronic.length) rankedTable('#chronic',chronic,[
    {key:'combo',label:'Client \u00b7 Kit',bar:true},
    {key:'total',label:'Tickets',num:true,render:r=>fmt(r.total)},
    {key:'open',label:'Open',num:true,render:r=>fmt(r.open)},
    {key:'rma',label:'RMA/NoSol',num:true,render:r=>fmt(r.rma)},
  ],'total'); else $('#chronic').innerHTML='<div class="empty">No client + kit seen 3+ times here.</div>';
  renderAccountHealth(f);
}

/* ============================================================ STATE */
let ALL=[], NORM={}, CUR=[], META={generated:null}, charts={}, sortState={};
let KIT_PARETO_LABELS=[];
const F={from:'',to:'',tech:'',kit:'',status:''};
let activeTab='overview';

function applyFilters(){
  const from=F.from?parseDate(F.from):null, to=F.to?parseDate(F.to):null;
  return ALL.filter(t=>{
    if(from&&(!t.date||t.date<from))return false;
    if(to&&(!t.date||t.date>to))return false;
    if(F.tech&&t.tech!==F.tech)return false;
    if(F.kit&&t.product!==F.kit)return false;
    if(F.status&&t.canon!==F.status)return false;
    return true;
  });
}

/* ============================================================ CHART/TABLE HELPERS */
function destroyCharts(){for(const k in charts){try{charts[k].destroy();}catch(e){}}charts={};}
function chart(id,cfg){const cv=$('#'+id);if(!cv)return;if(charts[id]){try{charts[id].destroy();}catch(e){}}charts[id]=new Chart(cv.getContext('2d'),cfg);}
const GRID={color:'#eef1f4'}, TICK={color:'#6b7280',font:{size:11}}, noLeg={legend:{display:false}};
const hbar=(extra)=>Object.assign({indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:noLeg,scales:{x:{beginAtZero:true,grid:GRID,ticks:TICK},y:{grid:{display:false},ticks:{...TICK,font:{size:10}}}}},extra||{});

function rankedTable(host,rows,cols,maxBarCol){
  const max=rows.length?Math.max(...rows.map(r=>r[maxBarCol]||0)):0;
  const st=sortState[host]||{col:maxBarCol,dir:-1};
  rows.sort((a,b)=>{const va=a[st.col],vb=b[st.col];if(typeof va==='number'&&typeof vb==='number')return (va-vb)*st.dir;return String(va).localeCompare(String(vb))*st.dir;});
  const thead='<thead><tr>'+cols.map(c=>`<th class="${c.num?'num':''}" data-col="${c.key}">${c.label}${st.col===c.key?`<span class="arrow">${st.dir<0?'▼':'▲'}</span>`:''}</th>`).join('')+'</tr></thead>';
  const tbody='<tbody>'+rows.map(r=>'<tr>'+cols.map(c=>{let v=r[c.key],cell;
    if(c.render)cell=c.render(r);
    else if(c.bar){const w=max?Math.round((r[maxBarCol]||0)/max*100):0;cell=`<div class="bar-cell"><div class="fill" style="width:${w}%"></div><span>${esc(v)}</span></div>`;}
    else cell=esc(v==null?'':v);
    return `<td class="${c.num?'num':''}">${cell}</td>`;}).join('')+'</tr>').join('')+'</tbody>';
  const node=$(host); node.innerHTML=`<table>${thead}${tbody}</table>`;
  $$('thead th',node).forEach(th=>th.onclick=()=>{const col=th.dataset.col;const cur=sortState[host]||{col:maxBarCol,dir:-1};sortState[host]=cur.col===col?{col,dir:-cur.dir}:{col,dir:-1};rankedTable(host,rows,cols,maxBarCol);});
}

/* ============================================================ KIT MODEL */
function kitStats(f){
  const names=[...new Set(f.map(t=>t.product).filter(Boolean))];
  const recentCut=90, priorCut=180;
  return names.map(kit=>{
    const ks=f.filter(t=>t.product===kit);
    const total=ks.length;
    const resolved=ks.filter(isResolved).length, open=ks.filter(isOpen).length;
    const nosol=ks.filter(t=>t.canon==='nosol').length, rma=ks.filter(t=>t.canon==='rma').length, destroy=ks.filter(t=>t.canon==='destroy').length;
    const fail=nosol+rma+destroy;
    const score=Math.min(100,Math.round(((nosol*1+rma*2+destroy*3+open*0.5)/total)*100/3));
    const rec=ks.filter(t=>t.ageDays!=null&&t.ageDays<=recentCut);
    const pri=ks.filter(t=>t.ageDays!=null&&t.ageDays>recentCut&&t.ageDays<=priorCut);
    const recFail=pct(rec.filter(isFail).length,rec.length), priFail=pct(pri.filter(isFail).length,pri.length);
    const vt=topN(countBy(ks.filter(t=>t.vehicle),t=>t.vehicle),1)[0];
    const fix=topResolution(ks);
    const iss=keywords(ks,'issue',1)[0];
    return {kit,total,open,resolved,nosol,rma,destroy,fail,
      failRate:pct(fail,total),resRate:pct(resolved,total),score,
      recFail,priFail,trend:Math.round((recFail-priFail)*10)/10,
      vehicle:vt?vt[0]:'—', fix:fix?fix.disp:'', fixShare:fix?pct(fix.n,total):0, issue:iss?iss[0]:'—'};
  });
}
function scoreColor(s){ const h=Math.round((1-s/100)*120); return `hsl(${h},65%,45%)`; }

/* ============================================================ KPIs */
function renderKPIs(f){
  const total=f.length, resolved=f.filter(isResolved).length, open=f.filter(isOpen).length;
  const rma=f.filter(t=>t.canon==='rma').length, esc2=f.filter(isFail).length;
  const cc=new Map(); for(const t of f){if(t.client)cc.set(t.client,(cc.get(t.client)||0)+1);}
  let repeat=0; for(const v of cc.values())if(v>1)repeat++;
  $('#kpis').innerHTML=`
    <div class="kpi"><div class="v">${fmt(total)}</div><div class="l">Tickets (filtered)</div><div class="d">${fmt(ALL.length)} all-time</div></div>
    <div class="kpi g"><div class="v">${pct(resolved,total)}%</div><div class="l">Resolution rate</div><div class="d">${fmt(resolved)} resolved</div></div>
    <div class="kpi a"><div class="v">${fmt(open)}</div><div class="l">Open / awaiting</div></div>
    <div class="kpi r"><div class="v">${pct(rma,total)}%</div><div class="l">RMA rate</div><div class="d">${pct(esc2,total)}% escalated</div></div>
    <div class="kpi p"><div class="v">${fmt(repeat)}</div><div class="l">Repeat clients</div><div class="d">${pct(repeat,cc.size)}% of ${fmt(cc.size)}</div></div>
    <div class="kpi"><div class="v">${fmt([...new Set(f.map(t=>t.product).filter(Boolean))].length)}</div><div class="l">Active kits</div></div>`;
}

/* ============================================================ TAB: OVERVIEW */
let tsMode='month';
function renderOverview(f){
  const dated=f.filter(t=>t.date);
  let keyFn,lblFn;
  if(tsMode==='day'){keyFn=t=>t.dateStr;lblFn=k=>k.slice(5);}
  else if(tsMode==='week'){keyFn=t=>weekKey(t.date);lblFn=k=>k.slice(5);}
  else{keyFn=t=>monthKey(t.date);lblFn=monthLbl;}
  const m=countBy(dated,keyFn); let keys=[...m.keys()].sort();
  if(tsMode==='day'&&keys.length>90)keys=keys.slice(-90);
  if(tsMode==='week'&&keys.length>60)keys=keys.slice(-60);
  chart('ts',{type:'line',data:{labels:keys.map(lblFn),datasets:[{data:keys.map(k=>m.get(k)),borderColor:'#6366f1',backgroundColor:'rgba(99,102,241,.12)',fill:true,tension:.3,pointRadius:keys.length>40?0:3,borderWidth:2}]},options:{responsive:true,maintainAspectRatio:false,plugins:noLeg,scales:{x:{grid:{display:false},ticks:{...TICK,maxRotation:0,autoSkip:true,maxTicksLimit:12}},y:{beginAtZero:true,grid:GRID,ticks:TICK}}}});
  const sm=countBy(f,t=>t.canon); const order=['resolved','open','nosol','rma','destroy','distrib','woe','other'].filter(k=>sm.has(k));
  chart('status',{type:'doughnut',data:{labels:order.map(k=>STATUS_LABEL[k]),datasets:[{data:order.map(k=>sm.get(k)),backgroundColor:order.map(k=>STATUS_COLOR[k]),borderWidth:2,borderColor:'#fff'}]},options:{responsive:true,maintainAspectRatio:false,cutout:'62%',plugins:{legend:{position:'right',labels:{boxWidth:12,font:{size:11},padding:9}}}}});
  const c=new Array(7).fill(0); for(const t of f)if(t.date)c[dowIdx(t.date)]++;
  chart('dow',{type:'bar',data:{labels:DOW,datasets:[{data:c,backgroundColor:'#a5b4fc',borderRadius:5}]},options:{responsive:true,maintainAspectRatio:false,plugins:noLeg,scales:{x:{grid:{display:false},ticks:TICK},y:{beginAtZero:true,grid:GRID,ticks:TICK}}}});
  const open=f.filter(isOpen);
  const bk=[['0–7d',0,7],['8–14d',8,14],['15–30d',15,30],['31–60d',31,60],['61–90d',61,90],['90d+',91,1e9]];
  const cols=['#86efac','#fde68a','#fca5a5','#f87171','#ef4444','#b91c1c'];
  chart('age',{type:'bar',data:{labels:bk.map(b=>b[0]),datasets:[{data:bk.map(([_,lo,hi])=>open.filter(t=>t.ageDays>=lo&&t.ageDays<=hi).length),backgroundColor:cols,borderRadius:5}]},options:{responsive:true,maintainAspectRatio:false,plugins:noLeg,scales:{x:{grid:{display:false},ticks:TICK},y:{beginAtZero:true,grid:GRID,ticks:TICK}}}});
  const stale=open.filter(t=>t.ageDays>30).length;
  $('#aging-note').innerHTML=open.length?`${fmt(open.length)} open · <b style="color:var(--red)">${fmt(stale)}</b> older than 30 days.`:'No open tickets in this filter.';
  renderQualityTrends(f);
  renderFunnel(f);
}

/* ============================================================ TAB: KITS */
function renderKitsTab(f){
  const rows=kitStats(f);
  const top=[...rows].sort((a,b)=>b.total-a.total).slice(0,14);
  chart('kit-vol',{type:'bar',data:{labels:top.map(r=>r.kit),datasets:[{data:top.map(r=>r.total),backgroundColor:'#fcd34d',borderRadius:5}]},options:hbar()});
  const byScore=rows.filter(r=>r.total>=3).sort((a,b)=>b.score-a.score).slice(0,14);
  chart('kit-score',{type:'bar',data:{labels:byScore.map(r=>r.kit),datasets:[{data:byScore.map(r=>r.score),backgroundColor:byScore.map(r=>scoreColor(r.score)),borderRadius:5}]},options:hbar({plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>'Trouble score '+c.raw}}}})});
  rankedTable('#kit-table',rows,[
    {key:'kit',label:'Kit',bar:true},
    {key:'total',label:'Tickets',num:true,render:r=>fmt(r.total)},
    {key:'open',label:'Open',num:true,render:r=>fmt(r.open)},
    {key:'fail',label:'Fail load',num:true,render:r=>fmt(r.fail)},
    {key:'failRate',label:'Fail %',num:true,render:r=>`<span class="pct">${r.failRate}%</span>`},
    {key:'resRate',label:'Resolved %',num:true,render:r=>`<span class="pct">${r.resRate}%</span>`},
    {key:'score',label:'Trouble',num:true,render:r=>`<span class="score" style="background:${scoreColor(r.score)}">${r.score}</span>`},
  ],'total');
  const rising=rows.filter(r=>r.total>=4&&r.trend>0&&r.recFail>r.priFail).sort((a,b)=>b.trend-a.trend).slice(0,8);
  $('#kit-rising').innerHTML = rising.length
    ? rising.map(r=>`<div class="anomaly ${r.trend>=20?'hot':''}"><span>📈</span><div><b>${esc(r.kit)}</b> — failure rate ${r.priFail}% → <b>${r.recFail}%</b> (last 90 days), up ${r.trend} pts.</div></div>`).join('')
    : '<div class="empty">No kits trending worse in this filter.</div>';
  renderKitHeat($('#kit-heat'), top.slice(0,12), f);
  renderKitPareto(f);
  const sig=rows.filter(r=>r.total>=3).sort((a,b)=>b.total-a.total).slice(0,20);
  rankedTable('#kit-sig',sig,[
    {key:'kit',label:'Kit',bar:true},
    {key:'issue',label:'Top issue word',render:r=>esc(r.issue)},
    {key:'fix',label:'Usual fix',render:r=>esc(r.fix.length>40?r.fix.slice(0,40)+'…':r.fix)},
    {key:'fixShare',label:'Fix %',num:true,render:r=>`<span class="pct">${r.fixShare}%</span>`},
    {key:'vehicle',label:'Top vehicle',render:r=>esc(r.vehicle)},
  ],'total');
}
function renderKitHeat(host,kits,f){
  const cats=['resolved','open','nosol','rma','destroy'];
  let html='<table class="heat"><thead><tr><th>Kit</th>'+cats.map(c=>`<th class="num">${STATUS_LABEL[c]}</th>`).join('')+'<th class="num">Total</th></tr></thead><tbody>';
  for(const k of kits){
    const ks=f.filter(t=>t.product===k.kit); const tot=ks.length;
    html+=`<tr><td class="lab">${esc(k.kit)}</td>`;
    for(const c of cats){const n=ks.filter(t=>t.canon===c).length;const share=tot?n/tot:0;
      const col=STATUS_COLOR[c]; const bg=`${col}${Math.round(share*200+15).toString(16).padStart(2,'0')}`;
      html+=`<td class="h" style="background:${n?bg:'#fff'}">${n||''}</td>`;}
    html+=`<td class="h" style="font-weight:700">${tot}</td></tr>`;
  }
  host.innerHTML=html+'</tbody></table>';
}

/* ============================================================ TAB: TECHNICIANS */
function renderTechsTab(f){
  const names=[...new Set(f.map(t=>t.tech).filter(Boolean))];
  const rows=names.map(name=>{
    const ts=f.filter(t=>t.tech===name);
    const resolved=ts.filter(isResolved).length, open=ts.filter(isOpen).length;
    const rma=ts.filter(t=>t.canon==='rma').length, nosol=ts.filter(t=>t.canon==='nosol'||t.canon==='destroy').length;
    const topKit=topN(countBy(ts.filter(t=>t.product),t=>t.product),1)[0];
    return {tech:name,total:ts.length,open,resolved,rate:pct(resolved,ts.length),rmaRate:pct(rma,ts.length),nosolRate:pct(nosol,ts.length),specialty:topKit?topKit[0]:'—'};
  });
  const top=[...rows].sort((a,b)=>b.total-a.total).slice(0,12);
  chart('tech-vol',{type:'bar',data:{labels:top.map(r=>r.tech),datasets:[{data:top.map(r=>r.total),backgroundColor:'#818cf8',borderRadius:5}]},options:hbar()});
  rankedTable('#tech-table',rows,[
    {key:'tech',label:'Technician',bar:true},
    {key:'total',label:'Total',num:true,render:r=>fmt(r.total)},
    {key:'open',label:'Open',num:true,render:r=>fmt(r.open)},
    {key:'rate',label:'Resolved %',num:true,render:r=>`<span class="pct">${r.rate}%</span>`},
    {key:'rmaRate',label:'RMA %',num:true,render:r=>`<span class="pct">${r.rmaRate}%</span>`},
    {key:'nosolRate',label:'No-sol %',num:true,render:r=>`<span class="pct">${r.nosolRate}%</span>`},
    {key:'specialty',label:'Most-handled kit',render:r=>esc(r.specialty)},
  ],'total');
}


/* (clients/vehicles split into renderAccountsTab + renderVehiclesTab above) */

/* ============================================================ TAB: QUALITY */
/* ============================================================ TAB: QUALITY */
function renderQualityTab(f){
  const fill=(host,words,phrases)=>{const n=$(host);let h=words.length?words.map(([w,c])=>`<span class="kw">${esc(w)} <b>${c}</b></span>`).join(''):'<span class="note">No text.</span>';
    if(phrases.length)h+=`<div class="note" style="margin:10px 0 5px;font-weight:600">Common phrases</div>`+phrases.map(([w,c])=>`<span class="kw" style="background:var(--accent-soft);border-color:#dfe3ff">${esc(w)} <b>${c}</b></span>`).join('');n.innerHTML=h;};
  fill('#kw-issue',keywords(f,'issue',16),bigrams(f,'issue',8));
  fill('#kw-res',keywords(f,'resolution',16),bigrams(f,'resolution',8));
  renderAnomalies(f);
  const dg=new Map(); for(const t of f.filter(isOpen)){const k=(t.client+'|'+t.product).toLowerCase();dg.set(k,(dg.get(k)||0)+1);}
  let dup=0; for(const v of dg.values())if(v>1)dup+=v-1;
  $('#dup-note').innerHTML=`<b>${fmt(dup)}</b> likely duplicate open tickets (same client + kit) — ${pct(dup,f.length)}% of filtered.`;
  renderDataQuality();
}
function renderAnomalies(f){
  const host=$('#anomalies'); const items=[];
  const byDay=countBy(f.filter(t=>t.date),t=>t.dateStr); const vals=[...byDay.values()];
  if(vals.length>=7){const mean=vals.reduce((a,b)=>a+b,0)/vals.length;const std=Math.sqrt(vals.reduce((a,b)=>a+(b-mean)**2,0)/vals.length)||0;const thr=mean+2*std;
    for(const [d,v] of [...byDay.entries()].filter(([_,v])=>v>thr&&v>=Math.max(3,mean*1.5)).sort((a,b)=>b[1]-a[1]).slice(0,5))
      items.push({hot:v>mean+3*std,txt:`<b>${v}</b> tickets on <b>${d}</b> — ${Math.round(v/mean*10)/10}× the daily average.`});}
  const rec=f.filter(t=>t.date&&t.ageDays<=30), pri=f.filter(t=>t.date&&t.ageDays>30&&t.ageDays<=90);
  const rk=countBy(rec.filter(isFail),t=>t.product), pk=countBy(pri.filter(isFail),t=>t.product);
  for(const [kit,rc] of topN(rk,8)){const base=(pk.get(kit)||0)/3;if(rc>=4&&rc>base*2)items.push({hot:rc>base*4,txt:`Kit <b>${esc(kit)}</b> logged <b>${rc}</b> failures in 30 days vs ~${Math.round(base*10)/10}/mo baseline.`});}
  host.innerHTML=items.length?items.map(i=>`<div class="anomaly ${i.hot?'hot':''}"><span>${i.hot?'🔴':'⚠️'}</span><div>${i.txt}</div></div>`).join(''):'<div class="empty">No anomalies in this filter.</div>';
}
function renderDataQuality(){
  const host=$('#dataquality'); if(!host)return; if(!NORM||!NORM.vehicle){host.innerHTML='<div class="empty">—</div>';return;}
  const v=NORM.vehicle,c=NORM.client,te=NORM.tech;
  const blanks=field=>ALL.filter(t=>!t[field]).length;
  const ex=m=>m.length?m.slice(0,6).map(([a,b])=>`<div style="font-size:12px;padding:2px 0"><span style="color:var(--muted)">${esc(a)}</span> → <b>${esc(b)}</b></div>`).join(''):'<div class="note">none</div>';
  const stat=(o,l)=>`<div><div style="font-size:20px;font-weight:700;font-family:var(--mono)">${fmt(o.before)}→${fmt(o.after)}</div><div class="note">${l} (${fmt(o.merges.length)} merged)</div></div>`;
  host.innerHTML=`<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px">${stat(v,'vehicle labels')}${stat(c,'client labels')}${stat(te,'tech names')}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px"><div><div class="note" style="font-weight:600;margin-bottom:4px">Vehicle consolidations</div>${ex(v.merges)}</div><div><div class="note" style="font-weight:600;margin-bottom:4px">Client consolidations</div>${ex(c.merges)}</div></div>
    <div class="note" style="margin-top:12px">Missing — vehicle: ${fmt(blanks('vehicle'))}, client: ${fmt(blanks('client'))}, status: ${fmt(blanks('status'))} (blank = open). 2-digit years expand to 4; different years are kept separate.</div>`;
}

/* ============================================================ TAB DISPATCH */
function renderActive(){
  destroyCharts();
  const f=CUR;
  if(activeTab==='overview')renderOverview(f);
  else if(activeTab==='kits')renderKitsTab(f);
  else if(activeTab==='techs')renderTechsTab(f);
  else if(activeTab==='accounts')renderAccountsTab(f);
  else if(activeTab==='vehicles')renderVehiclesTab(f);
  else if(activeTab==='quality')renderQualityTab(f);
}
function showTab(name){
  activeTab=name;
  $$('.tab').forEach(b=>b.classList.toggle('on',b.dataset.tab===name));
  $$('.tab-panel').forEach(p=>p.classList.toggle('show',p.dataset.tab===name));
  renderActive();
}
function recompute(){ CUR=applyFilters(); renderKPIs(CUR); renderActive(); }

/* ============================================================ CSV (filtered) */
function exportCSV(){
  const f=CUR;
  const cols=['date','ticket','tech','client','product','vehicle','issue','resolution','status','serial','notes'];
  const head=['Date','Ticket #','Tech','Client','Product','Vehicle','Issue','Resolution','Status','Serial','Notes'];
  const q=v=>{v=String(v==null?'':v);return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;};
  const lines=[head.join(',')];
  for(const t of f)lines.push(cols.map(c=>q(c==='date'?t.dateStr:t[c])).join(','));
  const blob=new Blob([lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`zz2-tickets-${new Date().toISOString().slice(0,10)}.csv`;
  document.body.appendChild(a);a.click();a.remove();toast(`Exported ${f.length} tickets`);
}

/* ============================================================ SHELL */
function buildShell(){
  const techs=[...new Set(ALL.map(t=>t.tech).filter(Boolean))].sort();
  const kits=[...new Set(ALL.map(t=>t.product).filter(Boolean))].sort();
  const statuses=['resolved','open','nosol','rma','destroy','distrib','woe','other'].filter(k=>ALL.some(t=>t.canon===k));
  const opt=(arr,lbl)=>arr.map(v=>`<option value="${esc(v)}">${esc(lbl?lbl(v):v)}</option>`).join('');
  $('#app').innerHTML=`
  <div class="wrap">
    <div class="filters">
      <div class="field"><label>From</label><input type="date" id="f-from"></div>
      <div class="field"><label>To</label><input type="date" id="f-to"></div>
      <div class="field"><label>Technician</label><select id="f-tech"><option value="">All techs</option>${opt(techs)}</select></div>
      <div class="field"><label>Kit / Product</label><select id="f-kit"><option value="">All kits</option>${opt(kits)}</select></div>
      <div class="field"><label>Status</label><select id="f-status"><option value="">All statuses</option>${opt(statuses,k=>STATUS_LABEL[k])}</select></div>
      <div class="grow"></div>
      <div style="display:flex;gap:8px"><button class="btn" id="f-clear">Clear</button><button class="btn accent" id="f-csv">⤓ Export CSV</button></div>
    </div>

    <div class="kpis" id="kpis"></div>

    <div class="tabs">
      <button class="tab on" data-tab="overview">Overview</button>
      <button class="tab" data-tab="kits">Kits</button>
      <button class="tab" data-tab="techs">Technicians</button>
      <button class="tab" data-tab="accounts">Accounts</button>
      <button class="tab" data-tab="vehicles">Vehicles</button>
      <button class="tab" data-tab="quality">Quality &amp; Text</button>
    </div>

    <div class="tab-panel show" data-tab="overview">
      <div class="grid">
        <div class="card col-12"><h2>Quality trends over time</h2><div class="sub">Monthly resolution, RMA, and no-solution rates &mdash; the heartbeat of the business</div>
          <div class="chart-box" style="height:280px"><canvas id="qtrend"></canvas></div><div class="note" id="qtrend-note" style="margin-top:8px"></div></div>
        <div class="card col-12"><h2>Outcome breakdown</h2><div class="sub">Every ticket's final status &mdash; RMA and no-solution are real cost</div><div id="funnel"></div></div>
        <div class="card col-8"><div class="head-row"><div><h2>Ticket volume over time</h2></div>
          <div class="seg" id="ts-seg"><button data-m="day">Day</button><button data-m="week">Week</button><button data-m="month" class="on">Month</button></div></div>
          <div class="chart-box tall"><canvas id="ts"></canvas></div></div>
        <div class="card col-4"><h2>Status breakdown</h2><div class="sub">Share of each outcome</div><div class="chart-box short"><canvas id="status"></canvas></div></div>
        <div class="card col-4"><h2>By day of week</h2><div class="chart-box short"><canvas id="dow"></canvas></div></div>
        <div class="card col-8"><h2>Open ticket aging</h2><div class="sub">How long open tickets have waited</div><div class="chart-box short"><canvas id="age"></canvas></div><div class="note" id="aging-note" style="margin-top:8px"></div></div>
      </div>
    </div>

    <div class="tab-panel" data-tab="kits">
      <div class="grid">
        <div class="card col-6"><h2>Ticket volume by kit</h2><div class="sub">Top 14</div><div class="chart-box tall"><canvas id="kit-vol"></canvas></div></div>
        <div class="card col-6"><h2>Kit trouble score</h2><div class="sub">Weighted by severity (No-sol·1, RMA·2, Destroy·3, open·0.5) → 0 best, 100 worst. Min 3 tickets.</div><div class="chart-box tall"><canvas id="kit-score"></canvas></div></div>
        <div class="card col-12"><h2>Kit Pareto &mdash; where volume concentrates</h2><div class="sub">Bars = tickets, line = cumulative %. Click any bar to drill into that kit.</div><div class="chart-box tall"><canvas id="kit-pareto"></canvas></div><div class="note" id="kit-pareto-note" style="margin-top:8px"></div></div>
        <div class="card col-12"><h2>Kit reliability</h2><div class="sub">Sortable — click any column header</div><div class="tbl-scroll" id="kit-table"></div></div>
        <div class="card col-6"><h2>Kits trending worse</h2><div class="sub">Failure rate, last 90 days vs the 90 before</div><div id="kit-rising"></div></div>
        <div class="card col-6"><h2>Status mix by kit</h2><div class="sub">Darker = larger share of that kit's tickets</div><div class="tbl-scroll" id="kit-heat"></div></div>
        <div class="card col-12"><h2>Kit signatures</h2><div class="sub">The usual issue, fix, and vehicle for each kit</div><div class="tbl-scroll" id="kit-sig"></div></div>
      </div>
    </div>

    <div class="tab-panel" data-tab="techs">
      <div class="grid">
        <div class="card col-5"><h2>Volume by technician</h2><div class="chart-box tall"><canvas id="tech-vol"></canvas></div></div>
        <div class="card col-7"><h2>Technician leaderboard</h2><div class="sub">Resolution rate and most-handled kit</div><div class="tbl-scroll" id="tech-table"></div></div>
      </div>
    </div>

    <div class="tab-panel" data-tab="accounts">
      <div class="grid">
        <div class="card col-7"><h2>Top clients</h2><div class="sub" id="cli-note">&mdash;</div><div class="tbl-scroll" id="cli-table"></div></div>
        <div class="card col-5"><h2>Chronic cases</h2><div class="sub">Same client + kit seen 3+ times</div><div class="tbl-scroll" id="chronic"></div></div>
        <div class="card col-7"><h2>New vs returning clients</h2><div class="sub" id="acct-seg">&mdash;</div><div class="chart-box tall"><canvas id="acct-trend"></canvas></div></div>
        <div class="card col-5"><h2>Accounts most at risk</h2><div class="sub">Highest RMA / no-solution rate (min 4 tickets)</div><div class="tbl-scroll" id="acct-risk"></div></div>
      </div>
    </div>

    <div class="tab-panel" data-tab="vehicles">
      <div class="grid">
        <div class="card col-5"><h2>Tickets by make</h2><div class="sub">Make inferred from the vehicle field &middot; click a bar to drill</div><div class="chart-box tall"><canvas id="veh-make"></canvas></div></div>
        <div class="card col-7"><h2>Make reliability</h2><div class="sub">Which brands fight back (MDX and RDX both count as Acura but stay distinct models below)</div><div class="tbl-scroll" id="veh-make-tbl"></div></div>
        <div class="card col-7"><h2>Failure rate by model year</h2><div class="sub">Are newer vehicles harder? (years with 5+ tickets)</div><div class="chart-box tall"><canvas id="veh-year"></canvas></div></div>
        <div class="card col-5"><h2>Top vehicles</h2><div class="chart-box tall"><canvas id="veh-vol"></canvas></div></div>
        <div class="card col-12"><h2>Kit × make failure matrix</h2><div class="sub">Fail count per cell, shaded by failure rate &mdash; the combos to document first</div><div class="tbl-scroll" id="veh-matrix"></div></div>
        <div class="card col-12"><h2>Vehicle reliability</h2><div class="sub">Specific vehicles driving the most failures (min 3 tickets)</div><div class="tbl-scroll" id="veh-table"></div></div>
      </div>
    </div>

    <div class="tab-panel" data-tab="quality">
      <div class="grid">
        <div class="card col-6"><h2>Common issue keywords</h2><div id="kw-issue" style="margin-top:6px"></div></div>
        <div class="card col-6"><h2>Common resolution keywords</h2><div id="kw-res" style="margin-top:6px"></div></div>
        <div class="card col-8"><h2>Anomaly flags</h2><div class="sub">Volume spikes and kit-failure surges</div><div id="anomalies" style="margin-top:6px"></div></div>
        <div class="card col-4"><h2>Operational</h2><div class="note" id="dup-note" style="margin:8px 0"></div>
          <details class="defs"><summary>How metrics are defined</summary><ul>
            <li><b>Open</b>=blank/waiting status ≤14 days; older waiting auto-counts resolved.</li>
            <li><b>Trouble score</b>=(No-sol·1+RMA·2+Destroy·3+open·0.5)÷total, 0–100.</li>
            <li><b>Fail load</b>=No-solution+RMA+Field-Destroy.</li>
            <li><b>Chronic case</b>=client+kit with 3+ tickets.</li>
            <li><b>Consolidation</b>=typos merged, 2-digit years expanded; different years stay separate.</li>
          </ul></details></div>
        <div class="card col-12"><h2>Data quality &amp; consolidation</h2><div id="dataquality" style="margin-top:6px"></div></div>
      </div>
    </div>

    <div id="drill" class="drill-overlay"><div class="drill-modal">
      <div class="drill-head"><div><h2 id="drill-title"></h2><div class="sub" id="drill-sub"></div></div><button class="drill-x" id="drill-x" aria-label="Close">&times;</button></div>
      <div class="tbl-scroll" id="drill-body" style="max-height:60vh"></div>
    </div></div>
  </div>`;

  const sync=()=>{F.from=$('#f-from').value;F.to=$('#f-to').value;F.tech=$('#f-tech').value;F.kit=$('#f-kit').value;F.status=$('#f-status').value;recompute();};
  ['f-from','f-to','f-tech','f-kit','f-status'].forEach(id=>$('#'+id).addEventListener('change',sync));
  $('#f-clear').onclick=()=>{['f-from','f-to','f-tech','f-kit','f-status'].forEach(id=>$('#'+id).value='');sync();};
  $('#f-csv').onclick=exportCSV;
  $$('.tab').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
  $$('#ts-seg button').forEach(b=>b.onclick=()=>{$$('#ts-seg button').forEach(x=>x.classList.remove('on'));b.classList.add('on');tsMode=b.dataset.m;if(activeTab==='overview')renderOverview(CUR);});
  wireDrill();
  let rz=null; window.addEventListener('resize',()=>{clearTimeout(rz);rz=setTimeout(renderActive,200);});
}

/* ============================================================ LOAD */
function _normFromSummary(s){ s=s||{}; return {merges:s.merges||[], before:s.before||0, after:s.after||0, map:new Map()}; }
async function loadData(){
  let payload=null,lastErr=null;
  for(const url of DATA_URLS){
    try{const r=await fetch(url+'?_='+Date.now(),{cache:'no-store',headers:authHeaders()});
      if(r.ok){payload=await r.json();break;} lastErr='HTTP '+r.status;}catch(e){lastErr=e.message;}
  }
  if(!payload)throw new Error(lastErr||'analytics not found');
  if(payload.building){ const e=new Error('Analytics view is building on the server'); e.building=true; throw e; }
  META.generated=payload.generated||payload.updated||payload.source_generated||null;
  if(payload&&payload.precomputed&&Array.isArray(payload.rows)){
    // Server already normalized + consolidated the store — just rehydrate Date
    // objects and recompute age (kept fresh client-side).
    ALL=payload.rows.map(r=>{
      const date=parseDate(r.date);
      return { date, dateStr:date?date.toISOString().slice(0,10):(r.date||''),
        ageDays:date?Math.round((today-date)/DAY):null,
        ticket:r.ticket||'', tech:r.tech||'', client:r.client||'', product:r.product||'',
        vehicle:r.vehicle||'', issue:r.issue||'', resolution:r.resolution||'',
        status:r.status||'', canon:r.canon||'open' };
    });
    const N=payload.norm||{};
    NORM={ vehicle:_normFromSummary(N.vehicle), client:_normFromSummary(N.client), tech:_normFromSummary(N.tech) };
  }else{
    // Legacy/raw store (pre-precompute server, or direct file) — aggregate here.
    const rows=Array.isArray(payload)?payload:(payload.tickets||payload.rows||[]);
    ALL=normalize(rows).filter(t=>t.ticket||t.date||t.client);
    NORM={ vehicle:consolidate(ALL.map(t=>t.vehicleRaw),{expandYears:true,fuzzy:true}),
           client:consolidate(ALL.map(t=>t.clientRaw),{expandYears:false,fuzzy:true}),
           tech:consolidate(ALL.map(t=>t.techRaw),{expandYears:false,fuzzy:false}) };
    for(const t of ALL){ t.vehicle=NORM.vehicle.map.get(t.vehicleRaw)||t.vehicle; t.client=NORM.client.map.get(t.clientRaw)||t.client; t.tech=NORM.tech.map.get(t.techRaw)||t.tech; }
  }
  if(!ALL.length)throw new Error('store loaded but contained no tickets');
  // make/year/model are cheap parses of the (now canonical) vehicle — derive for
  // BOTH paths so the Vehicles tab is identical whether or not it was precomputed.
  for(const t of ALL){ t.make=vehMake(t.vehicle); t.year=vehYear(t.vehicle); t.model=vehModel(t.vehicle); }
}
function setMeta(){const g=META.generated?new Date(META.generated):null;
  $('#meta').innerHTML=`<b>${fmt(ALL.length)}</b> tickets`+(g&&!isNaN(g)?` · as of ${g.toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}`:'');}
function showError(msg){$('#app').innerHTML=`<div class="errstate"><div class="box"><h3>Couldn't load analytics</h3><p class="note">${esc(msg)}</p><p class="note">The store is built nightly. Click <b>Rebuild</b>, or check <code>${esc(REBUILD_URL)}</code>.</p></div></div>`;}

function showBuilding(n){ $('#app').innerHTML=`<div class="loading"><div><div class="spinner"></div><div class="note">Building analytics on the server…<br>first load after a restart can take up to a minute.${n>1?` (checking… ${n})`:''}</div></div></div>`; }
async function loadDataRetry(maxTries){
  maxTries=maxTries||40;
  for(let i=0;i<maxTries;i++){
    try{ await loadData(); return; }
    catch(e){ if(e&&e.building){ showBuilding(i+1); await new Promise(r=>setTimeout(r,3000)); continue; } throw e; }
  }
  throw new Error('Analytics is taking too long to build on the server. Click Rebuild, or check server load.');
}
async function init(){ try{ await loadDataRetry(); buildShell(); setMeta(); recompute(); }catch(e){ console.error(e); showError(e.message); } }

$('#btn-reload').onclick=async()=>{toast('Reloading…');try{await loadDataRetry();buildShell();setMeta();recompute();toast('Reloaded');}catch(e){showError(e.message);toast('Reload failed',true);}};
$('#btn-rebuild').onclick=async()=>{toast('Rebuilding on server…');try{const r=await fetch(REBUILD_URL+'?_='+Date.now(),{cache:'no-store',headers:authHeaders()});if(!r.ok)throw new Error('HTTP '+r.status);toast('Rebuild requested — reloading');setTimeout(async()=>{try{await loadDataRetry();buildShell();setMeta();recompute();toast('Updated');}catch(e){showError(e.message);toast('Reload failed',true);}},1400);}catch(e){toast('Rebuild unavailable ('+e.message+'). Nightly job still runs.',true);}};

(async function guard(){
  if(!TOKEN){location.href='/login.html';return;}
  try{const r=await ZZ2.verifySession(TOKEN);
    const d=await r.json(); if(!d.ok){localStorage.removeItem(AUTH_KEY);location.href='/login.html';return;}
    // Permission-driven: admins/super_admins have `analytics` implicitly; a tech
    // who's been granted it gets in too. Everyone else goes back to the tool.
    if(!(d.permissions||[]).includes('analytics')){location.href='/index.html';return;} if(window.zz2BuildNav) zz2BuildNav(d.permissions||[]); init();
  }catch(e){location.href='/login.html';}
})();
