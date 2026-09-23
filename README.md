# ESP Temperature/Humidity Monitor API + Web

Web API (FastAPI + Python) รับค่าอุณหภูมิ/ความชื้นจาก ESP32/ESP8266 ผ่าน POST เดียว
แล้วเขียนลง InfluxDB ทันทีในตัว handler เดียวกัน (ไม่มีการยิงต่อไปอีก service)
พร้อมระบบ user/login ด้วย JWT, export user table เป็น .csv และ **หน้าเว็บแดชบอร์ด**
สำหรับดูกราฟอุณหภูมิ/ความชื้นและจัดการผู้ใช้

ทุกอย่าง (API + หน้าเว็บ) อยู่ใต้ path เดียวกันคือ **`/iot`** เพื่อให้ใช้ reverse
proxy ร่วมกับ service อื่นบนเซิร์ฟเวอร์ได้โดยไม่ชน path กัน

รันด้วย `uvicorn app.main:app` เหมือน container `ocr-meter-store` ที่มีอยู่แล้ว
แต่พอร์ตฝั่ง host **กำหนดเองได้ผ่านตัวแปร `API_PORT` ใน `.env`** (container ข้างใน
คงที่ที่ `8000` เสมอ ไม่ต้องแก้) ค่าเริ่มต้นตั้งไว้ที่ `3005` ซึ่งไม่ชนกับ
`ocr-meter-store` ที่ใช้ host `3003` อยู่แล้ว และไม่ชนพอร์ตอื่นบนเซิร์ฟเวอร์
(3001, 3002, 3004, 3306 = mariadb, 5432 = timescaledb, 80/81, 443) — ถ้าจะเปลี่ยน
พอร์ตทีหลัง แก้แค่บรรทัด `API_PORT=` ใน `.env` แล้ว `docker compose up -d` ใหม่
ไม่ต้องแตะไฟล์ `docker-compose.yml`

## โครงสร้างไฟล์

```
esp-monitor/
├── app/
│   ├── main.py         # FastAPI app: router prefix /iot + endpoints ทั้งหมด
│   ├── auth.py          # JWT / hash password
│   ├── database.py      # SQLite: ตาราง users
│   ├── influx.py         # เขียน/อ่านค่า sensor ใน InfluxDB (ตัวที่มีอยู่แล้ว)
│   ├── schemas.py       # pydantic models
│   └── static/           # หน้าเว็บแดชบอร์ด (index.html, app.js, style.css)
├── Dockerfile
├── docker-compose.yml     # แค่ service esp-monitor-api ตัวเดียว, พอร์ต host อ่านจาก ${API_PORT}
├── scripts/test_local.sh  # สคริปต์ smoke test
├── requirements.txt
└── .env / .env.example
```

## InfluxDB ที่ใช้

โปรเจกต์นี้**ไม่ได้รัน InfluxDB ของตัวเอง** — ต่อกับ InfluxDB ที่มีอยู่แล้วที่
`innovation.ntplc.co.th:3002` (คนละเซิร์ฟเวอร์กับที่รัน container นี้ เข้าถึงผ่าน
โดเมนสาธารณะทางอินเทอร์เน็ตปกติ ไม่ต้องต่อ docker network ใดๆ) ตั้งค่าที่ต้องมีใน
`.env`:

```dotenv
INFLUX_URL=http://innovation.ntplc.co.th:3002
INFLUX_ORG=<ชื่อ org หรือ orgID>
INFLUX_BUCKET=<bucket สำหรับโปรเจกต์นี้>
INFLUX_TOKEN=<admin token ของ InfluxDB ตัวนี้>
```

**หา org/token ยังไง:** เข้า `http://innovation.ntplc.co.th:3002` (มี admin token
อยู่แล้ว) → เมนูซ้าย "Load Data" → "API Tokens" ดู/สร้าง token, หรือ "Buckets" เพื่อ
สร้าง bucket ใหม่แยกสำหรับ ESP โปรเจกต์นี้ (แนะนำให้แยก ไม่ปนกับ bucket เดิม เช่น
`13301` หรือ `Power AI`) ส่วนชื่อ org ดูได้จากมุมบนซ้ายของหน้า UI หรือใน Settings →
About

## อุปกรณ์ (Device) และสิทธิ์การเข้าถึง

