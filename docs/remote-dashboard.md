# Remote dashboard access

DDoJA can be reached from a phone using a temporary **email-protected Cloudflare
Quick Tunnel**. No VPN client, router port forwarding, Windows service, admin
installation, Cloudflare account or domain is required. The PC must stay awake,
online and running the dashboard and connector.

This is a temporary personal development setup. Quick Tunnels have no uptime
guarantee and their address changes when restarted. A persistent deployment
should use a named Cloudflare Tunnel with Cloudflare Access and a stable domain.

## Access and security

The connector must support `--allowed-mail`. Only one exact owner email is allowed
by the launcher; public mode and wildcard email domains are not supported. Open
the displayed HTTPS URL in your phone browser, enter the authorized email and
complete Cloudflare's emailed-code login. Authentication happens before any
dashboard page, static file, API response or action is forwarded.

Cloudflare handles the public HTTPS connection and email authentication; it is
a trusted intermediary for dashboard traffic. Provider keys stay in DDoJA's
existing Windows-user encrypted storage and are not returned by dashboard APIs.
The authenticated owner can see applicant data and use campaign/inbox controls.
Never put provider keys or the local dashboard token in a URL.

The existing dashboard remains bound to `127.0.0.1:8765`. A separate loopback-only
adapter validates the exact external Origin, translates it for the local
dashboard, strips forwarded credentials and requires the original dashboard
API token on protected APIs. It does not fetch or inject that token for callers.
The connector supplies a generated origin Host; the adapter rejects other Hosts
and stays disabled until its exact public hostname is known. This adapter is
only safe behind the launcher's email-protected connector and must never be
exposed directly.

The connector binary must be downloaded from the official
`cloudflare/cloudflared` GitHub release and verified against the release asset's
SHA-256 digest. The ignored local verification record is
`data/maintenance/remote-dashboard-download.json`; it records `asset`, `sha256`
and `digest_verified`. Each start rechecks the checksum and email-auth flag.

## Start and stop

With the verified connector at `data/bin/cloudflared.exe`:

```powershell
.venv\Scripts\python.exe tools\remote_dashboard.py start --email <your-exact-email>
.venv\Scripts\python.exe tools\remote_dashboard.py status
.venv\Scripts\python.exe tools\remote_dashboard.py stop
```

Run the normal dashboard first. The gateway starts separately and does not
restart the dashboard or change campaign controls, worker sessions or settings.
Background processes use hidden windows. Stop writes a run-scoped shutdown
request to the live supervisor; it does not kill a PID read from an old file.
The supervisor owns its child process handles and closes the connector and
adapter if either exits. This adds no automatic service at login or reboot.

Runtime configuration, URL, exact authorized email, process identity and logs
stay under ignored `data/remote-dashboard/`. Request access logs are disabled;
dashboard response bodies, API keys and authentication cookies are not logged.

Before sharing a new URL, verify anonymously that `/`, `/api/session`,
`/api/settings/workers`, `/api/pipelines` and a static file redirect to
`https://login.trycloudflare.com/`, and that a POST without a login returns 401.
Also verify the loopback adapter's valid-Origin/token path and rejection of a
foreign Origin. The first authenticated phone visit remains a separate check.

References: [Protected Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/#restrict-access-by-email),
[Cloudflare's authentication description](https://blog.cloudflare.com/protected-quick-tunnels/).
