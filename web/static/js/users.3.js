const SERVER_URL = window.location.origin;
const AUTH_KEY = 'zz2_auth_token';
let TOKEN = localStorage.getItem(AUTH_KEY) || '';
const HJ = () => ({ 'Authorization':'Bearer '+TOKEN, 'Content-Type':'application/json' });
const H  = () => ({ 'Authorization':'Bearer '+TOKEN });
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(m,err){const t=$('#toast');t.textContent=m;t.className='show'+(err?' err':'');clearTimeout(t._h);t._h=setTimeout(()=>t.className='',3000);}

let CAN = { users:false, roles:false };
let TAB = 'users';
let U = { users:[], roles:[], assignable:[], catalog:[], actorPerms:[], actorUser:'' };  // users tab data
let R = { roles:[], catalog:[], actorPerms:[], maxRank:1 };                               // roles tab data

function rankClass(rank){ return rank>=100?'rank-hi':(rank>=50?'rank-mid':''); }
function permLabel(k){ const c=(U.catalog.length?U.catalog:R.catalog).find(p=>p.key===k); return c?c.label:k; }
function rolePermsOf(key){ const r=U.roles.find(x=>x.key===key); return r?(r.permissions||[]):[]; }
function roleLabelOf(key){ const r=(U.roles.length?U.roles:R.roles).find(x=>x.key===key); return r?r.label:key; }

/* ---------------- shell ---------------- */
function renderShell(){
  const tabs =
    (CAN.users ? '<button class="tab'+(TAB==='users'?' active':'')+'" data-tab="users">Users</button>' : '') +
    (CAN.roles ? '<button class="tab'+(TAB==='roles'?' active':'')+'" data-tab="roles">Roles</button>' : '');
  $('#app').innerHTML = '<div class="wrap"><div class="tabs">'+tabs+'</div><div id="tabview"></div></div>';
  document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{ TAB=b.dataset.tab; renderShell(); loadTab(); });
}
function errBox(msg){ return '<div class="errstate"><div><h3 style="margin:0 0 8px">Unavailable</h3><p style="color:var(--muted);margin:0">'+esc(msg)+'</p></div></div>'; }

async function loadTab(){
  if (TAB==='users') await loadUsers(); else await loadRoles();
}

/* ================= USERS ================= */
async function loadUsers(){
  $('#tabview').innerHTML = '<div class="loading"><div><div class="spinner"></div></div></div>';
  const r = await fetch(SERVER_URL+'/api/users?_='+Date.now(),{cache:'no-store',headers:H()});
  if(r.status===403){ $('#tabview').innerHTML=errBox("You don't have the Manage users permission."); return; }
  if(!r.ok){ $('#tabview').innerHTML=errBox('HTTP '+r.status); return; }
  const d = await r.json();
  U.users=d.users||[]; U.roles=d.roles||[]; U.assignable=d.assignable_roles||[];
  U.catalog=d.permission_catalog||[]; U.actorPerms=d.actor_perms||[]; U.actorUser=(d.actor_username||'').toLowerCase();
  renderUsers();
}
function userPermChips(u){
  const fromRole = rolePermsOf(u.role);
  const extras = (u.permissions||[]);
  let chips = fromRole.map(k=>'<span class="chip role-perm" title="From role">'+esc(permLabel(k))+'</span>');
  chips = chips.concat(extras.filter(k=>fromRole.indexOf(k)<0).map(k=>'<span class="chip" title="Extra grant">'+esc(permLabel(k))+'</span>'));
  if(!chips.length) return '<span class="u-sub">—</span>';
  if(chips.length>4) return chips.slice(0,4).join('')+'<span class="chip more">+'+(chips.length-4)+'</span>';
  return chips.join('');
}
function renderUsers(){
  const rows = U.users.map(u=>{
    const can=u.manageable, self=u.is_self;
    const selfTag=self?'<span class="self-tag">you</span>':'';
    const ed=(!can&&!self)?'disabled':'', pd=(!can&&!self)?'disabled':'', dd=(!can||self)?'disabled':'';
    const lock=(!can&&!self)?'<div class="locktip">Outranks you</div>':'';
    return '<tr class="'+(self?'row-self':'')+'">'+
      '<td><div class="u-name">'+esc(u.display||u.username)+selfTag+'</div><div class="u-sub">@'+esc(u.username)+'</div></td>'+
      '<td><span class="role '+rankClass(u.role_rank)+'">'+esc(u.role_label||u.role)+'</span></td>'+
      '<td class="col-status">'+(u.active?'<span class="pill on">Active</span>':'<span class="pill off">Inactive</span>')+'</td>'+
      '<td class="col-perms">'+userPermChips(u)+'</td>'+
      '<td><div class="actions">'+
        '<button class="btn sm" '+ed+' data-act="edit" data-u="'+esc(u.username)+'">Edit</button>'+
        '<button class="btn sm" '+pd+' data-act="pw" data-u="'+esc(u.username)+'">Password</button>'+
        '<button class="btn sm danger" '+dd+' data-act="del" data-u="'+esc(u.username)+'">Delete</button>'+
      '</div>'+lock+'</td></tr>';
  }).join('');
  const canAdd=U.assignable.length>0;
  $('#tabview').innerHTML =
    '<div class="page-head"><div><h1>Users</h1><p>'+U.users.length+' account'+(U.users.length===1?'':'s')+'</p></div>'+
    (canAdd?'<button class="btn accent" id="btn-add-user">+ Add user</button>':'')+'</div>'+
    '<div class="panel"><table><thead><tr><th>User</th><th>Role</th><th class="col-status">Status</th><th class="col-perms">Permissions</th><th style="text-align:right">Actions</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  const a=$('#btn-add-user'); if(a) a.onclick=()=>openUserModal(null);
  document.querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>{
    const u=U.users.find(x=>x.username===b.dataset.u); if(!u) return;
    if(b.dataset.act==='edit') openUserModal(u);
    else if(b.dataset.act==='pw') openPwModal(u);
    else if(b.dataset.act==='del') deleteUser(u);
  });
}

