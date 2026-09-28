#!/usr/bin/env bash
#
# Pull a full dump of the production database down to this machine.
#
#   scripts/backup-db.sh                 # dump, verify the TOC, prune old ones
#   scripts/backup-db.sh --verify-restore # also restore it into a scratch
#                                         # container and count the rows back
#   scripts/backup-db.sh --keep 90        # retention in days (default 30)
#   scripts/backup-db.sh --out DIR        # where dumps live (default ~/scarves-backups)
#
# WHY THIS EXISTS AT ALL
# Railway's own backups and point-in-time recovery are a paid plan feature, and
# this project isn't on it. But even on that plan the backups would live in the
# same Railway account as the database, which does not protect against anything
# that takes the account — a billing lapse, a deleted project, a mistake made
# while logged in. A copy on a laptop in the house is the only one that is
# somewhere else. That is the whole argument: not that it is cheaper, that it
# is elsewhere.
#
# WHY DOCKER RUNS pg_dump AND NOT THE HOMEBREW ONE
# Production is Postgres 16. pg_dump refuses to dump a server newer than
# itself, so the 14.x on this Mac aborts with "server version mismatch" and
# writes nothing. Pinning the image to the server's major version below means
# the dumper always matches, and a Railway upgrade is a one-line change here
# rather than a mysterious failure. Nothing needs to be installed on the host.
#
# WHY THE CREDENTIALS ARE NOT IN THIS FILE
# The connection URL is read from the Railway CLI at run time. A password in a
# script is a password in git history, a Time Machine snapshot and every backup
# of this laptop, and rotating it then means editing files. `railway login`
# holds the only secret, and it can be revoked in one place.
#
# WHAT THIS DOES NOT COVER
# The photographs. Product images live in a Railway bucket, not in Postgres, so
# a restore from one of these dumps comes back with every FinishedProductImage
# row pointing at an object this dump never held. That is a known gap and it is
# written down rather than half-solved: see docs/claude/labels.md for how the
# images get there in the first place.

set -uo pipefail

PG_MAJOR=16                       # must match the production server's major version
OUT_DIR="${HOME}/scarves-backups"
KEEP_DAYS=30
VERIFY_RESTORE=""
SERVICE="${RAILWAY_DB_SERVICE:-Postgres}"

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --out=*) OUT_DIR="${1#*=}"; shift ;;
    --keep) KEEP_DAYS="$2"; shift 2 ;;
    --keep=*) KEEP_DAYS="${1#*=}"; shift ;;
    --verify-restore) VERIFY_RESTORE=1; shift ;;
    -h|--help) sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
done

