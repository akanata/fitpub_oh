# FitPub packaged for OpenHost.
#
# OpenHost runs one rootless-podman container per app, so everything
# FitPub's docker-compose splits into services has to live in this
# single image:
#
#   * FitPub itself      — inherited from the upstream image (Alpine,
#                          app at /app/fitpub.jar, user `fitpub` 1001)
#   * PostgreSQL+PostGIS — from Alpine's packages; replaces the
#                          postgis/postgis compose sidecar
#   * MailPit            — local SMTP sink for registration codes;
#                          static Go binary from upstream releases
#   * auth_proxy.py      — hybrid gate: federation + web UI public,
#                          /admin, /actuator and /mailpit owner-only
#
# Base image: `nightly` (tracks FitPub main), NOT `latest`. The 1.1.x
# release line predates the admin UI (/admin, FITPUB_ADMIN_EMAILS) and
# the Basic-auth actuator chain, both of which this wrapper relies on.
# Switch back to a release label once one ships with those features.
# NOTE: Flyway migrations are one-way — once nightly has migrated the
# database, rolling back to an older image is not supported.
ARG FITPUB_IMAGE_LABEL=nightly
FROM codeberg.org/fitpub/fitpub:${FITPUB_IMAGE_LABEL}

# The upstream image drops to USER fitpub (uid 1001); we need root to
# install packages and to run start.sh, which chowns persistent dirs
# and drops privileges per-service (postgres → postgres, fitpub jar
# and mailpit → fitpub).
USER root

# postgis pulls in the matching postgresql server version; -contrib
# and -client complete initdb/psql. su-exec is the privilege-drop
# helper (Alpine's gosu); bash is required by start.sh.
RUN apk add --no-cache \
        bash \
        postgis \
        postgresql \
        postgresql-contrib \
        python3 \
        tini \
        curl \
        su-exec \
        ca-certificates

# MailPit is a single static binary; Alpine doesn't package it.
# Pinned version — bump deliberately.
ARG MAILPIT_VERSION=v1.30.3
RUN curl -fsSL "https://github.com/axllent/mailpit/releases/download/${MAILPIT_VERSION}/mailpit-linux-amd64.tar.gz" \
    | tar -xz -C /usr/local/bin mailpit \
 && chmod 755 /usr/local/bin/mailpit

COPY --chmod=755 start.sh      /opt/openhost-fitpub/start.sh
COPY --chmod=644 auth_proxy.py /opt/openhost-fitpub/auth_proxy.py

# OpenHost-routed port (the gate-proxy). FitPub (8081), PostgreSQL
# (5432) and MailPit (1025/8025) stay loopback-only.
EXPOSE 8080

ENTRYPOINT ["/sbin/tini", "--", "/opt/openhost-fitpub/start.sh"]