- **admin** เป็นคนสร้าง/ผูกอุปกรณ์ให้ user ผ่านแผง **"จัดการอุปกรณ์"** ในหน้าเว็บ
  (ใส่ `device_id` ที่ตรงกับที่ ESP ส่งมา, ตั้งชื่อเล่น, เลือก user เจ้าของ)
- **user ทั่วไป** จะเห็นเฉพาะอุปกรณ์ของตัวเอง (dropdown เลือกอุปกรณ์ด้านบนกราฟ)
  และตั้งค่าแจ้งเตือนได้เฉพาะอุปกรณ์ของตัวเองเท่านั้น — ไม่เห็นแผงจัดการอุปกรณ์/
  ผู้ใช้ของ admin
- ESP ยังโพสต์ข้อมูลเข้า `/iot/api/sensor` ได้เสมอไม่ว่าจะลงทะเบียนอุปกรณ์ไว้
  หรือยัง (ข้อมูลจะเข้า InfluxDB ปกติ) แต่ต้องให้ admin เพิ่ม/ผูก device นั้นกับ
  user ก่อน ข้อมูลถึงจะไปโผล่ในหน้าเว็บของ user คนนั้น

## แจ้งเตือน Telegram เมื่ออุณหภูมิ/ความชื้นเกินกำหนด

- ตั้งค่า `TELEGRAM_BOT_TOKEN` ใน `.env` (เอา token จากการคุยกับ `@BotFather`
  ใน Telegram → `/newbot`)
- ที่เหลือ (chat ID ปลายทาง, ค่า threshold อุณหภูมิ/ความชื้น, เปิด/ปิด, ระยะ
  เว้นแจ้งเตือนซ้ำ) ตั้งค่าได้ที่หน้าเว็บ `http://<host>:<API_PORT>/iot/` ใน
  แผง **"ตั้งค่าแจ้งเตือน Telegram"** — ตั้งได้ **แยกต่ออุปกรณ์** ตาม dropdown
  เลือกอุปกรณ์ด้านบน (ทุกคนตั้งของตัวเองได้ ไม่ต้องเป็น admin แต่ต้องเป็น
  เจ้าของอุปกรณ์นั้น หรือเป็น admin)
- หา chat ID ได้โดยส่งข้อความหาบอทที่สร้างไว้ก่อน แล้วเปิด
  `https://api.telegram.org/bot<TOKEN>/getUpdates` ดู `chat.id` หรือใช้บอท
  อย่าง `@userinfobot` ก็ได้
- มีปุ่ม **"ส่งข้อความทดสอบ"** ในหน้าเว็บ เพื่อเช็คว่า token/chat id ถูกต้อง
  ก่อนใช้งานจริง

พฤติกรรมการแจ้งเตือน:
- แจ้งเตือน **ครั้งเดียว** ตอนค่าข้ามเกณฑ์ที่ตั้งไว้ (ไม่สแปมทุกครั้งที่ ESP
  ส่งข้อมูลเข้ามา)
- ถ้ายังเกินอยู่ต่อเนื่อง จะไม่แจ้งซ้ำจนกว่าจะครบ "ระยะเว้นแจ้งเตือนซ้ำ" ที่ตั้งไว้
- พอค่ากลับเข้าสู่ช่วงปกติ จะส่งข้อความแจ้ง "กลับสู่ระดับปกติแล้ว" ให้อีกครั้ง
- ถ้า Telegram ส่งไม่ได้ (เช่น token ผิด) จะไม่กระทบการรับข้อมูล sensor —
  ระบบยังเขียนลง InfluxDB ได้ตามปกติเสมอ

### กันแจ้งถี่ตอนค่าแกว่งใกล้เส้น (Hysteresis)

ถ้าค่าแกว่งอยู่ชิดเส้นเกณฑ์พอดี (เช่น 29, 30, 29, 30 สลับกันไปเรื่อยๆ) การมีเกณฑ์
เส้นเดียวจะทำให้แจ้ง "เกิน" สลับกับ "กลับปกติ" ถี่เกินไป ในหน้าเว็บมีช่องเพิ่ม
**"กลับปกติเมื่อต่ำกว่า/สูงกว่า..."** แยกจากเกณฑ์แจ้งเตือนหลัก:

- เกณฑ์แจ้งเตือน (เช่น "อุณหภูมิสูงกว่า 30") = จุดที่เริ่มแจ้งเตือน
- เกณฑ์กลับปกติ (เช่น "กลับปกติเมื่อต่ำกว่า 28") = จุดที่ต้องลดลงมาถึงจริงๆ
  ระบบถึงจะถือว่า "หายแล้ว" และพร้อมแจ้งเตือนรอบใหม่

