    const SERVER_URL = window.location.origin;
    const AUTH_KEY   = 'zz2_auth_token';
    const ROLE_KEY   = 'zz2_auth_role';
    const STATS_CACHE_KEY = 'zz2_dash_stats';

    function doLogout() {
      var token = localStorage.getItem(AUTH_KEY);
      ZZ2.logoutRequest().catch(function(){});
      localStorage.removeItem(AUTH_KEY);
      localStorage.removeItem(ROLE_KEY);
      window.location.href = '/login.html';
    }

    window.addEventListener('load', function() {
      var token = localStorage.getItem(AUTH_KEY);
      if (!token) { window.location.href = '/login.html'; return; }

      ZZ2.verifySession(token)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d.ok) { window.location.href = '/login.html'; return; }

        var perms = d.permissions || [];
        function can(p){ return perms.indexOf(p) >= 0; }
        if(window.zz2BuildNav) zz2BuildNav(perms);

        // The dashboard itself requires the `dashboard` permission.
        if (!can('dashboard')) { window.location.href = '/index.html'; return; }

        // Set UI
        document.getElementById('user-name').textContent = d.display;
        document.getElementById('user-role').textContent = (d.role || '').replace('_', ' ');
        document.getElementById('greeting-text').textContent = 'Welcome back, ' + d.display;

        var now = new Date();
        document.getElementById('date-text').textContent = now.toLocaleDateString('en-US', {
          weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
        });

        // Reveal each card by the permission it needs.
        function show(id){ var el = document.getElementById(id); if (el) el.classList.remove('hidden'); }
        if (can('tickets'))         show('card-tickets');
        if (can('analytics'))       show('card-analytics');
        if (can('manage_users') || can('manage_roles')) show('card-users');
        if (can('system_settings')) show('card-settings');

        // Stats need the `dashboard` permission (already required above).
        // Paint cached numbers instantly, then refresh silently in the background.
        renderCachedStats();
        loadStats(token);
        setInterval(function() { loadStats(token); }, 60000);
      })
      .catch(function() { window.location.href = '/login.html'; });
    });

    function applyStats(d) {
      document.getElementById('stat-today').textContent = d.today || 0;
      document.getElementById('stat-week').textContent  = d.this_week || 0;
      document.getElementById('stat-month').textContent = d.this_month || 0;
      document.getElementById('stat-total').textContent = d.total || 0;
    }

    function renderCachedStats() {
      // Show the last known counts immediately so the cards never sit on "—"
      // while the (slow) sheet query runs.
      try {
        var c = JSON.parse(localStorage.getItem(STATS_CACHE_KEY) || 'null');
        if (c) applyStats(c);
      } catch (e) {}
    }

    function _localISO(d) {
      return d.getFullYear() + '-' + ('0' + (d.getMonth()+1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
    }

    function loadStats(token) {
      // Ticket counts come from GET /api/ticket-records/count, which counts in
      // the database without returning rows. The four ranges run in parallel.
      var now = new Date();
      var today = _localISO(now);
      var week = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      week.setDate(week.getDate() - ((week.getDay() + 6) % 7));
      var month = new Date(now.getFullYear(), now.getMonth(), 1);
      var tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate()+1);
      function count(since, before) {
        var params = [];
        if (since) params.push('since=' + encodeURIComponent(since));
        if (before) params.push('before=' + encodeURIComponent(before));
        return fetch(SERVER_URL + '/api/ticket-records/count' + (params.length ? '?' + params.join('&') : ''), {
          cache:'no-store', headers:{'Authorization':'Bearer '+token}
        }).then(function(r){
          if (!r.ok) throw new Error('Ticket count HTTP '+r.status);
          return r.json();
        }).then(function(d){ return (d && d.count) || 0; });
      }
      var upper=_localISO(tomorrow);
      Promise.all([count(today,upper),count(_localISO(week),upper),
        count(_localISO(month),upper),count('','')])
      .then(function(counts) {
        var s = { today:counts[0], this_week:counts[1], this_month:counts[2], total:counts[3] };
        applyStats(s);
        try { localStorage.setItem(STATS_CACHE_KEY, JSON.stringify(s)); } catch (e) {}
      })
      .catch(function(){});
    }
