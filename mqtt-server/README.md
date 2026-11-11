# MQTT-сервер (Mosquitto, QNAP 10.100.202.5)

Файлы: `mosquitto.conf`, `acl`. Пароли (`passwd`) в репозиторий не входят.

```bash
# создать пользователей
docker exec -it <контейнер> mosquitto_passwd -c /mosquitto/config/passwd inverter
docker exec -it <контейнер> mosquitto_passwd -b /mosquitto/config/passwd dashboard <пароль>
```
В `docker-compose.yml` порты: `1883:1883`, `9001:9001`; затем `docker compose up -d`.

| Порт | Протокол | Кто подключается |
|---|---|---|
| 1883 | MQTT | ESP32 (inverter, jkbms), collector |
| 9001 | MQTT over WebSocket | браузер (через nginx `/mqtt`) |

## Топики

| Топик | Источник | Retain | Содержимое |
|---|---|---|---|
| `daxtromn/aoh/qpigs` | inverter-esp32 | да | JSON со всеми полями QPIGS (сеть, АКБ, нагрузка, PV...) |
| `daxtromn/aoh/<поле>` | inverter-esp32 | да | каждое поле отдельным топиком |
| `jkbms/jkbms_c5/state` | jkbms-esp32 | нет | JSON: напряжение, ток, SOC, ячейки, температуры (каждые 2 с) |
| `jkbms/jkbms_c5/settings` | jkbms-esp32 | да | настройки BMS (раз в 5 мин) |
| `jkbms/jkbms_c5/bms_online` | jkbms-esp32 | да | `1` / `0` — BMS отвечает |
| `jkbms/jkbms_c5/status` | jkbms-esp32 (LWT) | да | `online` / `offline` — связь платы |
| `homeassistant/sensor/jkbms_c5/...` | jkbms-esp32 | да | MQTT discovery для Home Assistant |