command -v railway >/dev/null 2>&1 || {
  echo "railway CLI not found. brew install railway, then railway login." >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || {
  echo "docker not found. This uses it to run a pg_dump matching the server." >&2
  exit 2
}

echo "=== Backup: production database ==="

# --- the connection URL, held in a variable and never written to disk --------
# DATABASE_PUBLIC_URL is the one routed over the internet; DATABASE_URL points
# at Railway's private network and resolves to nothing from here.
DB_URL="$(railway variables --service "$SERVICE" --kv 2>/dev/null \
          | sed -n 's/^DATABASE_PUBLIC_URL=//p')"
if [ -z "$DB_URL" ]; then
  echo "  FAILED: could not read DATABASE_PUBLIC_URL from service '$SERVICE'." >&2
  echo "  Check 'railway status' is linked to the right project, and that the" >&2
  echo "  database service is called '$SERVICE' (override: RAILWAY_DB_SERVICE)." >&2
  exit 1
fi

export DB_URL

mkdir -p "$OUT_DIR" || exit 1
STAMP="$(date +%Y-%m-%d_%H%M)"
DUMP="scarves_${STAMP}.dump"
DEST="${OUT_DIR}/${DUMP}"

# --- the dump ---------------------------------------------------------------
# -Fc  custom format: compressed, and pg_restore can pull one table out of it
#      or turn the whole thing back into plain SQL with `pg_restore -f -`, so
#      choosing it costs no readability later.
# The dump is written to a .part file and renamed only on success, so an
# interrupted run can never leave something that looks like a usable backup.
echo "[1/4] dumping (Postgres ${PG_MAJOR} client via docker)..."
# The URL goes in via the environment, not as an argument: an argument is
# visible in `ps` on the host for as long as the dump runs, which would put the
# database password in front of any other process on this machine.
if docker run --rm -i -e DB_URL \
     "postgres:${PG_MAJOR}" \
     sh -c 'pg_dump --format=custom --no-owner --no-privileges --dbname="$DB_URL"' \
     > "${DEST}.part" 2>/tmp/backup-db.err; then
  mv "${DEST}.part" "$DEST"
  echo "      wrote $DEST ($(du -h "$DEST" | cut -f1))"
else
  echo "      FAILED — pg_dump said:" >&2
  sed 's/^/      /' /tmp/backup-db.err >&2
  rm -f "${DEST}.part"
  exit 1
fi

# --- cheap integrity check --------------------------------------------------
# A truncated dump still looks like a file of about the right size. Listing the
# table of contents is the least that distinguishes one that will restore from
# one that will not, and it costs a fraction of a second.
echo "[2/4] checking the archive reads back..."
# The archive is mounted rather than piped in: reading a custom-format table
# of contents means seeking, and a pipe cannot seek — piping it here failed
# with "did not find magic string in file header" on a dump that was perfectly
# good, which is the worst possible false alarm for a backup check to raise.
TOC_COUNT="$(docker run --rm -v "${OUT_DIR}:/b:ro" "postgres:${PG_MAJOR}" \
             pg_restore -l "/b/${DUMP}" 2>/dev/null | grep -c '^[0-9]')"
if [ "${TOC_COUNT:-0}" -lt 1 ]; then
  echo "      FAILED: the archive has no readable table of contents." >&2
  echo "      Keeping $DEST for inspection." >&2
  exit 1
fi
echo "      ok — ${TOC_COUNT} objects listed"

# --- the real check, on request ---------------------------------------------
# An untested backup is a guess. This restores into a throwaway container and
# counts rows in the tables that would actually hurt to lose, which is the only
# thing that answers "would this have worked".
if [ -n "$VERIFY_RESTORE" ]; then
  echo "[3/4] restoring into a scratch container to prove it..."
  CID="$(docker run --rm -d -e POSTGRES_PASSWORD=verify -e POSTGRES_DB=verify \
         -v "${OUT_DIR}:/b:ro" "postgres:${PG_MAJOR}")"
  trap 'docker kill "$CID" >/dev/null 2>&1' EXIT
  for _ in $(seq 1 30); do
    docker exec "$CID" pg_isready -q -U postgres && break
    sleep 1
  done
  if docker exec "$CID" pg_restore --no-owner --no-privileges \
       -U postgres -d verify "/b/${DUMP}" >/dev/null 2>&1; then
    echo "      restored. Rows that came back:"
    docker exec -i "$CID" psql -U postgres -d verify -At -c "
      select 'recipes          '||count(*) from scarves_recipe
      union all select 'finished products '||count(*) from scarves_finishedproduct
      union all select 'inventory log    '||count(*) from scarves_inventorylog
      union all select 'sale lines       '||count(*) from scarves_saleline;
    " 2>/dev/null | sed 's/^/        /'
  else
    echo "      FAILED: the dump would not restore. This backup is not usable." >&2
    exit 1
  fi
  docker kill "$CID" >/dev/null 2>&1; trap - EXIT
else
  echo "[3/4] full restore test skipped (pass --verify-restore to run it)"
fi

# --- retention --------------------------------------------------------------
# The dump is a few megabytes, so this is about a legible directory rather than
# disk. The 1st of each month is kept regardless: the failure a daily window
# cannot catch is a slow one — a bad value written weeks ago and only noticed
# now — and that needs a copy from before it happened.
echo "[4/4] pruning dumps older than ${KEEP_DAYS} days (keeping each month's 1st)..."
PRUNED=0
while IFS= read -r old; do
  case "$(basename "$old")" in scarves_????-??-01_*) continue ;; esac
  rm -f "$old" && PRUNED=$((PRUNED+1))
done < <(find "$OUT_DIR" -name 'scarves_*.dump' -type f -mtime "+${KEEP_DAYS}" 2>/dev/null)
TOTAL="$(find "$OUT_DIR" -name 'scarves_*.dump' -type f 2>/dev/null | wc -l | tr -d ' ')"
echo "      removed ${PRUNED}; ${TOTAL} dumps on hand, $(du -sh "$OUT_DIR" | cut -f1) total"

echo
echo "=== Done. Restore with: ==="
echo "  docker run --rm -v ${OUT_DIR}:/b:ro postgres:${PG_MAJOR} \\"
echo "    pg_restore --no-owner --no-privileges --clean --if-exists \\"
echo "    --dbname=\"\$(railway variables --service ${SERVICE} --kv | sed -n 's/^DATABASE_PUBLIC_URL=//p')\" \\"
echo "    /b/${DUMP}"
echo
echo "Read the dump as plain SQL without a server:"
echo "  docker run --rm -v ${OUT_DIR}:/b:ro postgres:${PG_MAJOR} pg_restore -f - /b/${DUMP} | less"
