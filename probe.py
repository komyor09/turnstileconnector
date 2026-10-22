"""
Диагностика связи с контроллером: шлёт пакеты и показывает всё, что пришло в ответ.

    uv run probe.py 192.168.140.13 222110572
    uv run probe.py 192.168.140.22 222114632
"""
import socket, struct, sys, time

ip, sn = sys.argv[1], int(sys.argv[2])


def packet(fn, serial, data=b""):
    return (bytes([0x17, fn, 0, 0]) + struct.pack("<I", serial) + data).ljust(64, b"\0")


def try_send(title, pkt, wait=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    s.settimeout(0.5)
    s.sendto(pkt, (ip, 60000))
    print(f"\n{title}: отправлено с {s.getsockname()[0]}:{s.getsockname()[1]} → {ip}:60000")
    print("  пакет:", pkt[:12].hex(" "), "…")
    end, got = time.monotonic() + wait, False
    while time.monotonic() < end:
        try:
            r, addr = s.recvfrom(1024)
        except socket.timeout:
            continue
        except OSError as e:
            print("  ошибка сокета:", e)
            break
        got = True
        print(f"  ОТВЕТ от {addr[0]}:{addr[1]}, {len(r)} байт, через {wait - (end - time.monotonic()):.2f} сек")
        print("  ", r[:32].hex(" "))
        if len(r) >= 8:
            print(f"   тип 0x{r[0]:02X}, функция 0x{r[1]:02X}, серийник в ответе {struct.unpack('<I', r[4:8])[0]}")
    if not got:
        print(f"  ответа нет за {wait} сек")
    s.close()


try_send("1. Статус (0x20) с серийником " + str(sn), packet(0x20, sn))
try_send("2. Поиск (0x94) с серийником 0 — контроллер должен назвать свой серийник", packet(0x94, 0))
try_send("3. Число карт (0x58)", packet(0x58, sn))

