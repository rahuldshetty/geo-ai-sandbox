#!/usr/bin/env bash
# Build the Spatial Intelligence AppImage via the manylinux build container
# (Route B).
#
# Usage: packaging/build-appimage.sh [output-dir] [extra docker build args...]
#   (output-dir defaults to dist)
#
# Requires: docker with BuildKit (docker buildx or docker >= 19.03).
#
# Examples:
#   packaging/build-appimage.sh
#   packaging/build-appimage.sh dist --build-arg BASE_IMAGE=rockylinux:8
#   packaging/build-appimage.sh dist --build-arg BASE_IMAGE=quay.io/pypa/manylinux_2_28_x86_64:2026.09.05-1
set -euo pipefail

cd "$(dirname "$0")/.."

DIST="${1:-dist}"
shift || true
mkdir -p "$DIST"

exec docker build -f packaging/Dockerfile.build --output "$DIST" "$@" .
