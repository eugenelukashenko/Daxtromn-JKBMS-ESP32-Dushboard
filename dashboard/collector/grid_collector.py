#!/usr/bin/env python3
"""Облік для дашборду: наявність мережі, SOC, навантаження й потужність панелей.

Слухає MQTT (daxtromn/aoh/*, jkbms/jkbms_c5/state/soc) і постійно, незалежно від відкритої сторінки, пише в SQLite:
  - hours:   щогодини секунди "є мережа / немає / немає даних" (для сум за добу і місяць);
  - events:  моменти зміни стану мережі (для лінії 0/1 на графіку);
  - samples: раз на хвилину SOC, навантаження (Вт), потужність панелей (Вт) і потужність батареї (Вт:
             мінус — розряд, плюс — заряд, як у струму BMS).
Віддає їх по HTTP на 127.0.0.1 (nginx проксіює /api/):  /api/grid  і  /api/series?from=&to=
"""
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BROKER = os.environ.get("MQTT_HOST", "10.100.202.5")
PORT = int(os.environ.get("MQTT_PORT", "1883"))
USER = os.environ.get("MQTT_USER", "dashboard")
PASSWORD = os.environ.get("MQTT_PASS", "")
INV_BASE = os.environ.get("INV_BASE", "daxtromn/aoh")
BMS_BASE = os.environ.get("BMS_BASE", "jkbms/jkbms_c5")
DB_PATH = os.environ.get("GRID_DB", "/var/lib/grid-collector/grid.db")
HTTP_HOST = os.environ.get("HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8090"))

TOPICS = {
    INV_BASE + "/grid_v": "grid_v",
    INV_BASE + "/grid_hz": "grid_hz",
    INV_BASE + "/out_w": "out_w",
    INV_BASE + "/pv_w": "pv_w",
    BMS_BASE + "/state/soc": "soc",
    BMS_BASE + "/state/current": "bcur",
    BMS_BASE + "/state/power": "bpow",
}

V_MIN = 80.0       # В: нижче цього мережі немає (без мережі інвертор показує ~2 В)
HZ_MIN = 40.0      # Гц
STALE_S = 120      # даних про мережу не було довше — "немає даних"
SAMPLE_STALE_S = 180
GAP_S = 30         # перерва в роботі сервісу довша за це не рахується
KEEP_SAMPLES_DAYS = 40
KEEP_DAYS = 400


# Допустимі діапазони значень BMS. Збиті кадри з UART дають сміття (732 В, 19136 А, SOC 0/122).
BMS_RANGE = {"soc": (1, 100), "bcur": (-300, 300), "bpow": (-20000, 20000)}
BMS_JUMP = {"soc": 3, "bcur": 10, "bpow": 300}   # різкий стрибок приймаємо, лише якщо наступне значення його підтвердить


class Plausible:
    """Відсікає сміття: значення поза діапазоном і поодинокі різкі стрибки."""

    def __init__(self):
        self.last = {}
        self.pending = {}

    def accept(self, kind, v):
        r = BMS_RANGE.get(kind)
        if r is None:
            return True
        if not (r[0] <= v <= r[1]):
            return False
        last = self.last.get(kind)
        tol = BMS_JUMP[kind]
        if last is None or abs(v - last) <= tol:
            self.last[kind] = v
            self.pending.pop(kind, None)
            return True
        p = self.pending.get(kind)
        if p is not None and abs(v - p) <= tol:      # другий поспіль — це справжня зміна
            self.last[kind] = v
            self.pending.pop(kind, None)
            return True
        self.pending[kind] = v
        return False


