#pragma once
// Піни ESP32-C5. Змініть під вашу проводку.
#define BMS_RX_PIN     23   // сюди TX BMS (GPS-роз'єм)
#define BMS_TX_PIN     24   // звідси в RX BMS (якщо немає даних 12 с — пін-и міняються місцями)
// Світлодіоди на платі ESP32-C5-DevKitC-1:
//  - червоний (живлення) апаратний, горить сам;
//  - RGB WS2812 (GPIO27) показує Wi-Fi жовтим: блимає — немає, світить — є.
#define LED_YELLOW_RGB 1
#define LED_BRIGHTNESS 40   // 0..255

#define BMS_BAUD       115200
#define POLL_MS        2000
#define SETTINGS_MS    300000
#define MQTT_BASE      "jkbms"
#define DEVICE_ID      "jkbms_c5"
