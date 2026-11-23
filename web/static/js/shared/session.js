/* ZZ2 shared session helper.
 *
 * Every page reimplemented the same three things: read the token from
 * localStorage, POST it to /verify, redirect to the sign-in page when that fails.
 * Eight copies, each slightly different, and all of them sent the token in the
 * request body:
 *
 *     fetch('/verify', { method: 'POST',
 *                        headers: { 'Content-Type': 'application/json' },
 *                        body: JSON.stringify({ token: TOKEN }) })
 *
 * A credential in a request body ends up in any proxy that logs bodies, and the
 * request can be triggered cross-site by a plain form post. The server no longer
 * accepts it: tokens must arrive in the Authorization header. This module is how
 * the pages do that, in one place.
 *
 * Deliberately a classic script assigning to window.ZZ2 rather than an ES module.
 * The pages are classic scripts loaded in document order; switching them to
 * modules would change their execution timing, which is a much larger change than
 * the security fix needs.
 */
(function () {
  'use strict';

  var AUTH_KEY = 'zz2_auth_token';
  var ROLE_KEY = 'zz2_auth_role';
  var LOGIN_PAGE = '/login.html';

  /* The auth client the pages used before the move to server-issued sessions
   * left a refresh session under sb-<ref>-auth-token. Nothing reads it any
   * more, but it is a credential sitting in storage, so signing out removes it. */
  var LEGACY_AUTH_KEY_PATTERN = /^sb-.*-auth-token$/;

  function token() {
    try {
      return localStorage.getItem(AUTH_KEY) || '';
    } catch (e) {
      /* Private browsing and blocked storage both throw here. Treat it as
       * signed out rather than letting the exception break the page. */
      return '';
    }
  }

  function setToken(value) {
    try {
      localStorage.setItem(AUTH_KEY, value);
    } catch (e) { /* nothing useful to do */ }
  }

  function setRole(value) {
    try {
      localStorage.setItem(ROLE_KEY, value || 'tech');
    } catch (e) { /* nothing useful to do */ }
  }

  function clearSession() {
    try {
      localStorage.removeItem(AUTH_KEY);
      localStorage.removeItem(ROLE_KEY);
      for (var i = localStorage.length - 1; i >= 0; i--) {
        var key = localStorage.key(i);
        if (key && LEGACY_AUTH_KEY_PATTERN.test(key)) localStorage.removeItem(key);
      }
    } catch (e) { /* nothing useful to do */ }
  }

  function authHeaders(extra) {
    var headers = { 'Authorization': 'Bearer ' + token() };
    if (extra) {
      for (var key in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, key)) headers[key] = extra[key];
      }
    }
    return headers;
  }

  function jsonHeaders() {
    return authHeaders({ 'Content-Type': 'application/json' });
  }

  function redirectToLogin() {
    clearSession();
    window.location.href = LOGIN_PAGE;
  }

  /* Confirm the session and return the caller's identity.
   *
   * Returns the fetch Response, not the parsed body, so the existing call sites
   * keep working unchanged — they all do `.then(r => r.json())` or
   * `await r.json()`. The accessToken argument is accepted and ignored for the
   * same reason: the pages pass their local TOKEN variable, and the real token
   * now travels in the header. */
  function verifySession(accessToken) {
    var headers = accessToken
      ? { 'Authorization': 'Bearer ' + accessToken }
      : authHeaders();
    return fetch('/api/session/verify', { method: 'POST', headers: headers });
  }

  /* Tell the server to drop its cached principal. Returns the fetch promise so
   * an existing `.catch(function(){})` at the call site still chains.
   *
   * Does not clear local storage or redirect: the call sites that use this
   * already do both, and doing it here as well would fight them. Use logout()
   * for the complete flow. */
  function logoutRequest() {
    return fetch('/api/session/logout', { method: 'POST', headers: authHeaders() });
  }

  /* Sign out completely. Fire-and-forget on the network: the server has only a
   * short-lived cache to drop, and the local session must be cleared whether or
   * not the call lands. */
  function logout() {
    try {
      logoutRequest().catch(function () {});
    } catch (e) { /* nothing useful to do */ }
    clearSession();
    window.location.href = LOGIN_PAGE;
  }

  /* Authenticated fetch, with a 401 sending the user to sign in.
   *
   * Without the 401 handling, an expired token turns every page into a wall of
   * "Forbidden" errors instead of a redirect to the sign-in form. */
  function api(method, path, body) {
    var options = { method: method, headers: authHeaders() };
    if (body !== undefined && body !== null) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(function (response) {
      if (response.status === 401) {
        redirectToLogin();
        throw new Error('Session expired');
      }
      return response;
    });
  }

  function apiJson(method, path, body) {
    return api(method, path, body).then(function (response) {
      return response.json();
    });
  }

  window.ZZ2 = {
    AUTH_KEY: AUTH_KEY,
    ROLE_KEY: ROLE_KEY,
    LOGIN_PAGE: LOGIN_PAGE,
    token: token,
    setToken: setToken,
    setRole: setRole,
    clearSession: clearSession,
    authHeaders: authHeaders,
    jsonHeaders: jsonHeaders,
    redirectToLogin: redirectToLogin,
    verifySession: verifySession,
    logoutRequest: logoutRequest,
    logout: logout,
    api: api,
    apiJson: apiJson
  };
})();
