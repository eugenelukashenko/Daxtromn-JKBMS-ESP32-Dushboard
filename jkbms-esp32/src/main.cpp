// JK-BMS (порт GPS, UART 3.3 В) -> MQTT, ESP32-C5.
// Протокол: див. ../протокол_gps_uart.md
#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <WebServer.h>
#include <Preferences.h>
#include <time.h>
#include "config.h"
#include "secrets.h"
#include "dashboard.h"

static const size_t FRAME_LEN = 300;
static const uint8_t HDR[4] = {0x55, 0xAA, 0xEB, 0x90};

// ---- Лічильник часу від батареї по днях + історія за 24 год ----
#define TZ_INFO "EET-2EEST,M3.5.0/3,M10.5.0/4"   // Київ
static const float ON_BATT_A = 0.5f;              // |струм| більше цього = працює від батареї (розряд) / заряд
struct DayRec { uint32_t key, disc, chg; };       // key = РРРРММДД, секунди розряду / заряду
static const int NDAYS = 30;
static DayRec days[NDAYS];
static Preferences prefs;
static bool daysDirty = false;
static uint32_t tSave = 0, lastCountMs = 0, remDisc = 0, remChg = 0;

struct Hp { uint16_t v; int16_t p; uint8_t soc; }; // v = В*100, p = Вт
static const int HN = 1440;                        // 24 год з кроком 60 с
static const uint32_t HSTEP_MS = 60000;
static Hp hist[HN];
static int hHead = 0, hCount = 0;
static uint32_t tHist = 0;

static uint32_t todayKey() {
  time_t t = time(nullptr);
  if (t < 1700000000) return 0;                    // час ще не синхронізовано
  struct tm tm; localtime_r(&t, &tm);
  return (tm.tm_year + 1900) * 10000 + (tm.tm_mon + 1) * 100 + tm.tm_mday;
}

static DayRec *dayRec(uint32_t key) {
  int free = -1, oldest = 0;
  for (int i = 0; i < NDAYS; i++) {
    if (days[i].key == key) return &days[i];
    if (days[i].key == 0 && free < 0) free = i;
    if (days[i].key < days[oldest].key) oldest = i;
  }
  DayRec *r = &days[free >= 0 ? free : oldest];
  r->key = key; r->disc = 0; r->chg = 0;
  return r;
}

static void countTime(float currentA) {
  uint32_t now = millis();
  uint32_t dt = lastCountMs ? now - lastCountMs : 0;
  lastCountMs = now;
  if (dt > 10000) dt = 0;                          // пропуск у даних не рахуємо
  uint32_t k = todayKey();
  if (!k) return;
  DayRec *r = dayRec(k);
  if (currentA < -ON_BATT_A)      { remDisc += dt; r->disc += remDisc / 1000; remDisc %= 1000; daysDirty = true; }
  else if (currentA > ON_BATT_A)  { remChg += dt;  r->chg  += remChg / 1000;  remChg  %= 1000;  daysDirty = true; }
}

static void saveDays() {
  if (!daysDirty) return;
  prefs.putBytes("d", days, sizeof(days));
  daysDirty = false;
  tSave = millis();
}

static void sampleHistory(float v, float pW, uint8_t soc) {
  uint32_t now = millis();
  if (tHist && now - tHist < HSTEP_MS) return;
  tHist = now;
  hist[hHead] = {(uint16_t)(v * 100.0f + 0.5f), (int16_t)pW, soc};
  hHead = (hHead + 1) % HN;
  if (hCount < HN) hCount++;
}

WebServer web(80);
static char lastState[1024] = "";
static char lastSettings[768] = "";
WiFiClient net;
PubSubClient mqtt(net);

static uint8_t frame[FRAME_LEN];
static uint32_t rxBytes = 0, rxFirst = 0;
static int rxPin = BMS_RX_PIN, txPin = BMS_TX_PIN;
static bool pinsSwapped = false;
static uint32_t tPoll = 0, tSettings = 0, tMqttTry = 0, tLed = 0, tLastData = 0;
static bool bmsOnline = false;
static bool discoverySent = false;
static uint8_t nCells = 8;

