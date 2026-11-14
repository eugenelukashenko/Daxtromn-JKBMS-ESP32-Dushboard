import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alerts as A


def run(a, ta, tb, **vals):
    """Подає значення щосекунди й збирає повідомлення."""
    out = []
    for t in range(ta, tb):
        for k, v in vals.items():
            a.on_value(k, v, t)
        out += a.check(t)
    return out


def healthy(a, ta, tb, **over):
    v = dict(grid_v=230, grid_hz=50, out_w=300, voltage=25.6, soc=80, cell_min=3.30, cell_max=3.33,
             cell_delta=0.03, t_mos=30, t1=28, t2=28, temp_c=40, load_pct=20)
    v.update(over)
    return run(a, ta, tb, **v)


def test_quiet_when_healthy():
    a = A.Alerts(now=0)
    assert healthy(a, 0, 600) == []


def test_grid_off_on():
    a = A.Alerts(now=0)
    healthy(a, 0, 100)
    m = healthy(a, 100, 130, grid_v=2.0, grid_hz=0)
    assert len(m) == 1 and "відключено" in m[0], m
    m = healthy(a, 130, 400)
    assert len(m) == 1 and "з'явилась" in m[0] and "хв" in m[0], m


def test_grid_blip_ignored():
    a = A.Alerts(now=0)
    healthy(a, 0, 100)
    assert healthy(a, 100, 103, grid_v=2.0, grid_hz=0) == []
    assert healthy(a, 103, 200) == []


def test_soc_levels_and_hysteresis():
    a = A.Alerts(now=0)
    healthy(a, 0, 100)
    m = healthy(a, 100, 130, soc=19)
    assert len(m) == 1 and "20%" in m[0], m
    assert healthy(a, 130, 200, soc=22) == []          # ще нижче 25% — мовчимо
    m = healthy(a, 200, 230, soc=26)
    assert len(m) == 1 and "відновився" in m[0], m


def test_soc_crit_replaces_warning():
    a = A.Alerts(now=0)
    healthy(a, 0, 100)
    m = healthy(a, 100, 130, soc=8)
    assert len(m) == 1 and "критично" in m[0], m


def test_cell_and_temp():
    a = A.Alerts(now=0)
    healthy(a, 0, 100)
    m = healthy(a, 100, 200, cell_max=3.65, t1=55)
    assert any("перезаряджена" in x for x in m) and any("Перегрів батареї" in x for x in m), m
    assert len(m) == 2


def test_garbage_ignored():
    a = A.Alerts(now=0)
    healthy(a, 0, 100)
    assert healthy(a, 100, 200, cell_max=7.5, t1=-90) == []
    # збиті значення поза діапазоном просто не потрапляють у стан
    a.on_value("soc", 0, 300)
    assert a.vals["soc"][0] != 0


def test_data_lost_and_restored():
    a = A.Alerts(now=0)
    healthy(a, 0, 100)
    out = []
    for t in range(100, 400):
        out += a.check(t)                      # даних немає
    assert sum("інвертора" in x for x in out) == 1 and sum("BMS" in x for x in out) == 1, out
    m = healthy(a, 400, 420)
    assert sum("відновились" in x for x in m) == 2, m


def test_cooldown_suppresses_flapping():
    c = A.Cfg(); c.cooldown_s = 1800
    a = A.Alerts(c, now=0)
    healthy(a, 0, 100)
    first = healthy(a, 100, 170, load_pct=95)
    healthy(a, 170, 240)                        # знялась
    again = healthy(a, 240, 320, load_pct=95)   # знову за кілька хв — не повторюємо
    assert len(first) == 1 and again == [], (first, again)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
