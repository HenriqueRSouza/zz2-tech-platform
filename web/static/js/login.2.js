    const SERVER_URL = window.location.origin;
    const AUTH_KEY   = 'zz2_auth_token';
    const ROLE_KEY   = 'zz2_auth_role';
    let sbAuth = null;

    async function initSupabaseAuth() {
      var errEl = document.getElementById('error-msg');
      try {
        var cfg = await fetch(SERVER_URL + '/api/config', {cache:'no-store'}).then(function(r){return r.json();});
        if (!window.supabase || !cfg.supabase_url || !cfg.supabase_publishable_key) {
          throw new Error('Supabase Auth is not configured.');
        }
        sbAuth = window.supabase.createClient(cfg.supabase_url, cfg.supabase_publishable_key);
        var result = await sbAuth.auth.getSession();
        var session = result.data && result.data.session;
        if (session) {
          localStorage.setItem(AUTH_KEY, session.access_token);
          redirect();
        }
      } catch (err) {
        errEl.textContent = err.message || 'Could not initialize authentication.';
      }
    }
    window.addEventListener('load', initSupabaseAuth);

    function redirect(role) {
      // All roles land on the dashboard now; it shows role-appropriate cards
      // (techs see Tool + Tickets; admins/super-admins see everything).
      window.location.href = '/dashboard.html';
    }

    async function doLogin() {
      var email    = document.getElementById('username').value.trim();
      var password = document.getElementById('password').value.trim();
      var errEl    = document.getElementById('error-msg');
      var btn      = document.getElementById('login-btn');

      errEl.textContent = '';
      if (!email || !password) {
        errEl.textContent = 'Enter your email and password.';
        return;
      }
      if (!sbAuth) {
        errEl.textContent = 'Authentication is still loading. Try again.';
        return;
      }

      btn.disabled = true;
      document.getElementById('spinner').classList.add('active');

      try {
        var result = await sbAuth.auth.signInWithPassword({email: email, password: password});
        if (result.error) throw result.error;
        var session = result.data && result.data.session;
        if (!session) throw new Error('Supabase did not return a session.');
        btn.disabled = false;
        document.getElementById('spinner').classList.remove('active');
        localStorage.setItem(AUTH_KEY, session.access_token);
        var meta = (session.user && (session.user.app_metadata || session.user.user_metadata)) || {};
        localStorage.setItem(ROLE_KEY, meta.role || 'tech');
        redirect();
      } catch (err) {
        btn.disabled = false;
        document.getElementById('spinner').classList.remove('active');
        errEl.textContent = err.message || 'Invalid email or password.';
        document.getElementById('password').value = '';
        document.getElementById('password').focus();
      }
    }
