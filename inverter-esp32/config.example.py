# Налаштування. Wi-Fi тільки 2,4 ГГц.
WIFI_SSID = "CHANGE_ME"
WIFI_PASS = "CHANGE_ME"

# UART до модуля MAX3232 (TTL-бік): модуль TXD -> D7 (GPIO44), модуль RXD -> D6 (GPIO43); плата Seeed XIAO ESP32-S3
UART_ID = 1
PIN_RX = 44
PIN_TX = 43
BAUD = 2400

PIN_LED = 4          # D3 (GPIO4) -> 330 Ом -> світлодіод -> GND
POLL_S = 5           # період опитування QPIGS, с

# MQTT-брокер на QNAP (None у MQTT_HOST вимикає публікацію)
MQTT_HOST = "10.100.202.5"
MQTT_PORT = 1883
MQTT_USER = "CHANGE_ME"
MQTT_PASS = "CHANGE_ME"
MQTT_TOPIC = b"daxtromn/aoh/qpigs"
