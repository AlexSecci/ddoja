# Remote dashboard

The remote gateway has one owner account with a username and password. Cloudflare
Quick Tunnel supplies free HTTPS transport; it does not handle authentication.
No Cloudflare Access, email codes, VPN client, domain purchase or Windows admin
installation is required. The PC must remain awake and online.

## First use

Start the normal dashboard, then run:

```powershell
.venv\Scripts\python.exe tools\remote_dashboard.py start
```

The first start prints a private setup URL. Open it directly in Safari or Chrome
and choose the owner username and a password/passphrase of at least 15 characters.
This setup link expires after 24 hours and can create exactly one account once.
Its random secret is in the URL fragment, not an HTTP path/query, and is removed
from the address bar before submitting the form. Setup cannot overwrite an
existing owner. Do not publish the private setup link.

Future visits use the ordinary dashboard URL and the same credentials. The
browser remembers the login for 90 days, renewed after a day of authenticated
activity. Page refreshes, closing/reopening the browser and gateway/dashboard
restarts do not invalidate a saved login at the same address. Private browsing
or deleting cookies removes the browser's saved login.

Quick Tunnel hostnames are temporary and change when the connector restarts.
A new hostname requires signing in again with the same saved credentials.
Reloading just the gateway keeps its address and login. A stable hostname needs
a named tunnel/custom domain or another persistent HTTPS reverse proxy; the
login itself is independent of the transport. Quick Tunnels have no uptime
guarantee and do not support Server-Sent Events; this dashboard uses polling.

## Management

```powershell
.venv\Scripts\python.exe tools\remote_dashboard.py status
.venv\Scripts\python.exe tools\remote_dashboard.py reload
.venv\Scripts\python.exe tools\remote_dashboard.py stop
```

Reload replaces only the gateway process while retaining the connector, URL and
authentication database. Stop shuts down only this remote-access run. Neither
action restarts the main dashboard, changes campaign controls or stops workers.
Processes run in hidden windows. No automatic login/reboot service is installed.
The supervisor owns its process handles and validates creation identity before
stopping its child tree; shutdown never kills a PID loaded from an old file.

To replace an unused setup link, run `setup-link`. This invalidates the previous
setup link and prints a fresh private one. To reset forgotten credentials from
the PC, use the terminal-only command below; it prompts without echoing the
password and revokes all existing remote sessions:

```powershell
.venv\Scripts\python.exe tools\remote_dashboard.py configure --username admin
```

The dashboard's **Sign out** button revokes the current browser session.

## Security boundary

- Every dashboard page, asset, API and artifact download is gated by the owner
  session. Only the sign-in/setup page, their two assets and a minimal login
  status endpoint are available before authentication. APIs return JSON 401
  rather than silently redirecting a fetch into a third-party login page.
- Passwords use Argon2id (64 MiB, three iterations, parallelism two). The local
  database stores only the password hash and hashes of random session bearers.
  Raw passwords and session cookies are never logged or returned by status APIs.
- The session cookie is host-bound, `Secure`, `HttpOnly`, `SameSite=Lax`, and has
  a 90-day lifetime. Sessions persist in `data/secrets/dashboard-auth.sqlite`.
  Logging out or resetting credentials revokes the relevant saved sessions.
- Login/setup attempts have persistent per-client and global rate limits, a
  small concurrency cap for password hashing, and bounded request sizes.
- State-changing requests require the exact HTTPS Origin; login/setup require
  JSON. Sign-out also requires a session-bound CSRF token. Forged forwarded
  headers and gateway cookies are stripped before reaching the local app.
- The adapter listens only on loopback, accepts only its configured connector
  Host, and stays disabled until its exact HTTPS origin is known. It retains
  the local API's existing token guard and scoped application approvals. The UI
  refreshes an expired local API token once only after the exact middleware
  rejection that proves no action executed; ambiguous network sends are not
  retried. Password login does not authorize submissions or inbox decisions.
- Cloudflare is the HTTPS transport intermediary and can process dashboard
  traffic. Provider API keys remain in their existing Windows-user encrypted
  local storage and are not returned by dashboard APIs. The original app stays
  bound to `127.0.0.1:8765`; local worker access is unchanged.

Runtime configuration, process identities and logs are ignored under
`data/remote-dashboard/`. The optional unconsumed setup token is stored with
Windows user-bound encryption in `data/secrets/dashboard-setup.dpapi`.
Request access logs are disabled, and login responses use no-store and no-referrer.

The connector at `data/bin/cloudflared.exe` must match an official
`cloudflare/cloudflared` GitHub release asset's SHA-256 digest, recorded in
`data/maintenance/remote-dashboard-download.json`. Every start rechecks it.
No billing account or paid transport feature is used.

## Verification

Unit tests cover one-time setup, password hashing, persistent/renewed sessions,
reset/logout revocation, concurrent setup, rate limits, Origin/CSRF protection,
anonymous route denial and the existing API-token forwarding guard.

The opt-in live check creates a separate disposable owner behind a separate
HTTPS tunnel, with an upstream transport that refuses every method except
GET/HEAD. It uses an actual mobile-sized browser to test setup/login, pipeline
cards, worker details, job records, filters, refreshes, cookie persistence across
gateway/browser restart, local-token refresh and logout/login. It cannot change
real campaign controls or make application actions:

```powershell
.venv\Scripts\python.exe tools\remote_dashboard_smoke.py --run-live-check
```

References: [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/),
[OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[MDN secure cookies](https://developer.mozilla.org/en-US/docs/Web/Security/Practical_implementation_guides/Cookies).
