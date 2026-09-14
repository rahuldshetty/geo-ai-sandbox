#!/usr/bin/env bash
# Build the Spatial Intelligence AppImage via the manylinux build container
# (Route B).
#
# Usage: packaging/build-appimage.sh [output-dir]   (default: dist)
# Requires: docker with BuildKit (docker buildx or docker >= 19.03).
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p "${1:-dist}"
exec docker build -f packaging/Dockerfile.build --output "${1:-dist}" .