static void ledYellow(bool on) {
  rgbLedWrite(RGB_BUILTIN, on ? LED_BRIGHTNESS : 0, on ? LED_BRIGHTNESS * 3 / 5 : 0, 0);
}

static void updateLeds();

static uint16_t crc16(const uint8_t *d, size_t n) {
  uint16_t c = 0xFFFF;
  for (size_t i = 0; i < n; i++) {
    c ^= d[i];
    for (int b = 0; b < 8; b++) c = (c & 1) ? (c >> 1) ^ 0xA001 : c >> 1;
  }
  return c;
}

// Буфер RX > кадру (300 байт): інакше при затримці циклу (WiFi, веб) байти губляться і поля зсуваються.
static void beginBms(int rx, int tx) {
  Serial1.setRxBufferSize(1024);
  Serial1.begin(BMS_BAUD, SERIAL_8N1, rx, tx);
}

static void sendRequest(uint16_t reg) {
  uint8_t r[11] = {0x01, 0x10, (uint8_t)(reg >> 8), (uint8_t)reg, 0x00, 0x01, 0x02, 0x00, 0x00};
  uint16_t c = crc16(r, 9);
  r[9] = c & 0xFF;
  r[10] = c >> 8;
  while (Serial1.available()) Serial1.read();
  Serial1.write(r, sizeof(r));
}

// Читає кадр 300 байт із заголовком 55 AA EB 90. Повертає тип кадру або -1.
static int readFrame(uint32_t timeoutMs) {
  size_t pos = 0, hdr = 0;
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    if (!Serial1.available()) {
      if (pos == 0 && hdr == 0) { updateLeds(); web.handleClient(); }  // не відволікаємось посеред кадру
      delay(1);
      continue;
    }
    uint8_t b = Serial1.read();
    if (rxBytes == 0) rxFirst = b;
    rxBytes++;
    if (pos == 0) {
      if (b == HDR[hdr]) { frame[hdr++] = b; if (hdr == 4) pos = 4; }
      else hdr = (b == HDR[0]) ? (frame[0] = b, 1) : 0;
    } else {
      frame[pos++] = b;
      if (pos == FRAME_LEN) return frame[4];
    }
  }
  return -1;
}

static inline uint16_t u16(size_t o) { return frame[o] | (frame[o + 1] << 8); }
static inline int16_t  s16(size_t o) { return (int16_t)u16(o); }
static inline uint32_t u32(size_t o) { return u16(o) | ((uint32_t)u16(o + 2) << 16); }
static inline int32_t  s32(size_t o) { return (int32_t)u32(o); }

static uint32_t badFrames = 0, crcMiss = 0;
static bool crcTrusted = false;  // стає true після першого кадру зі збіглою контрольною сумою

// Контрольна сума: останній байт кадру = сума перших 299 байт (mod 256).
// Поки жодного разу не збіглась — не покладаємось на неї, лише на перевірку діапазонів.
static bool frameValid() {
  uint8_t sum = 0;
  for (size_t i = 0; i < FRAME_LEN - 1; i++) sum += frame[i];
  bool crcOk = sum == frame[FRAME_LEN - 1];
  if (crcOk) crcTrusted = true;
  else { crcMiss++; if (crcTrusted) return false; }
  if (frame[4] != 2) return true;
  float v = s32(150) / 1000.0f, i = s32(158) / 1000.0f;
  float rem = u32(174) / 1000.0f, nom = u32(178) / 1000.0f;
  uint8_t n = __builtin_popcount(u32(70));
  if (v < 1 || v > 120 || fabsf(i) > 300 || frame[173] > 100 || frame[190] > 100) return false;
  if (nom < 1 || nom > 2000 || rem > nom * 1.2f || n < 1 || n > 32) return false;
  for (int k = 0; k < 32; k++) {
    uint16_t c = u16(6 + 2 * k);
    if (c > 5000) return false;
  }
  return true;
}

static void publish(const char *sub, const char *payload, bool retain = false) {
  char t[96];
  snprintf(t, sizeof(t), MQTT_BASE "/" DEVICE_ID "/%s", sub);
  mqtt.publish(t, payload, retain);
}

