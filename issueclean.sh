#!/usr/bin/env bash
set -euo pipefail

UPSTREAM="peteromallet/desloppify"
FORK="citizenadam/desloppify"

echo "Ensuring issues are enabled on $FORK..."
gh repo edit "$FORK" --enable-issues

echo "Deleting ALL existing issues in $FORK..."
existing_issues=$(gh issue list -R "$FORK" --state all --limit 1000 --json number -q '.[].number')

for num in $existing_issues; do
  echo "Deleting issue #$num from $FORK..."
  gh issue delete "$num" -R "$FORK" --yes
done

echo "Copying open issues fresh from $UPSTREAM to $FORK..."
gh issue list -R "$UPSTREAM" --state open --limit 1000 --json title,body | \
jq -c '.[]' | \
while read -r issue; do
  title=$(echo "$issue" | jq -r '.title')
  body=$(echo "$issue" | jq -r '.body')
  
  gh issue create -R "$FORK" --title "$title" --body "$body"
done

echo "Done! Issues in $FORK have been purged and re-imported."
