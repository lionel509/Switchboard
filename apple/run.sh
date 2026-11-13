#!/bin/sh
# Build (when shim.swift is newer) and run the Apple Foundation Models shim on localhost (#98).
# Runs in the foreground; Ctrl-C stops it. Nothing keeps it alive: it is on-demand.
set -eu
cd "$(dirname "$0")"
mkdir -p build
if [ ! -x build/apple-shim ] || [ shim.swift -nt build/apple-shim ]; then
  /Library/Developer/CommandLineTools/usr/bin/swiftc -O -swift-version 5 \
    -sdk /Library/Developer/CommandLineTools/SDKs/MacOSX27.0.sdk -target arm64-apple-macos27.0 \
    -o build/apple-shim shim.swift
fi
exec build/apple-shim "${APPLE_SHIM_PORT:-8788}"