let editingUser=null;
function buildUserPerms(roleKey, extras){
  extras = extras||[];
  const fromRole = rolePermsOf(roleKey);
  $('#u-perm-list').innerHTML = U.catalog.map(p=>{
    const inRole = fromRole.indexOf(p.key)>=0;
    const canGrant = U.actorPerms.indexOf(p.key)>=0;
    const checked = inRole || extras.indexOf(p.key)>=0;
    const disabled = inRole || !canGrant;
    const cls = 'perm '+(disabled?'disabled':'enabled');
    const tag = inRole?'<span class="frm">from role</span>':'';
    return '<label class="'+cls+'"><input type="checkbox" value="'+esc(p.key)+'"'+(checked?' checked':'')+(disabled?' disabled':'')+'>'+
      '<span><span class="pl">'+esc(p.label)+tag+'</span><br><span class="pd">'+esc(p.desc||'')+'</span></span></label>';
  }).join('');
}
function openUserModal(u){
  editingUser=u; const isCreate=!u;
  $('#user-modal-title').textContent=isCreate?'Add user':('Edit '+(u.display||u.username));
  $('#user-msg').textContent='';
  let opts=U.assignable.slice();
  if(u && opts.indexOf(u.role)<0) opts=[u.role].concat(opts);
  $('#f-role').innerHTML=opts.map(k=>'<option value="'+esc(k)+'">'+esc(roleLabelOf(k))+'</option>').join('');
  $('#f-display').value=u?(u.display||''):'';
  $('#f-username').value=u?u.username:'';
  $('#f-role').value=u?u.role:(U.assignable[0]||'');
  const lockRole=u&&u.is_self;
  $('#f-role').disabled=!!lockRole;
  $('#role-hint').textContent=lockRole?"You can't change your own role.":'';
  $('#field-password').style.display=isCreate?'':'none'; $('#f-password').value='';
  if(u&&!u.is_self){ $('#field-active').style.display=''; $('#f-active').checked=!!u.active; }
  else $('#field-active').style.display='none';
  buildUserPerms($('#f-role').value, u?u.permissions:[]);
  $('#f-role').onchange=()=>buildUserPerms($('#f-role').value, collectUserExtras());
  $('#user-overlay').classList.add('show'); setTimeout(()=>$('#f-display').focus(),50);
}
function collectUserExtras(){
  // checked + enabled checkboxes = extras (role-derived are disabled, excluded)
  return Array.prototype.filter.call(document.querySelectorAll('#u-perm-list input'),i=>i.checked&&!i.disabled).map(i=>i.value);
}
function closeUserModal(){ $('#user-overlay').classList.remove('show'); editingUser=null; }
async function saveUser(){
  const msg=$('#user-msg'); msg.className='modal-msg';
  const display=$('#f-display').value.trim();
  const username=$('#f-username').value.trim().toLowerCase();
  const role=$('#f-role').value;
  const perms=collectUserExtras();
  if(!display){ msg.className='modal-msg err'; msg.textContent='Display name is required.'; return; }
  if(!/^[a-z0-9._-]{2,32}$/.test(username)){ msg.className='modal-msg err'; msg.textContent='Username must be 2-32 chars: letters, numbers, . _ -'; return; }
  $('#user-save').disabled=true;
  try{
    let r;
    if(!editingUser){
      const pw=$('#f-password').value;
      if(pw.length<6){ msg.className='modal-msg err'; msg.textContent='Password must be at least 6 characters.'; $('#user-save').disabled=false; return; }
      r=await fetch(SERVER_URL+'/api/users/create',{method:'POST',headers:HJ(),body:JSON.stringify({username,display,password:pw,role,permissions:perms})});
    }else{
      const body={username:editingUser.username,display,permissions:perms};
      if(!editingUser.is_self){ body.role=role; body.active=$('#f-active').checked; }
      if(username!==editingUser.username) body.new_username=username;
      r=await fetch(SERVER_URL+'/api/users/update',{method:'POST',headers:HJ(),body:JSON.stringify(body)});
    }
    const d=await r.json();
    if(!r.ok||!d.ok){ msg.className='modal-msg err'; msg.textContent=d.error||('HTTP '+r.status); $('#user-save').disabled=false; return; }
    closeUserModal(); toast(editingUser?'User updated':'User created'); await loadUsers();
  }catch(e){ msg.className='modal-msg err'; msg.textContent='Could not reach server.'; }
  $('#user-save').disabled=false;
}

