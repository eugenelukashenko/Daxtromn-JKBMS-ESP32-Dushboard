"""Тривоги в Telegram для колектора.

Чиста логіка (Alerts) не знає про мережу й годинник, тому її можна тестувати.
Відправку робить Telegram: окремий потік із чергою й повторами, тож збій Telegram не зупиняє облік.

Кожне правило: умова "погано" (bad), умова "знову добре" (ok, гістерезис), утримання hold_s
(умова має тривати стільки секунд, щоб відсікти поодинокі збої) і ліміт повторів cooldown_s.
Налаштування порогів — змінні оточення ALERT_* (див. grid-collector.env.example).
"""
import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


class Cfg:
    def __init__(self):
        self.soc1 = _f("ALERT_SOC_LOW", 20)            # %, перше попередження
        self.soc2 = _f("ALERT_SOC_CRIT", 10)           # %, критичний рівень
        self.soc_back = _f("ALERT_SOC_HYST", 5)        # на скільки % треба піднятись, щоб тривога зняла себе
        self.cell_hi = _f("ALERT_CELL_MAX_V", 3.60)    # В
        self.cell_lo = _f("ALERT_CELL_MIN_V", 2.90)    # В
        self.delta = _f("ALERT_CELL_DELTA_V", 0.10)    # В, дисбаланс комірок
        self.bat_t = _f("ALERT_BAT_TEMP_C", 50)        # °C, MOS і датчики батареї
        self.inv_t = _f("ALERT_INV_TEMP_C", 70)        # °C, інвертор
        self.load_pct = _f("ALERT_LOAD_PCT", 90)       # %
        self.stale_s = _f("ALERT_DATA_STALE_S", 180)   # с без даних → тривога
        self.cooldown_s = _f("ALERT_COOLDOWN_S", 1800)


# допустимі діапазони, щоб збиті кадри BMS не давали хибних тривог
RANGE = {"soc": (1, 100), "cell_min": (1, 5), "cell_max": (1, 5), "cell_delta": (0, 5),
         "t_mos": (-50, 150), "t1": (-50, 150), "t2": (-50, 150), "temp_c": (-50, 150),
         "load_pct": (0, 200), "grid_v": (0, 500), "grid_hz": (0, 100)}

# топік (відносно BMS_BASE/state або INV_BASE) → ім'я величини
BMS_KINDS = ("soc", "cell_min", "cell_max", "cell_delta", "t_mos", "t1", "t2", "voltage")
INV_KINDS = ("temp_c", "load_pct", "grid_v", "grid_hz", "out_w")


