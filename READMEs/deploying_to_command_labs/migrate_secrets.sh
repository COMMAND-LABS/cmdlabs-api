#!/usr/bin/env bash
#
# Copy every Secret Manager secret referenced by service.yaml from the old
# Kalygo project into command-labs, then grant the Cloud Run runtime service
# account read access to them.
#
# Run locally, authenticated as a principal with:
#   - secretmanager.versions.access on SRC_PROJECT
#   - secretmanager.admin on DST_PROJECT
#
# Idempotent: existing secrets in DST_PROJECT are skipped, not overwritten.
# Dry run first:  DRY_RUN=1 ./migrate_secrets.sh

set -euo pipefail

SRC_PROJECT="${SRC_PROJECT:-kalygo-436411}"
DST_PROJECT="${DST_PROJECT:-command-labs}"
RUNTIME_SA="${RUNTIME_SA:-382688591561-compute@developer.gserviceaccount.com}"
DRY_RUN="${DRY_RUN:-0}"

SERVICE_YAML="$(cd "$(dirname "$0")/../.." && pwd)/service.yaml"

if [ ! -f "$SERVICE_YAML" ]; then
    echo "ERROR: service.yaml not found at $SERVICE_YAML" >&2
    exit 1
fi

# Pull the secret names straight out of the spec so this can't drift from it.
mapfile -t SECRETS < <(
    grep -A2 'secretKeyRef' "$SERVICE_YAML" \
        | grep -E '^\s+name:' \
        | sed -E 's/.*name:\s*//' \
        | tr -d '"' \
        | sort -u
)

if [ "${#SECRETS[@]}" -eq 0 ]; then
    echo "ERROR: no secretKeyRef entries parsed from service.yaml" >&2
    exit 1
fi

echo "Source project : $SRC_PROJECT"
echo "Dest project   : $DST_PROJECT"
echo "Runtime SA     : $RUNTIME_SA"
echo "Secrets found  : ${#SECRETS[@]}"
[ "$DRY_RUN" = "1" ] && echo "MODE           : DRY RUN (no writes)"
echo

created=0; skipped=0; failed=0

for name in "${SECRETS[@]}"; do
    if gcloud secrets describe "$name" --project="$DST_PROJECT" >/dev/null 2>&1; then
        echo "  SKIP    $name (already exists in $DST_PROJECT)"
        skipped=$((skipped + 1))
        continue
    fi

    if ! value="$(gcloud secrets versions access latest --secret="$name" \
                    --project="$SRC_PROJECT" 2>/dev/null)"; then
        echo "  MISSING $name (not readable in $SRC_PROJECT — create by hand)"
        failed=$((failed + 1))
        continue
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo "  WOULD   $name (${#value} bytes)"
        created=$((created + 1))
        continue
    fi

    printf '%s' "$value" | gcloud secrets create "$name" \
        --project="$DST_PROJECT" \
        --replication-policy="automatic" \
        --data-file=- >/dev/null
    echo "  CREATED $name (${#value} bytes)"
    created=$((created + 1))
done

echo
echo "created=$created skipped=$skipped missing=$failed"

if [ "$DRY_RUN" != "1" ] && [ "$created" -gt 0 ]; then
    echo
    echo "Granting secretAccessor to $RUNTIME_SA ..."
    gcloud projects add-iam-policy-binding "$DST_PROJECT" \
        --member="serviceAccount:$RUNTIME_SA" \
        --role="roles/secretmanager.secretAccessor" \
        --condition=None >/dev/null
    echo "Done."
fi

if [ "$failed" -gt 0 ]; then
    echo
    echo "WARNING: $failed secret(s) could not be read from $SRC_PROJECT." >&2
    echo "         Cloud Run will fail to start until they exist in $DST_PROJECT." >&2
    exit 1
fi
