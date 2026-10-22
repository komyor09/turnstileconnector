"""
Короткий протокол контроллеров WGACCESS / N3000 (пакеты 0x2x) — чистый stdlib.

Разобран по захватам Wireshark: протокол ОТКРЫТЫЙ (без шифрования, SM4/DLL не
нужны — контроллеры настроены без пароля связи). Общий кадр:

    [0]      байт 0x2x: 0x20 статус, 0x23 привилегия карты (0x21/0x24/0x25 — блоки N3000)
    [1]      функция; в ОТВЕТЕ возвращается функция | 1
    [2:4]    CRC-16/IBM всего пакета с обнулённым [2:4], little-endian (DLL: CRC_16_IBM)
    [4:8]    counter (uint32 LE); в ответе возвращается эхом
    [8:12]   ЗАПРОС: 0 ; ОТВЕТ: серийник контроллера
    [12:16]  ЗАПРОС: серийник ; ОТВЕТ: 0
    [16:20]  00 81 02 00  (постоянная субшапка)
    [20:…]   данные функции

Команда статуса (fn 0x20, 28 Б) и команда привилегии карты (p0 0x23 fn 0x20, 44 Б)
сверены с захватами БАЙТ-В-БАЙТ. Дата карты упакована как ((год-2000)<<9)|(мес<<5)|день.
"""
import socket
import struct

PORT = 60000
SUBHDR = bytes.fromhex("00810200")           # [16:20] во всех пакетах
STATUS_TAIL = bytes.fromhex("00ffffffff0f000000")  # хвост запроса статуса (после субшапки)


# ---------------------------------------------------------------- CRC / рамка
def crc_ibm(data: bytes) -> int:
    """CRC-16/IBM (ARC): рефлексивный, poly 0xA001, init 0."""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c & 0xFFFF


def seal(pkt: bytearray) -> bytes:
    pkt[2] = pkt[3] = 0
    c = crc_ibm(bytes(pkt))
    pkt[2], pkt[3] = c & 0xFF, c >> 8
    return bytes(pkt)


def crc_ok(pkt: bytes) -> bool:
    b = bytearray(pkt)
    want = b[2] | b[3] << 8
    b[2] = b[3] = 0
    return crc_ibm(bytes(b)) == want


def _counter() -> int:
    """Уникальный счётчик (микросекунды) — чтобы не нарваться на анти-повтор."""
    import time
    return int(time.time() * 1000) & 0x7FFFFFFF


# ----------------------------------------------------------------- даты карты
def enc_date(y: int, m: int, d: int) -> int:
    return (((y - 2000) << 9) | (m << 5) | d) & 0xFFFF


def dec_date(w: int) -> tuple[int, int, int]:
    return 2000 + (w >> 9), (w >> 5) & 0xF, w & 0x1F


# -------------------------------------------------------------------- билдеры
def build_status(serial: int, counter: int, fn: int = 0x20) -> bytes:
    """28-байтный запрос статуса/чтения (кнопка Check в N3000)."""
    p = bytearray(28)
    p[0] = 0x20
    p[1] = fn
    p[4:8] = struct.pack("<I", counter & 0xFFFFFFFF)
    p[12:16] = struct.pack("<I", serial)
    p[16:20] = SUBHDR
    p[20:28] = STATUS_TAIL
    return seal(p)


def build_privilege(serial: int, counter: int, card: int,
                    activate: tuple, deactivate: tuple,
                    doors=(1, 0, 0, 0), enable: bool = True) -> bytes:
    """
    44-байтная команда привилегии карты (N3000: Access Privilege → Allow/Prohibit And Upload).
    Воспроизводит байты N3000 1-в-1.
      activate/deactivate: (год, месяц, день)
      doors: до 4 флагов по дверям контроллера (1 = разрешено)
      enable=False + doors=0 → запрет (Prohibit).
    """
    p = bytearray(44)
    p[0] = 0x23
    p[1] = 0x20
    p[4:8] = struct.pack("<I", counter & 0xFFFFFFFF)
    p[12:16] = struct.pack("<I", serial)
    p[16:20] = SUBHDR
    p[20:24] = struct.pack("<I", card)          # [24:28] = 0 (слот)
    p[28] = 0xA0 if enable else 0x20            # бит 0x80 = «разрешено»
    p[29:32] = bytes.fromhex("4e4605")          # зашитое время (Dea.Time 23:59)
    p[32:34] = struct.pack("<H", enc_date(*activate))
    p[34:36] = struct.pack("<H", enc_date(*deactivate))
    doors = list(doors)[:4] + [0] * (4 - len(doors))
    p[36:40] = bytes(1 if x else 0 for x in doors)
    return seal(p)


