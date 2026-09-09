#!/bin/bash
# Compiles ApplicationCockpit.swift into a universal (Apple Silicon + Intel)
# binary and installs it as the .app bundle's executable. Run this after
# editing the .swift source, and on first clone — the binary is not
# committed, so the .app bundle has no executable until you run this.
set -e
cd "$(dirname "$0")"

APP="../Application Cockpit.app"
BIN="$APP/Contents/MacOS/ApplicationCockpit"

# git tracks no empty directories, and the binary itself is gitignored, so
# Contents/MacOS/ does not exist on a fresh clone.
mkdir -p "$(dirname "$BIN")"

swiftc -O -target arm64-apple-macosx11.0 ApplicationCockpit.swift -o /tmp/ApplicationCockpit-arm64
swiftc -O -target x86_64-apple-macosx11.0 ApplicationCockpit.swift -o /tmp/ApplicationCockpit-x86_64
lipo -create /tmp/ApplicationCockpit-arm64 /tmp/ApplicationCockpit-x86_64 -output "$BIN"
rm -f /tmp/ApplicationCockpit-arm64 /tmp/ApplicationCockpit-x86_64
chmod +x "$BIN"

echo "Built universal binary -> $BIN"
lipo -info "$BIN"