ตราบใดที่ค่ายังไม่ลดต่ำกว่าเกณฑ์กลับปกติ ระบบจะไม่แจ้งซ้ำเลยแม้จะเช็คทุกครั้งที่มี
ข้อมูลเข้ามา — ถ้าเว้นช่องนี้ว่างไว้ จะใช้ค่าเดียวกับเกณฑ์แจ้งเตือน (พฤติกรรมเดิม
ไม่มี hysteresis)

### ลิงก์ "ดูกราฟ" แนบในข้อความแจ้งเตือน (เข้าได้ทันที ไม่ต้อง login)

ตั้งค่า `APP_PUBLIC_URL` ใน `.env` เป็น URL ที่เปิดหน้าเว็บนี้ได้จากข้างนอก (เช่น
`http://<server-ip>:<API_PORT>`) แล้วทุกข้อความแจ้งเตือนจะแนบ**ลิงก์สั้น** พาไปหน้า
กราฟของอุปกรณ์นั้นโดยตรง (เช่น `http://.../iot/s/IxmqgFk3` ยาวแค่ ~40 ตัวอักษร
แทนที่จะเป็น URL เต็มที่มี token ยาวเป็นร้อยตัวอักษร) กดแล้วเซิร์ฟเวอร์จะ redirect
ไปหน้าเต็มที่มี token พิเศษฝังอยู่ในลิงก์ ทำให้กดแล้วเข้าถึงทันที **ไม่ต้อง login** เลย

token นี้ไม่ใช่ "ข้ามระบบความปลอดภัยไปเฉยๆ" แต่เป็น token ที่ถูกจำกัดสิทธิ์ไว้
ตั้งแต่สร้าง:

- ใช้ได้กับ **อุปกรณ์เครื่องที่ระบุไว้ในลิงก์เท่านั้น** เครื่องอื่นเข้าไม่ได้แม้จะ
  แก้ URL เอง (เช็คฝั่ง server)
- ดูกราฟ **และแก้ threshold/Telegram chat id ของอุปกรณ์นั้นได้** (สะดวกเวลา
  อยากปรับค่าทันทีตอนเห็นแจ้งเตือน) แต่แตะ user คนอื่น, อุปกรณ์เครื่องอื่น,
  หรือหน้าจัดการ admin **ไม่ได้เลย** (ได้ 401/403 ทันทีถ้าพยายาม)
- **หมดอายุอัตโนมัติใน 24 ชั่วโมง** (ปรับได้ด้วย `DEVICE_VIEW_LINK_HOURS` ใน `.env`)
  ทุกครั้งที่มี alert ใหม่จะได้ token ใหม่เสมอ

ถ้าเบราว์เซอร์ที่กดลิงก์ login เต็มสิทธิ์ (username/password) ค้างอยู่แล้วในนั้น
ระบบจะใช้สิทธิ์เต็มของ session เดิมแทน (เห็นได้มากกว่า token ในลิงก์) — token
ในลิงก์เป็นแค่ทางเลือกสำรองไว้ตอนที่เบราว์เซอร์ยังไม่เคย login เท่านั้น

เว้น `APP_PUBLIC_URL` ว่างไว้ถ้าไม่ต้องการแนบลิงก์เลย

## หน้าเว็บ (Dashboard)

เปิดที่ **`http://<host>:<API_PORT>/iot/`** (ค่าเริ่มต้น `API_PORT=3005` → `http://<host>:3005/iot/`)

- หน้า login (username/password → ได้ JWT เก็บใน localStorage)
- กราฟอุณหภูมิ และกราฟความชื้น (เลือก device / ช่วงเวลาได้)
- ตารางข้อมูลล่าสุด
- แผงตั้งค่าแจ้งเตือน Telegram สำหรับอุปกรณ์ที่เลือกอยู่ (ทุกคนใช้ได้ ไม่ใช่แค่ admin)
- ถ้า login เป็น `admin` จะเห็นเพิ่ม: แผงจัดการอุปกรณ์ (ผูกอุปกรณ์กับ user) และ
  แผงจัดการผู้ใช้ (เพิ่ม user ใหม่ + ปุ่มดาวน์โหลด `users.csv`)
