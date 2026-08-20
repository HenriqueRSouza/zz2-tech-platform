#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ZZ2 Tech Support Tool - Server
Handles static file serving, authentication, and API proxy to Apps Script.
"""
import os, json, hashlib, hmac, time, threading, shutil, datetime, copy
import BaseHTTPServer, SimpleHTTPServer, SocketServer, urllib2, urllib
import base64, re as _re

# -- Paths ---------------------------------------------------------------------
ZZ2_BUILD    = '2026-06-16-health'   # bump on deploy to verify which server is live
BASE_DIR     = os.environ.get('ZZ2_BASE_DIR') or '/mnt/disk1/share/ZZ2 Tech Support Tool'
CREDS_FILE   = os.path.join(BASE_DIR, 'credentials.json')
ROLES_FILE   = os.path.join(BASE_DIR, 'roles.json')
CONFIG_FILE  = os.path.join(BASE_DIR, 'config.json')
SESSIONS_FILE= os.path.join(BASE_DIR, 'sessions.json')
STATS_FILE   = os.path.join(BASE_DIR, 'stats.json')
ANALYTICS_FILE=os.path.join(BASE_DIR, 'analytics.json')
AUDIT_LOG    = os.path.join(BASE_DIR, 'logs', 'audit.log')
SERVER_LOG   = os.path.join(BASE_DIR, 'logs', 'server.log')
DRAFTS_DIR   = os.path.join(BASE_DIR, 'drafts')
BACKUPS_DIR  = os.path.join(BASE_DIR, 'backups')
CACHE_DIR    = os.path.join(BASE_DIR, 'cache')
DATA_DIR     = os.path.join(BASE_DIR, 'data')
IMAGES_DIR   = os.path.join(BASE_DIR, 'images')
MANUALS_DIR  = os.path.join(BASE_DIR, 'manuals')
KIT_HISTORY_DIR = os.path.join(BASE_DIR, 'data', 'kit_history')
KITS_JSON    = os.path.join(DATA_DIR, 'kits.json')
TS_JSON      = os.path.join(DATA_DIR, 'troubleshooting.json')
BLUEPRINTS_JSON = os.path.join(DATA_DIR, 'blueprints.json')
CATALOG_META_JSON = os.path.join(DATA_DIR, 'catalog_meta.json')
PENDING_FILE = os.path.join(BASE_DIR, 'pending_writes.json')
DEAD_LETTERS_FILE = os.path.join(BASE_DIR, 'dead_letters.json')
LOGIN_FAILS_FILE  = os.path.join(BASE_DIR, 'login_fails.json')

SESSION_TTL  = 86400 * 7   # 7 days
MAX_FAILS    = 5            # lockout after 5 failed attempts
LOCKOUT_TIME = 300          # 5 minute lockout
WORK_START   = 9 * 3600 + 30 * 60   # 9:30 AM in seconds from midnight
WORK_END     = 20 * 3600             # 8:00 PM in seconds from midnight
MAX_BODY     = 32 * 1024 * 1024      # 32 MB cap on request bodies (memory-DoS guard)
AUTO_RESOLVE_DAYS = 7   # blank / waiting-for-feedback tickets older than this auto-resolve

_lock = threading.Lock()
_ARGV0 = [None]   # absolute path to this script, captured at launch (for restart)

# Telemetry counters surfaced on the System Settings page (single-element lists
# so the background loops can update them without a `global` declaration).
_START_TIME     = time.time()
_LAST_POLL      = [0.0]
_LAST_RECONCILE = [0.0]
_POLL_ADDED     = [0]
_CPU_PREV       = [None]   # (total, idle) from last /proc/stat sample

# -- Startup -------------------------------------------------------------------
def _ensure_dirs():
    for d in [DRAFTS_DIR, BACKUPS_DIR, CACHE_DIR, DATA_DIR,
              IMAGES_DIR, MANUALS_DIR, KIT_HISTORY_DIR,
              os.path.join(BASE_DIR, 'logs')]:
        if not os.path.exists(d):
            os.makedirs(d)

def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def _save_json(path, data):
    # Atomic write: serialize to a temp file, fsync, then rename into place. A
    # crash or power loss mid-write can no longer truncate the real file (this
    # protects sessions/roles/credentials/pending_writes/config on a NAS that
    # can lose power). os.rename is atomic on the same filesystem.
    tmp = '%s.tmp.%d' % (path, os.getpid())
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
        # Local kit data is authoritative; mirror it to Supabase shortly after
        # each atomic save. The globals guard keeps early startup writes safe.
        if path in (KITS_JSON, TS_JSON, BLUEPRINTS_JSON) \
           and '_schedule_auxiliary_sync' in globals():
            _schedule_auxiliary_sync()
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

def _backup_file(path):
    """Snapshot a single file into backups/pre_<ts>/ before a risky rewrite."""
    try:
        if not os.path.exists(path):
            return False
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        dest = os.path.join(BACKUPS_DIR, 'pre_' + ts)
        if not os.path.exists(dest):
            os.makedirs(dest)
        shutil.copy2(path, dest)
        return True
    except Exception:
        return False

def _log(msg, path=SERVER_LOG):
    try:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(path, 'a') as f:
            f.write('[%s] %s\n' % (ts, msg))
    except:
        pass

def _redact(s):
    """Strip the Apps Script secret key out of anything we log."""
    return _re.sub(r'(key=)[^&\s]+', r'\1***', str(s))

# -- Sessions ------------------------------------------------------------------
def _load_sessions():
    sessions = _load_json(SESSIONS_FILE, {})
    now = time.time()
    # Clean expired
    cleaned = {k: v for k, v in sessions.items() if v.get('expires', 0) > now}
    if len(cleaned) != len(sessions):
        _save_json(SESSIONS_FILE, cleaned)
    return cleaned

def _save_session(token, data):
    with _lock:
        sessions = _load_sessions()
        sessions[token] = data
        _save_json(SESSIONS_FILE, sessions)

def _delete_session(token):
    with _lock:
        sessions = _load_sessions()
        if token in sessions:
            del sessions[token]
            _save_json(SESSIONS_FILE, sessions)

def _get_session(token):
    sessions = _load_sessions()
    s = sessions.get(token)
    if s and s.get('expires', 0) > time.time():
        return s
    return _get_supabase_session(token)

# Supabase Auth access tokens are accepted alongside the legacy NAS sessions.
# This keeps every existing authenticated API working while credentials and
# session refresh are owned by Supabase Auth. Tokens are checked with Supabase's
# /auth/v1/user endpoint (signature/expiry included) and cached briefly so a page
# full of parallel API calls does not create a matching burst of auth requests.
_supabase_session_cache = {}
_supabase_session_lock = threading.Lock()
_SUPABASE_SESSION_TTL = 60

def _get_supabase_session(token):
    if not token or token.count('.') != 2:
        return None
    now = time.time()
    with _supabase_session_lock:
        cached = _supabase_session_cache.get(token)
        if cached and cached[1] > now:
            return cached[0]

    cfg = _load_json(CONFIG_FILE, {})
    base = (cfg.get('supabase_url') or '').rstrip('/')
    public_key = (cfg.get('supabase_publishable_key') or
                  cfg.get('supabase_anon_key') or '')
    if not base or not public_key:
        return None
    req = urllib2.Request(base + '/auth/v1/user', headers={
        'apikey': public_key,
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json',
        'User-Agent': 'ZZ2Server/1.0'})
    try:
        raw = json.loads(urllib2.urlopen(req, timeout=10).read())
    except Exception:
        return None

    app_meta = raw.get('app_metadata') or {}
    user_meta = raw.get('user_metadata') or {}
    email = raw.get('email') or ''
    display = (user_meta.get('display_name') or user_meta.get('full_name') or
               user_meta.get('name') or email or raw.get('id') or 'User')
    role = app_meta.get('role') or user_meta.get('role') or 'tech'
    permissions = app_meta.get('permissions') or user_meta.get('permissions') or []
    if not isinstance(permissions, list):
        permissions = []
    user = {
        'id': raw.get('id'), 'username': email, 'display': display,
        'role': role, 'permissions': permissions, 'active': True,
        '_supabase': True,
    }
    session = {
        'username': email, 'display': display, 'role': role,
        'expires': now + _SUPABASE_SESSION_TTL, '_supabase': True,
        '_supabase_user': user,
    }
    with _supabase_session_lock:
        # Keep this process cache bounded on long-running NAS installations.
        if len(_supabase_session_cache) > 500:
            for key, value in list(_supabase_session_cache.items()):
                if value[1] <= now:
                    del _supabase_session_cache[key]
        _supabase_session_cache[token] = (session, now + _SUPABASE_SESSION_TTL)
    return session

# -- Auth ----------------------------------------------------------------------
# Failed-login throttling. Keyed by "username|ip" so an attacker hammering one
# username can't lock the real user out from their own machine, and persisted to
# disk so a restart doesn't silently wipe an active lockout.
_fails_lock = threading.Lock()
def _fail_key(username, ip):
    return '%s|%s' % ((username or '').lower(), ip or '')
_login_fails = _load_json(LOGIN_FAILS_FILE, {})
if not isinstance(_login_fails, dict):
    _login_fails = {}

def _make_token(username):
    # Cryptographically strong, unguessable session token. Independent of the
    # username or login time, so it can't be predicted or brute-forced.
    return hashlib.sha256(os.urandom(32)).hexdigest()

def _consteq(a, b):
    """Constant-time string comparison (mitigates timing attacks)."""
    a = '' if a is None else str(a)
    b = '' if b is None else str(b)
    try:
        return hmac.compare_digest(a, b)
    except AttributeError:
        if len(a) != len(b):
            return False
        r = 0
        for x, y in zip(a, b):
            r |= ord(x) ^ ord(y)
        return r == 0

def _pbkdf2_pure(password, salt, iterations, dklen=32):
    """Pure-Python PBKDF2-HMAC-SHA256, needed on Python builds older than 2.7.8
    that lack hashlib.pbkdf2_hmac. Optimized for the slow path: the HMAC inner/
    outer pads are derived from the password ONCE and reused via .copy() each
    round (instead of a fresh hmac.new() every time), and the per-round XOR
    accumulates as a big integer. Several times faster than the naive loop, with
    byte-for-byte identical output, so existing password hashes still verify."""
    import struct, binascii
    # Build the HMAC key block (SHA-256 block size = 64 bytes), same as hmac does.
    if len(password) > 64:
        password = hashlib.sha256(password).digest()
    password = password + b'\x00' * (64 - len(password))
    pw = bytearray(password)
    inner = hashlib.sha256(bytes(bytearray((b ^ 0x36) for b in pw)))
    outer = hashlib.sha256(bytes(bytearray((b ^ 0x5c) for b in pw)))

    def _prf(msg):
        ic = inner.copy(); ic.update(msg)
        oc = outer.copy(); oc.update(ic.digest())
        return oc.digest()

    dk = b''
    block = 1
    while len(dk) < dklen:
        u = _prf(salt + struct.pack('>I', block))
        t = int(binascii.hexlify(u), 16)
        for _ in range(iterations - 1):
            u = _prf(u)
            t ^= int(binascii.hexlify(u), 16)
        dk += binascii.unhexlify('%064x' % t)
        block += 1
    return dk[:dklen]

def _pbkdf2_sha256(password, salt, iterations, dklen=32):
    """Use the fast C implementation when present, else the pure-Python one."""
    if hasattr(hashlib, 'pbkdf2_hmac'):
        return hashlib.pbkdf2_hmac('sha256', password, salt, iterations, dklen)
    return _pbkdf2_pure(password, salt, iterations, dklen)

# Fewer rounds when we're stuck on the (much slower) pure-Python path so logins
# and user creation stay responsive on weak NAS CPUs. The count is stored in each
# hash, so verification always uses the right number.
_DEFAULT_PBKDF2_ITERS = 120000 if hasattr(hashlib, 'pbkdf2_hmac') else 15000

def _hash_password(password, iterations=None, salt=None):
    """Return a PBKDF2 hash string: pbkdf2$<iters>$<salt_hex>$<hash_hex>."""
    if iterations is None:
        iterations = _DEFAULT_PBKDF2_ITERS
    if salt is None:
        salt = os.urandom(16)
    dk = _pbkdf2_sha256(password.encode('utf-8'), salt, iterations)
    return 'pbkdf2$%d$%s$%s' % (iterations, salt.encode('hex'), dk.encode('hex'))

def _verify_password(tech, supplied):
    """Check a supplied password against a technician record, constant-time.
    Prefers a PBKDF2 hash (field `password_hash`); falls back to legacy
    plaintext (`password`) so existing credentials keep working until migrated."""
    supplied = supplied or ''
    ph = tech.get('password_hash', '')
    if ph and ph.startswith('pbkdf2$'):
        try:
            _, iters, salt_hex, hash_hex = ph.split('$', 3)
            dk = _pbkdf2_sha256(supplied.encode('utf-8'),
                                salt_hex.decode('hex'), int(iters))
            return _consteq(dk.encode('hex'), hash_hex)
        except Exception:
            return False
    return _consteq(tech.get('password', ''), supplied)

def _check_lockout(username, ip):
    fail = _login_fails.get(_fail_key(username, ip), {})
    return fail.get('until', 0) > time.time()

def _record_fail(username, ip):
    with _fails_lock:
        k = _fail_key(username, ip)
        fail = _login_fails.get(k, {'count': 0, 'until': 0})
        fail['count'] = fail.get('count', 0) + 1
        fail['username'] = (username or '').lower()
        fail['ip'] = ip
        if fail['count'] >= MAX_FAILS:
            fail['until'] = time.time() + LOCKOUT_TIME
            _log('LOCKOUT: %s from %s after %d fails' % (username, ip, fail['count']), AUDIT_LOG)
        _login_fails[k] = fail
        _save_json(LOGIN_FAILS_FILE, _login_fails)

def _clear_fails(username, ip=None):
    with _fails_lock:
        uname = (username or '').lower()
        removed = False
        for k in list(_login_fails.keys()):
            ku = _login_fails[k].get('username', k.split('|')[0])
            if ku == uname and (ip is None or _login_fails[k].get('ip') == ip):
                del _login_fails[k]; removed = True
        if removed:
            _save_json(LOGIN_FAILS_FILE, _login_fails)

def _migrate_plaintext_passwords():
    """On boot: hash any legacy plaintext passwords and strip the plaintext, so
    a leaked credentials.json (or any backup of it) can't expose real passwords.
    After this runs the plaintext branch in _verify_password is dead code."""
    creds = _load_json(CREDS_FILE, {})
    techs = creds.get('technicians', [])
    todo  = [t for t in techs if t.get('password') and not t.get('password_hash')]
    strip = [t for t in techs if t.get('password_hash') and 'password' in t]
    if not todo and not strip:
        return 0
    _backup_file(CREDS_FILE)
    for t in todo:
        t['password_hash'] = _hash_password(t.get('password') or '')
        t.pop('password', None)
    for t in strip:
        t.pop('password', None)
    _save_json(CREDS_FILE, creds)
    _log('Startup: migrated %d plaintext password(s), stripped %d residual'
         % (len(todo), len(strip)), AUDIT_LOG)
    return len(todo)

def _upgrade_password_if_needed(username, supplied):
    """On a successful login, transparently bring the stored hash up to date:
    upgrade a legacy plaintext password to PBKDF2, and re-hash a PBKDF2 hash that
    uses MORE iterations than the current default down to the default (so the slow
    pure-Python verify on this hardware speeds up for everyone over time). We have
    the plaintext here, so this is the natural place to do it."""
    creds = _load_json(CREDS_FILE, {})
    changed = False
    for t in creds.get('technicians', []):
        if t.get('username', '').lower() == (username or '').lower():
            ph = t.get('password_hash')
            if not ph:
                t['password_hash'] = _hash_password(supplied)
                t.pop('password', None); changed = True
            else:
                if 'password' in t:
                    t.pop('password', None); changed = True
                try:
                    parts = ph.split('$')
                    if len(parts) == 4 and parts[0] == 'pbkdf2' \
                       and int(parts[1]) > _DEFAULT_PBKDF2_ITERS:
                        t['password_hash'] = _hash_password(supplied)
                        changed = True
                except Exception:
                    pass
            break
    if changed:
        _save_json(CREDS_FILE, creds)
        _log('Upgraded password hash for %s' % username, AUDIT_LOG)

# -- Users, roles & permissions ------------------------------------------------
# Roles are data, not hardcoded: admins/super-admins can create custom roles and
# choose each role's permission set. A role has a numeric `rank` that defines the
# management hierarchy -- you can only manage users and roles ranked BELOW your
# own, and you can only grant permissions you yourself hold. The built-in
# super_admin role is `root` (every permission, top rank, can manage anyone).
#
# The catalog below is the full set of capabilities on the site.
PERMISSION_CATALOG = [
    {'key': 'tickets',         'label': 'Tickets view',
     'desc': 'Open the Tickets spreadsheet and search ticket history.'},
    {'key': 'create_tickets',  'label': 'Create & edit tickets',
     'desc': 'Create new support tickets and edit existing ones from the tool.'},
    {'key': 'kit_editor',      'label': 'Kit editor',
     'desc': 'Create and edit kits, DIP/wiring images, and troubleshooting trees.'},
    {'key': 'analytics',       'label': 'Analytics dashboard',
     'desc': 'View ticket trends, kit trouble scores, and tech performance.'},
    {'key': 'dashboard',       'label': 'Admin dashboard',
     'desc': 'Open the admin dashboard and its at-a-glance ticket stats.'},
    {'key': 'manage_users',    'label': 'Manage users',
     'desc': 'Create, edit, deactivate, and delete user accounts.'},
    {'key': 'manage_roles',    'label': 'Manage roles',
     'desc': 'Create roles and decide which permissions each role grants.'},
    {'key': 'system_settings', 'label': 'System settings',
     'desc': 'Server status, backups, logs, and system configuration.'},
]
PERMISSION_KEYS = set(p['key'] for p in PERMISSION_CATALOG)

# Which permission each non-csv GET-proxy action needs. Anything not listed
# defaults to 'tickets' (the proxy fundamentally reads ticket/sheet data), so a
# permission-less but logged-in account can no longer pull data through it.
PROXY_GET_ACTION_PERMS = {'recent': 'tickets', 'stats': 'dashboard', 'search': 'tickets'}

ROOT_RANK = 100
ROLE_KEY_RE = r'^[a-z][a-z0-9_]{1,31}$'

def _builtin_roles():
    """Seed definitions for the three system roles. `root` => every permission."""
    return [
        {'key': 'super_admin', 'label': 'Super Admin', 'rank': ROOT_RANK,
         'permissions': list(PERMISSION_KEYS), 'system': True, 'root': True},
        {'key': 'admin', 'label': 'Admin', 'rank': 50, 'system': True,
         'permissions': ['tickets', 'create_tickets', 'kit_editor', 'analytics',
                         'dashboard', 'manage_users', 'manage_roles']},
        {'key': 'tech', 'label': 'Tech', 'rank': 10, 'system': True,
         'permissions': ['tickets', 'create_tickets', 'dashboard']},
    ]

def _load_roles():
    """Roles list, seeding/repairing the three system roles as needed."""
    roles = _load_json(ROLES_FILE, None)
    if not isinstance(roles, list) or not roles:
        roles = _builtin_roles()
        _save_json(ROLES_FILE, roles)
        return roles
    have = set(r.get('key') for r in roles)
    changed = False
    for b in _builtin_roles():
        if b['key'] not in have:
            roles.append(b); changed = True
    # super_admin must always be root, top rank, all permissions.
    for r in roles:
        if r.get('key') == 'super_admin':
            if not r.get('root') or r.get('rank') != ROOT_RANK \
               or set(r.get('permissions') or []) != set(PERMISSION_KEYS):
                r['root'] = True; r['system'] = True
                r['rank'] = ROOT_RANK; r['permissions'] = list(PERMISSION_KEYS)
                changed = True
    # Everyone gets the dashboard by default: guarantee the built-in roles include
    # it even if roles.json was seeded before this default existed.
    for r in roles:
        if r.get('system') and not r.get('root') \
           and 'dashboard' not in (r.get('permissions') or []):
            r.setdefault('permissions', []).append('dashboard')
            changed = True
    if changed:
        _save_json(ROLES_FILE, roles)
    return roles

def _save_roles(roles):
    _save_json(ROLES_FILE, roles)

def _role_map():
    return dict((r.get('key'), r) for r in _load_roles())

def _role(key):
    return _role_map().get(key)

def _role_rank(key):
    r = _role(key)
    return r.get('rank', 0) if r else 0

def _is_root_role(key):
    r = _role(key)
    return bool(r and r.get('root'))

def _role_perms(key):
    r = _role(key)
    if not r:
        return set()
    if r.get('root'):
        return set(PERMISSION_KEYS)
    return set(p for p in (r.get('permissions') or []) if p in PERMISSION_KEYS)

def _clean_perms(lst):
    """Keep only known permission keys, de-duplicated, in catalog order."""
    if not isinstance(lst, list):
        return []
    given = set(lst)
    return [p['key'] for p in PERMISSION_CATALOG if p['key'] in given]

def _effective_perms(user):
    """Permissions a user actually has: their role's set, plus any per-user
    extra grants (additive). A root role grants everything."""
    if not user:
        return set()
    perms = set(_role_perms(user.get('role', '')))
    perms |= set(p for p in (user.get('permissions') or []) if p in PERMISSION_KEYS)
    return perms

def _has_perm(user, perm):
    return bool(user) and perm in _effective_perms(user)

def _user_record(username):
    creds = _load_json(CREDS_FILE, {})
    for t in creds.get('technicians', []):
        if t.get('username', '').lower() == (username or '').lower():
            return t
    return None

def _session_user(session):
    """Fresh credentials record for a live session, or None if the account is
    gone or deactivated. Reading fresh means role/permission/deactivation changes
    take effect on the next request without forcing a re-login."""
    if not session:
        return None
    if session.get('_supabase'):
        return session.get('_supabase_user')
    u = _user_record(session.get('username', ''))
    if not u or u.get('active', True) == False:
        return None
    return u

def _actor_rank(actor):
    return _role_rank((actor or {}).get('role', ''))

def _count_active_root(techs):
    """How many active users hold a root role (used to prevent lockout)."""
    return sum(1 for t in techs
               if _is_root_role(t.get('role')) and t.get('active', True) != False)

def _can_manage(actor, target_role):
    """Can `actor` manage a user/role at `target_role`? Requires manage_users and
    a strictly higher rank (root can manage anyone, including peers)."""
    if not _has_perm(actor, 'manage_users'):
        return False
    if _is_root_role((actor or {}).get('role')):
        return True
    return _role_rank(target_role) < _actor_rank(actor)

def _assignable_roles(actor):
    """Role keys this actor may assign to a user: below their rank, and whose
    permission set they fully hold (no privilege escalation). Root: any role."""
    if not _has_perm(actor, 'manage_users'):
        return []
    if _is_root_role((actor or {}).get('role')):
        return [r['key'] for r in _load_roles()]
    arank = _actor_rank(actor)
    aperms = _effective_perms(actor)
    out = []
    for r in _load_roles():
        if r.get('root'):
            continue
        if r.get('rank', 0) < arank and set(_role_perms(r['key'])) <= aperms:
            out.append(r['key'])
    return out

def _can_edit_role(actor, role):
    """Can `actor` modify/delete this role definition? Requires manage_roles,
    can't touch root, and (unless root actor) must outrank the role."""
    if not role or not _has_perm(actor, 'manage_roles'):
        return False
    if role.get('root'):
        return False
    if _is_root_role((actor or {}).get('role')):
        return True
    return role.get('rank', 0) < _actor_rank(actor)

def _rename_user_sessions(old, new):
    """Migrate any live sessions when a user renames themselves (avoids lockout)."""
    with _lock:
        sessions = _load_sessions()
        changed = False
        for tok, s in sessions.items():
            if s.get('username', '').lower() == (old or '').lower():
                s['username'] = new
                changed = True
        if changed:
            _save_json(SESSIONS_FILE, sessions)


# -- Config / proxy ------------------------------------------------------------
def _get_config():
    return _load_json(CONFIG_FILE, {})

# -- Live, super-admin-tunable settings ---------------------------------------
# These read config.json on EVERY call, so the System Settings page can change
# them at runtime with no server restart. The module constants above are the
# defaults used when a key is missing or invalid. Every value is clamped to a
# safe range so a bad edit can't wedge the server (e.g. a 0-second poll loop).
def _cfg_num(key, default, lo, hi):
    try:
        v = float(_get_config().get(key, default))
    except Exception:
        return default
    if v != v:                      # NaN guard
        return default
    return max(lo, min(hi, v))

def _cfg_bool(key, default):
    v = _get_config().get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')

def _poll_interval():      return int(_cfg_num('poll_interval',      POLL_INTERVAL,         5,   3600))
def _pane_refresh():       return int(_cfg_num('pane_refresh_interval', 30,                  2,   3600))
def _reconcile_interval(): return int(_cfg_num('reconcile_interval', RECONCILE_INTERVAL,    30,  86400))
def _reconcile_days():     return int(_cfg_num('reconcile_days',     RECONCILE_DAYS,        1,   30))
def _session_ttl():        return int(_cfg_num('session_ttl_days',   SESSION_TTL / 86400.0, 0.5, 90) * 86400)

def _parse_hhmm(val, default_secs):
    try:
        hh, mm = str(val).split(':')
        hh = int(hh); mm = int(mm)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh * 3600 + mm * 60
    except Exception:
        pass
    return default_secs

def _work_window_secs():
    cfg = _get_config()
    return (_parse_hhmm(cfg.get('work_start'), WORK_START),
            _parse_hhmm(cfg.get('work_end'),   WORK_END))

def _within_work_window():
    """True when the poller is allowed to run right now. With work_window_enabled
    off (the default) this is always True, so behaviour is unchanged. With it on,
    the poller pauses outside the configured hours to spare Apps Script quota."""
    if not _cfg_bool('work_window_enabled', False):
        return True
    ws, we = _work_window_secs()
    now  = datetime.datetime.now()
    secs = now.hour * 3600 + now.minute * 60 + now.second
    if ws <= we:
        return ws <= secs < we
    return secs >= ws or secs < we   # window wraps past midnight

