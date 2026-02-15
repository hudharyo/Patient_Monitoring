#include <WiFi.h>
#include <HTTPClient.h>
#include "HX711.h"
#include "soc/rtc.h"
#include <Wire.h>
#include "MAX30100_PulseOximeter.h"
#include "ClosedCube_MAX30205.h"   

/* Sensor USed
- Load Cell --> 
- MAX30100 --> Spo2
- MAX30205MTA --> Body Temperature
*/

// const char* ssid = "Wifine Riyo";
// const char* password = "natalia25";

//======= Load cells ============
// HX711 circuit wiring
const int LOADCELL_DOUT_PIN = 16;
const int LOADCELL_SCK_PIN = 4;

HX711 scale;

int Calibration_scale ;
float Infus_full, infus_empty;

//======= MAX30100 (SPO2) ============
PulseOximeter pox;

//====== MAX30205 (Temperature) =======
ClosedCube_MAX30205 max30205;   
float max30205Temp = 0;  

//======= Communications ============
const char* ssid = "Sasugaainzsama";
const char* password = "bangbang123";
// Data
int temperature, heart_rate,Spo2, infuse_level;
// update time
long interval_read = 500 ;
long interval_send = 1000;
long last_read,last_send;

// const char* serverUrl = "http://192.168.1.9:5000/data";
const char* serverUrl = "http://10.22.227.52:5000//data";

void setup() {
  Serial.begin(115200);

  // Slowing down the ESP32 for the load cell
  rtc_cpu_freq_config_t config;
  rtc_clk_cpu_freq_get_config(&config);
  rtc_clk_cpu_freq_to_config(RTC_CPU_FREQ_80M, &config);
  rtc_clk_cpu_freq_set_config_fast(&config);

  // Connecting to Wifi
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi connected");
  
  //Settingup the Load cell
  Serial.println("Initializing the scale");

  scale.begin(LOADCELL_DOUT_PIN, LOADCELL_SCK_PIN);
            
  scale.set_scale(Calibration_scale);
  //scale.set_scale(-471.497);                      // this value is obtained by calibrating the scale with known weights; see the README for details
  scale.tare();               // reset the scale to 0

  //Settingup MAX30100 
    Serial.print("Initializing pulse oximeter..");
    if (!pox.begin()) {
        Serial.println("FAILED");
        for(;;);
    } else {
        Serial.println("SUCCESS");
    }

  //Settingup MAX30205
  max30205.begin(0x4F);    

}

void loop() {
  if (millis() - last_send > interval_send ){
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
    last_send = millis();
  }

  if (millis() - last_read > interval_read){
    // Reading the load cell
    // Serial.print("\t| average:\t");
    // Serial.println(scale.get_units(10), 5);
    float infus_weight = scale.get_units(10);
    infuse_level = map(infus_weight, infus_empty, Infus_full, 0,100);

    // Reading the 30100
   
    // Serial.print("Heart rate:");
    // Serial.print(pox.getHeartRate());
    heart_rate = pox.getHeartRate();
    // Serial.print("bpm / SpO2:");
    // Serial.print(pox.getSpO2());
    // Serial.println("%");
    Spo2 = pox.getSpO2();

    // Reading MAX30305
    temperature = max30205.readTemperature();  

    last_read = millis();
  }
 pox.update();
}
