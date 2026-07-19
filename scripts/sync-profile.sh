#!/bin/bash
# Refresh the vendored resume library from the resume-builder repo.
#
# The worker reads resume variants from data/profile.json (committed) so it works
# in GitHub Actions, where the sibling ../resume-builder repo isn't checked out.
# Run this whenever you update your resumes in the builder, then commit the change.
#
#   ./scripts/sync-profile.sh
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$DIR/../resume-builder/src/data/profile.json"
DEST="$DIR/data/profile.json"
if [ ! -f "$SRC" ]; then
  echo "resume-builder profile not found at $SRC" >&2
  exit 1
fi
# Safety: refuse to vendor if it somehow contains contact PII.
if grep -qiE '"(email|phone)":[[:space:]]*"[^"]+"' "$SRC"; then
  echo "refusing: $SRC appears to contain email/phone. Keep contact PII out of profile.json." >&2
  exit 1
fi
cp "$SRC" "$DEST"
echo "synced -> data/profile.json"
