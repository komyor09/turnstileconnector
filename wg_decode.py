"""
Разбор захвата Wireshark (pcap/pcapng): UDP-пакеты порта 60000 прогоняются через
схемы из n3k_comm.dll (wgcrypto.py), показывается расшифровка, где виден серийник
контроллера или сходится CRC. Только читает файл, ничего не шлёт.

    uv run wg_decode.py capture.pcapng 222110572 [222114632 ...]
"""
import struct
import sys

import wgcrypto as W

PORT = 60000


def udp_payloads(path):
    """Достаёт (src_port, dst_port, payload) из pcap/pcapng. Ethernet/IPv4/UDP."""
    data = open(path, "rb").read()
    magic = data[:4]
    out = []

    def parse_eth_ip_udp(frame, linktype):
        if linktype == 1:  # Ethernet
            if len(frame) < 14 or frame[12:14] != b"\x08\x00":
                return None
            ip = frame[14:]
        elif linktype in (101, 12, 14):  # raw IP
            ip = frame
        else:
            return None
        if len(ip) < 20 or ip[0] >> 4 != 4:
            return None
        ihl = (ip[0] & 0xF) * 4
        if ip[9] != 17:  # UDP
            return None
        udp = ip[ihl:]
        if len(udp) < 8:
            return None
        sp, dp, ln = struct.unpack(">HHH", udp[:6])
        return sp, dp, udp[8:ln] if 8 <= ln <= len(udp) else udp[8:]

    if magic == b"\xd4\xc3\xb2\xa1" or magic == b"\xa1\xb2\xc3\xd4":
        be = magic == b"\xa1\xb2\xc3\xd4"
        e = ">" if be else "<"
        linktype = struct.unpack(e + "I", data[20:24])[0]
        pos = 24
        while pos + 16 <= len(data):
            _, _, caplen, _ = struct.unpack(e + "IIII", data[pos:pos + 16])
            pos += 16
            frame = data[pos:pos + caplen]
            pos += caplen
            r = parse_eth_ip_udp(frame, linktype)
            if r:
                out.append(r)
    elif magic == b"\x0a\x0d\x0d\x0a":  # pcapng
        e = "<"
        pos = 0
        linktype = 1
        while pos + 12 <= len(data):
            btype, blen = struct.unpack(e + "II", data[pos:pos + 8])
            if btype == 0x0A0D0D0A:
                bom = data[pos + 8:pos + 12]
                e = ">" if bom == b"\x1a\x2b\x3c\x4d" else "<"
                btype, blen = struct.unpack(e + "II", data[pos:pos + 8])
            block = data[pos:pos + blen]
            if btype == 1:  # Interface Description
                linktype = struct.unpack(e + "H", block[8:10])[0]
            elif btype == 6:  # Enhanced Packet
                caplen = struct.unpack(e + "I", block[20:24])[0]
                frame = block[28:28 + caplen]
                r = parse_eth_ip_udp(frame, linktype)
                if r:
                    out.append(r)
            elif btype == 3:  # Simple Packet
                caplen = blen - 16
                frame = block[12:12 + caplen]
                r = parse_eth_ip_udp(frame, linktype)
                if r:
                    out.append(r)
            if blen < 12:
                break
            pos += blen
    else:
        raise SystemExit("Не pcap и не pcapng: " + magic.hex())
    return out


def try_decode(payload, serials):
    """Возвращает список (схема, расшифровка, пометка) для одного пакета."""
    res = []
    p = payload
    if len(p) >= 64:
        p64 = p[:64]
        dec, ok = W.dec64(p64)
        sn = struct.unpack("<I", dec[4:8])[0]
        note = []
        if ok:
            note.append("CRC OK")
        if sn in serials:
            note.append(f"серийник {sn}")
        res.append(("dec64", dec, ", ".join(note) or "—"))
    if len(p) >= 4 and p[0] & 0x80:
        for name, key in [("AES", W.KEY), ("AES_DB", W.KEY_DB)]:
            try:
                d = W.dec(p, key)
                sn = struct.unpack("<I", d[4:8])[0] if len(d) >= 8 else 0
                res.append((name, d, f"серийник {sn}" if sn in serials else "—"))
            except Exception:
                pass
    # Короткий протокол (0x20) — SM4. Смещение заголовка неизвестно, пробуем 0/4/8.
    if p[:1] == b"\x20":
        for off in (0, 4, 8):
            body = p[off:]
            n = len(body) // 16 * 16
            if n < 16:
                continue
            for kname, key in [("SM4", W.KEY), ("SM4_DB", W.KEY_DB)]:
                for mname, dec in [("ECB", W.sm4_ecb(body[:n], key, True)),
                                   ("CBC", W.sm4_cbc(body[:n], key, W.IV, True))]:
                    hdr = p[:off] + dec
                    sn = struct.unpack("<I", hdr[4:8])[0] if len(hdr) >= 8 else 0
                    tag = f"серийник {sn}" if sn in serials else "—"
                    res.append((f"{kname}/{mname}@{off}", hdr, tag))
    return res


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    serials = {int(x) for x in sys.argv[2:]}
    pkts = udp_payloads(path)
    hits = [p for p in pkts if PORT in (p[0], p[1])]
    print(f"Всего UDP: {len(pkts)}, из них порт {PORT}: {len(hits)}\n")
    for i, (sp, dp, pl) in enumerate(hits):
        direction = "N3000 → контроллер" if dp == PORT else "контроллер → N3000"
        print(f"[{i}] {direction}  {sp}→{dp}  {len(pl)} байт")
        print("    сырое:", pl[:16].hex(" "), "…" if len(pl) > 16 else "")
        for scheme, dec, note in try_decode(pl, serials):
            fn = dec[1] if len(dec) > 1 else 0
            print(f"    {scheme:7} [{note}]  тип=0x{dec[0]:02X} fn=0x{fn:02X}")
            print("           ", dec[:32].hex(" "))
        print()


if __name__ == "__main__":
    main()
