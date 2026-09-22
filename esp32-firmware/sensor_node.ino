#include <WiFi.h>
#include <HTTPClient.h>

// ===================================================================
// ส่วนที่ "ต้องแก้" ให้ตรงกับของจริง 5 อย่าง
// ===================================================================

// 1) ชื่อ WiFi และรหัสผ่าน
const char* WIFI_SSID     = "ใส่ชื่อ WiFi ของคุณ";
const char* WIFI_PASSWORD = "ใส่รหัสผ่าน WiFi";

// 2) IP และพอร์ตของเซิร์ฟเวอร์ (ต้องตรงกับ APP_PUBLIC_URL / API_PORT ใน .env)
const char* SERVER_HOST = "192.168.20.9";
const int   SERVER_PORT = 3005;

// 3) device_id - ต้องตรงกับที่ admin สร้าง/ผูกไว้ในหน้าเว็บ /iot/ เป๊ะๆ
const char* DEVICE_ID = "esp32-01";

// 4) API key - ต้องตรงกับ DEVICE_API_KEY ใน .env ของเซิร์ฟเวอร์
const char* DEVICE_API_KEY = "local-dev-device-key";

// 5) ส่งทุกกี่มิลลิวินาที (30000 = 30 วินาที)
const unsigned long SEND_INTERVAL_MS = 30000;

// ===================================================================
// ไม่ต้องแก้ด้านล่างนี้ (ยกเว้นจุดอ่านค่าเซนเซอร์ที่ทำเครื่องหมายไว้)
// ===================================================================

unsigned long lastSend = 0;

void setup() {
  Serial.begin(115200);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("กำลังเชื่อมต่อ WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nเชื่อมต่อ WiFi สำเร็จ, IP ของ ESP32: " + WiFi.localIP().toString());
}

void loop() {
  unsigned long now = millis();
  if (now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;
    sendSensorData();
  }
}

// -------------------------------------------------------------------
// TODO: ตรงนี้แหละที่ต้องใส่โค้ดอ่านค่าจากเซนเซอร์จริงทีหลัง
// ตอนนี้ใส่ค่าจำลอง (random) ไปก่อน เพื่อให้เห็นว่าระบบทั้งหมดทำงานได้
// พอมีเซนเซอร์จริง แค่แก้ 2 บรรทัดนี้ให้ไปอ่านค่าจริงแทน
// -------------------------------------------------------------------
float readTemperature() {
  return 25.0 + random(-50, 100) / 10.0;  // ค่าจำลอง 20.0 - 35.0 องศา
}

float readHumidity() {
  return 55.0 + random(-100, 100) / 10.0; // ค่าจำลอง 45.0 - 65.0 %
}

// -------------------------------------------------------------------
// ส่วนส่งข้อมูล - ไม่ต้องแก้อะไรตรงนี้
// -------------------------------------------------------------------
void sendSensorData() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi หลุด ข้ามรอบนี้ไปก่อน");
    return;
  }

  float temperature = readTemperature();
  float humidity    = readHumidity();

  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + "/iot/api/sensor";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", DEVICE_API_KEY);

  String body = String("{\"device_id\":\"") + DEVICE_ID +
                "\",\"temperature\":" + String(temperature, 1) +
                ",\"humidity\":" + String(humidity, 1) + "}";

  Serial.println("กำลังส่ง: " + body);
  int httpCode = http.POST(body);

  if (httpCode > 0) {
    Serial.printf("ส่งสำเร็จ - HTTP %d\n", httpCode);
    Serial.println("server ตอบ: " + http.getString());
  } else {
    Serial.printf("ส่งไม่สำเร็จ - error: %s\n", http.errorToString(httpCode).c_str());
  }

  http.end();
}
