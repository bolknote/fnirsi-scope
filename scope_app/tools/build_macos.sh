#!/bin/bash
set -euo pipefail
SCOPE_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$SCOPE_ROOT"
flutter build macos --release "$@"
SCOPE_APP="$SCOPE_ROOT/build/macos/Build/Products/Release/fnirsi_scope.app"
# Incremental Flutter builds can replace App.framework without re-signing the
# outer bundle. Seal it last, preserving USB and user-selected-file entitlements.
codesign --force --sign "${SCOPE_SIGN_IDENTITY:--}" --timestamp=none \
  --entitlements "$SCOPE_ROOT/macos/Runner/Release.entitlements" "$SCOPE_APP"
codesign --verify --deep --strict --verbose=1 "$SCOPE_APP"
