#!/usr/bin/env bash
#
# The Monday-after-a-weekend pull: sales from Square, then weather from the
# archive, both against production.
#
#   scripts/weekly-pull.sh              # the whole current season, then weather
#   scripts/weekly-pull.sh --since 10   # only ask Square for the last 10 days
#   scripts/weekly-pull.sh --dry-run    # fetch and reconcile, write nothing
#
# Why the default re-reads the whole season rather than --since: a week that
# gets skipped is the normal failure of a script somebody runs by hand, and a
# narrow window would then drop the orders in the gap without saying so.
# Re-reading is harmless (the order is the unit; already-loaded orders are
# left alone) — it just costs a few thousand reads. Pass --since when you
# know nothing was missed and want it quick.
#
# Why both halves always run: the two are independent, and a Square outage
# should not also cost the weather. Each reports for itself and the script
# exits non-zero if either failed.

set -uo pipefail

SERVICE="${RAILWAY_SERVICE:-Server}"

SINCE=""
DRY_RUN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --since=*) SINCE="${1#*=}"; shift ;;
    --dry-run) DRY_RUN="--dry-run"; shift ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
done

command -v railway >/dev/null 2>&1 || {
  echo "railway CLI not found. brew install railway, then railway login." >&2
  exit 2
}

run() {  # run <label> <command…>
  local label="$1"; shift
  echo
  echo "=== $label ==="
  # railway ssh takes the command as one string.
  railway ssh --service "$SERVICE" "$*"
}

SALES_CMD="python manage.py import_square_orders"
[ -n "$SINCE" ] && SALES_CMD="$SALES_CMD --since $SINCE"
[ -n "$DRY_RUN" ] && SALES_CMD="$SALES_CMD $DRY_RUN"

WEATHER_CMD="python manage.py fetch_weather"
[ -n "$DRY_RUN" ] && WEATHER_CMD="$WEATHER_CMD $DRY_RUN"

run "Sales — Square Orders API" "$SALES_CMD"
sales=$?

run "Weather — Open-Meteo archive" "$WEATHER_CMD"
weather=$?

echo
echo "=== Summary ==="
[ $sales -eq 0 ]   && echo "  sales:   ok" || echo "  sales:   FAILED (exit $sales)"
[ $weather -eq 0 ] && echo "  weather: ok" || echo "  weather: FAILED (exit $weather)"
echo
echo "Days the archive has no reading for yet are normal: it lags about five"
echo "days, so the last weekend usually lands on the run after this one."

[ $sales -eq 0 ] && [ $weather -eq 0 ] || exit 1
