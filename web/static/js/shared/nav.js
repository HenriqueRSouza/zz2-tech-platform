(function(){
  function curPage(){ var f=(location.pathname.split('/').pop()||'').toLowerCase(); if(!f) return 'index'; return f.replace('.html',''); }
  window.zz2NavToggle=function(open){
    var nav=document.getElementById('zz2Nav'), ov=document.getElementById('zz2NavOverlay');
    if(!nav) return;
    var show=(open===undefined)?!nav.classList.contains('open'):!!open;
    nav.classList.toggle('open',show); if(ov) ov.classList.toggle('show',show);
    nav.setAttribute('aria-hidden',show?'false':'true');
  };
  window.zz2BuildNav=function(perms){
    perms=perms||[];
    function has(spec){ if(!spec) return true; return spec.split('|').some(function(p){return perms.indexOf(p)>=0;}); }
    var cp=curPage(), links=document.querySelectorAll('#zz2Nav .zz2-nav-link');
    for(var i=0;i<links.length;i++){
      var el=links[i], perm=el.getAttribute('data-perm'), page=el.getAttribute('data-page');
      el.style.display=has(perm)?'flex':'none';
      if(page===cp){ el.classList.add('active'); el.removeAttribute('href'); }
      else el.classList.remove('active');
    }
  };
  document.addEventListener('keydown',function(e){ if(e.key==='Escape'||e.key==='Esc') zz2NavToggle(false); });
  function wire(){ var ov=document.getElementById('zz2NavOverlay'), x=document.getElementById('zz2NavClose');
    if(ov) ov.addEventListener('click',function(){zz2NavToggle(false);});
    if(x) x.addEventListener('click',function(){zz2NavToggle(false);}); }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',wire); else wire();
})();