def _proxy_get(url):
    """Make a GET request server-side, adding the secret key if it isn't already
    on the URL. The key is never written to the logs."""
    try:
        config = _get_config()
        secret = config.get('secret_key', '')
        if secret and 'key=' not in url:
            sep = '&' if '?' in url else '?'
            full_url = url + sep + 'key=' + secret
        else:
            full_url = url
        _log('PROXY_GET: ' + _redact(full_url)[:160])
        req = urllib2.Request(full_url, headers={'User-Agent': 'Mozilla/5.0 (compatible; ZZ2Server/1.0)'})
        resp = urllib2.urlopen(req, timeout=30)
        data = resp.read()
        _log('PROXY_GET OK: %d bytes, status %d' % (len(data), resp.getcode()))
        return data, resp.getcode()
    except urllib2.HTTPError as e:
        body = ''
        try: body = e.read()[:200]
        except: pass
        _log('PROXY_GET HTTP ERROR: %d - %s' % (e.code, body))
        return json.dumps({'error': 'HTTP %d' % e.code}), e.code
    except Exception as e:
        _log('PROXY_GET EXCEPTION: ' + str(e))
        return json.dumps({'error': str(e)}), 500

CSV_CACHE = {}  # {url: (data, timestamp)}
CSV_CACHE_TTL = 300  # 5 minutes

def _fetch_csv(url):
    """Fetch a CSV from Google Sheets, using local cache to avoid slow HTTPS calls."""
    global CSV_CACHE
    now = time.time()
    cached = CSV_CACHE.get(url)
    if cached and (now - cached[1]) < CSV_CACHE_TTL:
        return cached[0], 200

    # Try to load from disk cache first
    import hashlib
    cache_key = hashlib.md5(url.encode()).hexdigest()[:8]
    cache_file = os.path.join(CACHE_DIR, 'csv_' + cache_key + '.cache')
    cache_meta = cache_file + '.ts'

    if os.path.exists(cache_file) and os.path.exists(cache_meta):
        try:
            with open(cache_meta) as f:
                ts = float(f.read().strip())
            if now - ts < CSV_CACHE_TTL:
                with open(cache_file, 'rb') as f:
                    data = f.read()
                CSV_CACHE[url] = (data, ts)
                return data, 200
        except:
            pass

    # Fetch fresh from Google
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; ZZ2Server/1.0)'}
    for attempt in range(3):
        try:
            req = urllib2.Request(url, headers=headers)
            resp = urllib2.urlopen(req, timeout=30)
            data = resp.read()
            # Save to disk cache
            try:
                with open(cache_file, 'wb') as f: f.write(data)
                with open(cache_meta, 'w') as f: f.write(str(now))
            except: pass
            CSV_CACHE[url] = (data, now)
            _log('CSV fetched and cached: ' + url[:60])
            return data, 200
        except Exception as e:
            _log('CSV fetch attempt %d failed: %s' % (attempt+1, str(e)))
            if attempt < 2:
                time.sleep(2)

    # Fall back to disk cache even if stale
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'rb') as f:
                data = f.read()
            _log('CSV fetch failed, using stale cache')
            return data, 200
        except: pass

    return '', 500

# -- Customer history (separate Supabase project, read-only) ------------------
# customer_performance_semesters/months live in an unrelated company Supabase
# project. RLS blocks the anon key on those tables, so this MUST go through
# the server with the secret key -- never expose that key to the browser.
_CUSTOMER_HISTORY_CACHE = {}   # {norm_client_key: (response_dict, timestamp)}
_CUSTOMER_HISTORY_TTL = 600    # 10 minutes
_CUSTOMER_SUMMARIES_CACHE = [None, 0]  # [response_dict, timestamp]
_CUSTOMER_SUMMARIES_TTL = 600          # 10 minutes
_ITEM_SALES_CACHE = {}                  # {normalized_item: (response_dict, timestamp)}
_ITEM_SALES_TTL = 600                   # 10 minutes
_RETURNS_RATE_ALERT_THRESHOLD = 0.15

def _supabase_get(path_and_query):
    config = _get_config()
    base = (config.get('supabase_url') or '').rstrip('/')
    key  = config.get('supabase_service_key') or ''
    if not base or not key:
        return None, 'Supabase not configured'
    # No "Mozilla/..."-style UA here: Supabase's secret-key gateway refuses the
    # key outright ("Forbidden use of secret API key in browser") for any
    # request whose User-Agent looks browser-like, as an extra safety net
    # against a leaked secret key being used client-side. This call is
    # server-side only, so a plain, clearly-non-browser UA is correct, not
    # just a workaround.
    req = urllib2.Request(base + '/rest/v1/' + path_and_query, headers={
        'apikey': key, 'Authorization': 'Bearer ' + key,
        'Accept': 'application/json',
        'Accept-Profile': config.get('customer_history_schema') or 'zoho',
        'User-Agent': 'ZZ2Server/1.0'})
    try:
        resp = urllib2.urlopen(req, timeout=15)
        return json.loads(resp.read()), None
    except urllib2.HTTPError as e:
        body = ''
        try: body = e.read()[:200]
        except Exception: pass
        _log('SUPABASE HTTP ERROR %d - %s' % (e.code, body))
        message = ''
        try:
            parsed = json.loads(body)
            message = _u(parsed.get('message', '')).strip()
        except Exception:
            pass
        return None, ('HTTP %d: %s' % (e.code, message)) if message else ('HTTP %d' % e.code)
    except Exception as e:
        _log('SUPABASE EXCEPTION: ' + str(e))
        return None, str(e)

def _supabase_rest(method, resource, payload=None, prefer=''):
    """Server-only REST request against the techSupport schema."""
    cfg = _get_config()
    base = (cfg.get('supabase_url') or '').rstrip('/')
    key = cfg.get('supabase_service_key') or ''
    if not base or not key:
        return False, 'Supabase not configured'
    headers = {
        'apikey': key, 'Authorization': 'Bearer ' + key,
        'Accept': 'application/json', 'Accept-Profile': 'techSupport',
        'Content-Profile': 'techSupport', 'User-Agent': 'ZZ2Server/1.0',
    }
    if prefer:
        headers['Prefer'] = prefer
    body = None
    if payload is not None:
        body = json.dumps(payload)
        headers['Content-Type'] = 'application/json'
    req = urllib2.Request(base + '/rest/v1/' + resource, data=body, headers=headers)
    req.get_method = lambda: method.upper()
    try:
        resp = urllib2.urlopen(req, timeout=30)
        raw = resp.read()
        return True, (json.loads(raw) if raw else None)
    except urllib2.HTTPError as e:
        raw = ''
        try: raw = e.read()[:500]
        except Exception: pass
        return False, 'HTTP %d: %s' % (e.code, raw)
    except Exception as e:
        return False, str(e)

_aux_sync_lock = threading.Lock()
_aux_sync_timer = [None]

def _auxiliary_snapshot():
    generation = int(time.time() * 1000)
    synced_at = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    kits = _load_json(KITS_JSON, {}) or {}
    trees = _load_json(TS_JSON, {}) or {}
    blueprints = _load_json(BLUEPRINTS_JSON, {}) or {}
    kit_rows, tree_rows, blueprint_rows, manual_rows = [], [], [], []
    for key, value in kits.items():
        value = value if isinstance(value, dict) else {}
        kit_rows.append({
            'kit_key': key, 'vehicle': value.get('vehicle', ''),
            'switch_button': value.get('switch_button', ''),
            'audio_method': value.get('audio_method', ''), 'notes': value.get('notes', ''),
            'dip_image': value.get('dip_image', ''), 'wiring_image': value.get('wiring_image', ''),
            'misc_image': value.get('misc_image', ''), 'manual': value.get('manual', ''),
            'local_updated_at': value.get('updated_at'),
            'sync_generation': generation, 'synced_at': synced_at,
        })
        tree_rows.append({'kit_key': key, 'tree': trees.get(key, []) or [],
                          'sync_generation': generation, 'synced_at': synced_at})
        manual_dir = os.path.join(MANUALS_DIR, key)
        if os.path.isdir(manual_dir):
            for filename in sorted(os.listdir(manual_dir)):
                full = os.path.join(manual_dir, filename)
                if not os.path.isfile(full):
                    continue
                try:
                    digest = hashlib.sha256()
                    with open(full, 'rb') as f:
                        while True:
                            chunk = f.read(1024 * 1024)
                            if not chunk: break
                            digest.update(chunk)
                    size = os.path.getsize(full)
                except Exception:
                    digest = None; size = 0
                manual_rows.append({
                    'kit_key': key, 'filename': filename,
                    'local_path': 'manuals/' + key + '/' + filename,
                    'size_bytes': size, 'sha256': digest.hexdigest() if digest else '',
                    'sync_generation': generation, 'synced_at': synced_at,
                })
    for bid, value in blueprints.items():
        value = value if isinstance(value, dict) else {}
        blueprint_rows.append({
            'id': bid, 'name': value.get('name', ''),
            'troubleshooting': value.get('troubleshooting', []) or [],
            'created_at': value.get('created_at'), 'updated_at': value.get('updated_at'),
            'sync_generation': generation, 'synced_at': synced_at,
        })
    return generation, kit_rows, tree_rows, blueprint_rows, manual_rows

def _sync_auxiliary_to_supabase():
    """Mirror one coherent local snapshot, pruning rows from older snapshots."""
    with _aux_sync_lock:
        generation, kits, trees, blueprints, manuals = _auxiliary_snapshot()
        batches = [
            ('kits?on_conflict=kit_key', kits),
            ('troubleshooting?on_conflict=kit_key', trees),
            ('blueprints?on_conflict=id', blueprints),
            ('manual_assets?on_conflict=kit_key,filename', manuals),
        ]
        for resource, rows in batches:
            if not rows:
                continue
            ok, info = _supabase_rest('POST', resource, rows,
                                      'resolution=merge-duplicates,return=minimal')
            if not ok:
                _log('Auxiliary Supabase sync failed (%s): %s' % (resource.split('?')[0], info))
                return False, info
        # Only prune after every upsert succeeded, so a transient failure can
        # never empty the remote backup.
        gen = str(generation)
        for table in ('manual_assets', 'troubleshooting', 'blueprints', 'kits'):
            ok, info = _supabase_rest('DELETE', table + '?sync_generation=neq.' + gen,
                                      None, 'return=minimal')
            if not ok:
                _log('Auxiliary Supabase prune failed (%s): %s' % (table, info))
                return False, info
        _log('Auxiliary Supabase sync complete: %d kits, %d trees, %d blueprints, %d manuals' %
             (len(kits), len(trees), len(blueprints), len(manuals)))
        return True, {'kits': len(kits), 'troubleshooting': len(trees),
                      'blueprints': len(blueprints), 'manuals': len(manuals)}

def _run_scheduled_auxiliary_sync():
    with _aux_sync_lock:
        _aux_sync_timer[0] = None
    _sync_auxiliary_to_supabase()

def _schedule_auxiliary_sync(delay=2.0):
    """Debounce several related JSON saves into one coherent mirror upload."""
    with _aux_sync_lock:
        old = _aux_sync_timer[0]
        if old is not None:
            try: old.cancel()
            except Exception: pass
        timer = threading.Timer(delay, _run_scheduled_auxiliary_sync)
        timer.daemon = True
        _aux_sync_timer[0] = timer
        timer.start()

def _client_match_score(a, b):
    """How well a ticket's free-text client name matches a customer_name from
    Supabase. Deliberately skips _num_sig/_model_sig (product-model-number
    signatures) -- meaningless for company/shop names."""
    if not a or not b:
        return 0.0
    if _norm_key(a) == _norm_key(b):
        return 1.0
    ta, tb = _token_set_key(a), _token_set_key(b)
    if ta == tb:
        return 0.97
    ml = max(len(ta), len(tb))
    if ml == 0:
        return 0.0
    return max(0.0, 1 - float(_lev(ta, tb)) / ml)

def _best_customer_match(client_name, candidates):
    best, best_score = None, 0.0
    for c in candidates:
        sc = _client_match_score(client_name, c)
        if sc > best_score:
            best, best_score = c, sc
    if best_score < 0.72:   # tunable; the score is always echoed in the response, never silent
        return None, best_score
    return best, best_score

def _customer_name_candidates(client_name):
    """Narrow the candidate set with an ilike on the longest word of the
    client name, rather than pulling the whole table for every lookup."""
    words = sorted([w for w in _norm_key(client_name).split(' ') if w], key=len, reverse=True)
    if not words:
        return [], 'Empty client name'
    q = 'customer_name=ilike.*' + urllib.quote(words[0]) + '*&select=customer_name&limit=100'
    data, err = _supabase_get('customer_performance_semesters?' + q)
    if err:
        return [], err
    names = sorted(set(r.get('customer_name', '') for r in (data or []) if r.get('customer_name')))
    return names, None

def _customer_summaries():
    """Return every customer with lifetime sales/RMA totals aggregated from
    semester rows. PostgREST installations commonly cap a response at 1,000
    rows, so page explicitly instead of silently omitting older customers."""
    now = time.time()
    cached, cached_at = _CUSTOMER_SUMMARIES_CACHE
    if cached is not None and (now - cached_at) < _CUSTOMER_SUMMARIES_TTL:
        return cached

    rows = []
    page_size = 1000
    offset = 0
    for _page in range(50):
        query = ('customer_performance_semesters?select=customer_name,sales_quantity,'
                 'returns_quantity&order=customer_name.asc&limit=%d&offset=%d' %
                 (page_size, offset))
        page, err = _supabase_get(query)
        if err:
            return {'ok': False, 'error': err, 'customers': []}
        page = page or []
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    grouped = {}
    for row in rows:
        name = _u(row.get('customer_name', '')).strip()
        if not name:
            continue
        key = _norm_key(name)
        if key not in grouped:
            grouped[key] = {'name': name, 'products_purchased': 0.0, 'rmas': 0.0}
        try:
            grouped[key]['products_purchased'] += float(row.get('sales_quantity') or 0)
        except (TypeError, ValueError):
            pass
        try:
            grouped[key]['rmas'] += float(row.get('returns_quantity') or 0)
        except (TypeError, ValueError):
            pass

    customers = sorted(grouped.values(), key=lambda item: _norm_key(item['name']))
    for item in customers:
        for field in ('products_purchased', 'rmas'):
            value = item[field]
            if value == int(value):
                item[field] = int(value)
    result = {'ok': True, 'customers': customers}
    _CUSTOMER_SUMMARIES_CACHE[0] = result
    _CUSTOMER_SUMMARIES_CACHE[1] = now
    return result

def _returns_alert(semesters, months):
    row = (months[0] if months else (semesters[0] if semesters else None))
    if not row:
        return False
    try:
        rate = float(row.get('returns_rate') or 0)
    except Exception:
        rate = 0.0
    if rate >= _RETURNS_RATE_ALERT_THRESHOLD:
        return True
    recent = (months[:3] if months else semesters[:3])
    return sum(1 for r in recent if float(r.get('returns_quantity') or 0) > 0) >= 2

def _build_customer_history(client):
    try:
        candidates, err = _customer_name_candidates(client)
        if err:
            return {'ok': False, 'error': err, 'matched_customer': None,
                     'match_score': 0, 'alert': False, 'semesters': [], 'months': []}
        matched, score = _best_customer_match(client, candidates)
        if not matched:
            return {'ok': True, 'matched_customer': None, 'match_score': round(score, 2),
                     'alert': False, 'semesters': [], 'months': [],
                     'message': 'No matching customer found'}
        enc = urllib.quote(matched)
        sem, _sem_err = _supabase_get(
            'customer_performance_semesters?customer_name=eq.' + enc +
            '&select=period_year,semester,period_start,period_end,sales_quantity,'
            'returns_quantity,returns_rate,bcy_total&order=period_year.desc,semester.desc&limit=6')
        mon, _mon_err = _supabase_get(
            'customer_performance_months?customer_name=eq.' + enc +
            '&select=period_year,month,sales_quantity,returns_quantity,returns_rate,bcy_total'
            '&order=period_year.desc,month.desc&limit=12')
        semesters, months = sem or [], mon or []
        return {'ok': True, 'matched_customer': matched, 'match_score': round(score, 2),
                 'alert': _returns_alert(semesters, months), 'semesters': semesters, 'months': months}
    except Exception as e:
        _log('CUSTOMER HISTORY EXCEPTION: ' + str(e))
        return {'ok': False, 'error': str(e), 'matched_customer': None,
                 'match_score': 0, 'alert': False, 'semesters': [], 'months': []}

def _build_item_sales_summary(product):
    """Lifetime quantity sold for an item/kit from zoho.sales_by_item."""
    try:
        enc = urllib.quote(_u(product).encode('utf-8'))
        rows = []
        page_size = 1000
        for page in range(50):
            query = ('sales_by_item?or=(item_sku.ilike.%s,item_name.ilike.%s)'
                     '&select=item_sku,item_name,quantity_sold,period_start,period_end,semester,source'
                     '&order=period_start.desc&limit=%d&offset=%d' %
                     (enc, enc, page_size, page * page_size))
            batch, err = _supabase_get(query)
            if err:
                return {'ok': False, 'error': err, 'product': product,
                        'quantity_sold': None, 'period_rows': 0}
            batch = batch or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
        quantity = 0.0
        for row in rows:
            try:
                quantity += float(row.get('quantity_sold') or 0)
            except (TypeError, ValueError):
                pass
        if quantity == int(quantity):
            quantity = int(quantity)
        return {'ok': True, 'product': product, 'quantity_sold': quantity,
                'period_rows': len(rows), 'scope': 'all_time'}
    except Exception as e:
        _log('ITEM SALES SUMMARY EXCEPTION: ' + str(e))
        return {'ok': False, 'error': str(e), 'product': product,
                'quantity_sold': None, 'period_rows': 0}

# -- Analytics store (local ticket database) ----------------------------------
# A local mirror of every ticket in techSupport.tickets. Supabase is the same
# authoritative source used by tickets.html; the mirror only exists so Analytics
# can serve its expensive, precomputed view without fetching thousands of rows
# on every page load.
def _analytics_store():
    return _load_json(ANALYTICS_FILE, {'generated': '', 'count': 0, 'tickets': []})

def _ticket_record(data, ticket_num):
    """Build a store record from the params of a tool-created/updated ticket."""
    return {
        'date':       datetime.date.today().isoformat(),
        'ticket':     ticket_num or '',
        'tech':       data.get('tech', ''),
        'client':     data.get('client', ''),
        'product':    data.get('product', ''),
        'vehicle':    data.get('vehicle', ''),
        'issue':      data.get('issue', ''),
        'resolution': data.get('resolution', ''),
        'status':     data.get('status', ''),
        'serial':     data.get('serial', ''),
        'notes':      data.get('notes', ''),
    }

def _format_ticket_number(ticket_num):
    """Return the canonical ticket number stored in Supabase (TKT-7388)."""
    raw = _u(ticket_num).strip()
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if not digits:
        return raw
    return 'TKT-' + digits

def _supabase_ticket_record(data, ticket_num):
    """Map the main ticket form to techSupport.tickets."""
    return {
        'ticket_date': datetime.date.today().isoformat(),
        'ticket_number': _format_ticket_number(ticket_num),
        'tech': data.get('tech') or None,
        'client': data.get('client') or None,
        'product': data.get('product') or None,
        'vehicle': data.get('vehicle') or None,
        'issue': data.get('issue') or None,
        'resolution': data.get('resolution') or None,
        'status': data.get('status') or None,
        'serial': data.get('serial') or None,
        'notes': data.get('notes') or None,
    }

def _supabase_ticket_filter(ticket_num):
    return 'tickets?ticket_number=eq.' + urllib.quote(_format_ticket_number(ticket_num))

def _supabase_create_ticket(data, ticket_num):
    """Create the ticket in Supabase before acknowledging it to the browser."""
    ok, result = _supabase_rest(
        'POST', 'tickets', _supabase_ticket_record(data, ticket_num),
        'return=representation')
    return ok, result

def _supabase_update_ticket(data, ticket_num):
    record = _supabase_ticket_record(data, ticket_num)
    record.pop('ticket_date', None)  # edits retain their original creation date
    record.pop('ticket_number', None)
    return _supabase_rest('PATCH', _supabase_ticket_filter(ticket_num), record,
                          'return=minimal')

def _supabase_delete_ticket(ticket_num):
    return _supabase_rest('DELETE', _supabase_ticket_filter(ticket_num), None,
                          'return=minimal')

def _supabase_ticket_store_record(row):
    """Map a techSupport.tickets row to the local analytics-store shape."""
    return {
        'date':       row.get('ticket_date', ''),
        'ticket':     row.get('ticket_number', ''),
        'tech':       row.get('tech', ''),
        'client':     row.get('client', ''),
        'product':    row.get('product', ''),
        'vehicle':    row.get('vehicle', ''),
        'issue':      row.get('issue', ''),
        'resolution': row.get('resolution', ''),
        'status':     row.get('status', ''),
        'serial':     row.get('serial', ''),
        'notes':      row.get('notes', ''),
    }

def _fetch_all_supabase_tickets():
    """Fetch the complete authoritative ticket table, bypassing PostgREST's
    per-request row cap with deterministic pagination. Returns (ok, rows/error)."""
    fields = ('ticket_date,ticket_number,tech,client,product,vehicle,issue,'
              'resolution,status,serial,notes')
    rows = []
    page_size = 1000
    for page in range(100):                 # safety cap: 100k tickets
        resource = ('tickets?select=%s&order=ticket_number.asc&limit=%d&offset=%d' %
                    (fields, page_size, page * page_size))
        ok, info = _supabase_rest('GET', resource)
        if not ok:
            return False, info
        batch = info if isinstance(info, list) else []
        rows.extend(_supabase_ticket_store_record(row) for row in batch)
        if len(batch) < page_size:
            return True, rows
    return False, 'Supabase ticket export exceeded the 100,000-row safety cap'

def _now_iso():
    return datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

# Server-side port of analytics.html's normalize() + consolidate() pipeline.
# Parity-tested byte-for-byte (canonical assignments) against the JS version.
import re, datetime, threading
try:
    from collections import OrderedDict
except ImportError:
    OrderedDict = dict
try:
    from functools import cmp_to_key
except ImportError:
    def cmp_to_key(mycmp):
        class K(object):
            __slots__ = ['o']
            def __init__(s, o, *a): s.o = o
            def __lt__(s, o): return mycmp(s.o, o.o) < 0
            def __eq__(s, o): return mycmp(s.o, o.o) == 0
        return K

_YY = datetime.datetime.now().year % 100

try:
    _AN_TEXT = unicode          # py2: keep unicode, never str()-encode (UnicodeEncodeError)
except NameError:
    _AN_TEXT = str              # py3

def _an_text(v):
    if v is None:
        return _AN_TEXT('')
    if isinstance(v, _AN_TEXT):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode('utf-8')
        except Exception:
            return v.decode('utf-8', 'replace')
    return _AN_TEXT(v)


def _pick(o, names):
    for n in names:
        v = o.get(n)
        if v is not None and v != '':
            return v
    return ''

_re_iso = re.compile(r'^(\d{4})-(\d{1,2})-(\d{1,2})')
_re_us  = re.compile(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})')
_re_named = re.compile(r'\b([A-Za-z]{3})[a-z]*\s+(\d{1,2})\s+(\d{4})\b')
_MONTHS = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
           'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}

def _parse_date_str(v):
    """Return 'YYYY-MM-DD' for the parsed calendar date, or '' if unparseable.
    Mirrors analytics.html parseDate (US M/D/Y; ET-equivalent date string)."""
    if v is None:
        return ''
    s = _an_text(v).strip()
    if not s:
        return ''
    m = _re_iso.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _re_us.match(s)
        if m:
            y = int(m.group(3));  y = y + 2000 if y < 100 else y
            mo, d = int(m.group(1)), int(m.group(2))
        else:
            # native Date() fallback: handle "Wed Jun 10 2026 ..." / "Jun 10 2026"
            mm = _re_named.search(s)
            if not mm:
                return ''
            mo = _MONTHS.get(mm.group(1).lower())
            if not mo:
                return ''
            y, d = int(mm.group(3)), int(mm.group(2))
    try:
        return '%04d-%02d-%02d' % (datetime.date(y, mo, d).year,
                                   datetime.date(y, mo, d).month,
                                   datetime.date(y, mo, d).day)
    except Exception:
        return ''

def _status_canon(raw):
    s = _an_text(raw or '').strip().lower()
    if not s: return 'open'
    if 'resolv' in s: return 'resolved'
    if 'wait' in s: return 'open'
    if 'no sol' in s or 'nosol' in s: return 'nosol'
    if 'rma' in s: return 'rma'
    if 'destroy' in s: return 'destroy'
    if 'distrib' in s: return 'distrib'
    if s == 'w.o.e.' or s == 'woe' or 'w.o.e' in s: return 'woe'
    return 'other'

# ---- label helpers ----
def _clean_label(s):
    return re.sub(r'\s+', ' ', s or '').strip()

def _expand_years(s):
    if not re.search(r'[a-z]', s, re.I):
        return s
    def rep(m):
        pre, d = m.group(1), m.group(2)
        n = int(d)
        return pre + str(2000 + n if n <= _YY + 1 else 1900 + n)
    return re.sub(r"(^|[\s(/-])'?(\d{2})(?=[\s)/-]|$)", rep, s)

def _norm_key(s):
    return _clean_label(s).lower().replace('.', '').replace(',', '')

def _token_set_key(s):
    return ' '.join(sorted([w for w in _norm_key(s).split(' ') if w]))

def _num_sig(s):
    return ','.join(sorted(re.findall(r'\d+', _an_text(s))))

def _model_sig(s):
    return ','.join(sorted([w for w in _norm_key(s).split(' ')
                            if w and re.match(r'^[a-z]+$', w) and len(w) <= 4]))