let pwUser=null;
function openPwModal(u){ pwUser=u; $('#pw-title').textContent=u.is_self?'Change your password':('Set password · '+(u.display||u.username)); $('#pw-1').value='';$('#pw-2').value='';$('#pw-msg').textContent=''; $('#pw-overlay').classList.add('show'); setTimeout(()=>$('#pw-1').focus(),50); }
function closePwModal(){ $('#pw-overlay').classList.remove('show'); pwUser=null; }
async function savePw(){
  const msg=$('#pw-msg'); msg.className='modal-msg';
  const a=$('#pw-1').value,b=$('#pw-2').value;
  if(a.length<6){ msg.className='modal-msg err'; msg.textContent='Password must be at least 6 characters.'; return; }
  if(a!==b){ msg.className='modal-msg err'; msg.textContent='Passwords do not match.'; return; }
  $('#pw-save').disabled=true;
  try{ const r=await fetch(SERVER_URL+'/api/users/password',{method:'POST',headers:HJ(),body:JSON.stringify({username:pwUser.username,password:a})});
    const d=await r.json();
    if(!r.ok||!d.ok){ msg.className='modal-msg err'; msg.textContent=d.error||('HTTP '+r.status); $('#pw-save').disabled=false; return; }
    closePwModal(); toast('Password updated');
  }catch(e){ msg.className='modal-msg err'; msg.textContent='Could not reach server.'; }
  $('#pw-save').disabled=false;
}
async function deleteUser(u){
  if(!confirm('Delete user "'+(u.display||u.username)+'" (@'+u.username+')? This cannot be undone.')) return;
  try{ const r=await fetch(SERVER_URL+'/api/users/delete',{method:'POST',headers:HJ(),body:JSON.stringify({username:u.username})});
    const d=await r.json(); if(!r.ok||!d.ok){ toast(d.error||('HTTP '+r.status),true); return; }
    toast('User deleted'); await loadUsers();
  }catch(e){ toast('Could not reach server.',true); }
}