// Окремий топік для кожного значення: jkbms/<id>/state/<key>
static void pubVal(const char *key, float v, int dec) {
  char t[64], p[24];
  snprintf(t, sizeof(t), MQTT_BASE "/" DEVICE_ID "/state/%s", key);
  snprintf(p, sizeof(p), "%.*f", dec, v);
  mqtt.publish(t, p);
}

static void sendDiscovery() {
  struct S { const char *key, *name, *unit, *cls; };
  static const S sensors[] = {
    {"voltage", "Voltage", "V", "voltage"},
    {"current", "Current", "A", "current"},
    {"power", "Power", "W", "power"},
    {"soc", "SOC", "%", "battery"},
    {"soh", "SOH", "%", ""},
    {"remaining_ah", "Remaining capacity", "Ah", ""},
    {"nominal_ah", "Nominal capacity", "Ah", ""},
    {"cycles", "Cycles", "", ""},
    {"t_mos", "MOS temperature", "°C", "temperature"},
    {"t1", "Temperature 1", "°C", "temperature"},
    {"t2", "Temperature 2", "°C", "temperature"},
    {"cell_avg", "Cell average", "V", "voltage"},
    {"cell_delta", "Cell delta", "V", "voltage"},
    {"cell_min", "Cell min", "V", "voltage"},
    {"cell_max", "Cell max", "V", "voltage"},
    {"balance_current", "Balance current", "A", "current"},
  };
  char topic[128], pl[640];
  for (const S &s : sensors) {
    snprintf(topic, sizeof(topic), "homeassistant/sensor/" DEVICE_ID "/%s/config", s.key);
    int n = snprintf(pl, sizeof(pl),
      "{\"name\":\"%s\",\"uniq_id\":\"" DEVICE_ID "_%s\",\"stat_t\":\"" MQTT_BASE "/" DEVICE_ID "/state\","
      "\"val_tpl\":\"{{ value_json.%s }}\",\"avty_t\":\"" MQTT_BASE "/" DEVICE_ID "/status\"",
      s.name, s.key, s.key);
    if (s.unit[0]) n += snprintf(pl + n, sizeof(pl) - n, ",\"unit_of_meas\":\"%s\"", s.unit);
    if (s.cls[0]) n += snprintf(pl + n, sizeof(pl) - n, ",\"dev_cla\":\"%s\"", s.cls);
    n += snprintf(pl + n, sizeof(pl) - n, ",\"stat_cla\":\"measurement\","
      "\"dev\":{\"ids\":[\"" DEVICE_ID "\"],\"name\":\"JK-BMS\",\"mf\":\"JK\"}}");
    mqtt.publish(topic, pl, true);
  }
  for (int i = 1; i <= nCells; i++) {
    snprintf(topic, sizeof(topic), "homeassistant/sensor/" DEVICE_ID "/cell%d/config", i);
    snprintf(pl, sizeof(pl),
      "{\"name\":\"Cell %d\",\"uniq_id\":\"" DEVICE_ID "_cell%d\",\"stat_t\":\"" MQTT_BASE "/" DEVICE_ID "/state\","
      "\"val_tpl\":\"{{ value_json.cell%d }}\",\"unit_of_meas\":\"V\",\"dev_cla\":\"voltage\",\"stat_cla\":\"measurement\","
      "\"avty_t\":\"" MQTT_BASE "/" DEVICE_ID "/status\","
      "\"dev\":{\"ids\":[\"" DEVICE_ID "\"],\"name\":\"JK-BMS\",\"mf\":\"JK\"}}", i, i, i);
    mqtt.publish(topic, pl, true);
  }
  discoverySent = true;
}