def _case_score(s):
    sc = 0
    for w in re.split(r'\s+', _an_text(s)):
        if not re.search(r'[a-z]', w, re.I): continue
        if re.match(r'[A-Z][a-z]', w): sc += 2
        elif re.match(r'^[A-Z]+$', w) and len(w) <= 4: sc += 1
        elif re.match(r'^[A-Z0-9]+$', w): sc -= 1
    return sc

def _lev(a, b):
    m, n = len(a), len(b)
    if not m: return n
    if not n: return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 1))
        prev = cur
    return prev[n]

def _merge_ok(a, b):
    if a == b: return True
    if _num_sig(a) != _num_sig(b): return False
    if _model_sig(a) != _model_sig(b): return False
    if _token_set_key(a) == _token_set_key(b): return True
    ml = max(len(a), len(b))
    if ml < 5: return False
    d = _lev(a, b)
    return d <= 2 and (1 - float(d) / ml) >= 0.85

def _consolidate(values, expand_years=False, fuzzy=False):
    items = []
    for v in values:
        e = _clean_label(v)
        if not e: continue
        if expand_years: e = _expand_years(e)
        items.append((v, e, _norm_key(e)))
    freq, disp = OrderedDict(), OrderedDict()
    for (orig, exp, key) in items:
        freq[key] = freq.get(key, 0) + 1
        dd = disp.get(key)
        if dd is None: dd = OrderedDict(); disp[key] = dd
        dd[exp] = dd.get(exp, 0) + 1
    key_display = {}
    for key, dd in disp.items():
        exps = list(dd.keys())
        exps.sort(key=cmp_to_key(lambda a, b: (dd[b] - dd[a]) or (_case_score(b) - _case_score(a))))
        key_display[key] = exps[0]
    keys = list(freq.keys())
    keys.sort(key=cmp_to_key(lambda a, b: freq[b] - freq[a]))

    def bk(k):
        letters = re.sub(r'[^a-z]', '', k)
        return _num_sig(k) + '|' + (letters[0] if letters else '')

    canon_of_key, canons, buckets = {}, [], OrderedDict()
    for k in keys:
        hit = None
        if fuzzy:
            for c in buckets.get(bk(k), []):
                if _merge_ok(k, c): hit = c; break
        if hit is not None:
            canon_of_key[k] = hit
        else:
            canon_of_key[k] = k; canons.append(k)
            buckets.setdefault(bk(k), []).append(k)
    cmap, merges, seen = {}, [], set()
    for (orig, exp, key) in items:
        label = key_display[canon_of_key[key]]
        cmap[orig] = label
        if _clean_label(orig) != label:
            sig = orig + '>>' + label
            if sig not in seen:
                seen.add(sig); merges.append([orig, label])
    return {'map': cmap, 'merges': merges, 'before': len(freq), 'after': len(canons)}

def _normalize(rows):
    out = []
    for r in rows:
        ds = _parse_date_str(_pick(r, ['date', 'Date', 'DATE']))
        t = {
            'dateStr': ds,
            'ticket': _an_text(_pick(r, ['ticket', 'ticket_no', 'Ticket #', 'number']) or ''),
            'tech': _an_text(_pick(r, ['tech', 'technician', 'Tech']) or '').strip(),
            'client': _an_text(_pick(r, ['client', 'customer', 'Client']) or '').strip(),
            'product': _an_text(_pick(r, ['product', 'kit', 'Product', 'Kit']) or '').strip(),
            'vehicle': _an_text(_pick(r, ['vehicle', 'Vehicle']) or '').strip(),
            'issue': _an_text(_pick(r, ['issue', 'Issue']) or '').strip(),
            'resolution': _an_text(_pick(r, ['resolution', 'Resolution']) or '').strip(),
            'status': _an_text(_pick(r, ['status', 'Status']) or '').strip(),
        }
        t['canon'] = _status_canon(t['status'])
        out.append(t)
    return out

def compute_analytics(tickets, generated=None):
    """Normalize + consolidate; return the precomputed analytics payload that
    analytics.html consumes directly (no client-side aggregation of raw data)."""
    rows = _normalize(tickets)
    rows = [t for t in rows if t['ticket'] or t['dateStr'] or t['client']]
    veh = _consolidate([t['vehicle'] for t in rows], expand_years=True,  fuzzy=True)
    cli = _consolidate([t['client']  for t in rows], expand_years=False, fuzzy=True)
    tec = _consolidate([t['tech']    for t in rows], expand_years=False, fuzzy=False)
    out_rows = []
    for t in rows:
        out_rows.append({
            'date':       t['dateStr'],
            'ticket':     t['ticket'],
            'tech':       tec['map'].get(t['tech'], t['tech']),
            'client':     cli['map'].get(t['client'], t['client']),
            'product':    t['product'],
            'vehicle':    veh['map'].get(t['vehicle'], t['vehicle']),
            'canon':      t['canon'],
            'status':     t['status'],
            'issue':      t['issue'],
            'resolution': t['resolution'],
        })
    def nsum(c):
        return {'before': c['before'], 'after': c['after'], 'merges': c['merges']}
    return {
        'precomputed': True,
        'generated': generated,
        'count': len(out_rows),
        'rows': out_rows,
        'norm': {'vehicle': nsum(veh), 'client': nsum(cli), 'tech': nsum(tec)},
    }


# ---- precomputed analytics VIEW (served to analytics.html instead of raw store) ----
ANALYTICS_VIEW_FILE = os.path.join(BASE_DIR, 'data', 'analytics_view.json')
_view_lock = threading.Lock()
_view_compute_lock = threading.Lock()  # serializes view builds: only ONE compute at a time
_view_serve_lock = threading.Lock()     # serializes the cheap serve-time delta merge
_view_serve_cache = {'key': None, 'json': None}

def _build_analytics_view(store=None, background=False):
    # Heavy (~fuzzy consolidate over thousands of values). Serialize so we never
    # run more than one at a time; background callers skip entirely if a build is
    # already in progress (prevents request pile-ups from pinning the CPU).
    if background:
        if not _view_compute_lock.acquire(False):
            return None
    else:
        _view_compute_lock.acquire()
    try:
        if store is None:
            store = _load_json(ANALYTICS_FILE, None)
        if not isinstance(store, dict):
            return None
        tickets = store.get('tickets') or []
        view = compute_analytics(tickets, store.get('generated'))
        view['source_generated'] = store.get('generated')
        view['source_count'] = store.get('count', len(tickets))
        with _view_lock:
            _save_json(ANALYTICS_VIEW_FILE, view)
        _log('Analytics view built: %d rows' % view.get('count', 0))
        return view
    except Exception as e:
        _log('Analytics view build failed: ' + str(e))
        return None
    finally:
        _view_compute_lock.release()

def _view_build_async():
    th = threading.Thread(target=lambda: _build_analytics_view(background=True),
                          name='analytics_view_build')
    th.daemon = True
    th.start()

def _analytics_view_fresh():
    view = _load_json(ANALYTICS_VIEW_FILE, None)
    if not isinstance(view, dict):
        return None
    store = _load_json(ANALYTICS_FILE, None)
    if isinstance(store, dict) and view.get('source_generated') != store.get('generated'):
        return None
    return view

def _ensure_analytics_view():
    try:
        v = _load_json(ANALYTICS_VIEW_FILE, None)
        if not (isinstance(v, dict) and isinstance(v.get('rows'), list)) and os.path.exists(ANALYTICS_FILE):
            _build_analytics_view(background=True)
    except Exception as e:
        _log('ensure analytics view: ' + str(e))

def _delta_canon_index(rows):
    """normKey -> canonical display, derived from the cached view's rows, so new
    tickets reuse the SAME canonical label as already-consolidated ones (handles
    case/punctuation). Only true typo-variants wait for the nightly rebuild."""
    veh, cli, tec = {}, {}, {}
    for r in rows:
        v = r.get('vehicle')
        if v:
            k = _norm_key(v)
            if k and k not in veh: veh[k] = v
        c = r.get('client')
        if c:
            k = _norm_key(c)
            if k and k not in cli: cli[k] = c
        t = r.get('tech')
        if t:
            k = _norm_key(t)
            if k and k not in tec: tec[k] = t
    return veh, cli, tec

def _delta_row(tk, veh, cli, tec):
    t = _normalize([tk])[0]
    if not (t['ticket'] or t['dateStr'] or t['client']):
        return None
    vraw = t['vehicle']
    vk = _norm_key(_expand_years(_clean_label(vraw))) if vraw else ''
    veh_disp = veh.get(vk) or _clean_label(_expand_years(vraw)) if vraw else ''
    ck = _norm_key(t['client'])
    cli_disp = cli.get(ck) or _clean_label(t['client'])
    tck = _norm_key(t['tech'])
    tec_disp = tec.get(tck) or _clean_label(t['tech'])
    return {'date': t['dateStr'], 'ticket': t['ticket'], 'tech': tec_disp,
            'client': cli_disp, 'product': t['product'], 'vehicle': veh_disp,
            'canon': t['canon'], 'status': t['status'],
            'issue': t['issue'], 'resolution': t['resolution']}

def _analytics_payload():
    """Return the served analytics JSON: the cached precomputed view with today's
    new/changed tickets folded in by ticket id. Cheap (no fuzzy consolidate);
    memoized on (store mtime, view mtime) so unchanged reloads are instant."""
    try: vmt = os.path.getmtime(ANALYTICS_VIEW_FILE)
    except OSError: return None
    try: smt = os.path.getmtime(ANALYTICS_FILE)
    except OSError: smt = None
    key = (smt, vmt)
    with _view_serve_lock:
        if _view_serve_cache['json'] is not None and _view_serve_cache['key'] == key:
            return _view_serve_cache['json']
        base = _load_json(ANALYTICS_VIEW_FILE, None)
        if not (isinstance(base, dict) and isinstance(base.get('rows'), list)):
            return None
        rows = base['rows']
        store = _load_json(ANALYTICS_FILE, None)
        out = base
        if isinstance(store, dict):
            idx = {}
            for i, r in enumerate(rows):
                tid = r.get('ticket')
                if tid: idx[tid] = i
            veh, cli, tec = _delta_canon_index(rows)
            merged = list(rows)
            appended = updated = 0
            for tk in (store.get('tickets') or []):
                tid = _an_text(_pick(tk, ['ticket', 'ticket_no', 'Ticket #', 'number']) or '')
                if tid and tid in idx:
                    canon = _status_canon(_pick(tk, ['status', 'Status']))
                    br = merged[idx[tid]]
                    if br.get('canon') != canon:
                        br = dict(br); br['canon'] = canon
                        br['status'] = _an_text(_pick(tk, ['status', 'Status']) or '').strip()
                        merged[idx[tid]] = br; updated += 1
                    continue
                dr = _delta_row(tk, veh, cli, tec)
                if dr is not None:
                    merged.append(dr); appended += 1
            if appended or updated:
                out = dict(base)
                out['rows'] = merged
                out['count'] = len(merged)
                out['generated'] = store.get('generated') or base.get('generated')
                out['live_appended'] = appended
                out['live_updated'] = updated
        js = json.dumps(out)
        _view_serve_cache['key'] = key
        _view_serve_cache['json'] = js
        return js

def _rebuild_analytics():
    """Rebuild Analytics from the same Supabase table used by tickets.html.
    Returns (ok, count_or_error_string)."""
    ok, tickets = _fetch_all_supabase_tickets()
    if not ok:
        return False, 'Supabase ticket export failed: %s' % tickets
    store = {'generated': _now_iso(), 'count': len(tickets),
             'source': 'supabase', 'tickets': tickets}
    with _lock:
        ok = _save_json(ANALYTICS_FILE, store)
    if not ok:
        return False, 'could not write store'
    _log('Analytics rebuilt from Supabase: %d tickets' % len(tickets))
    _build_analytics_view(store)
    return True, len(tickets)

def _autoresolve_call(days, apply, mx):
    """One Apps Script autoresolve call. Returns (ok, dict)."""
    cfg = _get_config()
    webapp = cfg.get('webapp_url', '')
    if not webapp:
        return False, {'error': 'webapp_url not configured'}
    sep = '&' if '?' in webapp else '?'
    url = webapp + sep + ('action=autoresolve&days=%d&apply=%s&max=%d'
                          % (int(days), 'true' if apply else 'false', int(mx)))
    body, code = _proxy_get(url)
    if code != 200:
        return False, {'error': 'autoresolve HTTP %d' % code}
    try:
        parsed = json.loads(body)
    except Exception as e:
        return False, {'error': 'bad JSON: %s' % e}
    if isinstance(parsed, dict) and parsed.get('error'):
        return False, parsed
    return True, (parsed if isinstance(parsed, dict) else {'ok': True})

def _auto_resolve(days=None, apply=False, time_budget=None):
    """Mark stale tickets Resolved in the sheet. Stale = status blank OR
    'waiting for customer feedback', AND dated older than `days` (the age guard
    applies to blank too, so new tickets are safe). apply=False is a dry run.
    apply=True writes in small chunks (only the changed cells) and is idempotent,
    so it's safe to re-run and it picks up where it left off. time_budget caps
    the wall-clock for one call (the browser uses a short budget and reports
    `remaining`; the nightly passes None to run to completion). Returns (ok, dict)."""
    cfg = _get_config()
    if days is None:
        try:
            days = int(cfg.get('auto_resolve_days', AUTO_RESOLVE_DAYS))
        except Exception:
            days = AUTO_RESOLVE_DAYS
    if not apply:
        return _autoresolve_call(days, False, 0)

    CHUNK = 50                               # small: each write stays well under any limit
    total_applied = 0
    candidates = None
    remaining = None
    start = time.time()
    for _ in range(500):                     # safety cap (500 * 50 = 25k)
        ok, data = _autoresolve_call(days, True, CHUNK)
        if not ok:
            # a chunk failed: stop cleanly. Idempotent, so a re-run resumes.
            out = {'ok': True, 'applied': total_applied, 'partial': True,
                   'remaining': remaining, 'done': False}
            if isinstance(data, dict):
                out['error'] = data.get('error')
            return (True, out) if total_applied else (False, data)
        if candidates is None:
            candidates = data.get('candidates', 0)
        a = (data.get('applied', 0) or 0)
        total_applied += a
        remaining = (data.get('remaining', 0) or 0)
        if a == 0 or remaining <= 0:
            return True, {'ok': True, 'applied': total_applied, 'remaining': 0,
                          'done': True, 'candidates': candidates}
        if time_budget is not None and (time.time() - start) >= time_budget:
            return True, {'ok': True, 'applied': total_applied, 'remaining': remaining,
                          'done': False, 'candidates': candidates}
        time.sleep(0.2)
    return True, {'ok': True, 'applied': total_applied,
                  'remaining': (remaining or 0), 'done': ((remaining or 0) <= 0),
                  'candidates': candidates}

def _append_ticket_to_store(record):
    """Append one tool-created ticket so the store stays current without a full
    re-export. Never raises (the sheet remains the source of truth)."""
    try:
        if not os.path.exists(ANALYTICS_FILE):
            return  # store not seeded yet; a rebuild/seed will include this ticket
        with _lock:
            store = _analytics_store()
            if not isinstance(store.get('tickets'), list):
                return  # not seeded yet; the next rebuild will pick it up
            store['tickets'].append(record)
            store['count'] = len(store['tickets'])
            store['updated'] = _now_iso()
            _save_json(ANALYTICS_FILE, store)
    except Exception as e:
        _log('Analytics store append failed: %s' % e)

def _update_ticket_in_store(ticket_num, record):
    """Update the matching ticket in the store after an edit through the tool."""
    try:
        tn = str(ticket_num or '').strip()
        if not tn:
            return
        if not os.path.exists(ANALYTICS_FILE):
            return  # store not seeded yet
        with _lock:
            store = _analytics_store()
            tickets = store.get('tickets')
            if not isinstance(tickets, list):
                return
            for t in tickets:
                if str(t.get('ticket', '')).strip() == tn:
                    t.update(record)
                    break
            store['updated'] = _now_iso()
            _save_json(ANALYTICS_FILE, store)
    except Exception as e:
        _log('Analytics store update failed: %s' % e)

def _delete_ticket_from_store(ticket_num):
    """Remove a ticket from the local store (used by Discard/delete). Matches on
    the NUMERIC part of the ticket number, so the delete still lands even when the
    stored value is formatted differently ("TKT-1234" vs "1234", int vs str, stray
    whitespace). A strict string match could miss, leaving the ticket in the store
    to reappear in the Today pane until the next periodic reconcile (~minutes)."""
    try:
        target = ''.join(ch for ch in str(ticket_num or '') if ch.isdigit())
        if not target or not os.path.exists(ANALYTICS_FILE):
            return
        with _lock:
            store = _analytics_store()
            tickets = store.get('tickets')
            if not isinstance(tickets, list):
                return
            def _num(t):
                return ''.join(ch for ch in str(t.get('ticket', '') or t.get('ticketNum', '')) if ch.isdigit())
            store['tickets'] = [t for t in tickets if _num(t) != target]
            store['count'] = len(store['tickets'])
            store['updated'] = _now_iso()
            _save_json(ANALYTICS_FILE, store)
    except Exception as e:
        _log('Analytics store delete failed: %s' % e)

def _ticket_digits(t):
    """Integer ticket number of a stored row, or None if it has none."""
    d = ''.join(ch for ch in str(t.get('ticket', '') or t.get('ticketNum', '')) if ch.isdigit())
    if not d:
        return None
    try:
        return int(d)
    except Exception:
        return None

def _store_result(t):
    """Map a stored ticket to the result shape the tool's panel expects."""
    return {
        'ticketNum':  t.get('ticket', '') or t.get('ticketNum', ''),
        'date':       t.get('date', ''),
        'tech':       t.get('tech', ''),
        'client':     t.get('client', ''),
        'product':    t.get('product', ''),
        'vehicle':    t.get('vehicle', ''),
        'issue':      t.get('issue', ''),
        'resolution': t.get('resolution', ''),
        'status':     t.get('status', ''),
        'serial':     t.get('serial', ''),
        'notes':      t.get('notes', ''),
    }

def _supabase_ticket_result(row):
    """Map a techSupport.tickets row to the Today/Search UI shape."""
    return {
        'ticketNum':  row.get('ticket_number', ''),
        'date':       row.get('ticket_date', ''),
        'tech':       row.get('tech', ''),
        'client':     row.get('client', ''),
        'product':    row.get('product', ''),
        'vehicle':    row.get('vehicle', ''),
        'issue':      row.get('issue', ''),
        'resolution': row.get('resolution', ''),
        'status':     row.get('status', ''),
        'serial':     row.get('serial', ''),
        'notes':      row.get('notes', ''),
    }

try:
    _TEXT_TYPE = unicode      # Python 2
    _BYTES_TYPE = str
except NameError:
    _TEXT_TYPE = str          # Python 3
    _BYTES_TYPE = bytes

def _u(v):
    """Coerce any value to text without blowing up on non-ASCII under Python 2
    (ticket 'issue' strings contain the U+2192 arrow, so str() would raise)."""
    if v is None:
        return u''
    if isinstance(v, _TEXT_TYPE):
        return v
    if isinstance(v, _BYTES_TYPE):
        try:
            return v.decode('utf-8')
        except Exception:
            return v.decode('latin-1', 'ignore')
    return _TEXT_TYPE(v)

# -- Write-behind queue (fast ticket saves) ------------------------------------
# Saving a ticket used to block on the (slow) Apps Script round-trip. Instead we
# now update the local store immediately, hand the user a result right away, and
# push to the Google Sheet in the background. The queue is persisted to disk and
# retried so a write always reaches the sheet eventually -- even across restarts.
_pending_lock = threading.Lock()
_last_reserved_num = [0]     # in-process high-water mark for predicted ticket #s
WRITE_RETRY_BASE = 5         # seconds; backoff grows with attempts
MAX_WRITE_ATTEMPTS = 25      # after this, stop retrying and dead-letter the op

def _reserve_next_num():
    """Reserve the next ticket number from the in-process high-water mark, so a
    save can ack the moment it's queued instead of parsing/writing the 2.3 MB
    store on the request thread. Seeded once from the store's max; the poll/
    reconcile paths keep it in step with numbers added directly in the sheet.
    Returns None if the store isn't seeded yet (caller uses the sync path)."""
    # Do the network read before taking the local store lock. Concurrent callers
    # still serialize the final reservation below and therefore receive distinct
    # numbers, while unrelated store reads are not blocked by Supabase latency.
    remote_max = 0
    ok, rows = _supabase_rest(
        'GET', 'tickets?select=ticket_number&order=ticket_number.desc&limit=100')
    if ok and isinstance(rows, list):
        for row in rows:
            digits = ''.join(ch for ch in str(row.get('ticket_number') or '') if ch.isdigit())
            if digits:
                try:
                    remote_max = max(remote_max, int(digits))
                except Exception:
                    pass

    with _lock:
        if _last_reserved_num[0] <= 0:
            if not os.path.exists(ANALYTICS_FILE):
                return None
            store = _analytics_store()
            if not isinstance(store.get('tickets'), list):
                return None
            _last_reserved_num[0] = _max_ticket_num(store)
        # Tickets can also be created from tickets.html directly in Supabase.
        # Include its high-water mark so this form does not reuse their numbers.
        _last_reserved_num[0] = max(_last_reserved_num[0], remote_max)
        _last_reserved_num[0] += 1
        return _last_reserved_num[0]

def _bump_reserved(store):
    """Keep the high-water mark at/above the store max. Caller must hold _lock
    (this never acquires it, since threading.Lock is not reentrant)."""
    try:
        m = _max_ticket_num(store)
        if m > _last_reserved_num[0]:
            _last_reserved_num[0] = m
    except Exception:
        pass

_mirror_jobs = []
_mirror_cv = threading.Condition()
_mirror_running = [False]

def _mirror_worker():
    while True:
        with _mirror_cv:
            while not _mirror_jobs:
                _mirror_cv.wait()
            fn = _mirror_jobs.pop(0)
        try:
            fn()
        except Exception as e:
            _log('store mirror op failed: %s' % e)

def _run_bg(fn):
    """Queue a store-mirror op to run on a single worker thread in submission
    order, so a create-append always lands before a follow-up update for the
    same ticket. Returns immediately; the durable write queue is the real safety
    net. Falls back to inline execution if the worker isn't running yet."""
    if _mirror_running[0]:
        with _mirror_cv:
            _mirror_jobs.append(fn)
            _mirror_cv.notify()
    else:
        try:
            fn()
        except Exception as e:
            _log('inline mirror failed: %s' % e)

def _gen_id():
    return '%d-%s' % (int(time.time() * 1000), os.urandom(4).encode('hex'))

def _load_pending():
    items = _load_json(PENDING_FILE, [])
    return items if isinstance(items, list) else []

def _save_pending(items):
    _save_json(PENDING_FILE, items)

# -- Dead letters: writes that exhausted their retries. Kept (not dropped) so an
#    operator can see store-vs-sheet divergence and replay them. ---------------
_dead_lock = threading.Lock()
def _load_dead():
    items = _load_json(DEAD_LETTERS_FILE, [])
    return items if isinstance(items, list) else []
def _save_dead(items):
    _save_json(DEAD_LETTERS_FILE, items)
def _dead_count():
    try:
        return len(_load_dead())
    except Exception:
        return 0
def _add_dead(op, reason):
    with _dead_lock:
        items = _load_dead()
        rec = dict(op)
        rec['dead_reason'] = reason
        rec['dead_at'] = _now_iso()
        items.append(rec)
        if len(items) > 500:
            items = items[-500:]
        _save_dead(items)

def _enqueue_write(op):
    with _pending_lock:
        items = _load_pending()
        items.append(op)
        _save_pending(items)

def _pending_ticket_nums():
    """Predicted numbers of tickets still waiting to reach the sheet."""
    out = set()
    with _pending_lock:
        for op in _load_pending():
            n = op.get('predicted_num')
            if n:
                try: out.add(int(n))
                except Exception: pass
    return out

def _pending_delete_nums():
    """Ticket numbers with a delete still waiting to reach the sheet, so a
    reconcile doesn't briefly re-add a ticket we've just deleted locally."""
    out = set()
    with _pending_lock:
        for op in _load_pending():
            if op.get('action') == 'deleteticket':
                n = (op.get('params') or {}).get('ticketNum')
                if n:
                    try: out.add(int(''.join(ch for ch in str(n) if ch.isdigit())))
                    except Exception: pass
    return out

def _remap_pending_ticketnum(old_num, new_num):
    """After the sheet assigns a number different from our prediction, repoint any
    still-queued update/delete ops for that ticket at the real number, so a
    create-then-discard (or create-then-update) doesn't act on the wrong row."""
    try:
        old = str(old_num).strip()
        new = str(new_num).strip()
        if not old or old == new:
            return
        with _pending_lock:
            items = _load_pending()
            changed = False
            for op in items:
                if op.get('action') in ('updateticket', 'deleteticket'):
                    p = op.get('params') or {}
                    cur = ''.join(ch for ch in str(p.get('ticketNum', '')) if ch.isdigit())
                    if cur and cur == old:
                        p['ticketNum'] = new
                        op['params'] = p
                        changed = True
            if changed:
                _save_pending(items)
    except Exception as e:
        _log('remap pending ticketNum failed: %s' % e)

def _optimistic_new_ticket(record):
    """Assign the next ticket number and append to the store in one lock, so the
    save can return instantly. Returns the number, or None if the store isn't
    seeded yet (caller then falls back to the synchronous path)."""
    with _lock:
        if not os.path.exists(ANALYTICS_FILE):
            return None
        store = _analytics_store()
        if not isinstance(store.get('tickets'), list):
            return None
        nxt = max(_max_ticket_num(store), _last_reserved_num[0]) + 1
        _last_reserved_num[0] = nxt
        rec = dict(record)
        rec['ticket'] = str(nxt)
        store['tickets'].append(rec)
        store['count'] = len(store['tickets'])
        store['updated'] = _now_iso()
        _save_json(ANALYTICS_FILE, store)
        return nxt

