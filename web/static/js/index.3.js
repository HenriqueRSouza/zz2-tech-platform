      // ── PASTE YOUR PUBLISHED CSV URLS HERE ──────────────────────

      // ── PASTE YOUR PUBLISHED CSV URLS HERE ──────────────────────
      // Same convention already used by kit-editor.html: the API always lives
      // on the same origin the page was loaded from (the NAS in production,
      // localhost/whatever host in local dev), so no hardcoded IP is needed.
      const SERVER_URL = window.location.origin;
      const API_URL    = SERVER_URL + '/api/proxy';
      // Realtime signaling only (presence + broadcast on ephemeral per-ticket
      // channels) -- ticket data itself never touches this project. RLS on the
      // real data tables in this project blocks this key (see the server-side
      // customer-history proxy), and channels here carry no ticket content
      // anyway, only names/ids/"reload" nudges.
      const SUPABASE_URL             = 'https://sctzhnojdcnquwmafyxo.supabase.co';
      const SUPABASE_PUBLISHABLE_KEY = 'sb_publishable_6Usj8W8jWS2dPp5j8eZBRA_0XPaE6oT';
      const _sb = (window.supabase && window.supabase.createClient)
        ? window.supabase.createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY) : null;
      // The Apps Script URL + secret key stay on the server. Every ticket call is
      // routed through the authenticated /api/proxy so the key never reaches the
      // browser.
      function fetchAppConfig(token) { return Promise.resolve(); }
      function _proxyHeaders() {
        return { 'Authorization': 'Bearer ' + (localStorage.getItem(AUTH_TOKEN_KEY) || '') };
      }
      function proxyGet(query) {
        return fetch(API_URL + '?' + query + '&_=' + Date.now(),
                     { cache: 'no-store', headers: _proxyHeaders() });
      }
      function proxyPost(payload) {
        return fetch(API_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json',
                     'Authorization': _proxyHeaders().Authorization },
          body: JSON.stringify(payload)
        });
      }
      // Fast reads served from the server's local ticket store (no Google round-trip).
      function _apiGet(path) {
        var sep = path.indexOf('?') >= 0 ? '&' : '?';
        return fetch(SERVER_URL + path + sep + '_=' + Date.now(),
                     { cache: 'no-store', headers: _proxyHeaders() });
      }

      // ────────────────────────────────────────────────────────────

      let KITS   = {};
      let IMAGES = {};
      let _authToken   = null;
      let _authDisplay = null;
      let _authRole    = null;
      let _authPerms   = [];

      // ── localStorage cache ────────────────────────────────────
      const CACHE_KEY = 'zz2_kit_cache_v1';
      const DAILY_CACHE_KEY = 'zz2_daily_cache_v1';

      function _applyLocalCatalog(kitsJson, tsJson) {
        if (!kitsJson || typeof kitsJson !== 'object' || Array.isArray(kitsJson)) {
          throw new Error('Invalid kit catalogue returned by server');
        }
        if (Object.keys(kitsJson).length === 0) {
          throw new Error('Kit catalogue is empty on the server. Import the VPS data directory and try again.');
        }

        KITS = {};
        for (const [key, k] of Object.entries(kitsJson)) {
          KITS[key] = {
            vehicle:       k.vehicle || '',
            switch_button: k.switch_button || '',
            audio_method:  k.audio_method || '',
            notes:         k.notes || '',
            nodes:         [],
          };
        }
        IMAGES = {};
        for (const [key, k] of Object.entries(kitsJson)) {
          const imgs = {};
          function _resolveImg(u) {
            if (!u) return '';
            if (u.startsWith('/images/')) return SERVER_URL + u;
            return driveToImg(u);
          }
          if (k.dip_image)    imgs.dip    = _resolveImg(k.dip_image);
          if (k.wiring_image) imgs.wiring = _resolveImg(k.wiring_image);
          if (k.misc_image)   imgs.misc   = _resolveImg(k.misc_image);
          if (k.manual)       imgs.manual = k.manual;
          if (Object.keys(imgs).length > 0) IMAGES[key] = imgs;
        }
        for (const [key, issues] of Object.entries(tsJson || {})) {
          if (!KITS[key]) continue;
          KITS[key].nodes = _buildNodesFromLocal(issues);
        }
      }

      function loadFromCache() {
        try {
          if (typeof localStorage === 'undefined') return false;
          const raw = localStorage.getItem(CACHE_KEY);
          if (!raw) return false;
          const parsed = JSON.parse(raw);
          if (!parsed || !parsed.kitsText || !parsed.tsText) return false;
          const { kitsText, tsText, timestamp } = parsed;
          // The compatibility API used to return CSV and now returns local JSON.
          // Detect old caches by content so a cache written by either generation
          // remains usable across a deploy.
          if (parsed.format === 'json' || kitsText.trim().startsWith('{')) {
            _applyLocalCatalog(JSON.parse(kitsText), JSON.parse(tsText));
          } else {
            const { kits, images } = buildKits(parseCSV(kitsText), parseCSV(tsText));
            KITS = kits; IMAGES = images;
          }
          applyKitExtras();
          buildSidebar();
          updateLastRefreshed(new Date(timestamp));
          return true;
        } catch(e) { return false; }
      }
      function saveToCache(kitsText, tsText, format) {
        try {
          const data = JSON.stringify({ kitsText, tsText, format: format || 'csv', timestamp: Date.now() });
          localStorage.setItem(CACHE_KEY, data);
        } catch(e) {
          // localStorage unavailable (private mode, security policy, etc.) — ignore
        }
      }

      // ── CSV parser ───────────────────────────────────────────────
      function parseCSV(text) {
        const lines  = text.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split('\n');
        const headers = parseCSVLine(lines[0]);
        const rows   = [];
        for (let i = 1; i < lines.length; i++) {
          if (!lines[i].trim()) continue;
          const cols = parseCSVLine(lines[i]);
          const row  = {};
          headers.forEach((h,j) => row[h.trim()] = (cols[j]||'').trim());
          rows.push(row);
        }
        return rows;
      }

      function parseCSVLine(line) {
        const res = []; let cur = '', inQ = false;
        for (let i = 0; i < line.length; i++) {
          const c = line[i];
          if (c === '"') {
            if (inQ && line[i+1] === '"') { cur += '"'; i++; }
            else inQ = !inQ;
          } else if (c === ',' && !inQ) { res.push(cur); cur = ''; }
          else cur += c;
        }
        res.push(cur);
        return res;
      }

      // ── Build KITS from two sheets ───────────────────────────────
      function buildKits(kitsRows, tsRows) {
        const kits   = {};
        const images = {};

        // Step 1 — base kit objects from Kits sheet
        for (const row of kitsRows) {
          const key = row['Kit Key'];
          if (!key) continue;
          kits[key] = {
            vehicle:       row['Vehicle']       || '',
            switch_button: row['Switch Button'] || '',
            audio_method:  row['Audio Method']  || '',
            notes:         row['Notes']         || '',
            nodes: [],
          };
          // Image URLs (convert Drive view links to direct)
          const dipURL    = driveToImg(row['DIP Image URL']    || '');
          const wiringURL = driveToImg(row['Wiring Image URL'] || '');
          const miscURL   = driveToImg(row['Misc Image URL']   || '');
          const manualURL = driveToManual(row['Manual URL'] || '');
          if (dipURL || wiringURL || miscURL || manualURL) {
            images[key] = {};
            if (dipURL)    images[key].dip    = dipURL;
            if (wiringURL) images[key].wiring = wiringURL;
            if (miscURL)   images[key].misc   = miscURL;
            if (manualURL) images[key].manual = manualURL;
          }
        }

        // Step 2 — build node tree from Troubleshooting sheet
        // Group rows by kit
        const byKit = {};
        for (const row of tsRows) {
          const key = row['Kit Key'];
          if (!key || !kits[key]) continue;
          if (!byKit[key]) byKit[key] = [];
          byKit[key].push(row);
        }

        // For each kit, build a tree of question + fix nodes
        for (const [key, rows] of Object.entries(byKit)) {
          // Build a lookup: issue title → its row
          const byTitle = {};
          for (const row of rows) byTitle[(row['Issue Title']||'').trim()] = row;

          // Find top-level rows (blank Parent Issue)
          const topLevel = rows.filter(r => !r['Parent Issue']);

          // Recursively build choices for a question node
          function buildChoices(parentTitle) {
            return rows
              .filter(r => (r['Parent Issue'] || '').trim() === parentTitle.trim())
              .map(r => {
                const steps = [];
                for (let i = 1; i <= 7; i++) {
                  const s = r[`Step ${i}`];
                  if (s) steps.push(s);
                }
                const hasChildren = rows.some(c => c['Parent Issue'] === r['Issue Title']);
                const hasSteps    = steps.length > 0;

                // Question Text column: separate label (menu button) from question heading
                const menuLabel    = r['Issue Title'].trim();
                const rawQuestion  = (r['Question Text'] || '').trim();
                const questionText = rawQuestion || menuLabel;

                if (hasChildren && !hasSteps) {
                  // Pure question node — branches further.
                  // If no Question heading was entered for this node, fall back to a
                  // generic prompt rather than echoing the label the tech just clicked
                  // (that made the follow-up read as e.g. "No audio" again). The
                  // breadcrumb above already shows which branch they're in.
                  return {
                    type:    'question',
                    text:    rawQuestion || 'Which of these applies?',
                    label:   menuLabel,      // shown in the parent menu button
                    choices: buildChoices(menuLabel),
                  };
                } else if (hasChildren && hasSteps) {
                  // Has steps AND children — show steps first, then sub-choices
                  return {
                    type:    'fix-and-question',
                    text:    questionText,
                    label:   menuLabel,
                    steps:   steps,
                    choices: buildChoices(menuLabel),
                  };
                } else {
                  // Pure fix node
                  return {
                    type:  'fix',
                    text:  menuLabel,
                    steps: steps,
                  };
                }
              });
          }

          // Build the start question node
          const choices = buildChoices('');
          kits[key].nodes = [{
            id:      'start',
            type:    'question',
            text:    'What is the issue?',
            choices: choices,
          }];
        }

        return { kits, images };
      }

      // Convert Drive view URL to direct-load URL
      function driveToImg(url) {
        if (!url) return '';
        const m = url.match(/\/file\/d\/([a-zA-Z0-9_-]+)/);
        if (m) {
          const id = m[1];
          // Use thumbnail URL — works without CORS issues and doesn't require login
          // sz=w2000 gives up to 2000px wide which is sufficient for diagrams
          return `https://lh3.googleusercontent.com/d/${id}=w2000`;
        }
        return url;
      }
      // Convert Google Drive view URL to embeddable PDF preview URL
      function driveToManual(url) {
        if (!url) return '';
        const m = url.match(/\/file\/d\/([a-zA-Z0-9_-]+)/);
        if (m) return `https://drive.google.com/file/d/${m[1]}/preview`;
        if (url.includes('/preview')) return url;
        return url;
      }


      // ── Load from sheets ─────────────────────────────────────────
      // Rebuild nodeStack against fresh kit data after a resync
      // so updated step text shows immediately without re-clicking
      function rebuildStack(oldStack, kit) {
        if (!oldStack || oldStack.length === 0) return ['start'];
        const newStack = [];
        for (let i = 0; i < oldStack.length; i++) {
          const entry = oldStack[i];
          if (entry === 'start' || (typeof entry === 'string' && kit.nodes.find(n => n.id === entry))) {
            newStack.push(entry);
          } else {
            // Inline object — find matching node in fresh data by text/label
            const label = typeof entry === 'object' ? (entry.label || entry.text) : entry;
            const freshNode = findNodeByLabel(kit.nodes[0], label);
            newStack.push(freshNode || entry);
          }
        }
        return newStack.length > 0 ? newStack : ['start'];
      }

      function findNodeByLabel(startNode, label) {
        if (!startNode || !startNode.choices) return null;
        for (const c of startNode.choices) {
          if ((c.label || c.text) === label) return c;
          // Search recursively in nested choices
          if (c.choices) {
            const found = findNodeByLabel(c, label);
            if (found) return found;
          }
        }
        return null;
      }

      
      // ── Auth ─────────────────────────────────────────────────────────────
      const AUTH_TOKEN_KEY = 'zz2_auth_token';

      function authInit() {
        var token = localStorage.getItem(AUTH_TOKEN_KEY);
        if (!token) {
          window.location.href = '/login.html';
          return;
        }
        ZZ2.verifySession(token)
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (d.ok) {
            _authToken   = token;
            _authDisplay = d.display;
            _authRole    = d.role;
            _authPerms   = d.permissions || [];
            authUpdateUI();
            // Fetch config (webapp URL/key) then open daily view
            fetchAppConfig(token).then(function() {
              // On phones the ticket panel is a full-screen sheet, so don't
              // auto-open it on load (it would bury the kit tool). The Tickets
              // button still opens it on demand.
              if (window.matchMedia('(min-width: 641px)').matches) {
                showDailyView();
              }
            });
          } else {
            localStorage.removeItem(AUTH_TOKEN_KEY);
            window.location.href = '/login.html';
          }
        })
        .catch(function() { authUpdateUI(); });
      }

      function authLogin(username, password, cb) {
        fetch(SERVER_URL + '/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: username, password: password })
        })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (d.ok) {
            _authToken   = d.token;
            _authDisplay = d.display;
            localStorage.setItem(AUTH_TOKEN_KEY, d.token);
            authUpdateUI();
            cb(null, d);
          } else {
            cb(d.error || 'Login failed');
          }
        })
        .catch(function(e) { cb('Server unreachable'); });
      }

      function authLogout() {
        var token = _authToken;
        _authToken = null; _authDisplay = null; _authRole = null; _authPerms = [];
        localStorage.removeItem(AUTH_TOKEN_KEY);
        localStorage.removeItem('zz2_auth_role');
        if (token) {
          ZZ2.logoutRequest().catch(function(){});
        }
        window.location.href = '/login.html';
      }

      function _hasPerm(p) { return _authPerms.indexOf(p) >= 0; }
      function authUpdateUI() {
        var loggedIn    = !!_authToken;
        var canEdit     = loggedIn && _hasPerm('kit_editor');
        var canTicket   = loggedIn && _hasPerm('create_tickets');
        var loginBtn    = document.getElementById('auth-login-btn');
        var logoutBtn   = document.getElementById('auth-logout-btn');
        var userLabel   = document.getElementById('auth-user-label');
        var ticketBtn   = document.getElementById('ticket-create-btn');
        var editBtn     = document.getElementById('auth-editkits-btn');
        if (loginBtn)  loginBtn.style.display   = loggedIn ? 'none' : 'inline-flex';
        if (logoutBtn) logoutBtn.style.display  = loggedIn ? 'inline-flex' : 'none';
        if (userLabel) userLabel.textContent    = loggedIn ? _authDisplay : '';
        if (ticketBtn) ticketBtn.style.display  = canTicket ? 'inline-flex' : 'none';
        if (editBtn)   editBtn.style.display    = canEdit  ? 'inline-flex' : 'none';
        if (window.zz2BuildNav) zz2BuildNav(_authPerms || []);
      }

      function showLoginModal() {
        document.getElementById('login-modal').style.display = 'flex';
        document.getElementById('login-username').focus();
        document.getElementById('login-error').textContent = '';
      }

      function hideLoginModal() {
        document.getElementById('login-modal').style.display = 'none';
      }

      // ── Ticket Panel ──────────────────────────────────────────────────────
      let _ticketMode     = 'new';  // 'new' or 'edit'
      let _ticketReturnTab = 'today'; // last list pane (today/search) to return to on exit/save
      let _ticketRowData  = null;
      let _ticketAutoCreateTimer = null;
      let _ticketAutoCreating = false;
      let _ticketSaveAfterAutoCreate = false;
      let _ticketFormGeneration = 0;

      // ── Realtime collaboration state (Supabase presence/broadcast) ─────────
      let _ticketChannel      = null;   // current per-ticket Supabase Realtime channel, or null
      let _ticketChannelNum   = null;   // ticketNum (string) the channel above is joined to
      let _channelWasDown     = false;  // true while reconnecting after CHANNEL_ERROR/TIMED_OUT
      let _ticketDirty        = false;  // true once the open ticket has unsaved local edits
      let _pendingRemoteTicket = null;  // latest server copy, held until the user confirms reload
      const _rtClientId = 'c_' + Math.random().toString(36).slice(2) + Date.now().toString(36);

      window.addEventListener('beforeunload', function () { _leaveTicketChannel(); });

      function _ticketFormPayload() {
        return {
          tech:       _tv('ticket-tech'),
          client:     _tv('ticket-client').trim(),
          product:    _tv('ticket-product').trim(),
          vehicle:    _tv('ticket-vehicle'),
          issue:      _tv('ticket-issue'),
          resolution: _tv('ticket-resolution'),
          status:     _tv('ticket-status'),
          serial:     _tv('ticket-serial'),
          notes:      _tv('ticket-notes')
        };
      }

      // Reserve and persist the ticket as soon as either identifying field is
      // entered. The explicit Save button then updates this same row.
      function ticketAutoCreateSchedule() {
        if (_ticketMode !== 'new' || _ticketAutoCreating) return;
        var payload = _ticketFormPayload();
        if (!payload.client && !payload.product) return;
        if (_ticketAutoCreateTimer) clearTimeout(_ticketAutoCreateTimer);
        _ticketAutoCreateTimer = setTimeout(ticketAutoCreateNow, 650);
      }

      function ticketAutoCreateNow() {
        if (_ticketMode !== 'new' || _ticketAutoCreating) return;
        if (_ticketAutoCreateTimer) {
          clearTimeout(_ticketAutoCreateTimer);
          _ticketAutoCreateTimer = null;
        }
        var payload = _ticketFormPayload();
        if (!payload.client && !payload.product) return;
        var generation = _ticketFormGeneration;
        payload.action = 'newticket';
        _ticketAutoCreating = true;
        var msg = document.getElementById('ticket-msg');
        if (msg) msg.textContent = 'Creating ticket in Supabase...';
        proxyPost(payload)
        .then(function(r) { return r.json(); })
        .then(function(d) {
          _ticketAutoCreating = false;
          if (!d || !d.ok) {
            _ticketSaveAfterAutoCreate = false;
            if (msg) msg.textContent = 'Error: ' + ((d && d.error) || 'could not create ticket');
            return;
          }
          // The user may have discarded/reset while the request was in flight.
          // Remove that just-created row instead of leaving an orphan ticket.
          if (generation !== _ticketFormGeneration || _ticketMode !== 'new') {
            proxyPost({ action: 'deleteticket', ticketNum: d.ticketNum }).catch(function() {});
            ticketAutoCreateSchedule();
            return;
          }
          var current = _ticketFormPayload();
          var ticket = {
            ticketNum: String(d.ticketNum), date: _todayISO(),
            tech: current.tech, client: current.client, product: current.product,
            vehicle: current.vehicle, issue: current.issue, resolution: current.resolution,
            status: current.status, serial: current.serial, notes: current.notes
          };
          _ticketMode = 'edit';
          _ticketRowData = ticket;
          document.getElementById('ticket-num-display').textContent = _fmtTicketNum(d.ticketNum);
          document.getElementById('ticket-save-btn').textContent = 'Update Ticket';
          _pendingOptimistic[String(d.ticketNum)] = { t: ticket, ts: Date.now() };
          _upsertDailyTicket(ticket);
          _saveDailyCache();
          _clearDraft();
          _ticketDirty = false;
          _joinTicketChannel(d.ticketNum);
          if (msg) msg.textContent = 'Ticket ' + _fmtTicketNum(d.ticketNum) + ' created in Supabase.';
          if (_ticketSaveAfterAutoCreate) {
            _ticketSaveAfterAutoCreate = false;
            ticketSave();
          }
        })
        .catch(function() {
          _ticketAutoCreating = false;
          _ticketSaveAfterAutoCreate = false;
          if (msg) msg.textContent = 'Could not reach server to create the ticket.';
        });
      }

      (function () {
        var form = document.getElementById('ticket-form-view');
        if (form) form.addEventListener('input', function (e) {
          if (e.target && e.target.id && e.target.id.indexOf('ticket-') === 0) _ticketDirty = true;
        });
      })();

      function _joinTicketChannel(ticketNum) {
        if (!_sb || !ticketNum) return;
        var key = String(ticketNum);
        if (_ticketChannelNum === key && _ticketChannel) return;   // already on this ticket's channel
        _leaveTicketChannel();
        var ch = _sb.channel('ticket:' + key, {
          config: { broadcast: { self: false }, presence: { key: _rtClientId } }
        });
        ch.on('presence', { event: 'sync' }, function () { _renderTicketPresence(); });
        ch.on('broadcast', { event: 'ticket_updated' }, function (m) { _onRemoteTicketUpdated(m.payload); });
        ch.on('broadcast', { event: 'ticket_deleted' }, function (m) { _onRemoteTicketDeleted(m.payload); });
        ch.subscribe(function (status) { _onTicketChannelStatus(status, key); });
        _ticketChannel = ch;
        _ticketChannelNum = key;
      }

      function _leaveTicketChannel() {
        if (_ticketChannel) {
          try { _ticketChannel.untrack(); } catch (e) {}
          try { _sb && _sb.removeChannel(_ticketChannel); } catch (e) {}
        }
        _ticketChannel = null;
        _ticketChannelNum = null;
        _channelWasDown = false;
        _renderTicketPresence();
      }

      function _onTicketChannelStatus(status, ticketNum) {
        if (!_ticketChannel || _ticketChannelNum !== ticketNum) return;  // callback from a channel already left
        if (status === 'SUBSCRIBED') {
          try {
            _ticketChannel.track({ name: _authDisplay || 'Unknown', role: _authRole || '', clientId: _rtClientId, ts: Date.now() });
          } catch (e) {}
          if (_channelWasDown) {
            _channelWasDown = false;
            _handleActiveTicketChangedRemotely({ ticketNum: ticketNum, by: '' });
            loadTodayTickets(true);
          }
        } else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT') {
          _channelWasDown = true;
          setTimeout(function () {
            if (_ticketChannelNum === ticketNum) _joinTicketChannel(ticketNum);   // single retry, only if still relevant
          }, 4000);
        }
      }

      function _renderTicketPresence() {
        var row = document.getElementById('ticket-presence-row');
        if (!row) return;
        try {
          if (!_ticketChannel) { row.style.display = 'none'; row.innerHTML = ''; return; }
          var state = _ticketChannel.presenceState();
          var names = [];
          Object.keys(state).forEach(function (key) {
            (state[key] || []).forEach(function (p) {
              if (p && p.clientId !== _rtClientId && p.name) names.push(p.name);
            });
          });
          if (!names.length) { row.style.display = 'none'; row.innerHTML = ''; return; }
          row.style.display = 'flex';
          row.innerHTML = '👀 ' + names.map(function (n) {
            return '<span style="background:#eef2ff;color:#3730a3;border-radius:10px;padding:1px 7px;">' + _esc(n) + '</span>';
          }).join(' ');
        } catch (e) { row.style.display = 'none'; }
      }

      function _onRemoteTicketUpdated(p) {
        if (!p || !p.ticketNum) return;
        loadTodayTickets(true);
        if (_ticketMode === 'edit' && _ticketRowData && String(_ticketRowData.ticketNum) === String(p.ticketNum)) {
          _handleActiveTicketChangedRemotely(p);
        }
      }

      function _onRemoteTicketDeleted(p) {
        if (!p || !p.ticketNum) return;
        loadTodayTickets(true);
        if (_ticketMode === 'edit' && _ticketRowData && String(_ticketRowData.ticketNum) === String(p.ticketNum)) {
          _showTicketGoneBanner(p.by);
        }
      }

      function _handleActiveTicketChangedRemotely(p) {
        _apiGet('/api/tickets/get?ticketNum=' + encodeURIComponent(p.ticketNum))
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!d.ok || !d.result) return;
            if (_ticketMode !== 'edit' || !_ticketRowData || String(_ticketRowData.ticketNum) !== String(p.ticketNum)) return;
            if (_ticketDirty) {
              _pendingRemoteTicket = d.result;
              _showStaleBanner(p.by);
            } else {
              ticketLoadResult(d.result);   // safe: no local edits to lose
            }
          })
          .catch(function () {});
      }

      function _showStaleBanner(by) {
        var el = document.getElementById('ticket-stale-banner');
        if (!el) return;
        var who = by ? _esc(by) : 'Someone';
        el.style.display = 'flex';
        el.innerHTML = '<span>' + who + ' updated this ticket.</span>' +
          '<span><a href="#" onclick="_applyPendingRemoteTicket();return false;" style="font-weight:700;">Reload</a>' +
          '&nbsp;&nbsp;<a href="#" onclick="_hideStaleBanner();return false;">×</a></span>';
      }

      function _showTicketGoneBanner(by) {
        var el = document.getElementById('ticket-stale-banner');
        if (!el) return;
        var who = by ? _esc(by) : 'Someone';
        el.style.display = 'flex';
        el.innerHTML = '<span>' + who + ' discarded this ticket.</span>' +
          '<span><a href="#" onclick="ticketNew();_setActiveTab(\'today\');return false;" style="font-weight:700;">OK</a></span>';
      }

      function _hideStaleBanner() {
        var el = document.getElementById('ticket-stale-banner');
        if (el) { el.style.display = 'none'; el.innerHTML = ''; }
        _pendingRemoteTicket = null;
      }

      function _applyPendingRemoteTicket() {
        if (_pendingRemoteTicket) {
          var t = _pendingRemoteTicket;
          _pendingRemoteTicket = null;
          ticketLoadResult(t);
        }
        _hideStaleBanner();
      }

      function ticketNew() {
        _ticketFormGeneration += 1;
        if (_ticketAutoCreateTimer) {
          clearTimeout(_ticketAutoCreateTimer);
          _ticketAutoCreateTimer = null;
        }
        _ticketSaveAfterAutoCreate = false;
        _leaveTicketChannel();
        _ticketDirty = false;
        _hideStaleBanner();
        _ticketMode   = 'new';
        _ticketRowData = null;
        document.getElementById('ticket-num-display').textContent = 'Auto';
        // Tech autofills from the logged-in technician; everything else is blank.
        document.getElementById('ticket-tech').value       = _authDisplay || '';
        document.getElementById('ticket-client').value     = '';
        document.getElementById('ticket-client').classList.remove('input-error');
        customerPickerClose();
        _hideCustomerSummary();
        _resetCustomerHistory();
        _resetKitHistory();
        document.getElementById('ticket-product').value    = '';
        document.getElementById('ticket-vehicle').value    = '';
        document.getElementById('ticket-issue').value      = '';
        document.getElementById('ticket-resolution').value = '';
        document.getElementById('ticket-status').value     = '';
        document.getElementById('ticket-serial').value     = '';
        document.getElementById('ticket-notes').value      = '';
        document.getElementById('ticket-save-btn').textContent = 'Save Ticket';
        document.getElementById('ticket-save-btn').disabled = false;
        document.getElementById('ticket-search-results').style.display = 'none';
        document.getElementById('ticket-search-input').value = '';
        document.getElementById('ticket-msg').textContent = '';
        _hideDup();
        _resetApply();
        // Offer (don't force) recovery of an unsaved draft, so "New" stays blank.
        _offerDraftRestore();
        backToKits();   // leaving or resetting a ticket clears the kit from the screen
      }

      // Discard. For a ticket that's already been saved, this deletes it from the
      // sheet (and the local store). For a new, unsaved ticket it just clears the
      // form and returns to the daily list.
      function ticketDiscard() {
        if (_ticketMode === 'edit' && _ticketRowData && _ticketRowData.ticketNum) {
          var num = _ticketRowData.ticketNum;
          if (!confirm('Delete ticket ' + num + ' from the sheet? This cannot be undone.')) return;
          var msg = document.getElementById('ticket-msg');
          if (msg) msg.textContent = 'Deleting...';
          proxyPost({ action: 'deleteticket', ticketNum: num })
          .then(function(r) { return r.json(); })
          .then(function(d) {
            if (d && d.ok) {
              if (_ticketRowData && _ticketRowData.client) {
                delete _customerHistoryCache[_customerNorm(_ticketRowData.client)];
              }
              if (_ticketRowData && _ticketRowData.product) {
                delete _kitHistoryCache[_customerNorm(_ticketRowData.product)];
              }
              delete _pendingOptimistic[String(num)];   // stop _mergePending re-adding it
              _dailyTickets = _dailyTickets.filter(function(t) {
                return String(t.ticketNum || t.ticket || '') !== String(num);
              });
              _saveDailyCache();
              _clearDraft();
              if (_ticketChannel && _ticketChannelNum === String(num)) {
                try {
                  _ticketChannel.send({ type: 'broadcast', event: 'ticket_deleted',
                    payload: { ticketNum: String(num), by: _authDisplay || '', ts: Date.now() } });
                } catch (e) {}
              }
              ticketNew();
              _setActiveTab('today');
              loadTodayTickets(true);
            } else if (msg) {
              msg.textContent = 'Delete failed: ' + ((d && d.error) || 'unknown');
            }
          })
          .catch(function() {
            if (msg) msg.textContent = 'Could not reach server.';
          });
          return;
        }
        var hasContent = !_draftIsEmpty(_collectDraft());
        if (hasContent && !confirm('Discard this ticket? Unsaved changes will be lost.')) return;
        _clearDraft();
        ticketNew();
        _setActiveTab('today');
      }

      // Exit the currently viewed/loaded ticket and return to whichever list pane
      // the user came from — Today by default, or Search if they were searching.
      function ticketExit() {
        ticketNew();
        _setActiveTab(_ticketReturnTab);
      }

      function _getCurrentIssue() {
        // Build issue string from nodeStack
        var parts = [];
        for (var i = 1; i < nodeStack.length; i++) {
          var n = nodeStack[i];
          if (n && typeof n === 'object') {
            parts.push(n.label || n.text || '');
          } else if (typeof n === 'string') {
            parts.push(n);
          }
        }
        return parts.filter(Boolean).join(' → ');
      }

      function ticketOnKitSelect(key) {
        if (!KITS[key]) return;
        document.getElementById('ticket-product').value  = key;
        kitHistorySchedule(key, true);
        selectKit(key);
        ticketAutoCreateSchedule();
      }

      function ticketSave() {
        var client = (document.getElementById('ticket-client').value || '').trim();
        if (!client) {
          var cl = document.getElementById('ticket-client');
          document.getElementById('ticket-msg').textContent = 'Please enter a client name.';
          cl.classList.add('input-error');
          var body = document.querySelector('#ticket-form-view .ticket-body');
          if (body) body.scrollTop = 0;            // instant scroll to the top of the form
          cl.scrollIntoView({ block: 'center' });  // and centre the field
          setTimeout(function () {
            try { cl.focus({ preventScroll: true }); } catch (e) { cl.focus(); }
          }, 30);
          return;
        }

        // If the identifying-field auto-create is still pending, finish that
        // first and then update the same ticket instead of creating a duplicate.
        if (_ticketMode === 'new' && (_ticketAutoCreateTimer || _ticketAutoCreating)) {
          _ticketSaveAfterAutoCreate = true;
          ticketAutoCreateNow();
          return;
        }

        var payload = _ticketFormPayload();
        payload.client = client;

        document.getElementById('ticket-msg').textContent = 'Saving...';
        document.getElementById('ticket-save-btn').disabled = true;

        if (_ticketMode === 'edit' && _ticketRowData) {
          payload.action    = 'updateticket';
          payload.ticketNum = _ticketRowData.ticketNum;
        } else {
          payload.action = 'newticket';
        }

        // Routed through the authenticated proxy: the server attaches the secret
        // key and mirrors the ticket into the local store.
        proxyPost(payload)
        .then(function(r) { return r.json(); })
        .then(function(d) {
          document.getElementById('ticket-save-btn').disabled = false;
          if (d.ok) {
            var wasEdit = (_ticketMode === 'edit');
            delete _customerHistoryCache[_customerNorm(payload.client)];
            delete _kitHistoryCache[_customerNorm(payload.product)];
            if (_ticketRowData && _ticketRowData.client) {
              delete _customerHistoryCache[_customerNorm(_ticketRowData.client)];
            }
            if (_ticketRowData && _ticketRowData.product) {
              delete _kitHistoryCache[_customerNorm(_ticketRowData.product)];
            }
            // Build an optimistic copy so Today shows the ticket right away, even
            // before it lands in the store/sheet. Keep the original date on edits.
            var optDate = (wasEdit && _ticketRowData && _ticketRowData.date)
                          ? _ticketRowData.date : _todayISO();
            var optT = {
              ticketNum: String(d.ticketNum),
              tech: payload.tech, client: payload.client, product: payload.product,
              vehicle: payload.vehicle, issue: payload.issue, resolution: payload.resolution,
              status: payload.status, serial: payload.serial, notes: payload.notes,
              date: optDate
            };
            _pendingOptimistic[String(d.ticketNum)] = { t: optT, ts: Date.now() };
            _upsertDailyTicket(optT);
            _saveDailyCache();

            if (wasEdit && _ticketChannel && _ticketChannelNum === String(d.ticketNum)) {
              try {
                _ticketChannel.send({ type: 'broadcast', event: 'ticket_updated',
                  payload: { ticketNum: String(d.ticketNum), by: _authDisplay || '', ts: Date.now() } });
              } catch (e) {}
            }

            _clearDraft();
            _hideDup();
            ticketNew();              // reset the form (also deselects the kit)
            _setActiveTab(_ticketReturnTab);   // back to Today, or Search if that's where you were
          } else {
            document.getElementById('ticket-msg').textContent = 'Error: ' + (d.error || 'unknown');
          }
        })
        .catch(function() {
          document.getElementById('ticket-save-btn').disabled = false;
          document.getElementById('ticket-msg').textContent = 'Could not reach server.';
        });
      }

      // ── Ticket-form interactive layer ─────────────────────────────────────
      let _searchLimit  = 300;   // scan only the most recent N rows (sheet is large)
      let _draftTimer   = null;
      let _dupTimer     = null;
      let _pendingDraft = null;  // a recoverable draft awaiting opt-in restore

      function onSearchLimitChange(val) {
        _searchLimit = parseInt(val) || 300;
        var q = (document.getElementById('ticket-search-input').value || '').trim();
        if (q.length >= 2) ticketSearch();
      }

      function _tv(id) { var e = document.getElementById(id); return e ? e.value : ''; }
      function _fmtTicketNum(n) {
        var s = String(n == null ? '' : n).trim();
        if (!s) return s;
        return /^tkt-/i.test(s) ? s : 'TKT-' + s;
      }
      function _esc(s) {
        return String(s == null ? '' : s)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      }
      function _authBearer() {
        return _authToken || localStorage.getItem(AUTH_TOKEN_KEY) || '';
      }

      // ── Kit type-to-search dropdown ───────────────────────────────────────
      function kitSearchInput(val) {
        var dd = document.getElementById('kit-search-dropdown');
        if (!dd) return;
        var f = (val || '').trim().toLowerCase();
        if (f.length < 1) { dd.style.display = 'none'; return; }
        var keys = Object.keys(KITS).sort().filter(function(k) {
          return k.toLowerCase().indexOf(f) >= 0 ||
                 ((KITS[k].vehicle || '').toLowerCase().indexOf(f) >= 0);
        }).slice(0, 12);
        if (!keys.length) { dd.style.display = 'none'; return; }
        dd.innerHTML = keys.map(function(k) {
          var veh = (KITS[k].vehicle || '').split('(')[0].trim();
          return '<div style="padding:7px 10px;cursor:pointer;border-bottom:1px solid #f0f0f0;" ' +
            'onmousedown="ticketPickKit(\'' + k.replace(/'/g, "\\'") + '\')" ' +
            'onmouseover="this.style.background=\'#f4f4f4\'" onmouseout="this.style.background=\'#fff\'">' +
            '<div style="font-weight:600;font-size:12px;color:#464f56;">' + _esc(k) + '</div>' +
            '<div style="font-size:11px;color:#888;">' + _esc(veh) + '</div></div>';
        }).join('');
        dd.style.display = 'block';
      }
      function kitSearchBlur() {
        // delay so an onmousedown selection registers before the dropdown hides
        setTimeout(function() {
          var dd = document.getElementById('kit-search-dropdown');
          if (dd) dd.style.display = 'none';
        }, 150);
      }
      function ticketPickKit(key) {
        var dd = document.getElementById('kit-search-dropdown');
        if (dd) dd.style.display = 'none';
        ticketOnKitSelect(key);   // sets product + vehicle, loads kit context
        dupSchedule();
        draftSchedule();
      }

      // ── Apply troubleshooting path to the ticket ──────────────────────────
      // The tech walks the flowchart, optionally unchecks steps they don't want,
      // then taps "Apply to ticket": the navigated path fills Issue and the checked
      // steps fill Resolution. Everything is additive — segments split on ";" so
      // anything typed by hand is never overwritten.
      function _escA(s) {
        return String(s == null ? '' : s)
          .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
          .replace(/</g, '&lt;').replace(/>/g, '&gt;');
      }
      function _segs(v) {
        return (v || '').split(';').map(function(s){ return s.trim(); }).filter(Boolean);
      }
      function _hasSeg(v, text) {
        var t = (text || '').trim().toLowerCase();
        return _segs(v).some(function(s){ return s.toLowerCase() === t; });
      }
      // What the last Apply contributed to Issue / Resolution, so a re-apply
      // overwrites its own previous contribution — never the tech's typed text.
      var _lastApply = { issue: [], resolution: [] };
      function _resetApply() { _lastApply = { issue: [], resolution: [] }; }
      // Remove the previously-applied segments, then add the new ones. Anything the
      // tech typed by hand (not in prevSegs) is left exactly as-is.
      function _setAppliedSegs(fieldId, prevSegs, newSegs) {
        var el = document.getElementById(fieldId);
        if (!el) return;
        var prevLower = (prevSegs || []).map(function(s){ return (s || '').toLowerCase(); });
        var segs = _segs(el.value).filter(function(s){ return prevLower.indexOf(s.toLowerCase()) < 0; });
        (newSegs || []).forEach(function(ns) {
          var t = (ns || '').trim();
          if (!t) return;
          if (!segs.some(function(s){ return s.toLowerCase() === t.toLowerCase(); })) segs.push(t);
        });
        el.value = segs.join('; ');
      }
      // Flowchart step checkbox interactions.
      function toggleStepCheck(cbId, row) {
        var cb = document.getElementById(cbId);
        if (!cb) return;
        cb.checked = !cb.checked;
        syncStepCheck(cbId, cb, row);
      }
      function syncStepCheck(cbId, cb, row) {
        if (row) row.classList.toggle('step-checked', !!(cb && cb.checked));
      }
      // Make the ticket form visible without wiping any fields.
      function _ensureTicketCreateVisible() {
        var panel = document.getElementById('ticket-panel');
        if (panel && panel.style.display !== 'flex') {
          panel.style.display = 'flex';
          document.body.classList.add('ticket-panel-open');
          if (typeof _populateTicketKitSelect === 'function') _populateTicketKitSelect();
        }
        _setActiveTab('create');
      }
      // Apply the current flowchart path + checked steps to the ticket. Re-applying
      // replaces whatever the previous Apply put in Issue/Resolution (no duplicates),
      // while leaving any hand-typed text alone.
      function applyPathToTicket() {
        var path  = _getCurrentIssue();
        var boxes = document.querySelectorAll('#content .step-checkbox');
        var steps = [];
        for (var i = 0; i < boxes.length; i++) {
          if (boxes[i].checked) {
            var s = boxes[i].getAttribute('data-step');
            if (s) steps.push(s);
          }
        }
        var prod = document.getElementById('ticket-product');
        if (prod && !prod.value && activeKit) {
          prod.value = activeKit;
          kitHistorySchedule(activeKit, true);
        }

        var newIssue = path ? [path] : [];
        _setAppliedSegs('ticket-issue', _lastApply.issue, newIssue);
        _setAppliedSegs('ticket-resolution', _lastApply.resolution, steps);
        _lastApply.issue = newIssue.slice();
        _lastApply.resolution = steps.slice();

        _ensureTicketCreateVisible();
        // When this Apply is starting a fresh ticket (tech still blank), fill the
        // tech field with the logged-in user, like a new ticket does. If a ticket
        // is already in progress or being edited, its tech is already set, so leave it.
        var techEl = document.getElementById('ticket-tech');
        if (techEl && !techEl.value) techEl.value = _authDisplay || '';
        draftSchedule();

        var msg = document.getElementById('ticket-msg');
        if (msg) {
          msg.textContent = 'Applied to ticket \u2713  (' + steps.length + ' step' + (steps.length === 1 ? '' : 's') + ')';
          msg.style.color = '#caa400';
          setTimeout(function(){ if (msg) msg.style.color = ''; }, 2500);
        }
        _flashApplyBtn();
      }
      function _flashApplyBtn() {
        var b = document.getElementById('apply-path-btn');
        if (!b) return;
        var orig = b.getAttribute('data-label') || b.innerHTML;
        b.setAttribute('data-label', orig);
        b.innerHTML = 'Added \u2713';
        b.classList.add('applied');
        setTimeout(function(){ if (b){ b.innerHTML = orig; b.classList.remove('applied'); } }, 1400);
      }

      // ── Status change ─────────────────────────────────────────────────────
      function onStatusChange(val) {
        draftSchedule();
      }

      // ── Duplicate detection ───────────────────────────────────────────────
      // A ticket is "open" unless its status is one of these terminal states.
      var _dupClosedStatuses = ['Resolved', 'No Solution', 'Field Destroy', 'W.O.E.'];
      function _hideDup() {
        var w = document.getElementById('ticket-dup-warning');
        if (w) w.style.display = 'none';
      }
      function dupSchedule() {
        if (_ticketMode !== 'new') { _hideDup(); return; }
        if (_dupTimer) clearTimeout(_dupTimer);
        _dupTimer = setTimeout(_checkDuplicate, 600);
      }

      // ── Customer picker and purchase/RMA summary ─────────────────────────
      let _customerPickerItems = [];
      let _customerPickerVisibleItems = [];
      let _customerPickerActive = -1;
      let _customerPickerLoaded = false;
      let _customerPickerLoading = false;
      let _customerHistoryTimer = null;
      let _customerHistoryRequest = 0;
      const _customerHistoryCache = {};
      let _customerHistoryTickets = [];
      let _kitHistoryTimer = null;
      let _kitHistoryRequest = 0;
      const _kitHistoryCache = {};

      function _customerNorm(value) {
        return (value || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').trim();
      }

      function _customerEditDistance(a, b) {
        var prev = [], cur = [], i, j;
        for (j = 0; j <= b.length; j++) prev[j] = j;
        for (i = 1; i <= a.length; i++) {
          cur = [i];
          for (j = 1; j <= b.length; j++) {
            cur[j] = Math.min(cur[j - 1] + 1, prev[j] + 1,
              prev[j - 1] + (a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1));
          }
          prev = cur;
        }
        return prev[b.length];
      }

      function _customerMatchScore(name, query) {
        var n = _customerNorm(name), q = _customerNorm(query);
        if (!q) return 1;
        if (n === q) return 4;
        if (n.indexOf(q) === 0) return 3;
        if (n.indexOf(q) >= 0) return 2;
        var words = n.split(/\s+/);
        for (var i = 0; i < words.length; i++) if (words[i].indexOf(q) === 0) return 1.8;
        var distance = _customerEditDistance(n, q);
        return Math.max(0, 1 - distance / Math.max(n.length, q.length, 1));
      }

      function _loadCustomerPicker() {
        if (_customerPickerLoaded || _customerPickerLoading) return;
        _customerPickerLoading = true;
        _apiGet('/api/customers')
          .then(function (r) { return r.json(); })
          .then(function (d) {
            _customerPickerLoading = false;
            if (!d || !d.ok) throw new Error((d && d.error) || 'Customer lookup failed');
            _customerPickerItems = d.customers || [];
            _customerPickerLoaded = true;
            if (document.activeElement === document.getElementById('ticket-client')) {
              customerPickerFilter(_tv('ticket-client'));
            } else {
              _showCustomerSummaryForName(_tv('ticket-client'));
            }
          })
          .catch(function () {
            _customerPickerLoading = false;
            var menu = document.getElementById('ticket-client-dropdown');
            if (menu) {
              menu.style.display = 'block';
              menu.innerHTML = '<div class="customer-picker-empty">Could not load customers.</div>';
            }
          });
      }

      function customerPickerOpen() {
        _loadCustomerPicker();
        customerPickerFilter(_tv('ticket-client'));
      }

      function customerPickerFilter(query) {
        _hideCustomerSummary();
        var menu = document.getElementById('ticket-client-dropdown');
        var input = document.getElementById('ticket-client');
        if (!menu || !input) return;
        input.setAttribute('aria-expanded', 'true');
        menu.style.display = 'block';
        if (!_customerPickerLoaded) {
          menu.innerHTML = '<div class="customer-picker-empty">Loading customers…</div>';
          _loadCustomerPicker();
          return;
        }
        var q = _customerNorm(query);
        _customerPickerVisibleItems = _customerPickerItems.map(function (item) {
          return { item: item, score: _customerMatchScore(item.name, q) };
        }).filter(function (entry) {
          return !q || entry.score >= 0.45;
        }).sort(function (a, b) {
          return b.score - a.score || a.item.name.localeCompare(b.item.name);
        }).map(function (entry) { return entry.item; });
        _customerPickerActive = _customerPickerVisibleItems.length ? 0 : -1;
        if (!_customerPickerVisibleItems.length) {
          menu.innerHTML = '<div class="customer-picker-empty">No similar customer found.</div>';
          return;
        }
        menu.innerHTML = _customerPickerVisibleItems.map(function (item, idx) {
          return '<div class="customer-picker-option' + (idx === 0 ? ' is-active' : '') + '" role="option"' +
            ' onmousedown="event.preventDefault();customerPickerSelect(' + idx + ')">' + _esc(item.name) + '</div>';
        }).join('');
      }

      function customerPickerSelect(index) {
        var item = _customerPickerVisibleItems[index];
        if (!item) return;
        document.getElementById('ticket-client').value = item.name;
        customerPickerClose();
        _showCustomerSummary(item);
        customerHistorySchedule(item.name, true);
        dupSchedule();
        draftSchedule();
        ticketAutoCreateSchedule();
      }

      function customerPickerKeydown(event) {
        var menu = document.getElementById('ticket-client-dropdown');
        if (event.key === 'Escape') { customerPickerClose(); return; }
        if (!menu || menu.style.display === 'none') return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          var step = event.key === 'ArrowDown' ? 1 : -1;
          _customerPickerActive = Math.max(0, Math.min(_customerPickerVisibleItems.length - 1,
            _customerPickerActive + step));
          var options = menu.querySelectorAll('.customer-picker-option');
          for (var i = 0; i < options.length; i++) options[i].classList.toggle('is-active', i === _customerPickerActive);
          if (options[_customerPickerActive]) options[_customerPickerActive].scrollIntoView({block:'nearest'});
        } else if (event.key === 'Enter' && _customerPickerActive >= 0) {
          event.preventDefault();
          customerPickerSelect(_customerPickerActive);
        }
      }

      function customerPickerBlur() {
        setTimeout(function () {
          var typed = _customerNorm(_tv('ticket-client'));
          var exact = _customerPickerItems.filter(function (item) { return _customerNorm(item.name) === typed; })[0];
          customerPickerClose();
          if (exact) {
            _showCustomerSummary(exact);
            customerHistorySchedule(exact.name, true);
          }
        }, 120);
      }

      function customerPickerClose() {
        var menu = document.getElementById('ticket-client-dropdown');
        var input = document.getElementById('ticket-client');
        if (menu) menu.style.display = 'none';
        if (input) input.setAttribute('aria-expanded', 'false');
      }

      function _formatCustomerCount(value) {
        var number = Number(value || 0);
        return number.toLocaleString(undefined, { maximumFractionDigits: 2 });
      }

      function _showCustomerSummary(item) {
        var summary = document.getElementById('ticket-customer-summary');
        if (!summary || !item) return;
        var products = Number(item.products_purchased || 0);
        var rmas = Number(item.rmas || 0);
        var rate = products > 0 ? rmas / products : 0;
        var ratePercent = Math.round(rate * 1000) / 10;
        var isHigh = rate >= 0.15;
        var status = document.getElementById('ticket-customer-status');
        document.getElementById('ticket-customer-products').textContent = _formatCustomerCount(products);
        document.getElementById('ticket-customer-rmas').textContent = _formatCustomerCount(rmas);
        document.getElementById('ticket-customer-rate').textContent =
          ratePercent.toLocaleString(undefined, { maximumFractionDigits: 1 }) + '%';
        status.textContent = isHigh ? 'HIGH' : 'GOOD';
        status.classList.toggle('is-good', !isHigh);
        status.classList.toggle('is-high', isHigh);
        summary.style.display = 'grid';
      }

      function _showCustomerSummaryForName(name) {
        _loadCustomerPicker();
        if (!_customerPickerLoaded) return;
        var wanted = _customerNorm(name);
        var exact = _customerPickerItems.filter(function (item) {
          return _customerNorm(item.name) === wanted;
        })[0];
        if (exact) _showCustomerSummary(exact);
        else _hideCustomerSummary();
      }

      function _hideCustomerSummary() {
        var summary = document.getElementById('ticket-customer-summary');
        if (summary) summary.style.display = 'none';
      }

      function _resetCustomerHistory() {
        if (_customerHistoryTimer) clearTimeout(_customerHistoryTimer);
        _customerHistoryTimer = null;
        _customerHistoryRequest++;
        var client = document.getElementById('customer-history-client');
        var alert = document.getElementById('customer-history-alert');
        var content = document.getElementById('customer-history-content');
        _customerHistoryTickets = [];
        if (client) client.textContent = 'Select a client to view their tickets';
        if (alert) { alert.style.display = 'none'; alert.textContent = ''; }
        if (content) content.innerHTML = '<div class="customer-history-state">Previously created tickets will appear here.</div>';
      }

      function customerHistorySchedule(name, immediate) {
        if (_customerHistoryTimer) clearTimeout(_customerHistoryTimer);
        var value = (name || '').trim();
        if (value.length < 2) { _resetCustomerHistory(); return; }
        _customerHistoryTimer = setTimeout(function () {
          _customerHistoryTimer = null;
          _loadCustomerHistory(value);
        }, immediate ? 0 : 550);
      }

      function _historyTicketDate(value) {
        var raw = String(value || '').trim();
        if (!raw) return 'No date';
        var date = new Date(raw.length === 10 ? raw + 'T12:00:00' : raw);
        if (isNaN(date.getTime())) return raw;
        return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: '2-digit' });
      }

      function _historyNameKey(value) {
        return _customerNorm(value).replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
      }

      function _historyClientQuery(value) {
        var tokens = _historyNameKey(value).split(' ').filter(Boolean);
        var numeric = [];
        tokens.forEach(function (token) {
          var parts = token.match(/\d+/g);
          if (parts) numeric = numeric.concat(parts);
        });
        var ignored = { car:1, stereo:1, audio:1, alarm:1, the:1, and:1, inc:1, llc:1 };
        var distinctive = tokens.filter(function (token) { return !ignored[token]; });
        var choices = numeric.length ? numeric : (distinctive.length ? distinctive : tokens);
        choices.sort(function (a, b) { return b.length - a.length; });
        return '%' + (choices[0] || _historyNameKey(value)) + '%';
      }

      function _isCustomerHistoryMatch(ticketName, requestedName) {
        var ticketKey = _historyNameKey(ticketName);
        var wantedKey = _historyNameKey(requestedName);
        if (!ticketKey || !wantedKey) return false;
        if (ticketKey === wantedKey || ticketKey.replace(/\s/g, '') === wantedKey.replace(/\s/g, '') ||
            ticketKey.indexOf(wantedKey) === 0 || wantedKey.indexOf(ticketKey) === 0) return true;
        var ticketTokens = ticketKey.split(' '), wantedTokens = wantedKey.split(' ');
        var ticketNumbers = ticketTokens.filter(function (token) { return /\d/.test(token); });
        var wantedNumbers = wantedTokens.filter(function (token) { return /\d/.test(token); });
        if (ticketNumbers.length && wantedNumbers.length && ticketNumbers.join('|') !== wantedNumbers.join('|')) return false;
        var common = ticketTokens.filter(function (token) { return wantedTokens.indexOf(token) >= 0; }).length;
        return common / Math.min(ticketTokens.length, wantedTokens.length) >= 0.75;
      }

      function _historyRowFromSupabase(row) {
        return {
          ticketNum: row.ticket_number || '', date: row.ticket_date || '',
          tech: row.tech || '', client: row.client || '', product: row.product || '',
          vehicle: row.vehicle || '', issue: row.issue || '', resolution: row.resolution || '',
          status: row.status || '', serial: row.serial || '', notes: row.notes || ''
        };
      }

      function _ticketHistoryData(field, value) {
        // Read the same live table used by tickets.html. This keeps history
        // current even if the Python process has not yet restarted after a deploy.
        if (!_sb) return _ticketHistoryDataFromServer(field, value);
        var rows = [], pageSize = 1000;
        var queryValue = field === 'client' ? _historyClientQuery(value) : value;
        function loadPage(from) {
          return _sb.schema('techSupport').from('tickets')
            .select('ticket_date,ticket_number,tech,client,product,vehicle,issue,resolution,status,serial,notes')
            .ilike(field, queryValue)
            .order('ticket_date', { ascending: false, nullsFirst: false })
            .order('ticket_number', { ascending: false })
            .range(from, from + pageSize - 1)
            .then(function (result) {
              if (result.error) throw result.error;
              var batch = result.data || [];
              rows = rows.concat(batch);
              return batch.length === pageSize ? loadPage(from + pageSize) : rows;
            });
        }
        return loadPage(0).then(function (liveRows) {
          return { ok: true, source: 'supabase_browser', field: field, value: value,
            count: liveRows.length, results: liveRows.map(_historyRowFromSupabase) };
        }).catch(function () {
          return _ticketHistoryDataFromServer(field, value);
        });
      }

      function _ticketHistoryDataFromServer(field, value) {
        var fuzzy = field === 'client' ? '&fuzzy=1' : '';
        return _apiGet('/api/tickets/history?' + field + '=' + encodeURIComponent(value) + fuzzy)
          .then(function (response) { return response.json(); });
      }

      function _historyWithinThreeMonths(value) {
        var date = _ticketDate(value);
        if (!date) return false;
        var cutoff = new Date();
        cutoff.setHours(0, 0, 0, 0);
        cutoff.setMonth(cutoff.getMonth() - 3);
        return date >= cutoff;
      }

      function _historyMoreButton(kind, olderCount) {
        if (!olderCount) return '';
        return '<button id="' + kind + '-history-more-btn" class="history-more-btn" type="button"' +
          ' aria-expanded="false" onclick="toggleHistoryOlder(\'' + kind + '\')">' +
          'View more · ' + olderCount + ' older ticket' + (olderCount === 1 ? '' : 's') + '</button>';
      }

      function toggleHistoryOlder(kind) {
        var older = document.getElementById(kind + '-history-older');
        var button = document.getElementById(kind + '-history-more-btn');
        if (!older || !button) return;
        var opening = older.style.display === 'none';
        older.style.display = opening ? 'block' : 'none';
        button.setAttribute('aria-expanded', opening ? 'true' : 'false');
        button.textContent = opening ? 'Show last 3 months' :
          'View more · ' + button.getAttribute('data-older-count') + ' older ticket' +
          (button.getAttribute('data-older-count') === '1' ? '' : 's');
      }

      function _renderCustomerHistory(data, requestedName) {
        var client = document.getElementById('customer-history-client');
        var alert = document.getElementById('customer-history-alert');
        var content = document.getElementById('customer-history-content');
        if (!content) return;
        if (!data || !data.ok) {
          if (client) client.textContent = requestedName;
          content.innerHTML = '<div class="customer-history-state">Customer history could not be loaded.</div>';
          return;
        }
        if (client) client.textContent = requestedName;
        if (alert) alert.style.display = 'none';
        var rows = (data.results || []).filter(function (ticket) {
          return _isCustomerHistoryMatch(ticket.client, requestedName);
        });
        _customerHistoryTickets = rows;
        if (client) client.textContent = requestedName + ' · ' + rows.length +
          ' ticket' + (rows.length === 1 ? '' : 's');
        if (!rows.length) {
          content.innerHTML = '<div class="customer-history-state">No created tickets were found for this client.</div>';
          return;
        }
        var entries = rows.map(function (row, index) { return { row: row, index: index }; });
        var recent = entries.filter(function (entry) { return _historyWithinThreeMonths(entry.row.date); });
        var older = entries.filter(function (entry) { return !_historyWithinThreeMonths(entry.row.date); });
        function renderEntries(list, recentStyle) {
          return list.map(function (entry) {
          var row = entry.row, index = entry.index;
          var label = _historyTicketDate(row.date);
          var ticket = _fmtTicketNum(row.ticketNum || '');
          var status = (row.status || '').trim() || 'Open';
          var product = (row.product || '').trim() || 'No kit informed';
          var issue = (row.issue || '').trim() || 'No issue description';
          return '<div class="customer-timeline-item is-ticket' + (recentStyle ? ' is-recent' : '') + '"' +
            ' role="button" tabindex="0" onclick="openCustomerHistoryTicket(' + index + ')"' +
            ' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();openCustomerHistoryTicket(' + index + ')}">' +
            '<div class="customer-timeline-date">' + _esc(label) + '</div>' +
            '<div class="customer-timeline-content">' +
              '<div class="customer-timeline-title">Ticket ' + _esc(ticket) + '</div>' +
              '<div class="customer-timeline-detail">' + _esc(product) + '</div>' +
              '<div class="customer-timeline-detail customer-timeline-issue">' + _esc(issue) + '</div>' +
              '<span class="customer-timeline-status">' + _esc(status) + '</span>' +
            '</div></div>';
          }).join('');
        }
        var recentHtml = recent.length
          ? '<div class="customer-timeline">' + renderEntries(recent, true) + '</div>'
          : '<div class="customer-history-state">No tickets in the last 3 months.</div>';
        var olderHtml = older.length
          ? '<div id="customer-history-older" style="display:none"><div class="customer-timeline">' +
              renderEntries(older, false) + '</div></div>'
          : '';
        content.innerHTML = recentHtml + _historyMoreButton('customer', older.length) + olderHtml;
        var moreButton = document.getElementById('customer-history-more-btn');
        if (moreButton) moreButton.setAttribute('data-older-count', String(older.length));
      }

      function openCustomerHistoryTicket(index) {
        var ticket = _customerHistoryTickets[index];
        if (ticket) ticketLoadResult(ticket);
      }

      function _loadCustomerHistory(name) {
        var key = _customerNorm(name);
        var request = ++_customerHistoryRequest;
        var client = document.getElementById('customer-history-client');
        var alert = document.getElementById('customer-history-alert');
        var content = document.getElementById('customer-history-content');
        if (client) client.textContent = name;
        if (alert) alert.style.display = 'none';
        if (_customerHistoryCache[key]) {
          _renderCustomerHistory(_customerHistoryCache[key], name);
          return;
        }
        if (content) content.innerHTML = '<div class="customer-history-state">Loading customer history…</div>';
        _ticketHistoryData('client', name)
          .then(function (data) {
            if (request !== _customerHistoryRequest) return;
            if (data && data.results && data.results.length) _customerHistoryCache[key] = data;
            _renderCustomerHistory(data, name);
          })
          .catch(function () {
            if (request !== _customerHistoryRequest || !content) return;
            content.innerHTML = '<div class="customer-history-state">Customer history could not be loaded.</div>';
          });
      }

      function _resetKitHistory() {
        if (_kitHistoryTimer) clearTimeout(_kitHistoryTimer);
        _kitHistoryTimer = null;
        _kitHistoryRequest++;
        var name = document.getElementById('kit-history-name');
        var content = document.getElementById('kit-history-content');
        var stats = document.getElementById('kit-history-stats');
        if (name) name.textContent = 'Enter or select an item/kit — searches all tickets';
        if (stats) stats.style.display = 'none';
        var sales = document.getElementById('kit-history-sales');
        var tickets = document.getElementById('kit-history-tickets');
        var rate = document.getElementById('kit-history-rate');
        if (sales) sales.textContent = '—';
        if (tickets) tickets.textContent = '0';
        if (rate) rate.textContent = '—';
        if (content) content.innerHTML = '<div class="customer-history-state">Ticket totals by date will appear here.</div>';
      }

      function kitHistorySchedule(name, immediate) {
        if (_kitHistoryTimer) clearTimeout(_kitHistoryTimer);
        var value = (name || '').trim();
        if (value.length < 2) { _resetKitHistory(); return; }
        _kitHistoryTimer = setTimeout(function () {
          _kitHistoryTimer = null;
          _loadKitHistory(value);
        }, immediate ? 0 : 550);
      }

      function _renderKitHistory(data, requestedKit) {
        var name = document.getElementById('kit-history-name');
        var content = document.getElementById('kit-history-content');
        if (!content) return;
        if (!data || !data.ok) {
          var failedStats = document.getElementById('kit-history-stats');
          if (failedStats) failedStats.style.display = 'none';
          if (name) name.textContent = requestedKit;
          content.innerHTML = '<div class="customer-history-state">Item/kit history could not be loaded.</div>';
          return;
        }
        var wanted = _customerNorm(requestedKit);
        var tickets = (data.results || []).filter(function (ticket) {
          return _customerNorm(ticket.product) === wanted;
        });
        var stats = document.getElementById('kit-history-stats');
        var salesValue = document.getElementById('kit-history-sales');
        var ticketsValue = document.getElementById('kit-history-tickets');
        var rateValue = document.getElementById('kit-history-rate');
        var quantitySold = data.sales_quantity == null ? null : Number(data.sales_quantity);
        if (stats) stats.style.display = 'grid';
        if (salesValue) salesValue.textContent = quantitySold == null ? '—' :
          quantitySold.toLocaleString(undefined, { maximumFractionDigits: 1 });
        if (ticketsValue) ticketsValue.textContent = tickets.length.toLocaleString();
        if (rateValue) rateValue.textContent = quantitySold > 0
          ? ((tickets.length / quantitySold) * 100).toLocaleString(undefined, { maximumFractionDigits: 1 }) + '%'
          : '—';
        if (name) name.textContent = requestedKit + ' · ' + tickets.length +
          ' ticket' + (tickets.length === 1 ? '' : 's') + ' across all customers';
        if (!tickets.length) {
          content.innerHTML = '<div class="customer-history-state">No created tickets were found for this item/kit.</div>';
          return;
        }
        var groups = [], byDay = {};
        tickets.forEach(function (ticket) {
          var parsed = _ticketDate(ticket.date);
          var key = parsed
            ? parsed.getFullYear() + '-' + ('0' + (parsed.getMonth() + 1)).slice(-2) + '-' + ('0' + parsed.getDate()).slice(-2)
            : (String(ticket.date || '').trim() || 'undated');
          if (!byDay[key]) {
            byDay[key] = { date: ticket.date, tickets: [] };
            groups.push(byDay[key]);
          }
          byDay[key].tickets.push(ticket);
        });
        var recentGroups = groups.filter(function (group) { return _historyWithinThreeMonths(group.date); });
        var olderGroups = groups.filter(function (group) { return !_historyWithinThreeMonths(group.date); });
        function renderGroups(list, recentStyle) {
          return list.map(function (group) {
          var count = group.tickets.length;
          var numbers = group.tickets.slice(0, 5).map(function (ticket) {
            return _fmtTicketNum(ticket.ticketNum || '');
          }).filter(Boolean).join(', ');
          if (group.tickets.length > 5) numbers += ' +' + (group.tickets.length - 5);
          return '<div class="customer-timeline-item' + (recentStyle ? ' is-recent' : '') + '">' +
            '<div class="customer-timeline-date">' + _esc(_historyTicketDate(group.date)) + '</div>' +
            '<div class="customer-timeline-content">' +
              '<div class="customer-timeline-title">' + count + ' ticket' + (count === 1 ? '' : 's') + '</div>' +
              '<div class="customer-timeline-detail customer-timeline-issue">' + _esc(numbers) + '</div>' +
            '</div></div>';
          }).join('');
        }
        var recentHtml = recentGroups.length
          ? '<div class="customer-timeline">' + renderGroups(recentGroups, true) + '</div>'
          : '<div class="customer-history-state">No tickets in the last 3 months.</div>';
        var olderCount = olderGroups.reduce(function (total, group) { return total + group.tickets.length; }, 0);
        var olderHtml = olderGroups.length
          ? '<div id="kit-history-older" style="display:none"><div class="customer-timeline">' +
              renderGroups(olderGroups, false) + '</div></div>'
          : '';
        content.innerHTML = recentHtml + _historyMoreButton('kit', olderCount) + olderHtml;
        var moreButton = document.getElementById('kit-history-more-btn');
        if (moreButton) moreButton.setAttribute('data-older-count', String(olderCount));
      }

      function _loadKitHistory(name) {
        var key = _customerNorm(name);
        var request = ++_kitHistoryRequest;
        var label = document.getElementById('kit-history-name');
        var content = document.getElementById('kit-history-content');
        var stats = document.getElementById('kit-history-stats');
        if (label) label.textContent = name;
        if (stats) stats.style.display = 'none';
        if (_kitHistoryCache[key]) {
          _renderKitHistory(_kitHistoryCache[key], name);
          return;
        }
        if (content) content.innerHTML = '<div class="customer-history-state">Loading item/kit history…</div>';
        Promise.all([_ticketHistoryData('product', name), _itemSalesData(name)])
          .then(function (values) {
            if (request !== _kitHistoryRequest) return;
            var data = values[0] || { ok: false, results: [] };
            var sales = values[1] || { ok: false };
            data.sales_quantity = sales.ok ? sales.quantity_sold : null;
            if (data && data.results && data.results.length) _kitHistoryCache[key] = data;
            _renderKitHistory(data, name);
          })
          .catch(function () {
            if (request !== _kitHistoryRequest || !content) return;
            content.innerHTML = '<div class="customer-history-state">Item/kit history could not be loaded.</div>';
          });
      }

      function _itemSalesData(product) {
        return _apiGet('/api/item-sales?product=' + encodeURIComponent(product))
          .then(function (response) { return response.json(); })
          .catch(function () { return { ok: false, quantity_sold: null }; });
      }

      function _checkDuplicate() {
        var client  = _tv('ticket-client').trim();
        var product = _tv('ticket-product').trim();
        if (client.length < 2) { _hideDup(); return; }
        _apiGet('/api/tickets/search?q=' + encodeURIComponent(client) + '&limit=' + _searchLimit)
        .then(function(r) { return r.json(); })
        .then(function(d) {
          var w = document.getElementById('ticket-dup-warning');
          if (!w) return;
          var cl = client.toLowerCase();
          var pr = product.toLowerCase();
          var matches = ((d && d.results) || []).filter(function(t) {
            if ((t.client || '').trim().toLowerCase() !== cl) return false;
            if (pr && (t.product || '').trim().toLowerCase() !== pr) return false;
            return _dupClosedStatuses.indexOf((t.status || '').trim()) < 0; // blank = open
          });
          if (!matches.length) { w.style.display = 'none'; return; }
          _dupResultCache = matches;
          var lines = matches.slice(0, 4).map(function(t, i) {
            var label = (t.ticketNum || '?') + ' · ' + (t.product || 'no kit') +
                        ' · ' + (t.status || 'Open');
            return '<div style="cursor:pointer;padding:3px 0;text-decoration:underline;" ' +
                   'onclick="ticketLoadDupByIdx(' + i + ')">' + _esc(label) + '</div>';
          }).join('');
          w.innerHTML = '<strong>\u26A0 Possible duplicate</strong> \u2014 open ticket(s) for this client' +
            (product ? ' &amp; kit' : '') + ':' + lines;
          w.style.display = 'block';
        })
        .catch(function() { _hideDup(); });
      }

      // ── Draft autosave (per-user, server-side) ────────────────────────────
      function _collectDraft() {
        return {
          client:         _tv('ticket-client'),
          product:        _tv('ticket-product'),
          vehicle:        _tv('ticket-vehicle'),
          serial:         _tv('ticket-serial'),
          issue:          _tv('ticket-issue'),
          resolution:     _tv('ticket-resolution'),
          status:         _tv('ticket-status'),
          notes:          _tv('ticket-notes'),
        };
      }
      function _draftIsEmpty(d) {
        return !Object.keys(d).some(function(k) {
          return (d[k] || '').toString().trim() !== '';
        });
      }
      function draftSchedule() {
        // Only autosave brand-new (unsaved) tickets; edits persist explicitly.
        if (_ticketMode !== 'new') return;
        if (_draftTimer) clearTimeout(_draftTimer);
        _draftTimer = setTimeout(_saveDraft, 1200);
      }
      function _saveDraft() {
        var draft = _collectDraft();
        if (_draftIsEmpty(draft)) return;
        fetch(SERVER_URL + '/api/draft', {
          method: 'POST',
          headers: { 'Authorization': 'Bearer ' + _authBearer(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ draft: draft }),
        })
        .then(function(r) { return r.json(); })
        .then(function(d) { if (d && d.ok) _flashDraftIndicator(); })
        .catch(function() {});
      }
      function _clearDraft() {
        if (_draftTimer) { clearTimeout(_draftTimer); _draftTimer = null; }
        fetch(SERVER_URL + '/api/draft', {
          method: 'POST',
          headers: { 'Authorization': 'Bearer ' + _authBearer(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ clear: true }),
        }).catch(function() {});
      }
      function _offerDraftRestore() {
        // Check for an unsaved draft and, if present, show a non-intrusive
        // "Restore" link rather than auto-filling — keeps New Ticket blank.
        _pendingDraft = null;
        fetch(SERVER_URL + '/api/draft', { headers: { 'Authorization': 'Bearer ' + _authBearer() } })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (!d || !d.ok || !d.draft || _ticketMode !== 'new') return;
          if (_draftIsEmpty(d.draft)) return;
          _pendingDraft = d.draft;
          var msg = document.getElementById('ticket-msg');
          if (msg) msg.innerHTML = '<span style="cursor:pointer;color:#2563eb;text-decoration:underline;" ' +
            'onclick="applyDraftRestore()">\u21A9 Restore unsaved draft</span>';
        })
        .catch(function() {});
      }
      function applyDraftRestore() {
        if (!_pendingDraft) return;
        var dr = _pendingDraft;
        var map = {
          client: 'ticket-client', product: 'ticket-product', vehicle: 'ticket-vehicle',
          serial: 'ticket-serial', issue: 'ticket-issue',
          resolution: 'ticket-resolution', notes: 'ticket-notes',
        };
        for (var k in map) { var e = document.getElementById(map[k]); if (e) e.value = dr[k] || ''; }
        var s = document.getElementById('ticket-status'); if (s) s.value = dr.status || '';
        var msg = document.getElementById('ticket-msg'); if (msg) msg.textContent = 'Draft restored.';
        _pendingDraft = null;
        _resetApply();
        dupSchedule();
        _showCustomerSummaryForName(dr.client || '');
        customerHistorySchedule(dr.client || '', true);
        kitHistorySchedule(dr.product || '', true);
      }
      function _flashDraftIndicator() {
        var el = document.getElementById('ticket-draft-indicator');
        if (!el) return;
        el.textContent = '\u2713 Draft saved';
        el.style.opacity = '1';
        clearTimeout(el._fadeTimer);
        el._fadeTimer = setTimeout(function() { el.style.opacity = '0'; }, 1500);
      }

      var _searchTimer = null;
      function ticketSearchDebounced() {
        if (_searchTimer) clearTimeout(_searchTimer);
        _searchTimer = setTimeout(ticketSearch, 400);
      }

      function ticketSearch() {
        var q = (document.getElementById('ticket-search-input').value || '').trim();
        if (q.length < 2) {
          var res = document.getElementById('ticket-search-results');
          if (res) res.innerHTML = '<div style="padding:12px;color:#aaa;font-size:12px;text-align:center;">Type at least 2 characters</div>';
          return;
        }
        document.getElementById('ticket-search-results').style.display = 'block';
        document.getElementById('ticket-search-results').innerHTML =
          '<div style="padding:8px;color:#888;font-size:12px;">Searching...</div>';

        _apiGet('/api/tickets/search?q=' + encodeURIComponent(q) + '&limit=' + (_searchLimit||500))
          .then(function(r) { return r.json(); })
          .then(function(d) {
            var el = document.getElementById('ticket-search-results');
            if (!d.results || d.results.length === 0) {
              el.innerHTML = '<div style="padding:8px;color:#888;font-size:12px;">No results found.</div>';
              return;
            }
            var html2 = '';
            _searchResultCache = d.results;
            var _n = d.results.length;
            var _capped = (_n >= 500);
            html2 += '<div style="padding:4px 2px 8px;color:#888;font-size:11px;">' +
                     (_capped ? 'Showing first 500 matches (narrow your scan)'
                              : (_n + ' result' + (_n === 1 ? '' : 's'))) +
                     '</div>';
            d.results.forEach(function(r, i) {
              html2 += '<div class="ticket-result" onclick="_setActiveTab(\'create\');ticketLoadResultByIdx(' + i + ')">' +
                '<span style="font-weight:600;color:#464f56;">' + _esc(r.ticketNum||'') + '</span>' +
                '<span style="color:#666;margin-left:8px;">' + _esc(r.client||'') + '</span>' +
                '<span style="color:#999;margin-left:8px;font-size:11px;">' + _esc(r.product||'') + '</span>' +
                '</div>';
            });
            el.innerHTML = html2;
          })
          .catch(function() {
            document.getElementById('ticket-search-results').innerHTML =
              '<div style="padding:8px;color:#c00;font-size:12px;">Search failed.</div>';
          });
      }

      function doLogin() {
        var u = (document.getElementById('login-username').value || '').trim();
        var p = (document.getElementById('login-password').value || '').trim();
        if (!u || !p) { document.getElementById('login-error').textContent = 'Enter username and password.'; return; }
        document.getElementById('login-error').textContent = 'Logging in...';
        authLogin(u, p, function(err) {
          if (err) {
            document.getElementById('login-error').textContent = err;
          } else {
            hideLoginModal();
            document.getElementById('login-password').value = '';
          }
        });
      }

      function _populateTicketKitSelect() {
        var sel = document.getElementById('ticket-kit-select');
        if (!sel) return;
        var keys = Object.keys(KITS).sort();
        sel.innerHTML = '<option value="">-- Select a kit --</option>';
        keys.forEach(function(k) {
          var opt = document.createElement('option');
          opt.value = k; opt.textContent = k;
          if (k === activeKit) opt.selected = true;
          sel.appendChild(opt);
        });
      }

      function ticketPanelOpen() {
        if (!_authToken) return;
        showDailyView();
      }

      function _syncCustomerHistoryToggle() {
        var button = document.getElementById('ticket-history-toggle');
        if (!button) return;
        var open = document.body.classList.contains('ticket-history-open');
        button.setAttribute('aria-expanded', open ? 'true' : 'false');
        button.textContent = open ? 'Hide history ▶' : '◀ History';
        button.title = open ? 'Close ticket history' : 'Open ticket history';
      }

      function toggleCustomerHistory() {
        document.body.classList.toggle('ticket-history-open');
        _syncCustomerHistoryToggle();
      }

      function ticketPanelClose() {
        _leaveTicketChannel();
        document.getElementById('ticket-panel').style.display = 'none';
        document.body.classList.remove('ticket-panel-open');
        document.body.classList.remove('ticket-create-active');
        document.body.classList.remove('ticket-history-open');
        _syncCustomerHistoryToggle();
        if (_dailyRefreshTimer) { clearInterval(_dailyRefreshTimer); _dailyRefreshTimer = null; }
        _dailyViewMode = true;
      }

      // Search/dup results are cached here and referenced by index, so we never
      // serialize ticket data (which can contain apostrophes, quotes, or newlines)
      // into an inline onclick attribute -- that escaping break is what stopped
      // older, resolution-filled tickets from opening when clicked.
      var _searchResultCache = [];
      var _dupResultCache = [];
      function ticketLoadResultByIdx(i) { var r = _searchResultCache[i]; if (r) ticketLoadResult(r); }
      function ticketLoadDupByIdx(i)    { var r = _dupResultCache[i];    if (r) ticketLoadResult(r); }

      function ticketLoadResult(r) {
        _ticketMode   = 'edit';
        _ticketRowData = r;
        document.getElementById('ticket-num-display').textContent  = _fmtTicketNum(r.ticketNum || '');
        document.getElementById('ticket-tech').value        = r.tech || '';
        document.getElementById('ticket-client').value      = r.client || '';
        document.getElementById('ticket-product').value     = r.product || '';
        document.getElementById('ticket-vehicle').value     = r.vehicle || '';
        document.getElementById('ticket-issue').value       = r.issue || '';
        document.getElementById('ticket-resolution').value  = r.resolution || '';
        document.getElementById('ticket-status').value      = r.status || '';
        document.getElementById('ticket-serial').value      = r.serial || '';
        document.getElementById('ticket-notes').value       = r.notes || '';
        document.getElementById('ticket-save-btn').textContent = 'Update Ticket';
        document.getElementById('ticket-search-results').style.display = 'none';
        document.getElementById('ticket-msg').textContent  = '';
        _hideDup();
        _resetApply();
        // Load the kit if it matches
        if (r.product && KITS[r.product]) selectKit(r.product);
        _ticketDirty = false;
        _hideStaleBanner();
        _joinTicketChannel(r.ticketNum);
        _showCustomerSummaryForName(r.client || '');
        customerHistorySchedule(r.client || '', true);
        kitHistorySchedule(r.product || '', true);
      }


      // Convert local troubleshooting JSON to node tree format.
      // Mirrors the CSV buildChoices() contract the renderer expects:
      //   node.text  = the heading shown when you're ON the node (the question)
      //   node.label = the button text shown in the PARENT menu (the issue)
      // The JSON stores the heading under `question` and the menu label under
      // `issue`; earlier this put `issue` into `text`, so a branching node's
      // heading echoed the button just clicked (e.g. "No audio" → "No audio").
      function _buildNodesFromLocal(issues) {
        if (!issues || !issues.length) return [{ id: 'start', type: 'question', text: 'What is the issue?', choices: [] }];

        function childrenOf(parentTitle) {
          return issues
            .filter(function (r) { return (r.parent || '').trim() === parentTitle.trim(); })
            .map(function (r) {
              var label    = (r.issue || '').trim();
              var rawQ     = (r.question || '').trim();
              var steps    = (r.steps || []).filter(function (s) { return s != null && String(s).trim() !== ''; });
              var hasKids  = issues.some(function (c) { return (c.parent || '').trim() === label; });
              var hasSteps = steps.length > 0;

              if (hasKids && !hasSteps) {
                // Pure question node — the heading is its own question, not its label
                return { type: 'question', text: rawQ || 'Which of these applies?', label: label, choices: childrenOf(label) };
              } else if (hasKids && hasSteps) {
                // Steps first, then sub-choices
                return { type: 'fix-and-question', text: rawQ || label, label: label, steps: steps, choices: childrenOf(label) };
              } else {
                // Leaf fix
                return { type: 'fix', text: label, label: label, steps: hasSteps ? steps : ['See manual for details'] };
              }
            });
        }

        return [{ id: 'start', type: 'question', text: 'What is the issue?', choices: childrenOf('') }];
      }


      // ── Daily Tickets View ────────────────────────────────────────────────
      let _dailyViewMode  = true;   // true = daily view, false = create/edit form
      let _dailyRefreshTimer = null;
      let _pollSecs       = 60;   // Today-window refresh seconds; synced from server poll_interval
      let _dailyTickets   = [];
      let _dailyFilter    = '';

      // Tickets just saved through the app may not be in the server store/sheet
      // for a moment. We show them in Today right away and keep showing them
      // (replacing the server copy) until the server catches up or a short TTL.
      var _pendingOptimistic = {};      // ticketNum -> { t: ticket, ts: ms }
      var _OPT_TTL = 30000;
      function _todayISO() {
        var d = new Date();
        return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) +
               '-' + ('0' + d.getDate()).slice(-2);
      }
      function _ticketKey(t) {
        var raw = String((t && (t.ticketNum || t.ticket)) || '');
        var digits = raw.replace(/\D/g, '');
        return digits || raw;
      }
      function _upsertDailyTicket(t) {
        var num = _ticketKey(t), i = -1;
        for (var k = 0; k < _dailyTickets.length; k++) {
          if (_ticketKey(_dailyTickets[k]) === num) { i = k; break; }
        }
        if (i >= 0) _dailyTickets[i] = t; else _dailyTickets.push(t);
      }
      function _mergePending(list) {
        var now = Date.now(), pos = {};
        list.forEach(function(x, i) { pos[_ticketKey(x)] = i; });
        for (var num in _pendingOptimistic) {
          var p = _pendingOptimistic[num];
          if (now - p.ts > _OPT_TTL) { delete _pendingOptimistic[num]; continue; }
          if (pos[num] != null) list[pos[num]] = p.t;   // show our copy until TTL
          else list.push(p.t);                          // server doesn't have it yet
        }
        return list;
      }
      var EMPTY_STATE_HTML =
        '<div class="empty-state">' +
        '<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">' +
        '<path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2"/>' +
        '<rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 12h6M9 16h4"/></svg>' +
        '<div style="font-size: 15px; font-weight: 600; color: #aaa">Select a kit to get started</div></div>';

      function showDailyView() {
        document.getElementById('ticket-panel').style.display = 'flex';
        document.body.classList.add('ticket-panel-open');
        _populateTicketKitSelect();
        _setActiveTab('today');
        loadTodayTickets();
        _scheduleDailyRefresh();
      }

      // Refresh the Today window on the server's configured poll interval (the
      // "Poll interval" setting, seconds, min 5). Re-armed whenever the value
      // changes so settings edits take effect within one cycle.
      function _scheduleDailyRefresh() {
        if (_dailyRefreshTimer) clearInterval(_dailyRefreshTimer);
        _dailyRefreshTimer = setInterval(function() {
          if (_dailyViewMode) loadTodayTickets(true);
        }, Math.max(2, _pollSecs) * 1000);
      }

      function showNewTicketForm() {
        _setActiveTab('create');
        ticketNew();
      }

      function backToDailyView() {
        _setActiveTab('today');
      }

      function _setActiveTab(tab) {
        // Remember the last list pane so exit/save/update return to where the
        // user came from (Today by default, Search if they were searching).
        if (tab === 'today' || tab === 'search') _ticketReturnTab = tab;
        document.body.classList.toggle('ticket-create-active', tab === 'create');
        var views = { today: 'daily-view', search: 'search-view', create: 'ticket-form-view' };
        var tabs  = { today: 'panel-tab-today', search: 'panel-tab-search', create: 'panel-tab-create' };
        for (var key in views) {
          var v = document.getElementById(views[key]);
          var t = document.getElementById(tabs[key]);
          if (v) v.style.display = (key === tab) ? 'flex' : 'none';
          if (t) {
            if (key === tab) t.classList.add('panel-tab-active');
            else t.classList.remove('panel-tab-active');
          }
        }
        _dailyViewMode = (tab === 'today');
        if (tab === 'today') loadTodayTickets(true);
        if (tab === 'search') setTimeout(function() {
          var inp = document.getElementById('ticket-search-input');
          if (inp) inp.focus();
        }, 50);
      }

      let _dailyDays = 1; // date range in days

      // Persist today's tickets so a full page reload renders instantly from
      // cache (then the background fetch tops it up with any new entries).
      function _saveDailyCache() {
        try {
          if (typeof localStorage === 'undefined') return;
          localStorage.setItem(DAILY_CACHE_KEY, JSON.stringify({
            days: _dailyDays, ts: Date.now(), tickets: _dailyTickets
          }));
        } catch (e) { /* storage unavailable — ignore */ }
      }
      function _loadDailyCache() {
        try {
          if (typeof localStorage === 'undefined') return;
          var raw = localStorage.getItem(DAILY_CACHE_KEY);
          if (!raw) return;
          var c = JSON.parse(raw);
          // only reuse a fresh cache for the same day-range
          if (!c || c.days !== _dailyDays) return;
          if (!c.ts || (Date.now() - c.ts) > 24*60*60*1000) return;
          if (Array.isArray(c.tickets)) _dailyTickets = c.tickets;
        } catch (e) { /* ignore */ }
      }

      function loadTodayTickets(silent) {
        var el = document.getElementById('daily-tickets-list');
        // Restore persisted cache so a fresh page load shows tickets immediately
        if (_dailyTickets.length === 0) _loadDailyCache();
        // Show cached immediately - no flash
        if (_dailyTickets.length > 0) {
          renderDailyTickets();
        } else if (!silent && el) {
          el.innerHTML = '<div style="padding:20px;text-align:center;color:#888;font-size:13px;">Loading...</div>';
        }
        _apiGet('/api/tickets/recent?days=' + _dailyDays)
        .then(function(r) { return r.json(); })
        .then(function(d) {
          // sync the Today-window refresh rate to its own setting (separate from
          // the server's sheet poll interval)
          var ps = parseInt(d.pane_refresh, 10);
          if (ps && ps !== _pollSecs) { _pollSecs = ps; if (_dailyRefreshTimer) _scheduleDailyRefresh(); }
          _dailyTickets = _mergePending(d.results || []);
          _saveDailyCache();
          renderDailyTickets();
          var ts = document.getElementById('daily-last-updated');
          if (ts) {
            var now = new Date();
            ts.textContent = 'Updated ' + now.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
          }
        })
        .catch(function() {
          if (!silent && el && _dailyTickets.length === 0) {
            el.innerHTML = '<div style="padding:20px;text-align:center;color:#888;font-size:13px;">Could not load tickets.</div>';
          }
        });
      }

      function onDaysFilterChange(val) {
        _dailyDays = parseInt(val) || 1;
        _dailyTickets = [];
        var el = document.getElementById('daily-tickets-list');
        if (el) el.innerHTML = '<div style="padding:20px;text-align:center;color:#888;font-size:13px;">Loading...</div>';
        loadTodayTickets(false);
      }

      // Parse a stored ticket date into a *local* day (time stripped), tolerant of
      // the various formats the sheet emits: ISO date / ISO datetime, US M/D/Y, and
      // JS date strings. Date-only and ISO values are built from their written
      // calendar day so they never shift across our timezone. Returns a Date or null.
      function _ticketDate(d) {
        if (!d) return null;
        var s = String(d).trim();
        var m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);          // ISO date or datetime
        if (m) return new Date(+m[1], +m[2] - 1, +m[3]);
        var u = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);    // US M/D/Y
        if (u) return new Date(+u[3], +u[1] - 1, +u[2]);
        var dt = new Date(s);                                 // JS string / fallback
        if (isNaN(dt.getTime())) return null;
        return new Date(dt.getFullYear(), dt.getMonth(), dt.getDate());
      }
      // "Wed Jun 10 2026" (no time).
      function _fmtTicketDate(d) {
        var x = _ticketDate(d);
        return x ? x.toDateString() : (d ? String(d) : '');
      }
      function _dayKey(dt) {
        var mo = dt.getMonth() + 1, da = dt.getDate();
        return dt.getFullYear() + '-' + (mo < 10 ? '0' : '') + mo + '-' + (da < 10 ? '0' : '') + da;
      }
      function _ticketDayKey(d) {
        var x = _ticketDate(d);
        return x ? _dayKey(x) : '';
      }
      // Tickets within the selected day range, after the text filter, in store order.
      // Used by both the renderer and the click handler so their indexes line up.
      function _filteredDailyTickets() {
        var cutoffDt = new Date();
        cutoffDt.setDate(cutoffDt.getDate() - (_dailyDays - 1));
        var cutoff = _dayKey(cutoffDt);
        var run = '';
        for (var i = 0; i < _dailyTickets.length; i++) {
          var d = _dailyTickets[i].date;
          if (d) run = d;
          _dailyTickets[i]._d = d || run;       // carry the last known date into blanks
        }
        var q = (_dailyFilter || '').toLowerCase();
        return _dailyTickets.filter(function(t) {
          var k = _ticketDayKey(t.date || t._d || '');
          if (!k || k < cutoff) return false;
          if (q) {
            return (t.tech||'').toLowerCase().includes(q) ||
                   (t.client||'').toLowerCase().includes(q) ||
                   (t.product||'').toLowerCase().includes(q) ||
                   (t.ticketNum||'').toLowerCase().includes(q);
          }
          return true;
        });
      }

      function renderDailyTickets() {
        var el = document.getElementById('daily-tickets-list');
        if (!el) return;
        var tickets = _filteredDailyTickets();
        if (!tickets.length) {
          var label = _dailyDays === 1 ? 'today' : 'in the last ' + _dailyDays + ' days';
          el.innerHTML = '<div style="padding:24px;text-align:center;color:#888;font-size:13px;">No tickets ' + label + '.</div>';
          return;
        }
        var statusColors = {
          'Resolved': '#10b981',
          'W.O.E.': '#6366f1',
          'No Solution': '#ef4444',
          'Waiting for customer feedback': '#f59e0b',
          'RMA/Return': '#f97316',
          'RMA - ADV REP REQ GROUND': '#f97316',
          'RMA - ADV REP REQ OVERNIGHT': '#dc2626',
          'DISTRIBUTOR': '#8b5cf6',
          'Field Destroy': '#374151',
        };
        // Reverse: most recent first
        tickets = tickets.slice().reverse();
        var html2 = '';
        var lastDateSep = '';
        tickets.forEach(function(t) {
          var tDate = _fmtTicketDate(t.date || t._d || '');
          if (tDate && tDate !== lastDateSep) {
            lastDateSep = tDate;
            html2 += '<div style="font-size:10px;font-weight:700;color:#bbb;text-transform:uppercase;' +
              'letter-spacing:0.07em;padding:10px 4px 3px;margin-top:2px;">' + tDate + '</div>';
          }
          var color = statusColors[t.status] || '#9ca3af';
          var statusDot = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + color + ';margin-right:6px;flex-shrink:0;"></span>';
          html2 += '<div class="daily-ticket-card" onclick="_openDailyTicketByIdx(' + tickets.indexOf(t) + ')">' +
            '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:3px;">' +
              '<span style="font-weight:700;color:#464f56;font-size:12px;">' + _esc(t.ticketNum ? _fmtTicketNum(t.ticketNum) : '—') + '</span>' +
              '<span style="font-size:11px;color:#aaa;">' + _esc(t.tech||'') + '</span>' +
            '</div>' +
            '<div style="font-size:13px;color:#222;font-weight:500;margin-bottom:2px;">' + _esc(t.client||'—') + '</div>' +
            '<div style="font-size:11px;color:#888;margin-bottom:5px;">' + _esc(t.product||'') + (t.vehicle ? ' · ' + _esc(t.vehicle) : '') + '</div>' +
            '<div style="display:flex;align-items:center;">' +
              statusDot +
              '<span style="font-size:11px;color:#666;">' + _esc(t.status||'Open') + '</span>' +
            '</div>' +
          '</div>';
        });
        el.innerHTML = html2;
      }


      function _openDailyTicketByIdx(idx) {
        var filtered = _filteredDailyTickets().slice().reverse();
        var t = filtered[idx];
        if (t) openTicketFromDaily(t);
      }

      function openTicketFromDaily(t) {
        _setActiveTab('create');
        ticketLoadResult(t);
      }

      async function loadFromSheets(silent) {
        // Data loads through server proxy - no client-side config needed

        const savedKit   = activeKit;
        const savedTab   = activeTab;
        const savedStack = nodeStack.slice();

        let _hadCache = false;
        if (!silent) {
          _hadCache = loadFromCache();
          if (!_hadCache) showLoadingState('Loading kit data...');
        }

        try {
          const _token = localStorage.getItem('zz2_auth_token') || '';
          const _bust = '&_=' + Date.now();
          const _headers = { 'Authorization': 'Bearer ' + _token };
          // Fetch both data sources - server returns JSON (local) or CSV (Google Sheets)
          const [kitsResp, tsResp] = await Promise.all([
            fetch(API_URL + '?action=csv&sheet=kits' + _bust,            {cache:'no-cache', headers: _headers}),
            fetch(API_URL + '?action=csv&sheet=troubleshooting' + _bust, {cache:'no-cache', headers: _headers}),
          ]);

          if (!kitsResp.ok) throw new Error('Kits data: HTTP ' + kitsResp.status);
          if (!tsResp.ok)   throw new Error('Troubleshooting data: HTTP ' + tsResp.status);

          const kitsContentType = kitsResp.headers.get('Content-Type') || '';
          const tsContentType   = tsResp.headers.get('Content-Type') || '';
          const dataSource      = kitsResp.headers.get('X-Data-Source') || 'google';

          let kitsText, tsText;

          // If server returns JSON (local files), handle differently
          if (kitsContentType.includes('application/json')) {
            const kitsJson = await kitsResp.json();
            const tsJson   = await tsResp.json();
            // Local JSON is already parsed - apply directly
            _applyLocalCatalog(kitsJson, tsJson);
            kitsText = JSON.stringify(kitsJson);
            tsText   = JSON.stringify(tsJson);
            console.log('[ZZ2] Loaded from local files (' + Object.keys(KITS).length + ' kits)');
          } else {
            // CSV from Google Sheets - use existing parser
            kitsText = await kitsResp.text();
            tsText   = await tsResp.text();
            const built = buildKits(parseCSV(kitsText), parseCSV(tsText));
            KITS = built.kits; IMAGES = built.images;
            console.log('[ZZ2] Loaded from Google Sheets (' + Object.keys(KITS).length + ' kits)');
          }

          saveToCache(kitsText, tsText,
                      kitsContentType.includes('application/json') ? 'json' : 'csv');

          applyKitExtras();

          if (savedKit && KITS[savedKit]) {
            // Resyncing — restore position
            activeKit = savedKit; activeTab = savedTab;
            nodeStack = rebuildStack(savedStack, KITS[savedKit]);
            buildSidebar(); renderKit();
          } else if (_hadCache) {
            // Cache had already populated the UI — refresh sidebar and re-render
            // current kit if one is active so fresh data shows without ReSync
            buildSidebar();
            if (activeKit && KITS[activeKit]) {
              nodeStack = rebuildStack(nodeStack, KITS[activeKit]);
              renderKit();
            }
          } else {
            // First ever load, no cache — show empty state
            hideLoadingState(); buildSidebar();
          }
          updateLastRefreshed();

        } catch(err) {
          if (KITS && Object.keys(KITS).length > 0) {
            updateLastRefreshed();
          } else {
            showErrorState(String(err));
          }
        }
      }

      function showLoadingState(msg) {
        document.getElementById('content').innerHTML =
          `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:60vh;gap:16px;color:#888;">
            <div class="loading-spinner"></div>
            <div style="font-size:15px;">${msg}</div>
           </div>`;
        document.getElementById('sidebar').innerHTML =
          '<div style="padding:16px;color:#888;font-size:13px;">Loading...</div>';
      }

      function hideLoadingState() {
        document.getElementById('content').innerHTML =
          '<div style="display:flex;align-items:center;justify-content:center;height:60vh;color:#888;font-size:15px;">Select a kit from the sidebar</div>';
      }

      function showErrorState(msg) {
        document.getElementById('content').innerHTML =
          `<div style="padding:32px;max-width:600px;margin:0 auto;">
            <div style="background:#fff3cd;border:1px solid #ffcd00;border-radius:8px;padding:20px;">
              <h3 style="margin:0 0 12px;color:#856404;">⚠️ Could not load kit data</h3>
              <p style="margin:0 0 12px;color:#666;">${msg}</p>
              <p style="margin:0 0 8px;color:#666;font-size:13px;">If you are using Edge, try allowing mixed content or switching to Chrome.</p>
              <p style="margin:0 0 16px;color:#666;font-size:13px;">Make sure the sheet tabs are published as CSV and the URLs in SHEETS_CONFIG are correct.</p>
              <button onclick="loadFromSheets()" style="background:#464f56;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;">Retry</button>
            </div>
           </div>`;
      }

      function showConfigPrompt() {
        document.getElementById('content').innerHTML =
          `<div style="padding:32px;max-width:700px;margin:0 auto;">
            <div style="background:#e8f4f8;border:1px solid #bee5eb;border-radius:8px;padding:24px;">
              <h3 style="margin:0 0 16px;color:#0c5460;">📋 Setup Required</h3>
              <p style="margin:0 0 12px;color:#444;">The Google Sheets URLs have not been configured yet.</p>
              <ol style="color:#444;line-height:2;margin:0 0 16px;padding-left:20px;">
                <li>Open your <b>ZZ2 Tech Support</b> Google Sheet</li>
                <li>Go to <b>File → Share → Publish to web</b></li>
                <li>Publish the <b>Kits</b> tab as <b>CSV</b> — copy the URL</li>
                <li>Publish the <b>Troubleshooting</b> tab as <b>CSV</b> — copy the URL</li>
                <li>Open this HTML file in a text editor and paste both URLs into <code>SHEETS_CONFIG</code></li>
              </ol>
            </div>
           </div>`;
      }

      function updateLastRefreshed(date) {
        const el = document.getElementById('last-refresh');
        if (el) el.textContent = 'Updated ' + (date || new Date()).toLocaleTimeString();
      }


      const KIT_EXTRA = {
        "IT3-NBT": {
          dip_switches: [{
            label: "IT3-NBT — Vehicle Selection",
            count: 8,
            rows: [
              { desc: "DIP 1 — Keep ON always", dips: ["ON","—","—","—","—","—","—","—"] },
              { desc: "DIP 2 — Keep ON always", dips: ["—","ON","—","—","—","—","—","—"] },
              { desc: "8.8″ screen", dips: ["ON","ON","ON","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "10.2″ or 6.5″ screen", dips: ["ON","ON","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "DIP 4–8 — Not used (default OFF)", dips: ["—","—","—","OFF","OFF","OFF","OFF","OFF"] },
            ],
            notes: "Remove CAN plug while adjusting DIP switches. Reconnect after."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS Cable", desc: "Connects from OE radio LVDS cable to IT3-NBT module" },
              { label: "Main Harness", desc: "Plugs into OE BMW radio main connector (Quadlock)" },
              { label: "Fiber Optic", desc: "Must be removed from OE radio and moved to ZZ2 T-Harness provided plug" },
              { label: "MIC T-Harness", desc: "Included — required for microphone. DIP7 OFF = OEM mic, DIP7 ON = external mic" },
              { label: "FAKRA Antenna", desc: "WiFi/BT antenna — route outside metal for best signal" },
              { label: "AV/USB Cable", desc: "USB input for phone connection and updates" },
            ],
            notes: "GND wire: CONNECT for CCC/NBT. DISCONNECT for CIC."
          }]
        },
        "IT3-CIC": {
          dip_switches: [{
            label: "IT3-CIC — Vehicle Selection",
            count: 8,
            rows: [
              { desc: "DIP 1 — Not used (default OFF)", dips: ["OFF","—","—","—","—","—","—","—"] },
              { desc: "DIP 2 — Series 6 ONLY", dips: ["—","OFF","—","—","—","—","—","—"] },
              { desc: "8.8″ screen", dips: ["OFF","OFF","ON","ON","OFF","OFF","OFF","OFF"] },
              { desc: "10.2″ or 6.5″ screen", dips: ["OFF","OFF","OFF","ON","OFF","OFF","OFF","OFF"] },
              { desc: "DIP 4 — Default ON (required)", dips: ["—","—","—","ON","—","—","—","—"] },
              { desc: "DIP 7 — External MIC (ON) / OEM MIC (OFF)", dips: ["—","—","—","—","—","—","OFF","—"] },
              { desc: "Series 6 Low Speed CAN (try first)", dips: ["OFF","ON","ON","ON","OFF","OFF","OFF","OFF"] },
              { desc: "Series 6 High Speed CAN (if Low Speed fails)", dips: ["ON","ON","ON","OFF","OFF","OFF","OFF","OFF"] },
            ],
            notes: "Series 6: start with Low Speed CAN (DIP2+3+4 ON). If no switch via MENU, do CAN reset (lock all doors, wait 5 min). If still no switch, try High Speed CAN (DIP1+2+3 ON)."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS IN Cable", desc: "From OE radio LVDS to IT3-CIC module input" },
              { label: "LVDS OUT Cable", desc: "From IT3-CIC module to OE screen" },
              { label: "iDrive T-Harness", desc: "Try Low Speed (green) connection first; if no switch try High Speed (violet)" },
              { label: "CAN Harness", desc: "Included in kits from 10/18/21+" },
              { label: "Main Harness", desc: "Plugs in-line with OE BMW radio main connector" },
              { label: "MIC T-Harness", desc: "Required for microphone. DIP7 OFF = OEM mic" },
              { label: "AUX", desc: "Radio MUST be on AUX mode for audio. Connect AUX cable to BMW AUX port." },
            ]
          }]
        },
        "IT3-EVO": {
          switch_button: "MENU button",
          audio_method: "OEM BT Channel — ZZPLAY → Setup → scroll to bottom → 'Use Car's BT Channel' ON",
          dip_switches: [{
            label: "IT3-EVO — Screen Size",
            count: 8,
            rows: [
              { desc: "6.5″ screen", dips: ["OFF","ON","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "8.8″ screen (default)", dips: ["OFF","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "10.25″ screen", dips: ["ON","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
            ],
            notes: "Do NOT use the Back button on EVO — it restarts the unit. Use button 1 (hold 3 sec)."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS OUT Cable", desc: "Connects to OE EVO screen at RADIO connector" },
              { label: "Main Harness", desc: "Plugs into OE BMW EVO radio connector" },
              { label: "Fiber Optic", desc: "Must be moved to ZZ2 T-Harness — no audio without this" },
              { label: "WiFi/BT Antenna (FAKRA)", desc: "Route externally" },
            ]
          }]
        },
        "ITB-EVO": {
          dip_switches: [{
            label: "ITB-EVO — Screen Size",
            count: 8,
            rows: [
              { desc: "6.5″ screen", dips: ["OFF","ON","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "8.8″ screen (default)", dips: ["OFF","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "10.25″ screen", dips: ["ON","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
            ],
            notes: "DIFFERENT kit from IT3-EVO — different PCB and harness. Use ITB-EVO manual only."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS OUT Cable", desc: "Connects module to OE EVO screen" },
              { label: "Main Harness", desc: "Plugs into OE BMW EVO radio connector" },
              { label: "Fiber Optic", desc: "Must be moved to ZZ2 T-Harness for audio" },
              { label: "Internal Ribbon Cables", desc: "All ribbons must be fully seated and latched — ITB involves screen modification" },
              { label: "WiFi/BT Antenna (SMB)", desc: "Route externally" },
            ]
          }]
        },
        "IT3-NTG5": {
          dip_switches: [{
            label: "IT3-NTG5 — DIP by Model and Screen Size",
            count: 8,
            rows: [
              { desc: "A/B/CLA/GLA (W176/W246), CLS, GLE/GLS, SLC, E (W212) — 7″ / 8″", dips: ["OFF","OFF","OFF","ON","OFF","OFF","OFF","ON"] },
              { desc: "C (W205), GLC, V (W447), X (W470) — 7″", dips: ["OFF","OFF","OFF","OFF","OFF","OFF","OFF","ON"] },
              { desc: "C (W205), GLC, V, X — 8.4″", dips: ["OFF","OFF","OFF","OFF","ON","ON","OFF","ON"] },
              { desc: "S-Class (W222) — 12″", dips: ["OFF","OFF","OFF","OFF","OFF","ON","OFF","ON"] },
            ],
            notes: "After any DIP change: power cycle the unit."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS Y-Cable (male)", desc: "Connects to BLUE LVDS connector behind the radio" },
              { label: "LVDS Y-Cable (female)", desc: "Connects to original LVDS cable removed from radio" },
              { label: "Main Harness", desc: "Plugs into OE Mercedes Quadlock radio connector" },
              { label: "RCA/USB Cable", desc: "USB input for phone and updates" },
              { label: "USB to AUX Adapter", desc: "For AUX audio output to OE radio AUX input" },
              { label: "FAKRA Antenna", desc: "WiFi/BT antenna" },
            ]
          }]
        },
        "IT3-NTG45": {
          dip_switches: [{
            label: "IT3-NTG45 — DIP by Screen Size",
            count: 8,
            rows: [
              { desc: "5.8″ screen — Type 1 (try first)", dips: ["OFF","OFF","ON","OFF","ON","OFF","OFF","ON"] },
              { desc: "5.8″ screen — Type 2 (if Type 1 fails)", dips: ["OFF","OFF","ON","OFF","ON","ON","OFF","ON"] },
              { desc: "7″ screen", dips: ["OFF","OFF","ON","OFF","OFF","ON","OFF","ON"] },
            ],
            notes: "After any DIP change: RESET POWER PLUG (physically unplug and replug power), not just CAN reset."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS Y-Cable (male)", desc: "Connects to GRAY LVDS connector behind the radio" },
              { label: "LVDS Y-Cable (female)", desc: "Connects to original LVDS cable from radio" },
              { label: "Main Harness", desc: "Plugs into OE Mercedes Quadlock connector" },
              { label: "USB to AUX Adapter", desc: "For AUX audio to OE radio AUX input" },
              { label: "FAKRA Antenna", desc: "WiFi/BT antenna" },
            ]
          }]
        },
        "ITZ-NTG4-A": {
          dip_switches: [{
            label: "ITZ-NTG4-A — DIP by Screen Size",
            count: 5,
            rows: [
              { desc: "5.8″ screen — Type 1 (try first)", dips: ["ON","ON","ON","ON","OFF"] },
              { desc: "5.8″ screen — Type 2 (if Type 1 fails)", dips: ["ON","ON","ON","ON","ON"] },
              { desc: "7″ screen — Type 1 (try first)", dips: ["ON","ON","ON","OFF","OFF"] },
              { desc: "7″ screen — Type 2 (if Type 1 fails)", dips: ["ON","ON","ON","OFF","ON"] },
            ],
            notes: "Disconnect power before adjusting DIP switches. LVDS connectors on module: dimples must face BOTTOM of unit."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS IN Cable", desc: "Female end connects to LVDS cable removed from OE radio. Dimples face BOTTOM of unit." },
              { label: "LVDS OUT Cable", desc: "Male end connects to OE screen. Dimples face BOTTOM." },
              { label: "Main Harness", desc: "Plugs into OE Mercedes radio connector" },
              { label: "Fiber Optic", desc: "Remove from OE radio plug and connect to T-Harness provided plug — REQUIRED for audio" },
              { label: "Reverse wire (brown)", desc: "If using wire trigger: connect to reverse light. Set Reversing Mode to 12V Active." },
            ],
            notes: "Radio must be on Bluetooth Audio or AUX for sound. If using BT: Link Split Audio must be ON in ZZ2 settings."
          }]
        },
        "ITZ-NTG3-A": {
          dip_switches: [{
            label: "ITZ-NTG3-A — All Vehicles",
            count: 5,
            rows: [
              { desc: "8″ screen — all NTG3-A vehicles", dips: ["OFF","OFF","OFF","OFF","OFF"] },
            ],
            notes: "Disconnect power before adjusting DIP switches."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS IN Cable", desc: "From OE LVDS (radio side) to module. Video connector orientation matters." },
              { label: "LVDS OUT Cable", desc: "From module to OE screen" },
              { label: "Main Harness", desc: "Plugs into OE Mercedes radio connector" },
              { label: "BT Mic", desc: "Included. S-Class requires S-Class specific MIC T-Harness — standard MIC does not work." },
              { label: "AUX (radio)", desc: "Radio MUST be on AUX mode — no BT streaming on NTG3 generation" },
            ]
          }]
        },
        "ITZ-NTG35-A": {
          dip_switches: [{
            label: "ITZ-NTG35-A — Screen Size",
            count: 5,
            rows: [
              { desc: "8″ screen — Type 1 (try first)", dips: ["OFF","OFF","OFF","ON","OFF"] },
              { desc: "8″ screen — Type 2 (if Type 1 fails)", dips: ["OFF","OFF","OFF","OFF","ON"] },
            ],
            notes: "Disconnect power before adjusting DIP switches. AMI Adapter (AUX) included — connect to vehicle AMI port."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS-Y Cable", desc: "Male end to radio LVDS; female end receives cable from OE radio" },
              { label: "AMI Adapter (AUX)", desc: "Connect to vehicle AMI port for AUX audio. Radio on AUX mode." },
              { label: "Main Harness", desc: "Plugs into OE Mercedes radio connector" },
              { label: "BT Mic", desc: "Included for phone calls" },
            ]
          }]
        },
        "IT3-LEX15": {
          dip_switches: [{
            label: "IT3-LEX15 — DIP Settings",
            count: 6,
            rows: [
              { desc: "DIP 1 and 2 always ON for LEX15", dips: ["ON","ON","—","—","—","—"] },
              { desc: "7″–8″–10″–12″ split screens (up to 2015)", dips: ["ON","ON","OFF","OFF","OFF","OFF"] },
              { desc: "IS screen 7″ only (2013–2016)", dips: ["ON","ON","ON","ON","OFF","OFF"] },
              { desc: "10″ or some 12″ screens (2018+)", dips: ["ON","ON","OFF","ON","OFF","OFF"] },
              { desc: "12″ screens only (2016+)", dips: ["ON","ON","ON","OFF","OFF","OFF"] },
            ],
            notes: "Joystick equipped: set Setup → Remote Lever. Touchpad: set Setup → Mini Touch Pad. Screen Correction T-Harness MUST be connected to screen."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "GVIF Y-Cable (OLD type)", desc: "OE screen GVIF cable connects here; other end to OE Lexus radio" },
              { label: "Joystick T-Harness (15H-KT)", desc: "Connects to OE joystick plug if vehicle has joystick. NOT used for touchpad vehicles." },
              { label: "Screen Correction T-Harness", desc: "MUST connect to screen. Disconnect GRAY connector if screen flashes. Disconnect GREEN if RVC abnormal." },
              { label: "Audio Ground Wire", desc: "MUST connect to chassis ground if using AUX for audio on Lexus." },
              { label: "AUX Cable (3.5mm)", desc: "Connect to factory AUX port for AUX audio" },
            ]
          }]
        },
        "IT3-LEX17": {
          dip_switches: [{
            label: "IT3-LEX17 — DIP Settings",
            count: 6,
            rows: [
              { desc: "DIP 1 always ON for LEX17", dips: ["ON","OFF","—","—","—","—"] },
              { desc: "7″–8″–10″–12″ split screens (up to 2015)", dips: ["ON","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "IS screen 7″ only (2013–2016)", dips: ["ON","OFF","ON","ON","OFF","OFF"] },
              { desc: "10″ or some 12″ screens (2018+)", dips: ["ON","OFF","OFF","ON","OFF","OFF"] },
              { desc: "12″ screens only (2016+)", dips: ["ON","OFF","ON","OFF","OFF","OFF"] },
            ],
            notes: "IS models: key-ways may need to be shaved off the Blue T-Harness. Screen Correction T-Harness MUST be connected."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "GVIF Y-Cable (OLD type)", desc: "OE screen GVIF cable; other end to OE Lexus radio" },
              { label: "Screen Correction T-Harness", desc: "MUST connect to screen — prevents flickering" },
              { label: "Audio Ground Wire", desc: "MUST connect to chassis ground when using AUX" },
              { label: "Touchpad Connection", desc: "For touchpad vehicles: A connector plugs into C connector (see manual diagram)" },
            ]
          }]
        },
        "IT3-PCM31A": {
          dip_switches: [{
            label: "IT3-PCM31A — Internal PCB Modification",
            count: 1,
            rows: [
              { desc: "PCM3.1 uses internal PCB modification — no external DIP switches", dips: ["N/A"] },
            ],
            notes: "SAT tuner MUST remain connected at all times. Do NOT remove it."
          }],
          wiring: [{
            title: "Modification Components",
            items: [
              { label: "Internal MOD PCB", desc: "Installed inside PCM3.1 radio — requires radio disassembly" },
              { label: "Flat Flex Cables (x2)", desc: "Connect OE screen ribbons to modification PCB" },
              { label: "Stand-Offs (18mm)", desc: "5x used to mount PCB: 2 stacked on red locations, 1 on yellow" },
              { label: "WiFi/BT Antenna", desc: "Route through SAT tuner antenna hole to outside radio" },
              { label: "Main Power + USB", desc: "Connect to PCB inside radio; exit through radio chassis" },
            ],
            notes: "Audio: switch OEM to BT Audio or AUX before using CarPlay. SAT tuner: do NOT remove — audio depends on it."
          }]
        },
        "IT3-PCM3": {
          dip_switches: [{
            label: "IT3-PCM3 — DIP by Transmission Type",
            count: 8,
            rows: [
              { desc: "DIP 1, 2, 3 — Default OFF", dips: ["OFF","OFF","OFF","—","—","—","—","—"] },
              { desc: "DIP 4 ON — Automatic Transmission (CAN reverse trigger)", dips: ["—","—","—","ON","OFF","—","—","—"] },
              { desc: "DIP 5 ON — Manual Transmission (wire trigger required)", dips: ["—","—","—","OFF","ON","—","—","—"] },
              { desc: "DIP 6 — Default ON (required)", dips: ["—","—","—","—","—","ON","—","—"] },
              { desc: "DIP 8 — External MIC (ON) / OEM MIC (OFF)", dips: ["—","—","—","—","—","—","—","OFF"] },
            ],
            notes: "NOT compatible with PCM2.0 (silver) radios. SAT tuner MUST stay connected."
          }],
          wiring: [{
            title: "Modification Components",
            items: [
              { label: "Internal MOD PCB", desc: "Installed inside PCM3.0 radio on bottom board" },
              { label: "Flat Flex Cables (x2)", desc: "Replace OE screen ribbons through modification PCB" },
              { label: "Stand-Offs", desc: "4x for radio with SAT tuner; 1x if no SAT tuner" },
              { label: "LCD/DATA Cable", desc: "Exits through notch cut in back of radio to external interface" },
              { label: "WiFi/BT Antenna", desc: "Route through notch to outside radio chassis" },
              { label: "AUX (radio)", desc: "Must insert CD disc then enable AUX in PCM options. Radio MUST be on AUX." },
            ]
          }]
        },
        "ITZ-PCM4-C": {
          dip_switches: [{
            label: "ITZ-PCM4-C — All Vehicles",
            count: 8,
            rows: [
              { desc: "OEM Microphone (default)", dips: ["ON","ON","OFF","ON","OFF","OFF","OFF","OFF"] },
              { desc: "External Microphone add-on", dips: ["ON","ON","OFF","ON","OFF","OFF","OFF","ON"] },
            ],
            notes: "Reboot the interface after changing any DIP setting."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "Radio T-Harness", desc: "Plug-and-play harness to OE PCM4 radio connector" },
              { label: "4-pin LVDS Y-Cable", desc: "Connects between OE radio and OE screen" },
              { label: "USB and CVBS Cable", desc: "USB input for phone/updates; CVBS for camera" },
              { label: "WiFi/BT Antenna", desc: "Route externally away from metal" },
              { label: "OEM MIC Audio / Ground", desc: "Connect to OEM MIC connector (external MIC optional)" },
              { label: "Audio", desc: "Setup → BT Channel ON for BT Audio. Or select AUX Input on OEM stereo for AUX." },
            ]
          }]
        },
        "ITZ-CDR31-A": {
          dip_switches: [{
            label: "ITZ-CDR31-A — All Vehicles",
            count: 8,
            rows: [
              { desc: "CDR 3.1 Radio (all models)", dips: ["OFF","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
            ],
            notes: "Touch calibration: hold screen for 12 seconds after installation. Audio: RADIO MUST BE ON AUX MODE."
          }],
          wiring: [{
            title: "Modification and Key Connections",
            items: [
              { label: "Touch/Video PCB Module", desc: "Mounts to back of radio chassis with double-sided tape" },
              { label: "Modification Ribbon", desc: "Connects OE screen ribbons through ZZ2 PCB. Wide OE ribbon = video FFC. Small OE ribbon = touch FFC." },
              { label: "Switching/LVDS Harness", desc: "Routes from Touch/Video PCB to external ZZPLAY module" },
              { label: "External Button", desc: "Provided with kit — press and hold to enter/exit CarPlay" },
              { label: "AUX", desc: "Radio MUST be on AUX mode for audio" },
            ],
            notes: "Metal tab must be cut from chassis to allow ribbon routing. Cover serrated edge with cloth tape."
          }]
        },
        "IT3-SYM": {
          dip_switches: [{
            label: "IT3-SYM — A4/A5/Q5 (without MMI)",
            count: 8,
            rows: [
              { desc: "IT3-SYM — A4/A5/Q5 2009–2016 (without MMI)", dips: ["OFF","OFF","OFF","ON","OFF","ON","OFF","OFF"] },
            ],
            notes: "DIP7 OFF = car with parking button/sensors. DIP7 ON = without. DIP8 OFF = OEM mic."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS Y-Cable", desc: "Installs at the RADIO (glovebox) — NOT at the screen" },
              { label: "Main T-Harness", desc: "Plugs into OE Audi Quadlock connector behind radio" },
              { label: "AMI AUX Cable", desc: "Connect to factory AMI port for AUX audio. Select AUX in MMI." },
              { label: "WiFi/BT Antenna", desc: "FAKRA antenna — keep away from metal" },
              { label: "Secondary T-harness", desc: "Symphony/RMC: Climate Control module. MMI3G: CAN Gateway (left of glovebox). MIB2: none needed." },
            ]
          }]
        },
        "IT3-MMI3G-A4": {
          dip_switches: [{
            label: "IT3-MMI3G-A4 — Vehicle Selection",
            count: 8,
            rows: [
              { desc: "Q5 2013–2017 / A4/S4/A5/S5 2012–2016", dips: ["OFF","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "Q5 2009–2012 / A4/S4/A5/S5 2009–2011", dips: ["OFF","OFF","OFF","ON","ON","ON","OFF","OFF"] },
            ],
            notes: "DIP7 OFF = with parking button/sensors. DIP7 ON = without. Reboot after DIP change."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS Y-Cable", desc: "Installs at the RADIO (glovebox), not the screen" },
              { label: "Main T-Harness (HAR-Q5U)", desc: "Plugs into OE Audi Quadlock" },
              { label: "Secondary T-harness", desc: "Plugs into the AC controls connector" },
              { label: "AMI AUX Cable", desc: "To factory AMI port for AUX audio" },
            ]
          }]
        },
        "IT3-MMI3G-A6/A7": {
          dip_switches: [{
            label: "IT3-MMI3G-A6/A7 — Vehicle Selection",
            count: 8,
            rows: [
              { desc: "A6/A7/S6/S7/Q7 2012–2015 (incl. Huracan)", dips: ["OFF","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "A6/A7/S6/S7/Q7 2009–2011 (older MMI3G)", dips: ["OFF","OFF","OFF","ON","ON","ON","OFF","OFF"] },
              { desc: "A6 2010–2011 C6", dips: ["OFF","OFF","ON","ON","OFF","ON","OFF","OFF"] },
              { desc: "Q7 2010–2015", dips: ["OFF","OFF","OFF","OFF","ON","OFF","OFF","OFF"] },
            ],
            notes: "Huracan older: cut yellow 12V wire at CAN connector, extend to ACC source. Run reverse wire to violet, set Line Detect ON."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS Y-Cable", desc: "Installs at RADIO (glovebox)" },
              { label: "Main T-Harness (HAR-A6 or HAR-C6)", desc: "Plugs into OE Audi Quadlock. A6/A7 T-harness differs from A4 T-harness." },
              { label: "Secondary T-harness (AC Controls)", desc: "Connects to AC controls module" },
              { label: "AMI AUX Cable", desc: "To factory AMI port" },
            ]
          }]
        },
        "IT3-MIB2": {
          dip_switches: [{
            label: "IT3-MIB2 — by Model and Screen Size",
            count: 8,
            rows: [
              { desc: "A3 2013–2016 (5.8″)", dips: ["ON","ON","OFF","OFF","ON","ON","OFF","OFF"] },
              { desc: "A3 2017 (5.8″)", dips: ["ON","ON","OFF","ON","ON","OFF","OFF","OFF"] },
              { desc: "A3 2013–2016 (7″)", dips: ["ON","ON","ON","OFF","OFF","ON","OFF","OFF"] },
              { desc: "A3 2017 (7″)", dips: ["ON","ON","ON","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "A4/S4/A5/S5 2017, Q5 2018 (7″)", dips: ["ON","ON","OFF","ON","OFF","OFF","OFF","OFF"] },
              { desc: "Q7 2016–2018 (7″)", dips: ["ON","ON","ON","OFF","ON","ON","OFF","OFF"] },
              { desc: "A6/A7 2016–2018", dips: ["OFF","OFF","ON","ON","ON","ON","OFF","OFF"] },
            ],
            notes: "MIB2: NO secondary T-harness needed. DIP7 OFF = with parking button/sensors. DIP8 OFF = OEM mic."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS Y-Cable", desc: "Installs at RADIO (glovebox) — no secondary harness for MIB2" },
              { label: "Main T-Harness (HAR-B9 or HAR-A7C)", desc: "Plugs into OE MIB2 radio Quadlock" },
              { label: "AMI AUX Cable", desc: "To factory AMI port for AUX audio" },
            ]
          }]
        },
        "DZZ BMW": {
          dip_switches: [{
            label: "DZZ BMW — Screen Resolution",
            count: 1,
            rows: [
              { desc: "Set via software: Setup → Car Settings → OEM Screen Resolution", dips: ["N/A"] },
              { desc: "GND wire: CONNECT for CCC and NBT systems", dips: ["—"] },
              { desc: "GND wire: DISCONNECT for CIC systems", dips: ["—"] },
            ],
            notes: "No external DIP switches on DZZ BMW. All screen settings via software menu."
          }]
        },
        "DZZ MERCEDES": {
          dip_switches: [{
            label: "DZZ Mercedes — Note",
            count: 1,
            rows: [
              { desc: "Screen resolution set via software: Settings → Car Settings → OEM Screen Resolution", dips: ["N/A"] },
              { desc: "For NTG4 systems: use older-style LVDS connector on replacement screen", dips: ["—"] },
            ],
            notes: "Fiber optic MUST be moved to T-Harness. No external DIP switches."
          }]
        },
        "DZZ AUDI": {
          vehicle: "Audi (MIB / MMI / Symphony / RMC)",
          switch_button: "Touchscreen — tap AUDI icon for OEM system",
          audio_method: "OEM Bluetooth Audio (recommended) or AUX",
          notes: "If using factory Bluetooth for calls and music, enable Use Car's BT Channel. Otherwise disable it and select AUX.",
          dip_switches: [{
            label: "DZZ AUDI — Secondary T-Harness Location",
            count: 1,
            rows: [
              { desc: "Symphony / RMC radios: connect to Climate Control module", dips: ["N/A"] },
              { desc: "MMI3G radio: connect to CAN Gateway (left of glovebox)", dips: ["N/A"] },
              { desc: "MIB radio: NO secondary T-harness needed", dips: ["N/A"] },
              { desc: "RMC-Q3/RMC-A1: connect to CAN Gateway (driver side, right of steering wheel)", dips: ["N/A"] },
            ],
            notes: "Hazard switch/airbag harness MUST be reconnected to DZZ replacement bracket. Fiber optic: move from OE Quadlock to T-Harness plug."
          }]
        },
        "ITZ-ACURA-A": {
          dip_switches: [{
            label: "ITZ-ACURA-A — Vehicle Selection",
            count: 5,
            rows: [
              { desc: "TLX / RLX / RDX / Odyssey", dips: ["OFF","OFF","OFF","OFF","OFF"] },
              { desc: "MDX — Type 1 / Accord", dips: ["OFF","OFF","OFF","OFF","ON"] },
              { desc: "MDX — Type 2", dips: ["OFF","OFF","OFF","ON","OFF"] },
            ],
            notes: "Disconnect power before adjusting DIP switches."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS-Y Cable", desc: "Connects between OE screen and module" },
              { label: "Main Harness", desc: "Plugs into OE Acura/Honda radio connector" },
              { label: "Supplemental MIC T-Harness", desc: "For most vehicles: connect at dome light assembly. PIN 2 = MIC+. PIN 8 = ANC fallback." },
              { label: "Audio (Factory BT)", desc: "Connect phone to OEM Acura/Honda BT. Link Split Audio ON = factory BT audio (recommended). Path: About → Factory Settings → 8888 → Link Split Audio." },
              { label: "Camera Harness", desc: "For reverse camera input" },
            ]
          }]
        },
        "ITZ-ACH": {
          dip_switches: [{
            label: "ITZ-ACH — Vehicle Selection",
            count: 5,
            rows: [
              { desc: "TLX / RLX / RDX / Odyssey", dips: ["OFF","OFF","OFF","OFF","OFF"] },
              { desc: "MDX — Type 1 / Accord", dips: ["OFF","OFF","OFF","OFF","ON"] },
              { desc: "MDX — Type 2", dips: ["OFF","OFF","OFF","ON","OFF"] },
            ],
            notes: "DIFFERENT kit from ITZ-ACURA-A — different LVDS cable and software update file. Same DIP table as ACURA-A."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS Cable", desc: "Different cable from ACURA-A — connects between OE screen and module" },
              { label: "Main Harness", desc: "Plugs into OE Acura/Honda radio connector" },
              { label: "OEM MIC T-Harness", desc: "Connect at dome light. PIN 2 = default. PIN 8 = ANC fallback." },
              { label: "Camera Harness", desc: "For reverse camera input" },
              { label: "Audio (Factory BT)", desc: "Link Split Audio ON = factory BT (recommended). Path: About → Factory Settings → 8888 → Link Split Audio." },
            ]
          }]
        },
        "ITZ-JGC": {
          dip_switches: [{
            label: "ITZ-JGC — Vehicle Selection",
            count: 5,
            rows: [
              { desc: "Grand Cherokee / Durango / Charger", dips: ["OFF","OFF","OFF","OFF","OFF"] },
              { desc: "Chrysler 200 / Jeep Cherokee", dips: ["ON","OFF","OFF","OFF","OFF"] },
              { desc: "Jeep Compass", dips: ["OFF","ON","OFF","OFF","OFF"] },
              { desc: "RAM 1500 / Chrysler 300", dips: ["ON","ON","OFF","OFF","OFF"] },
            ],
            notes: "Pins in T-harness are easy to bend — check carefully before connecting. After DIP change: reboot required."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "Main T-Harness", desc: "Connects to OE radio main power harness. Pins bend easily — be careful." },
              { label: "LVDS Y-Cable", desc: "Between OE screen and module" },
              { label: "PCB with FFCs", desc: "Screen modification PCB installed inside OE Uconnect 8 inch screen" },
              { label: "LCD-OUT Cable", desc: "Runs from internal mod-PCB through drilled hole to external interface" },
              { label: "Audio (Disable Phone Link)", desc: "ZZPLAY → Settings → System → Factory Mode → Phone Link Setting → Disable Phone Link Audio ON = factory BT audio" },
            ]
          }]
        },
        "ITZ-NIF": {
          dip_switches: [{
            label: "ITZ-NIF — DIP Settings",
            count: 8,
            rows: [
              { desc: "DIP 1 ON = OEM Camera / DIP 1 OFF = Aftermarket Camera", dips: ["ON","ON","ON","OFF","ON","ON","ON","—"] },
              { desc: "DIP 4 ON = Wire trigger for RVC / DIP 4 OFF = CAN trigger (default)", dips: ["ON","ON","ON","OFF","ON","ON","ON","—"] },
              { desc: "Standard setup: OEM camera, CAN trigger", dips: ["ON","ON","ON","OFF","ON","ON","ON","—"] },
              { desc: "With OEM camera and wire trigger", dips: ["ON","ON","ON","ON","ON","ON","ON","—"] },
              { desc: "Adding aftermarket camera with wire trigger", dips: ["OFF","ON","ON","ON","ON","ON","ON","—"] },
            ],
            notes: "HDMI is NOT supported on ITZ-NIF. If reverse not triggering from interface side: connect wire to brown REV(+) wire and set DIP4 ON."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "Modification PCB", desc: "Installed inside OE Infiniti screen. Wide OE gold ribbon = video (lower-right FFC). Small OE gold ribbon = touch (lower-left FFC). Pull ribbon back so corners align before clamping." },
              { label: "PCB Cable", desc: "Exits through bottom hole of screen chassis to external module" },
              { label: "Screen T-Harness", desc: "Connects to OE screen main plug" },
              { label: "Power T-Harness", desc: "Plugs into OE radio power harness" },
              { label: "Brown REV(+) wire", desc: "If using wire trigger: connect to reverse light and set DIP4 ON" },
              { label: "Audio (BT Channel ON)", desc: "BT Channel ON by default. Connect phone to OEM Infiniti BT. Set OEM radio to BT Audio." },
            ]
          }]
        },
        "ITZ-GX1-A": {
          dip_switches: [{
            label: "ITZ-GX1-A — Note",
            count: 1,
            rows: [
              { desc: "DIP details: see manual diagram page. Not fully detailed in text portion of manual.", dips: ["—"] },
            ],
            notes: "Radio MUST be on AUX mode for audio. USB/AUX Hub MUST be reconnected after install or AUX disappears as a source."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "LVDS-IN Cable", desc: "Connects OE NAV display GVIF cable to module" },
              { label: "GVIF Y-Cable", desc: "OE screen GVIF cable connects here; other end to OE GX460 radio" },
              { label: "Main T-Harness", desc: "Plugs into OE GX460 radio connector" },
              { label: "USB/AUX Hub", desc: "MUST be reconnected — if disconnected, AUX disappears from radio sources" },
              { label: "Audio T-Harness", desc: "Connects to OE radio; 3.5mm AUX cable at other end to factory AUX port" },
              { label: "Reverse wire (green)", desc: "For wire trigger: connect to reverse light. Set Reversing Mode to 12V Active." },
            ]
          }]
        },
        "ITZ-MYLINK": {
          dip_switches: [{
            label: "ITZ-MYLINK — DIP Settings",
            count: 1,
            rows: [
              { desc: "DIP 1 ON — Enables reverse camera (RVC)", dips: ["ON"] },
              { desc: "DIP 1 OFF — Default (no RVC)", dips: ["OFF"] },
            ],
            notes: "TWO purple wires MUST be separated. LVDS connects to HMI module, NOT to radio."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "TWO Purple Wires", desc: "MUST be separated — do not connect them together" },
              { label: "LVDS Cable", desc: "Connects to the HMI module — NOT to the radio" },
              { label: "Main Harness", desc: "Plugs into OE MyLink radio connector" },
              { label: "Ribbon Cables", desc: "Multiple ribbons inside modified screen — all must be fully seated" },
            ]
          }]
        },
        "ITZ-TOY": {
          dip_switches: [{
            label: "ITZ-TOY — DIP Settings",
            count: 8,
            rows: [
              { desc: "Default (normal operation)", dips: ["OFF","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "DIP 1 ON — Fixes RVC staying on when not in reverse", dips: ["ON","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "DIP 4 ON — Alternate activation (if normal switch fails)", dips: ["OFF","OFF","OFF","ON","OFF","OFF","OFF","OFF"] },
              { desc: "DIP 5 ON — Prevents unit switching back to OE screen after a few moments", dips: ["OFF","OFF","OFF","OFF","ON","OFF","OFF","OFF"] },
              { desc: "DIP 6 ON — Restores OEM reverse camera in OE mode if lost", dips: ["OFF","OFF","OFF","OFF","OFF","ON","OFF","OFF"] },
            ],
            notes: "Not compatible: Corolla 86140-02540 Fujitsu, 4Runner Pioneer radio, Prius Prime 11.6in screen, RAV4 86100-42241. Use AUX source only."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "Plug and Play Harness", desc: "Connects to OE Toyota radio connector. No modification needed." },
              { label: "USB Harness", desc: "USB input for phone connection and updates" },
              { label: "WiFi Antenna", desc: "Route externally" },
              { label: "Audio (AUX only)", desc: "Stay on AUX source. Delete OEM Toyota BT from phone entirely. Do NOT use BT Channel." },
            ]
          }]
        },
        "ITZ-TOY2": {
          dip_switches: [{
            label: "ITZ-TOY2 — DIP Settings",
            count: 8,
            rows: [
              { desc: "Default (normal operation)", dips: ["OFF","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "DIP 1 ON — Fixes RVC staying on when not in reverse", dips: ["ON","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
              { desc: "DIP 4 ON — Alternate activation (if normal switch fails)", dips: ["OFF","OFF","OFF","ON","OFF","OFF","OFF","OFF"] },
              { desc: "DIP 5 ON — Prevents switching back to OE screen after a few moments", dips: ["OFF","OFF","OFF","OFF","ON","OFF","OFF","OFF"] },
              { desc: "DIP 6 ON — Restores OEM reverse camera in OE mode if lost", dips: ["OFF","OFF","OFF","OFF","OFF","ON","OFF","OFF"] },
            ],
            notes: "Same compatibility list as ITZ-TOY. Not compatible: Sienna 86140-02540, RAV4 86100-42241."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "Plug and Play Harness", desc: "Connects to OE Toyota radio — no modification" },
              { label: "USB Harness", desc: "USB input for phone/updates" },
              { label: "WiFi Antenna", desc: "Route externally" },
              { label: "Audio", desc: "AUX source (hold vol knob 2 sec or double-tap to switch). BT Channel also available on newer SW." },
            ]
          }]
        },
        "ITZ-LRB": {
          dip_switches: [{
            label: "ITZ-LRB — Vehicle Selection",
            count: 5,
            rows: [
              { desc: "Range Rover Sport / typical (all OFF)", dips: ["OFF","OFF","OFF","OFF","OFF"] },
              { desc: "Discovery / Land Rover 4", dips: ["ON","OFF","OFF","ON","OFF"] },
              { desc: "Jaguar", dips: ["OFF","ON","OFF","ON","OFF"] },
              { desc: "Harman Style", dips: ["OFF","OFF","OFF","ON","OFF"] },
              { desc: "European Style", dips: ["OFF","OFF","OFF","OFF","ON"] },
              { desc: "DIP 1 ON — Use if no switch with NAV button press", dips: ["ON","OFF","OFF","—","—"] },
              { desc: "DIP 5 ON — Use if overlay issue present", dips: ["—","—","—","—","ON"] },
            ],
            notes: "CRITICAL: When changing DIPs, unplug screen T-harness from car side first. Reconnect, then power on — screen and unit MUST boot together. Factory Navigation inoperable after install. Aftermarket camera cannot be added. Time out home must be OFF in OE settings."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "GVIF Y-Cable", desc: "Connects between OE screen and module. Screen and module must boot simultaneously." },
              { label: "Main Harness", desc: "Plugs into OE Land Rover/Jaguar radio connector (BOSCH system)" },
              { label: "WiFi/BT Antenna (FAKRA)", desc: "Route externally" },
              { label: "Audio", desc: "OEM BT streaming recommended (sw 2023-12-05+). Some vehicles: AUX under My Music. Only ground Audio Ground Wire if no audio on AUX." },
              { label: "Time out home", desc: "Must be turned OFF in OE factory radio settings or CarPlay will drop out" },
            ]
          }]
        },
        "IT2-INF-Q70": {
          dip_switches: [{
            label: "IT2-INF-Q70 — DIP Settings",
            count: 8,
            rows: [
              { desc: "DIP 1, 2, 3 — Keep OFF", dips: ["OFF","OFF","OFF","—","—","—","—","—"] },
              { desc: "DIP 4 — OFF: CAN trigger for RVC / ON: Wire trigger", dips: ["—","—","—","OFF","—","—","—","—"] },
              { desc: "DIP 5, 6, 7 — Keep OFF", dips: ["—","—","—","—","OFF","OFF","OFF","—"] },
              { desc: "DIP 8 — OFF: Aftermarket RVC / ON: OEM RVC", dips: ["—","—","—","—","—","—","—","OFF"] },
            ],
            notes: "Factory NAV REQUIRED — will not work without it. Cycle power after every DIP change."
          }],
          wiring: [{
            title: "Key Connections",
            items: [
              { label: "CAN T-Harness", desc: "Connects to OE Infiniti Q70 CAN bus" },
              { label: "Media Harness", desc: "Plugs into OE Infiniti radio media connector" },
              { label: "LVDS IN / LVDS OUT", desc: "Connect between OE screen and module" },
              { label: "3.5mm Audio Cable", desc: "For AUX audio to OE radio AUX input" },
              { label: "USB to AUX Adapter", desc: "For audio path" },
              { label: "MIC", desc: "Included — connect to provided MIC cable" },
              { label: "Before switching", desc: "OEM must be on AUX or BT Audio mode before switching to CarPlay" },
            ]
          }]
        },
        "ITZ-CCC": {
          vehicle: "BMW (CCC iDrive)",
          switch_button: "Menu (hold 2s)",
          audio_method: "AUX \u2014 radio must be on AUX mode",
          nodes: [
            { id:"q1", type:"question", text:"Is CarPlay showing on screen?",
              choices:[{text:"Yes",next:"q2"},{text:"No",next:"q3"}]},
            { id:"q2", type:"question", text:"Is there audio?",
              choices:[{text:"Yes \u2014 working fine",next:"done_ok"},{text:"No audio",next:"a_audio"}]},
            { id:"q3", type:"result", text:"Hold Menu/iDrive button ~2s. Check DIP for screen size. Verify fiber optic moved to T-Harness.", resolution:"Hold Menu 2s / Check DIP / fiber optic" },
            { id:"a_audio", type:"result", text:"Radio MUST be on AUX mode. Connect AUX cable to BMW AUX port. Select AUX on radio before switching to CarPlay.", resolution:"Switch radio to AUX" },
            { id:"done_ok", type:"result", text:"System is working correctly.", resolution:"Working" }
          ]
        },
        "ITZ-CCC-A": {
          vehicle: "BMW (CCC iDrive \u2014 A variant)",
          switch_button: "Menu (hold 2s)",
          audio_method: "AUX \u2014 radio must be on AUX mode",
          nodes: [
            { id:"q1", type:"question", text:"Is CarPlay showing on screen?",
              choices:[{text:"Yes",next:"q2"},{text:"No",next:"q3"}]},
            { id:"q2", type:"question", text:"Is there audio?",
              choices:[{text:"Yes \u2014 working fine",next:"done_ok"},{text:"No audio",next:"a_audio"}]},
            { id:"q3", type:"result", text:"Hold Menu 2s. Check DIP. Verify LVDS-IN cable orientation (white mark on correct side). Fiber optic must be moved to T-Harness.", resolution:"Hold Menu / DIP / LVDS orientation" },
            { id:"a_audio", type:"result", text:"Radio MUST be on AUX mode. Connect AUX cable to BMW AUX port.", resolution:"Switch radio to AUX" },
            { id:"done_ok", type:"result", text:"System is working correctly.", resolution:"Working" }
          ]
        },
        "IT2-JGC": {
          vehicle: "Chrysler/Dodge/Jeep (RA3/RA4 8.4in \u2014 IT2 version)",
          switch_button: "Touchscreen (push button)",
          audio_method: "Factory Bluetooth \u2014 connect phone to OEM Bluetooth first",
          nodes: [
            { id:"q1", type:"question", text:"Is CarPlay showing on screen?",
              choices:[{text:"Yes",next:"q2"},{text:"No",next:"q3"}]},
            { id:"q2", type:"question", text:"Is there audio?",
              choices:[{text:"Yes \u2014 working fine",next:"done_ok"},{text:"No audio",next:"a_audio"}]},
            { id:"q3", type:"result", text:"Press the push button to enter CarPlay. Check PCB mod \u2014 all FFC ribbons must be seated fully. Check T-Harness pins (bend easily).", resolution:"Check PCB mod / button / T-Harness pins" },
            { id:"a_audio", type:"result", text:"Connect phone to OEM Jeep/Dodge Bluetooth. Audio routes through factory BT. Enable Disable Phone Link Audio ON in ZZPLAY factory settings.", resolution:"Connect OEM Bluetooth" },
            { id:"done_ok", type:"result", text:"System is working correctly.", resolution:"Working" }
          ]
        },
        "IT2-INF-Q": {
          vehicle: "Infiniti Q50 (Dual Screen)",
          switch_button: "Hold Back Arrow",
          audio_method: "AUX \u2014 radio must be on AUX before switching",
          nodes: [
            { id:"q1", type:"question", text:"Is CarPlay on the upper screen?",
              choices:[{text:"Yes",next:"q2"},{text:"No",next:"q3"}]},
            { id:"q2", type:"question", text:"Is there audio?",
              choices:[{text:"Yes \u2014 working fine",next:"done_ok"},{text:"No audio",next:"a_audio"}]},
            { id:"q3", type:"result", text:"Hold Back Arrow to switch. Switch to AUX/USB first. Check PCB mod inside upper screen \u2014 all ribbons must be fully seated. Check DIP switch.", resolution:"Hold Back Arrow / Check PCB / DIP" },
            { id:"a_audio", type:"result", text:"Switch radio to AUX mode. USB-AUX adapter must be in OE USB port.", resolution:"Switch to AUX / connect USB-AUX adapter" },
            { id:"done_ok", type:"result", text:"System is working correctly.", resolution:"Working" }
          ]
        },
        "IT3-AUDI": {
          vehicle: "Audi (MMI3G / MIB2 / Symphony / RMC \u2014 all variants)",
          switch_button: "NAVI (hold 3s)",
          audio_method: "BT Channel (recommended if BT streaming equipped) or AUX/AMI",
          nodes: [
            { id:"q1", type:"question", text:"Is CarPlay showing on screen?",
              choices:[{text:"Yes",next:"q2"},{text:"No",next:"q3"}]},
            { id:"q2", type:"question", text:"Is there audio?",
              choices:[{text:"Yes \u2014 working fine",next:"done_ok"},{text:"No audio",next:"a_audio"}]},
            { id:"q3", type:"question", text:"Did you hold NAVI for 3 seconds?",
              choices:[{text:"Yes, nothing happened",next:"q4"},{text:"No, trying now",next:"q3b"}]},
            { id:"q3b", type:"result", text:"Hold NAVI button for 3 seconds to enter CarPlay.", resolution:"Hold NAVI 3s" },
            { id:"q4", type:"question", text:"Is the DIP correct for your exact Audi model and radio type?",
              choices:[{text:"Not sure",next:"a_dip"},{text:"Confirmed correct",next:"q5"}]},
            { id:"a_dip", type:"result", text:"DIP differs by radio type: Symphony, MMI3G-A4, MMI3G-A6/Q7, MIB2. See DIP tab. Reboot after any DIP change.", resolution:"Check DIP for your exact radio type" },
            { id:"q5", type:"result", text:"LVDS Y-Cable installs at the RADIO (not screen). Verify secondary T-harness connected (Symphony=AC controls, MMI3G=CAN Gateway, MIB2=none needed). Fiber optic to T-Harness.", resolution:"Check LVDS at radio / secondary T-harness" },
            { id:"a_audio", type:"result", text:"If BT streaming equipped: BT Channel ON in ZZPLAY, connect phone to OEM Audi BT, use BT Audio source. Otherwise: AMI-AUX adapter to AMI port, select AUX in MMI.", resolution:"BT Channel ON or AMI-AUX" },
            { id:"done_ok", type:"result", text:"System is working correctly.", resolution:"Working" }
          ]
        },
        "IT3-LEX19": {
          vehicle: "Lexus LEX19 (2019+ models)",
          switch_button: "MENU or MAP (hold)",
          audio_method: "BT Channel (if equipped) or AUX",
          nodes: [
            { id:"q1", type:"question", text:"Is CarPlay showing on screen?",
              choices:[{text:"Yes",next:"q2"},{text:"No",next:"q3"}]},
            { id:"q2", type:"question", text:"Is there audio?",
              choices:[{text:"Yes \u2014 working fine",next:"done_ok"},{text:"No audio",next:"a_audio"}]},
            { id:"q3", type:"result", text:"Hold MENU or MAP to switch. Check DIP for LEX19. Screen Correction T-Harness MUST be connected. Audio Ground wire must connect to chassis ground.", resolution:"Check DIP / Screen Correction T-Harness / Ground wire" },
            { id:"a_audio", type:"result", text:"Audio Ground wire MUST connect to chassis ground. Use BT Channel if equipped, otherwise select AUX on radio.", resolution:"Ground wire / BT Channel / AUX" },
            { id:"done_ok", type:"result", text:"System is working correctly.", resolution:"Working" }
          ]
        },
        "ITZ-NTG4-SLS": {
          vehicle: "Mercedes NTG4 (SLS / special variant)",
          switch_button: "Dial (hold 3s)",
          audio_method: "AUX or BT Audio \u2014 Link Split Audio ON for BT",
          nodes: [
            { id:"q1", type:"question", text:"Is CarPlay showing on screen?",
              choices:[{text:"Yes",next:"q2"},{text:"No",next:"q3"}]},
            { id:"q2", type:"question", text:"Is there audio?",
              choices:[{text:"Yes \u2014 working fine",next:"done_ok"},{text:"No audio",next:"a_audio"}]},
            { id:"q3", type:"result", text:"Check DIP for your screen size (same table as ITZ-NTG4-A). LVDS connectors: dimples face BOTTOM of unit. Fiber optic must be moved to T-Harness.", resolution:"Check DIP / LVDS orientation / fiber optic" },
            { id:"a_audio", type:"result", text:"Radio must be on AUX or Bluetooth Audio. If using BT: Link Split Audio must be ON in ZZPLAY settings.", resolution:"AUX or BT with Link Split Audio ON" },
            { id:"done_ok", type:"result", text:"System is working correctly.", resolution:"Working" }
          ]
        },
        "ITZ-RMC": {
          vehicle: "Audi (RMC Radio)",
          switch_button: "NAVI (hold 3s)",
          audio_method: "BT Channel (if equipped) or AMI-AUX",
          nodes: [
            { id:"q1", type:"question", text:"Is CarPlay showing on screen?",
              choices:[{text:"Yes",next:"q2"},{text:"No",next:"q3"}]},
            { id:"q2", type:"question", text:"Is there audio?",
              choices:[{text:"Yes \u2014 working fine",next:"done_ok"},{text:"No audio",next:"a_audio"}]},
            { id:"q3", type:"result", text:"Hold NAVI 3s to enter CarPlay. Check DIP. Secondary T-harness: RMC-Q3/A1 = CAN Gateway (driver side right of steering wheel). Standard RMC = left of glovebox. Reboot after DIP changes.", resolution:"Hold NAVI 3s / DIP / secondary T-harness" },
            { id:"a_audio", type:"result", text:"If BT streaming equipped: BT Channel ON, use BT Audio source. Otherwise: AMI-AUX adapter to AMI port, select AUX on radio.", resolution:"BT Channel ON or AMI-AUX" },
            { id:"done_ok", type:"result", text:"System is working correctly.", resolution:"Working" }
          ]
        },

        "ITZ-LR12-C": {
          dip_switches: [{ label:"ITZ-LR12-C \u2014 DIP Switches (8 total)", count:8, rows:[
            { desc:"DIP 1 ON \u2014 Discovery/LR4 style", dips:["ON","OFF","OFF","OFF","OFF","OFF","OFF","OFF"] },
            { desc:"DIP 2 ON \u2014 Jaguar", dips:["OFF","ON","OFF","OFF","OFF","OFF","OFF","OFF"] },
            { desc:"DIP 3 ON \u2014 Harman style", dips:["OFF","OFF","ON","OFF","OFF","OFF","OFF","OFF"] },
            { desc:"DIP 4 ON \u2014 European style", dips:["OFF","OFF","OFF","ON","OFF","OFF","OFF","OFF"] },
            { desc:"DIP 5 ON \u2014 Enable if unit does not activate", dips:["OFF","OFF","OFF","OFF","ON","OFF","OFF","OFF"] },
          ], notes:"\u26a0\ufe0f DIP change procedure: unplug SCREEN T-HARNESS from car side FIRST. Screen and unit must boot together. CAN reset after every change (lock doors, 5 min)." }],
          wiring: [{ title:"Key Connections", items:[
            { label:"LVDS Y-Cable", desc:"Connects OE GVIF/LVDS cable between screen and unit" },
            { label:"Main Harness", desc:"Connects to OE radio connector" },
            { label:"RCA/USB Cable", desc:"Camera and USB inputs" },
            { label:"Audio Ground", desc:"Connect ONLY if no audio on AUX" },
            { label:"Reverse wire (violet)", desc:"Connect to reverse light; set Reverse Line Detect ON in Car Settings" },
          ]}]
        },

        "ITZ-BNT": {
          dip_switches: [{ label:"ITZ-BNT \u2014 DIP Switches", count:8, rows:[
            { desc:"DIP 5 ON \u2014 Setting only if radio is Heating", dips:["OFF","OFF","OFF","OFF","ON","OFF","OFF","OFF"] },
          ], notes:"Disconnect power before adjusting DIPs." }],
          wiring: [{ title:"Key Connections", items:[
            { label:"PCB Harness", desc:"Connects to PCB Mod board inside radio chassis" },
            { label:"Main Harness", desc:"OE Bentley radio main power/CAN connector" },
            { label:"AMI-AUX Adapter", desc:"Connect to OE AMI port for AUX audio if needed" },
          ]}]
        },

        "ITZ-BNT-A": {
          dip_switches: [{ label:"ITZ-BNT-A \u2014 DIP Switches", count:8, rows:[
            { desc:"DIP 5 ON \u2014 Setting only if radio is Heating", dips:["OFF","OFF","OFF","OFF","ON","OFF","OFF","OFF"] },
          ], notes:"Disconnect power before adjusting DIPs. Settings identical to ITZ-BNT." }],
          wiring: [{ title:"Key Connections", items:[
            { label:"Internal Modification PCB", desc:"Upper black ribbon \u2192 OE connector; lower black ribbon \u2192 radio chassis" },
            { label:"PCB Cable", desc:"Exits through drilled hole in top plate; connects to middle connector on PCB" },
            { label:"Main Harness", desc:"OE Bentley radio main power connector" },
            { label:"AMI-AUX Adapter", desc:"Connect to OE AMI port for AUX audio" },
          ]}]
        },

        "ZW-RAM": {
          dip_switches: [{ label:"ZW-RAM \u2014 DIP Switches (6 total)", count:6, rows:[
            { desc:"DIP 1 ON \u2014 Disable High Beam flashing", dips:["ON","\u2014","\u2014","\u2014","\u2014","\u2014"] },
            { desc:"DIP 2 ON \u2014 Disable Low Beam flashing", dips:["\u2014","ON","\u2014","\u2014","\u2014","\u2014"] },
            { desc:"DIP 3 ON \u2014 Enable Reverse Light in pattern (may trigger RVC)", dips:["\u2014","\u2014","ON","\u2014","\u2014","\u2014"] },
            { desc:"DIP 4 ON \u2014 Halogen mode (slower); OFF = LED", dips:["\u2014","\u2014","\u2014","ON","\u2014","\u2014"] },
            { desc:"DIP 5 ON \u2014 Disable STROBE every ~3 sec", dips:["\u2014","\u2014","\u2014","\u2014","ON","\u2014"] },
            { desc:"DIP 6 ON \u2014 Disable Trailer lights from flashing", dips:["\u2014","\u2014","\u2014","\u2014","\u2014","ON"] },
          ], notes:"IGN ON or engine running required to activate. Snow Plow mode: apply 12V to INPUT 2 (purple wire on button harness) BEFORE activating." }],
          wiring: [{ title:"Key Connections", items:[
            { label:"12V Power", desc:"Constant 12V (fused)" },
            { label:"Ground", desc:"Clean chassis ground" },
            { label:"IGN/ACC", desc:"12V when key ON \u2014 required for activation" },
            { label:"INPUT 1 (push button)", desc:"12V trigger \u2014 press to activate/deactivate" },
            { label:"INPUT 2 (Snow Plow)", desc:"Apply 12V BEFORE activating for Snow Plow mode (purple wire)" },
            { label:"OUTPUT 2/3 (turn signals)", desc:"Right/Left 12V turn signal outputs when unit active" },
          ]}]
        },

      };

      // Public manuals published by ZZ2. Store only the allowlisted Drive file
      // id; links are built locally so arbitrary catalogue text can never turn
      // into an external iframe URL.
      const OFFICIAL_MANUALS = {
        "DZZ AUDI": "1uBAdnwKN76SHvxYgjDJyQK6SOj6fkWlR",
        "DZZ BMW": "1v_M6SE_gMIqCE2WHgt0krO2WbHXWFbOW",
        "DZZ MERCEDES": "1CA0vEvwGfaM7nlnGVAAKGN_pMGtcQrO2",
        "IT2-JGC": "1d_EdvDr1eF-YLef3pON9p3SCRd2VyT5E",
        "IT2-MAS-QTG": "1qrlXlCquYUmc9YV9pl295D2xP861paiT",
        "IT3-AUDI": "1TgTB_ONoWfSUBmiDEGSoDYQ_J4y0FJ73",
        "IT3-CIC": "1X2DphOFYE8pdSK9P3xDcYpR5io9KL7Z1",
        "IT3-LEX15": "18bwgmMHhZLsX47SXTBi6qxMU-rV91sjA",
        "IT3-LEX17": "18bwgmMHhZLsX47SXTBi6qxMU-rV91sjA",
        "IT3-LEX19": "18bwgmMHhZLsX47SXTBi6qxMU-rV91sjA",
        "IT3-NBT": "1XeKIiloIuZU2DvwPZQEUPXsrGhtiTBxI",
        "IT3-NTG45": "1uKDh3qK9Ko1gsngvYijIol4Kn_PCHNQ1",
        "IT3-NTG5": "1vSq80VWuaT33YsRnP-ZK0THgwCLnjkQG",
        "IT3-PCM3": "1yZH-kVuVIZKH35GJnVg27F--mQK79tPp",
        "IT3-PCM31A": "1DFl62Ks7AAZh_v1VlSuQ9M_MWxJuWpAn",
        "IT3-SYM": "1_iz9q0_7r0apAgJfvZSuMHJ8ZJ84j8zc",
        "IT3-VOLVO": "11ceaWR3lt1kakhZokZQuruLbYjefolMC",
        "ITB-EVO": "1XDsL4eLnwOjkm6UHnAmy-nroWIraZX0N",
        "ITB-RMC": "16WOjh0kCq1G-en88ucxGMDDp_741SD4m",
        "ITZ-458": "1TdLJ32d9YJOS-tJgicLa-mLLmWUbGhPX",
        "ITZ-ACH": "117nn51sVZzFaFc728J22aYyx3wkJNUjl",
        "ITZ-ACURA-A": "1_H2Nv3RkWLY5K62SOtQIw6btMu2FycXd",
        "ITZ-ALFA-A": "1dZtEKGHLMSZNt3Ja7kbKiljlXtsA7zNx",
        "ITZ-BNT": "1zzhH2c1JYVtoG_sbABbn2BhWS3TjHH_i",
        "ITZ-BNT-A": "1zzhH2c1JYVtoG_sbABbn2BhWS3TjHH_i",
        "ITZ-CDR31-A": "1AhNqGeheyX7rKjqPDgRa1nYAzVembVUb",
        "ITZ-FCL": "1foQGkRnSLxHhWrNcEgpa4INB_t-yoCae",
        "ITZ-GX1-A": "10gxTiwwTYi6e8sNPXFE4AScu554o33K3",
        "ITZ-HON": "1OYA0BxbqsUcdOaYjxn84knVXZqq3ExVv",
        "ITZ-JGC": "1d_EdvDr1eF-YLef3pON9p3SCRd2VyT5E",
        "ITZ-LR12-C": "1RwNmngxD0slJuMwidPiH8LY3jm2kexHM",
        "ITZ-LR15T": "1VQ_NcD2hDF1huC9XMb_urlVDN6Hn39d2",
        "ITZ-LRB": "160JuU_HtM3VlXeLa9VHEh7BtrH14dvYn",
        "ITZ-MCL": "1UNfjsNdGIlOYMScsPUicueGRTytzoC--",
        "ITZ-MYLINK": "1llX5vgZW3dV3_e2QVfmIYyJTglOmBMPR",
        "ITZ-NIF": "1OxTZi0ZN4J4jXTXPqP_v5soS677d_2QB",
        "ITZ-NTG3": "13VXSg9uTLJePtmY5hi6t5XmIlvXfcFTn",
        "ITZ-NTG3-A": "13VXSg9uTLJePtmY5hi6t5XmIlvXfcFTn",
        "ITZ-NTG35-A": "1_1m8kk2WzfjWqb3j0YP846IwvpnrjSF0",
        "ITZ-NTG4": "1eG4ouuvCS820pMb7PGEotDDoW69vFE3H",
        "ITZ-NTG4-A": "1n6cjryMg42n6iRiSV8hTLWwqeesi0dA-",
        "ITZ-NTG4-SLS": "1n6cjryMg42n6iRiSV8hTLWwqeesi0dA-",
        "ITZ-NTG6": "18wQ2-Whr0n4_jtr2PZsvxDhf9pfZFquG",
        "ITZ-PCM4-C": "1yI8zc8loxkGvsw6WBVaWRg-quwARVU5u",
        "ITZ-PTC4": "1bth_2ajVjPdMVowF-GYKisJ30M_-5FKD",
        "ITZ-RMC": "1VdQbwJve2YGQ1mzdr4318d2nq0dP-zbF",
        "ITZ-SYNC2": "1ZBeV6LicGPwcOzxZT40KYmIEfw_CcfoC",
        "ITZ-TOY2": "1Oufe17b_u04GpY6cq0BPFumbWMmK9S1t",
        "ITZ-TRG": "1cLMWZ3zLrDbrhFtvQEEcvvE6H9WOsbdo",
        "ZW-FRD": "1gmLut_kzBsgxFb5SIdB-m2GEIUP5aiZq",
        "ZW-FSD": "1WIqBN7yhEbGWh2K_a86YofALlbpDrar6",
        "ZW-GM": "1yKdqlbFcBnvuWPYheTlQRVFyNiwSnwyD",
        "ZW-GMLC-EV": "1oWoMGamNoiimXYBMJFX-WQdNo4Ttgn4X",
        "ZW-RAM": "14_Pq_LKz5XdRaaBL6VRy7q_NovXKK6VH"
      };

      // Add bundled diagrams and fill known metadata gaps without replacing
      // values maintained through the Kit Editor. Keeping this in one function
      // makes fresh API data and localStorage cache behave identically.
      function applyKitExtras() {
        for (const [key, extra] of Object.entries(KIT_EXTRA)) {
          const kit = KITS[key];
          if (!kit) continue;
          for (const field of ['vehicle', 'switch_button', 'audio_method', 'notes']) {
            if (!kit[field] && extra[field]) kit[field] = extra[field];
          }
          if (extra.dip_switches) kit.dip_switches = extra.dip_switches;
          if (extra.wiring)       kit.wiring       = extra.wiring;
        }
        for (const [key, driveId] of Object.entries(OFFICIAL_MANUALS)) {
          if (!KITS[key]) continue;
          if (!IMAGES[key]) IMAGES[key] = {};
          if (!IMAGES[key].manual) {
            IMAGES[key].manual = 'https://drive.google.com/file/d/' + driveId + '/preview';
          }
        }
      }

      const sidebar = document.getElementById("sidebar");
      const content = document.getElementById("content");
      let activeKit = null;
      let activeTab = "troubleshoot"; // "troubleshoot", "dip", "wiring"
      let nodeStack = []; // stack of node ids for back navigation

      // Build sidebar
      // ── Zoom + pan ──
      var _ZS = {}, _ZX = {}, _ZY = {};
      var _mediaObjectUrls = [];

      function _mediaError(id, message) {
        var img = document.getElementById(id + 'I');
        var error = document.getElementById(id + 'E');
        var detail = document.getElementById(id + 'ED');
        if (img) img.style.display = 'none';
        if (detail && message) detail.textContent = message;
        if (error) error.style.display = 'flex';
      }

      async function _loadKitImage(id, src) {
        var img = document.getElementById(id + 'I');
        if (!img) return;

        // Kit media is private. A plain <img src> cannot attach the bearer token
        // kept in localStorage, so fetch same-origin media first and render a
        // browser-local object URL. Official legacy Drive images remain direct.
        if (!src.startsWith(SERVER_URL + '/images/')) {
          img.src = src;
          return;
        }

        try {
          var token = _authToken || localStorage.getItem(AUTH_TOKEN_KEY) || '';
          var response = await fetch(src, {
            cache: 'no-cache',
            headers: { 'Authorization': 'Bearer ' + token },
          });
          if (!response.ok) throw new Error('HTTP ' + response.status);
          var objectUrl = URL.createObjectURL(await response.blob());
          _mediaObjectUrls.push(objectUrl);
          img.src = objectUrl;
        } catch (error) {
          _mediaError(id, 'The kit file could not be loaded. Refresh and try again.');
        }
      }

      function makeZoomImg(src, alt) {
        var id = 'z' + (Math.random() * 1e9 | 0);
        var out =
          '<div class="manual-img-wrap">' +
            '<div class="zoom-controls">' +
              '<button class="zoom-btn" onclick="_zpOut(\'' + id + '\')">&#8722;</button>' +
              '<span class="zoom-label" id="' + id + 'L">…</span>' +
              '<button class="zoom-btn" onclick="_zpIn(\'' + id + '\')">+</button>' +
              '<button class="zoom-btn" onclick="_zpReset(\'' + id + '\')" style="font-size:12px;width:50px">Reset</button>' +
              '<span class="zoom-hint">drag to pan · pinch to zoom</span>' +
            '</div>' +
            '<div class="img-viewport" id="' + id + 'V">' +
              '<img id="' + id + 'I" alt="' + alt + '"' +
              ' onerror="_mediaError(\'' + id + '\')" />' +
              '<div id="' + id + 'E" style="display:none;flex-direction:column;align-items:center;justify-content:center;height:200px;color:#888;gap:8px;">' +
              '<div style="font-size:32px;">🖼️</div>' +
              '<div style="font-size:13px;">Image could not load.</div>' +
              '<div id="' + id + 'ED" style="font-size:11px;">Refresh and try again.</div>' +
              '</div>' +
            '</div>' +
          '</div>';
        // Poll until the element + image are ready
        var attempts = 0;
        function tryInit() {
          var vp = document.getElementById(id + 'V');
          var img = document.getElementById(id + 'I');
          if (vp && img && img.naturalWidth > 0) {
            _zpInit(id);
          } else if (attempts++ < 40) {
            setTimeout(tryInit, 50);
          }
        }
        setTimeout(function() {
          _loadKitImage(id, src);
          tryInit();
        }, 30);
        return out;
      }

      async function _loadKitManual(id, url) {
        var link = document.getElementById(id + 'A');
        var frame = document.getElementById(id + 'F');
        var status = document.getElementById(id + 'S');
        try {
          var token = _authToken || localStorage.getItem(AUTH_TOKEN_KEY) || '';
          var response = await fetch(url, {
            cache: 'no-cache',
            headers: { 'Authorization': 'Bearer ' + token },
          });
          if (!response.ok) throw new Error('HTTP ' + response.status);
          var objectUrl = URL.createObjectURL(await response.blob());
          _mediaObjectUrls.push(objectUrl);
          if (link) {
            link.href = objectUrl;
            link.removeAttribute('aria-disabled');
          }
          if (frame) frame.src = objectUrl;
          if (status) status.textContent = '';
        } catch (error) {
          if (status) status.textContent = 'Manual could not load. Refresh and try again.';
        }
      }

      function makePrivateManual(url, filename) {
        var id = 'm' + (Math.random() * 1e9 | 0);
        setTimeout(function() { _loadKitManual(id, url); }, 30);
        return '<a id="' + id + 'A" class="manual-open" href="#" aria-disabled="true" target="_blank" rel="noopener">' +
          '<span class="manual-open-ic">📄</span>' +
          '<span class="manual-open-txt"><b>Open manual</b>' +
            '<span class="manual-open-sub">' + filename + '</span></span>' +
          '<span class="manual-open-go">↗</span>' +
        '</a>' +
        '<div id="' + id + 'S" style="padding:8px 0;color:#888;font-size:12px;">Loading manual…</div>' +
        '<div class="manual-frame-wrap" style="width:100%;height:calc(100vh - 160px);min-height:500px;">' +
          '<iframe id="' + id + 'F" title="' + filename + '" width="100%" height="100%" style="border:none;border-radius:8px;"></iframe>' +
        '</div>';
      }

      // Apply scale + pan offset to image via absolute position
      function _zpApply(id) {
        var img = document.getElementById(id + 'I');
        var lbl = document.getElementById(id + 'L');
        if (!img) return;
        var s = _ZS[id] || 1, x = _ZX[id] || 0, y = _ZY[id] || 0;
        img.style.width  = Math.round(img.naturalWidth  * s) + 'px';
        img.style.height = Math.round(img.naturalHeight * s) + 'px';
        img.style.left   = Math.round(x) + 'px';
        img.style.top    = Math.round(y) + 'px';
        if (lbl) lbl.textContent = Math.round(s * 100) + '%';
      }

      // Clamp pan so image can't be dragged fully off screen
      function _zpClamp(id) {
        var vp  = document.getElementById(id + 'V');
        var img = document.getElementById(id + 'I');
        if (!vp || !img) return;
        var s = _ZS[id] || 1;
        var iw = img.naturalWidth * s, ih = img.naturalHeight * s;
        var vw = vp.clientWidth,       vh = vp.clientHeight;
        // Allow panning up to (imageSize - viewportSize) in both directions
        // but always show at least 40px of image
        var margin = 40;
        _ZX[id] = Math.max(Math.min(margin, _ZX[id]), Math.min(0, vw - iw + margin));
        _ZY[id] = Math.max(Math.min(margin, _ZY[id]), Math.min(0, vh - ih + margin));
      }

      function _zpFit(id) {
        var vp  = document.getElementById(id + 'V');
        var img = document.getElementById(id + 'I');
        if (!vp || !img || !img.naturalWidth) return;
        var s = Math.min(
          vp.clientWidth  / img.naturalWidth,
          vp.clientHeight / img.naturalHeight
        );
        _ZS[id] = s; _ZX[id] = 0; _ZY[id] = 0;
        _zpApply(id);
      }

      function _zpInit(id) {
        var vp  = document.getElementById(id + 'V');
        var img = document.getElementById(id + 'I');
        if (!vp || !img) return;

        _zpFit(id);

        // Mouse wheel zoom (pivot = cursor pos in viewport)
        vp.addEventListener('wheel', function(e) {
          e.preventDefault();
          var rect = vp.getBoundingClientRect();
          var mx = e.clientX - rect.left;
          var my = e.clientY - rect.top;
          var factor = e.deltaY < 0 ? 1.15 : (1 / 1.15);
          var oldS = _ZS[id];
          var newS = Math.max(0.05, Math.min(10, oldS * factor));
          // Adjust pan so point under cursor stays fixed
          _ZX[id] = mx - (mx - _ZX[id]) * (newS / oldS);
          _ZY[id] = my - (my - _ZY[id]) * (newS / oldS);
          _ZS[id] = newS;
          _zpClamp(id);
          _zpApply(id);
        }, { passive: false });

        // Mouse drag
        var dn = false, ox, oy, ox0, oy0;
        vp.addEventListener('mousedown', function(e) {
          dn = true; ox = e.clientX; oy = e.clientY;
          ox0 = _ZX[id]; oy0 = _ZY[id];
          vp.classList.add('panning');
          e.preventDefault();
        });
        window.addEventListener('mousemove', function(e) {
          if (!dn) return;
          _ZX[id] = ox0 + (e.clientX - ox);
          _ZY[id] = oy0 + (e.clientY - oy);
          _zpClamp(id);
          _zpApply(id);
        });
        window.addEventListener('mouseup', function() {
          if (dn) { dn = false; vp.classList.remove('panning'); }
        });

        // Touch: one-finger drag to pan, two-finger pinch to zoom
        var tx0, ty0, txp, typ;
        var pinchD0 = 0, pinchS0 = 1, pinchCX = 0, pinchCY = 0, pinchX0 = 0, pinchY0 = 0;
        function _tdist(t) {
          var dx = t[0].clientX - t[1].clientX, dy = t[0].clientY - t[1].clientY;
          return Math.sqrt(dx * dx + dy * dy);
        }
        vp.addEventListener('touchstart', function(e) {
          if (e.touches.length === 1) {
            tx0 = e.touches[0].clientX; ty0 = e.touches[0].clientY;
            txp = _ZX[id]; typ = _ZY[id];
          } else if (e.touches.length === 2) {
            var rect = vp.getBoundingClientRect();
            pinchD0 = _tdist(e.touches);
            pinchS0 = _ZS[id] || 1;
            pinchCX = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
            pinchCY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top;
            pinchX0 = _ZX[id]; pinchY0 = _ZY[id];
            e.preventDefault();
          }
        }, { passive: false });
        vp.addEventListener('touchmove', function(e) {
          if (e.touches.length === 2 && pinchD0 > 0) {
            var d = _tdist(e.touches);
            var newS = Math.max(0.05, Math.min(10, pinchS0 * (d / pinchD0)));
            // keep the point under the pinch midpoint fixed while scaling
            _ZX[id] = pinchCX - (pinchCX - pinchX0) * (newS / pinchS0);
            _ZY[id] = pinchCY - (pinchCY - pinchY0) * (newS / pinchS0);
            _ZS[id] = newS;
            _zpClamp(id); _zpApply(id);
            e.preventDefault();
          } else if (e.touches.length === 1 && pinchD0 === 0) {
            _ZX[id] = txp + (e.touches[0].clientX - tx0);
            _ZY[id] = typ + (e.touches[0].clientY - ty0);
            _zpClamp(id); _zpApply(id);
            e.preventDefault();
          }
        }, { passive: false });
        vp.addEventListener('touchend', function(e) {
          if (e.touches.length < 2) pinchD0 = 0;
          if (e.touches.length === 1) {   // re-anchor pan after lifting one finger
            tx0 = e.touches[0].clientX; ty0 = e.touches[0].clientY;
            txp = _ZX[id]; typ = _ZY[id];
          }
        }, { passive: true });
      }

      var _zpSteps = [0.05,0.1,0.15,0.25,0.33,0.5,0.67,0.75,1,1.25,1.5,2,3,4,6,8,10];

      function _zpIn(id) {
        var s = _ZS[id] || 1;
        var ns = _zpSteps.find(function(x) { return x > s + 0.01; }) || 10;
        _ZS[id] = ns; _zpClamp(id); _zpApply(id);
      }
      function _zpOut(id) {
        var s = _ZS[id] || 1, ns = 0.05;
        for (var i = _zpSteps.length - 1; i >= 0; i--) {
          if (_zpSteps[i] < s - 0.01) { ns = _zpSteps[i]; break; }
        }
        _ZS[id] = ns; _zpClamp(id); _zpApply(id);
      }
      function _zpReset(id) { _zpFit(id); }


      function buildSidebar(filter = "") {
        const f = filter.toLowerCase();
        let html = "";
        let count = 0;
        const sorted = Object.keys(KITS).sort();
        for (const key of sorted) {
          const k = KITS[key];
          const match =
            !f ||
            key.toLowerCase().includes(f) ||
            (k.vehicle || "").toLowerCase().includes(f);
          const cls = match ? "" : " hidden";
          const active = key === activeKit ? " active" : "";
          const shortVeh = (k.vehicle || "")
            .split("(")[0]
            .trim()
            .substring(0, 40);
          html += `<div class="kit-item${active}${cls}" data-key="${key}" onclick="selectKit('${key.replace(/'/g, "\'")}')">
      <div>
        <div class="kit-name">${key}</div>
        <div class="kit-vehicle">${shortVeh}</div>
      </div>
    </div>`;
          if (match) count++;
        }
        if (count === 0) html = '<div class="no-results">No kits found</div>';
        sidebar.innerHTML = html;
      }

      function selectKit(key) {
        activeKit = key;
        activeTab = "troubleshoot";
        nodeStack = ["start"];
        document.body.classList.add("kit-active");
        buildSidebar(document.getElementById("search").value);
        renderKit();
        if (window.matchMedia('(max-width: 640px)').matches) {
          _scrollPageTop();                 // mobile: detail takes over, start at top
        } else {
          const el = sidebar.querySelector(".active");
          if (el) el.scrollIntoView({ block: "nearest" });
        }
      }

      // Reliably return the page to the top after a mobile master/detail switch.
      // The scroll position can stick when a tall panel is shown/hidden, so we
      // reset after the layout reflows (rAF) and cover every scroll root.
      function _scrollPageTop() {
        function reset() {
          try { window.scrollTo(0, 0); } catch (e) {}
          if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
          if (document.documentElement) document.documentElement.scrollTop = 0;
          if (document.body) document.body.scrollTop = 0;
          var c = document.getElementById('content'); if (c) c.scrollTop = 0;
          var s = document.getElementById('sidebar'); if (s) s.scrollTop = 0;
        }
        reset();
        requestAnimationFrame(reset);
      }

      // Mobile: leave the kit detail and return to the full kit list.
      function backToKits() {
        activeKit = null;
        document.body.classList.remove("kit-active");
        var se = document.getElementById("search");
        buildSidebar(se ? se.value : "");
        var c = document.getElementById("content");
        if (c) c.innerHTML = EMPTY_STATE_HTML;   // back to the "select a kit" page
        _scrollPageTop();
      }

      function setTab(tab) {
        activeTab = tab;
        renderKit();
        content.scrollTo({ top: 0, behavior: "smooth" });
      }

            // ── Choice registry ─────────────────────────────────────────
      // Stores choice objects so onclick handlers never embed serialized JSON
      let _choiceReg = [];
      function _regChoice(obj) {
        _choiceReg.push(obj);
        return _choiceReg.length - 1;
      }
      function _clickChoice(idx) {
        const c = _choiceReg[idx];
        if (!c) return;
        if (nodeStack.length <= 1) { nodeStack = ['start', c]; }
        else { nodeStack.push(c); }
        renderKit();
        document.getElementById('content').scrollTo({ top: 99999, behavior: 'smooth' });
      }
      function _rerouteChoice(questionIndex, choiceIdx) {
        const c = _choiceReg[choiceIdx];
        if (!c) return;
        nodeStack = nodeStack.slice(0, questionIndex + 1);
        nodeStack.push(c);
        renderKit();
        document.getElementById('content').scrollTo({ top: 99999, behavior: 'smooth' });
      }

      function renderKit() {
        const k = KITS[activeKit];
        if (!k) return;
        _choiceReg = []; // Reset choice registry on each render

        const warn = k.notes
          ? `<div class="warning-banner">⚠️ <span>${k.notes}</span></div>`
          : "";

        // Tab bar
        const kitImgs = IMAGES[activeKit] || {};
        const hasDip = !!(kitImgs.dip) || !!(k.dip_switches && k.dip_switches.length > 0 && k.dip_switches.some(g => g.rows && g.rows.some(r => r.dips && r.dips.some(d => d !== 'N/A' && d !== '—' && d !== 'n/a'))));
        const hasWiring = !!(kitImgs.wiring) || !!(k.wiring && k.wiring.length > 0);
        const hasMisc   = !!(kitImgs.misc);
        const hasManual = !!(kitImgs.manual);
        let tabsHtml = `<div class="tab-bar">`;
        tabsHtml += `<button class="tab-btn${activeTab === 'troubleshoot' ? ' tab-active' : ''}" onclick="setTab('troubleshoot')">🔧 Troubleshoot</button>`;
        if (hasDip) {
          tabsHtml += `<button class="tab-btn${activeTab === 'dip' ? ' tab-active' : ''}" onclick="setTab('dip')">⚙️ DIP Switches</button>`;
        }
        if (hasWiring) {
          tabsHtml += `<button class="tab-btn${activeTab === 'wiring' ? ' tab-active' : ''}" onclick="setTab('wiring')">🔌 Wiring</button>`;
        }
        if (hasMisc) {
          tabsHtml += `<button class="tab-btn${activeTab === 'misc' ? ' tab-active' : ''}" onclick="setTab('misc')">📎 Misc</button>`;
        }
        if (hasManual) {
          tabsHtml += `<button class="tab-btn${activeTab === 'manual' ? ' tab-active' : ''}" onclick="setTab('manual')">📖 Manual</button>`;
        }
        tabsHtml += `</div>`;

        let mainContent = "";

        if (activeTab === "dip") {
          if (kitImgs.dip) {
            mainContent = makeZoomImg(kitImgs.dip, "DIP switches");
          } else {
          // DIP switch table
          let dipHtml = `<div class="dip-section">`;
          for (const group of k.dip_switches) {
            if (group.label) {
              dipHtml += `<div class="dip-group-label">${group.label}</div>`;
            }
            dipHtml += `<table class="dip-table"><thead><tr><th>Setting</th>`;
            for (let i = 1; i <= (group.count || 8); i++) {
              dipHtml += `<th>DIP ${i}</th>`;
            }
            dipHtml += `</tr></thead><tbody>`;
            for (const row of group.rows) {
              dipHtml += `<tr><td class="dip-desc">${row.desc}</td>`;
              for (const v of row.dips) {
                const cls = v === "ON" ? " dip-on" : v === "OFF" ? " dip-off" : " dip-na";
                dipHtml += `<td class="dip-val${cls}">${v}</td>`;
              }
              dipHtml += `</tr>`;
            }
            dipHtml += `</tbody></table>`;
            if (group.notes) {
              dipHtml += `<div class="dip-note">📝 ${group.notes}</div>`;
            }
          }
          dipHtml += `</div>`;
          mainContent = dipHtml;
          }

        } else if (activeTab === "wiring") {
          if (kitImgs.wiring) {
            mainContent = makeZoomImg(kitImgs.wiring, "Wiring diagram");
          } else {
          // Wiring info
          let wireHtml = `<div class="wiring-section">`;
          for (const section of k.wiring) {
            if (section.title) {
              wireHtml += `<div class="wire-section-title">${section.title}</div>`;
            }
            if (section.items) {
              wireHtml += `<table class="wire-table"><tbody>`;
              for (const item of section.items) {
                wireHtml += `<tr><td class="wire-label">${item.label}</td><td class="wire-desc">${item.desc}</td></tr>`;
              }
              wireHtml += `</tbody></table>`;
            }
            if (section.notes) {
              wireHtml += `<div class="dip-note">📝 ${section.notes}</div>`;
            }
          }
          wireHtml += `</div>`;
          mainContent = wireHtml;
          }

        } else if (activeTab === 'misc') {
          if (kitImgs.misc) {
            mainContent = makeZoomImg(kitImgs.misc, 'Misc diagram');
          } else {
            mainContent = '<div style="padding:24px;color:#888;">No misc image available.</div>';
          }
        } else if (activeTab === 'manual') {
          if (kitImgs.manual) {
            // Kit Editor manuals are private local/Storage files. Legacy kits
            // fall back to an allowlisted official ZZ2 Google Drive preview.
            var manualAsset = kitImgs.manual;
            var officialManual = /^https:\/\/drive\.google\.com\/file\/d\/[A-Za-z0-9_-]+\/preview$/.test(manualAsset);
            var manualFilename = officialManual ? activeKit + ' — official manual' : manualAsset;
            var manualUrl = officialManual
              ? manualAsset
              : SERVER_URL + '/manuals/' + encodeURIComponent(activeKit) + '/' + encodeURIComponent(manualAsset);
            mainContent = officialManual ?
              '<a class="manual-open" href="' + manualUrl + '" target="_blank" rel="noopener">' +
                '<span class="manual-open-ic">📄</span>' +
                '<span class="manual-open-txt"><b>Open manual</b>' +
                  '<span class="manual-open-sub">' + manualFilename + '</span></span>' +
                '<span class="manual-open-go">↗</span>' +
              '</a>' +
              '<div class="manual-frame-wrap" style="width:100%;height:calc(100vh - 160px);min-height:500px;">' +
                '<iframe src="' + manualUrl + '" width="100%" height="100%" style="border:none;border-radius:8px;"></iframe>' +
              '</div>' : makePrivateManual(manualUrl, manualFilename);
          } else {
            mainContent = '<div style="padding:24px;color:#888;">No manual available for this kit.</div>';
          }
        } else {
          // Troubleshoot tab — flowchart
          let nodesHtml = "";
          for (let i = 0; i < nodeStack.length; i++) {
            const nodeId = nodeStack[i];
            const node = (typeof nodeId === 'object') ? nodeId : k.nodes.find((n) => n.id === nodeId);
            if (!node) continue;
            const isLast = i === nodeStack.length - 1;

            if (node.type === "question") {
              let choicesHtml = "";
              for (const c of node.choices) {
                const nextInStack = nodeStack[i + 1];
                const nextInStackText = nextInStack
                  ? (typeof nextInStack === 'object' ? (nextInStack.label || nextInStack.text) : nextInStack)
                  : null;
                const isSelected = !isLast && nextInStack &&
                  ((c.next && c.next === nextInStack) ||
                   (c.text && c.text === nextInStackText) ||
                   (c.label && c.label === nextInStackText));
                const selCls = isSelected ? " selected" : "";
                const cIdx = _regChoice(c);
                const clickFn = isLast
                  ? `_clickChoice(${cIdx})`
                  : `_rerouteChoice(${i}, ${cIdx})`;
                choicesHtml += `<button class="choice-btn${selCls}" onclick="${clickFn}">
          <span>${c.label || c.text}</span>
          <span class="choice-arrow">${isSelected ? "✓" : "→"}</span>
        </button>`;
              }
              nodesHtml += `<div class="node-wrap">
        <div class="node-question">
          <div class="node"><div class="node-head">${node.text}</div><div class="choices">${choicesHtml}</div></div>
        </div>
      </div>`;

            } else if (node.type === 'fix-and-question') {
              // Has both steps AND sub-choices
              let stepsHtml = '';
              let stepN = 1;
              let ci = 0;
              for (const step of (node.steps || [])) {
                const isWarn = step.startsWith('⚠️');
                const txt = isWarn ? step.substring(2).trim() : step;
                const cbId = 'cb_' + i + '_' + (ci++);
                if (!isWarn) {
                  stepsHtml += `<div class="step-row step-row-check" onclick="toggleStepCheck('${cbId}',this)">
          <input type="checkbox" id="${cbId}" class="step-checkbox" data-step="${_escA(txt)}" onclick="event.stopPropagation();" onchange="syncStepCheck('${cbId}',this,this.closest('.step-row-check'))">
          <div class="step-num step-num-check">${stepN++}</div>
          <div class="step-text">${txt}</div>
        </div>`;
                } else {
                  stepsHtml += `<div class="step-row step-row-check step-warn" onclick="toggleStepCheck('${cbId}',this)">
          <input type="checkbox" id="${cbId}" class="step-checkbox" data-step="${_escA('⚠️ ' + txt)}" onclick="event.stopPropagation();" onchange="syncStepCheck('${cbId}',this,this.closest('.step-row-check'))">
          <div class="step-num">!</div>
          <div class="step-text warn-text">${txt}</div>
        </div>`;
                }
              }
              let subChoicesHtml = '';
              for (const c of (node.choices || [])) {
                const cIdx = _regChoice(c);
                const clickFn = isLast
                  ? `_clickChoice(${cIdx})`
                  : `_rerouteChoice(${i}, ${cIdx})`;
                subChoicesHtml += `<button class="choice-btn" onclick="${clickFn}">
          <span>${c.label || c.text}</span><span class="choice-arrow">→</span>
        </button>`;
              }
              nodesHtml += `<div class="node-wrap"><div class="node-fix">
        <div class="node"><div class="node-head">✓ ${node.text}</div>
        <div class="steps-list">${stepsHtml}</div>
        <div class="choices" style="margin-top:12px;border-top:1px solid #eee;padding-top:12px;">${subChoicesHtml}</div>
        </div></div></div>`;

            } else if (node.type === "fix") {
              let stepsHtml = "";
              let stepN = 1;
              let ci = 0;
              for (const step of node.steps) {
                const isWarn = step.startsWith("⚠️");
                const txt = isWarn ? step.substring(2).trim() : step;
                const cbId = 'cb_' + i + '_' + (ci++);
                if (!isWarn) {
                  stepsHtml += `<div class="step-row step-row-check" onclick="toggleStepCheck('${cbId}',this)">
          <input type="checkbox" id="${cbId}" class="step-checkbox" data-step="${_escA(txt)}" onclick="event.stopPropagation();" onchange="syncStepCheck('${cbId}',this,this.closest('.step-row-check'))">
          <div class="step-num step-num-check">${stepN++}</div>
          <div class="step-text">${txt}</div>
        </div>`;
                } else {
                  stepsHtml += `<div class="step-row step-row-check step-warn" onclick="toggleStepCheck('${cbId}',this)">
          <input type="checkbox" id="${cbId}" class="step-checkbox" data-step="${_escA('⚠️ ' + txt)}" onclick="event.stopPropagation();" onchange="syncStepCheck('${cbId}',this,this.closest('.step-row-check'))">
          <div class="step-num">!</div>
          <div class="step-text warn-text">${txt}</div>
        </div>`;
                }
              }
              nodesHtml += `<div class="node-wrap">
        <div class="node-fix">
          <div class="node"><div class="node-head">✓ ${node.text}</div><div class="steps-list">${stepsHtml}</div></div>
        </div>
      </div>`;
            }

            // Connector between nodes
            if (i < nodeStack.length - 1) {
              nodesHtml += `<div class="connector"><div class="connector-dot"></div></div>`;
            }
          }

          // Back button + "Apply to ticket" once a path has been chosen.
          let btnsHtml = "";
          if (nodeStack.length > 1) {
            btnsHtml += `<div style="margin-top:16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <button class="back-btn" style="margin-top:0" onclick="goBack()">← Back</button>
      <button id="apply-path-btn" class="apply-path-btn" style="margin-top:0" onclick="applyPathToTicket()">➕ Apply to ticket</button>
      <span class="apply-hint">Path → Issue · checked steps → Resolution</span>
    </div>`;
          }

          mainContent = `<div class="flowchart">${nodesHtml}</div>${btnsHtml}`;
        }

        const _canEditKit = (typeof _hasPerm === 'function') && _hasPerm('kit_editor');
        const _editKitBtn = _canEditKit
          ? `<button class="kit-edit-btn" onclick="window.location.href='/kit-editor.html?kit=${encodeURIComponent(activeKit)}'" title="Open this kit in the editor">✏️ Edit this kit</button>`
          : "";

        content.innerHTML = `
    <div class="kit-header">
      <button class="kit-back" onclick="backToKits()">← All kits</button>
      ${_editKitBtn}
      <div class="kit-title">${activeKit}</div>
      <div class="kit-sub">${k.vehicle || ""}</div>
      <div class="kit-meta">
        <div class="meta-pill">🔘 <span><b>Switch:</b> ${k.switch_button || "—"}</span></div>
        <div class="meta-pill">🔊 <span><b>Audio:</b> ${k.audio_method || "—"}</span></div>
      </div>
    </div>
    ${warn}
    ${tabsHtml}
    ${mainContent}
  `;
      }

      
      // ── Choice registry ───────────────────────────────────────────
      // Stores inline choice objects so onclick handlers never embed

      function goNode(nodeId) {
        const k = KITS[activeKit];
        // Resolve a text string to the actual inline choice object
        function resolveNode(ref) {
          if (typeof ref === 'object') return ref;
          // Try by id first
          const byId = k.nodes.find(n => n.id === ref);
          if (byId) return byId;
          // Try by text/label in the start node choices
          const startNode = k.nodes.find(n => n.id === 'start');
          if (startNode) {
            const match = startNode.choices.find(c =>
              (c.text && c.text.trim() === ref.trim()) ||
              (c.label && c.label.trim() === ref.trim())
            );
            if (match) return match;
          }
          return ref; // fallback
        }
        const resolved = resolveNode(nodeId);
        if (nodeStack.length <= 1) {
          nodeStack = ['start', resolved];
        } else {
          nodeStack.push(resolved);
        }
        renderKit();
        content.scrollTo({ top: content.scrollHeight, behavior: "smooth" });
      }

      // Re-route from a past question at index i, choosing a new path

      function goBack() {
        if (nodeStack.length > 1) {
          nodeStack.pop();
          renderKit();
        }
      }

      document.getElementById("search").addEventListener("input", function () {
        buildSidebar(this.value);
      });

      loadFromSheets();
      authInit();
