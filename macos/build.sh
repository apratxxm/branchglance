#!/bin/sh
# Build the macOS notch host: ./macos/build.sh, then ./macos/build/BranchglanceNotch
set -e
cd "$(dirname "$0")"
mkdir -p build
swiftc -O -o build/BranchglanceNotch BranchglanceNotch.swift -framework Cocoa -framework WebKit
