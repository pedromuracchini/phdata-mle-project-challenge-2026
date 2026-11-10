#!/bin/sh
# Live walkthrough of the API against a running container (`make docker-run` in another
# terminal). Each step prints the request, then the response: status line, the X-Request-ID
# header and the JSON body.
#
#   make demo                          # http://localhost:8000
#   DEMO_URL=http://host:8000 scripts/demo.sh
#
# Needs curl. JSON is pretty-printed with python3 if it is on PATH, printed raw otherwise.
set -eu

URL="${DEMO_URL:-http://localhost:8000}"

command -v curl >/dev/null 2>&1 || { echo "error: curl not found on PATH" >&2; exit 1; }
if ! curl -fsS "$URL/health" >/dev/null 2>&1; then
    echo "error: no API at $URL. Start it first: make docker-build && make docker-run" >&2
    exit 1
fi

pretty() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -m json.tool 2>/dev/null || cat
    else
        cat
    fi
}

step=0
title() {
    step=$((step + 1))
    printf '\n\033[1m== %d. %s\033[0m\n' "$step" "$1"
}

# post TITLE BODY [extra curl args...]
post() {
    name="$1"
    body="$2"
    shift 2
    title "$name"
    printf '$ curl -X POST %s/predict -H "Content-Type: application/json"' "$URL"
    for arg in "$@"; do
        case "$arg" in
            *" "*) printf ' "%s"' "$arg" ;;
            *) printf ' %s' "$arg" ;;
        esac
    done
    printf " \\\\\n    -d '%s'\n\n" "$body"
    response=$(curl -sS -i -X POST "$URL/predict" -H "Content-Type: application/json" "$@" \
        -d "$body")
    show "$response"
}

# get TITLE PATH
get() {
    title "$1"
    printf '$ curl %s%s\n\n' "$URL" "$2"
    response=$(curl -sS -i "$URL$2")
    show "$response"
}

# The status line and X-Request-ID from the headers, then the body.
show() {
    printf '%s\n' "$1" | tr -d '\r' | awk '
        NR == 1 { print; next }
        /^$/ { exit }
        tolower($0) ~ /^x-request-id:/ { print }'
    printf '%s\n' "$1" | tr -d '\r' | awk 'body { print } /^$/ { body = 1 }' | pretty
}

# First row of src/data/future_unseen_examples.csv, schema fields only.
COMPLETE='{"bedrooms": 4, "bathrooms": 1.0, "sqft_living": 1680, "sqft_lot": 5043, "floors": 1.5, "sqft_above": 1680, "sqft_basement": 0, "zipcode": "98118"}'

post "Complete request" "$COMPLETE"

post "Nulls: sqft_basement is derived exactly, bedrooms and floors go to KNN" \
    '{"bedrooms": null, "bathrooms": 1.0, "sqft_living": 1680, "sqft_lot": 5043, "floors": null, "sqft_above": 1680, "sqft_basement": null, "zipcode": "98118"}'
echo "(The price often stays the same: the model is a uniform 5-NN, so it moves only when an"
echo " imputed value changes the neighbour set. docs/imputation_report.md, finding 2.)"

post "Missing zipcode: 422" \
    '{"bedrooms": 4, "bathrooms": 1.0, "sqft_living": 1680, "sqft_lot": 5043, "floors": 1.5, "sqft_above": 1680, "sqft_basement": 0}'

post "Unknown zipcode: 422" \
    '{"bedrooms": 4, "bathrooms": 1.0, "sqft_living": 1680, "sqft_lot": 5043, "floors": 1.5, "sqft_above": 1680, "sqft_basement": 0, "zipcode": "00000"}'

post "Unknown field (a column the model does not use): 422" \
    '{"bedrooms": 4, "bathrooms": 1.0, "sqft_living": 1680, "sqft_lot": 5043, "floors": 1.5, "sqft_above": 1680, "sqft_basement": 0, "zipcode": "98118", "waterfront": 0}'

post "X-Request-ID: a caller's ID is echoed back" "$COMPLETE" -H "X-Request-ID: demo-123"

get "Readiness" /ready

title "Metrics (excerpt)"
printf '$ curl -s %s/metrics | grep -E ...\n\n' "$URL"
curl -sS "$URL/metrics" | grep -E \
    '^(http_requests_total|prediction_missing_fields_total|predicted_price_dollars_(count|sum))' \
    || true
