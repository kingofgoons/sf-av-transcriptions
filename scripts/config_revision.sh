#!/usr/bin/env bash
#
# config_revision.sh — compute the canonical revision hash for a config file
#
# ONE implementation, used by three callers: publish_config.sh (to stamp the staged
# copy) and 04_deploy_notebook.sh / 09_deploy_dashboard.sh (to detect a stale
# staged copy). Duplicating this logic in each would let the three drift apart,
# which is the same class of bug the config store itself was created to fix.
#
# WHAT IS HASHED, AND WHY
#
#   Only `SET ...` statements, so editing a comment does NOT churn the revision.
#   That matters because the deploy scripts REFUSE to deploy on a revision
#   mismatch: if a comment edit changed the hash, documenting your own config
#   would look identical to a stale staged copy, and operators would learn to
#   ignore the warning.
#
#   Inline trailing comments are stripped for the same reason.
#
#   CONFIG_REVISION itself is excluded. It would otherwise be self-referential:
#   the value being computed would be part of its own input.
#
#   Lines are sorted, so reordering statements without changing any value keeps the
#   revision stable. Same values means same revision, regardless of layout.
#
# Usage:
#   ./config_revision.sh scripts/00_config.sql      -> prints e.g. 3f9a1c07b82e
#
set -euo pipefail

REVISION_LENGTH=12

if [[ $# -lt 1 ]]; then
    echo "Usage: $(basename "$0") <config-file>" >&2
    exit 2
fi

CONFIG_FILE="$1"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "Error: ${CONFIG_FILE} not found" >&2
    exit 1
fi

# Canonical form:
#   1. keep only SET statements
#   2. drop CONFIG_REVISION (self-reference)
#   3. truncate at the first ';' to remove trailing comments
#   4. collapse whitespace runs so realignment does not change the hash
#   5. sort for order independence
#
# `|| true` is required: grep exits 1 when it matches nothing, and under
# `set -euo pipefail` that would abort the script here, losing the explanatory
# error below and leaving the caller with a bare exit code.
CANONICAL="$(
    { grep -E '^SET[[:space:]]' "${CONFIG_FILE}" || true; } \
        | { grep -vE '^SET[[:space:]]+CONFIG_REVISION[[:space:]]*=' || true; } \
        | sed 's/;.*$/;/' \
        | tr -s '[:space:]' ' ' \
        | sort
)"

if [[ -z "${CANONICAL}" ]]; then
    echo "Error: no SET statements found in ${CONFIG_FILE}." >&2
    echo "       Is this actually a deployment config file?" >&2
    exit 1
fi

printf '%s' "${CANONICAL}" | shasum -a 256 | cut -c1-"${REVISION_LENGTH}"