/* ================= ROLES ================= */
async function loadRoles(){
  $('#tabview').innerHTML='<div class="loading"><div><div class="spinner"></div></div></div>';
  const r=await fetch(SERVER_URL+'/api/roles?_='+Date.now(),{cache:'no-store',headers:H()});
  if(r.status===403){ $('#tabview').innerHTML=errBox("You don't have the Manage roles permission."); return; }
  if(!r.ok){ $('#tabview').innerHTML=errBox('HTTP '+r.status); return; }
  const d=await r.json();
  R.roles=d.roles||[]; R.catalog=d.permission_catalog||[]; R.actorPerms=d.actor_perms||[]; R.maxRank=d.max_assignable_rank||1;
  renderRoles();
}
function rolePermChips(r){
  if(r.root) return '<span class="chip role-perm">Everything</span>';
  const ks=r.permissions||[]; if(!ks.length) return '<span class="u-sub">—</span>';
  let chips=ks.map(k=>'<span class="chip">'+esc((R.catalog.find(p=>p.key===k)||{}).label||k)+'</span>');
  if(chips.length>5) return chips.slice(0,5).join('')+'<span class="chip more">+'+(chips.length-5)+'</span>';
  return chips.join('');
}
function renderRoles(){
  const rows=R.roles.map(r=>{
    const sysTag=r.system?'<span class="sys-tag">built-in</span>':'';
    const ed=r.editable?'':'disabled', dd=r.deletable?'':'disabled';
    const delTip=(r.editable&&!r.system&&r.user_count>0)?'<div class="locktip">'+r.user_count+' user'+(r.user_count===1?'':'s')+'</div>':'';
    return '<tr>'+
      '<td><div class="u-name">'+esc(r.label)+sysTag+'</div><div class="u-sub">'+esc(r.key)+'</div></td>'+
      '<td class="count-cell">'+r.rank+'</td>'+
      '<td class="col-perms">'+rolePermChips(r)+'</td>'+
      '<td class="count-cell">'+r.user_count+'</td>'+
      '<td><div class="actions">'+
        '<button class="btn sm" '+ed+' data-ract="edit" data-k="'+esc(r.key)+'">Edit</button>'+
        '<button class="btn sm danger" '+dd+' data-ract="del" data-k="'+esc(r.key)+'">Delete</button>'+
      '</div>'+delTip+'</td></tr>';
  }).join('');
  $('#tabview').innerHTML=
    '<div class="page-head"><div><h1>Roles</h1><p>'+R.roles.length+' role'+(R.roles.length===1?'':'s')+
      ' · higher level = more authority (you can manage up to level '+R.maxRank+')</p></div>'+
      '<button class="btn accent" id="btn-add-role">+ New role</button></div>'+
    '<div class="panel"><table><thead><tr><th>Role</th><th>Level</th><th class="col-perms">Permissions</th><th>Users</th><th style="text-align:right">Actions</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  $('#btn-add-role').onclick=()=>openRoleModal(null);
  document.querySelectorAll('[data-ract]').forEach(b=>b.onclick=()=>{
    const r=R.roles.find(x=>x.key===b.dataset.k); if(!r) return;
    if(b.dataset.ract==='edit') openRoleModal(r); else deleteRole(r);
  });
}