def _renumber_store_ticket(old_num, new_num):
    """If the sheet ends up assigning a different number than we predicted, fix
    the store entry so it matches the sheet."""
    try:
        with _lock:
            store = _analytics_store()
            for t in store.get('tickets', []):
                if str(t.get('ticket', '')).strip() == str(old_num):
                    t['ticket'] = str(new_num)
                    break
            store['updated'] = _now_iso()
            _save_json(ANALYTICS_FILE, store)
    except Exception as e:
        _log('Renumber %s->%s failed: %s' % (old_num, new_num, e))

def _build_appscript_url(data):
    """Build the signed Apps Script GET URL from a params dict."""
    import urllib
    config = _get_config()
    webapp = config.get('webapp_url', '')
    secret = config.get('secret_key', '')
    params = []
    for k, v in data.items():
        k_enc = unicode(k).encode('utf-8') if not isinstance(k, str) else k.encode('utf-8') if isinstance(k, unicode) else k
        v_enc = unicode(v).encode('utf-8') if not isinstance(v, str) else v.encode('utf-8') if isinstance(v, unicode) else str(v).encode('utf-8')
        params.append(urllib.quote(k_enc, safe='') + '=' + urllib.quote(v_enc, safe=''))
    params.append('key=' + urllib.quote(secret.encode('utf-8'), safe=''))
    return webapp + '?' + '&'.join(params)

def _process_write(op):
    """Push one queued op to the sheet. Returns True on success."""
    try:
        url = _build_appscript_url(op.get('params', {}))
        req = urllib2.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; ZZ2Server/1.0)'})
        resp = urllib2.urlopen(req, timeout=60)
        result = resp.read()
        try:
            parsed = json.loads(result)
        except Exception:
            parsed = {}
        # An explicit application error means the row didn't land -> retry.
        if isinstance(parsed, dict) and parsed.get('ok') is False:
            _log('Write-behind app error (%s): %s' % (op.get('action'), parsed.get('error')))
            return False
        # Reconcile the real sheet number into the store if it differs.
        if op.get('action') == 'newticket':
            actual = isinstance(parsed, dict) and parsed.get('ticketNum')
            pred = op.get('predicted_num')
            if actual and pred and str(actual) != str(pred):
                _renumber_store_ticket(pred, actual)
                _remap_pending_ticketnum(pred, actual)
        return True
    except Exception as e:
        _log('Write-behind failed (%s): %s' % (op.get('action'), e))
        return False

def _write_behind_loop():
    """Drain the pending queue in FIFO order, retrying the head item on failure
    so sheet order (and thus ticket numbering) is preserved."""
    while True:
        op = None
        with _pending_lock:
            items = _load_pending()
            if items:
                op = items[0]
        if not op:
            time.sleep(2)
            continue
        if _process_write(op):
            with _pending_lock:
                items = _load_pending()
                items = [x for x in items if x.get('id') != op.get('id')]
                _save_pending(items)
        else:
            attempts = op.get('attempts', 0) + 1
            if attempts >= MAX_WRITE_ATTEMPTS:
                # Give up so one bad op can't block the queue forever. The local
                # store already reflects the change; a later reconcile/rebuild
                # will resync this row from the sheet's actual state.
                _log('Write-behind GAVE UP after %d tries (%s %s): dead-lettered'
                     % (attempts, op.get('action'), op.get('predicted_num') or ''), AUDIT_LOG)
                _add_dead(op, 'gave up after %d attempts' % attempts)
                with _pending_lock:
                    items = _load_pending()
                    items = [x for x in items if x.get('id') != op.get('id')]
                    _save_pending(items)
                continue
            with _pending_lock:
                items = _load_pending()
                if items and items[0].get('id') == op.get('id'):
                    items[0]['attempts'] = attempts
                    _save_pending(items)
            time.sleep(min(120, WRITE_RETRY_BASE * attempts))


# Google can't push to a LAN box, so to catch tickets typed straight into the
# sheet we poll cheaply: ask Apps Script for anything newer than the highest
# ticket number we already have, and append it. Runs every POLL_INTERVAL.
POLL_INTERVAL = 30  # seconds

def _max_ticket_num(store):
    mx = 0
    for t in store.get('tickets', []):
        digits = ''.join(ch for ch in str(t.get('ticket', '')) if ch.isdigit())
        if digits:
            try:
                n = int(digits)
                if n > mx: mx = n
            except Exception:
                pass
    return mx

def _poll_new_tickets():
    """Pull tickets newer than our current max and merge them. Returns (ok, added)."""
    if not os.path.exists(ANALYTICS_FILE):
        return False, 'store not seeded'
    config = _get_config()
    webapp = config.get('webapp_url', '')
    if not webapp:
        return False, 'no webapp_url'
    with _lock:
        store = _analytics_store()
    after = _max_ticket_num(store)
    url = webapp + ('&' if '?' in webapp else '?') + 'action=since&after=' + str(after)
    body, code = _proxy_get(url)
    if code != 200:
        return False, 'since HTTP %d' % code
    try:
        parsed = json.loads(body)
    except Exception as e:
        return False, 'bad since JSON: %s' % e
    if isinstance(parsed, dict) and parsed.get('error'):
        return False, str(parsed['error'])
    newt = (parsed.get('tickets') if isinstance(parsed, dict) else parsed) or []
    if not newt:
        return True, 0
    with _lock:
        store = _analytics_store()
        if not isinstance(store.get('tickets'), list):
            return False, 'store not a list'
        have = set(str(t.get('ticket', '')).strip() for t in store['tickets'])
        added = 0
        for t in newt:
            tn = str(t.get('ticket', '')).strip()
            if tn and tn not in have:
                store['tickets'].append(t); have.add(tn); added += 1
        if added:
            store['count'] = len(store['tickets'])
            store['updated'] = _now_iso()
            _save_json(ANALYTICS_FILE, store)
        _bump_reserved(store)
    return True, added

RECONCILE_DAYS     = 2     # how much of the recent window to keep sheet-accurate
RECONCILE_INTERVAL = 300   # seconds between intraday reconciles

def _reconcile_recent(days=None):
    """Re-pull the last `days` of tickets from the sheet and make the store's
    recent window match it exactly -- so deletions and edits made directly in the
    sheet show up intraday, not only at the midnight rebuild. Only the recent
    window (ticket numbers at/above the lowest recent number) is replaced; older
    rows are untouched and wait for the nightly full rebuild. A blank/failed
    fetch is a no-op so we never wipe the window on a transient error.
    Returns (ok, removed) where removed is the net rows dropped (>= 0)."""
    if days is None:
        days = _reconcile_days()
    if not os.path.exists(ANALYTICS_FILE):
        return False, 'store not seeded'
    config = _get_config()
    webapp = config.get('webapp_url', '')
    if not webapp:
        return False, 'no webapp_url'
    url = webapp + ('&' if '?' in webapp else '?') + 'action=recent&days=' + str(int(days))
    body, code = _proxy_get(url)
    if code != 200:
        return False, 'recent HTTP %d' % code
    try:
        parsed = json.loads(body)
    except Exception as e:
        return False, 'bad recent JSON: %s' % e
    if isinstance(parsed, dict) and parsed.get('error'):
        return False, str(parsed['error'])
    if isinstance(parsed, dict):
        recent = parsed.get('results') or parsed.get('tickets')
    else:
        recent = parsed
    if not isinstance(recent, list) or not recent:
        return True, 0   # nothing authoritative came back -> leave the store alone

    def _num(t):
        digits = ''.join(ch for ch in str(t.get('ticket', '') or t.get('ticketNum', '')) if ch.isdigit())
        return int(digits) if digits else None

    nums = [n for n in (_num(t) for t in recent) if n is not None]
    if not nums:
        return True, 0
    floor = min(nums)

    norm = []
    for t in recent:
        r = dict(t)
        if not r.get('ticket') and r.get('ticketNum'):
            r['ticket'] = r.get('ticketNum')
        norm.append(r)

    # The sheet dates only the first ticket of each day (a visual carry-forward),
    # so the rest of the day comes back with a blank date. Forward-fill by ticket
    # number (which increases chronologically) so a blank row inherits the date of
    # the nearest earlier ticket -- otherwise yesterday's rows land in the store
    # dateless, since today is re-dated client-side and older days only get fixed
    # at the nightly rebuild.
    norm.sort(key=lambda r: (_num(r) is None, _num(r) or 0))
    _last_date = ''
    for r in norm:
        dv = _an_text(r.get('date') or r.get('Date') or '').strip()
        if dv:
            _last_date = dv
        elif _last_date:
            r['date'] = _last_date

    with _lock:
        store = _analytics_store()
        tickets = store.get('tickets')
        if not isinstance(tickets, list):
            return False, 'store not a list'
        pending = _pending_ticket_nums()
        deleting = _pending_delete_nums()
        recent_nums = set(nums)
        kept, dropped = [], 0
        for t in tickets:
            n = _num(t)
            if n is not None and n >= floor:
                # A just-created ticket that's still queued for the sheet won't be
                # in `recent` yet -- keep it so it doesn't blink out of the view.
                if n in pending and n not in recent_nums:
                    kept.append(t)
                else:
                    dropped += 1                 # in the recent window -> rebuilt from sheet
            else:
                kept.append(t)
        # Don't re-add a ticket whose delete is still in flight to the sheet.
        kept.extend([t for t in norm if _num(t) not in deleting])
        store['tickets'] = kept
        store['count'] = len(kept)
        store['updated'] = _now_iso()
        _save_json(ANALYTICS_FILE, store)
        _bump_reserved(store)
    net = dropped - len(norm)
    return True, (net if net > 0 else 0)

def _poll_loop():
    last_reconcile = 0
    while True:
        time.sleep(_poll_interval())
        if not _within_work_window():
            continue                       # outside configured hours: stay idle
        try:
            ok, info = _poll_new_tickets()
            _LAST_POLL[0] = time.time()
            if ok and info:
                _POLL_ADDED[0] += info
                _log('Poller: +%d sheet-direct ticket(s)' % info)
        except Exception as e:
            _log('Poller error: %s' % e)
        # Periodically reconcile the recent window so sheet-direct deletions and
        # edits are reflected intraday (the nightly rebuild still does the full pass).
        now = time.time()
        if now - last_reconcile >= _reconcile_interval():
            last_reconcile = now
            _LAST_RECONCILE[0] = now
            try:
                rok, removed = _reconcile_recent(_reconcile_days())
                if rok and removed:
                    _log('Reconcile: -%d removed/edited in recent window' % removed)
            except Exception as e:
                _log('Reconcile error: %s' % e)

# -- Stats ---------------------------------------------------------------------
def _increment_stat(username, kit=None, status=None):
    with _lock:
        stats = _load_json(STATS_FILE, {})
        today = datetime.date.today().isoformat()
        week  = datetime.date.today().strftime('%Y-W%W')
        month = datetime.date.today().strftime('%Y-%m')

        if username not in stats:
            stats[username] = {'total': 0, 'today': 0, 'this_week': 0,
                               'this_month': 0, 'last_date': '', 'last_week': '', 'last_month': ''}
        u = stats[username]
        if u.get('last_date') != today:
            u['today'] = 0; u['last_date'] = today
        if u.get('last_week') != week:
            u['this_week'] = 0; u['last_week'] = week
        if u.get('last_month') != month:
            u['this_month'] = 0; u['last_month'] = month

        u['total']      = u.get('total', 0) + 1
        u['today']      = u.get('today', 0) + 1
        u['this_week']  = u.get('this_week', 0) + 1
        u['this_month'] = u.get('this_month', 0) + 1
        stats[username] = u
        _save_json(STATS_FILE, stats)

# -- Kit editor helpers --------------------------------------------------------
def _safe_key(key):
    key = (key or '').strip()
    if not key or '/' in key or '\\' in key or '..' in key:
        return ''
    return key

def _safe_filename(name):
    name = os.path.basename((name or '').strip())
    name = _re.sub(r'[^A-Za-z0-9._-]', '_', name)
    return name

def _standard_troubleshooting_tree():
    """Safe baseline used once for kits that have no diagnostic content yet."""
    return [
        {'parent': '', 'issue': 'No power / black screen',
         'question': 'When does the screen stay black?', 'steps': []},
        {'parent': 'No power / black screen', 'issue': 'No power at any time',
         'question': '', 'steps': [
             'Confirm the vehicle and kit part number match the installation.',
             'Inspect the main harness, power connector, fuse, and ground connection.',
             'Measure constant power, accessory power, and ground with a multimeter.',
             'Reconnect the original equipment temporarily and confirm the factory system operates.',
             'Record measured voltages and connector photos before escalating.'
         ]},
        {'parent': 'No power / black screen', 'issue': 'Black screen after switching to the interface',
         'question': '', 'steps': [
             'Verify the LVDS input and output cables are connected in the correct direction.',
             'Compare all DIP-switch positions with the diagram for this exact kit.',
             'Power the vehicle and interface off, reseat the LVDS cables, then restart.',
             'Test the factory display path before reconnecting the interface.',
             'Capture a wiring photo and short video if the screen remains black.'
         ]},

        {'parent': '', 'issue': 'No audio / incorrect audio source',
         'question': 'Which audio symptom is present?', 'steps': []},
        {'parent': 'No audio / incorrect audio source', 'issue': 'No sound from CarPlay or Android Auto',
         'question': '', 'steps': [
             'Confirm the radio is set to the audio source required by the kit.',
             'Verify the AUX, USB, or fiber-loop connection shown in the wiring diagram.',
             'Raise phone, interface, and vehicle volume and disable mute.',
             'Delete the phone pairing, restart the phone, and pair again.',
             'Test with a second phone and record the result.'
         ]},
        {'parent': 'No audio / incorrect audio source', 'issue': 'Factory audio works but interface audio is distorted or intermittent',
         'question': '', 'steps': [
             'Reseat the interface audio connection and inspect it for bent or loose pins.',
             'Disable phone EQ, sound enhancement, and accessibility audio processing.',
             'Test at moderate volume with a second cable or second phone.',
             'Confirm the kit audio method matches the vehicle configuration.',
             'Document whether calls, music, and navigation prompts are affected equally.'
         ]},

        {'parent': '', 'issue': 'CarPlay / Android Auto will not connect',
         'question': 'How is the phone trying to connect?', 'steps': []},
        {'parent': 'CarPlay / Android Auto will not connect', 'issue': 'Wired connection',
         'question': '', 'steps': [
             'Use a known-good data cable; charge-only cables will not work.',
             'Connect directly to the kit USB port without an adapter or extension.',
             'Unlock the phone and approve every CarPlay or Android Auto prompt.',
             'Remove the vehicle from the phone and the phone from the interface, then reconnect.',
             'Test with a second supported phone.'
         ]},
        {'parent': 'CarPlay / Android Auto will not connect', 'issue': 'Wireless connection',
         'question': '', 'steps': [
             'Enable Bluetooth, Wi-Fi, and CarPlay or Android Auto on the phone.',
             'Forget the interface in both Bluetooth and Wi-Fi settings.',
             'Restart the phone and power-cycle the vehicle before pairing again.',
             'Temporarily disable VPN, personal hotspot, and wireless projection to other vehicles.',
             'Test wired operation first, then retry wireless pairing.'
         ]},

        {'parent': '', 'issue': 'Factory controls are not working',
         'question': 'Which controls are affected?', 'steps': []},
        {'parent': 'Factory controls are not working', 'issue': 'Steering-wheel buttons',
         'question': '', 'steps': [
             'Confirm the CAN and control connectors are fully seated in the supplied harness.',
             'Verify the vehicle profile and DIP-switch configuration.',
             'Test the buttons in the factory menu to separate a vehicle fault from an interface fault.',
             'Power-cycle the vehicle after changing configuration.',
             'List each working and non-working button before escalating.'
         ]},
        {'parent': 'Factory controls are not working', 'issue': 'Knob, touch, or console controller',
         'question': '', 'steps': [
             'Inspect the controller pass-through connectors and cable direction.',
             'Confirm the correct controller type is selected in interface settings.',
             'Test short press, long press, rotation, and directional movement separately.',
             'Reconnect the factory configuration and verify the controller itself works.',
             'Record the interface firmware version and a video of the symptom.'
         ]},

        {'parent': '', 'issue': 'Camera problem',
         'question': 'What happens when reverse is selected?', 'steps': []},
        {'parent': 'Camera problem', 'issue': 'No camera image',
         'question': '', 'steps': [
             'Confirm the camera type and voltage requirement before applying power.',
             'Inspect camera power, ground, video input, and reverse-trigger connections.',
             'Select the correct factory or aftermarket camera option in interface settings.',
             'Test the video signal with a known-good source when available.',
             'Capture the wiring and settings screens before escalating.'
         ]},
        {'parent': 'Camera problem', 'issue': 'Camera image remains on or switches at the wrong time',
         'question': '', 'steps': [
             'Verify the reverse trigger is connected to the specified wire and is not constant power.',
             'Check the camera delay and trigger mode in interface settings.',
             'Confirm CAN connections and DIP-switch positions.',
             'Power-cycle the vehicle after changing settings.',
             'Record when the image appears and disappears relative to gear changes.'
         ]},

        {'parent': '', 'issue': 'Intermittent operation / restarting',
         'question': 'When does the failure occur?', 'steps': []},
        {'parent': 'Intermittent operation / restarting', 'issue': 'Failure occurs while driving',
         'question': '', 'steps': [
             'Inspect all harness locks and strain relief; gently move each connection while testing.',
             'Monitor power and ground for voltage drop during the failure.',
             'Remove nonessential USB devices, adapters, and extensions.',
             'Check for overheating or a module mounted against a hot surface.',
             'Record the time, vehicle state, and video of the next occurrence.'
         ]},
        {'parent': 'Intermittent operation / restarting', 'issue': 'Failure occurs during startup',
         'question': '', 'steps': [
             'Allow the vehicle to shut down completely before retesting.',
             'Verify constant power, accessory power, CAN, and ground connections.',
             'Disconnect the phone and USB accessories, then test a clean startup.',
             'Confirm the interface firmware and vehicle profile.',
             'Compare cold-start and warm-start behavior.'
         ]},

        {'parent': '', 'issue': 'Installation or manual question',
         'question': '', 'steps': [
             'Confirm the exact kit key and vehicle year/model.',
             'Open the Manual tab and compare every connector with the wiring diagram.',
             'Use the DIP Switches tab before changing any switch position.',
             'Photograph the factory connectors and current installation before disconnecting anything.',
             'Contact support with the kit key, vehicle details, serial number, and photos if clarification is still needed.'
         ]}
    ]

def _ensure_local_kit_catalog():
    """Create/repair the server-local kit and troubleshooting stores.

    Older installs loaded both datasets from Google Sheets and therefore may
    have images on disk but no data/*.json files. On first local request we
    bootstrap kit records from the existing image/manual directories, then keep
    all subsequent edits in the two JSON stores. Existing records and trees are
    never overwritten.
    """
    kits_exists = os.path.exists(KITS_JSON)
    ts_exists = os.path.exists(TS_JSON)
    kits = _load_json(KITS_JSON, {}) if kits_exists else {}
    ts = _load_json(TS_JSON, {}) if ts_exists else {}
    if not isinstance(kits, dict):
        kits = {}
    if not isinstance(ts, dict):
        ts = {}

    changed_kits = not kits_exists
    changed_ts = not ts_exists
    disk_keys = set()
    for root in (IMAGES_DIR, MANUALS_DIR):
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isdir(full) and _safe_key(name):
                disk_keys.add(name)

    # Only infer new kit records when the catalog itself did not exist. Once it
    # exists, the Kit Editor is authoritative (a deliberately deleted kit must
    # not reappear merely because an old image folder remains on disk).
    if not kits_exists:
        for key in sorted(disk_keys):
            image_dir = os.path.join(IMAGES_DIR, key)
            manual_dir = os.path.join(MANUALS_DIR, key)
            record = {
                'vehicle': '', 'switch_button': '', 'audio_method': '',
                'notes': '', 'dip_image': '', 'wiring_image': '',
                'misc_image': '', 'manual': '',
            }
            for kind in ('dip', 'wiring', 'misc'):
                for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                    filename = kind + ext
                    if os.path.isfile(os.path.join(image_dir, filename)):
                        record[kind + '_image'] = '/images/' + key + '/' + filename
                        break
            if os.path.isdir(manual_dir):
                pdfs = sorted(f for f in os.listdir(manual_dir)
                              if f.lower().endswith('.pdf') and
                              os.path.isfile(os.path.join(manual_dir, f)))
                if pdfs:
                    record['manual'] = pdfs[0]
            kits[key] = record

    # Every kit has a local tree, even if it is intentionally empty.
    for key in kits:
        if key not in ts or not isinstance(ts.get(key), list):
            ts[key] = []
            changed_ts = True

    # One-time seed: populate only empty trees. Existing/customized diagnostic
    # content is preserved byte-for-byte, and a tree intentionally emptied after
    # this migration stays empty because the version marker prevents reseeding.
    meta = _load_json(CATALOG_META_JSON, {}) or {}
    try:
        seed_version = int(meta.get('troubleshooting_seed_version', 0))
    except Exception:
        seed_version = 0
    if seed_version < 1:
        standard_tree = _standard_troubleshooting_tree()
        for key in kits:
            if not ts.get(key):
                ts[key] = copy.deepcopy(standard_tree)
                changed_ts = True

    blueprints_exists = os.path.exists(BLUEPRINTS_JSON)
    blueprints = _load_json(BLUEPRINTS_JSON, {}) if blueprints_exists else {}
    if not isinstance(blueprints, dict):
        blueprints = {}
    changed_blueprints = not blueprints_exists
    if not blueprints_exists:
        now = int(time.time())
        blueprints['bp_standard_diagnostics'] = {
            'name': 'Standard interface diagnostics',
            'troubleshooting': copy.deepcopy(_standard_troubleshooting_tree()),
            'created_at': now, 'updated_at': now,
        }

    if changed_kits and not _save_json(KITS_JSON, kits):
        return False, 'Could not create local kits store'
    if changed_ts and not _save_json(TS_JSON, ts):
        return False, 'Could not create local troubleshooting store'
    if changed_blueprints and not _save_json(BLUEPRINTS_JSON, blueprints):
        return False, 'Could not create local blueprints store'
    if seed_version < 1:
        meta['troubleshooting_seed_version'] = 1
        meta['seeded_at'] = int(time.time())
        if not _save_json(CATALOG_META_JSON, meta):
            return False, 'Could not save local catalog version'
    if changed_kits or changed_ts or changed_blueprints:
        _log('Local kit catalog ready: %d kits, %d troubleshooting trees' %
             (len(kits), len(ts)))
    return True, {'kits': kits, 'troubleshooting': ts}

def _snapshot_kit(key, kits, ts):
    """Save the current kit + troubleshooting state to kit_history before a change."""
    try:
        hist_dir = os.path.join(KIT_HISTORY_DIR, key)
        if not os.path.exists(hist_dir):
            os.makedirs(hist_dir)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        path = os.path.join(hist_dir, stamp + '.json')
        n = 1
        while os.path.exists(path):
            path = os.path.join(hist_dir, '%s-%d.json' % (stamp, n)); n += 1
        with open(path, 'w') as f:
            json.dump({'saved_at': stamp, 'kit': kits.get(key),
                       'troubleshooting': ts.get(key, [])}, f)
        snaps = sorted([x for x in os.listdir(hist_dir) if x.endswith('.json')])
        while len(snaps) > 30:
            try: os.remove(os.path.join(hist_dir, snaps.pop(0)))
            except Exception: break
    except Exception as e:
        _log('Snapshot failed for %s: %s' % (key, e))

# -- Backup --------------------------------------------------------------------
import hashlib as _hashlib_bk

# Directories never included in a backup (transient, huge, or self-referential).
_BACKUP_SKIP_DIRS = set(['backups', 'cache', 'logs', 'drafts'])

def _sha256_file(path):
    try:
        h = _hashlib_bk.sha256()
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ''

def _backup_keep():
    try:
        return int(_cfg_num('backup_keep', 30, 3, 365))
    except Exception:
        return 30

# Single-file pre-rewrite snapshots (backups/pre_<ts>/) are kept in their own
# small bucket so they can never crowd a real full backup out of retention.
_SINGLE_KEEP = 10

def _backup_sort_key(nm):
    """Age key for a backup dir: parse the timestamp embedded in the name (works
    for both '20260611_120114' and 'pre_20260611_120114'); fall back to mtime."""
    base = nm[4:] if nm.startswith('pre_') else nm
    try:
        return time.mktime(time.strptime(base[:15], '%Y%m%d_%H%M%S'))
    except Exception:
        try:
            return os.path.getmtime(os.path.join(BACKUPS_DIR, nm))
        except Exception:
            return 0.0

def _backup_dirs():
    """(full, singles) backup dir names, each sorted oldest -> newest by real age.
    'config' (the live config-snapshot dir) is never a retention candidate."""
    full, singles = [], []
    try:
        names = os.listdir(BACKUPS_DIR)
    except Exception:
        names = []
    for nm in names:
        if nm == 'config' or not os.path.isdir(os.path.join(BACKUPS_DIR, nm)):
            continue
        (singles if nm.startswith('pre_') else full).append(nm)
    full.sort(key=_backup_sort_key)
    singles.sort(key=_backup_sort_key)
    return full, singles

def _backup_sources():
    """Everything that makes the site restorable: ALL top-level code + state
    files (*.py, *.html, *.js, *.json) plus the data/ tree. Images and manuals
    are included only when `backup_include_media` is enabled (they're large and
    change rarely), so routine snapshots stay small enough to keep many of them."""
    files, dirs = [], ['data']
    try:
        for nm in sorted(os.listdir(BASE_DIR)):
            full = os.path.join(BASE_DIR, nm)
            if os.path.isfile(full) and nm.rsplit('.', 1)[-1].lower() in ('py', 'html', 'js', 'json'):
                files.append(nm)
    except Exception:
        pass
    if _cfg_bool('backup_include_media', False):
        dirs += ['images', 'manuals']
    return files, dirs

