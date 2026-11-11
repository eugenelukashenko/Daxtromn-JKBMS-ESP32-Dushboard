# Daxtromn AOH-3.2KVA (PI30, 2400 8N1) -> ESP32-S3 -> Wi-Fi/MQTT
# Світлодіод: немає Wi-Fi - мигає 1 раз на секунду, є Wi-Fi - світиться постійно.
import time, json, network
from machine import UART, Pin, Timer
import config

led = Pin(config.PIN_LED, Pin.OUT)
wlan = network.WLAN(network.STA_IF)
wlan.active(True)

def led_tick(_t):
    if wlan.isconnected():
        led.value(1)
    else:
        led.value(not led.value())      # період таймера 500 мс = 1 Гц

led_timer = Timer(0)
led_timer.init(period=500, mode=Timer.PERIODIC, callback=led_tick)

uart = UART(config.UART_ID, baudrate=config.BAUD, bits=8, parity=None, stop=1,
            tx=config.PIN_TX, rx=config.PIN_RX, timeout=100)

def crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc

def _fix(x):
    return x + 1 if x in (0x28, 0x0D, 0x0A) else x

def query(cmd, timeout_ms=2000):
    """Шле команду з CRC, повертає тіло відповіді (str без '(') або None."""
    while uart.any():
        uart.read()
    c = crc16(cmd)
    uart.write(cmd + bytes((_fix(c >> 8), _fix(c & 0xFF))) + b"\r")
    buf = b""
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < timeout_ms:
        d = uart.read()
        if d:
            buf += d
            if buf.endswith(b"\r"):
                break
        else:
            time.sleep_ms(10)
    if len(buf) < 4 or buf[0] != 0x28 or not buf.endswith(b"\r"):
        return None
    body, rx = buf[:-3], buf[-3:-1]
    c = crc16(body)
    if bytes((_fix(c >> 8), _fix(c & 0xFF))) != rx:
        return None
    return body[1:].decode()

NAMES = ("grid_v", "grid_hz", "out_v", "out_hz", "out_va", "out_w", "load_pct",
         "bus_v", "batt_v", "batt_chg_a", "batt_pct", "temp_c", "pv_a", "pv_v",
         "batt_scc_v", "batt_dis_a", "status", "fan_offset", "eeprom_ver",
         "pv_w", "flags")

def parse_qpigs(s):
    p = s.split()
    d = {}
    for i, n in enumerate(NAMES):
        if i < len(p):
            d[n] = p[i] if n in ("status", "flags") else float(p[i])
    return d

SKIP_TOPICS = ("fan_offset", "eeprom_ver", "flags")   # ці поля окремими топіками не публікуємо
mqtt = None
mqtt_next = 0

def mqtt_publish(payload, data):
    global mqtt, mqtt_next
    if not config.MQTT_HOST or not wlan.isconnected():
        return
    try:
        if mqtt is None:
            if time.ticks_diff(time.ticks_ms(), mqtt_next) < 0:
                return
            from umqtt.simple import MQTTClient
            mqtt = MQTTClient("aoh-esp32s3", config.MQTT_HOST, config.MQTT_PORT,
                              config.MQTT_USER, config.MQTT_PASS, keepalive=60)
            mqtt.connect()
        mqtt.publish(config.MQTT_TOPIC, payload, retain=True, qos=0)
        base = config.MQTT_TOPIC.rsplit(b"/", 1)[0]      # daxtromn/aoh
        for k, v in data.items():
            if k in SKIP_TOPICS:
                continue
            mqtt.publish(base + b"/" + k.encode(), str(v), retain=True, qos=0)
    except Exception as e:
        print("MQTT error:", e)
        mqtt = None
        mqtt_next = time.ticks_add(time.ticks_ms(), 30000)

wifi_next = 0
while True:
    now = time.ticks_ms()
    if not wlan.isconnected() and time.ticks_diff(now, wifi_next) >= 0:
        wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
        wifi_next = time.ticks_add(now, 15000)
    r = query(b"QPIGS")
    if r is None:
        print('{"error":"no reply"}')
    else:
        try:
            data = parse_qpigs(r)
            out = json.dumps(data)
            print(out)
            mqtt_publish(out, data)
        except Exception as e:
            print("parse error:", e, r)
    time.sleep(config.POLL_S)
