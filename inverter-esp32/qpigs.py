#!/usr/bin/env python3
"""Читає QPIGS з інвертора через CH341 + MAX3232 і друкує поля з назвами.
Запуск: python3 qpigs.py [/dev/ttyUSB0] [період_с]   (без періоду: один запит)"""
import sys, time, serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
PERIOD = float(sys.argv[2]) if len(sys.argv) > 2 else 0

FIELDS = [("Напруга мережі", "В"), ("Частота мережі", "Гц"), ("Напруга виходу", "В"), ("Частота виходу", "Гц"),
          ("Повна потужність", "ВА"), ("Активна потужність", "Вт"), ("Навантаження", "%"), ("Напруга шини", "В"),
          ("Напруга акумулятора", "В"), ("Струм заряду", "А"), ("Ємність акумулятора", "%"), ("Температура", "°C"),
          ("Струм PV", "А"), ("Напруга PV", "В"), ("Напруга акум. від SCC", "В"), ("Струм розряду", "А"),
          ("Статус (біти)", ""), ("Зсув вентилятора", ""), ("Версія EEPROM", ""), ("Потужність PV", "Вт"), ("Прапори", "")]

def crc(d):
    c = 0
    for b in d:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c

def fix(x): return x + 1 if x in (0x28, 0x0D, 0x0A) else x

def qpigs(s):
    c = crc(b"QPIGS")
    s.reset_input_buffer()
    s.write(b"QPIGS" + bytes([fix(c >> 8), fix(c & 255)]) + b"\r")
    buf, t0 = b"", time.time()
    while time.time() - t0 < 2.5 and not buf.endswith(b"\r"):
        buf += s.read(200)
    if not buf.startswith(b"(") or not buf.endswith(b"\r"):
        return None
    cc = crc(buf[:-3])
    if bytes([fix(cc >> 8), fix(cc & 255)]) != buf[-3:-1]:
        return None
    return buf[1:-3].decode().split()

with serial.Serial(PORT, 2400, timeout=0.3) as s:
    while True:
        p = qpigs(s)
        print(time.strftime("%H:%M:%S"))
        if p is None:
            print("  немає відповіді або помилка CRC")
        else:
            for (n, u), v in zip(FIELDS, p):
                print(f"  {n:24s} {v} {u}")
        if not PERIOD:
            break
        time.sleep(PERIOD)