def _run_backup(label='auto'):
    """Thorough snapshot: app code + state + data (+ optional media), with a
    manifest (sha256 of every file) so a snapshot can be verified and restored."""
    try:
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        dest = os.path.join(BACKUPS_DIR, ts)
        if os.path.exists(dest):
            ts = ts + '_' + os.urandom(2).encode('hex')
            dest = os.path.join(BACKUPS_DIR, ts)
        os.makedirs(dest)
        files, dirs = _backup_sources()
        manifest_files = []
        total = 0
        def _record(rel, full):
            try:
                sz = os.path.getsize(full)
            except Exception:
                sz = 0
            manifest_files.append({'path': rel, 'size': sz, 'sha256': _sha256_file(full)})
            return sz
        for nm in files:
            src = os.path.join(BASE_DIR, nm)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dest, nm))
                total += _record(nm, src)
        for d in dirs:
            src = os.path.join(BASE_DIR, d)
            if d in _BACKUP_SKIP_DIRS or not os.path.exists(src):
                continue
            shutil.copytree(src, os.path.join(dest, d))
            for root, _ds, fns in os.walk(src):
                for fn in fns:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, BASE_DIR)
                    total += _record(rel, full)
        import sys as _sys
        manifest = {
            'created':  _now_iso(),
            'label':    label,
            'ts':       ts,
            'base_dir': BASE_DIR,
            'python':   '.'.join(str(x) for x in _sys.version_info[:3]),
            'include_media': _cfg_bool('backup_include_media', False),
            'file_count': len(manifest_files),
            'total_bytes': total,
            'files':    sorted(manifest_files, key=lambda x: x['path']),
        }
        _save_json(os.path.join(dest, 'manifest.json'), manifest)
        # Retention: prune by real age (oldest first), in two independent buckets
        # so the tiny single-file 'pre_' snapshots never evict a full backup.
        keep = _backup_keep()
        full, singles = _backup_dirs()
        doomed = (full[:-keep] if keep > 0 else full) + \
                 (singles[:-_SINGLE_KEEP] if _SINGLE_KEEP > 0 else singles)
        for old in doomed:
            try:
                shutil.rmtree(os.path.join(BACKUPS_DIR, old))
            except Exception:
                pass
        _log('Backup completed: %s (%s, %d files, %d bytes)' % (ts, label, len(manifest_files), total))
        return ts
    except Exception as e:
        _log('Backup failed: ' + str(e))
        return None

def _backup_manifest(name):
    """Manifest for a snapshot. Synthesized on the fly for legacy backups that
    predate manifests so they can still be listed, downloaded, and restored."""
    safe = os.path.basename(name or '')
    path = os.path.join(BACKUPS_DIR, safe)
    if not safe or not os.path.isdir(path):
        return None
    mf = os.path.join(path, 'manifest.json')
    if os.path.exists(mf):
        m = _load_json(mf, None)
        if isinstance(m, dict):
            return m
    files, total = [], 0
    for root, _ds, fns in os.walk(path):
        for fn in fns:
            if fn == 'manifest.json':
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, path)
            try:
                sz = os.path.getsize(full)
            except Exception:
                sz = 0
            total += sz
            files.append({'path': rel, 'size': sz, 'sha256': ''})
    try:
        created = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        created = None
    return {'created': created, 'label': 'legacy', 'ts': safe, 'file_count': len(files),
            'total_bytes': total, 'files': sorted(files, key=lambda x: x['path']), 'legacy': True}

def _safe_rel(rel):
    """Reject absolute paths / traversal / protected dirs in a restore target."""
    rel = (rel or '').replace('\\', '/')
    if not rel or rel.startswith('/') or os.path.isabs(rel):
        return None
    rel = rel.strip('/')
    if not rel or rel.startswith('..') or '/../' in rel or rel.endswith('/..'):
        return None
    top = rel.split('/', 1)[0]
    if top in _BACKUP_SKIP_DIRS:
        return None
    return rel

def _restore_backup(name, wanted):
    """Restore files from a snapshot into BASE_DIR. Snapshots the CURRENT state
    first (label 'pre-restore') so the restore itself is reversible. `wanted` is
    a list of relpaths, or 'all'. Returns (ok, info)."""
    safe = os.path.basename(name or '')
    src_root = os.path.join(BACKUPS_DIR, safe)
    man = _backup_manifest(safe)
    if not man:
        return False, 'Backup not found'
    available = [f['path'] for f in man.get('files', [])]
    if wanted == 'all' or wanted == ['all'] or not wanted:
        targets = available
    else:
        targets = [r for r in wanted if r in available]
    targets = [r for r in targets if _safe_rel(r)]
    if not targets:
        return False, 'No valid files to restore'
    _run_backup(label='pre-restore')   # safety net before we overwrite anything
    restored, failed = [], []
    for rel in targets:
        srcf = os.path.join(src_root, rel)
        dstf = os.path.join(BASE_DIR, rel)
        try:
            d = os.path.dirname(dstf)
            if d and not os.path.exists(d):
                os.makedirs(d)
            tmp = dstf + '.restore.tmp'
            shutil.copy2(srcf, tmp)
            os.rename(tmp, dstf)        # atomic swap into place
            restored.append(rel)
        except Exception as e:
            failed.append({'path': rel, 'error': str(e)})
    return True, {'restored': restored, 'failed': failed, 'count': len(restored)}

def _backup_zip(name):
    """Build a .zip of a snapshot in CACHE_DIR and return its path (for download)."""
    import zipfile
    safe = os.path.basename(name or '')
    src_root = os.path.join(BACKUPS_DIR, safe)
    if not safe or not os.path.isdir(src_root):
        return None
    out = os.path.join(CACHE_DIR, 'zz2-backup-%s.zip' % safe)
    zf = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED)
    try:
        for root, _ds, fns in os.walk(src_root):
            for fn in fns:
                full = os.path.join(root, fn)
                arc = os.path.join(safe, os.path.relpath(full, src_root))
                zf.write(full, arc)
    finally:
        zf.close()
    return out

def _restart_server():
    """Re-exec this process so a freshly-restored/edited server.py takes effect.
    The listening socket is FD_CLOEXEC (set at startup), so exec releases port
    8080 and the new process rebinds it (allow_reuse_address covers TIME_WAIT).
    Same PID; queued writes, sessions, and lockouts all persist on disk across
    it. If exec fails, the socket stays open and the server keeps running."""
    import sys as _sys
    try:
        argv0 = _ARGV0[0] or os.path.abspath(_sys.argv[0])
        _log('Server re-executing: %s %s' % (_sys.executable, argv0), AUDIT_LOG)
        os.execv(_sys.executable, [_sys.executable, argv0] + _sys.argv[1:])
    except Exception as e:
        _log('Restart failed (server left running): %s' % e, AUDIT_LOG)

def _troubleshooting_coverage():
    """Health of every kit's troubleshooting tree: empty trees, and branching
    question nodes with no question heading (these render a generic prompt
    instead of a real question -- the class of bug that hid in DZZ BMW)."""
    ts = _load_json(TS_JSON, {}) or {}
    kits = _load_json(KITS_JSON, {}) or {}
    if isinstance(kits, list):
        kit_keys = [(k.get('key') or k.get('name')) for k in kits if isinstance(k, dict)]
    elif isinstance(kits, dict):
        kit_keys = list(kits.keys())
    else:
        kit_keys = []
    rows = []
    total_missing = 0
    all_keys = sorted(set([k for k in kit_keys if k] + list(ts.keys())))
    for key in all_keys:
        nodes = ts.get(key) or []
        if not isinstance(nodes, list):
            nodes = []
        nodes = [n for n in nodes if isinstance(n, dict)]
        missing = []
        for n in nodes:
            issue = (n.get('issue') or '').strip()
            if not issue:
                continue
            has_children = any((c.get('parent') or '').strip() == issue for c in nodes)
            has_steps = bool([x for x in (n.get('steps') or []) if _an_text(x).strip()])
            if has_children and not has_steps and not (n.get('question') or '').strip():
                missing.append(issue)
        total_missing += len(missing)
        rows.append({
            'kit': key,
            'nodes': len(nodes),
            'top_level': len([n for n in nodes if not (n.get('parent') or '').strip()]),
            'empty': len(nodes) == 0,
            'missing_headings': missing,
            'missing_count': len(missing),
        })
    rows.sort(key=lambda r: (-r['missing_count'], 0 if r['empty'] else 1, r['kit']))
    return {
        'ok': True,
        'kits_total': len(rows),
        'kits_empty': len([r for r in rows if r['empty']]),
        'kits_with_gaps': len([r for r in rows if r['missing_count'] > 0]),
        'missing_headings_total': total_missing,
        'rows': rows,
    }

# -- Nightly scheduler ---------------------------------------------------------
_last_nightly = None

_last_health = [0]   # epoch of the last health-summary log line

def _health_snapshot():
    """Cheap point-in-time view of the write pipeline + session/store state, used
    for the periodic health log so problems surface in the log instead of staying
    invisible behind swallowed exceptions."""
    try:
        pend = _load_pending()
        sess = _load_json(SESSIONS_FILE, {})
        now = time.time()
        active = sum(1 for v in (sess or {}).values() if (v.get('expires') or 0) > now)
        store = _analytics_store()
        tickets = store.get('tickets') if isinstance(store.get('tickets'), list) else []
        return {
            'pending':        len(pend),
            'oldest_pending': (pend[0].get('created') if pend else None),
            'dead':           _dead_count(),
            'sessions':       active,
            'tickets':        len(tickets),
            'last_poll':      _LAST_POLL[0],
            'last_reconcile': _LAST_RECONCILE[0],
        }
    except Exception as e:
        return {'error': str(e)}

def _log_health():
    """Write one HEALTH summary line, plus a HEALTH WARNING line when something
    needs a human (dead letters present, or the write queue backing up)."""
    h = _health_snapshot()
    if h.get('error'):
        _log('HEALTH error: %s' % h['error']); return
    _log('HEALTH pending=%(pending)s dead=%(dead)s sessions=%(sessions)s '
         'tickets=%(tickets)s last_poll=%(last_poll)s last_reconcile=%(last_reconcile)s' % h)
    warns = []
    if h['dead'] > 0:
        warns.append('%d dead-letter write(s) need attention' % h['dead'])
    if h['pending'] >= 25:
        warns.append('write queue backing up (%d pending, oldest %s)'
                     % (h['pending'], h['oldest_pending']))
    if warns:
        _log('HEALTH WARNING: ' + '; '.join(warns))

def _scheduler():
    global _last_nightly
    while True:
        try:
            now = datetime.datetime.now()
            today = now.date().isoformat()
            # Run nightly tasks at midnight if not already run today
            if now.hour == 0 and now.minute < 5 and _last_nightly != today:
                _last_nightly = today
                _log('Starting nightly tasks')
                _run_backup()
                if _get_config().get('auto_resolve_enabled', True):
                    ar_ok, ar = _auto_resolve(apply=True)
                    _log('Nightly auto-resolve: ok=%s applied=%s' %
                         (ar_ok, (ar.get('applied') if isinstance(ar, dict) else '?')))
                ok, info = _rebuild_analytics()
                _log('Nightly analytics rebuild: ok=%s (%s)' % (ok, info))
                _log('Nightly tasks complete')
        except Exception as e:
            _log('Scheduler error: ' + str(e))
        # Hourly health summary (and prompt warnings) so the write pipeline's
        # state is visible in the log without anyone opening a page.
        try:
            if time.time() - _last_health[0] >= 3600:
                _last_health[0] = time.time()
                _log_health()
        except Exception as e:
            _log('Health log error: ' + str(e))
        time.sleep(60)

# -- System telemetry (for the System Settings page) ---------------------------
def _read_proc_meminfo():
    info = {}
    try:
        f = open('/proc/meminfo')
        try:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    try:
                        info[parts[0].strip()] = int(parts[1].strip().split()[0])
                    except Exception:
                        pass
        finally:
            f.close()
    except Exception:
        pass
    return info

def _proc_self_rss_kb():
    try:
        f = open('/proc/self/status')
        try:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
        finally:
            f.close()
    except Exception:
        pass
    return 0

def _host_uptime():
    try:
        f = open('/proc/uptime')
        try:
            return float(f.read().split()[0])
        finally:
            f.close()
    except Exception:
        return None

def _thread_alive(name):
    try:
        for t in threading.enumerate():
            if t.name == name:
                return bool(t.is_alive())
    except Exception:
        pass
    return False

def _tail_lines(path, n=40):
    try:
        if not os.path.exists(path):
            return []
        f = open(path, 'rb')
        try:
            f.seek(0, 2)
            size = f.tell()
            chunk = size if size < 65536 else 65536
            f.seek(size - chunk)
            data = f.read()
        finally:
            f.close()
        lines = data.decode('utf-8', 'replace').splitlines()
        return lines[-n:]
    except Exception:
        return []

def _cpu_percent():
    """Live CPU busy %. Uses the delta between this call and the previous one
    (so it reflects activity over the page refresh interval); on the very first
    call it takes a brief inline sample so a value is always returned."""
    def snap():
        try:
            f = open('/proc/stat'); line = f.readline(); f.close()
            vals = [float(x) for x in line.split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)   # idle + iowait
            return sum(vals), idle
        except Exception:
            return None
    cur = snap()
    if cur is None:
        return None
    prev = _CPU_PREV[0]
    if prev is None:
        time.sleep(0.25)
        cur2 = snap()
        if cur2 is None:
            return None
        prev, cur = cur, cur2
    _CPU_PREV[0] = cur
    dt = cur[0] - prev[0]; di = cur[1] - prev[1]
    if dt <= 0:
        return None
    pct = 100.0 * (1.0 - di / dt)
    if pct < 0: pct = 0.0
    if pct > 100: pct = 100.0
    return round(pct, 1)

def _security_summary():
    now = time.time()
    locked = []; failing = []
    try:
        for u, f in _login_fails.items():
            until = f.get('until', 0); cnt = f.get('count', 0)
            uname = f.get('username', u.split('|')[0])
            if until > now:
                locked.append({'username': uname, 'ip': f.get('ip'), 'remaining_sec': int(until - now), 'count': cnt})
            elif cnt > 0:
                failing.append({'username': uname, 'ip': f.get('ip'), 'count': cnt})
    except Exception:
        pass
    creds = _load_json(CREDS_FILE, {}) or {}
    techs = creds.get('technicians', []) or []
    plain = [t.get('username') for t in techs if t.get('password') and not t.get('password_hash')]
    return {
        'locked':      sorted(locked, key=lambda x: -x['remaining_sec']),
        'failing':     sorted(failing, key=lambda x: -x['count']),
        'max_fails':   MAX_FAILS,
        'lockout_min': round(LOCKOUT_TIME / 60.0, 1),
        'plaintext':   {'count': len(plain), 'total': len(techs), 'users': plain},
    }

def _backups_list(limit=15):
    try:
        names = sorted([n for n in os.listdir(BACKUPS_DIR)
                        if n != 'config' and os.path.isdir(os.path.join(BACKUPS_DIR, n))],
                       key=_backup_sort_key, reverse=True)
    except Exception:
        names = []
    out = []
    for nm in names[:limit]:
        path = os.path.join(BACKUPS_DIR, nm)
        if nm == 'config' or not os.path.isdir(path):
            continue
        size = 0
        try:
            for root, dirs, fnames in os.walk(path):
                for fn in fnames:
                    try:
                        size += os.path.getsize(os.path.join(root, fn))
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = None
        label, nfiles, has_code = '', None, False
        m = _load_json(os.path.join(path, 'manifest.json'), None)
        if isinstance(m, dict):
            label = m.get('label', '')
            nfiles = m.get('file_count')
            has_code = any(str(f.get('path', '')).endswith(('.py', '.html', '.js'))
                           for f in m.get('files', []))
        elif nm.startswith('pre_'):
            label = 'file snapshot'
        out.append({'name': nm, 'size': size, 'mtime': mtime,
                    'label': label, 'files': nfiles, 'has_code': has_code})
    real = [n for n in names if n != 'config' and os.path.isdir(os.path.join(BACKUPS_DIR, n))]
    return {'count': len(real), 'recent': out}

