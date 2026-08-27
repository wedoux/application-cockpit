#!/bin/bash
# Compiles ApplicationCockpit.swift into a universal (Apple Silicon + Intel)
# binary and installs it as the .app bundle's executable. Run this after
# editing the .swift source, or on first clone if the committed binary
# doesn't run on your Mac's architecture (e.g. Christophe testing on a
# different chip than it was built on).
set -e
cd "$(dirname "$0")"

APP="../Application Cockpit.app"
BIN="$APP/Contents/MacOS/ApplicationCockpit"

swiftc -O -target arm64-apple-macosx11.0 ApplicationCockpit.swift -o /tmp/ApplicationCockpit-arm64
swiftc -O -target x86_64-apple-macosx11.0 ApplicationCockpit.swift -o /tmp/ApplicationCockpit-x86_64
lipo -create /tmp/ApplicationCockpit-arm64 /tmp/ApplicationCockpit-x86_64 -output "$BIN"
rm -f /tmp/ApplicationCockpit-arm64 /tmp/ApplicationCockpit-x86_64
chmod +x "$BIN"

echo "Built universal binary -> $BIN"
lipo -info "$BIN"