static void handleData() {
  uint32_t mask = u32(70);
  uint8_t n = __builtin_popcount(mask);
  if (n >= 1 && n <= 32) nCells = n;
  uint16_t cells[32];
  uint16_t vmin = 0xFFFF, vmax = 0;
  for (int i = 0; i < 32; i++) {
    cells[i] = u16(6 + 2 * i);
  }
  uint8_t idx = 0;
  char cellJson[512];
  int cn = 0;
  for (int i = 0; i < 32 && idx < nCells; i++) {
    if (!(mask & (1UL << i))) continue;
    idx++;
    if (cells[i] < vmin) vmin = cells[i];
    if (cells[i] > vmax) vmax = cells[i];
    cn += snprintf(cellJson + cn, sizeof(cellJson) - cn, ",\"cell%d\":%.3f", idx, cells[i] / 1000.0f);
  }

  char js[1024];
  snprintf(js, sizeof(js),
    "{\"voltage\":%.3f,\"current\":%.3f,\"power\":%.1f,\"soc\":%u,\"soh\":%u,"
    "\"remaining_ah\":%.3f,\"nominal_ah\":%.3f,\"cycles\":%lu,"
    "\"t_mos\":%.1f,\"t1\":%.1f,\"t2\":%.1f,"
    "\"cell_avg\":%.3f,\"cell_delta\":%.3f,\"cell_min\":%.3f,\"cell_max\":%.3f,"
    "\"balance_current\":%.3f,\"balancing\":%u,\"charge_mos\":%u,\"discharge_mos\":%u,"
    "\"cells_count\":%u,\"uptime_s\":%lu%s}",
    s32(150) / 1000.0f, s32(158) / 1000.0f, s32(154) / 1000.0f,
    frame[173], frame[190],
    u32(174) / 1000.0f, u32(178) / 1000.0f, (unsigned long)u32(182),
    s16(144) / 10.0f, s16(162) / 10.0f, s16(164) / 10.0f,
    u16(74) / 1000.0f, u16(76) / 1000.0f, vmin / 1000.0f, vmax / 1000.0f,
    s16(170) / 1000.0f, frame[172], frame[198], frame[199],
    nCells, (unsigned long)u32(194), cellJson);
  strlcpy(lastState, js, sizeof(lastState));
  countTime(s32(158) / 1000.0f);
  sampleHistory(s32(150) / 1000.0f, (s32(158) < 0 ? -1.0f : 1.0f) * s32(154) / 1000.0f, frame[173]);
  if (mqtt.connected()) {
    if (!discoverySent) sendDiscovery();
    publish("state", js);
    pubVal("voltage", s32(150) / 1000.0f, 3);
    pubVal("current", s32(158) / 1000.0f, 3);
    pubVal("power", s32(154) / 1000.0f, 1);
    pubVal("soc", frame[173], 0);
    pubVal("soh", frame[190], 0);
    pubVal("remaining_ah", u32(174) / 1000.0f, 3);
    pubVal("nominal_ah", u32(178) / 1000.0f, 3);
    pubVal("cycles", u32(182), 0);
    pubVal("t_mos", s16(144) / 10.0f, 1);
    pubVal("t1", s16(162) / 10.0f, 1);
    pubVal("t2", s16(164) / 10.0f, 1);
    pubVal("cell_avg", u16(74) / 1000.0f, 3);
    pubVal("cell_delta", u16(76) / 1000.0f, 3);
    pubVal("cell_min", vmin / 1000.0f, 3);
    pubVal("cell_max", vmax / 1000.0f, 3);
    pubVal("balance_current", s16(170) / 1000.0f, 3);
    pubVal("balancing", frame[172], 0);
    pubVal("charge_mos", frame[198], 0);
    pubVal("discharge_mos", frame[199], 0);
    pubVal("cells_count", nCells, 0);
    pubVal("uptime_s", u32(194), 0);
    uint8_t ci = 0;
    for (int i = 0; i < 32 && ci < nCells; i++) {
      if (!(mask & (1UL << i))) continue;
      ci++;
      char k[12];
      snprintf(k, sizeof(k), "cell%d", ci);
      pubVal(k, cells[i] / 1000.0f, 3);
    }
  }
  Serial.println(js);
}

