"""
Эмулятор контроллеров WG3000/ACB для теста на своём ПК. Карты хранит в памяти.

    uv run fake_controller.py                                   # отвечает за любой серийник
    uv run fake_controller.py --serials 223218042,123228092     # только за эти (как настоящие)
    uv run fake_controller.py --fail-put 19999999               # отклонять запись этой карты (тест сбоев)

Поведение — по описанию протокола WG3000/UHPPOTE (проект uhppoted) в тех местах, на которые опирается коннектор:
  * пакет — 64 байта, тип 0x17, байты 2–3 нулевые, серийник в 4–7 (little-endian);
  * чужой серийник или неизвестная функция → ответа НЕТ (контроллер молчит, у клиента таймаут);
  * 0x50 запись карты → байт 8 = 1 успех / 0 отказ;
  * 0x52 удаление → байт 8 = 1 удалена / 0 такой не было;
  * 0x5A чтение карты → если карты нет, номер карты в ответе = 0 (так коннектор и проверяет);
  * 0x58 число карт, 0x20 статус.
Эмулятор не заменяет проверку на железе: для неё есть `connector.py … --selftest <карта>`.

Ctrl+C — выключить. Чтобы проверить ошибки и повторы — остановите и запустите снова.
"""
import argparse, socket, struct
from datetime import datetime

KNOWN = {0x20: "статус", 0x50: "запись карты", 0x52: "удаление карты", 0x58: "число карт", 0x5A: "чтение карты"}

ap = argparse.ArgumentParser()
ap.add_argument("--serials", help="через запятую; по умолчанию — любой серийник")
ap.add_argument("--fail-put", type=int, action="append", default=[], help="номер карты, запись которой отклонять")
ap.add_argument("--port", type=int, default=60000)
args = ap.parse_args()
serials = {int(x) for x in args.serials.split(",")} if args.serials else None

cards: dict[int, dict[int, bytes]] = {}          # serial -> card -> 19 байт записи (8..26)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", args.port))
s.settimeout(0.5)                                 # иначе на Windows Ctrl+C не прерывает recvfrom
print(f"эмулятор слушает UDP {args.port}; серийники: {sorted(serials) if serials else 'любые'}"
      f"{'; отказ записи: ' + str(args.fail_put) if args.fail_put else ''}  (Ctrl+C — выключить)")


def now():
    return datetime.now().strftime("%H:%M:%S")


while True:
    try:
        p, addr = s.recvfrom(1024)
    except (socket.timeout, ConnectionResetError):  # тишина / Windows шумит после ответа закрытому сокету
        continue
    except KeyboardInterrupt:
        print("эмулятор остановлен")
        break

    # --- проверка пакета: как у настоящего контроллера, на мусор он просто не отвечает
    if len(p) != 64 or p[0] != 0x17 or p[2:4] != b"\0\0":
        print(f"{now()} пропущен некорректный пакет от {addr[0]} ({len(p)} байт)")
        continue
    fn, sn = p[1], struct.unpack("<I", p[4:8])[0]
    if serials is not None and sn not in serials:
        continue                                  # не наш серийник — молчим
    if fn not in KNOWN:
        print(f"{now()} [{sn}] неизвестная функция 0x{fn:02X} — без ответа")
        continue

    card = struct.unpack("<I", p[8:12])[0]
    d = cards.setdefault(sn, {})
    r = bytearray(p[:8]) + bytearray(56)

    if fn == 0x50:                                # добавить / обновить карту
        if card == 0 or card in args.fail_put:
            r[8] = 0
            print(f"{now()} [{sn}] ! отказ записи карты {card}")
        else:
            d[card] = p[8:27]
            r[8] = 1
            doors = [i + 1 for i, v in enumerate(p[20:24]) if v]
            print(f"{now()} [{sn}] + карта {card}  {p[12:16].hex()} → {p[16:20].hex()}  двери {doors}   (всего {len(d)})")
    elif fn == 0x52:                              # удалить карту
        r[8] = 1 if d.pop(card, None) else 0
        print(f"{now()} [{sn}] − карта {card}   (всего {len(d)})")
    elif fn == 0x5A:                              # прочитать карту; нет карты → номер 0
        if card in d:
            r[8:27] = d[card]
    elif fn == 0x58:                              # сколько карт
        r[8:12] = struct.pack("<I", len(d))
    # 0x20 статус: достаточно эха заголовка
    s.sendto(bytes(r), addr)
