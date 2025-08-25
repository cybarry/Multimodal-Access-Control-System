#include <WiFi.h>
#include <HTTPClient.h>
#include "esp_camera.h"

// ====== WiFi ======
const char* ssid     = "cybarry";
const char* password = "cybarryinc";

// ====== Server ======
String serverRecognize = "http://192.168.0.123:5000/api/recognize";
String serverHealth    = "http://192.168.0.123:5000/api/health";
String apiKey          = "cybarry";     // must match server

// ====== Relay ======
// NOTE: On ESP32-CAM AI Thinker, the on-board flash LED is GPIO 4 (via transistor).
// GPIO 2 is usually the small on-board blue LED. If you're driving an external relay,
// GPIO 4 is often the safer choice. Change if needed:
#define RELAY_PIN 2               // or 4 if you're using the flash LED transistor path
const uint32_t UNLOCK_MS = 5000;  // door unlock duration
bool doorUnlocked = false;
uint32_t unlockStart = 0;

// ====== Capture/Retry ======
const uint32_t CAPTURE_INTERVAL_MS = 1500;
const uint8_t  CAPTURE_RETRIES     = 3;

// ====== Camera Model ======
#define CAMERA_MODEL_AI_THINKER
#include "camera_pins.h"

uint32_t lastCapture = 0;

void connectWiFi() {
  Serial.printf("Connecting to WiFi '%s' ...\n", ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  uint8_t tries = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    if (++tries > 40) {
      Serial.println("\nWiFi retry...");
      WiFi.disconnect(true);
      delay(1000);
      WiFi.begin(ssid, password);
      tries = 0;
    }
  }
  Serial.printf("\nWiFi connected. IP: %s\n", WiFi.localIP().toString().c_str());
}

bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    config.frame_size   = FRAMESIZE_QVGA;  // stable & light; SVGA works if needed
    config.jpeg_quality = 10;
    config.fb_count     = 2;
  } else {
    config.frame_size   = FRAMESIZE_QVGA;
    config.jpeg_quality = 12;
    config.fb_count     = 1;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed: 0x%x\n", err);
    return false;
  }
  Serial.println("Camera initialized.");
  return true;
}

bool postFrame(const uint8_t *buf, size_t len, String &response) {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  WiFiClient client;
  HTTPClient http;

  // FIX: use the correct variable name (serverRecognize), and pass client explicitly
  if (!http.begin(client, serverRecognize)) {
    Serial.println("HTTP begin failed");
    return false;
  }

  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-API-Key", apiKey);

  // Cast away const for POST(), safe here because http.POST doesn't modify the buffer
  int code = http.POST((uint8_t*)buf, len);

  if (code > 0) {
    response = http.getString();
    http.end();
    return (code == 200);
  } else {
    Serial.printf("Error in POST: %d\n", code);
    http.end();
    return false;
  }
}

void unlockDoor() {
  digitalWrite(RELAY_PIN, HIGH);
  doorUnlocked = true;
  unlockStart = millis();
  Serial.println("Door UNLOCKED.");
}

void lockDoorIfTime() {
  if (doorUnlocked && (millis() - unlockStart >= UNLOCK_MS)) {
    digitalWrite(RELAY_PIN, LOW);
    doorUnlocked = false;
    Serial.println("Door LOCKED.");
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);

  // IDE: Tools → PSRAM: Enabled, Partition: Huge APP
  connectWiFi();
  if (!initCamera()) {
    Serial.println("Restarting in 5s due to camera fail...");
    delay(5000);
    ESP.restart();
  }

  // Optional health ping
  WiFiClient client;
  HTTPClient http;
  if (http.begin(client, serverHealth)) {
    http.addHeader("X-API-Key", apiKey);
    int code = http.GET();
    Serial.printf("Server health: %d\n", code);
    http.end();
  } else {
    Serial.println("Health check: HTTP begin failed");
  }
}

void loop() {
  lockDoorIfTime();

  if (millis() - lastCapture < CAPTURE_INTERVAL_MS) return;
  lastCapture = millis();

  // Capture with retries
  camera_fb_t* fb = nullptr;
  for (uint8_t i = 0; i < CAPTURE_RETRIES; i++) {
    fb = esp_camera_fb_get();
    if (fb) break;
    Serial.println("Capture failed, retrying...");
    delay(250);
  }
  if (!fb) return;

  String resp;
  bool ok = postFrame(fb->buf, fb->len, resp);
  esp_camera_fb_return(fb);

  if (!ok) {
    Serial.println("Recognition request failed.");
    return;
  }

  Serial.printf("Server: %s\n", resp.c_str());
  if (resp.indexOf("granted") >= 0) {
    if (!doorUnlocked) unlockDoor();
  }
}
