#include <SPI.h>
#include <MFRC522.h>
#include <WiFi.h>
#include <HTTPClient.h>

#define RST_PIN  22
#define SS_PIN   5

MFRC522 mfrc522(SS_PIN, RST_PIN);

// WiFi
const char* ssid     = "cybarry";
const char* password = "cybarryinc";

// Server
String serverRFID = "http://192.168.0.123:5000/api/rfid";
String apiKey     = "cybarry";

void connectWiFi() {
  Serial.printf("Connecting to %s ...\n", ssid);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\nWiFi connected. IP: %s\n", WiFi.localIP().toString().c_str());
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("Booting RFID Node...");
  SPI.begin();
  mfrc522.PCD_Init();
  connectWiFi();
  mfrc522.PCD_DumpVersionToSerial();

  Serial.println("RFID reader ready. Swipe a card...");
  //Serial.println("RFID reader ready.");
}

void loop() {
  // Look for card
  if (!mfrc522.PICC_IsNewCardPresent()) return;
  if (!mfrc522.PICC_ReadCardSerial()) return;

  // Get UID
  String uid = "";
  for (byte i = 0; i < mfrc522.uid.size; i++) {
    if (mfrc522.uid.uidByte[i] < 0x10) uid += "0"; // pad 0
    uid += String(mfrc522.uid.uidByte[i], HEX);
    if (i != mfrc522.uid.size - 1) uid += ":";
  }
  uid.toUpperCase();
  Serial.printf("Card detected: %s\n", uid.c_str());

  // Send to server
  HTTPClient http;
  http.begin(serverRFID);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", apiKey);

  String payload = "{\"uid\":\"" + uid + "\"}";
  int code = http.POST(payload);

  if (code > 0) {
    String resp = http.getString();
    Serial.printf("Server response: %s\n", resp.c_str());
  } else {
    Serial.printf("HTTP error: %d\n", code);
  }
  http.end();

  delay(2000); // debounce
}
