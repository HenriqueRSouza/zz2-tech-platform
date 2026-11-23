# Sign in with a university account (SAML + Microsoft Entra ID)

The platform is a SAML 2.0 **service provider** (SP). Microsoft Entra ID (Azure
AD — the directory behind the university's Outlook accounts) is the **identity
provider** (IdP). A user clicks *Sign in with Microsoft*, authenticates at
Microsoft with their university account (including MFA if the university enforces
it), and comes back signed in.

```
Browser            TSP (this server)                     Entra ID
  │ GET /api/auth/saml/login ──▶ AuthnRequest + state cookie
  │ ◀── 302 ───────────────────  login.microsoftonline.com/<tenant>/saml2?SAMLRequest=…
  │ ───────────────────────────────────────────────────────▶ sign in (+ MFA)
  │ ◀──────────────────────────────── HTML form auto-POST, signed SAMLResponse
  │ POST /api/auth/saml/acs ──▶ validate signature, issuer, audience, recipient,
  │                              time window, InResponseTo, cookie, replay;
  │                              domain allowlist; role
  │ ◀── 303 /login.html#sso=<one-time code>
  │ POST /api/auth/saml/exchange {code} ──▶ { token }   (HS256, 8 h, revocable)
  │ → dashboard, Authorization: Bearer <token>
```

## 1. Register the application in Entra ID

Who can do this: an Entra administrator of the tenant (for a university tenant,
usually IT — send them this section). For development you can use a free
Microsoft 365 developer tenant or your own Azure subscription's tenant.

1. **Entra admin center → Enterprise applications → New application → Create
   your own application.** Name: `Tech Support Platform`. Choose *Integrate any
   other application you don't find in the gallery (Non-gallery)*.
2. **Single sign-on → SAML.**
3. **Basic SAML Configuration → Edit** (or *Upload metadata file* with the XML
   from `https://<your-host>/api/auth/saml/metadata`):

   | Field | Value |
   |---|---|
   | Identifier (Entity ID) | `https://<your-host>/api/auth/saml/metadata` |
   | Reply URL (ACS URL) | `https://<your-host>/api/auth/saml/acs` |
   | Sign on URL | `https://<your-host>/api/auth/saml/login` |
   | Relay State, Logout URL | leave empty |

   *Sign on URL* matters: it makes the tile in *My Apps* start an SP-initiated
   sign-in. IdP-initiated (unsolicited) responses are refused on purpose — see
   below.
4. **Attributes & Claims.** The defaults work (NameID = `user.userprincipalname`,
   plus `emailaddress`, `name`, `givenname`, `surname`). Optional: *Add a group
   claim → Security groups* if you want group → role mapping.
5. **SAML Certificates.** Keep *Signing option* at *Sign SAML assertion* (the
   default; *Sign SAML response* and *both* also work) and the algorithm at
   **SHA-256**. Download **Certificate (Base64)**. Leave token encryption **off**.
6. **Set up Tech Support Platform** section: copy **Login URL** and **Microsoft
   Entra Identifier**.
7. **Users and groups → Add user/group**, and under **Properties** set
   *Assignment required?* = **Yes**, so only assigned students/staff can sign in.

## 2. Configure the server

In `/etc/zz2/zz2.env` (see `deploy/.env.example`):

```bash
ZZ2_PUBLIC_URL=https://suporte.example.edu          # must match the URLs above
ZZ2_SAML_ENABLED=true
ZZ2_SAML_IDP_ENTITY_ID=https://sts.windows.net/<tenant-id>/       # Microsoft Entra Identifier
ZZ2_SAML_IDP_SSO_URL=https://login.microsoftonline.com/<tenant-id>/saml2   # Login URL
ZZ2_SAML_IDP_X509_CERT=MIIC8DCCAdigAwIBAgIQ...      # body of the Base64 certificate
ZZ2_SAML_ALLOWED_DOMAINS=mackenzista.com.br,mackenzie.br
ZZ2_SAML_DEFAULT_ROLE=tech
ZZ2_SAML_SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
```

Restart (`sudo systemctl restart zz2`). The process refuses to start if SAML is
enabled but half-configured. The sign-in page then shows **Sign in with Microsoft
(university account)**.

Local development: the state cookie is `Secure; SameSite=None`, which browsers
accept on `http://localhost`. Entra requires the Reply URL to be `https` except
for `http://localhost`, so register `http://localhost:8080/api/auth/saml/acs` as
an additional Reply URL in a test app and set `ZZ2_PUBLIC_URL=http://localhost:8080`.

## 3. What a SAML user is allowed to do

1. The email domain must be in `ZZ2_SAML_ALLOWED_DOMAINS`.
2. If an account with the same email exists in Supabase (Users page), its role
   and extra permissions apply — and if it is **deactivated, the sign-in is
   refused**.
3. Otherwise `ZZ2_SAML_GROUP_ROLE_MAP` (`<group-object-id>=<role>,…`; the
   highest-ranked match wins), otherwise `ZZ2_SAML_DEFAULT_ROLE`.

The role is fixed when the session is issued; a change applies at the next
sign-in (at most `ZZ2_SAML_SESSION_TTL_SECONDS`, 8 h by default).

## 4. Security properties

| Check | Attack it stops |
|---|---|
| XML signature verified against the configured certificate (signxml, SHA-1 refused) | Forged assertions |
| Only the element the signature covers is read; exactly one `Assertion`, a direct child of `Response` | XML signature wrapping |
| DTDs/entities refused, parser with no network | XXE, entity expansion |
| `Issuer`, `Audience`, `Recipient`/`Destination`, `NotBefore`/`NotOnOrAfter` (±120 s) | Assertions from another tenant or for another app, stale assertions |
| `InResponseTo` must match a request we issued, consumed once | Unsolicited responses, replay |
| Browser-binding cookie (hash kept server-side) | Login CSRF — an attacker's valid response posted from a victim's browser |
| Assertion ID cache | Replay within the validity window |
| Code handed over in the URL fragment, single use, 60 s | Token in logs / `Referer` |
| Own HS256 key, different from Supabase's; revoked on sign-out | Cross-issuer token confusion, use of a copied token after sign-out |
| Failed responses count towards the brute-force lockout | Repeated forging attempts |

Each rejection is written to the audit log as `saml.rejected` with a machine
reason (`signature_invalid`, `audience_mismatch`, `unknown_or_reused_request`, …);
the browser only sees a generic message.

## 5. Limitations

* **Tickets spreadsheet page** (`tickets.html`) reads Supabase directly from the
  browser and therefore needs a Supabase session; a Microsoft-only user sees a
  message there. Every other page works. Removing this needs either Supabase's
  own SAML SSO (paid plan, `supabase sso add`) or moving that page to the
  server API.
* Pending requests, revocations and the replay cache are in memory: correct for
  this deployment's single process. A restart signs nobody out, but forgets
  revocations (a revoked token would work again until it expires) and cancels
  sign-ins in flight.
* Single Logout (SLO) is not implemented: signing out of TSP does not sign the
  user out of Microsoft.
* Encrypted assertions are not supported.

## 6. Troubleshooting

| Symptom | Look at |
|---|---|
| Microsoft shows `AADSTS50011` | Reply URL in Entra ≠ `<ZZ2_PUBLIC_URL>/api/auth/saml/acs` |
| Microsoft shows `AADSTS700016` | Identifier in Entra ≠ `ZZ2_SAML_SP_ENTITY_ID` (default `<ZZ2_PUBLIC_URL>/api/auth/saml/metadata`) |
| Back on sign-in page with "could not be completed" | `grep saml.rejected /var/lib/zz2/logs/audit.log` — the `reason` names the check |
| `browser_binding_mismatch` | The sign-in started in another browser/tab profile, or cookies are blocked |
| `signature_invalid` | Certificate rotated in Entra; paste the new one |
| "not allowed to sign in" | `saml.denied`: domain not in the allowlist, or the matching account is deactivated |
