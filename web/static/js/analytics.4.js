/* analytics: make Chart.js colors theme-aware (additive); light values match originals exactly */
(function(){
  function dark(){return document.documentElement.getAttribute('data-theme')==='dark';}
  function applyChartTheme(){
    var d=dark();
    try{ if(typeof GRID!=='undefined'&&GRID) GRID.color=d?'#2a313a':'#eef1f4'; }catch(e){}
    try{ if(typeof TICK!=='undefined'&&TICK) TICK.color=d?'#aab2bd':'#6b7280'; }catch(e){}
    try{ if(window.Chart&&Chart.defaults) Chart.defaults.color=d?'#cfd4da':'#666666'; }catch(e){}
  }
  applyChartTheme();
  window.zz2OnThemeChange=function(){
    applyChartTheme();
    try{ if(typeof ALL!=='undefined'&&ALL&&ALL.length&&typeof renderActive==='function') renderActive(); }catch(e){}
  };
})();
