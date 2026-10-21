"""
Эмулятор контроллеров WG3000/ACB для теста на своём ПК. Карты и события хранит в памяти.

    uv run fake_controller.py                                   # отвечает за любой серийник
    uv run fake_controller.py --serials 223218042,123228092     # только за эти (как настоящие)
    uv run fake_controller.py --fail-put 19999999               # отклонять запись этой карты (тест сбоев)

Приложить карту (из другого окна, пока эмулятор работает):
    uv run fake_controller.py swipe 13158327                    # вход, дверь 1, контроллер 223218042
    uv run fake_controller.py swipe 13158327 --out --door 2 --sn 123228092

Поведение — по описанию протокола WG3000/UHPPOTE (проект uhppoted) в тех местах, на которые опирается коннектор:
  * пакет — 64 байта, тип 0x17, байты 2–3 нулевые, серийник в 4–7 (little-endian);
  * чужой серийник или неизвестная функция → ответа НЕТ (контроллер молчит, у клиента таймаут);
  * 0x50 запись карты → байт 8 = 1 успех / 0 отказ;  0x52 удаление → 1 удалена / 0 не было;
  * 0x5A чтение карты → если карты нет, номер карты в ответе = 0;  0x58 число карт;
  * 0x20 статус: номер и поля последнего события, часы контроллера;
  * 0xB0 событие по номеру: нет такого → номер 0;
  * 0x90 / 0x92 адрес для push событий; при каждом проходе контроллер шлёт туда пакет статуса;
  * 0x30 установка часов.
Коды причин отказа в эмуляторе условные (1 — пропущен, 6 — карты нет или нет прав).
Эмулятор не заменяет проверку на железе: для неё есть `connector.py … --selftest <карта>`.

Ctrl+C — выключить. После перезапуска память пустая (карты и события), как у сброшенного контроллера.
"""
import argparse, socket, struct, sys
from datetime import datetime, timedelta

SWIPE = 0xF1   # служебная команда только этого эмулятора: «приложили карту»

KNOWN = {0x20: "статус", 0x30: "часы", 0x50: "запись карты", 0x52: "удаление карты", 0x58: "число карт",
         0x5A: "чтение карты", 0x90: "push: установить", 0x92: "push: прочитать", 0xB0: "событие", SWIPE: "проход"}


def bcd(dt, fmt):
    return bytes.fromhex(dt.strftime(fmt))


def swipe_command():
    ap = argparse.ArgumentParser(prog="fake_controller.py swipe")
    ap.add_argument("card", type=int)
    ap.add_argument("--sn", type=int, default=223218042)
    ap.add_argument("--door", type=int, default=1)
    ap.add_argument("--out", action="store_true", help="выход (по умолчанию вход)")
    ap.add_argument("--port", type=int, default=60000)
    a = ap.parse_args(sys.argv[2:])
    pkt = (bytes([0x17, SWIPE, 0, 0]) + struct.pack("<II", a.sn, a.card)
           + bytes([a.door, 2 if a.out else 1])).ljust(64, b"\0")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1)
    s.sendto(pkt, ("127.0.0.1", a.port))
    try:
        r, _ = s.recvfrom(64)
    except (socket.timeout, ConnectionResetError):
        sys.exit("эмулятор не ответил — он запущен? верный серийник?")
    idx = struct.unpack("<I", r[8:12])[0]
    print(f"карта {a.card}, контроллер {a.sn}, дверь {a.door}, {'выход' if a.out else 'вход'}: "
          f"{'ПРОПУЩЕН' if r[13] == 1 else 'ОТКАЗ'} (событие №{idx})")


