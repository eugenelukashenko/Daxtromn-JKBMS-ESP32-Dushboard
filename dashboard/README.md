# Вебдашборд: BMS + инвертор через MQTT

Страница (`index.html`) подписывается на MQTT по WebSocket и показывает батарею (JK-BMS) и инвертор Daxtromn.
Топики: `jkbms/jkbms_c5/#` (BMS) и `daxtromn/aoh/#` (инвертор). Внешних зависимостей нет: `mqtt.min.js` лежит рядом.

## Схема

```text
браузер -> http://10.100.202.8:8077/ (nginx: страница + /mqtt) -> ws 10.100.202.5:9001 (Mosquitto в Docker на QNAP)
```

Браузер не умеет обычный MQTT (порт 1883), только MQTT через WebSocket, поэтому на брокере добавлен listener 9001.

## 1. Брокер: 10.100.202.5 (QNAP, Mosquitto в Docker)

Compose-проект: `/share/CACHEDEV1_DATA/.qpkg/container-station/data/application/mosquitto`
Конфиг и данные: `/share/Container/mosquitto/{config,data}`

`config/mosquitto.conf`:
```text
persistence true
persistence_location /mosquitto/data/
log_dest stdout
allow_anonymous false
password_file /mosquitto/config/passwd
acl_file /mosquitto/config/acl

listener 1883
protocol mqtt

listener 9001
protocol websockets
```

`config/acl` (пользователь `inverter` — полный доступ, `dashboard` — только чтение):
```text
user inverter
topic readwrite #

user dashboard
topic read jkbms/#
topic read daxtromn/#
```

Пользователь страницы: `docker exec -it <контейнер> mosquitto_passwd -b /mosquitto/config/passwd dashboard <пароль>`.
В `docker-compose.yml` к портам добавить `9001:9001`, затем `docker compose up -d`.

## 2. Сервер страницы: 10.100.202.8 (nginx)

```bash
mkdir -p /var/www/dashboard && cp index.html config.js mqtt.min.js /var/www/dashboard/
cp nginx-site.conf /etc/nginx/sites-available/dashboard
ln -sf /etc/nginx/sites-available/dashboard /etc/nginx/sites-enabled/dashboard
nginx -t && systemctl reload nginx
```

Сайт слушает порт **8077**, поэтому стандартный сайт на порту 80 не затрагивается.
Проверка: `http://10.100.202.8:8077/` — в шапке должно быть «MQTT подключено».


## 3. Сервис учёта (сеть, SOC, нагрузка) на 10.100.202.8

Сервис `collector/grid_collector.py` слушает MQTT и постоянно, даже когда страница не открыта, пишет в SQLite
`/var/lib/grid-collector/grid.db`:

| Таблица | Что хранит | Для чего |
|---|---|---|
| `hours` | секунды «есть сеть / нет / нет данных» по часам | плашки «Мережа: сьогодні / цей місяць» |
| `events` | моменты смены состояния сети | линия 0/1 на графике «Мережа» |
| `samples` | раз в минуту SOC, нагрузка (Вт), мощность панелей (Вт), мощность батареи (Вт, минус = разряд, плюс = заряд) | графики SOC и «Потужність» за сутки |

Сеть считается присутствующей при `grid_v >= 80 В` и `grid_hz >= 40 Гц`. Простой самого сервиса в учёт не попадает,
а на графике «Мережа» отображается как провал. Данные по минутам хранятся 40 суток, события и часы 400 суток.
HTTP (только 127.0.0.1:8090, дальше проксирует nginx `/api/`): `/api/grid?days=62`, `/api/series?from=<epoch>&to=<epoch>`.

Первая установка:
```bash
apt install python3-paho-mqtt
useradd --system --no-create-home --shell /usr/sbin/nologin gridcol
mkdir -p /opt/grid-collector && cp collector/grid_collector.py /opt/grid-collector/
cp collector/grid-collector.env.example /etc/grid-collector.env && chmod 600 /etc/grid-collector.env
cp collector/grid-collector.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now grid-collector
```

Обновление (новая версия сервиса и страницы):
```bash
cp collector/grid_collector.py /opt/grid-collector/ && systemctl restart grid-collector
cp index.html config.js /var/www/dashboard/
curl -s "http://127.0.0.1:8090/api/series?from=$(date -d 'today 00:00' +%s)" | head -c 300
```
Новые таблицы и колонки создаются сервисом сами (в том числе миграция старой таблицы `samples`), накопленные счётчики сохраняются. График SOC и нагрузки начнёт заполняться
с момента запуска новой версии. Тест логики: `python3 collector/test_collector.py`.

## Ограничения

- Графики показывают текущие сутки (с 00:00 до 24:00); данные берутся с сервера, поэтому не зависят от того, открыта ли страница.
- Отсутствие данных о сети (`нет данных`) учитывается отдельно и в «нет мережі» не входит.
- Счётчик времени от батареи по дням есть только на странице самой платы (`http://<IP платы>/`).
- Логин и пароль в `config.js` видны любому, кто откроет страницу; у `dashboard` права только на чтение.