- **อัปเดตอัตโนมัติ**: มี dropdown เลือกความถี่ (ปิด / ทุก 10 วิ / 30 วิ / 1 นาที)
  ข้างปุ่มรีเฟรช กราฟ/ตารางจะรีเฟรชเองตามรอบที่ตั้งไว้ โดยจะหยุดอัตโนมัติถ้าสลับ
  แท็บ/ปิดหน้าจอไป (ประหยัดแบต/เน็ต) แล้วกลับมาทำงานต่อเองเมื่อกลับมาที่หน้านี้
- **รองรับมือถือ**: ปรับ layout ให้ element ต่างๆ เรียงเต็มความกว้างจอบนมือถือ
  (viewport แคบกว่า 640px) ปุ่ม/ช่องกรอกใหญ่พอสำหรับนิ้วสัมผัส

กราฟใช้ Chart.js ที่**เก็บไว้ในเซิร์ฟเวอร์เราเอง** (`app/static/vendor/chart.umd.js`
+ `chartjs-adapter-date-fns.bundle.min.js` สำหรับแกนเวลา) ไม่ได้โหลดจาก CDN
ภายนอกแล้ว — กันปัญหากราฟไม่ขึ้นตอนเปิดผ่านเครือข่าย/เบราว์เซอร์ที่บล็อกการโหลด
จากโดเมนภายนอก (เช่น in-app browser บางแอป) แกนเวลาบนกราฟจะโชว์เฉพาะเวลา
(ไม่มีวันที่) เว้นระยะห่างประมาณ 1 ชั่วโมงต่อขีดอัตโนมัติ ครอบคลุมเต็มช่วงเวลาที่
เลือกไว้ (24 ชม. หรือทั้งวันที่เลือกจากปฏิทิน)

## ทดสอบก่อนขึ้นจริง

ต้องแก้ `.env` ให้ชี้ไปที่ InfluxDB จริงก่อน (ดูหัวข้อ "InfluxDB ที่ใช้" ด้านบน —
ใส่ `INFLUX_URL`, `INFLUX_ORG`, `INFLUX_BUCKET`, `INFLUX_TOKEN` ให้ครบ) แล้วรัน:

```bash
cd esp-monitor
docker compose up -d --build
```

เปิดหน้าเว็บ:

```
http://localhost:3005/iot/
```

login ด้วย `admin` / ค่าใน `DEFAULT_ADMIN_PASSWORD` หรือรัน smoke test ผ่าน curl:

```bash
./scripts/test_local.sh
```

สคริปต์นี้จะ login, ยิงข้อมูล sensor จำลอง, ดึงข้อมูลกลับมา, และ export users.csv
ให้ดูว่า API ทำงานครบทุก endpoint ไหม (ข้อมูล sensor จะเข้าไปเขียนจริงใน InfluxDB
ที่ `innovation.ntplc.co.th` ด้วย เพราะไม่มี InfluxDB แยกไว้ทดสอบต่างหากแล้ว)

ปิดการทดสอบ:

```bash
docker compose down
```

> ⚠️ ค่าอื่นที่ไม่ใช่ InfluxDB (`JWT_SECRET`, `DEVICE_API_KEY`, `DEFAULT_ADMIN_PASSWORD`)
> ถ้ายังเป็นค่า dummy อยู่ ต้องเปลี่ยนก่อนขึ้น production จริงด้วย

## วิธีติดตั้งบนเซิร์ฟเวอร์จริง

1. คัดลอกโฟลเดอร์นี้ขึ้นเซิร์ฟเวอร์ แล้วแก้ค่าใน `.env` ทั้งหมดให้เป็นความลับจริง
   (token, password, secret ต่าง ๆ — ห้ามใช้ค่า dummy ที่มากับ repo)

   ```bash
   nano .env
   ```

2. ถ้ายังมี container InfluxDB เก่าของเราเองอยู่ (เช่นจากที่เคยทดสอบก่อนหน้านี้)
   ลบทิ้งได้เลย เพราะโปรเจกต์นี้ไม่ได้รัน InfluxDB ของตัวเองแล้ว:

   ```bash
   docker rm -f esp-monitor-influxdb
   docker volume rm esp-monitor_influx-data   # ถ้าไม่ต้องการข้อมูลเก่าที่เคยทดสอบไว้
   ```

