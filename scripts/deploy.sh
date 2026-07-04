#!/usr/bin/env bash
set -euo pipefail

# Commit and push local changes. GitHub remains the source of truth and Railway
# deploys from GitHub.

cd "$(dirname "$0")/.."

if [ "$#" -lt 1 ] || [ -z "${1:-}" ]; then
  echo "Usage: scripts/deploy.sh \"commit message\""
  exit 1
fi

git status
git add .
git commit -m "$1"
git push
