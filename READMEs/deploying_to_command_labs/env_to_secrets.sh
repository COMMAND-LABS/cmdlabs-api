#!/usr/bin/env bash
#
# Push values from .env into Google Secret Manager.
#
# By default only uploads the variables service.yaml actually references, so
# local-only entries (POSTGRES_TEST_URL, GCS_SA_PATH, ...) don't leak into the
# deployed project. Set ALL=1 to upload every uncommented variable instead.
#
# Run locally, authenticated as a principal with secretmanager.admin on PROJECT.
#
#   DRY_RUN=1 ./env_to_secrets.sh    # preview, no writes, no values printed
#   ./env_to_secrets.sh              # create missing secrets
#   UPDATE=1 ./env_to_secrets.sh     # also add a new version where the value differs
#   ALL=1 ./env_to_secrets.sh        # every uncommented var, not just what's needed
#
# Existing secrets are skipped unless UPDATE=1. Values are never echoed.

set -euo pipefail

PROJECT="${PROJECT:-command-labs}"
RUNTIME_SA="${RUNTIME_SA:-382688591561-compute@developer.gserviceaccount.com}"
DRY_RUN="${DRY_RUN:-0}"
UPDATE="${UPDATE:-0}"
ALL="${ALL:-0}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
SERVICE_YAML="$REPO_ROOT/service.yaml"

[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found" >&2; exit 1; }

# Which secrets does the deployed spec actually reference?
# Kept as a newline-delimited string rather than an associative array so this
# runs on macOS's stock bash 3.2, which has neither `declare -A` nor `mapfile`.
NEEDED_LIST=""
NEEDED_COUNT=0
if [ "$ALL" != "1" ]; then
    [ -f "$SERVICE_YAML" ] || { echo "ERROR: $SERVICE_YAML not found (use ALL=1 to skip this check)" >&2; exit 1; }
    NEEDED_LIST="$(
        grep -A2 'secretKeyRef' "$SERVICE_YAML" \
            | grep -E '^[[:space:]]+name:' \
            | sed -E 's/.*name:[[:space:]]*//' \
            | tr -d '"' \
            | sort -u
    )"
    NEEDED_COUNT="$(printf '%s\n' "$NEEDED_LIST" | grep -c . || true)"
fi

echo "Project    : $PROJECT"
echo "Env file   : $ENV_FILE"
echo "Scope      : $([ "$ALL" = "1" ] && echo "every uncommented var" || echo "$NEEDED_COUNT vars referenced by service.yaml")"
[ "$DRY_RUN" = "1" ] && echo "Mode       : DRY RUN (no writes)"
[ "$UPDATE" = "1" ] && echo "Update     : ON (new version when value differs)"
echo

created=0; updated=0; skipped=0; unchanged=0; ignored=0

while IFS= read -r line || [ -n "$line" ]; do
    # Skip blanks and comments (leading whitespace tolerated).
    case "$(printf '%s' "$line" | sed -E 's/^[[:space:]]+//')" in
        ''|'#'*) continue ;;
    esac

    [[ "$line" != *"="* ]] && continue

    key="${line%%=*}"
    val="${line#*=}"

    # Tolerate `export FOO=bar` and surrounding whitespace.
    key="$(printf '%s' "$key" | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/[[:space:]]+$//')"

    # Keys must look like env var names; anything else is a malformed line.
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

    # Strip one layer of matching surrounding quotes. Inline `#` comments are
    # NOT stripped -- values legitimately contain '#' and guessing corrupts them.
    if [[ "$val" =~ ^\"(.*)\"$ ]] || [[ "$val" =~ ^\'(.*)\'$ ]]; then
        val="${BASH_REMATCH[1]}"
    fi

    if [ "$ALL" != "1" ] && ! printf '%s\n' "$NEEDED_LIST" | grep -qxF "$key"; then
        ignored=$((ignored + 1))
        continue
    fi

    if [ -z "$val" ]; then
        echo "  EMPTY     $key (no value in .env -- skipped)"
        skipped=$((skipped + 1))
        continue
    fi

    if gcloud secrets describe "$key" --project="$PROJECT" >/dev/null 2>&1; then
        if [ "$UPDATE" != "1" ]; then
            echo "  EXISTS    $key (skipped; UPDATE=1 to add a version)"
            skipped=$((skipped + 1))
            continue
        fi
        current="$(gcloud secrets versions access latest --secret="$key" \
                     --project="$PROJECT" 2>/dev/null || true)"
        if [ "$current" = "$val" ]; then
            echo "  SAME      $key (${#val} bytes)"
            unchanged=$((unchanged + 1))
            continue
        fi
        if [ "$DRY_RUN" = "1" ]; then
            echo "  WOULD ADD $key (${#val} bytes, differs from current)"
        else
            printf '%s' "$val" | gcloud secrets versions add "$key" \
                --project="$PROJECT" --data-file=- >/dev/null
            echo "  UPDATED   $key (${#val} bytes)"
        fi
        updated=$((updated + 1))
        continue
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo "  WOULD ADD $key (${#val} bytes, new)"
    else
        printf '%s' "$val" | gcloud secrets create "$key" \
            --project="$PROJECT" \
            --replication-policy="automatic" \
            --data-file=- >/dev/null
        echo "  CREATED   $key (${#val} bytes)"
    fi
    created=$((created + 1))
done < "$ENV_FILE"

echo
echo "created=$created updated=$updated unchanged=$unchanged skipped=$skipped not-in-scope=$ignored"

# Report anything service.yaml needs that .env could not supply.
if [ "$ALL" != "1" ] && [ "$NEEDED_COUNT" -gt 0 ] && [ "$DRY_RUN" != "1" ]; then
    missing=""
    while IFS= read -r n; do
        [ -n "$n" ] || continue
        if ! gcloud secrets describe "$n" --project="$PROJECT" >/dev/null 2>&1; then
            missing="$missing  $n
"
        fi
    done <<EOF
$NEEDED_LIST
EOF
    if [ -n "$missing" ]; then
        echo
        echo "WARNING: still absent from $PROJECT -- Cloud Run will not start:" >&2
        printf '%s' "$missing" >&2
        exit 1
    fi
fi

if [ "$DRY_RUN" != "1" ] && [ $((created + updated)) -gt 0 ]; then
    echo
    echo "Granting secretAccessor to $RUNTIME_SA ..."
    gcloud projects add-iam-policy-binding "$PROJECT" \
        --member="serviceAccount:$RUNTIME_SA" \
        --role="roles/secretmanager.secretAccessor" \
        --condition=None >/dev/null
    echo "Done."
fi
