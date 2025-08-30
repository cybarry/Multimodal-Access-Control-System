/* FaceAccessPro.ino
   ESP32-CAM: capture -> POST /api/recognize -> if granted, call ESP32-S /unlock
*/
#include <WiFi.h>
#include <HTTPClient.h>
#include "esp_camera.h"
#include "esp_http_server.h"

// WiFi
const char* ssid     = "cybarry";
const char* password = "cybarryinc";

// Backend server (Flask)
String serverRecognize = "http://192.168.0.123:5000/api/recognize";
String serverHealth    = "http://192.168.0.123:5000/api/health";
String apiKey          = "cybarry";     // must match Flask

// Lock controller (ESP32-S) — EDIT THIS to your ESP32-S IP
const char* LOCK_CONTROLLER_IP = "192.168.0.183";
const int   LOCK_CONTROLLER_PORT = 8080;

// Camera and capture settings
#define CAMERA_MODEL_AI_THINKER
#include "camera_pins.h"
const uint32_t CAPTURE_INTERVAL_MS = 1500;
const uint8_t  CAPTURE_RETRIES     = 3;
uint32_t lastCapture = 0;

// Stream server (optional preview)
httpd_handle_t stream_httpd = NULL;

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
    config.frame_size   = FRAMESIZE_QVGA;
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

esp_err_t stream_handler(httpd_req_t *req) {
  camera_fb_t * fb = NULL;
  esp_err_t res = ESP_OK;
  size_t jpg_buf_len = 0;
  uint8_t * jpg_buf = NULL;
  char part_buf[64];
  res = httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=frame");
  if (res != ESP_OK) return res;

  while (true) {
    fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("Camera capture failed in stream_handler");
      res = ESP_FAIL;
    } else {
      if (fb->format != PIXFORMAT_JPEG) {
        bool jpeg_converted = frame2jpg(fb, 80, &jpg_buf, &jpg_buf_len);
        if (!jpeg_converted) {
          Serial.println("JPEG compression failed");
          esp_camera_fb_return(fb);
          res = ESP_FAIL;
        }
      } else {
        jpg_buf_len = fb->len;
        jpg_buf = fb->buf;
      }
      if (res == ESP_OK) {
        size_t hlen = snprintf(part_buf, sizeof(part_buf),
                               "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                               (unsigned)jpg_buf_len);
        res = httpd_resp_send_chunk(req, part_buf, hlen);
      }
      if (res == ESP_OK) {
        res = httpd_resp_send_chunk(req, (const char *)jpg_buf, jpg_buf_len);
      }
      if (res == ESP_OK) {
        res = httpd_resp_send_chunk(req, "\r\n", 2);
      }
      if (fb->format != PIXFORMAT_JPEG) free(jpg_buf);
      esp_camera_fb_return(fb);
    }
    if (res != ESP_OK) break;
  }
  return res;
}

void startCameraServer(){
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 81;
  httpd_uri_t stream_uri = {
    .uri       = "/stream",
    .method    = HTTP_GET,
    .handler   = stream_handler,
    .user_ctx  = NULL
  };
  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
  }
}

// POST a frame to the Flask recognition endpoint
bool postFrame(const uint8_t *buf, size_t len, String &response) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi lost, reconnecting...");
    WiFi.disconnect(true);
    WiFi.begin(ssid, password);
    uint8_t tries = 0;
    while (WiFi.status() != WL_CONNECTED && ++tries < 60) {
      delay(200); Serial.print(".");
    }
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("\nFailed to reconnect WiFi");
      return false;
    }
  }

  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, serverRecognize)) {
    Serial.println("HTTP begin failed (recognize)");
    return false;
  }
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-API-Key", apiKey);

  int code = http.POST((uint8_t*)buf, len);
  if (code > 0) {
    response = http.getString();
    http.end();
    return (code == 200);
  } else {
    Serial.printf("Error in POST to /api/recognize: %d\n", code);
    http.end();
    return false;
  }
}

// Call ESP32-S /unlock with retries
bool callLockControllerUnlock(int retries=3, int timeout_ms=2500) {
  String url = String("http://") + LOCK_CONTROLLER_IP + ":" + String(LOCK_CONTROLLER_PORT) + "/unlock";
  for (int i=0; i<retries; ++i) {
    WiFiClient client;
    HTTPClient http;
    http.setTimeout(timeout_ms / 1000); // seconds-ish; library uses seconds param in begin with client? library has setTimeout
    if (!http.begin(client, url)) {
      Serial.println("HTTP begin failed (unlock)");
      http.end();
      delay(200);
      continue;
    }
    int code = http.GET();
    String resp = (code > 0) ? http.getString() : "";
    http.end();
    Serial.printf("Unlock call attempt %d -> code=%d resp=%s\n", i+1, code, resp.c_str());
    if (code == 200) {
      // optionally parse JSON for status
      if (resp.indexOf("ok") >= 0 || resp.indexOf("status") >= 0) return true;
      return true;
    }
    delay(250);
  }
  return false;
}

void setup() {
  Serial.begin(115200);
  Serial.println();
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  Serial.printf("Connecting to WiFi '%s' ...\n", ssid);
  uint8_t tries = 0;
  while (WiFi.status() != WL_CONNECTED && ++tries < 80) {
    delay(250); Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWiFi connected. IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\nWiFi failed to connect.");
  }

  if (!initCamera()) {
    Serial.println("Camera init failed; restarting in 5s...");
    delay(5000); ESP.restart();
  }

  startCameraServer();
  Serial.println("Camera stream ready on port 81.");

  // Optional server health ping
  WiFiClient client;
  HTTPClient http;
  if (http.begin(client, serverHealth)) {
    http.addHeader("X-API-Key", apiKey);
    int code = http.GET();
    Serial.printf("Server health: %d\n", code);
    http.end();
  }
}

void loop() {
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

  // If server said "granted", call lock controller unlock endpoint
  if (resp.indexOf("\"status\":\"granted\"") >= 0 || resp.indexOf("\"granted\"") >= 0) {
    Serial.println("Face Access GRANTED → Sending unlock to ESP32S...");
    bool uok = callLockControllerUnlock(3, 3000);
    if (uok) {
      Serial.println("Unlock command succeeded.");
    } else {
      Serial.println("Unlock command FAILED after retries.");
    }
  }
}
