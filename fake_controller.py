"""
Эмулятор контроллеров WG3000/ACB для теста на своём ПК.
Слушает UDP 60000 и отвечает за любой серийный номер, карты хранит в памяти.

    uv run fake_controller.py

Чтобы проверить ошибки и повторы — просто остановите его (Ctrl+C) и запустите снова.
"""
import socket, struct
from datetime import datetime

cards: dict[int, dict[int, bytes]] = {}          # serial -> card -> запись

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 60000))
s.settimeout(0.5)                                 # иначе на Windows Ctrl+C не прерывает recvfrom
print("эмулятор контроллеров слушает UDP 60000 (Ctrl+C — выключить)")

while True:
    try:
        p, addr = s.recvfrom(1024)
    except (socket.timeout, ConnectionResetError):  # тишина / Windows шумит после ответа закрытому сокету
        continue
    except KeyboardInterrupt:
        print("эмулятор остановлен")
        break
    if len(p) < 64 or p[0] != 0x17:
        continue
    fn, sn = p[1], struct.unpack("<I", p[4:8])[0]
    card = struct.unpack("<I", p[8:12])[0]
    d = cards.setdefault(sn, {})
    r = bytearray(p[:8]) + bytearray(56)
    now = datetime.now().strftime("%H:%M:%S")

    if fn == 0x50:                                # добавить / обновить карту
        d[card] = p[8:27]
        r[8] = 1
        doors = [i + 1 for i, v in enumerate(p[20:24]) if v]
        print(f"{now} [{sn}] + карта {card}  {p[12:16].hex()} → {p[16:20].hex()}  двери {doors}   (всего {len(d)})")
    elif fn == 0x52:                              # удалить карту
        r[8] = 1 if d.pop(card, None) else 0
        print(f"{now} [{sn}] − карта {card}   (всего {len(d)})")
    elif fn == 0x5A and card in d:                # прочитать карту
        r[8:27] = d[card]
    elif fn == 0x58:                              # сколько карт
        r[8:12] = struct.pack("<I", len(d))
    s.sendto(bytes(r), addr)
