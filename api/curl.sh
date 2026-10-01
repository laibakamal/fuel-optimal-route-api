#!/usr/bin/env bash
# Same requests as api/requests.http, as curl. Start the server first: make run
#
#   ./api/curl.sh            run them all
#   ./api/curl.sh 1          run just number 1
set -u
HOST="${HOST:-http://127.0.0.1:8000}"
ONLY="${1:-}"

run() {
  local n="$1" title="$2"; shift 2
  [ -n "$ONLY" ] && [ "$ONLY" != "$n" ] && return 0
  printf '\n\033[1m=== %s. %s ===\033[0m\n' "$n" "$title"
  curl -sS -w '\nHTTP %{http_code}  %{content_type}  %{time_total}s\n' "$@"
}

run 1 "Happy path: Dallas -> Chicago (1 routing call, 0 geocoding)" \
  "$HOST/api/v1/route?start=Dallas,+TX&finish=Chicago,+IL&geometry=none"

run 2 "Cache hit: identical request (0 external calls)" \
  "$HOST/api/v1/route?start=Dallas,+TX&finish=Chicago,+IL&geometry=none"

run 3 "Explicit coordinates" \
  "$HOST/api/v1/route?start=32.7767,-96.7970&finish=41.8781,-87.6298&geometry=none"

run 4 "POST with a JSON body" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"start":"Los Angeles, CA","finish":"New York, NY","geometry":"none"}' \
  "$HOST/api/v1/route"

run 5 "Long haul: LA -> NY (see unpriced_origin_miles)" \
  "$HOST/api/v1/route?start=Los+Angeles,+CA&finish=New+York,+NY&geometry=none"

run 6 "Short trip, inside one tank" \
  "$HOST/api/v1/route?start=Dallas,+TX&finish=Fort+Worth,+TX&geometry=none"

run 7 "Start == finish (200, not an error)" \
  "$HOST/api/v1/route?start=Dallas,+TX&finish=Dallas,+TX&geometry=none"

run 8 "Wider detour: 25 miles" \
  "$HOST/api/v1/route?start=Dallas,+TX&finish=Chicago,+IL&max_detour_miles=25&geometry=none"

run 10 "ERROR 422 infeasible: 0.1 mi corridor" \
  "$HOST/api/v1/route?start=Dallas,+TX&finish=Chicago,+IL&max_detour_miles=0.1&geometry=none"

run 11 "ERROR 400 ambiguous city" \
  "$HOST/api/v1/route?start=Springfield&finish=Chicago,+IL"

run 12 "ERROR 400 outside the USA" \
  "$HOST/api/v1/route?start=51.5074,-0.1278&finish=Chicago,+IL"

run 13 "ERROR 400 unknown place" \
  "$HOST/api/v1/route?start=Nowhereville,+ZZ&finish=Chicago,+IL"

run 14 "ERROR 400 validation (detour > 50)" \
  "$HOST/api/v1/route?start=Dallas,+TX&finish=Chicago,+IL&max_detour_miles=500"

run 15 "ERROR 405 wrong method" -X DELETE \
  "$HOST/api/v1/route?start=Dallas,+TX&finish=Chicago,+IL"
