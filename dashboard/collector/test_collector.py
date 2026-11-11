import os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["GRID_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import grid_collector as g

H = 3600
base = int(time.time()) // H * H - 5 * H     # недавня година (старіше за KEEP_* БД видаляє)

def feed(a, t, v=230.0, hz=50.0, **extra):
    a.on_value("grid_v", v, t); a.on_value("grid_hz", hz, t)
    for k, x in extra.items(): a.on_value(k, x, t)
    a.tick(t)

def test_hours_and_states():
    a = g.Accounting()
    a.tick(base); a.tick(base + 10)
    assert a.state(base + 10) == "unk"
    for s in range(11, 3701):
        feed(a, base + s)
    d, e, smp = a.take()
    assert round(d[base][2]) == 10 and round(d[base][0]) == 3590, d[base]
    assert round(d[base + H][0]) == 100, d[base + H]
    # подія: unk -> on
    assert [x[1] for x in e] == ["unk", "on"], e

def test_grid_logic():
    a = g.Accounting()
    t = base
    a.on_value("grid_v", 1.9, t); a.on_value("grid_hz", 0.0, t)
    assert a.state(t) == "off"
    a.on_value("grid_v", 230.0, t); a.on_value("grid_hz", 0.0, t)
    assert a.state(t) == "off"                          # є напруга, частоти немає
    a.on_value("grid_hz", 50.0, t)
    assert a.state(t) == "on"
    assert a.state(t + g.STALE_S + 5) == "unk"          # дані застаріли

def test_gap_and_events():
    a = g.Accounting()
    feed(a, base + 1); feed(a, base + 2)
    feed(a, base + 3, v=1.9, hz=0.0)                    # мережа зникла
    feed(a, base + 4, v=230, hz=50)                     # повернулась
    feed(a, base + 1000)                                # сервіс стояв ~1000 с
    _, e, _ = a.take()
    assert [x[1] for x in e] == ["on", "off", "on", "unk", "on"], e
    assert e[3][0] == base + 4                          # провал починається з останнього "живого" моменту

def test_samples():
    a = g.Accounting()
    for s in range(0, 200):
        feed(a, base + s, soc=87.0, out_w=500.0, pv_w=0.0)
    _, _, smp = a.take()
    assert len(smp) >= 2 and smp[0][1:4] == (87.0, 500.0, 0.0), smp
    assert smp[0][4] is None                              # струму/потужності BMS не було
    assert all(r[0] % 60 == 0 for r in smp)

def test_battery_power_sign():
    a = g.Accounting()
    for s in range(0, 130):
        feed(a, base + s, bcur=-4.0, bpow=100.0)          # розряд: BMS дає потужність без знака
    for s in range(130, 260):
        feed(a, base + s, bcur=3.0, bpow=80.0)            # заряд
    for s in range(260, 390):
        feed(a, base + s, bcur=0.01, bpow=0.3)            # спокій
    _, _, smp = a.take()
    vals = [r[4] for r in smp]
    assert -100.0 in vals and 80.0 in vals and 0.0 in vals, vals

def test_db_and_series():
    db = g.open_db(); lock = threading.Lock()
    a = g.Accounting()
    for s in range(0, 400):
        v = 230 if s < 200 else 1.9
        hz = 50 if s < 200 else 0
        feed(a, base + s, v=v, hz=hz, soc=90.0, out_w=700.0, pv_w=10.0)
    g.flush(a, db, lock)
    for s in range(400, 420):
        feed(a, base + s, v=1.9, hz=0, soc=90.0, out_w=700.0, pv_w=10.0)
    g.flush(a, db, lock)
    r = g.query_series(db, lock, base, base + 86400)
    assert [e[1] for e in r["events"]] == ["on", "off"], r["events"]
    assert len(r["samples"]) >= 6 and r["samples"][0][1] == 90.0 and len(r["samples"][0]) == 5
    assert r["prev"] is None
    r2 = g.query_series(db, lock, base + 100, base + 86400)      # вікно між "on" (base) і "off" (base+200)
    assert r2["prev"] == "on", r2["prev"]
    ls, alive = g.last_state_and_alive(db)
    assert ls == "off" and alive is not None
    # після перезапуску з великою перервою з'являється провал
    b = g.Accounting(ls); b.startup(alive + 3600, alive)
    assert b.events and b.events[0] == (int(alive + 60), "unk"), b.events

def test_migration():
    import sqlite3
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE samples(ts INTEGER PRIMARY KEY, soc REAL, load REAL, pv REAL)")
    old.execute("INSERT INTO samples VALUES(1,50,100,0)"); old.commit(); old.close()
    g.DB_PATH = path
    db = g.open_db()
    assert db.execute("SELECT ts,soc,load,pv,bat FROM samples").fetchall() == [(1, 50.0, 100.0, 0.0, None)]

def test_plausible():
    f = g.Plausible()
    assert f.accept("soc", 90)
    assert not f.accept("soc", 0) and not f.accept("soc", 122) and not f.accept("soc", 223)   # сміття з кадрів BMS
    assert f.accept("soc", 90)
    assert f.accept("bcur", 26.7) and not f.accept("bcur", 19136.8) and not f.accept("bcur", 486539.4)
    assert f.accept("bcur", 26.6)
    assert f.accept("bpow", 731.5) and not f.accept("bpow", 26.8)                              # у діапазоні, але стрибок
    assert f.accept("bpow", 732.0)
    assert not f.accept("bpow", 4000) and f.accept("bpow", 4010)                               # справжній стрибок: з другого разу
    assert f.accept("soc", 91) and f.accept("grid_v", 230)

for name, fn in list(globals().items()):
    if name.startswith("test_"):
        fn(); print("OK", name)
print("УСІ ТЕСТИ ПРОЙДЕНО")
