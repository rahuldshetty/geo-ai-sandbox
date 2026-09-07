#!/bin/sh
# AppImage wrapper: exec the PyInstaller one-dir bundle installed under
# usr/lib/geo-ai, keeping usr/bin/geo-ai a real executable for Exec=.
exec "$(dirname "$(readlink -f "$0")")"/../lib/geo-ai/geo-ai "$@"