def run_emulator():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serials", help="через запятую; по умолчанию — любой серийник")
    ap.add_argument("--fail-put", type=int, action="append", default=[], help="номер карты, запись которой отклонять")
    ap.add_argument("--port", type=int, default=60000)
    args = ap.parse_args()
    serials = {int(x) for x in args.serials.split(",")} if args.serials else None

    cards: dict[int, dict[int, bytes]] = {}      # serial -> card -> 19 байт записи (8..26)
    events: dict[int, list[bytes]] = {}          # serial -> события, 20 байт (12..31 пакета: тип..причина)
    listener: dict[int, tuple] = {}              # serial -> (ip, port, interval)
    clock_shift: dict[int, timedelta] = {}       # serial -> сдвиг часов относительно ПК

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", args.port))
    s.settimeout(0.5)                            # иначе на Windows Ctrl+C не прерывает recvfrom
    print(f"эмулятор слушает UDP {args.port}; серийники: {sorted(serials) if serials else 'любые'}"
          f"{'; отказ записи: ' + str(args.fail_put) if args.fail_put else ''}  (Ctrl+C — выключить)")

    def now():
        return datetime.now().strftime("%H:%M:%S")

    def status_packet(sn):
        ev = events.get(sn, [])
        clock = datetime.now() + clock_shift.get(sn, timedelta())
        r = bytearray(64)
        r[0], r[1] = 0x17, 0x20
        r[4:8] = struct.pack("<I", sn)
        if ev:
            r[8:12] = struct.pack("<I", len(ev))
            r[12:28] = ev[-1][:16]
        r[37:40] = bcd(clock, "%H%M%S")
        r[51:54] = bcd(clock, "%y%m%d")
        return r

    while True:
        try:
            p, addr = s.recvfrom(1024)
        except (socket.timeout, ConnectionResetError):  # тишина / Windows шумит после ответа закрытому сокету
            continue

        # --- проверка пакета: как у настоящего контроллера, на мусор он просто не отвечает
        if len(p) != 64 or p[0] != 0x17 or p[2:4] != b"\0\0":
            print(f"{now()} пропущен некорректный пакет от {addr[0]} ({len(p)} байт)")
            continue
        fn, sn = p[1], struct.unpack("<I", p[4:8])[0]
        if serials is not None and sn not in serials:
            continue                              # не наш серийник — молчим
        if fn not in KNOWN:
            print(f"{now()} [{sn}] неизвестная функция 0x{fn:02X} — без ответа")
            continue

        card = struct.unpack("<I", p[8:12])[0]
        d = cards.setdefault(sn, {})
        ev = events.setdefault(sn, [])
        r = bytearray(p[:8]) + bytearray(56)

        if fn == 0x50:                            # добавить / обновить карту
            if card == 0 or card in args.fail_put:
                r[8] = 0
                print(f"{now()} [{sn}] ! отказ записи карты {card}")
            else:
                d[card] = p[8:27]
                r[8] = 1
                doors = [i + 1 for i, v in enumerate(p[20:24]) if v]
                print(f"{now()} [{sn}] + карта {card}  {p[12:16].hex()} → {p[16:20].hex()}  двери {doors}   (всего {len(d)})")
        elif fn == 0x52:                          # удалить карту
            r[8] = 1 if d.pop(card, None) else 0
            print(f"{now()} [{sn}] − карта {card}   (всего {len(d)})")
        elif fn == 0x5A:                          # прочитать карту; нет карты → номер 0
            if card in d:
                r[8:27] = d[card]
        elif fn == 0x58:                          # сколько карт
            r[8:12] = struct.pack("<I", len(d))
        elif fn == 0x20:                          # статус
            r = status_packet(sn)
        elif fn == 0xB0:                          # событие по номеру; нет такого → номер 0
            if 1 <= card <= len(ev):
                r[8:12] = struct.pack("<I", card)
                r[12:28] = ev[card - 1][:16]
        elif fn == 0x90:                          # куда слать push
            ip, port, interval = socket.inet_ntoa(p[8:12]), struct.unpack("<H", p[12:14])[0], p[14]
            listener[sn] = (ip, port, interval)
            r[8] = 1
            print(f"{now()} [{sn}] push событий → {ip}:{port}")
        elif fn == 0x92:
            ip, port, interval = listener.get(sn, ("0.0.0.0", 0, 0))
            r[8:12], r[12:14], r[14] = socket.inet_aton(ip), struct.pack("<H", port), interval
        elif fn == 0x30:                          # установить часы
            try:
                t = datetime.strptime(p[8:15].hex(), "%Y%m%d%H%M%S")
                clock_shift[sn] = t - datetime.now()
                r[8:15] = p[8:15]
                print(f"{now()} [{sn}] часы выставлены: {t}")
            except ValueError:
                continue
        elif fn == SWIPE:                         # «приложили карту» (только эмулятор)
            door, direction = p[12], p[13]
            clock = datetime.now() + clock_shift.get(sn, timedelta())
            rec = d.get(card)
            today = clock.strftime("%Y%m%d")
            ok = bool(rec) and rec[4:8].hex() <= today <= rec[8:12].hex() and 1 <= door <= 4 and rec[12 + door - 1] != 0
            body = (bytes([1, 1 if ok else 0, door, direction]) + struct.pack("<I", card)
                    + bcd(clock, "%Y%m%d%H%M%S") + bytes([1 if ok else 6]))
            ev.append(body)
            r[8:12] = struct.pack("<I", len(ev))
            r[12:28] = body
            print(f"{now()} [{sn}] проход: карта {card}, дверь {door}, {'выход' if direction == 2 else 'вход'} — "
                  f"{'пропущен' if ok else 'ОТКАЗ'} (событие №{len(ev)})")
            if sn in listener and listener[sn][1]:
                s.sendto(bytes(status_packet(sn)), listener[sn][:2])
        s.sendto(bytes(r), addr)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "swipe":
        swipe_command()
    else:
        # Ctrl+C может прийти в любой строке цикла, не только во время recvfrom, — ловим снаружи
        try:
            run_emulator()
        except KeyboardInterrupt:
            print("эмулятор остановлен")