let editingRole=null;
function buildRolePerms(checked){
  checked=checked||[];
  $('#r-perm-list').innerHTML=R.catalog.map(p=>{
    const canGrant=R.actorPerms.indexOf(p.key)>=0;
    const cls='perm '+(canGrant?'enabled':'disabled');
    const dis=canGrant?'':' disabled';
    const tag=canGrant?'':'<span class="frm">not yours</span>';
    return '<label class="'+cls+'"><input type="checkbox" value="'+esc(p.key)+'"'+(checked.indexOf(p.key)>=0?' checked':'')+dis+'>'+
      '<span><span class="pl">'+esc(p.label)+tag+'</span><br><span class="pd">'+esc(p.desc||'')+'</span></span></label>';
  }).join('');
}
function openRoleModal(r){
  editingRole=r; const isCreate=!r;
  $('#role-modal-title').textContent=isCreate?'New role':('Edit '+r.label);
  $('#role-msg').textContent='';
  $('#r-label').value=r?r.label:'';
  $('#r-key-field').style.display=isCreate?'':'none';
  $('#r-key').value=r?r.key:'';
  $('#r-rank').max=R.maxRank;
  $('#r-rank').value=r?r.rank:Math.min(20,R.maxRank);
  $('#r-rank').disabled=!!(r&&r.system);          // built-in roles keep their level
  $('#rank-hint').textContent=(r&&r.system)?'Fixed':('1–'+R.maxRank);
  buildRolePerms(r?r.permissions:[]);
  $('#role-overlay').classList.add('show'); setTimeout(()=>$('#r-label').focus(),50);
}
function closeRoleModal(){ $('#role-overlay').classList.remove('show'); editingRole=null; }
async function saveRole(){
  const msg=$('#role-msg'); msg.className='modal-msg';
  const label=$('#r-label').value.trim();
  const rank=parseInt($('#r-rank').value,10);
  const perms=Array.prototype.filter.call(document.querySelectorAll('#r-perm-list input'),i=>i.checked&&!i.disabled).map(i=>i.value);
  if(!label){ msg.className='modal-msg err'; msg.textContent='Role name is required.'; return; }
  if(!(rank>=1&&rank<=R.maxRank)){ msg.className='modal-msg err'; msg.textContent='Level must be between 1 and '+R.maxRank+'.'; return; }
  $('#role-save').disabled=true;
  try{
    let r;
    if(!editingRole){
      const key=$('#r-key').value.trim().toLowerCase();
      if(!/^[a-z][a-z0-9_]{1,31}$/.test(key)){ msg.className='modal-msg err'; msg.textContent='Role ID must start with a letter; letters, numbers, underscore.'; $('#role-save').disabled=false; return; }
      r=await fetch(SERVER_URL+'/api/roles/create',{method:'POST',headers:HJ(),body:JSON.stringify({key,label,rank,permissions:perms})});
    }else{
      const body={key:editingRole.key,label,permissions:perms};
      if(!editingRole.system) body.rank=rank;
      r=await fetch(SERVER_URL+'/api/roles/update',{method:'POST',headers:HJ(),body:JSON.stringify(body)});
    }
    const d=await r.json();
    if(!r.ok||!d.ok){ msg.className='modal-msg err'; msg.textContent=d.error||('HTTP '+r.status); $('#role-save').disabled=false; return; }
    closeRoleModal(); toast(editingRole?'Role updated':'Role created'); await loadRoles();
  }catch(e){ msg.className='modal-msg err'; msg.textContent='Could not reach server.'; }
  $('#role-save').disabled=false;
}
async function deleteRole(r){
  if(!confirm('Delete the role "'+r.label+'"? This cannot be undone.')) return;
  try{ const res=await fetch(SERVER_URL+'/api/roles/delete',{method:'POST',headers:HJ(),body:JSON.stringify({key:r.key})});
    const d=await res.json(); if(!res.ok||!d.ok){ toast(d.error||('HTTP '+res.status),true); return; }
    toast('Role deleted'); await loadRoles();
  }catch(e){ toast('Could not reach server.',true); }
}

/* ---------------- wire + guard ---------------- */
$('#user-cancel').onclick=closeUserModal; $('#user-save').onclick=saveUser;
$('#pw-cancel').onclick=closePwModal; $('#pw-save').onclick=savePw;
$('#role-cancel').onclick=closeRoleModal; $('#role-save').onclick=saveRole;
$('#user-overlay').onclick=e=>{ if(e.target===$('#user-overlay')) closeUserModal(); };
$('#pw-overlay').onclick=e=>{ if(e.target===$('#pw-overlay')) closePwModal(); };
$('#role-overlay').onclick=e=>{ if(e.target===$('#role-overlay')) closeRoleModal(); };
document.addEventListener('keydown',e=>{ if(e.key==='Escape'){ closeUserModal(); closePwModal(); closeRoleModal(); } });
$('#btn-logout').onclick=()=>{ ZZ2.logoutRequest().catch(()=>{}); localStorage.removeItem(AUTH_KEY); location.href='/login.html'; };

(async function guard(){
  if(!TOKEN){ location.href='/login.html'; return; }
  try{
    const r=await ZZ2.verifySession(TOKEN);
    const d=await r.json();
    if(!d.ok){ localStorage.removeItem(AUTH_KEY); location.href='/login.html'; return; }
    const perms=d.permissions||[];
    if(window.zz2BuildNav) zz2BuildNav(perms);
    CAN.users=perms.indexOf('manage_users')>=0;
    CAN.roles=perms.indexOf('manage_roles')>=0;
    if(!CAN.users&&!CAN.roles){ location.href='/index.html'; return; }
    $('#who').innerHTML='<b>'+esc(d.display)+'</b>'+esc(d.role.replace('_',' '));
    TAB = CAN.users?'users':'roles';
    renderShell(); await loadTab();
  }catch(e){ $('#app').innerHTML=errBox('Could not reach the server.'); }
})();