# -------------------------------------------------------------------- разбор
def parse_reply(resp: bytes) -> dict:
    return {
        "p0": resp[0],
        "fn": resp[1],
        "crc_ok": crc_ok(resp),
        "counter": struct.unpack("<I", resp[4:8])[0],
        "serial": struct.unpack("<I", resp[8:12])[0],
        "data": resp[16:],
    }


def transact(ip: str, req: bytes, timeout: float = 2.0):
    """Отправить один пакет, вернуть (ответ|None, разбор|None). Больше ничего не шлёт."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    s.sendto(req, (ip, PORT))
    try:
        resp, _ = s.recvfrom(2048)
    except socket.timeout:
        return None, None
    finally:
        s.close()
    return resp, parse_reply(resp)


def _ack_ok(req: bytes, info: dict, serial: int, counter: int) -> bool:
    return bool(info and info["crc_ok"] and info["p0"] == req[0]
                and info["fn"] == (req[1] | 1)
                and info["serial"] == serial and info["counter"] == counter)


# ---------------------------------------------------------------------- CLI
def _date(s):
    y, m, d = map(int, s.split("-"))
    return (y, m, d)


def _confirm(ip, req, what):
    print(f"\n{what}")
    print("  →", f"{ip}:{PORT}")
    print("  байты:", req.hex(" "))
    input("Enter — отправить, Ctrl+C — отмена... ")


def _main():
    import sys
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        print("Команды:")
        print("  status   <ip> <serial>")
        print("  grant    <ip> <serial> <card> <activate ГГГГ-ММ-ДД> <deactivate ГГГГ-ММ-ДД> <двери,напр 1 или 1,2>")
        print("  prohibit <ip> <serial> <card>")
        return

    cmd = a[0]
    if cmd == "status":
        ip, serial = a[1], int(a[2])
        cnt = _counter()
        req = build_status(serial, cnt)
        _confirm(ip, req, "Запрос статуса (чтение, безопасно):")
        resp, info = transact(ip, req)
        if not info:
            print("Ответа нет (таймаут).")
            return
        ok = _ack_ok(req, info, serial, cnt)
        print(f"Ответ {len(resp)} Б, CRC {'OK' if info['crc_ok'] else 'BAD'}, serial {info['serial']}")
        print(">>> СВЯЗЬ ПОДТВЕРЖДЕНА <<<" if ok else "поля не сошлись — пришли вывод.")

    elif cmd in ("grant", "prohibit"):
        ip, serial, card = a[1], int(a[2]), int(a[3])
        if cmd == "grant":
            act, dea, doors = _date(a[4]), _date(a[5]), [int(x) for x in a[6].split(",")]
            dmask = [1 if i + 1 in doors else 0 for i in range(4)]
            req = build_privilege(serial, _counter(), card, act, dea, dmask, enable=True)
            what = (f"ВЫДАТЬ карте {card}: двери {doors}, {a[4]}…{a[5]} "
                    f"на контроллере {serial}. Это ЗАПИСЬ в контроллер.")
        else:
            req = build_privilege(serial, _counter(), card, (2000, 1, 1), (2000, 1, 1),
                                  [0, 0, 0, 0], enable=False)
            what = f"ЗАПРЕТИТЬ карту {card} на контроллере {serial} (двери=0). Это ЗАПИСЬ в контроллер."
        cnt = struct.unpack("<I", req[4:8])[0]
        _confirm(ip, req, what)
        resp, info = transact(ip, req)
        if not info:
            print("Ответа нет (таймаут). В контроллер, скорее всего, ничего не записалось.")
            return
        ok = _ack_ok(req, info, serial, cnt)
        print(f"Ответ {len(resp)} Б: p0=0x{info['p0']:02X} fn=0x{info['fn']:02X} "
              f"CRC {'OK' if info['crc_ok'] else 'BAD'} ack[16:20]={info['data'][:4].hex(' ')}")
        print(">>> ПРИНЯТО КОНТРОЛЛЕРОМ <<<" if ok else "ack не сошёлся — пришли вывод.")
    else:
        print("неизвестная команда:", cmd)


if __name__ == "__main__":
    _main()