static void handleSettings() {
  char js[768];
  snprintf(js, sizeof(js),
    "{\"cell_uvp_mv\":%ld,\"cell_uvpr_mv\":%ld,\"cell_ovp_mv\":%ld,\"cell_ovpr_mv\":%ld,"
    "\"balance_trigger_mv\":%ld,\"soc100_mv\":%ld,\"soc0_mv\":%ld,"
    "\"max_charge_a\":%.1f,\"max_discharge_a\":%.1f,\"max_balance_a\":%.2f,"
    "\"charge_otp\":%.1f,\"discharge_otp\":%.1f,\"mos_otp\":%.1f,\"charge_utp\":%.1f,"
    "\"cell_count\":%ld,\"capacity_ah\":%.1f}",
    (long)s32(10), (long)s32(14), (long)s32(18), (long)s32(22),
    (long)s32(26), (long)s32(30), (long)s32(34),
    s32(50) / 1000.0f, s32(62) / 1000.0f, s32(78) / 1000.0f,
    s32(82) / 10.0f, s32(90) / 10.0f, s32(106) / 10.0f, s32(98) / 10.0f,
    (long)s32(114), s32(130) / 1000.0f);
  strlcpy(lastSettings, js, sizeof(lastSettings));
  if (mqtt.connected()) publish("settings", js, true);
}

// Якщо ніколи не було жодного байта від BMS — міняємо RX/TX місцями (раз на 12 с).
static void autoSwapPins() {
  static uint32_t tSwap = 0;
  uint32_t now = millis();
  if (bmsOnline || rxBytes > 0) return;
  if (now - tSwap < 12000) return;
  tSwap = now;
  if (now < 12000) return;
  pinsSwapped = !pinsSwapped;
  rxPin = pinsSwapped ? BMS_TX_PIN : BMS_RX_PIN;
  txPin = pinsSwapped ? BMS_RX_PIN : BMS_TX_PIN;
  Serial1.end();
  beginBms(rxPin, txPin);
  Serial.printf("Нема даних: міняю RX/TX -> RX=GPIO%d TX=GPIO%d\n", rxPin, txPin);
}

static void pollBms() {
  autoSwapPins();
  uint32_t now = millis();
  if (now - tPoll < POLL_MS && tPoll != 0) return;
  tPoll = now;
  bool wantSettings = (tSettings == 0) || (now - tSettings >= SETTINGS_MS);
  sendRequest(wantSettings ? 0x161E : 0x1620);
  int type = readFrame(1500);
  if (type < 0) {
    if (bmsOnline && now - tLastData > 10000) {
      bmsOnline = false;
      if (mqtt.connected()) publish("bms_online", "0", true);
    }
    return;
  }
  tLastData = millis();
  if (!bmsOnline) {
    bmsOnline = true;
    if (mqtt.connected()) publish("bms_online", "1", true);
  }
  if (!frameValid()) { badFrames++; return; }  // збитий кадр не публікуємо
  if (type == 2) handleData();
  else if (type == 1) { handleSettings(); tSettings = now ? now : 1; }
}

static void wifiAndMqtt() {
  uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqtt.connected()) { mqtt.loop(); return; }
  if (now - tMqttTry < 5000 && tMqttTry != 0) return;
  tMqttTry = now;
  char lwt[96];
  snprintf(lwt, sizeof(lwt), MQTT_BASE "/" DEVICE_ID "/status");
  bool ok = strlen(MQTT_USER)
    ? mqtt.connect(DEVICE_ID, MQTT_USER, MQTT_PASS, lwt, 0, true, "offline")
    : mqtt.connect(DEVICE_ID, lwt, 0, true, "offline");
  if (ok) {
    mqtt.publish(lwt, "online", true);
    discoverySent = false;
    Serial.println("MQTT підключено");
  } else {
    Serial.printf("MQTT помилка, rc=%d\n", mqtt.state());
  }
}

static void updateLeds() {
  uint32_t now = millis();
  if (WiFi.status() == WL_CONNECTED) {
    ledYellow(true);
  } else if (now - tLed >= 500) {
    tLed = now;
    static bool st = false;
    st = !st;
    ledYellow(st);
  }
}

static bool webAuth() {
  if (WEB_PASS[0] == 0) return true;            // пароль не задано — страница без авторизації
  if (web.authenticate(WEB_USER, WEB_PASS)) return true;
  web.requestAuthentication(BASIC_AUTH, "JK-BMS");
  return false;
}

