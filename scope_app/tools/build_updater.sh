#!/usr/bin/env bash
set -euo pipefail

scope_root="$(cd "$(dirname "$0")/.." && pwd)"
destination="$scope_root/build/firmware_updater"
mkdir -p "$destination"
cd "$scope_root/firmware_updater"

# Go 1.26 keeps the app and the helper compatible with macOS 12.
export GOTOOLCHAIN=go1.26.11
export CGO_ENABLED=0
name=fnirsi-iap
flags=()
host="$(go env GOOS)"
if [[ "$host" == windows ]]; then
  name+=.exe
  flags=(-ldflags=-H=windowsgui)
fi
go build -mod=readonly -trimpath "${flags[@]}" -o "$destination/$name" .

go mod download
go_root="$(go env GOROOT)"
system_module="$(go list -m -f '{{.Dir}}' golang.org/x/sys)"
plist_module="$(go list -m -f '{{.Dir}}' howett.net/plist)"
if [[ "$host" == windows ]]; then
  go_root="$(cygpath -u "$go_root")"
  system_module="$(cygpath -u "$system_module")"
  plist_module="$(cygpath -u "$plist_module")"
fi
{
  printf 'Go standard library\n\n'
  cat "$go_root/LICENSE"
  printf '\n\ngolang.org/x/sys\n\n'
  cat "$system_module/LICENSE"
  printf '\n\nhowett.net/plist\n\n'
  cat "$plist_module/LICENSE"
} > "$destination/firmware-updater-licenses.txt"
