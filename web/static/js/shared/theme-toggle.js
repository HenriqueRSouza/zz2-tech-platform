/* ZZ2 theme toggle - button shows the CURRENT mode; tooltip names the action */
(function(){
  function isDark(){return document.documentElement.getAttribute('data-theme')==='dark';}
  function syncBtn(){var b=document.getElementById('themeToggle');if(!b)return;var d=isDark();
    b.textContent=d?'\ud83c\udf19':'\u2600\ufe0f';b.title=d?'Switch to light mode':'Switch to dark mode';}
  window.zz2ToggleTheme=function(){var next=isDark()?'light':'dark';
    if(next==='dark')document.documentElement.setAttribute('data-theme','dark');
    else document.documentElement.removeAttribute('data-theme');
    try{localStorage.setItem('zz2-theme',next);}catch(e){}
    syncBtn();if(window.zz2OnThemeChange)try{window.zz2OnThemeChange(next);}catch(e){}};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',syncBtn);else syncBtn();
})();
