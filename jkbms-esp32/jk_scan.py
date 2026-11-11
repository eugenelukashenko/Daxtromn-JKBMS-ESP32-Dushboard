#!/usr/bin/env python3
"""Сканер JK-BMS через плату P-Link-CR (RS485_PC) и USB-RS485 адаптер.
Запуск: python3 jk_scan.py /dev/ttyUSB0 [бод=115200]
1) слушает линию 5 с (вдруг BMS шлёт сама), 2) шлёт активацию (0x01) и 'прочитать всё' (0x06)
по протоколу 'JKBMS RS485 Communication Protocol V2.0' (кадр 4E 57 ... 68 + сумма), 3) разбирает ответ."""
import sys, time, serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

def frame(cmd, ident, data=b"", src=3):
    body = (b"\x00\x00\x00\x00" + bytes([cmd, src, 0, ident]) + data + b"\x00\x00\x00\x00" + b"\x68")
    length = len(body) + 2 + 4          # длина поля + тело + контрольная сумма
    head = b"\x4e\x57" + length.to_bytes(2, "big") + body
    return head + (sum(head) & 0xFFFFFFFF).to_bytes(4, "big")

def temp(v):
    return v if v <= 100 else -(v - 100)

def parse(buf):
    i = buf.find(b"\x4e\x57")
    if i < 0 or len(buf) < i + 20: return None
    n = int.from_bytes(buf[i+2:i+4], "big")
    f = buf[i:i+2+n]
    if len(f) < 2 + n: return None
    p = f[11:-9]                          # інформаційне поле: id + дані
    out, k = {}, 0
    while k < len(p):
        t = p[k]; k += 1
        if t == 0x79:
            m = p[k]; k += 1
            out["cells_mV"] = [int.from_bytes(p[k+j+1:k+j+3], "big") for j in range(0, m, 3)]; k += m
        elif t in (0x80, 0x81, 0x82):
            out[{0x80:"T_mos",0x81:"T_box",0x82:"T_batt"}[t]] = temp(int.from_bytes(p[k:k+2], "big")); k += 2
        elif t == 0x83:
            out["V_total"] = int.from_bytes(p[k:k+2], "big") * 0.01; k += 2
        elif t == 0x84:
            v = int.from_bytes(p[k:k+2], "big"); k += 2
            out["I_raw"] = v; out["I_A"] = (v & 0x7FFF) * 0.01 * (1 if v & 0x8000 else -1)
        elif t == 0x85: out["SOC_%"] = p[k]; k += 1
        elif t == 0x86: out["n_temp"] = p[k]; k += 1
        elif t == 0x87: out["cycles"] = int.from_bytes(p[k:k+2], "big"); k += 2
        elif t == 0x89: out["cap_total_Ah"] = int.from_bytes(p[k:k+4], "big"); k += 4
        elif t == 0x8A: out["n_cells"] = int.from_bytes(p[k:k+2], "big"); k += 2
        elif t == 0x8B: out["warn_bits"] = format(int.from_bytes(p[k:k+2], "big"), "016b"); k += 2
        else:
            out[f"id_0x{t:02X}_rest"] = p[k-1:].hex(); break
    return out

with serial.Serial(PORT, BAUD, timeout=0.2) as s:
    print(f"== {PORT} {BAUD} 8N1: слухаю 5 с")
    t0, buf = time.time(), b""
    while time.time() - t0 < 5: buf += s.read(512)
    print("  пасивно:", buf.hex() if buf else "(тиша)")
    for name, fr in (("активація 0x01", frame(1, 0)), ("прочитати все 0x06", frame(6, 0))):
        s.reset_input_buffer(); s.write(fr); print(f"-> {name}: {fr.hex()}")
        time.sleep(1.5); r = s.read(2048)
        print("<-", r.hex() if r else "(тиша)")
        if r:
            d = parse(r)
            if d:
                for k, v in d.items(): print(f"   {k}: {v}")
