#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="eden"

if podman container exists "$CONTAINER_NAME"; then
  echo "Stopping ${CONTAINER_NAME}..."
  podman stop -t 10 "$CONTAINER_NAME"
  podman rm "$CONTAINER_NAME"
  echo "✓ ${CONTAINER_NAME} stopped and removed."
else
  echo "${CONTAINER_NAME} is not running."
fi
