#!/bin/bash
set -euo pipefail
SCOPE_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$SCOPE_ROOT"
bash tools/build_updater.sh
flutter build macos --release "$@"
SCOPE_APP="$SCOPE_ROOT/build/macos/Build/Products/Release/fnirsi_scope.app"
# Incremental Flutter builds can replace App.framework without re-signing the
# outer bundle. Seal it after embedding the firmware helper.
codesign --force --sign "${SCOPE_SIGN_IDENTITY:--}" --timestamp=none \
  --entitlements "$SCOPE_ROOT/macos/Runner/Release.entitlements" "$SCOPE_APP"
codesign --verify --deep --strict --verbose=1 "$SCOPE_APP"
