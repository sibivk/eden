#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="eden"
IMAGE_NAME="eden:latest"
ENV_FILE=".env"

# ── Preflight checks ──────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.example to .env and fill in your credentials."
  exit 1
fi

# Check available disk space (warn if < 2GB free on the relevant filesystem)
AVAIL_KB=$(df --output=avail . | tail -1)
if [ "$AVAIL_KB" -lt 2097152 ]; then
  echo "WARNING: Less than 2 GB free on this filesystem. Consider cleaning up before building."
fi

# ── Build image ───────────────────────────────────────────
echo "Building ${IMAGE_NAME}..."
podman build -t "$IMAGE_NAME" -f Containerfile .

# ── Cleanup dangling eden images ─────────────────────────
echo "Cleaning up dangling images..."
podman image prune -f --filter label=project=eden 2>/dev/null || true
podman images --format "{{.ID}} {{.Repository}}:{{.Tag}}" \
  | awk '/eden:/ && !/:latest/' \
  | awk '{print $1}' \
  | xargs -r podman rmi -f 2>/dev/null || true

# ── Create local directories ─────────────────────────────
mkdir -p ./logs ./data

# ── Reload quadlet and restart service ───────────────────
# The container is managed by systemd (Podman Quadlet).
# Reloading the daemon picks up any quadlet changes; restart
# stops the running instance (if any) before starting fresh.
echo "Reloading systemd and restarting ${CONTAINER_NAME} service..."
systemctl --user daemon-reload
systemctl --user restart "${CONTAINER_NAME}.service" || \
  systemctl --user start "${CONTAINER_NAME}.service"

echo ""
echo "✓ ${CONTAINER_NAME} service (re)started via systemd."
echo "  → http://localhost:5000"
echo ""
systemctl --user status "${CONTAINER_NAME}.service" --no-pager -l | head -20
