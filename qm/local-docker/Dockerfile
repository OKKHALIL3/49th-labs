# Bakes the REV CLIs into the local agent-computer image (the local sandbox backend
# does not install deployment-layer tool files). Rebuild after the tunnel URL changes.
FROM qm-acme-rev-sandbox-local:latest
COPY rev-refit rev-learn rev-pr /usr/local/bin/
RUN chmod 0755 /usr/local/bin/rev-refit /usr/local/bin/rev-learn /usr/local/bin/rev-pr