3. สั่งสร้างและรัน container (มีแค่ `esp-monitor-api` ตัวเดียวแล้ว
   `--remove-orphans` ช่วยเคลียร์ container เก่าที่ไม่ได้อยู่ใน compose ไฟล์แล้ว
   ให้อัตโนมัติด้วย)

   ```bash
   docker compose up -d --build --remove-orphans
   ```

4. ตรวจสอบว่า container รันอยู่

   ```bash
   docker ps
   docker logs -f esp-monitor-api
   ```

   ควรเห็นแถวคล้ายๆ:
   ```
   esp-monitor-api   ...   Up ...   0.0.0.0:3005->8000/tcp   esp-monitor-api
   ```

## Endpoints (ทั้งหมดอยู่ใต้ `/iot`)

### 1) ESP32 → ส่งค่าอุณหภูมิ/ความชื้น (POST เดียว, เขียนลง InfluxDB ในตัวเดียวกัน)

```
POST /iot/api/sensor
Header: X-API-Key: <DEVICE_API_KEY ที่ตั้งไว้ใน .env>
Body (JSON):
{
  "device_id": "esp32-01",
  "temperature": 29.4,
  "humidity": 65.2
}
```

ตัวอย่างโค้ดฝั่ง ESP32 (Arduino, HTTPClient):

```cpp
HTTPClient http;
http.begin("http://<SERVER_IP>:3005/iot/api/sensor");
http.addHeader("Content-Type", "application/json");
http.addHeader("X-API-Key", "change-this-device-key");
String body = "{\"device_id\":\"esp32-01\",\"temperature\":29.4,\"humidity\":65.2}";
int code = http.POST(body);
```

### 2) Login (ออก JWT token) — ใช้ทั้งหน้าเว็บและเรียกตรงก็ได้

```
POST /iot/api/login
Body: {"username": "admin", "password": "<DEFAULT_ADMIN_PASSWORD>"}
→ {"access_token": "...", "token_type": "bearer"}
```

user `admin` จะถูกสร้างอัตโนมัติตอน container เริ่มทำงานครั้งแรก
โดยใช้รหัสผ่านจาก `DEFAULT_ADMIN_PASSWORD` ใน `.env`

### 3) ดึงข้อมูลย้อนหลังไปแสดงกราฟ (ต้องมี token — user ทั่วไปดูได้แค่อุปกรณ์ตัวเอง)

```
GET /iot/api/sensor/data?hours=24&device_id=esp32-01
Header: Authorization: Bearer <token>
```
ถ้าไม่ใส่ `device_id` จะได้ข้อมูลรวมของทุกอุปกรณ์ที่ user คนนั้นเป็นเจ้าของ
(admin ไม่ใส่ device_id จะได้ข้อมูลทุกอุปกรณ์ในระบบ)

**ดูข้อมูลของวันใดวันหนึ่งย้อนหลัง (แทนที่ `hours`)** — ในหน้าเว็บมีช่อง
"หรือเลือกวันที่" (ปฏิทิน) ให้เลือกดูข้อมูลเฉพาะวันนั้นแทนช่วงเวลาล่าสุด เบื้องหลัง
เรียก endpoint เดียวกันนี้แต่ใส่ `start`/`stop` แทน `hours`:

```
GET /iot/api/sensor/data?device_id=esp32-01&start=2026-09-15T00:00:00Z&stop=2026-09-16T00:00:00Z
```
`start`/`stop` ต้องเป็น RFC3339 UTC (ลงท้าย `Z`) ทั้งคู่ — หน้าเว็บคำนวณจากวันที่
เลือกในปฏิทินให้เอง โดยยึดตาม**เขตเวลาของเบราว์เซอร์ผู้ดู** ไม่ใช่ของเซิร์ฟเวอร์
(กดวันที่ 15 ก.ย. จากเมืองไทย → ระบบดึงข้อมูลตั้งแต่เที่ยงคืนถึงเที่ยงคืนตามเวลา
ไทยจริงๆ ไม่ใช่เที่ยงคืน UTC)

**ดึงแค่ค่าล่าสุด (สำหรับเชื่อมกับระบบภายนอก)** — คืนแค่ 3 ฟิลด์ ไม่มี timestamp:

