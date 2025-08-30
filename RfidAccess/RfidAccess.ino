/* RFIDLockNode.ino
   ESP32-S DevKit: MFRC522 + Servo lock controller + small webserver
*/
#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <SPI.h>
#include <MFRC522.h>
#include <ESP32Servo.h>
#include <ArduinoJson.h>

// WiFi
const char* WIFI_SSID = "cybarry";
const char* WIFI_PASS = "cybarryinc";

// Flask backend
const char* SERVER_BASE = "http://192.168.0.123:5000";
const char* API_KEY    = "cybarry";

// RC522 pins
#define SS_PIN   5
#define RST_PIN  22
#define SPI_SCK  18
#define SPI_MISO 19
#define SPI_MOSI 23
MFRC522 mfrc522(SS_PIN, RST_PIN);

// Servo
#define SERVO_PIN 13
Servo doorServo;
const int LOCK_POS = 0;
const int UNLOCK_POS = 90;
const unsigned long UNLOCK_TIME_MS = 10000;

// Web server to accept unlock calls from CAM
WebServer server(8080);

// Status LED
#define LED_PIN 2
void ledBlink(int n, int d=80){
  for (int i=0;i<n;i++){ digitalWrite(LED_PIN,HIGH); delay(d); digitalWrite(LED_PIN,LOW); delay(d); }
}

// state
unsigned long unlockStart = 0;
bool doorUnlocked = false;

void handleUnlock() {
  Serial.println("✅ /unlock called (from CAM or manual).");
  if (!doorUnlocked) {
    doorServo.write(UNLOCK_POS);
    doorUnlocked = true;
    unlockStart = millis();
    ledBlink(2,60);
    Serial.println("Door UNLOCKED by /unlock");
  } else {
    Serial.println("Door already unlocked.");
  }
  server.send(200, "application/json", "{\"status\":\"ok\"}");
}

String uidToString(MFRC522::Uid *uid) {
  String s;
  for (byte i=0;i<uid->size;i++){
    if (uid->uidByte[i] < 0x10) s += "0";
    s += String(uid->uidByte[i], HEX);
    if (i+1 < uid->size) s += ":";
  }
  s.toUpperCase();
  return s;
}

bool callBackendRFID(const String& uid, String &outResp) {
  WiFiClient client;
  HTTPClient http;
  String url = String(SERVER_BASE) + "/api/rfid";
  if (!http.begin(client, url)) {
    Serial.println("HTTP begin failed (backend rfid)");
    return false;
  }
  http.addHeader("Content-Type","application/json");
  http.addHeader("X-API-Key", API_KEY);
  String body = String("{\"uid\":\"") + uid + "\"}";
  int code = http.POST(body);
  if (code <= 0) {
    Serial.printf("POST failed: %d\n", code);
    http.end();
    return false;
  }
  outResp = http.getString();
  http.end();
  Serial.printf("Backend %d: %s\n", code, outResp.c_str());
  return (code == 200);
}

void unlockDoorLocal() {
  doorServo.write(UNLOCK_POS);
  doorUnlocked = true;
  unlockStart = millis();
  Serial.println("Door UNLOCKED (local)");
}

void maybeLockDoor() {
  if (doorUnlocked && (millis() - unlockStart >= UNLOCK_TIME_MS)) {
    doorServo.write(LOCK_POS);
    doorUnlocked = false;
    Serial.println("Door LOCKED (local)");
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT); digitalWrite(LED_PIN, LOW);

  Serial.println("Booting RFID & Lock Node...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("Connecting to %s ...\n", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nWiFi connected. IP: %s\n", WiFi.localIP().toString().c_str());

  SPI.begin(SPI_SCK, SPI_MISO, SPI_MOSI, SS_PIN);
  mfrc522.PCD_Init();
  delay(50);
  byte v = mfrc522.PCD_ReadRegister(mfrc522.VersionReg);
  Serial.printf("MFRC522 Version: 0x%02X\n", v);

  // servo init
  doorServo.attach(SERVO_PIN);
  doorServo.write(LOCK_POS);

  // webserver
  server.on("/unlock", HTTP_GET, handleUnlock);
  server.begin();
  Serial.println("Lock controller HTTP server started on port 8080");

  ledBlink(2, 120);
}

void loop() {
  server.handleClient();
  maybeLockDoor();

  if (!mfrc522.PICC_IsNewCardPresent()) { delay(30); return; }
  if (!mfrc522.PICC_ReadCardSerial()) { delay(30); return; }

  String uid = uidToString(&mfrc522.uid);
  Serial.printf("Card detected: %s\n", uid.c_str());

  // send to Flask backend
  String backendResp;
  bool posted = callBackendRFID(uid, backendResp);
  if (posted) {
    // parse JSON
    DynamicJsonDocument doc(256);
    DeserializationError err = deserializeJson(doc, backendResp);
    if (!err) {
      const char* status = doc["status"];
      if (status && String(status) == "granted") {
        Serial.println("Access GRANTED by backend -> unlocking servo.");
        unlockDoorLocal();
        ledBlink(3, 80);
      } else {
        Serial.println("Access DENIED by backend.");
        ledBlink(1, 350);
      }
    } else {
      Serial.println("Failed to parse backend JSON.");
      ledBlink(1, 200);
    }
  } else {
    Serial.println("Failed to POST to backend.");
    ledBlink(1, 200);
  }

  mfrc522.PICC_HaltA();
  mfrc522.PCD_StopCrypto1();
  delay(500);
}