def dur(s):
    m = int(round(s / 60))
    if m < 1:
        return "менше хвилини"
    if m < 60:
        return "%d хв" % m
    return "%d год %02d хв" % (m // 60, m % 60)


class Rule:
    def __init__(self, key, bad, ok, hold_s, on_msg, off_msg):
        self.key, self.bad, self.ok, self.hold_s = key, bad, ok, hold_s
        self.on_msg, self.off_msg = on_msg, off_msg
        self.active = False
        self.bad_since = None      # коли умова "погано" почала виконуватись
        self.active_since = None
        self.last_sent = None
        self.notified = False      # про цю тривогу вже написали, тож напишемо й про відновлення


class Alerts:
    def __init__(self, cfg=None, now=0.0):
        self.c = cfg or Cfg()
        self.vals = {}              # kind -> (значення, час)
        self.started = now
        c = self.c
        v = self._v
        R = self.rules = []

        def add(*a):
            R.append(Rule(*a))

        # мережа: "погано" = мережі немає (за свіжими даними)
        def grid_bad(n):
            gv, hz = v("grid_v", n, 120), v("grid_hz", n, 120)
            return None if gv is None else (gv < 80 or (hz is not None and hz < 40))

        def grid_ok(n):
            b = grid_bad(n)
            return None if b is None else not b
        add("grid", grid_bad, grid_ok, 5,
            lambda n, d: "🔌 Мережу відключено",
            lambda n, d: "💡 Мережа з'явилась (не було %s)" % dur(d))

        for key, lim, name in (("soc1", c.soc1, "низький"), ("soc2", c.soc2, "критично низький")):
            add(key,
                (lambda n, lim=lim: None if v("soc", n, 300) is None else v("soc", n, 300) < lim),
                (lambda n, lim=lim: None if v("soc", n, 300) is None else v("soc", n, 300) >= lim + c.soc_back),
                10,
                (lambda n, d, lim=lim, name=name: ("🪫 Заряд батареї %s: %.0f%%" % (name, v("soc", n, 300) or 0))
                 + (" (поріг %.0f%%)" % lim)),
                (lambda n, d, name=name: "🔋 Заряд батареї відновився: %.0f%%" % (v("soc", n, 300) or 0)))
        # soc2 важливіший: коли діє soc2, повідомлення soc1 не дублюємо (див. check)

        add("cell_hi", lambda n: self._cmp("cell_max", n, lambda x: x > c.cell_hi),
            lambda n: self._cmp("cell_max", n, lambda x: x < c.cell_hi - 0.05), 30,
            lambda n, d: "⚡ Комірка перезаряджена: %.3f В (поріг %.2f В)" % (v("cell_max", n, 300), c.cell_hi),
            lambda n, d: "✅ Напруга комірок у нормі (макс. %.3f В)" % (v("cell_max", n, 300) or 0))
        add("cell_lo", lambda n: self._cmp("cell_min", n, lambda x: x < c.cell_lo),
            lambda n: self._cmp("cell_min", n, lambda x: x > c.cell_lo + 0.10), 30,
            lambda n, d: "⚡ Комірка розряджена: %.3f В (поріг %.2f В)" % (v("cell_min", n, 300), c.cell_lo),
            lambda n, d: "✅ Мінімальна напруга комірок у нормі (%.3f В)" % (v("cell_min", n, 300) or 0))
        add("delta", lambda n: self._cmp("cell_delta", n, lambda x: x > c.delta),
            lambda n: self._cmp("cell_delta", n, lambda x: x < c.delta * 0.7), 60,
            lambda n, d: "⚖️ Дисбаланс комірок: %.0f мВ (поріг %.0f мВ)" % (v("cell_delta", n, 300) * 1000, c.delta * 1000),
            lambda n, d: "✅ Дисбаланс комірок зменшився: %.0f мВ" % ((v("cell_delta", n, 300) or 0) * 1000))

        def bat_t(n):
            xs = [v(k, n, 300) for k in ("t_mos", "t1", "t2")]
            xs = [x for x in xs if x is not None]
            return max(xs) if xs else None
        self.bat_t = bat_t
        add("bat_t", lambda n: None if bat_t(n) is None else bat_t(n) > c.bat_t,
            lambda n: None if bat_t(n) is None else bat_t(n) < c.bat_t - 5, 30,
            lambda n, d: "🌡 Перегрів батареї: %.1f °C (поріг %.0f °C)" % (bat_t(n), c.bat_t),
            lambda n, d: "✅ Температура батареї в нормі: %.1f °C" % (bat_t(n) or 0))
        add("inv_t", lambda n: self._cmp("temp_c", n, lambda x: x > c.inv_t),
            lambda n: self._cmp("temp_c", n, lambda x: x < c.inv_t - 5), 30,
            lambda n, d: "🌡 Перегрів інвертора: %.0f °C (поріг %.0f °C)" % (v("temp_c", n, 300), c.inv_t),
            lambda n, d: "✅ Температура інвертора в нормі: %.0f °C" % (v("temp_c", n, 300) or 0))
        add("load", lambda n: self._cmp("load_pct", n, lambda x: x > c.load_pct),
            lambda n: self._cmp("load_pct", n, lambda x: x < c.load_pct - 10), 60,
            lambda n, d: "📈 Перевантаження інвертора: %.0f%% (поріг %.0f%%)" % (v("load_pct", n, 300), c.load_pct),
            lambda n, d: "✅ Навантаження інвертора знизилось: %.0f%%" % (v("load_pct", n, 300) or 0))

        # зв'язок: "погано" = давно немає даних (відлік іде від старту сервісу, якщо даних ще не було)
        def stale(kind):
            def bad(n):
                t = self.vals.get(kind)
                return n - (t[1] if t else self.started) > c.stale_s
            return bad
        add("inv_data", stale("out_w"), lambda n: not stale("out_w")(n), 0,
            lambda n, d: "📡 Немає даних від інвертора понад %s (ESP32 завис чи пропав Wi-Fi/MQTT)" % dur(c.stale_s),
            lambda n, d: "📡 Дані від інвертора відновились (перерва %s)" % dur(d))
        add("bms_data", stale("voltage"), lambda n: not stale("voltage")(n), 0,
            lambda n, d: "📡 Немає даних від BMS понад %s (ESP32 завис чи пропав зв'язок)" % dur(c.stale_s),
            lambda n, d: "📡 Дані від BMS відновились (перерва %s)" % dur(d))

    # --- допоміжне ---
    def _v(self, kind, now, max_age=300):
        t = self.vals.get(kind)
        return t[0] if t and now - t[1] <= max_age else None

    def _cmp(self, kind, now, f):
        x = self._v(kind, now)
        return None if x is None else f(x)

    def on_value(self, kind, value, now):
        r = RANGE.get(kind)
        if r and not (r[0] <= value <= r[1]):
            return
        self.vals[kind] = (value, now)

    def check(self, now):
        """Повертає список повідомлень, які треба надіслати зараз."""
        out = []
        for r in self.rules:
            b, ok = r.bad(now), r.ok(now)
            if not r.active:
                if b:
                    if r.bad_since is None:
                        r.bad_since = now
                    if now - r.bad_since >= r.hold_s:
                        r.active, r.active_since = True, r.bad_since
                        if r.last_sent is None or now - r.last_sent >= self.c.cooldown_s:
                            out.append(r.on_msg(now, 0))
                            r.last_sent, r.notified = now, True
                        else:
                            r.notified = False
                else:
                    r.bad_since = None
            else:
                if ok:
                    d = now - r.active_since
                    r.active, r.bad_since = False, None
                    if r.notified:
                        out.append(r.off_msg(now, d))
                    r.notified = False
        # критичний SOC перекриває попередження: не шлемо обидва в одному циклі
        if any(m.startswith("🪫") and "критично" in m for m in out):
            out = [m for m in out if not (m.startswith("🪫") and "критично" not in m)]
        return out


class Telegram:
    """Черга + потік: надсилає повідомлення з 3 повторами. Токен і chat_id беруться з оточення."""

    def __init__(self, token, chat_id, log=print):
        self.token, self.chat_id, self.log = token, chat_id, log
        self.q = queue.Queue(maxsize=100)
        if token and chat_id:
            threading.Thread(target=self._run, daemon=True).start()

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def send(self, text):
        if not self.enabled:
            self.log("[тривога без Telegram] " + text)
            return
        try:
            self.q.put_nowait(text)
        except queue.Full:
            pass

    def _post(self, text):
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % self.token, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("ok")

    def _run(self):
        while True:
            text = self.q.get()
            for attempt in range(3):
                try:
                    if self._post(text):
                        break
                except Exception as e:      # токен у повідомлення помилки не потрапляє: пишемо тільки тип
                    self.log("Telegram: помилка %s (спроба %d)" % (type(e).__name__, attempt + 1))
                time.sleep(5 * (attempt + 1))
