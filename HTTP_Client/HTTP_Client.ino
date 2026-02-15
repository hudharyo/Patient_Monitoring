#include <WiFi.h>
#include <HTTPClient.h>

// const char* ssid = "Wifine Riyo";
// const char* password = "natalia25";

const char* ssid = "Sasugaainzsama";
const char* password = "bangbang123";

int temperature, heart_rate,Spo2, infuse_level;

// const char* serverUrl = "http://192.168.1.9:5000/data";
const char* serverUrl = "http://10.22.227.52:5000//data";

void setup() {
  Serial.begin(115200);
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi connected");
  temperature = 0;
  heart_rate = 100;
  infuse_level = 0;
  Spo2 = 80; 
}

void loop() {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(serverUrl);
    http.addHeader("Content-Type", "application/json");

    String jsonData = "{\"temperature\":";
    jsonData += String(temperature) + ",";
    jsonData += "\"heart_rate\":" + String(heart_rate)+ ",";
    jsonData += "\"Spo2\":" + String(Spo2)+ ",";
    jsonData += "\"infuse_level\":" + String(infuse_level);
    jsonData += "}";
    int httpResponseCode = http.POST(jsonData);

    Serial.print("HTTP Response code: ");
    Serial.println(httpResponseCode);

    http.end();
  }

  if (temperature < 100 ){
    temperature++;
  }
  else{
    temperature = 0;
  }

  if (heart_rate > 0 ){
    heart_rate--;
  }
  else{
    heart_rate = 100;
  }

  if (infuse_level < 150 ){
    infuse_level++;
  }
  else{
    infuse_level = 0;
  }

  if (Spo2 < 180 ){
    Spo2++;
  }
  else{
    Spo2 = 0;
  }

  delay(1000);
}
