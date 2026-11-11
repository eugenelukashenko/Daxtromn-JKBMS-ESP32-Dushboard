#!/usr/bin/env python3
"""Визначення протоколу інвертора через FT232RL + MAX3232 (RS-232).
Запуск: python3 probe_protocol.py [/dev/ttyUSB0]
Спочатку слухає лінію (пасивно), потім пробує MPI/Voltronic (QPI, 2400) і PACE (9600)."""
import sys, time, serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"

def crc_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc

def mpi(cmd):
    c = crc_xmodem(cmd.encode())
    hi, lo = c >> 8, c & 0xFF
    fix = lambda x: x + 1 if x in (0x28, 0x0D, 0x0A) else x
    return cmd.encode() + bytes([fix(hi), fix(lo)]) + b"\r"

def plain(cmd):
    """Команда без CRC, як у MPI-документі (ПК -> інвертор: 'QMOD<cr>')."""
    return cmd.encode() + b"\r"

def pace(ver=0x25, adr=0x01, cid1=0x46, cid2=0x4F, info=b""):
    """Кадр PACE: ~ VER ADR CID1 CID2 LENGTH INFO CHKSUM CR (ASCII hex)."""
    n = len(info)
    lchk = ((~((n & 0xF) + ((n >> 4) & 0xF) + ((n >> 8) & 0xF)) & 0xF) + 1) & 0xF
    length = (lchk << 12) | n
    body = "%02X%02X%02X%02X%04X" % (ver, adr, cid1, cid2, length) + info.decode()
    s = sum(body.encode()) & 0xFFFF
    chk = ((~s & 0xFFFF) + 1) & 0xFFFF
    return b"~" + body.encode() + b"%04X" % chk + b"\r"

def tx(ser, frame, wait=1.5):
    ser.reset_input_buffer()
    ser.write(frame)
    time.sleep(wait)
    return ser.read(512)

def show(label, data):
    print(f"  {label}: {data!r}" if data else f"  {label}: (тиша)")

MPI = [(c + suf, f(c)) for c in ("QPI", "QMOD", "QID", "QMD", "QPIGS", "QPIRI")
       for suf, f in ((" без CRC", plain), (" з CRC", mpi))]
for baud, tests in ((2400, MPI),
                    (9600, [("PACE ver", pace(cid2=0x4F)), ("PACE analog", pace(cid2=0x42, info=b"01")),
                            ("QPI без CRC", plain("QPI")), ("QPI з CRC", mpi("QPI"))])):
    print(f"== {baud} 8N1")
    with serial.Serial(PORT, baud, timeout=0.2) as ser:
        show("пасивно 3 с", (time.sleep(3), ser.read(512))[1])
        for name, frame in tests:
            print(f"  -> {name}: {frame!r}")
            show("<-", tx(ser, frame))
