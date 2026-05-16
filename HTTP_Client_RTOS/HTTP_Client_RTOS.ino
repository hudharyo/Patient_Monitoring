#include <WiFi.h>
#include <HTTPClient.h>
#include "HX711.h"
#include "soc/rtc.h"
#include <Wire.h>
#include "MAX30100_PulseOximeter.h"
#include <math.h>
#include "ADS1X15.h"
// #include "ClosedCube_MAX30205.h"   

/* Sensor USed
- Load Cell --> 
- MAX30100 --> Spo2
- ADS1115 + NTC10K --> Body Temperature
*/

//======= Load cells ============
// HX711 circuit wiring
const int LOADCELL_DOUT_PIN = 15;
const int LOADCELL_SCK_PIN = 2;

HX711 scale;

float Calibration_scale = 626.932 ;
float infus_full = 1000;
float infus_empty = 10;
float infuse_weight = 0;

//======= MAX30100 (SPO2) ============
PulseOximeter pox;

//====== ADS1115 + NTC  (Temperature) =======
ADS1115 ADS(0x48);
float TempC, TempF;
float a = 639.5, b = -0.1332, c = -162.5;
float Rntc, Vntc, Temp; 
float f;

//======= Communications ============
const char* ssid = "Sasugaainzsama";
const char* password = "bangbang123";
// Data
typedef struct {
  float heart_rate;
  float spo2;
  int temperature;
  int infuse_level;
  bool infuse_alert;
} Data;

Data SensorData;
// update time
long interval_read = 500 ;
long interval_send = 5000;
long last_read,last_send;

// const char* serverUrl = "http://192.168.1.9:5000/data";
// const char* serverUrl = "http://10.22.227.52:5000//data";
const char* serverUrl = "http://10.15.245.52:5000/data";
QueueHandle_t dataQueue = NULL;

// Indicator Pin
#define com 12
#define buzz 25

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
  
  //======= Settingup the Load cell ==============
  Serial.println("Initializing the scale");

  scale.begin(LOADCELL_DOUT_PIN, LOADCELL_SCK_PIN);
            
  scale.set_scale(Calibration_scale);
  //scale.set_scale(-471.497);                      // this value is obtained by calibrating the scale with known weights; see the README for details
  scale.tare();               // reset the scale to 0

  //============= Settingup ADS1115 ==============
  Wire.begin();
  ADS.begin();   
  ADS.setGain(0);
  f = ADS.toVoltage(1);  //  voltage factor


  //=============== Settingup MAX30100 =============
    Serial.print("Initializing pulse oximeter..");
    if (!pox.begin()) {
        Serial.println("FAILED");
        for(;;);
    } else {
        Serial.println("SUCCESS");
    }

       // Register a callback for the beat detection
    // pox.setOnBeatDetectedCallback(onBeatDetected);

  //============== RTOS Configuration =============
  dataQueue = xQueueCreate(1, sizeof(SensorData));
  xTaskCreatePinnedToCore(Read_data, "sensor", 8192, NULL, 2, NULL, 1);
  xTaskCreatePinnedToCore(Indikator, "Indikator", 8192, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(Send_HTTP, "http", 12000, NULL, 1, NULL, 0);

  //============== GPIO setup ==========
  pinMode(com, OUTPUT);
  pinMode(buzz, OUTPUT);
  // digitalWrite(com, HIGH);
  // digitalWrite(buzz, HIGH);
  // tone(buzz,1000);
  // delay (250);
  // digitalWrite(com, LOW);
  // tone(buzz,0);
  // digitalWrite(buzz, LOW);
}


void loop() {

  //Kosong

}

float Read_temperature (){
  // Read the temperature
  int16_t val_0 = ADS.readADC(0);  
  float temp;
  Vntc = val_0 * f;
  // Serial.println(Vntc);
  Rntc = 10000.0 * ((3.20/Vntc) - 1);
  temp = a * pow(Rntc, b) + c;
  // Serial.print(Temp);
  // Serial.println("°C \t");
  return temp;
}

// Callback (registered below) fired when a pulse is detected
void onBeatDetected()
{
    Serial.println("Beat!");
}

void Read_data(void *parameter) {
  int read_interval = 10;
  int loop_count = 50;
  for (;;) {
    // Serial.println(loop_count);
      if (loop_count > read_interval){
      Serial.print("Reading data");
      // Reading infuse
      infuse_weight = scale.get_units();
      int infuse= constrain(map(infuse_weight, infus_empty, infus_full, 0,100),0,100);
      SensorData.infuse_level = infuse;
      if (infuse < 10){
        SensorData.infuse_alert = 1;
      }
      else{
        SensorData.infuse_alert = 0;
      }

      // Reading SPo2+HR
      SensorData.heart_rate = pox.getHeartRate();
      SensorData.spo2 = constrain(pox.getSpO2(),0,100);

      // Reading Temperature
      SensorData.temperature = Read_temperature();
      
      xQueueOverwrite(dataQueue, &SensorData); // keep latest value
      print_data();
      loop_count = 0;
    }
    // Serial.printf("potTask: Sent pot value %u\n", potValue);
    pox.update();
    loop_count++;
    vTaskDelay(25 / portTICK_PERIOD_MS);  // 100ms
  }
}

void Send_HTTP(void *parameter){
  int interval = 10;
  int interval_count = 0;
  int httpResponseCode = 0;
  for (;;) {
    if (interval_count > interval ){
      if (xQueueReceive(dataQueue, &SensorData, portMAX_DELAY)){
        if (WiFi.status() == WL_CONNECTED) {
          HTTPClient http;
          http.begin(serverUrl);
          http.addHeader("Content-Type", "application/json");

          String jsonData = "{\"temperature\":";
          jsonData += String(SensorData.temperature) + ",";
          jsonData += "\"heart_rate\":" + String(SensorData.heart_rate)+ ",";
          jsonData += "\"Spo2\":" + String(SensorData.spo2)+ ",";
          jsonData += "\"infuse_level\":" + String(SensorData.infuse_level);
          jsonData += "}";
          httpResponseCode = http.POST(jsonData);

          Serial.print("HTTP Response code: ");
          Serial.println(httpResponseCode);

          http.end();
        }
      }
      interval_count = 0;
    }
    if (httpResponseCode == 200 || httpResponseCode == -11 ){
      if (interval_count<2){
        digitalWrite(com, HIGH);
      }
      else{
        httpResponseCode = 0;
        digitalWrite(com, LOW);
      }     
    }
    vTaskDelay(50 / portTICK_PERIOD_MS);  // 100ms
    interval_count++; 
  }
}

void print_data(){
  Serial.print("HR: ");
  Serial.print(SensorData.heart_rate);

  Serial.print(" | SpO2: ");
  Serial.print(SensorData.spo2);

  Serial.print(" | Temperature: ");
  Serial.print(SensorData.temperature);

  Serial.print(" | Infuse: ");
  Serial.println(SensorData.infuse_level);

}

void Indikator(void *parameter) {
  int update_interval = 10;
  int loop_count = 50;
  for (;;) {
    // Serial.println(loop_count);
      if (loop_count > update_interval){
        if (xQueueReceive(dataQueue, &SensorData, portMAX_DELAY)){
          if (SensorData.infuse_alert){
          tone(buzz,2000,500);
          tone(buzz,0,500);
          }
          loop_count =0;
        }
      }
    loop_count++;
    vTaskDelay(50 / portTICK_PERIOD_MS);  // 100ms
  }
}
