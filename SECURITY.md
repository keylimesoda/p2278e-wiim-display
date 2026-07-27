# Security policy

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** / private security advisory flow
for this repository rather than opening a public issue. Include the affected
commit, reproduction steps, impact, and any suggested mitigation. Do not include
real tokens, signed media URLs, private track history, or LAN addresses.

## Supported version

Security fixes target the current `main` branch until versioned releases begin.

## Security boundaries

- The maintenance UI is intended for loopback or a trusted home LAN only.
- GET diagnostics are unauthenticated; POST mutations require a bearer token.
- The UI is plain HTTP and must not be exposed to the public internet.
- WiiM/Linkplay and album-art/media inputs are untrusted network data.
- Runtime tokens, health metadata, configuration, caches, and logs must remain
  outside version control.

If you accidentally expose an admin token, stop the admin service, delete
`pi-app/run/admin.token`, rerun the installer to generate a replacement, and
review LAN access/logs.
