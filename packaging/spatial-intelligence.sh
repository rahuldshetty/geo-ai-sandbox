#!/bin/sh
# AppImage wrapper: exec the PyInstaller one-dir bundle installed under
# usr/lib/spatial-intelligence, keeping usr/bin/spatial-intelligence a real
# executable for Exec=.
exec "$(dirname "$(readlink -f "$0")")"/../lib/spatial-intelligence/spatial-intelligence "$@"