static void setupWeb() {
  web.on("/", []() {
    if (!webAuth()) return;
    web.send_P(200, "text/html; charset=utf-8", DASHBOARD_HTML);
  });
  web.on("/api/state", []() {
    if (!webAuth()) return;
    static char out[1280];
    long age = lastState[0] ? (long)((millis() - tLastData) / 1000) : -1;
    snprintf(out, sizeof(out), "{\"online\":%s,\"age\":%ld,\"rssi\":%d,\"data\":%s}",
             bmsOnline ? "true" : "false", age, WiFi.RSSI(), lastState[0] ? lastState : "{}");
    web.sendHeader("Cache-Control", "no-store");
    web.send(200, "application/json", out);
  });
  web.on("/api/settings", []() {
    if (!webAuth()) return;
    web.send(200, "application/json", lastSettings[0] ? lastSettings : "{}");
  });
  web.on("/api/days", []() {
    if (!webAuth()) return;
    static char out[2200];
    int n = snprintf(out, sizeof(out), "{\"valid\":%s,\"threshold\":%.1f,\"today\":%lu,\"days\":[",
                     todayKey() ? "true" : "false", ON_BATT_A, (unsigned long)todayKey());
    bool first = true;
    for (int i = 0; i < NDAYS && n < (int)sizeof(out) - 80; i++) {
      if (!days[i].key) continue;
      n += snprintf(out + n, sizeof(out) - n, "%s{\"d\":%lu,\"disc\":%lu,\"chg\":%lu}",
                    first ? "" : ",", (unsigned long)days[i].key, (unsigned long)days[i].disc, (unsigned long)days[i].chg);
      first = false;
    }
    snprintf(out + n, sizeof(out) - n, "]}");
    web.sendHeader("Cache-Control", "no-store");
    web.send(200, "application/json", out);
  });
  web.on("/api/history", []() {
    if (!webAuth()) return;
    web.setContentLength(CONTENT_LENGTH_UNKNOWN);
    web.send(200, "application/json", "");
    char b[96];
    snprintf(b, sizeof(b), "{\"step\":%lu,\"age\":%lu,\"n\":%d,", (unsigned long)(HSTEP_MS / 1000),
             (unsigned long)(tHist ? (millis() - tHist) / 1000 : 0), hCount);
    web.sendContent(b);
    static const char *names[3] = {"\"soc\":[", "\"v\":[", "\"p\":["};
    for (int a = 0; a < 3; a++) {
      web.sendContent(names[a]);
      String chunk;
      chunk.reserve(640);
      for (int k = 0; k < hCount; k++) {
        const Hp &h = hist[(hHead - hCount + k + HN) % HN];
        if (k) chunk += ',';
        chunk += (a == 0) ? (int)h.soc : (a == 1) ? (int)h.v : (int)h.p;
        if (chunk.length() > 560) { web.sendContent(chunk); chunk = ""; }
      }
      chunk += (a < 2) ? "]," : "]}";
      web.sendContent(chunk);
    }
    web.sendContent("");
  });
  web.onNotFound([]() { web.send(404, "text/plain", "not found"); });
  web.begin();
}

void setup() {
  ledYellow(false);
  Serial.begin(115200);
  beginBms(BMS_RX_PIN, BMS_TX_PIN);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  prefs.begin("jkdays", false);
  prefs.getBytes("d", days, sizeof(days));
  configTzTime(TZ_INFO, "pool.ntp.org", "time.google.com");
  setupWeb();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(1536);
  mqtt.setKeepAlive(30);
}

static void statusLog() {
  static uint32_t t = 0;
  if (millis() - t < 5000) return;
  t = millis();
  Serial.printf("[%lus] wifi=%d ip=%s rssi=%d mqtt=%d(rc=%d) bms=%d rx=%lu first=0x%02lX RX=GPIO%d TX=GPIO%d bad=%lu crcmiss=%lu crc=%d\n", millis() / 1000,
                (int)WiFi.status(), WiFi.localIP().toString().c_str(), WiFi.RSSI(),
                mqtt.connected(), mqtt.state(), bmsOnline, (unsigned long)rxBytes, (unsigned long)rxFirst, rxPin, txPin,
                (unsigned long)badFrames, (unsigned long)crcMiss, crcTrusted);
}

void loop() {
  web.handleClient();
  if (daysDirty && millis() - tSave > 600000UL) saveDays();
  statusLog();
  updateLeds();
  wifiAndMqtt();
  pollBms();
  delay(5);
}