class Accounting:
    """Чиста логіка обліку: без мережі й годинника, щоб її можна було тестувати."""

    def __init__(self, last_state=None):
        self.lock = threading.Lock()
        self.vals = {}                 # kind -> (значення, час приходу)
        self.last_tick = None
        self.last_sample_min = None
        self.last_state = last_state   # 'on' | 'off' | 'unk' | None
        self.delta = {}                # година -> [є, немає, немає даних] секунди, ще не в БД
        self.events = []               # (ts, state) ще не в БД
        self.samples = []              # (ts, soc, load, pv) ще не в БД

    def startup(self, now, last_alive):
        """Після перезапуску: якщо сервіс стояв, позначаємо провал у даних."""
        with self.lock:
            if last_alive and now - last_alive > 120 and self.last_state != "unk":
                self.events.append((int(last_alive + 60), "unk"))
                self.last_state = "unk"

    def on_value(self, kind, value, now):
        with self.lock:
            self.vals[kind] = (value, now)

    def _get(self, kind, now, stale):
        v = self.vals.get(kind)
        return v[0] if v and now - v[1] <= stale else None

    def state(self, now):
        v = self._get("grid_v", now, STALE_S)
        if v is None:
            return "unk"
        hz = self._get("grid_hz", now, STALE_S)
        return "on" if v >= V_MIN and (hz is None or hz >= HZ_MIN) else "off"

    def _set_state(self, ts, st):
        if st != self.last_state:
            self.events.append((int(ts), st))
            self.last_state = st

    def tick(self, now):
        with self.lock:
            if self.last_tick is None:
                self.last_tick = now
                self._set_state(now, self.state(now))
                return
            if now - self.last_tick > GAP_S:      # сервіс (або годинник) стояв: цей проміжок не рахуємо
                self._set_state(self.last_tick, "unk")
                self.last_tick = now
                self._set_state(now, self.state(now))
                return
            st = self.state(now)
            idx = {"on": 0, "off": 1, "unk": 2}[st]
            t = self.last_tick
            while t < now:
                hour = int(t // 3600) * 3600
                seg = min(now, hour + 3600) - t
                self.delta.setdefault(hour, [0.0, 0.0, 0.0])[idx] += seg
                t += seg
            self.last_tick = now
            self._set_state(now, st)
            minute = int(now // 60) * 60
            if self.last_sample_min is None:
                self.last_sample_min = minute
            elif minute != self.last_sample_min:
                self.last_sample_min = minute
                cur = self._get("bcur", now, SAMPLE_STALE_S)
                pw = self._get("bpow", now, SAMPLE_STALE_S)
                bat = None
                if cur is not None and pw is not None:      # потужність BMS приходить без знака, знак — зі струму
                    bat = 0.0 if abs(cur) <= 0.05 else (pw if cur > 0 else -pw)
                row = (minute, self._get("soc", now, SAMPLE_STALE_S),
                       self._get("out_w", now, SAMPLE_STALE_S), self._get("pv_w", now, SAMPLE_STALE_S), bat)
                if any(x is not None for x in row[1:]):
                    self.samples.append(row)

    def take(self):
        with self.lock:
            d, e, s = self.delta, self.events, self.samples
            self.delta, self.events, self.samples = {}, [], []
        return d, e, s


def open_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("CREATE TABLE IF NOT EXISTS hours(hour INTEGER PRIMARY KEY, on_s REAL, off_s REAL, unk_s REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS events(ts INTEGER, state TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
    db.execute("CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY, soc REAL, load REAL, pv REAL)")
    if "bat" not in [r[1] for r in db.execute("PRAGMA table_info(samples)")]:
        db.execute("ALTER TABLE samples ADD COLUMN bat REAL")        # міграція зі старої версії
    db.commit()
    return db


def last_state_and_alive(db):
    r = db.execute("SELECT state FROM events ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
    a = db.execute("SELECT MAX(ts) FROM samples").fetchone()
    return (r[0] if r else None), (a[0] if a else None)


def flush(acc, db, lock):
    d, e, s = acc.take()
    if not (d or e or s):
        return
    with lock:
        for hour, (on, off, unk) in d.items():
            db.execute(
                "INSERT INTO hours(hour,on_s,off_s,unk_s) VALUES(?,?,?,?) "
                "ON CONFLICT(hour) DO UPDATE SET on_s=on_s+excluded.on_s, off_s=off_s+excluded.off_s, unk_s=unk_s+excluded.unk_s",
                (hour, on, off, unk))
        db.executemany("INSERT INTO events(ts,state) VALUES(?,?)", e)
        db.executemany("INSERT OR REPLACE INTO samples(ts,soc,load,pv,bat) VALUES(?,?,?,?,?)", s)
        now = int(time.time())
        db.execute("DELETE FROM hours WHERE hour < ?", (now - KEEP_DAYS * 86400,))
        db.execute("DELETE FROM events WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        db.execute("DELETE FROM samples WHERE ts < ?", (now - KEEP_SAMPLES_DAYS * 86400,))
        db.commit()


def query_hours(db, lock, days):
    since = int(time.time()) - days * 86400
    with lock:
        rows = db.execute("SELECT hour,on_s,off_s,unk_s FROM hours WHERE hour >= ? ORDER BY hour", (since,)).fetchall()
    return [[h, round(a), round(b), round(c)] for h, a, b, c in rows]


def query_series(db, lock, t_from, t_to):
    with lock:
        samples = db.execute("SELECT ts,soc,load,pv,bat FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts", (t_from, t_to)).fetchall()
        events = db.execute("SELECT ts,state FROM events WHERE ts >= ? AND ts < ? ORDER BY ts, rowid", (t_from, t_to)).fetchall()
        prev = db.execute("SELECT state FROM events WHERE ts < ? ORDER BY ts DESC, rowid DESC LIMIT 1", (t_from,)).fetchone()
    return {"prev": prev[0] if prev else None,
            "events": [[a, b] for a, b in events],
            "samples": [[ts, soc, ld, pv, bat] for ts, soc, ld, pv, bat in samples]}


def make_handler(acc, db, lock):
    class H(BaseHTTPRequestHandler):
        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            now = time.time()
            flush(acc, db, lock)
            if u.path == "/api/grid":
                try:
                    days = max(1, min(400, int(q.get("days", ["62"])[0])))
                except ValueError:
                    days = 62
                v = acc.vals.get("grid_v"); hz = acc.vals.get("grid_hz")
                self._json({"now": int(now), "state": acc.state(now), "v": v and v[0], "hz": hz and hz[0],
                            "rows": query_hours(db, lock, days)})
            elif u.path == "/api/series":
                try:
                    t_from = int(q["from"][0])
                    t_to = int(q.get("to", [str(t_from + 86400)])[0])
                except (KeyError, ValueError):
                    self.send_error(400)
                    return
                t_to = min(t_to, t_from + 3 * 86400)          # не більше 3 діб за запит
                out = query_series(db, lock, t_from, t_to)
                out.update({"now": int(now), "state": acc.state(now)})
                self._json(out)
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass
    return H


def main():
    import paho.mqtt.client as mqtt   # тут, щоб логіку обліку можна було тестувати без paho

    db = open_db()
    lock = threading.Lock()
    last_state, last_alive = last_state_and_alive(db)
    acc = Accounting(last_state)
    acc.startup(time.time(), last_alive)

    try:  # paho-mqtt 2.x
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="grid_collector")
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client(client_id="grid_collector")
    client.username_pw_set(USER, PASSWORD)

    def on_connect(c, userdata, flags, rc):
        print("MQTT підключено, rc =", rc, flush=True)
        c.subscribe([(t, 0) for t in TOPICS])

    plaus = Plausible()

    def on_message(c, userdata, msg):
        kind = TOPICS.get(msg.topic)
        if kind:
            try:
                v = float(msg.payload.decode())
                if plaus.accept(kind, v):
                    acc.on_value(kind, v, time.time())
            except ValueError:
                pass

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    client.connect_async(BROKER, PORT, keepalive=30)
    client.loop_start()

    srv = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), make_handler(acc, db, lock))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("HTTP на %s:%d" % (HTTP_HOST, HTTP_PORT), flush=True)

    n = 0
    while True:
        acc.tick(time.time())
        n += 1
        if n % 30 == 0:
            flush(acc, db, lock)
        time.sleep(1)


if __name__ == "__main__":
    main()
