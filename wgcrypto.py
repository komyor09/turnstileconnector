"""
Криптография контроллеров WGACCESS (ACB/N3000) — чистый Python, 1-в-1 с n3k_comm.dll.

Всё сверено с самой DLL (эмуляция x86-кода функций enc/dec/enc64/dec64/
calCRC_WGPacketShort/_ShortEncryptSM4_* на случайных данных). В DLL нет UDP-кода
(из сети там только SMTP: sendM = send mail), так что пакеты контроллеру
формирует сам N3000, а DLL — только шифр. Поэтому 32-битный Python не нужен.

    enc/dec        — AES-128-ECB по байтам [4:], хвост <16 байт XOR с байтом [4],
                     флаг шифрования — бит 0x80 в байте [0]
    enc64/dec64    — 64-байтный пакет: тип нечётный → +1, CRC (init 0xC78C) по
                     байтам [4:64] в [2..3] little-endian, затем [i] ^= i для i=1..63
    sm4_ecb/sm4_cbc — стандартный SM4. Причуда DLL: ТОЛЬКО в _ShortEncryptSM4_ECB
                     ключ "wg\\0\\0\\0\\0…" подменяется на "wiegand20170427" + key[15];
                     расшифровка ECB и оба CBC берут ключ как есть
"""
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = b"WGMASTERWgmaster"      # getK
IV = b"Wiegand1997csncl"       # getIV
KEY_DB = b"wgmasterWGMASTER"   # getKDB
IV_DB = b"wiegand2013csncl"    # getIVDB


def crc_short(data: bytes) -> int:
    """calCRC_WGPacketShort: CRC-16/CCITT, poly 0x1021, init 0xC78C."""
    crc = 0xC78C
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
    return crc & 0xFFFF


def enc64(pkt: bytes) -> bytes:
    p = bytearray(pkt[:64].ljust(64, b"\0"))
    if p[0] & 1:
        p[0] = (p[0] + 1) & 0xFF
    crc = crc_short(bytes(p[4:64]))
    p[2], p[3] = crc & 0xFF, crc >> 8
    for i in range(1, 64):
        p[i] ^= i
    return bytes(p)


def dec64(pkt: bytes) -> tuple[bytes, bool]:
    """→ (пакет, crc_ok). Как в DLL: тип чётный → -1, [i] ^= i, проверка CRC."""
    p = bytearray(pkt[:64].ljust(64, b"\0"))
    if p[0] & 1:
        return bytes(p), False
    p[0] = (p[0] - 1) & 0xFF
    for i in range(1, 64):
        p[i] ^= i
    crc = crc_short(bytes(p[4:64]))
    return bytes(p), (p[2] == crc & 0xFF and p[3] == crc >> 8)


def _aes(key, decrypt):
    c = Cipher(algorithms.AES(key), modes.ECB())
    return c.decryptor() if decrypt else c.encryptor()


def enc(pkt: bytes, key: bytes = KEY) -> bytes:
    """DLL enc(buf, len, key)."""
    p = bytearray(pkt)
    body = p[4:]
    if len(body) < 0 or len(p) < 4:
        raise ValueError("пакет короче 4 байт")
    first = body[0] if body else 0
    n = len(body) // 16 * 16
    c = _aes(key, False)
    out = bytearray(c.update(bytes(body[:n])) + c.finalize())
    out += bytes(b ^ first for b in body[n:])
    p[4:] = out
    p[0] |= 0x80
    return bytes(p)


def dec(pkt: bytes, key: bytes = KEY) -> bytes:
    """DLL dec(buf, len, key); ValueError, если нет флага 0x80."""
    p = bytearray(pkt)
    if not p[0] & 0x80:
        raise ValueError("пакет не зашифрован (нет бита 0x80)")
    body = p[4:]
    n = len(body) // 16 * 16
    c = _aes(key, True)
    out = bytearray(c.update(bytes(body[:n])) + c.finalize())
    first = out[0] if n else body[0]
    out += bytes(b ^ first for b in body[n:])
    p[4:] = out
    p[0] &= 0x7F
    return bytes(p)


def _sm4_key(key: bytes) -> bytes:
    k = bytearray(key[:16].ljust(16, b"\0"))
    if k[:6] == b"wg\0\0\0\0":
        k[1:15] = b"iegand20170427"
    return bytes(k)


def _sm4(key, mode, decrypt):
    c = Cipher(algorithms.SM4(key[:16].ljust(16, b"\0")), mode)
    return c.decryptor() if decrypt else c.encryptor()


def sm4_ecb(buf: bytes, key: bytes = KEY, decrypt=False) -> bytes:
    """_ShortEncryptSM4_ECB / _ShortDecryptSM4_ECB (длина 64 или 1024)."""
    c = _sm4(key if decrypt else _sm4_key(key), modes.ECB(), decrypt)
    return c.update(buf) + c.finalize()


def sm4_cbc(buf: bytes, key: bytes = KEY, iv: bytes = IV, decrypt=False) -> bytes:
    c = _sm4(key, modes.CBC(iv), decrypt)
    return c.update(buf) + c.finalize()
