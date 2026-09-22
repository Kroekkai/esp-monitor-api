#!/usr/bin/env bash
# ทดสอบ API ทั้งหมดบน localhost หลังจากรัน `docker compose up -d --build` แล้ว
# ใช้: ./scripts/test_local.sh
set -e

BASE_URL="${BASE_URL:-http://localhost:${API_PORT:-3005}}/iot"
HEALTH_URL="${BASE_URL%/iot}/health"
ADMIN_USER="admin"
ADMIN_PASS="${DEFAULT_ADMIN_PASSWORD:-admin123}"
DEVICE_KEY="${DEVICE_API_KEY:-local-dev-device-key}"

echo "== 1) health check =="
curl -s "$HEALTH_URL"; echo

echo "== 2) login =="
LOGIN_RESP=$(curl -s -X POST "$BASE_URL/api/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")
echo "$LOGIN_RESP"
TOKEN=$(echo "$LOGIN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "== 3) post sensor data (simulate ESP32) =="
curl -s -X POST "$BASE_URL/api/sensor" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $DEVICE_KEY" \
  -d '{"device_id":"esp32-01","temperature":29.4,"humidity":65.2}'
echo

echo "== 4) get recent sensor data (requires token) =="
curl -s "$BASE_URL/api/sensor/data?hours=24" \
  -H "Authorization: Bearer $TOKEN"
echo

echo "== 5) export users as csv (requires admin token) =="
curl -s "$BASE_URL/api/users/export" \
  -H "Authorization: Bearer $TOKEN"
echo

echo "== 6) เปิดหน้าเว็บได้ที่: ${BASE_URL}/ =="
echo "== done =="