```
GET /iot/api/sensor/latest?device_id=esp32-01
Header: Authorization: Bearer <token>
→ {"device_id": "esp32-01", "temperature": 26.5, "humidity": 70.0}
```
ไม่ใส่ `device_id` จะได้ array ของทุกอุปกรณ์ที่เข้าถึงได้แทน (สิทธิ์การเข้าถึงกฎ
เดียวกับ endpoint ด้านบนทุกอย่าง — user ทั่วไปเห็นแค่อุปกรณ์ตัวเอง)

### 4) จัดการอุปกรณ์ (admin เท่านั้น — ทำผ่านหน้าเว็บก็ได้)

```
GET    /iot/api/devices             # รายการอุปกรณ์ทั้งหมด + เจ้าของ
POST   /iot/api/devices             # สร้าง/แก้ไขอุปกรณ์ + ผูกกับ user
Body: {"device_id": "esp32-01", "name": "ตู้เย็น", "owner_username": "somchai"}
DELETE /iot/api/devices/{device_id} # ลบอุปกรณ์ออกจากระบบ
Header: Authorization: Bearer <admin token>
```

```
GET /iot/api/devices/mine
Header: Authorization: Bearer <token>
```
ทุก user เรียกได้ — คืนเฉพาะอุปกรณ์ของตัวเอง (admin จะได้ทั้งหมด)

### 5) สร้าง user เพิ่ม (admin เท่านั้น — ทำผ่านหน้าเว็บก็ได้)

```
POST /iot/api/users
Header: Authorization: Bearer <admin token>
Body: {"username": "somchai", "password": "xxxx", "role": "user"}
```

### 5.1) สมัครสมาชิกด้วยตัวเอง (ไม่ต้อง login — มีปุ่ม "สมัครสมาชิก" ในหน้าเว็บแล้ว)

```
POST /iot/api/register
Body: {"username": "somchai", "password": "xxxxxx"}   # password อย่างน้อย 6 ตัวอักษร
→ {"access_token": "...", "token_type": "bearer"}      # login ให้อัตโนมัติ
```

เปิดให้ใครก็สมัครได้ (ไม่ต้องมี token) แต่**ได้สิทธิ์ `user` เสมอ ไม่มีทางสมัครเป็น
admin ได้เลย** (endpoint นี้ไม่รับฟิลด์ `role` แม้จะพยายามส่งมาก็ถูกเพิกเฉย) และ
**ไม่มีอุปกรณ์ผูกไว้ให้เลยตอนสมัคร** ต้องรอ admin ไปผูกอุปกรณ์ให้ในแผง "จัดการ
อุปกรณ์" ก่อน ถึงจะเห็นข้อมูลอะไรในแดชบอร์ด

### 6) Export ตาราง user เป็น .csv (admin เท่านั้น — มีปุ่มในหน้าเว็บแล้ว)

```
GET /iot/api/users/export
Header: Authorization: Bearer <admin token>
→ ดาวน์โหลดไฟล์ users.csv
```

### 7) ตั้งค่า/ทดสอบแจ้งเตือน Telegram ต่ออุปกรณ์ (เจ้าของอุปกรณ์ หรือ admin — มีหน้าฟอร์มในเว็บแล้ว)

```
GET  /iot/api/settings/alerts/{device_id}        # ดูค่าปัจจุบันของอุปกรณ์นั้น
POST /iot/api/settings/alerts/{device_id}        # บันทึกค่าใหม่
POST /iot/api/settings/alerts/{device_id}/test   # ส่งข้อความทดสอบไปยัง chat id ที่ตั้งไว้
Header: Authorization: Bearer <token>
```
user ทั่วไปเรียกได้เฉพาะ `device_id` ที่ตัวเองเป็นเจ้าของ (ไม่งั้นได้ 403)

### 8) หน้าเว็บแดชบอร์ด

```
GET /iot/
```

## หมายเหตุ

- ข้อมูล user เก็บใน SQLite ที่ `./data/app.db` (persist ผ่าน volume `./data:/data`)
- ข้อมูล sensor เก็บใน InfluxDB ที่ `innovation.ntplc.co.th` (คนละเซิร์ฟเวอร์ ไม่มี
  volume ของเราเองแล้ว — สำรองข้อมูลต้องทำฝั่ง InfluxDB นั้นแทน)
- ถ้าต้องการเปลี่ยนพอร์ต host ให้แก้แค่บรรทัด `API_PORT=` ใน `.env` แล้วสั่ง
  `docker compose up -d` ใหม่ (container ข้างในคงที่ที่ 8000 เสมอ ไม่ต้องแก้
  `docker-compose.yml`)