def _system_telemetry():
    import sys as _sys
    now = time.time()
    mi = _read_proc_meminfo()
    mem_total = mi.get('MemTotal', 0)
    mem_free  = mi.get('MemFree', 0)
    mem_avail = mi.get('MemAvailable', mem_free + mi.get('Cached', 0) + mi.get('Buffers', 0))
    mem_used  = (mem_total - mem_avail) if mem_total else 0
    try:
        load = os.getloadavg()
    except Exception:
        load = (0, 0, 0)
    try:
        st = os.statvfs(BASE_DIR)
        s_total = st.f_blocks * st.f_frsize
        s_free  = st.f_bavail * st.f_frsize
        s_used  = s_total - s_free
    except Exception:
        s_total = s_free = s_used = 0
    store = _load_json(ANALYTICS_FILE, {}) or {}
    tickets = store.get('tickets', []) or []
    pend = _load_json(PENDING_FILE, []) or []
    pend_del = 0
    for o in pend:
        try:
            if o.get('action') == 'deleteticket':
                pend_del += 1
        except Exception:
            pass
    sessions = _load_sessions()
    sess_active = 0
    for v in sessions.values():
        if (v.get('expires') or 0) > now:
            sess_active += 1
    try:
        cfg = _get_config() or {}
    except Exception:
        cfg = {}
    _ws, _we = _work_window_secs()
    files = []
    for nm in ['credentials.json', 'roles.json', 'sessions.json', 'stats.json',
               'analytics.json', 'config.json', 'pending_writes.json']:
        pth = os.path.join(BASE_DIR, nm)
        try:
            stt = os.stat(pth)
            files.append({'name': nm, 'size_kb': round(stt.st_size / 1024.0, 1), 'mtime': stt.st_mtime})
        except Exception:
            files.append({'name': nm, 'size_kb': None, 'mtime': None})
    try:
        astore_size = os.path.getsize(ANALYTICS_FILE)
    except Exception:
        astore_size = 0
    return {
        'ok': True,
        'now': now,
        'server': {
            'python':  '.'.join(str(x) for x in _sys.version_info[:3]),
            'pid':     os.getpid(),
            'process_uptime_sec': now - _START_TIME,
            'started': _START_TIME,
            'threads': {
                'scheduler':    _thread_alive('scheduler'),
                'poller':       _thread_alive('poller'),
                'write_behind': _thread_alive('write_behind'),
            },
        },
        'host': {
            'uptime_sec':   _host_uptime(),
            'load':         [round(float(x), 2) for x in load],
            'mem_total_kb': mem_total,
            'mem_used_kb':  mem_used,
            'mem_avail_kb': mem_avail,
            'mem_pct':      round(100.0 * mem_used / mem_total, 1) if mem_total else 0,
            'proc_rss_kb':  _proc_self_rss_kb(),
            'cpu_pct':      _cpu_percent(),
        },
        'storage': {
            'total': s_total, 'used': s_used, 'free': s_free,
            'pct': round(100.0 * s_used / s_total, 1) if s_total else 0,
        },
        'store': {
            'ticket_count':    len(tickets),
            'generated':       store.get('generated'),
            'file_size_kb':    round(astore_size / 1024.0, 1),
            'last_nightly':    _last_nightly,
            'last_reconcile':  (_LAST_RECONCILE[0] or None),
            'last_poll':       (_LAST_POLL[0] or None),
            'poll_added_total': _POLL_ADDED[0],
            'pending_writes':  len(pend),
            'dead_letters':    _dead_count(),
            'pending_deletes': pend_del,
            'csv_cache':       len(CSV_CACHE),
            'sessions_active': sess_active,
        },
        'config': {
            'webapp_url':          cfg.get('webapp_url', ''),
            'poll_interval':       _poll_interval(),
            'pane_refresh_interval': _pane_refresh(),
            'reconcile_interval':  _reconcile_interval(),
            'reconcile_days':      _reconcile_days(),
            'session_ttl_days':    round(_session_ttl() / 86400.0, 2),
            'auto_resolve_enabled':_cfg_bool('auto_resolve_enabled', True),
            'auto_resolve_days':   int(_cfg_num('auto_resolve_days', AUTO_RESOLVE_DAYS, 1, 365)),
            'work_window_enabled': _cfg_bool('work_window_enabled', False),
            'work_start':          '%02d:%02d' % (_ws // 3600, (_ws % 3600) // 60),
            'work_end':            '%02d:%02d' % (_we // 3600, (_we % 3600) // 60),
            'work_window':         '%02d:%02d-%02d:%02d' % (_ws // 3600, (_ws % 3600) // 60, _we // 3600, (_we % 3600) // 60),
            'secret_key_set':      bool(cfg.get('secret_key')),
            'backup_keep':         _backup_keep(),
            'backup_include_media':_cfg_bool('backup_include_media', False),
            'base_dir':            BASE_DIR,
        },
        'data_files': files,
        'security': _security_summary(),
        'backups': _backups_list(),
    }

# -- HTTP Handler --------------------------------------------------------------
class ZZ2Handler(SimpleHTTPServer.SimpleHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            # API routes
            if path == '/api/config':
                return self._handle_config()
            if path == '/api/users':
                return self._handle_users_list()
            if path == '/api/roles':
                return self._handle_roles_list()
            if path == '/api/status':
                return self._handle_status()
            if path == '/api/system/info':
                return self._handle_system_info()
            if path == '/api/stats':
                return self._handle_stats()
            if path == '/api/analytics':
                return self._handle_analytics()
            if path == '/api/analytics/rebuild':
                return self._handle_analytics_rebuild()
            if path == '/api/tickets':
                return self._handle_tickets()
            if path == '/api/tickets/recent':
                return self._handle_tickets_recent()
            if path == '/api/tickets/search':
                return self._handle_tickets_search()
            if path == '/api/tickets/history':
                return self._handle_tickets_history()
            if path == '/api/item-sales':
                return self._handle_item_sales()
            if path == '/api/tickets/poll':
                return self._handle_tickets_poll()
            if path == '/api/tickets/get':
                return self._handle_tickets_get()
            if path == '/api/customer-history':
                return self._handle_customer_history()
            if path == '/api/customers':
                return self._handle_customers()
            if path.startswith('/api/proxy'):
                return self._handle_proxy()
            if path.startswith('/api/draft'):
                return self._handle_draft_get()
            if path == '/api/backup':
                return self._handle_backup()
            if path == '/api/kit/history':
                return self._handle_kit_history()
            if path == '/api/kit/coverage':
                return self._handle_kit_coverage()
            if path == '/api/blueprints':
                return self._handle_blueprints_list()
            if path == '/api/system/deadletters':
                return self._handle_system_deadletters()
            if path == '/api/system/sync-health':
                return self._handle_sync_health()
            if path == '/api/system/backup-info':
                return self._handle_backup_info()
            if path == '/api/system/backup-download':
                return self._handle_backup_download()

            # Root -> login page
            if path in ('/', ''):
                return self._serve_file('/login.html')

            # All file serving goes through _serve_file
            return self._serve_file(path)
        except Exception as e:
            _log('GET %s error: %s' % (path, e), AUDIT_LOG)
            try:
                self._json(500, {'error': 'Server error: %s' % e})
            except Exception:
                pass

    def do_POST(self):
        path = self.path
        try:
            body = self._read_body()
            if body is None:   # oversized body; 413 already sent
                return

            if path == '/login':
                return self._handle_login(body)
            if path == '/verify':
                return self._handle_verify(body)
            if path == '/logout':
                return self._handle_logout(body)
            if path == '/api/proxy':
                return self._handle_proxy_post(body)
            if path == '/api/draft':
                return self._handle_draft_post(body)
            if path == '/api/stats/increment':
                return self._handle_stats_increment(body)
            if path == '/api/system/backup':
                return self._handle_system_backup(body)
            if path == '/api/system/reconcile':
                return self._handle_system_reconcile(body)
            if path == '/api/system/rebuild':
                return self._handle_system_rebuild(body)
            if path == '/api/system/revoke':
                return self._handle_system_revoke(body)
            if path == '/api/system/clear-expired':
                return self._handle_system_clear_expired(body)
            if path == '/api/system/unlock':
                return self._handle_system_unlock(body)
            if path == '/api/system/autoresolve':
                return self._handle_system_autoresolve(body)
            if path == '/api/system/deadletters/replay':
                return self._handle_system_deadletters_replay(body)
            if path == '/api/system/deadletters/clear':
                return self._handle_system_deadletters_clear(body)
            if path == '/api/system/restore':
                return self._handle_system_restore(body)
            if path == '/api/system/restart':
                return self._handle_system_restart(body)
            if path == '/api/system/sync-supabase':
                return self._handle_system_sync_supabase(body)
            if path == '/api/system/config':
                return self._handle_system_config(body)
            if path == '/api/kit/save':
                return self._handle_kit_save(body)
            if path == '/api/kit/new':
                return self._handle_kit_new(body)
            if path == '/api/kit/duplicate':
                return self._handle_kit_duplicate(body)
            if path == '/api/kit/restore':
                return self._handle_kit_restore(body)
            if path == '/api/kit/delete':
                return self._handle_kit_delete(body)
            if path == '/api/blueprints/save':
                return self._handle_blueprint_save(body)
            if path == '/api/blueprints/delete':
                return self._handle_blueprint_delete(body)
            if path == '/api/blueprints/duplicate':
                return self._handle_blueprint_duplicate(body)
            if path == '/api/upload':
                return self._handle_upload(body)
            if path == '/api/users/create':
                return self._handle_users_create(body)
            if path == '/api/users/update':
                return self._handle_users_update(body)
            if path == '/api/users/password':
                return self._handle_users_password(body)
            if path == '/api/users/delete':
                return self._handle_users_delete(body)
            if path == '/api/roles/create':
                return self._handle_roles_create(body)
            if path == '/api/roles/update':
                return self._handle_roles_update(body)
            if path == '/api/roles/delete':
                return self._handle_roles_delete(body)

            self._json(404, {'error': 'Not found'})
        except Exception as e:
            _log('POST %s error: %s' % (path, e), AUDIT_LOG)
            try:
                self._json(500, {'error': 'Server error: %s' % e})
            except Exception:
                pass

    # -- Login --------------------------------------------------------------
    def _handle_login(self, body):
        data = self._parse(body)
        username = (data.get('username') or '').strip().lower()
        password = (data.get('password') or '').strip()
        ip = self.client_address[0]

        if _check_lockout(username, ip):
            _log('LOGIN BLOCKED (lockout): %s from %s' % (username, ip), AUDIT_LOG)
            return self._json(401, {'ok': False, 'error': 'Account temporarily locked. Try again in 5 minutes.'})

        creds = _load_json(CREDS_FILE, {})
        techs = creds.get('technicians', [])
        match = next((t for t in techs
                      if t.get('username','').lower() == username
                      and t.get('active', True) != False
                      and _verify_password(t, password)), None)

        if match:
            _clear_fails(username, ip)
            _upgrade_password_if_needed(match['username'], password)
            token = _make_token(username)
            session = {
                'username': match['username'],
                'display':  match['display'],
                'role':     match.get('role', 'tech'),
                'expires':  time.time() + _session_ttl(),
                'ip':       ip,
                'login_time': time.time(),
            }
            _save_session(token, session)
            _log('LOGIN: %s (%s) from %s' % (username, match.get('role','tech'), ip), AUDIT_LOG)
            self._json(200, {
                'ok':      True,
                'token':   token,
                'display': match['display'],
                'role':    match.get('role', 'tech'),
            })
        else:
            _record_fail(username, ip)
            _log('LOGIN FAIL: %s from %s' % (username, ip), AUDIT_LOG)
            self._json(401, {'ok': False, 'error': 'Invalid username or password.'})

    def _handle_verify(self, body):
        data = self._parse(body)
        token = data.get('token', '')
        session = _get_session(token)
        if not session:
            return self._json(401, {'ok': False, 'error': 'Invalid or expired session'})
        user = _session_user(session)   # fresh record; None if deactivated/deleted
        if not user:
            _delete_session(token)
            return self._json(401, {'ok': False, 'error': 'Account is inactive'})
        self._json(200, {
            'ok':          True,
            'display':     user.get('display', session.get('display', '')),
            'username':    user.get('username', session.get('username', '')),
            'role':        user.get('role', 'tech'),
            'permissions': sorted(_effective_perms(user)),
        })

    def _handle_logout(self, body):
        data = self._parse(body)
        token = data.get('token', '')
        if token:
            s = _get_session(token)
            if s:
                _log('LOGOUT: %s' % s.get('username','?'), AUDIT_LOG)
            _delete_session(token)
        self._json(200, {'ok': True})

    # -- Config (sanitized - no secrets) ------------------------------------
    def _handle_config(self):
        # The Apps Script secret key never leaves the server -- clients call the
        # authenticated /api/proxy instead. Only non-sensitive info is exposed.
        config = _get_config()
        self._json(200, {
            'server_url': 'http://192.168.1.190:8080',
            'webapp_url': config.get('webapp_url', ''),
            # Browser-safe public settings only. The service key never leaves
            # this process.
            'supabase_url': config.get('supabase_url', ''),
            'supabase_publishable_key': (config.get('supabase_publishable_key') or
                                         config.get('supabase_anon_key') or ''),
            'supabase_schema': 'techSupport',
        })

    # -- Status --------------------------------------------------------------
    def _handle_status(self):
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'system_settings'):
            return self._json(403, {'error': 'Forbidden'})

        import subprocess
        try:
            uptime = subprocess.check_output(['uptime']).strip()
        except:
            uptime = 'unknown'

        # Storage
        try:
            stat = os.statvfs(BASE_DIR)
            total_gb = (stat.f_blocks * stat.f_frsize) / (1024**3)
            free_gb  = (stat.f_bavail * stat.f_frsize) / (1024**3)
            used_gb  = total_gb - free_gb
        except:
            total_gb = used_gb = free_gb = 0

        # Active sessions
        sessions = _load_sessions()
        active = [{
            'username': v.get('username'),
            'display':  v.get('display'),
            'role':     v.get('role'),
            'ip':       v.get('ip'),
            'login_time': v.get('login_time'),
            'expires':  v.get('expires'),
        } for v in sessions.values()]

        self._json(200, {
            'ok': True,
            'uptime': str(uptime),
            'storage': {
                'total_gb': round(total_gb, 1),
                'used_gb':  round(used_gb, 1),
                'free_gb':  round(free_gb, 1),
            },
            'active_sessions': active,
            'last_backup': _last_nightly or 'Not yet run',
        })

    # -- System settings (super admin / `system_settings` only) --------------
    def _require_system(self):
        token = self._get_token()
        session = _get_session(token) if token else None
        user = _session_user(session)
        if not _has_perm(user, 'system_settings'):
            return None, None
        return user, token

    def _handle_system_info(self):
        user, token = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        info = _system_telemetry()
        now = time.time()
        sessions = _load_sessions()
        sess_list = []
        for tok, v in sessions.items():
            sess_list.append({
                'sid':        hashlib.sha256(tok.encode('utf-8')).hexdigest()[:12],
                'username':   v.get('username'),
                'display':    v.get('display'),
                'role':       v.get('role'),
                'ip':         v.get('ip'),
                'login_time': v.get('login_time'),
                'expires':    v.get('expires'),
                'expired':    bool((v.get('expires') or 0) and (v.get('expires') or 0) < now),
                'current':    (tok == token),
            })
        sess_list.sort(key=lambda x: x.get('login_time') or 0, reverse=True)
        info['sessions'] = sess_list
        info['recent_log'] = _tail_lines(AUDIT_LOG, 40)
        info['actor'] = user.get('display') or user.get('username')
        self._json(200, info)

    def _handle_system_backup(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        name = _run_backup(label='manual')
        _log('System: manual backup by %s (%s)' % (user.get('username'), name), AUDIT_LOG)
        man = _backup_manifest(name) if name else None
        self._json(200, {'ok': bool(name), 'name': name,
                         'files': (man or {}).get('file_count'),
                         'bytes': (man or {}).get('total_bytes')})

    def _handle_system_sync_supabase(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        ok, info = _sync_auxiliary_to_supabase()
        _log('System: auxiliary Supabase sync by %s (ok=%s)' %
             (user.get('username'), ok), AUDIT_LOG)
        if ok:
            return self._json(200, {'ok': True, 'synced': info})
        self._json(500, {'ok': False, 'error': info})

    def _handle_backup_info(self):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        name = self._qparam('name', '')
        man = _backup_manifest(name)
        if not man:
            return self._json(404, {'error': 'Backup not found'})
        return self._json(200, {'ok': True, 'manifest': man})

    def _handle_system_restore(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        data = self._parse(body)
        name = (data.get('name') or '').strip()
        files = data.get('files', 'all')
        ok, info = _restore_backup(name, files)
        if not ok:
            return self._json(400, {'ok': False, 'error': info})
        _log('System: restore from %s by %s (%d files)' %
             (name, user.get('username'), info.get('count', 0)), AUDIT_LOG)
        self._json(200, {'ok': True, 'name': name, 'result': info})

    def _handle_system_restart(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        if not _cfg_bool('allow_restart', True):
            return self._json(403, {'ok': False, 'error': 'Restart is disabled (set allow_restart=true in config.json).'})
        _log('System: RESTART requested by %s' % user.get('username'), AUDIT_LOG)
        self._json(200, {'ok': True, 'restarting': True})
        try:
            self.wfile.flush()
        except Exception:
            pass
        def _go():
            time.sleep(0.8)   # let the response reach the browser before we exec
            _restart_server()
        th = threading.Thread(target=_go, name='restart'); th.daemon = True; th.start()

    def _handle_backup_download(self):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        name = self._qparam('name', '')
        path = _backup_zip(name)
        if not path or not os.path.exists(path):
            return self._json(404, {'error': 'Backup not found'})
        try:
            with open(path, 'rb') as f:
                body = f.read()
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Disposition',
                         'attachment; filename="zz2-backup-%s.zip"' % os.path.basename(name))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_system_reconcile(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        try:
            ok, removed = _reconcile_recent()
            _LAST_RECONCILE[0] = time.time()
        except Exception as e:
            return self._json(500, {'ok': False, 'error': str(e)})
        _log('System: manual reconcile by %s (changed=%s)' % (user.get('username'), removed), AUDIT_LOG)
        self._json(200, {'ok': bool(ok), 'removed': removed})

    def _handle_system_rebuild(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        ok, info = _rebuild_analytics()
        _log('System: manual analytics rebuild by %s (ok=%s)' % (user.get('username'), ok), AUDIT_LOG)
        if ok:
            self._json(200, {'ok': True, 'count': info})
        else:
            _log('Analytics rebuild request failed: %s' % info)
            self._json(500, {'ok': False, 'error': info})

    def _handle_system_revoke(self, body):
        user, token = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        data = self._parse(body)
        sid = (data.get('sid') or '').strip()
        if not sid:
            return self._json(400, {'error': 'Missing sid'})
        sessions = _load_sessions()
        target = None
        for tok in list(sessions.keys()):
            if hashlib.sha256(tok.encode('utf-8')).hexdigest()[:12] == sid:
                target = tok
                break
        if not target:
            return self._json(404, {'error': 'Session not found'})
        if target == token:
            return self._json(400, {'error': "Can't revoke your own active session here."})
        who = sessions[target].get('username')
        _delete_session(target)
        _log('System: session revoked (%s) by %s' % (who, user.get('username')), AUDIT_LOG)
        self._json(200, {'ok': True, 'revoked': who})

    def _handle_system_clear_expired(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        now = time.time()
        sessions = _load_sessions()
        removed = 0
        for tok in list(sessions.keys()):
            if (sessions[tok].get('expires') or 0) < now:
                _delete_session(tok)
                removed += 1
        _log('System: cleared %d expired session(s) by %s' % (removed, user.get('username')), AUDIT_LOG)
        self._json(200, {'ok': True, 'removed': removed})

    def _handle_system_unlock(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        data = self._parse(body)
        uname = (data.get('username') or '').strip().lower()
        if not uname:
            return self._json(400, {'error': 'Missing username'})
        _clear_fails(uname)
        _log('System: lockout cleared for %s by %s' % (uname, user.get('username')), AUDIT_LOG)
        self._json(200, {'ok': True, 'unlocked': uname})

    def _handle_system_autoresolve(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        data = self._parse(body)
        apply = bool(data.get('apply'))
        cont = bool(data.get('cont'))   # continuation pass of a running apply
        days = data.get('days')
        try:
            days = int(days) if days not in (None, '') else None
        except Exception:
            days = None
        if apply and not cont:
            _run_backup()   # snapshot data files once, before the first write
        ok, res = _auto_resolve(days=days, apply=apply,
                                time_budget=(20 if apply else None))
        if ok and apply:
            if res.get('done'):
                try:
                    _rebuild_analytics()   # refresh the store once the run finishes
                except Exception:
                    pass
            _log('System: auto-resolve %s by %s (applied=%s remaining=%s)' %
                 ('DONE' if res.get('done') else 'pass', user.get('username'),
                  res.get('applied'), res.get('remaining')), AUDIT_LOG)
        elif ok:
            _log('System: auto-resolve preview by %s (candidates=%s)' %
                 (user.get('username'), res.get('candidates')), AUDIT_LOG)
        if ok:
            self._json(200, res)
        else:
            self._json(500, res if isinstance(res, dict) else {'error': str(res)})

    def _handle_system_config(self, body):
        """Super-admin-only write of whitelisted config.json values. Every field
        is validated and clamped; secret_key is never accepted from the browser.
        config.json is snapshotted before the write so a bad edit is recoverable."""
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        data = self._parse(body)
        if not isinstance(data, dict):
            return self._json(400, {'error': 'Bad payload'})

        cfg = _get_config()
        changes = []
        errors  = []

        def setnum(key, lo, hi, integer, label):
            if key not in data:
                return
            try:
                v = float(data.get(key))
            except Exception:
                errors.append('%s must be a number' % label); return
            if v != v or v < lo or v > hi:
                errors.append('%s must be between %s and %s' % (label, lo, hi)); return
            v = int(round(v)) if integer else round(v, 2)
            if cfg.get(key) != v:
                cfg[key] = v; changes.append(key)

        def setbool(key):
            if key not in data:
                return
            v = bool(data.get(key))
            if cfg.get(key) != v:
                cfg[key] = v; changes.append(key)

        def sethhmm(key, label):
            if key not in data:
                return
            raw = str(data.get(key) or '').strip()
            try:
                hh, mm = raw.split(':'); hh = int(hh); mm = int(mm)
                if not (0 <= hh < 24 and 0 <= mm < 60):
                    raise ValueError()
            except Exception:
                errors.append('%s must be HH:MM (24-hour)' % label); return
            norm = '%02d:%02d' % (hh, mm)
            if cfg.get(key) != norm:
                cfg[key] = norm; changes.append(key)

        setnum('poll_interval',      5,    3600,  True,  'Sheet poll interval')
        setnum('pane_refresh_interval', 2, 3600,  True,  'Today pane refresh')
        setnum('reconcile_interval', 30,   86400, True,  'Reconcile interval')
        setnum('reconcile_days',     1,    30,    True,  'Reconcile window')
        setnum('auto_resolve_days',  1,    365,   True,  'Auto-resolve threshold')
        setnum('session_ttl_days',   0.5,  90,    False, 'Session lifetime')
        setbool('auto_resolve_enabled')
        setbool('work_window_enabled')
        setnum('backup_keep', 3, 365, True, 'Backups to keep')
        setbool('backup_include_media')
        sethhmm('work_start', 'Work start')
        sethhmm('work_end',   'Work end')

        if 'webapp_url' in data:
            url = str(data.get('webapp_url') or '').strip()
            if url and not (url.startswith('https://script.google.com/') and '/exec' in url):
                errors.append('Apps Script URL must be an https://script.google.com/.../exec link')
            elif cfg.get('webapp_url') != url:
                cfg['webapp_url'] = url; changes.append('webapp_url')

        # secret_key is never writable from the browser
        if 'secret_key' in data:
            errors.append('secret_key cannot be changed from this page')

        if errors:
            return self._json(400, {'ok': False, 'error': '; '.join(errors)})
        if not changes:
            return self._json(200, {'ok': True, 'changed': []})

        # snapshot config.json before overwriting (keep last 20)
        try:
            if os.path.exists(CONFIG_FILE):
                bdir = os.path.join(BACKUPS_DIR, 'config')
                if not os.path.exists(bdir):
                    os.makedirs(bdir)
                ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
                shutil.copy2(CONFIG_FILE, os.path.join(bdir, 'config_%s.json' % ts))
                snaps = sorted(os.listdir(bdir))
                while len(snaps) > 20:
                    try:
                        os.remove(os.path.join(bdir, snaps.pop(0)))
                    except Exception:
                        break
        except Exception:
            pass

        if not _save_json(CONFIG_FILE, cfg):
            return self._json(500, {'ok': False, 'error': 'Could not write config.json'})
        _log('System: config updated by %s (%s)' %
             (user.get('username'), ', '.join(changes)), AUDIT_LOG)
        self._json(200, {'ok': True, 'changed': changes})

    def _require_perm(self, perm):
        token = self._get_token()
        session = _get_session(token) if token else None
        user = _session_user(session)
        if not _has_perm(user, perm):
            return None, None
        return user, token

    def _handle_sync_health(self):
        user, _t = self._require_perm('dashboard')
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        pend = _load_pending()
        store = _analytics_store()
        tickets = store.get('tickets') if isinstance(store.get('tickets'), list) else []
        self._json(200, {
            'ok': True,
            'pending_writes':   len(pend),
            'dead_letters':     _dead_count(),
            'oldest_pending':   (pend[0].get('created') if pend else None),
            'last_poll':        (_LAST_POLL[0] or None),
            'last_reconcile':   (_LAST_RECONCILE[0] or None),
            'poll_added_total': _POLL_ADDED[0],
            'ticket_count':     len(tickets),
            'store_generated':  store.get('generated'),
        })

    def _handle_system_deadletters(self):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        dead = _load_dead()
        out = []
        for d in dead[-200:]:
            p = d.get('params') or {}
            out.append({
                'id': d.get('id'), 'action': d.get('action'),
                'ticketNum': d.get('predicted_num') or p.get('ticketNum'),
                'client': p.get('client'), 'product': p.get('product'),
                'reason': d.get('dead_reason'), 'attempts': d.get('attempts'),
                'created': d.get('created'), 'dead_at': d.get('dead_at'),
            })
        self._json(200, {'ok': True, 'count': len(dead), 'items': out})

    def _handle_system_deadletters_replay(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        data = self._parse(body)
        wanted = (data.get('id') or '').strip()   # blank => replay all
        dead = _load_dead()
        keep, moved = [], 0
        for d in dead:
            if (not wanted) or d.get('id') == wanted:
                d['attempts'] = 0
                d.pop('dead_reason', None); d.pop('dead_at', None)
                _enqueue_write(d); moved += 1
            else:
                keep.append(d)
        _save_dead(keep)
        _log('System: replayed %d dead-letter(s) by %s' % (moved, user.get('username')), AUDIT_LOG)
        self._json(200, {'ok': True, 'replayed': moved, 'remaining': len(keep)})

    def _handle_system_deadletters_clear(self, body):
        user, _t = self._require_system()
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        n = len(_load_dead())
        _save_dead([])
        _log('System: cleared %d dead-letter(s) by %s' % (n, user.get('username')), AUDIT_LOG)
        self._json(200, {'ok': True, 'cleared': n})

    def _handle_kit_coverage(self):
        user, _t = self._require_perm('kit_editor')
        if not user:
            return self._json(403, {'error': 'Forbidden'})
        try:
            self._json(200, _troubleshooting_coverage())
        except Exception as e:
            _log('Tree coverage failed: %s' % e)
            self._json(200, {'ok': False, 'error': 'Coverage failed: %s' % e})

    # -- Proxy to Apps Script ------------------------------------------------
    def _handle_proxy(self):
        """GET proxy - handles CSV fetching and Apps Script calls server-side."""
        token = self._get_token()
        session = _get_session(token) if token else None

        config = _get_config()
        qs = self.path.split('?', 1)[1] if '?' in self.path else ''

        # Parse action
        action = ''
        for part in qs.split('&'):
            if part.startswith('action='):
                action = part[7:].split('&')[0]
                break

        # CSV proxy - allowed without auth (kit data is not sensitive)
        if action == 'csv':
            sheet = ''
            for part in qs.split('&'):
                if part.startswith('sheet='):
                    sheet = part[6:].split('&')[0]
                    break

            # Kits, diagrams, manuals and troubleshooting are server-local.
            # Never fall back to a remote CSV: a missing local store is safely
            # bootstrapped from images/ and manuals/ instead.
            if sheet == 'kits':
                local_file = KITS_JSON
            elif sheet == 'troubleshooting':
                local_file = TS_JSON
            else:
                return self._json(400, {'error': 'Unknown sheet: ' + sheet})

            ok, info = _ensure_local_kit_catalog()
            if not ok:
                return self._json(500, {'error': info})
            try:
                with open(local_file, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Data-Source', 'local')
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as e:
                _log('Local JSON read error: ' + str(e))
                return self._json(500, {'error': 'Could not read local ' + sheet + ' data'})

        # Apps Script proxy - require a real read permission (mapped per action),
        # not just any authenticated session.
        _puser = _session_user(session)
        if not _puser:
            return self._json(401, {'error': 'Unauthorized'})
        _need = PROXY_GET_ACTION_PERMS.get(action, 'tickets')
        if not _has_perm(_puser, _need):
            return self._json(403, {'error': 'You do not have permission for this action'})
        webapp = config.get('webapp_url', '')
        if not webapp:
            return self._json(500, {'error': 'webapp_url not configured'})

        # Correct query join: use '&' only if the webapp URL already has a '?'.
        # (The old code appended '&' whenever a query string was present, which
        # produced a malformed '...exec&action=...' URL with no '?'.)
        url = webapp + ('&' if '?' in webapp else '?') + qs
        # _proxy_get attaches the secret key server-side; don't add it here, and
        # never echo the URL (it would carry the key) or the response body (ticket
        # data) into the logs.
        _log('PROXY action=%s' % action)
        body, code = _proxy_get(url)
        _log('PROXY response: code=%d (%d bytes)' % (code, len(body)))
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_proxy_post(self, body):
        """POST proxy - ticket saves. Creates/edits return immediately after the
        local store is updated; the Google Sheet write happens in the background
        (durable, retried). Anything else proxies synchronously."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'create_tickets'):
            return self._json(403, {'error': 'You do not have permission to save tickets'})

        config = _get_config()
        webapp = config.get('webapp_url', '')
        if not webapp:
            return self._json(500, {'error': 'webapp_url not configured'})

        data = self._parse(body)
        if not data:
            return self._json(400, {'error': 'Invalid request body'})
        act = data.get('action')
        user = session.get('username', '?')

        # ---- Fast path: ticket create / edit -------------------------------
        # Ack as soon as the durable write is queued; mirror the (large) local
        # store on a background thread so the save returns instantly.
        if act == 'newticket':
            if not (_u(data.get('client', '')).strip() or
                    _u(data.get('product', '')).strip()):
                return self._json(400, {'ok': False,
                    'error': 'Enter a client or item before creating the ticket'})
            num = _reserve_next_num()
            if num is not None:
                sb_ok, sb_result = _supabase_create_ticket(data, num)
                if not sb_ok:
                    _log('Supabase ticket create failed #%s: %s' % (num, sb_result), AUDIT_LOG)
                    return self._json(502, {'ok': False,
                        'error': 'Could not create the ticket in Supabase'})
                if data.get('tech'):
                    _increment_stat(data['tech'].lower(), kit=data.get('product'))
                _enqueue_write({'id': _gen_id(), 'action': 'newticket', 'params': data,
                                'predicted_num': num, 'attempts': 0, 'created': _now_iso()})
                rec = _ticket_record(data, str(num))
                _run_bg(lambda: _append_ticket_to_store(rec))
                _log('Ticket queued (new #%d) user=%s' % (num, user))
                return self._json(200, {'ok': True, 'ticketNum': num, 'queued': True})
        elif act == 'updateticket':
            tnum = (data.get('ticketNum') or '')
            if tnum and os.path.exists(ANALYTICS_FILE):
                sb_ok, sb_result = _supabase_update_ticket(data, tnum)
                if not sb_ok:
                    _log('Supabase ticket update failed #%s: %s' % (tnum, sb_result), AUDIT_LOG)
                    return self._json(502, {'ok': False,
                        'error': 'Could not update the ticket in Supabase'})
                rec = _ticket_record(data, tnum)
                rec.pop('date', None)   # keep the original creation date on edits
                _enqueue_write({'id': _gen_id(), 'action': 'updateticket', 'params': data,
                                'predicted_num': None, 'attempts': 0, 'created': _now_iso()})
                _run_bg(lambda: _update_ticket_in_store(tnum, rec))
                _log('Ticket queued (update #%s) user=%s' % (tnum, user))
                return self._json(200, {'ok': True, 'ticketNum': tnum, 'queued': True})
        elif act == 'deleteticket':
            tnum = (data.get('ticketNum') or '')
            if tnum and os.path.exists(ANALYTICS_FILE):
                sb_ok, sb_result = _supabase_delete_ticket(tnum)
                if not sb_ok:
                    _log('Supabase ticket delete failed #%s: %s' % (tnum, sb_result), AUDIT_LOG)
                    return self._json(502, {'ok': False,
                        'error': 'Could not delete the ticket from Supabase'})
                _enqueue_write({'id': _gen_id(), 'action': 'deleteticket', 'params': data,
                                'predicted_num': None, 'attempts': 0, 'created': _now_iso()})
                _run_bg(lambda: _delete_ticket_from_store(tnum))
                _log('Ticket queued (delete #%s) user=%s' % (tnum, user))
                return self._json(200, {'ok': True, 'ticketNum': tnum, 'deleted': True, 'queued': True})

        # ---- Synchronous fallback (store not seeded, or other actions) -----
        url = _build_appscript_url(data)
        _log('Proxy POST action=%s user=%s (sync)' % (act, user))
        try:
            req = urllib2.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; ZZ2Server/1.0)'})
            resp = urllib2.urlopen(req, timeout=30)
            result = resp.read()
            if act == 'newticket' and data.get('tech'):
                _increment_stat(data['tech'].lower(), kit=data.get('product'))
            if act in ('newticket', 'updateticket'):
                try:
                    parsed = json.loads(result)
                except Exception:
                    parsed = {}
                tnum = parsed.get('ticketNum') or data.get('ticketNum') or ''
                if act == 'newticket':
                    _append_ticket_to_store(_ticket_record(data, tnum))
                else:
                    rec = _ticket_record(data, tnum)
                    rec.pop('date', None)
                    _update_ticket_in_store(tnum, rec)
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(result)))
            self.end_headers()
            self.wfile.write(result)
        except urllib2.HTTPError as e:
            err = 'HTTP %d: %s' % (e.code, e.reason)
            _log('Proxy POST error: ' + err)
            self._json(e.code, {'error': err})
        except Exception as e:
            _log('Proxy POST exception: ' + str(e))
            self._json(500, {'error': str(e)})

    # -- Draft tickets -------------------------------------------------------
    def _handle_draft_get(self):
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _session_user(session):
            return self._json(401, {'error': 'Unauthorized'})
        username = session['username']
        draft_file = os.path.join(DRAFTS_DIR, _safe_filename(username) + '.json')
        draft = _load_json(draft_file, None)
        self._json(200, {'ok': True, 'draft': draft})

    def _handle_draft_post(self, body):
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _session_user(session):
            return self._json(401, {'error': 'Unauthorized'})
        username = session['username']
        data = self._parse(body)
        draft_file = os.path.join(DRAFTS_DIR, _safe_filename(username) + '.json')
        if data.get('clear'):
            if os.path.exists(draft_file):
                os.remove(draft_file)
        else:
            _save_json(draft_file, data.get('draft'))
        self._json(200, {'ok': True})

    # -- Stats ----------------------------------------------------------------
    def _handle_stats(self):
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'dashboard'):
            return self._json(403, {'error': 'Forbidden'})
        stats = _load_json(STATS_FILE, {})
        self._json(200, {'ok': True, 'stats': stats})

    # -- Analytics ------------------------------------------------------------
    def _accepts_gzip(self):
        return 'gzip' in (self.headers.get('Accept-Encoding') or '').lower()

    def _gz(self, data):
        try:
            import gzip as _gzip
            from io import BytesIO
            buf = BytesIO()
            f = _gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6)
            f.write(data); f.close()
            return buf.getvalue()
        except Exception:
            return None

    def _send_payload(self, code, body, content_type='application/json; charset=utf-8', cache='no-store'):
        if not isinstance(body, bytes):
            body = body.encode('utf-8')
        enc = None
        if len(body) > 1024 and self._accepts_gzip():
            gz = self._gz(body)
            if gz is not None and len(gz) < len(body):
                body = gz; enc = 'gzip'
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', content_type)
        if enc:
            self.send_header('Content-Encoding', enc)
            self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Content-Length', str(len(body)))
        if cache:
            self.send_header('Cache-Control', cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _handle_analytics(self):
        """Serve the PRECOMPUTED analytics view (normalized + consolidated on the
        server, gzipped) so the page renders without crunching ~6k raw tickets in
        the browser. Falls back to a fresh build if the cached view is stale."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'analytics'):
            return self._json(403, {'error': 'Forbidden'})
        payload = _analytics_payload()
        if payload is not None:
            return self._send_payload(200, payload)
        # Not built yet: kick off a single background build and tell the client to
        # retry. We never run the heavy consolidate inside a request thread.
        _view_build_async()
        return self._json(202, {'building': True, 'rows': [],
            'message': 'Analytics view is building on the server; retry shortly.'})

    def _handle_analytics_rebuild(self):
        """Full re-pull of all tickets from Supabase. Requires `analytics`."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'analytics'):
            return self._json(403, {'error': 'Forbidden'})
        ok, info = _rebuild_analytics()
        if ok:
            self._json(200, {'ok': True, 'count': info})
        else:
            self._json(500, {'ok': False, 'error': info})

    # -- Tickets (spreadsheet view; any logged-in role) -----------------------
    def _handle_tickets(self):
        """Serve the local ticket store to any logged-in user (techs included).
        Same data as analytics, but not gated to admins."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'tickets'):
            return self._json(403, {'error': 'Forbidden'})
        if not os.path.exists(ANALYTICS_FILE):
            return self._json(404, {'ok': False,
                'error': 'Ticket store not built yet.', 'tickets': []})
        try:
            with open(ANALYTICS_FILE, 'rb') as f:
                body = f.read()
        except Exception as e:
            return self._json(500, {'error': str(e)})
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _qparam(self, name, default=''):
        qs = self.path.split('?', 1)[1] if '?' in self.path else ''
        for part in qs.split('&'):
            if part.startswith(name + '='):
                import urllib
                return urllib.unquote_plus(part[len(name) + 1:])
        return default

    def _handle_tickets_recent(self):
        """Return recent tickets from Supabase for every collaborator.

        The local analytics store only contains tickets mirrored through this
        server/Google Sheet, so using it alone hid tickets created directly in
        Supabase by other technicians. Supabase is authoritative here; local
        rows are merged only as a fallback/safety net for queued writes.
        """
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _session_user(session):
            return self._json(401, {'ok': False, 'error': 'Unauthorized', 'results': []})
        try:
            days = int(self._qparam('days', '1'))
        except Exception:
            days = 1
        if days < 1:
            days = 1

        store = _analytics_store()
        tickets = store.get('tickets') if isinstance(store.get('tickets'), list) else []
        n = min(3000, max(300, days * 200))
        deleting = _pending_delete_nums()

        # Fetch one extra calendar day. The browser applies the exact local-day
        # boundary, avoiding a midnight/timezone edge while keeping the query small.
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        fields = ('ticket_date,ticket_number,tech,client,product,vehicle,issue,'
                  'resolution,status,serial,notes')
        remote_rows = []
        remote_error = None
        page_size = 1000
        for page in range(10):
            resource = ('tickets?ticket_date=gte.%s&select=%s&'
                        'order=ticket_date.asc,ticket_number.asc&limit=%d&offset=%d' %
                        (cutoff, fields, page_size, page * page_size))
            ok, info = _supabase_rest('GET', resource)
            if not ok:
                remote_error = info
                break
            batch = info if isinstance(info, list) else []
            remote_rows.extend(batch)
            if len(batch) < page_size:
                break

        # Start with local rows so a just-queued legacy write is still visible;
        # matching Supabase rows replace them with the shared authoritative copy.
        merged = {}
        for index, ticket in enumerate(tickets[-n:]):
            number = _ticket_digits(ticket)
            if number in deleting:
                continue
            result = _store_result(ticket)
            key = str(number) if number is not None else 'local-%d' % index
            merged[key] = result

        if remote_error is None:
            for index, row in enumerate(remote_rows):
                result = _supabase_ticket_result(row)
                number = _ticket_digits({'ticket': result.get('ticketNum', '')})
                if number in deleting:
                    continue
                key = str(number) if number is not None else 'remote-%d' % index
                merged[key] = result
        else:
            _log('Recent tickets Supabase query failed; using local fallback: %s' % remote_error)

        results = list(merged.values())
        results.sort(key=lambda item: (
            _u(item.get('date', '')),
            _ticket_digits({'ticket': item.get('ticketNum', '')}) or 0))
        self._json(200, {'ok': True, 'results': results,
                         'pane_refresh': _pane_refresh(),
                         'source': 'supabase' if remote_error is None else 'local_fallback'})

    def _handle_tickets_get(self):
        """Fetch a single ticket by number straight from the local store --
        used by the realtime layer to resync one open ticket without waiting
        for the next Today-pane poll."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _session_user(session):
            return self._json(401, {'ok': False, 'error': 'Unauthorized'})
        raw = _u(self._qparam('ticketNum', '')).strip()
        wanted = _ticket_digits({'ticket': raw})
        if wanted is None:
            return self._json(400, {'ok': False, 'error': 'Missing ticketNum'})
        store = _analytics_store()
        tickets = store.get('tickets') if isinstance(store.get('tickets'), list) else []
        deleting = _pending_delete_nums()
        for t in reversed(tickets):   # tail first: the ticket being resynced is almost always recent
            if _ticket_digits(t) == wanted and wanted not in deleting:
                return self._json(200, {'ok': True, 'result': _store_result(t)})
        return self._json(404, {'ok': False, 'error': 'Ticket not found'})

    def _handle_tickets_search(self):
        """Full-text ticket search over the local store (newest first). The
        `limit` param is the SCAN window -- the dropdown reads "Scan last N
        tickets" -- so we search only the most recent N rows and return every
        match within that window (capped for the UI)."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'tickets'):
            return self._json(403, {'ok': False, 'error': 'Forbidden', 'results': []})
        q = _u(self._qparam('q', '')).strip().lower()
        try:
            scan = int(self._qparam('limit', '300'))
        except Exception:
            scan = 300
        scan = max(1, scan)
        MAX_RESULTS = 500            # protect the panel from a huge match set
        store = _analytics_store()
        tickets = store.get('tickets') if isinstance(store.get('tickets'), list) else []
        window = tickets[-scan:] if scan < len(tickets) else tickets
        deleting = _pending_delete_nums()   # hide rows whose delete is still in flight
        results = []
        if q:
            keys = ('ticket', 'client', 'product', 'tech', 'vehicle',
                    'issue', 'resolution', 'status', 'serial')
            for t in reversed(window):   # newest first, within the scan window
                if _ticket_digits(t) in deleting:
                    continue
                hay = u' '.join(_u(t.get(k, '')) for k in keys).lower()
                if q in hay:
                    results.append(_store_result(t))
                    if len(results) >= MAX_RESULTS:
                        break
        self._json(200, {'ok': True, 'results': results, 'scanned': len(window)})

    def _handle_tickets_history(self):
        """Exact ticket history for one client or item/kit from Supabase.

        The tickets spreadsheet page writes directly to techSupport.tickets, so
        history must read that table too. analytics.json can be stale or absent
        and is used only as a fallback when Supabase is unavailable.
        """
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'tickets'):
            return self._json(403, {'ok': False, 'error': 'Forbidden', 'results': [], 'count': 0})
        client = _u(self._qparam('client', '')).strip()
        product = _u(self._qparam('product', '')).strip()
        if not client and not product:
            return self._json(400, {'ok': False, 'error': 'Missing client or product',
                                    'results': [], 'count': 0})
        # Product history is always global: when a product is supplied, ignore
        # any client value and scan tickets from every customer.
        field = 'product' if product else 'client'
        value = product or client
        fuzzy = field == 'client' and self._qparam('fuzzy', '') in ('1', 'true', 'yes')
        if fuzzy:
            tokens = _re.findall(r'[a-z0-9]+', _norm_key(value))
            numeric = []
            for token in tokens:
                numeric.extend(_re.findall(r'\d+', token))
            ignored = set(['car', 'stereo', 'audio', 'alarm', 'the', 'and', 'inc', 'llc'])
            distinctive = [token for token in tokens if token not in ignored]
            choices = numeric or distinctive or tokens
            choices.sort(key=len, reverse=True)
            search_value = choices[0] if choices else value
            encoded = '*' + urllib.quote(_u(search_value).encode('utf-8')) + '*'
        else:
            encoded = urllib.quote(_u(value).encode('utf-8'))
        select = ('ticket_date,ticket_number,tech,client,product,vehicle,issue,'
                  'resolution,status,serial,notes')
        rows = []
        page_size = 1000
        supabase_error = None
        for page in range(50):
            resource = ('tickets?%s=ilike.%s&select=%s&order=ticket_date.desc.nullslast,'
                        'ticket_number.desc&limit=%d&offset=%d' %
                        (field, encoded, select, page_size, page * page_size))
            ok, info = _supabase_rest('GET', resource)
            if not ok:
                supabase_error = info
                break
            batch = info if isinstance(info, list) else []
            rows.extend(batch)
            if len(batch) < page_size:
                break

        if supabase_error is None:
            results = [{
                'ticketNum': row.get('ticket_number', ''),
                'date': row.get('ticket_date', ''),
                'tech': row.get('tech', ''),
                'client': row.get('client', ''),
                'product': row.get('product', ''),
                'vehicle': row.get('vehicle', ''),
                'issue': row.get('issue', ''),
                'resolution': row.get('resolution', ''),
                'status': row.get('status', ''),
                'serial': row.get('serial', ''),
                'notes': row.get('notes', ''),
            } for row in rows]
            return self._json(200, {'ok': True, 'results': results, 'count': len(results),
                                    'field': field, 'value': value,
                                    'scope': 'all_tickets' if field == 'product' else 'client',
                                    'source': 'supabase'})

        _log('Ticket history Supabase query failed; using local fallback: %s' % supabase_error)
        wanted = _norm_key(value)
        store = _analytics_store()
        tickets = store.get('tickets') if isinstance(store.get('tickets'), list) else []
        deleting = _pending_delete_nums()
        results = []
        for ticket in reversed(tickets):
            if _ticket_digits(ticket) in deleting:
                continue
            if _norm_key(_u(ticket.get(field, '')).strip()) == wanted:
                results.append(_store_result(ticket))
        self._json(200, {'ok': True, 'results': results, 'count': len(results),
                         'field': field, 'value': value,
                         'scope': 'all_tickets' if field == 'product' else 'client',
                         'source': 'local_fallback'})

    def _handle_customer_history(self):
        """Purchase/RMA history for the client on an open ticket, proxied
        server-side from a separate Supabase project (RLS there blocks the
        anon key, so the secret key -- config.json only -- never reaches the
        browser). Always answers HTTP 200; ok/error live in the body so the
        front-end never needs a separate HTTP-error branch from the
        no-match/no-config branches."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'tickets'):
            return self._json(403, {'ok': False, 'error': 'Forbidden'})
        client = _u(self._qparam('client', '')).strip()
        if not client:
            return self._json(400, {'ok': False, 'error': 'Missing client'})
        ck = _norm_key(client)
        now = time.time()
        cached = _CUSTOMER_HISTORY_CACHE.get(ck)
        if cached and (now - cached[1]) < _CUSTOMER_HISTORY_TTL:
            return self._json(200, cached[0])
        result = _build_customer_history(client)
        _CUSTOMER_HISTORY_CACHE[ck] = (result, now)
        self._json(200, result)

    def _handle_item_sales(self):
        """Lifetime sales quantity for one item/kit, read server-side because
        the zoho schema is not exposed to the browser publishable key."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'tickets'):
            return self._json(403, {'ok': False, 'error': 'Forbidden'})
        product = _u(self._qparam('product', '')).strip()
        if not product:
            return self._json(400, {'ok': False, 'error': 'Missing product'})
        key = _norm_key(product)
        now = time.time()
        cached = _ITEM_SALES_CACHE.get(key)
        if cached and (now - cached[1]) < _ITEM_SALES_TTL:
            return self._json(200, cached[0])
        result = _build_item_sales_summary(product)
        if result.get('ok'):
            _ITEM_SALES_CACHE[key] = (result, now)
        self._json(200, result)

    def _handle_customers(self):
        """Customer names and lifetime purchase/RMA totals for the searchable
        ticket client picker. The service key remains server-side."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'tickets'):
            return self._json(403, {'ok': False, 'error': 'Forbidden', 'customers': []})
        result = _customer_summaries()
        self._json(200 if result.get('ok') else 502, result)

    def _handle_tickets_poll(self):
        """Force an immediate poll for sheet-direct tickets, then report how many
        were added. Any logged-in role; used by the page's manual Refresh."""
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'tickets'):
            return self._json(403, {'error': 'Forbidden'})
        ok, info = _poll_new_tickets()
        removed = 0
        try:
            rok, rinfo = _reconcile_recent()
            if rok:
                removed = rinfo
        except Exception:
            removed = 0
        if ok:
            self._json(200, {'ok': True, 'added': info, 'removed': removed})
        else:
            self._json(200, {'ok': False, 'error': info, 'added': 0, 'removed': removed})

    def _handle_stats_increment(self, body):
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _session_user(session):
            return self._json(401, {'error': 'Unauthorized'})
        data = self._parse(body)
        username = session['username']
        _increment_stat(username, kit=data.get('kit'), status=data.get('status'))
        self._json(200, {'ok': True})

    # -- Backup trigger -------------------------------------------------------
    def _handle_backup(self):
        token = self._get_token()
        session = _get_session(token) if token else None
        if not _has_perm(_session_user(session), 'system_settings'):
            return self._json(403, {'error': 'Forbidden'})
        t = threading.Thread(target=_run_backup)
        t.daemon = True
        t.start()
        self._json(200, {'ok': True, 'message': 'Backup started'})

    # -- User management (admin / super_admin) --------------------------------
    def _users_actor(self):
        """The acting user, only if they may manage users."""
        token = self._get_token()
        session = _get_session(token) if token else None
        user = _session_user(session)
        if not _has_perm(user, 'manage_users'):
            self._json(403, {'error': 'User management requires the Manage users permission'})
            return None
        return user

    def _handle_users_list(self):
        actor = self._users_actor()
        if not actor: return
        creds = _load_json(CREDS_FILE, {})
        techs = creds.get('technicians', [])
        rmap = _role_map()
        actor_un = actor.get('username', '').lower()
        out = []
        for t in techs:
            role = t.get('role', 'tech')
            un = t.get('username', '')
            rdef = rmap.get(role) or {}
            out.append({
                'username':    un,
                'display':     t.get('display', ''),
                'role':        role,
                'role_label':  rdef.get('label', role),
                'role_rank':   rdef.get('rank', 0),
                'active':      t.get('active', True) != False,
                'permissions': _clean_perms(t.get('permissions')),  # per-user extras
                'hashed':      bool(t.get('password_hash')),
                'manageable':  _can_manage(actor, role),
                'is_self':     un.lower() == actor_un,
            })
        out.sort(key=lambda u: (-u['role_rank'], (u['display'] or u['username']).lower()))
        roles = [{'key': r['key'], 'label': r.get('label', r['key']),
                  'rank': r.get('rank', 0), 'system': bool(r.get('system')),
                  'root': bool(r.get('root')),
                  'permissions': list(PERMISSION_KEYS) if r.get('root')
                                 else _clean_perms(r.get('permissions'))}
                 for r in _load_roles()]
        roles.sort(key=lambda r: -r['rank'])
        self._json(200, {
            'ok':                 True,
            'actor_username':     actor.get('username', ''),
            'actor_role':         actor.get('role', ''),
            'actor_perms':        sorted(_effective_perms(actor)),
            'can_manage_roles':   _has_perm(actor, 'manage_roles'),
            'assignable_roles':   _assignable_roles(actor),
            'roles':              roles,
            'permission_catalog': PERMISSION_CATALOG,
            'users':              out,
        })

    def _handle_users_create(self, body):
        actor = self._users_actor()
        if not actor: return
        data = self._parse(body)
        username = (data.get('username') or '').strip().lower()
        display  = (data.get('display') or '').strip()
        password = data.get('password') or ''
        role     = (data.get('role') or 'tech').strip()
        if not _re.match(r'^[a-z0-9._-]{2,32}$', username):
            return self._json(400, {'error': 'Username must be 2-32 chars: letters, numbers, . _ -'})
        if not display:
            return self._json(400, {'error': 'Display name is required'})
        if len(password) < 6:
            return self._json(400, {'error': 'Password must be at least 6 characters'})
        if role not in _assignable_roles(actor):
            return self._json(403, {'error': "You can't create a user with that role"})
        # Per-user extra grants can't exceed what the actor holds.
        perms = [p for p in _clean_perms(data.get('permissions')) if p in _effective_perms(actor)]
        with _lock:
            creds = _load_json(CREDS_FILE, {})
            techs = creds.setdefault('technicians', [])
            if any(t.get('username', '').lower() == username for t in techs):
                return self._json(409, {'error': 'That username already exists'})
            techs.append({
                'username':      username,
                'display':       display,
                'role':          role,
                'active':        True,
                'permissions':   perms,
                'password_hash': _hash_password(password),
            })
            _save_json(CREDS_FILE, creds)
        _log('USER CREATE: %s (%s) by %s' % (username, role, actor['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'username': username})

    def _handle_users_update(self, body):
        actor = self._users_actor()
        if not actor: return
        data = self._parse(body)
        target_un = (data.get('username') or '').strip().lower()
        actor_un  = actor.get('username', '').lower()
        is_self   = (target_un == actor_un)
        rename_to = None
        with _lock:
            creds = _load_json(CREDS_FILE, {})
            techs = creds.get('technicians', [])
            target = next((t for t in techs if t.get('username', '').lower() == target_un), None)
            if not target:
                return self._json(404, {'error': 'User not found'})
            cur_role = target.get('role', 'tech')
            if not is_self and not _can_manage(actor, cur_role):
                return self._json(403, {'error': "You can't manage this user"})

            if 'display' in data:
                disp = (data.get('display') or '').strip()
                if not disp:
                    return self._json(400, {'error': 'Display name is required'})
                target['display'] = disp

            if 'role' in data and data.get('role') != cur_role:
                new_role = data.get('role')
                if is_self:
                    return self._json(403, {'error': "You can't change your own role"})
                if not _can_manage(actor, cur_role):
                    return self._json(403, {'error': "You can't manage this user"})
                if new_role not in _assignable_roles(actor):
                    return self._json(403, {'error': "You can't assign that role"})
                if _is_root_role(cur_role) and not _is_root_role(new_role) \
                   and _count_active_root(techs) <= 1:
                    return self._json(409, {'error': 'Cannot remove the last super admin'})
                target['role'] = new_role
                cur_role = new_role

            if 'active' in data:
                want_active = (data.get('active') != False)
                if not want_active:
                    if is_self:
                        return self._json(403, {'error': "You can't deactivate yourself"})
                    if _is_root_role(cur_role) and _count_active_root(techs) <= 1:
                        return self._json(409, {'error': 'Cannot deactivate the last super admin'})
                target['active'] = want_active

            if 'permissions' in data:
                # Per-user extras, capped to what the actor holds.
                target['permissions'] = [p for p in _clean_perms(data.get('permissions'))
                                         if p in _effective_perms(actor)]

            if data.get('new_username'):
                nu = data.get('new_username').strip().lower()
                if nu != target_un:
                    if not _re.match(r'^[a-z0-9._-]{2,32}$', nu):
                        return self._json(400, {'error': 'Invalid new username'})
                    if any(t.get('username', '').lower() == nu for t in techs):
                        return self._json(409, {'error': 'That username already exists'})
                    target['username'] = nu
                    if is_self:
                        rename_to = (target_un, nu)

            _save_json(CREDS_FILE, creds)
        if rename_to:
            _rename_user_sessions(rename_to[0], rename_to[1])
        _log('USER UPDATE: %s by %s' % (target_un, actor['username']), AUDIT_LOG)
        self._json(200, {'ok': True})

    def _handle_users_password(self, body):
        actor = self._users_actor()
        if not actor: return
        data = self._parse(body)
        target_un = (data.get('username') or '').strip().lower()
        password = data.get('password') or ''
        is_self = (target_un == actor.get('username', '').lower())
        if len(password) < 6:
            return self._json(400, {'error': 'Password must be at least 6 characters'})
        with _lock:
            creds = _load_json(CREDS_FILE, {})
            techs = creds.get('technicians', [])
            target = next((t for t in techs if t.get('username', '').lower() == target_un), None)
            if not target:
                return self._json(404, {'error': 'User not found'})
            if not is_self and not _can_manage(actor, target.get('role', 'tech')):
                return self._json(403, {'error': "You can't manage this user"})
            target['password_hash'] = _hash_password(password)
            target.pop('password', None)   # drop any legacy plaintext
            _save_json(CREDS_FILE, creds)
        _log('USER PASSWORD set: %s by %s' % (target_un, actor['username']), AUDIT_LOG)
        self._json(200, {'ok': True})

    def _handle_users_delete(self, body):
        actor = self._users_actor()
        if not actor: return
        data = self._parse(body)
        target_un = (data.get('username') or '').strip().lower()
        if target_un == actor.get('username', '').lower():
            return self._json(403, {'error': "You can't delete your own account"})
        with _lock:
            creds = _load_json(CREDS_FILE, {})
            techs = creds.get('technicians', [])
            target = next((t for t in techs if t.get('username', '').lower() == target_un), None)
            if not target:
                return self._json(404, {'error': 'User not found'})
            if not _can_manage(actor, target.get('role', 'tech')):
                return self._json(403, {'error': "You can't manage this user"})
            if _is_root_role(target.get('role')) and _count_active_root(techs) <= 1:
                return self._json(409, {'error': 'Cannot delete the last super admin'})
            creds['technicians'] = [t for t in techs
                                    if t.get('username', '').lower() != target_un]
            _save_json(CREDS_FILE, creds)
        _log('USER DELETE: %s by %s' % (target_un, actor['username']), AUDIT_LOG)
        self._json(200, {'ok': True})

    # -- Role management (manage_roles) ---------------------------------------
    def _roles_actor(self):
        token = self._get_token()
        session = _get_session(token) if token else None
        user = _session_user(session)
        if not _has_perm(user, 'manage_roles'):
            self._json(403, {'error': 'Role management requires the Manage roles permission'})
            return None
        return user

    def _count_role_users(self, role_key, techs=None):
        if techs is None:
            techs = _load_json(CREDS_FILE, {}).get('technicians', [])
        return sum(1 for t in techs if t.get('role') == role_key)

    def _handle_roles_list(self):
        actor = self._roles_actor()
        if not actor: return
        techs = _load_json(CREDS_FILE, {}).get('technicians', [])
        out = []
        for r in _load_roles():
            n = self._count_role_users(r['key'], techs)
            editable = _can_edit_role(actor, r)
            out.append({
                'key':         r['key'],
                'label':       r.get('label', r['key']),
                'rank':        r.get('rank', 0),
                'system':      bool(r.get('system')),
                'root':        bool(r.get('root')),
                'permissions': _clean_perms(r.get('permissions')) if not r.get('root') else list(PERMISSION_KEYS),
                'user_count':  n,
                'editable':    editable,
                'deletable':   editable and not r.get('system') and n == 0,
            })
        out.sort(key=lambda r: -r['rank'])
        # Highest rank this actor can give a role they create/edit.
        max_rank = (ROOT_RANK - 1) if _is_root_role(actor.get('role')) else (_actor_rank(actor) - 1)
        self._json(200, {
            'ok':                 True,
            'actor_role':         actor.get('role', ''),
            'actor_perms':        sorted(_effective_perms(actor)),
            'max_assignable_rank': max(1, max_rank),
            'permission_catalog': PERMISSION_CATALOG,
            'roles':              out,
        })

    def _validate_role_input(self, actor, label, rank, perms):
        """Shared label/rank/permission checks. Returns error string or None."""
        if not label:
            return 'Role name is required'
        try:
            rank = int(rank)
        except Exception:
            return 'Level must be a number'
        ceiling = (ROOT_RANK - 1) if _is_root_role(actor.get('role')) else (_actor_rank(actor) - 1)
        if rank < 1 or rank > ceiling:
            return 'Level must be between 1 and %d' % max(1, ceiling)
        bad = [p for p in perms if p not in _effective_perms(actor)]
        if bad:
            return "You can't grant permissions you don't have"
        return None

    def _handle_roles_create(self, body):
        actor = self._roles_actor()
        if not actor: return
        data = self._parse(body)
        key   = (data.get('key') or '').strip().lower()
        label = (data.get('label') or '').strip()
        perms = _clean_perms(data.get('permissions'))
        rank  = data.get('rank')
        if not _re.match(ROLE_KEY_RE, key):
            return self._json(400, {'error': 'Role ID must start with a letter; letters, numbers, underscore only'})
        err = self._validate_role_input(actor, label, rank, perms)
        if err:
            return self._json(400, {'error': err})
        with _lock:
            roles = _load_roles()
            if any(r.get('key') == key for r in roles):
                return self._json(409, {'error': 'A role with that ID already exists'})
            roles.append({'key': key, 'label': label, 'rank': int(rank),
                          'permissions': perms, 'system': False})
            _save_roles(roles)
        _log('ROLE CREATE: %s by %s' % (key, actor['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'key': key})

    def _handle_roles_update(self, body):
        actor = self._roles_actor()
        if not actor: return
        data = self._parse(body)
        key = (data.get('key') or '').strip().lower()
        with _lock:
            roles = _load_roles()
            role = next((r for r in roles if r.get('key') == key), None)
            if not role:
                return self._json(404, {'error': 'Role not found'})
            if not _can_edit_role(actor, role):
                return self._json(403, {'error': "You can't edit this role"})
            label = (data.get('label') if 'label' in data else role.get('label', '')).strip()
            perms = _clean_perms(data.get('permissions')) if 'permissions' in data \
                    else _clean_perms(role.get('permissions'))
            # System roles keep their fixed rank; custom roles may move.
            rank = role.get('rank') if role.get('system') else \
                   (data.get('rank') if 'rank' in data else role.get('rank'))
            err = self._validate_role_input(actor, label, rank, perms)
            if err:
                return self._json(400, {'error': err})
            role['label'] = label
            role['permissions'] = perms
            role['rank'] = int(rank)
            _save_roles(roles)
        _log('ROLE UPDATE: %s by %s' % (key, actor['username']), AUDIT_LOG)
        self._json(200, {'ok': True})

    def _handle_roles_delete(self, body):
        actor = self._roles_actor()
        if not actor: return
        data = self._parse(body)
        key = (data.get('key') or '').strip().lower()
        with _lock:
            roles = _load_roles()
            role = next((r for r in roles if r.get('key') == key), None)
            if not role:
                return self._json(404, {'error': 'Role not found'})
            if role.get('system'):
                return self._json(403, {'error': "Built-in roles can't be deleted"})
            if not _can_edit_role(actor, role):
                return self._json(403, {'error': "You can't delete this role"})
            n = self._count_role_users(key)
            if n > 0:
                return self._json(409, {'error': 'Reassign the %d user%s on this role first'
                                        % (n, '' if n == 1 else 's')})
            roles = [r for r in roles if r.get('key') != key]
            _save_roles(roles)
        _log('ROLE DELETE: %s by %s' % (key, actor['username']), AUDIT_LOG)
        self._json(200, {'ok': True})

    # -- Kit editor (requires the `kit_editor` permission) --------------------
    def _editor_session(self):
        token = self._get_token()
        session = _get_session(token) if token else None
        user = _session_user(session)
        if not user:
            self._json(401, {'error': 'Unauthorized'}); return None
        if not _has_perm(user, 'kit_editor'):
            self._json(403, {'error': 'Editor access required'}); return None
        return user

    def _handle_kit_save(self, body):
        session = self._editor_session()
        if not session: return
        data = self._parse(body)
        key = _safe_key(data.get('key', ''))
        if not key: return self._json(400, {'error': 'Missing or invalid kit key'})
        with _lock:
            kits = _load_json(KITS_JSON, {})
            ts   = _load_json(TS_JSON, {})
            if key not in kits:
                return self._json(404, {'error': 'Kit not found: ' + key})
            _snapshot_kit(key, kits, ts)
            fields = data.get('fields', {}) or {}
            k = kits[key]
            for f in ('vehicle', 'switch_button', 'audio_method', 'notes'):
                if f in fields: k[f] = fields[f]
            for f in ('dip_image', 'wiring_image', 'misc_image', 'manual'):
                if f in data: k[f] = data[f]
            kits[key] = k
            if isinstance(data.get('troubleshooting'), list):
                ts[key] = data['troubleshooting']
            saved_at = int(time.time())
            k['updated_at'] = saved_at
            _save_json(KITS_JSON, kits)
            _save_json(TS_JSON, ts)
        _log('KIT SAVE: %s by %s' % (key, session['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'key': key, 'updated_at': saved_at})

    def _handle_kit_delete(self, body):
        session = self._editor_session()
        if not session: return
        data = self._parse(body)
        key = _safe_key(data.get('key', ''))
        if not key: return self._json(400, {'error': 'Missing or invalid kit key'})
        with _lock:
            kits = _load_json(KITS_JSON, {})
            ts   = _load_json(TS_JSON, {})
            if key not in kits:
                return self._json(404, {'error': 'Kit not found: ' + key})
            _snapshot_kit(key, kits, ts)   # keep a recoverable copy in kit_history
            kits.pop(key, None)
            ts.pop(key, None)
            _save_json(KITS_JSON, kits)
            _save_json(TS_JSON, ts)
        _log('KIT DELETE: %s by %s' % (key, session['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'key': key})

    def _handle_kit_new(self, body):
        session = self._editor_session()
        if not session: return
        data = self._parse(body)
        key = _safe_key(data.get('key', ''))
        if not key: return self._json(400, {'error': 'Missing or invalid kit key'})
        with _lock:
            kits = _load_json(KITS_JSON, {})
            ts   = _load_json(TS_JSON, {})
            if key in kits:
                return self._json(409, {'error': 'Kit already exists: ' + key})
            fields = data.get('fields', {}) or {}
            kits[key] = {
                'vehicle':       fields.get('vehicle', ''),
                'switch_button': fields.get('switch_button', ''),
                'audio_method':  fields.get('audio_method', ''),
                'notes':         fields.get('notes', ''),
                'dip_image': '', 'wiring_image': '', 'misc_image': '', 'manual': '',
            }
            ts[key] = []
            _save_json(KITS_JSON, kits)
            _save_json(TS_JSON, ts)
        _log('KIT NEW: %s by %s' % (key, session['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'key': key})

    def _copy_kit_assets(self, src, key, newkit):
        """Copy a kit's image + manual files to a new key and repoint the kit's
        image path fields. The `manual` field is a bare filename, so copying the
        files into the new key's folder is enough -- no rewrite needed."""
        src_img = os.path.join(IMAGES_DIR, src)
        if os.path.isdir(src_img):
            dst_img = os.path.join(IMAGES_DIR, key)
            if not os.path.exists(dst_img):
                os.makedirs(dst_img)
            for fn in os.listdir(src_img):
                try:
                    shutil.copy2(os.path.join(src_img, fn), os.path.join(dst_img, fn))
                except Exception:
                    pass
        for kind in ('dip', 'wiring', 'misc'):
            field = kind + '_image'
            if newkit.get(field):
                newkit[field] = '/images/' + key + '/' + kind + '.jpg'
        src_man = os.path.join(MANUALS_DIR, src)
        if os.path.isdir(src_man):
            dst_man = os.path.join(MANUALS_DIR, key)
            if not os.path.exists(dst_man):
                os.makedirs(dst_man)
            for fn in os.listdir(src_man):
                try:
                    shutil.copy2(os.path.join(src_man, fn), os.path.join(dst_man, fn))
                except Exception:
                    pass

    def _handle_kit_duplicate(self, body):
        """Create a new kit as a full copy of an existing one (fields,
        troubleshooting tree, and image/manual files)."""
        session = self._editor_session()
        if not session: return
        data = self._parse(body)
        src = _safe_key(data.get('source', ''))
        key = _safe_key(data.get('key', ''))
        if not src or not key:
            return self._json(400, {'error': 'Missing or invalid kit key'})
        if src == key:
            return self._json(400, {'error': 'New key must differ from the source'})
        with _lock:
            kits = _load_json(KITS_JSON, {})
            ts   = _load_json(TS_JSON, {})
            if src not in kits:
                return self._json(404, {'error': 'Source kit not found: ' + src})
            if key in kits:
                return self._json(409, {'error': 'Kit already exists: ' + key})
            newkit = copy.deepcopy(kits[src])
            try:
                self._copy_kit_assets(src, key, newkit)
            except Exception as e:
                _log('Duplicate asset copy %s->%s failed: %s' % (src, key, e))
            newkit['updated_at'] = int(time.time())
            kits[key] = newkit
            ts[key] = copy.deepcopy(ts.get(src, []))
            _save_json(KITS_JSON, kits)
            _save_json(TS_JSON, ts)
        _log('KIT DUPLICATE: %s -> %s by %s' % (src, key, session['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'key': key, 'source': src})

    # ── Blueprints: reusable troubleshooting templates (no images/manuals) ──────
    # Stored in blueprints.json keyed by a generated id; `name` is editable and
    # independent of the id. The troubleshooting payload is the same flat
    # [{parent, issue, question, steps[]}] shape a kit uses. Applying a blueprint
    # to a kit is done client-side (merge + normal kit save), so the server only
    # needs CRUD here.
    def _handle_blueprints_list(self):
        session = self._editor_session()
        if not session: return
        bps = _load_json(BLUEPRINTS_JSON, {}) or {}
        self._json(200, {'ok': True, 'blueprints': bps})

    def _handle_blueprint_save(self, body):
        session = self._editor_session()
        if not session: return
        data = self._parse(body)
        bid  = (data.get('id') or '').strip()
        name = (data.get('name') or '').strip()
        if not name:
            return self._json(400, {'error': 'Blueprint name is required'})
        ts = data.get('troubleshooting')
        if not isinstance(ts, list): ts = []
        with _lock:
            bps = _load_json(BLUEPRINTS_JSON, {}) or {}
            now = int(time.time())
            if not bid or bid not in bps:
                if not bid:
                    bid = 'bp_' + _gen_id()
                bps[bid] = {'name': name, 'troubleshooting': ts,
                            'created_at': now, 'updated_at': now}
            else:
                bps[bid]['name'] = name
                bps[bid]['troubleshooting'] = ts
                bps[bid]['updated_at'] = now
            _save_json(BLUEPRINTS_JSON, bps)
            updated_at = bps[bid]['updated_at']
        _log('BLUEPRINT SAVE: %s (%s) by %s' % (bid, name, session['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'id': bid, 'name': name, 'updated_at': updated_at})

    def _handle_blueprint_delete(self, body):
        session = self._editor_session()
        if not session: return
        data = self._parse(body)
        bid = (data.get('id') or '').strip()
        if not bid:
            return self._json(400, {'error': 'Missing blueprint id'})
        with _lock:
            bps = _load_json(BLUEPRINTS_JSON, {}) or {}
            if bid not in bps:
                return self._json(404, {'error': 'Blueprint not found'})
            bps.pop(bid, None)
            _save_json(BLUEPRINTS_JSON, bps)
        _log('BLUEPRINT DELETE: %s by %s' % (bid, session['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'id': bid})

    def _handle_blueprint_duplicate(self, body):
        session = self._editor_session()
        if not session: return
        data = self._parse(body)
        src  = (data.get('id') or '').strip()
        name = (data.get('name') or '').strip()
        if not src or not name:
            return self._json(400, {'error': 'Missing source id or new name'})
        with _lock:
            bps = _load_json(BLUEPRINTS_JSON, {}) or {}
            if src not in bps:
                return self._json(404, {'error': 'Source blueprint not found'})
            now = int(time.time())
            nid = 'bp_' + _gen_id()
            bps[nid] = {'name': name,
                        'troubleshooting': copy.deepcopy(bps[src].get('troubleshooting', [])),
                        'created_at': now, 'updated_at': now}
            _save_json(BLUEPRINTS_JSON, bps)
        _log('BLUEPRINT DUPLICATE: %s -> %s by %s' % (src, nid, session['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'id': nid, 'name': name})

    def _handle_upload(self, body):
        session = self._editor_session()
        if not session: return
        data = self._parse(body)
        key  = _safe_key(data.get('key', ''))
        kind = (data.get('kind', '') or '').lower()
        b64  = data.get('data', '') or ''
        if not key: return self._json(400, {'error': 'Missing or invalid kit key'})
        if kind not in ('manual', 'dip', 'wiring', 'misc'):
            return self._json(400, {'error': 'Invalid kind: ' + kind})
        if b64[:5] == 'data:' and ',' in b64:
            b64 = b64.split(',', 1)[1]
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return self._json(400, {'error': 'Bad base64 data'})
        if not raw: return self._json(400, {'error': 'Empty upload'})
        try:
            if kind == 'manual':
                fname = _safe_filename(data.get('filename', '') or (key + '.pdf'))
                if not fname.lower().endswith('.pdf'): fname += '.pdf'
                dest_dir = os.path.join(MANUALS_DIR, key)
                if not os.path.exists(dest_dir): os.makedirs(dest_dir)
                with open(os.path.join(dest_dir, fname), 'wb') as f: f.write(raw)
                _schedule_auxiliary_sync()
                _log('UPLOAD manual: %s/%s by %s' % (key, fname, session['username']), AUDIT_LOG)
                return self._json(200, {'ok': True, 'kind': kind, 'filename': fname})
            dest_dir = os.path.join(IMAGES_DIR, key)
            if not os.path.exists(dest_dir): os.makedirs(dest_dir)
            with open(os.path.join(dest_dir, kind + '.jpg'), 'wb') as f: f.write(raw)
            _schedule_auxiliary_sync()
            _log('UPLOAD %s: %s by %s' % (kind, key, session['username']), AUDIT_LOG)
            return self._json(200, {'ok': True, 'kind': kind, 'path': '/images/' + key + '/' + kind + '.jpg'})
        except Exception as e:
            return self._json(500, {'error': str(e)})

    def _handle_kit_history(self):
        session = self._editor_session()
        if not session: return
        import urllib
        key = ''
        if '?' in self.path:
            for part in self.path.split('?', 1)[1].split('&'):
                if part.startswith('key='):
                    key = _safe_key(urllib.unquote(part[4:]))
        if not key: return self._json(400, {'error': 'Missing kit key'})
        hist_dir = os.path.join(KIT_HISTORY_DIR, key)
        versions = []
        if os.path.exists(hist_dir):
            for fn in sorted(os.listdir(hist_dir), reverse=True):
                if fn.endswith('.json'): versions.append(fn[:-5])
        self._json(200, {'ok': True, 'key': key, 'versions': versions})

    def _handle_kit_restore(self, body):
        session = self._editor_session()
        if not session: return
        data = self._parse(body)
        key = _safe_key(data.get('key', ''))
        ver = _safe_filename(data.get('version', ''))
        if not key or not ver: return self._json(400, {'error': 'Missing key or version'})
        snap_path = os.path.join(KIT_HISTORY_DIR, key, ver + '.json')
        if not os.path.exists(snap_path):
            return self._json(404, {'error': 'Version not found'})
        with _lock:
            snap = _load_json(snap_path, None)
            if not snap: return self._json(500, {'error': 'Could not read snapshot'})
            kits = _load_json(KITS_JSON, {})
            ts   = _load_json(TS_JSON, {})
            _snapshot_kit(key, kits, ts)
            if snap.get('kit') is not None: kits[key] = snap['kit']
            ts[key] = snap.get('troubleshooting', [])
            _save_json(KITS_JSON, kits)
            _save_json(TS_JSON, ts)
        _log('KIT RESTORE: %s -> %s by %s' % (key, ver, session['username']), AUDIT_LOG)
        self._json(200, {'ok': True, 'key': key, 'restored': ver})

    # -- Helpers --------------------------------------------------------------
    def _serve_file(self, path):
        """Serve a local file from BASE_DIR (read-only, traversal-safe)."""
        # URL decode path (handles spaces as %20 etc.)
        import urllib
        path = urllib.unquote(path)
        safe_path = path.lstrip('/').replace('\\', '/')
        # Resolve to a real absolute path and require it to stay inside BASE_DIR.
        # realpath collapses '..' and symlinks, so e.g. '/../../etc/passwd' can't
        # escape the share (the old startswith check on the un-normalized path could
        # be defeated by '..' sequences).
        base_real = os.path.realpath(BASE_DIR)
        full_path = os.path.realpath(os.path.join(base_real, safe_path))
        if full_path != base_real and not full_path.startswith(base_real + os.sep):
            return self._json(403, {'error': 'Forbidden'})
        # Block sensitive files/dirs from static serving. These hold passwords,
        # sessions, secrets and ticket data; they are reachable only through the
        # auth-gated API endpoints, never as plain files.
        rel = os.path.relpath(full_path, base_real).replace(os.sep, '/').lower()
        base = os.path.basename(rel)
        if base in ('credentials.json', 'sessions.json', 'config.json',
                    'stats.json', 'analytics.json', 'pending_writes.json',
                    'dead_letters.json', 'login_fails.json', 'roles.json') \
           or rel == '.' \
           or rel.startswith('backups/') or rel.startswith('drafts/') \
           or rel.startswith('logs/') or rel.startswith('data/kit_history/'):
            return self._json(403, {'error': 'Forbidden'})
        if not os.path.isfile(full_path):
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        ext = os.path.splitext(full_path)[1].lower()
        is_html = ext in ('.html', '.htm')
        mime_types = {
            '.html': 'text/html; charset=utf-8',
            '.htm':  'text/html; charset=utf-8',
            '.css':  'text/css',
            '.js':   'application/javascript',
            '.jpg':  'image/jpeg', '.jpeg': 'image/jpeg',
            '.png':  'image/png', '.gif': 'image/gif',
            '.pdf':  'application/pdf', '.json': 'application/json',
        }
        mime = mime_types.get(ext, 'application/octet-stream')
        try:
            mtime = int(os.path.getmtime(full_path))
        except Exception:
            mtime = None
        # Revalidation 304 only for cacheable static assets. HTML is intentionally
        # never revalidated here: deploys often preserve the file's mtime, which
        # would make this answer "304 Not Modified" and serve a stale page. HTML
        # is sent fresh every time (see no-store below) so edits always show.
        if mtime is not None and not is_html:
            ims = self.headers.get('If-Modified-Since')
            if ims:
                try:
                    import email.utils
                    tup = email.utils.parsedate_tz(ims)
                    if tup and email.utils.mktime_tz(tup) >= mtime:
                        self.send_response(304)
                        self._cors()
                        self.end_headers()
                        return
                except Exception:
                    pass
        # HTML: revalidate with a CONTENT-based ETag (md5 of the file bytes), not
        # the mtime. This keeps pages fresh after a deploy even when the deploy
        # preserves timestamps, while letting the browser reuse its cached copy
        # (a cheap 304 instead of re-downloading the whole page every load).
        if is_html:
            try:
                with open(full_path, 'rb') as f:
                    data = f.read()
            except Exception as e:
                return self._json(500, {'error': str(e)})
            etag = 'W/"' + hashlib.md5(data).hexdigest() + '"'
            inm = self.headers.get('If-None-Match')
            if inm and etag in [t.strip() for t in inm.split(',')]:
                self.send_response(304)
                self._cors()
                self.send_header('ETag', etag)
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                return
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', mime)
            enc = None
            if self._accepts_gzip() and len(data) > 1024:
                gz = self._gz(data)
                if gz is not None and len(gz) < len(data):
                    data = gz; enc = 'gzip'
            if enc:
                self.send_header('Content-Encoding', enc)
                self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('ETag', etag)
            # no-cache = the browser may cache the page but must revalidate each
            # load; the ETag makes that revalidation a tiny 304 when unchanged.
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
            return
        try:
            with open(full_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', mime)
            enc = None
            if ext in ('.js', '.css', '.json') and self._accepts_gzip() and len(data) > 1024:
                gz = self._gz(data)
                if gz is not None and len(gz) < len(data):
                    data = gz; enc = 'gzip'
            if enc:
                self.send_header('Content-Encoding', enc)
                self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(data)))
            # Static assets can sit in cache for an hour.
            self.send_header('Cache-Control', 'max-age=3600')
            if mtime is not None:
                self.send_header('Last-Modified', self.date_time_string(mtime))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json(500, {'error': str(e)})

    def _read_body(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b''
        if length > MAX_BODY:
            # Refuse oversized bodies instead of buffering them (memory-DoS guard).
            self._json(413, {'error': 'Request too large'})
            return None
        return self.rfile.read(length)

    def _parse(self, body):
        try:
            return json.loads(body)
        except:
            return {}

    def _get_token(self):
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:]
        # Also check query string
        if '?' in self.path:
            qs = self.path.split('?', 1)[1]
            for part in qs.split('&'):
                if part.startswith('token='):
                    return part[6:]
        return None

    def _json(self, code, obj):
        body = json.dumps(obj)
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('X-ZZ2-Build', ZZ2_BUILD)

    def log_message(self, fmt, *args):
        pass  # Suppress request logs to keep things clean

class ThreadedServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    pass

if __name__ == '__main__':
    import sys
    _ARGV0[0] = os.path.abspath(sys.argv[0])   # resolve before chdir, for restart
    _ensure_dirs()
    os.chdir(BASE_DIR)

    # Generate a PBKDF2 hash for a technician password, so credentials.json can
    # store `password_hash` instead of plaintext `password`:
    #   python server.py --hash-password
    if '--hash-password' in sys.argv:
        import getpass
        pw = getpass.getpass('New password: ')
        if pw != getpass.getpass('Confirm:      '):
            print('Passwords do not match.'); sys.exit(1)
        print('')
        print(_hash_password(pw))
        print('')
        print('In credentials.json, set this as the technician\'s "password_hash"')
        print('and delete their "password" field. Plaintext still works until you do.')
        sys.exit(0)

    # One-shot migration: pull all tickets from Supabase, write the store, exit.
    #   python server.py --rebuild-analytics
    if '--rebuild-analytics' in sys.argv:
        print('Rebuilding analytics store from Supabase...')
        ok, info = _rebuild_analytics()
        if ok:
            print('OK: %s tickets stored in %s' % (info, ANALYTICS_FILE))
            sys.exit(0)
        print('FAILED: %s' % info)
        sys.exit(1)

    # Seed on first run. Also migrate old sheet-backed stores once: tickets.html
    # now reads Supabase directly, so retaining the former source can produce a
    # tiny/stale Analytics count even though the Tickets page has thousands.
    _startup_store = _load_json(ANALYTICS_FILE, {}) if os.path.exists(ANALYTICS_FILE) else {}
    if _startup_store.get('source') != 'supabase':
        def _seed():
            time.sleep(2)
            ok, info = _rebuild_analytics()
            _log('Startup analytics seed: ok=%s (%s)' % (ok, info))
        st = threading.Thread(target=_seed); st.daemon = True; st.start()

    # Build the precomputed analytics view in the background so the Analytics
    # page loads instantly (it serves this instead of aggregating ~6k tickets in
    # the browser). Refreshed nightly and on manual Rebuild.
    def _seed_view():
        time.sleep(4)
        _ensure_analytics_view()
        # warm the ticket-number high-water mark so the very first save is instant
        try:
            if os.path.exists(ANALYTICS_FILE):
                with _lock:
                    _bump_reserved(_analytics_store())
        except Exception as e:
            _log('reserve-counter warm failed: %s' % e)
    vt = threading.Thread(target=_seed_view, name='analytics_view'); vt.daemon = True; vt.start()

    # Start the serialized store-mirror worker (ticket create/edit/delete mirror
    # the local store off the request path, in submission order).
    _mirror_running[0] = True
    mw = threading.Thread(target=_mirror_worker, name='store_mirror'); mw.daemon = True; mw.start()

    # Start scheduler thread (nightly backup + analytics rebuild)
    t = threading.Thread(target=_scheduler, name='scheduler')
    t.daemon = True
    t.start()

    # Start the new-ticket poller (catches tickets typed straight into the sheet)
    pt = threading.Thread(target=_poll_loop, name='poller')
    pt.daemon = True
    pt.start()

    # Start the write-behind worker (pushes queued ticket saves to the sheet)
    wb = threading.Thread(target=_write_behind_loop, name='write_behind')
    wb.daemon = True
    wb.start()

    # Migrate any legacy plaintext passwords to PBKDF2 in the BACKGROUND. Doing
    # it inline blocked startup while the NAS hashed each account (PBKDF2 is slow
    # on its CPU), so the whole site appeared dead for the first minute after a
    # restart. Plaintext logins keep working until each account is upgraded, so
    # backgrounding this is safe.
    mt = threading.Thread(target=_migrate_plaintext_passwords, name='pw_migrate')
    mt.daemon = True
    mt.start()

    server = ThreadedServer(('0.0.0.0', 8080), ZZ2Handler)

    # Optional TLS: set "tls_cert"/"tls_key" (PEM paths) in config.json to serve
    # HTTPS so tokens and passwords aren't sent in cleartext over WiFi.
    _cfg = _get_config()
    _cert, _key = _cfg.get('tls_cert'), _cfg.get('tls_key')
    if _cert and _key and os.path.exists(_cert) and os.path.exists(_key):
        try:
            import ssl
            try:
                _ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            except AttributeError:
                _ctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
            _ctx.load_cert_chain(certfile=_cert, keyfile=_key)
            server.socket = _ctx.wrap_socket(server.socket, server_side=True)
            _log('ZZ2 Server starting on port 8080 (TLS enabled)')
        except Exception as _e:
            _log('TLS requested but failed (%s); serving plain HTTP' % _e, AUDIT_LOG)
            _log('ZZ2 Server starting on port 8080')
    else:
        _log('ZZ2 Server starting on port 8080')
    # Initial mirror is delayed so startup and the HTTP listener are never held
    # up by Supabase. Later local saves debounce into the same sync path.
    _schedule_auxiliary_sync(5.0)
    # Mark the listening socket close-on-exec so a self-restart (os.execv) cleanly
    # releases port 8080 for the fresh process to rebind.
    try:
        import fcntl
        _fd = server.fileno()
        _fl = fcntl.fcntl(_fd, fcntl.F_GETFD)
        fcntl.fcntl(_fd, fcntl.F_SETFD, _fl | fcntl.FD_CLOEXEC)
    except Exception as _e:
        _log('Could not set FD_CLOEXEC on listen socket: %s' % _e)
    server.serve_forever()
