#include <Arduino.h>
// Сканер з'єднань: кожен пін по черзі виводить HIGH/LOW, інші читаються з підтяжкою вниз.
// Пропускаємо USB (13,14) та пін(и) флеша (16-22).
static const int pins[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 23, 24, 25, 26, 27, 28};
static const int N = sizeof(pins) / sizeof(pins[0]);
void setup() { Serial.begin(115200); }
void loop() {
  Serial.println("--- скан ---");
  bool any = false;
  for (int i = 0; i < N; i++) {
    for (int j = 0; j < N; j++) pinMode(pins[j], j == i ? OUTPUT : INPUT_PULLDOWN);
    digitalWrite(pins[i], HIGH); delay(3);
    bool hi[N]; for (int j = 0; j < N; j++) hi[j] = digitalRead(pins[j]);
    digitalWrite(pins[i], LOW); delay(3);
    for (int j = 0; j < N; j++) {
      if (j != i && hi[j] && !digitalRead(pins[j])) {
        Serial.printf("GPIO%d <-> GPIO%d з'єднані\n", pins[i], pins[j]);
        any = true;
      }
    }
  }
  if (!any) Serial.println("з'єднаних пар не знайдено");
  for (int j = 0; j < N; j++) pinMode(pins[j], INPUT);
  delay(3000);
}
