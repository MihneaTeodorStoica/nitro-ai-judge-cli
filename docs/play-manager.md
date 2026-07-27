# Nitro Play manager architecture

## Boundary

NAIJ 3.0 has one host-side Docker boundary: installing, updating, starting, or
uninstalling the manager container. Competition commands and the Textual TUI
call the authenticated manager API and never invoke Docker for competition
state. The manager mounts the selected local daemon socket and includes the
Docker CLI plus Compose plugin. It is not Docker-in-Docker.

The default endpoint is `http://localhost:51123/nitro/`. Jupyter and the
submission proxy retain internal ports 8888 and 9000 but are not published.
The manager joins their shared external `naij-play` network and forwards:

- `/nitro/competitions/{org}/{competition}/jupyter/...` unchanged to Jupyter.
- `/nitro/competitions/{org}/{competition}/proxy/...` with only the stable
  `/proxy/` prefix removed.

The proxy supports request and response headers, redirects, cookies, REST,
terminals, and bidirectional WebSockets. Jupyter receives a manager-owned
configuration volume with its full stable base URL, forwarded-header trust,
and remote access enabled. The notebook image's entrypoint remains unchanged.

## Host state and manager state

Private host configuration lives in `~/.naij/play-manager/` (or the selected
NAIJ state root):

- `manager.json`: atomic non-secret endpoint and image configuration.
- `compose.json`: JSON-compatible Compose configuration.
- `cli-api-token`: mode-0600 bearer credential exposed as a Compose secret.
- `dashboard-login-token`: mode-0600, present only for LAN mode.

Operational metadata, normalized Nitro credentials, adoption records,
operation events, and competition snapshots live transactionally in SQLite in
the private `naij-play-manager-state` volume. Manager restarts mark incomplete
operations interrupted, then reconstruct runtime truth from live Docker labels.
An explicit user stop is persisted and never auto-started.

## Runtime objects and ownership

Each competition has two services, a private project network, the shared Nitro
network, a `/home/jovyan` workspace volume, a proxy secret volume, and a
Jupyter-config volume. Unique aliases avoid collisions on the shared network.
Every new container, network, and volume has
`org.nitro-ai.naij.play.*` labels for owner, competition identity, role,
schema, API, and workspace. Destructive operations verify those labels first.
A verified adoption record is the sole exception for a legacy unlabeled
workspace volume, because Docker cannot add labels to an existing volume.

Compose files are generated and submitted with argument arrays. User values
never enter shell command strings. The fixed volume-population helper receives
content through stdin. Image names remain:

- `nitroai/{org}-{competition}-notebook:latest`
- `nitroai/{org}-{competition}-judge-proxy:latest`

GPU auto-detection, explicit require/disable modes, pull policies, proxy
environment variables, service capabilities, timeouts, and `/home/jovyan`
persistence remain part of the competition contract.

## Operations and API

Competition slugs must already be canonical lowercase Nitro slugs; invalid,
reserved, or ambiguous values are rejected. Every long action returns HTTP 202
and an operation UUID. Per-competition locking reuses identical active
operations and returns `competition_busy` for conflicts. SQLite retains
sequenced stage messages and bounded redacted failure logs.

API v1 exposes manager info/health, competition lists/details/images/open
routes, actions, logs and log following, operation snapshots/cancellation, and
credential synchronization. `GET /nitro/api/v1/events` streams a `sync` event
on connection, coalesced `refresh` invalidations, and 15-second keepalives.
`GET /nitro/api/v1/competitions?cached=true` returns SQLite snapshots without
Docker discovery or Nitro requests. Info and health are public. All other API
routes require the private CLI bearer or a browser session.

The dashboard uses one event stream after its first load and silently fetches
the cached snapshot after invalidation. Manual Refresh remains the full Docker
and Nitro reconciliation. The manager also supervises `docker events` for
owned containers, networks, volumes, and expected image tags, coalesces event
bursts, and writes only semantic state changes.

## Security

Loopback mode validates Host, issues HttpOnly SameSite=Strict browser sessions,
requires CSRF plus same-origin checks for browser mutations, and emits CSP,
frame denial, MIME-sniffing, and referrer protections. API credentials are
never accepted in URLs. Sensitive headers and token-like log fields are
redacted before streaming or persistence.

Non-loopback binding additionally requires a TLS certificate, TLS key, and
absolute HTTPS public URL. It enables a separate dashboard login token and
per-peer login rate limiting. Installation prints an exposure warning.

**Disconnect Nitro** deletes only credentials stored in manager SQLite and
leaves CLI credentials and workspaces untouched. LAN-only `POST
/nitro/api/v1/logout` is CSRF-protected, expires only the requesting browser's
session cookie, and leaves other browser sessions active. Expired sessions and
invalid dashboard tokens return the token login page with a visible error.

## Installation, update, and recovery

Installation validates the active Docker context and Linux-container mode,
rejects remote SSH/TCP endpoints, checks the selected port's identity, pulls
only a missing image, writes configuration atomically, starts the manager,
waits for health, verifies identity/API/minimum CLI compatibility, synchronizes
credentials, and submits read-only legacy discovery.

An update saves the prior image and Compose bytes. If the replacement is not
healthy, it restores and starts the prior configuration. Uninstall runs Compose
down without deleting the private state volume, competition metadata, or
workspaces. Purge requires explicit force and removes only the labeled
manager-private SQLite volume.

Before command-line Play use, TUI launch, install, start, or restart, an older
exact-version image from the official manager repository is updated through
that rollback path. Current, newer, digest-pinned, local, and custom images are
left unchanged.

Legacy discovery examines only the known generated state root, whitelisted
`.env` fields, exact Compose project/service labels, and inspected mounts. The
manager lazily stops a verified old container on first use. It reuses an
existing volume unchanged or copies container-layer data into a labeled volume,
starts the separately named manager project, validates both stable routes, and
only then removes the old container/network. Failure removes newly labeled
objects and restarts the old container.

## Verification boundaries

Ordinary tests use fake manager clients/backends and `aiohttp` test utilities.
The reverse-proxy tests exercise prefixed HTTP, stripped proxy paths, and
bidirectional WebSockets against local test services. Opt-in Linux Docker tests
build and install the manager, verify its health and socket/secret wiring,
restart it, and verify uninstall preservation. Production Nitro images,
workspace persistence across real notebook recreation, legacy cutover, and
Docker Desktop behavior require separate environment-backed smoke tests; a
unit or local fake-service pass is not evidence for those contracts.
