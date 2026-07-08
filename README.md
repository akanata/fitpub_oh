# fitpub_oh — FitPub packaged for OpenHost

Wraps [FitPub](https://codeberg.org/fitpub/fitpub) (a self-hosted,
ActivityPub-federated fitness tracker) as a single-container
[OpenHost](https://aaronw.selfhost.imbue.com/docs/creating_an_app) app.

FitPub normally deploys as a docker-compose stack (Spring Boot app +
PostgreSQL/PostGIS + an external SMTP service). OpenHost runs one
rootless-podman container per app, so this wrapper bundles everything:

```
browser / fediverse peer
  → OpenHost router  (TLS; stamps X-OpenHost-Is-Owner on owner requests)
  → :8080  auth_proxy.py — hybrid gate (the only routed port)
  ├→ 127.0.0.1:8081  FitPub (Spring Boot, prod profile)
  ├→ 127.0.0.1:8025  MailPit web UI (owner-only, served at /mailpit)
  └  127.0.0.1:5432  PostgreSQL + PostGIS  ← FitPub
     127.0.0.1:1025  MailPit SMTP          ← registration emails
```

## Access model (hybrid)

Federation only works if remote servers can reach webfinger / nodeinfo /
actor / inbox endpoints anonymously, so the manifest declares
`public_paths = ["/"]` and [auth_proxy.py](auth_proxy.py) enforces the
owner-only carve-outs itself using the `X-OpenHost-Is-Owner` header the
router stamps on authenticated owner requests:

| Path prefix  | Anonymous / fediverse | Zone owner |
|---|---|---|
| everything else | ✓ pass-through (FitPub's own JWT auth applies) | ✓ |
| `/admin` | 403 | ✓ forwarded (FitPub's ADMIN role still required) |
| `/actuator` | 403 | ✓ forwarded, Basic credentials auto-injected |
| `/mailpit` | 403 | ✓ MailPit inbox UI |
| `/api/debug` | 404 for everyone — see note in [auth_proxy.py](auth_proxy.py) |
| `/healthz` | ✓ answered by the proxy for the router's health checks |

## Deploying

From the OpenHost dashboard: **Deploy New App** → this repo's git URL.
The app comes up at `https://fitpub.<your-zone>/`. First boot takes a
couple of minutes (PostgreSQL initdb + Flyway migrations).

### First login

Email never leaves the box — the bundled MailPit catches everything, so
**only someone who can read `/mailpit` (you, the zone owner) can
complete a registration**. That makes signup effectively owner-approved
despite being "open":

1. Visit `https://fitpub.<zone>/register` and sign up (any address
   works — mail lands in MailPit regardless).
2. Open `https://fitpub.<zone>/mailpit` and read the 6-digit
   verification code (codes expire after 15 minutes).
3. Enter the code to activate the account, then log in.

To invite someone else, have them register, then read the code out of
MailPit and pass it to them.

### Admin role

FitPub grants admin to accounts whose email is in
`FITPUB_ADMIN_EMAILS`. The wrapper defaults that to
`admin@fitpub.<zone>` **plus, when exactly one account exists and no
override is configured, that account's email** — so on a single-owner
instance you become admin automatically. The check runs at container
start, after the schema exists: on a brand-new install, register and
then restart the app ("Update and reload" or restart from the
dashboard) to pick up the role. Override the list any time via
`config.env`.

## Configuration

Everything needed is generated or derived automatically:

* **Secrets** (DB password, JWT secret, email secret, actuator
  password) are generated on first boot into
  `$OPENHOST_APP_DATA_DIR/secrets.env` (mode 0600).
* **Domain / base URL** come from `$OPENHOST_APP_NAME` and
  `$OPENHOST_ZONE_DOMAIN`.

To override anything, create `$OPENHOST_APP_DATA_DIR/config.env` — it
is sourced on every boot after the defaults, and any `FITPUB_*`
variable it exports is passed to FitPub. Examples:

```bash
# Send real email through an external provider instead of MailPit
FITPUB_MAIL_HOST=smtp.example.com
FITPUB_MAIL_PORT=587
FITPUB_MAIL_USERNAME=me@example.com
FITPUB_MAIL_PASSWORD=app-password
FITPUB_MAIL_SMTP_AUTH=true
FITPUB_MAIL_STARTTLS_ENABLE=true
FITPUB_MAIL_FROM_ADDRESS=fitpub@example.com

# Require an invite password for registration
FITPUB_REGISTRATION_PASSWORD=friends-only

# Turn federation off entirely
FITPUB_ACTIVITYPUB_ENABLED=false
```

See FitPub's [CONTAINERS.md](https://codeberg.org/fitpub/fitpub/src/branch/main/CONTAINERS.md)
for the full `FITPUB_*` variable list.

## Data layout

Persistent, backed up (`$OPENHOST_APP_DATA_DIR`): `postgres/` (PGDATA),
`uploads/`, `images/`, `logs/`, `mailpit/`, `secrets.env`,
`config.env`. Recreatable cache (`$OPENHOST_APP_TEMP_DIR`): `tiles/`.

## Federation notes

The proxy forwards the **original `Host` header verbatim** to FitPub.
This is load-bearing: ActivityPub HTTP Signatures sign the `host`
header, and FitPub verifies inbound inbox deliveries against the raw
request headers. A proxy that rewrites Host to its loopback upstream
silently breaks all inbound federation — follows report success (the
outbound leg works) but the remote `Accept` and every workout `Create`
get 401'd, so nothing ever arrives.

Verified end-to-end with two wrapper instances federating over a
container network (follow → Accept → workout upload → signed delivery
→ federated timeline), per FitPub's own
`docker-compose.federation-test.yml` topology.

`FITPUB_REMOTE_ACTIVITY_BACKFILL` defaults to `true` here (upstream:
`false`): on startup FitPub re-fetches missing details for remote
activities it already knows about — a cheap repair after federation
outages. Note that following someone does **not** import their
historical workouts; only activities published after the follow are
delivered.

## Caveats

* The wrapper tracks `codeberg.org/fitpub/fitpub:nightly` (daily
  builds of FitPub `main`), because the 1.1.x release line lacks the
  admin UI and the Basic-auth actuator chain. Nightly is a moving,
  pre-release target: "Update and reload" always rebuilds against the
  newest build, and **Flyway migrations are one-way** — once nightly
  has migrated the database, rolling back to an older image is not
  supported. Switch the `FITPUB_IMAGE_LABEL` build arg back to a
  release label once one ships with those features.
* `/healthz` prefers the Basic-auth'd `/actuator/health` probe and
  falls back to fetching the public homepage on images that predate
  the actuator chain.
* The proxy buffers request bodies (256 MiB cap) and rejects chunked
  transfer encoding; FitPub's default per-file upload limit is 50 MB.

## Local smoke test

```bash
docker build -t fitpub-oh:test .
docker run -d --name fitpub-oh-test -m 2g -p 18080:8080 \
  -e OPENHOST_APP_NAME=fitpub -e OPENHOST_ZONE_DOMAIN=test.local \
  -e OPENHOST_APP_DATA_DIR=/data/app_data/fitpub \
  -e OPENHOST_APP_TEMP_DIR=/data/app_temp_data/fitpub \
  fitpub-oh:test
curl http://localhost:18080/healthz            # {"status":"UP"} once booted
curl -H 'X-OpenHost-Is-Owner: true' http://localhost:18080/mailpit/
```

## Files

* [openhost.toml](openhost.toml) — OpenHost manifest
* [Dockerfile](Dockerfile) — FitPub release image + PostgreSQL/PostGIS,
  MailPit, Python
* [start.sh](start.sh) — secrets, initdb, service supervision
* [auth_proxy.py](auth_proxy.py) — hybrid gate-proxy (adapted from
  [openhost-vscode](https://github.com/imbue-openhost/openhost-vscode))
