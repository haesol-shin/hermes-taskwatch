#!/bin/sh
# Run the apply check, then merge only that commit. Method is required.
set -eu
if [ "$#" -ne 5 ]; then
  echo "usage: merge_pr.sh <hermes> <root> <card> <pr> <method>" >&2
  exit 2
fi
hermes=$1
root=$2
card=$3
pr=$4
method=$5
: "${HERMES_PROFILE:?Set HERMES_PROFILE to the execution profile}"
case $method in
  squash|merge|rebase) ;;
  *)
    echo "method must be squash, merge, or rebase" >&2
    exit 2
    ;;
esac
sha=$("$hermes" -p "$HERMES_PROFILE" taskwatch check "$card" --apply --root "$root")
exec gh pr merge "$pr" "--$method" --match-head-commit "$sha"
