#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_OWNER="peteromallet"
FORK_OWNER="citizenadam"
REPO_NAME="desloppify"

UPSTREAM="$UPSTREAM_OWNER/$REPO_NAME"
FORK="$FORK_OWNER/$REPO_NAME"

if [ ! -d ".git" ]; then
  echo "No git repository found in current directory. Cloning $FORK..."
  git clone "https://github.com/$FORK.git" .
fi

# Set origin explicitly to your fork
git remote set-url origin "https://github.com/$FORK.git"

# Add upstream for fetching, but disable push capabilities to upstream completely
git remote add upstream "https://github.com/$UPSTREAM.git" 2>/dev/null || git remote set-url upstream "https://github.com/$UPSTREAM.git"
git remote set-url --push upstream "DISABLED_DO_NOT_PUSH"

echo "Fetching latest upstream updates..."
git fetch upstream
git checkout main
git pull upstream main

prs=$(gh pr list -R "$UPSTREAM" --state open --limit 1000 --json number -q '.[].number')

for pr in $prs; do
  echo "Processing PR #$pr..."
  git fetch upstream "pull/$pr/head:pr-$pr"
  
  if ! git merge "pr-$pr" --no-edit -m "Merge upstream PR #$pr"; then
    echo "Conflict detected on PR #$pr. Resolving using 'ours' strategy..."
    git merge --abort 2>/dev/null || true
    git merge "pr-$pr" --no-edit -s recursive -X ours -m "Merge upstream PR #$pr (resolved conflicts using ours)" || {
      echo "Failed to resolve PR #$pr, skipping..."
      git merge --abort 2>/dev/null || true
    }
  fi
done

# Verification check: Ensure origin is NOT peteromallet before pushing
ORIGIN_URL=$(git remote get-url origin)
if [[ "$ORIGIN_URL" == *"$UPSTREAM_OWNER"* ]]; then
  echo "CRITICAL ERROR: Origin points to $UPSTREAM_OWNER! Aborting push to protect upstream repository."
  exit 1
fi

echo "Safety check passed. Pushing merged changes to $ORIGIN_URL..."
git push origin main
