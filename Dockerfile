# FitPub packaged for OpenHost.
#
# OpenHost runs one rootless-podman container per app, so everything
# FitPub's docker-compose splits into services has to live in this
# single image:
#
#   * FitPub itself      — inherited from the upstream release image
#                          (Ubuntu-based eclipse-temurin JRE, app at
#                          /app/fitpub.jar, user `fitpub` uid 1001)
#   * PostgreSQL+PostGIS — from Ubuntu's packages; replaces the
#                          postgis/postgis compose sidecar
#   * MailPit            — local SMTP sink for registration codes;
#                          static Go binary from upstream releases
#   * auth_proxy.py      — hybrid gate: federation + web UI public,
#                          /admin, /actuator and /mailpit owner-only
#
# start.sh supervises the four processes; tini reaps zombies and
# forwards SIGTERM (same supervision model as openhost-vscode).

FROM codeberg.org/fitpub/fitpub:latest

# The upstream image drops to USER fitpub (uid 1001); we need root to
# install packages and to run start.sh, which chowns persistent dirs
# and drops privileges per-service (postgres → postgres, fitpub jar
# and mailpit → fitpub).
USER root

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
        postgresql \
        postgresql-postgis \
        python3 \
        tini \
        curl \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# MailPit is a single static binary; Ubuntu doesn't package it.
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

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/openhost-fitpub/start.sh"]
