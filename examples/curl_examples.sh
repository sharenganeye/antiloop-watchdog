#!/usr/bin/env bash
# curl walkthrough of the watchdog API. Run from the project directory; needs a running watchdog.
set -eu
URL="${WATCHDOG_URL:-http://127.0.0.1:8791}"
TOKEN="${WATCHDOG_TOKEN:-$(cat state/token)}"
SID="curl-demo-1"
H=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")
j() { python3 -m json.tool; }

echo "== health (no token needed)";            curl -s "$URL/health" | j
echo "== status";                              curl -s "${H[@]}" "$URL/status" | j | head -20
echo "== register session + set warn mode";    curl -s "${H[@]}" -X POST "$URL/events" -d "{\"type\":\"state\",\"session_id\":\"$SID\"}" | j
curl -s "${H[@]}" -X POST "$URL/sessions/$SID/mode" -d '{"mode":"warn"}' | j | head -8
echo "== three identical failing tool calls"
for i in 1 2 3; do
  curl -s "${H[@]}" -X POST "$URL/events" -d "{\"type\":\"tool_call\",\"session_id\":\"$SID\",\"tool\":\"terminal\",\"args\":{\"command\":\"make\"},\"status\":\"error\",\"result_text\":\"boom\"}" > /tmp/curl_last.json
done
j < /tmp/curl_last.json
echo "== directives (peek, no side effect)";   curl -s "${H[@]}" "$URL/sessions/$SID/directives?peek=1" | j
DID=$(curl -s "${H[@]}" "$URL/sessions/$SID/directives" | python3 -c 'import json,sys; print(json.load(sys.stdin)["directives"][0]["id"])')
echo "== ack $DID";                            curl -s "${H[@]}" -X POST "$URL/sessions/$SID/directives/$DID/ack" -d '{"result":"applied"}' | j | head -12
echo "== events for the session";              curl -s "${H[@]}" "$URL/events?session_id=$SID&event_type=detection" | j | head -30
echo "== manual redirect request";             curl -s "${H[@]}" -X POST "$URL/sessions/$SID/intervene" -d '{"action":"redirect","message":"Try a different approach."}' | j | head -8
echo "== change one setting at runtime";       curl -s "${H[@]}" -X POST "$URL/config" -d '{"intervention":{"cooldown_s":20}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["config"]["intervention"]["cooldown_s"])'
echo "== reset the session";                   curl -s "${H[@]}" -X POST "$URL/sessions/$SID/reset" -d '{}' | j | head -5
