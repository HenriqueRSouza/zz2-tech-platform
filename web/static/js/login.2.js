    const SERVER_URL = window.location.origin;
    const AUTH_KEY   = 'zz2_auth_token';
    const ROLE_KEY   = 'zz2_auth_role';
    const DEFAULT_NEXT = '/dashboard.html';

    /* Sessions are issued by this server (8 h) and kept in localStorage under
     * AUTH_KEY; every page sends them as "Authorization: Bearer" through
     * session.js, which also sends the user back here on a 401. There is no
     * refresh token: when the session expires, the user signs in again. */

    var SSO_ERRORS = {
      cancelled: 'O login com a Microsoft foi cancelado.',
      rejected: 'Não foi possível concluir o login com a Microsoft. Tente novamente.',
      denied: 'Esta conta Microsoft não tem permissão para acessar a plataforma. Fale com um administrador.',
      expired: 'O link de login expirou. Tente novamente.'
    };

    /* Where to go after signing in: ?next=<same-site path>, else the dashboard.
     * Anything that is not a plain same-site path ("//host", "/\host",
     * "https://...") is ignored so this page cannot become an open redirect. */
    function nextPath() {
      var value = new URLSearchParams(window.location.search).get('next') || '';
      if (!/^\/[A-Za-z0-9._~\/?=&%#-]*$/.test(value) || value.indexOf('//') === 0 ||
          value.indexOf('/login.html') === 0) {
        return DEFAULT_NEXT;
      }
      return value;
    }

    function setBusy(busy) {
      var btn = document.getElementById('login-btn');
      if (btn) btn.disabled = busy;
      document.getElementById('spinner').classList.toggle('active', busy);
    }

    async function errorMessage(res, fallback) {
      if (res.status === 429) return 'Muitas tentativas. Aguarde alguns minutos e tente novamente.';
      try {
        var body = await res.json();
        if (body && body.error) return body.error;
      } catch (e) { /* not JSON */ }
      return fallback;
    }

    function storeSession(token, role) {
      localStorage.setItem(AUTH_KEY, token);
      localStorage.setItem(ROLE_KEY, role || 'tech');
    }

    /* Returning from Microsoft (or SAML): the server redirected to #sso=<code>,
     * a single-use code that is exchanged here for the session token. The code
     * travels in the fragment so it never reaches a server log or a Referer,
     * and the fragment is wiped from history before anything else happens. */
    async function completeSso() {
      var hash = window.location.hash || '';
      var match = /^#(sso|sso_error)=([A-Za-z0-9_.~-]+)$/.exec(hash);
      if (!match) return false;
      history.replaceState(null, '', window.location.pathname + window.location.search);
      var errEl = document.getElementById('error-msg');
      if (match[1] === 'sso_error') {
        errEl.textContent = SSO_ERRORS[match[2]] || SSO_ERRORS.rejected;
        return false;
      }
      setBusy(true);
      try {
        var res = await fetch(SERVER_URL + '/api/auth/sso/exchange', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({code: match[2]})
        });
        if (!res.ok) throw new Error(await errorMessage(res, SSO_ERRORS.expired));
        var body = await res.json();
        if (!body.token) throw new Error(SSO_ERRORS.rejected);
        var who = await fetch(SERVER_URL + '/api/session/verify', {
          method: 'POST', headers: {'Authorization': 'Bearer ' + body.token}
        }).then(function(r){ return r.ok ? r.json() : {}; }).catch(function(){ return {}; });
        storeSession(body.token, who.role);
        window.location.href = body.next || nextPath();
        return true;
      } catch (err) {
        setBusy(false);
        errEl.textContent = err.message || SSO_ERRORS.rejected;
        return false;
      }
    }

    /* A token left over from an earlier visit: follow it only if the server
     * still accepts it. Redirecting on any stored token loops forever when the
     * server rejects it (the dashboard sends us back here, and the same token
     * sends us there again). */
    async function resumeSession() {
      var stored = localStorage.getItem(AUTH_KEY);
      if (!stored) return false;
      var check = await fetch(SERVER_URL + '/api/session/verify', {
        method: 'POST', headers: {'Authorization': 'Bearer ' + stored}
      }).catch(function(){ return null; });
      if (check && check.ok) {
        window.location.href = nextPath();
        return true;
      }
      localStorage.removeItem(AUTH_KEY);
      localStorage.removeItem(ROLE_KEY);
      return false;
    }

    async function initAuth() {
      var errEl = document.getElementById('error-msg');
      try {
        if (await completeSso()) return;
        var cfg = await fetch(SERVER_URL + '/api/config', {cache:'no-store'}).then(function(r){return r.json();});
        var next = encodeURIComponent(nextPath());
        var passwordOn = cfg.password_login_enabled !== false;
        if (!passwordOn) document.getElementById('password-block').hidden = true;
        if (cfg.microsoft_enabled || cfg.saml_enabled) {
          document.getElementById('sso-block').hidden = false;
          document.getElementById('sso-sep').hidden = !passwordOn;
        }
        if (cfg.microsoft_enabled) {
          var ms = document.getElementById('ms-btn');
          ms.href = '/api/auth/microsoft/login?next=' + next;
          ms.hidden = false;
        }
        if (cfg.saml_enabled) {
          var saml = document.getElementById('sso-btn');
          saml.href = '/api/auth/saml/login?next=' + next;
          saml.hidden = false;
        }
        if (!errEl.textContent) await resumeSession();
      } catch (err) {
        errEl.textContent = 'Não foi possível carregar a configuração de login. Recarregue a página.';
      }
    }
    window.addEventListener('load', initAuth);

    async function doLogin() {
      var email    = document.getElementById('username').value.trim();
      var password = document.getElementById('password').value;
      var errEl    = document.getElementById('error-msg');

      errEl.textContent = '';
      if (!email || !password) {
        errEl.textContent = 'Informe seu e-mail e senha.';
        return;
      }

      setBusy(true);
      try {
        var res = await fetch(SERVER_URL + '/api/auth/login', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({email: email, password: password})
        });
        if (!res.ok) throw new Error(await errorMessage(res, 'E-mail ou senha inválidos.'));
        var body = await res.json();
        if (!body.token) throw new Error('O servidor não retornou uma sessão.');
        storeSession(body.token, body.user && body.user.role);
        window.location.href = nextPath();
      } catch (err) {
        setBusy(false);
        errEl.textContent = err.message || 'E-mail ou senha inválidos.';
        document.getElementById('password').value = '';
        document.getElementById('password').focus();
      }
    }
