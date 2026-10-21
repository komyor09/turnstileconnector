"""
Коннектор «база ↔ турникеты». Отдельный сервис (служба Windows).

Две задачи:
  1. Карты: база → турникеты.
     Вход:   HTTP-вебхук POST /sync — его вызывает ISU после изменения списка жильцов.
             Нет вызовов — коннектор базу не трогает. Плюс полная сверка при запуске и, по желанию,
             раз в сутки (sync.reconcile_at) — на случай, если изменение прошло мимо вебхука.
     Выход:  контроллеры WG3000 / ACB по UDP 60000
     Ошибки: после max_attempts неудач — строка в turnstile_errors, дальше повтор раз в after_fail сек.
  2. Проходы: турникеты → база (секция [events] в конфиге).
     Контроллер сам присылает пакет при каждом проходе (push на listen_port) — это «звонок».
     По звонку коннектор читает новые события по номерам, начиная с последнего сохранённого
     (turnstile_event_cursor), и пишет их в turnstile_events. Пропусков нет даже после простоя.

Запуск:
    uv run connector.py config.test.toml            # работать
    uv run connector.py config.test.toml --check    # только проверить базу и контроллеры
    uv run connector.py config.toml --force         # при старте переписать все карты на контроллерах заново
    uv run connector.py config.toml --selftest 99999999   # проверить протокол на настоящем контроллере тестовой картой
"""
import heapq, itertools, json, logging, logging.handlers, os, socket, struct, sys, threading, time, tomllib
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymysql

log = logging.getLogger("turnstile")

# Заполняется из config.toml в load_config()
DB: dict = {}
VIEW = ""
RECONCILE_AT = ""      # "03:00" — полная сверка раз в сутки; "" — только при запуске
DRY_RUN = False
MAX_ATTEMPTS, RETRY_DELAYS, AFTER_FAIL = 5, [2, 5, 15, 60], 3600
HTTP_BIND, HTTP_PORT, HTTP_TOKEN = "0.0.0.0", 8090, ""
LOG: dict = {}
controllers: list = []
EVENTS: dict = {}      # секция [events]; пусто или enabled = false — проходы не читаются


def load_config(path):
    global DB, VIEW, RECONCILE_AT, DRY_RUN, MAX_ATTEMPTS, RETRY_DELAYS, AFTER_FAIL, HTTP_BIND, HTTP_PORT, HTTP_TOKEN
    global controllers, EVENTS, LOG
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    DB = cfg["db"]
    s = cfg["sync"]
    VIEW, DRY_RUN = s["view"], s.get("dry_run", False)
    MAX_ATTEMPTS = s.get("max_attempts", 5)
    RETRY_DELAYS = s.get("retry_delays", [2, 5, 15, 60])
    AFTER_FAIL = s.get("after_fail", 3600)
    RECONCILE_AT = s.get("reconcile_at", "")
    if RECONCILE_AT:
        datetime.strptime(RECONCILE_AT, "%H:%M")         # ошибка формата — сразу при запуске
    h = cfg["http"]
    HTTP_BIND, HTTP_PORT, HTTP_TOKEN = h.get("bind", "0.0.0.0"), h["port"], h["token"]
    if len(HTTP_TOKEN) < 16:
        raise SystemExit("http.token слишком короткий: нужно не меньше 16 символов")
    LOG = cfg.get("log", {})
    controllers = [Controller(c["sn"], c["ip"], {d: 1 for d in c["doors"]}, c.get("port", 60000))
                   for c in cfg["controllers"]]
    if len(RETRY_DELAYS) < MAX_ATTEMPTS - 1:
        raise SystemExit("retry_delays должен содержать max_attempts - 1 значений")
    e = cfg.get("events", {})
    EVENTS = {} if not e.get("enabled", False) else {
        "listen_port": e.get("listen_port", 60001),            # 0 = без push (тогда нужен poll_seconds)
        "listener_address": e.get("listener_address", ""),     # IP коннектора для контроллеров; "" = не настраивать
        "poll_seconds": e.get("poll_seconds", 0),               # 0 = только по push, без периодической сверки
        "history_on_first_start": e.get("history_on_first_start", False),
        "sync_time_hours": e.get("sync_time_hours", 24),       # 0 = не трогать часы контроллеров
        "batch": e.get("batch", 500),
    }
    if EVENTS and not EVENTS["listen_port"] and not EVENTS["poll_seconds"]:
        raise SystemExit("events: без push (listen_port = 0) нужен poll_seconds > 0, иначе проходы не читаются")


def log_error(cur, sid, card_no, action, error, attempts):
    """Запись ошибки после MAX_ATTEMPTS. Поменяйте на свою таблицу, если нужно."""
    cur.execute("INSERT INTO turnstile_errors (student_id, card_no, action, error, attempts)"
                " VALUES (%s, %s, %s, LEFT(%s, 500), %s)", (sid, card_no, action, error, attempts))


def resolve_errors(cur, sid):
    cur.execute("UPDATE turnstile_errors SET resolved_at = NOW()"
                " WHERE student_id = %s AND resolved_at IS NULL", (sid,))

# ============================ КОНТРОЛЛЕР ============================

class Controller:
    """WG3000 / UHPPOTE: UDP, 64 байта, little-endian, даты в BCD."""

    def __init__(self, sn, ip, doors, port=60000, timeout=1.0):
        self.sn, self.ip, self.port, self.timeout = sn, ip, port, timeout
        self.doors = bytes(int(doors.get(d, 0)) for d in (1, 2, 3, 4))
        self._ok_until = 0.0

    def _call(self, fn, data=b""):
        pkt = (bytes([0x17, fn, 0, 0]) + struct.pack("<I", self.sn) + data).ljust(64, b"\0")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(self.timeout)
            s.sendto(pkt, (self.ip, self.port))
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                try:
                    r, _ = s.recvfrom(1024)
                except (socket.timeout, ConnectionError):   # Windows: WinError 10054 = ConnectionResetError
                    break
                if len(r) >= 64 and r[0] == 0x17 and r[1] == fn and struct.unpack("<I", r[4:8])[0] == self.sn:
                    return r
        raise IOError(f"контроллер {self.sn} ({self.ip}) не отвечает")

    def alive(self):
        """Пинг с кэшем на 5 сек, чтобы при упавшем контроллере не ждать таймаут на каждом студенте."""
        if time.monotonic() < self._ok_until:
            return True
        try:
            self._call(0x20)
            self._ok_until = time.monotonic() + 5
            return True
        except IOError:
            return False

    def put_card(self, card, d_from: date, d_to: date):
        bcd = lambda d: bytes.fromhex(d.strftime("%Y%m%d"))
        r = self._call(0x50, struct.pack("<I", card) + bcd(d_from) + bcd(d_to) + self.doors + b"\0\0\0")
        if r[8] != 1:
            raise IOError(f"контроллер {self.sn} отклонил карту {card}")

    def delete_card(self, card):
        self._call(0x52, struct.pack("<I", card))

    def get_card(self, card):
        """(card, from 'YYYYMMDD', to 'YYYYMMDD', doors) или None. Нет карты → контроллер возвращает номер 0."""
        r = self._call(0x5A, struct.pack("<I", card))
        if struct.unpack("<I", r[8:12])[0] != card:
            return None
        return card, r[12:16].hex(), r[16:20].hex(), list(r[20:24])

    def has_card(self, card):
        return self.get_card(card) is not None

    def card_count(self):
        return struct.unpack("<I", self._call(0x58)[8:12])[0]

    # --- события (проходы) ---

    def status(self):
        """Последний номер события, само событие и часы контроллера."""
        r = self._call(0x20)
        clock = None
        try:   # дата yymmdd в 51..53, время HHMMSS в 37..39 (BCD)
            clock = datetime.strptime("20" + r[51:54].hex() + r[37:40].hex(), "%Y%m%d%H%M%S")
        except ValueError:
            pass
        return {"last_index": struct.unpack("<I", r[8:12])[0], "event": parse_event(r), "clock": clock}

    def get_event(self, index):
        """Событие №index. None — такого нет; {"overwritten": True} — уже затёрто (память кольцевая)."""
        return parse_event(self._call(0xB0, struct.pack("<I", index)))

    def set_listener(self, ip, port, interval=0):
        """Куда контроллеру слать события. interval=0 — только при событии, без периодических пакетов."""
        r = self._call(0x90, socket.inet_aton(ip) + struct.pack("<H", port) + bytes([interval]))
        return r[8] == 1

    def get_listener(self):
        r = self._call(0x92)
        return socket.inet_ntoa(r[8:12]), struct.unpack("<H", r[12:14])[0], r[14]

    def set_time(self, dt: datetime):
        r = self._call(0x30, bytes.fromhex(dt.strftime("%Y%m%d%H%M%S")))
        return bcd_datetime(r[8:15])


def bcd_datetime(b):
    try:
        return datetime.strptime(b.hex(), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def parse_event(r):
    """Поля события одинаковы в ответе статуса (0x20), чтения события (0xB0) и push-пакете."""
    index = struct.unpack("<I", r[8:12])[0]
    if index == 0:
        return None
    if r[12] == 0xFF:
        return {"index": index, "overwritten": True}
    return {"index": index, "type": r[12], "granted": r[13] == 1, "door": r[14], "direction": r[15],
            "card": struct.unpack("<I", r[16:20])[0], "time": bcd_datetime(r[20:27]), "reason": r[27]}

# ============================ ОЧЕРЕДЬ ============================

class Queue:
    """Очередь студентов на синхронизацию.

    Новое событие по студенту отменяет его старый повтор. Флаг force хранится в самой очереди
    под тем же замком: если по студенту уже ждёт задача с force, новая задача его наследует,
    и флаг не теряется, пока задача не будет выполнена.
    """

    def __init__(self):
        self.heap, self.latest, self.cv, self.seq = [], {}, threading.Condition(), itertools.count()

    def add(self, sid, attempt=0, delay=0.0, force=False):
        with self.cv:
            prev = self.latest.get(sid)
            force = force or (prev is not None and prev[1])
            tok = next(self.seq)
            self.latest[sid] = (tok, force)
            heapq.heappush(self.heap, (time.monotonic() + delay, tok, sid, attempt))
            self.cv.notify()

    def get(self):
        """Возвращает (sid, attempt, force)."""
        with self.cv:
            while True:
                if self.heap:
                    due, tok, sid, attempt = self.heap[0]
                    cur = self.latest.get(sid)
                    if cur is None or cur[0] != tok:          # устарело — было событие новее
                        heapq.heappop(self.heap)
                        continue
                    wait = due - time.monotonic()
                    if wait <= 0:
                        heapq.heappop(self.heap)
                        del self.latest[sid]
                        return sid, attempt, cur[1]
                    self.cv.wait(wait)
                else:
                    self.cv.wait()

    def size(self):
        with self.cv:
            return len(self.latest)


queue = Queue()

# ============================ СИНХРОНИЗАЦИЯ ============================

def connect():
    return pymysql.connect(**DB, autocommit=True, cursorclass=pymysql.cursors.DictCursor)


def alive_conn(conn):
    """Возвращает живое соединение: старое, если оно отвечает, иначе новое."""
    try:
        conn.ping(reconnect=False)
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return connect()


def as_date(v, default):
    """Дата из вьюхи: DATE, DATETIME или строка '2026-10-01' (если во вьюхе даты заданы константой)."""
    if v is None or v == "":
        return default
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, (str, bytes)):
        return date.fromisoformat((v.decode() if isinstance(v, bytes) else v)[:10])
    return v


def sync_student(cur, sid, force=False):
    """Приводит турникеты к состоянию студента во вьюхе. Возвращает действие или None.
    force=True — записать заново, даже если по turnstile_synced всё уже совпадает
    (контроллер заменили/сбросили, или кто-то загрузил права из N3000)."""
    cur.execute(f"SELECT card_no, valid_from, valid_to FROM {VIEW}"
                " WHERE student_id = %s AND card_no > 0 LIMIT 1", (sid,))
    w = cur.fetchone()
    want = (int(w["card_no"]), as_date(w["valid_from"], date.today()),
            as_date(w["valid_to"], date(2099, 12, 31))) if w else None

    cur.execute("SELECT card_no, valid_from, valid_to FROM turnstile_synced WHERE student_id = %s", (sid,))
    h = cur.fetchone()
    have = (int(h["card_no"]), h["valid_from"], h["valid_to"]) if h else None

    if want == have and not (force and want):
        return None

    if DRY_RUN:
        fmt = lambda x: f"карта {x[0]} ({x[1]} — {x[2]})" if x else "нет"
        return f"[DRY-RUN] на турникетах: {fmt(have)}  →  должно быть: {fmt(want)}"

    down = [f"{c.sn} ({c.ip})" for c in controllers if not c.alive()]
    if down:
        raise IOError("контроллер недоступен: " + ", ".join(down))

    # 1) Сначала новая карта — на ВСЕ контроллеры, с проверкой. Если где-то не вышло, старую карту
    #    не трогаем: студент сохраняет доступ по старой, задача повторится. put идемпотентен,
    #    поэтому повтор после частичного сбоя просто допишет недостающий контроллер.
    if want:
        for c in controllers:
            c.put_card(*want)
            if not c.has_card(want[0]):
                raise IOError(f"карта {want[0]} не появилась на контроллере {c.sn}")

    # 2) Только потом убираем старую карту (выселение или перевыпуск) — если она больше никому не нужна.
    old = have[0] if have and (not want or have[0] != want[0]) else None
    if old and card_in_use_by_other(cur, old, sid):
        log.warning("студент %s: карта %s теперь у другого жителя — с контроллеров не удаляю", sid, old)
        old = None
    if old:
        for c in controllers:
            c.delete_card(old)

    if want:
        cur.execute("INSERT INTO turnstile_synced (student_id, card_no, valid_from, valid_to)"
                    " VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE card_no = VALUES(card_no),"
                    " valid_from = VALUES(valid_from), valid_to = VALUES(valid_to)", (sid, *want))
        return (f"put {want[0]} ({want[1]} — {want[2]})" + (f", старая карта {old} удалена" if old else "")
                + (" [force]" if force and want == have else ""))
    cur.execute("DELETE FROM turnstile_synced WHERE student_id = %s", (sid,))
    if old:
        return f"delete {old}"
    return f"снят с учёта, карта {have[0]} оставлена на контроллерах — перевыдана другому жителю"


def card_in_use_by_other(cur, card_no, sid):
    """Карту перевыдали другому жителю? Тогда удалять её с контроллеров нельзя."""
    cur.execute(f"SELECT 1 FROM {VIEW} WHERE card_no = %s AND student_id <> %s LIMIT 1", (card_no, sid))
    return cur.fetchone() is not None


DB_ERRORS = (pymysql.err.OperationalError, pymysql.err.InterfaceError)


def worker():
    """Разбирает очередь. Подключение к базе — внутри обработки ошибок:
    база недоступна → задача возвращается в очередь без траты попыток, пауза растёт до 60 сек."""
    conn, db_backoff = None, 1
    while True:
        sid, attempt, force = queue.get()
        try:
            conn = alive_conn(conn) if conn else connect()
            if db_backoff > 1:
                log.info("база снова доступна")
            db_backoff = 1
            with conn.cursor() as cur:
                action = sync_student(cur, sid, force=force)
                if action:
                    if not DRY_RUN:
                        resolve_errors(cur, sid)
                    log.info("студент %s: %s", sid, action)
        except DB_ERRORS as e:
            # База недоступна — это не ошибка турникета: попытку не считаем, ждём базу.
            log.error("база недоступна (%s) — студент %s вернётся в очередь через %s сек", e, sid, db_backoff)
            conn = None
            queue.add(sid, attempt, db_backoff, force=force)
            time.sleep(db_backoff)
            db_backoff = min(db_backoff * 2, 60)
        except Exception as e:
            attempt += 1
            log.warning("студент %s: попытка %s — %s", sid, attempt, e)
            try:
                if attempt == MAX_ATTEMPTS:
                    conn = alive_conn(conn) if conn else connect()
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT (SELECT card_no FROM {VIEW} WHERE student_id = %s AND card_no > 0 LIMIT 1) AS want,"
                                    " (SELECT card_no FROM turnstile_synced WHERE student_id = %s) AS have", (sid, sid))
                        r = cur.fetchone()
                        log_error(cur, sid, r["want"] or r["have"], "put" if r["want"] else "delete", str(e), attempt)
                    log.error("студент %s: записано в turnstile_errors", sid)
            except Exception as e2:
                log.error("не смог записать ошибку: %s", e2)
                conn = None
            delay = RETRY_DELAYS[attempt - 1] if attempt < MAX_ATTEMPTS else AFTER_FAIL
            queue.add(sid, attempt, delay, force=force)


def supervised(target, *args):
    """Если фоновый поток всё-таки упал — завершаем процесс целиком, чтобы Docker/NSSM его перезапустили,
    а не оставили «живой» сервис без нужного потока."""
    def run():
        try:
            target(*args)
        except BaseException:
            log.exception("поток %s упал — перезапуск процесса", target.__name__)
        finally:
            os._exit(1)
    return threading.Thread(target=run, name=target.__name__, daemon=True)


# ============================ ВЕБХУК ============================

class Webhook(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"queue": queue.size(), "dry_run": DRY_RUN,
                                    "controllers": {c.sn: c.alive() for c in controllers},
                                    "events": {"enabled": bool(EVENTS), "last_push": last_push}})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {HTTP_TOKEN}":
            return self._send(401, {"error": "unauthorized"})
        if self.path != "/sync":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "ожидается JSON-объект"})
        force = bool(body.get("force"))
        if body.get("all"):
            try:
                n = enqueue_all(force=force)
            except DB_ERRORS as e:
                return self._send(503, {"error": f"база недоступна: {e}"})
            log.info("вебхук: полная сверка%s, %s студентов", " (force)" if force else "", n)
            return self._send(202, {"queued": n})
        ids = body.get("student_ids")
        if not isinstance(ids, list) or not ids:
            return self._send(400, {"error": "нужно student_ids: [..] или all: true"})
        try:
            ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            return self._send(400, {"error": "student_ids — только числа"})
        for sid in ids:
            queue.add(sid, force=force)
        log.info("вебхук: студенты %s%s", ids if len(ids) <= 20 else f"{ids[:20]}… (всего {len(ids)})",
                 " (force)" if force else "")
        self._send(202, {"queued": len(ids)})

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


def enqueue_all(force=False, delay=0.0):
    """Полная сверка: все из вьюхи + все, что уже стоит на турникетах.
    force=True — переписать все карты на контроллерах заново (≈ минута на 2000 студентов)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT student_id FROM {VIEW} UNION SELECT student_id FROM turnstile_synced")
        ids = [r["student_id"] for r in cur.fetchall()]
    for sid in ids:
        queue.add(int(sid), delay=delay, force=force)
    return len(ids)

def nightly_reconcile():
    """Раз в сутки в RECONCILE_AT — полная сверка (страховка, если изменение прошло мимо вебхука)."""
    hh, mm = map(int, RECONCILE_AT.split(":"))
    while True:
        now = datetime.now()
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        time.sleep((at - now).total_seconds())
        try:
            log.info("ночная сверка: %s студентов в очереди", enqueue_all())
        except DB_ERRORS as e:
            log.error("ночная сверка не выполнена — база недоступна: %s", e)

# ============================ ПРОХОДЫ: ТУРНИКЕТЫ → БАЗА ============================

events_wake = threading.Event()     # «звонок»: пришёл push или осталась недочитанная пачка
last_push: dict = {}                # sn → время последнего push (для /health)
DIRECTION = {1: "вход", 2: "выход"}


def save_cursor(cur, sn, index):
    cur.execute("INSERT INTO turnstile_event_cursor (controller_sn, last_index) VALUES (%s, %s)"
                " ON DUPLICATE KEY UPDATE last_index = VALUES(last_index)", (sn, index))


def store_event(cur, sn, ev):
    cur.execute("SELECT student_id FROM turnstile_synced WHERE card_no = %s LIMIT 1", (ev["card"],))
    r = cur.fetchone()
    sid = r["student_id"] if r else None
    cur.execute("INSERT IGNORE INTO turnstile_events (controller_sn, event_index, event_time, event_type, card_no,"
                " student_id, door, direction, granted, reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (sn, ev["index"], ev["time"] or datetime(1970, 1, 1), ev["type"], ev["card"], sid,
                 ev["door"], ev["direction"], int(ev["granted"]), ev["reason"]))
    if ev["type"] == 1:
        log.info("проход: карта %s%s, дверь %s, %s — %s (%s, №%s)", ev["card"],
                 f" (студент {sid})" if sid else " (нет в списке)", ev["door"],
                 DIRECTION.get(ev["direction"], ev["direction"]),
                 "пропущен" if ev["granted"] else f"ОТКАЗ, код {ev['reason']}", ev["time"], ev["index"])
    else:
        log.info("событие контроллера %s: тип %s, дверь %s, код %s (№%s)", sn, ev["type"], ev["door"], ev["reason"], ev["index"])


def same_event_as_stored(cur, c, index):
    """Событие №index в контроллере — то же, что мы сохранили? Ловит сброс памяти, после которого
    новых событий набралось больше, чем было прочитано (номер тогда не уменьшается)."""
    cur.execute("SELECT event_time, card_no FROM turnstile_events WHERE controller_sn = %s AND event_index = %s"
                " ORDER BY id DESC LIMIT 1", (c.sn, index))
    row = cur.fetchone()
    if row is None:                       # не с чем сравнить (история пропущена или событие было затёрто)
        return True
    ev = c.get_event(index)
    if ev is None or ev.get("overwritten"):
        return True
    return (ev["time"] or datetime(1970, 1, 1)) == row["event_time"] and ev["card"] == row["card_no"]


def fetch_new_events(cur, c):
    """Дочитывает события контроллера c от сохранённого номера до последнего. Возвращает True, если осталось ещё."""
    last = c.status()["last_index"]
    cur.execute("SELECT last_index FROM turnstile_event_cursor WHERE controller_sn = %s", (c.sn,))
    row = cur.fetchone()
    if row is None:
        start = 0 if EVENTS["history_on_first_start"] else last
        save_cursor(cur, c.sn, start)
        log.info("контроллер %s: первый запуск чтения проходов — %s", c.sn,
                 "читаю всю историю из памяти контроллера" if EVENTS["history_on_first_start"]
                 else f"старые события (до №{last}) пропускаю, читаю только новые")
        cursor = start
    else:
        cursor = row["last_index"]
    if last < cursor or (last > cursor > 0 and not same_event_as_stored(cur, c, cursor)):
        log.warning("контроллер %s: события в памяти не совпадают с сохранёнными (сейчас до №%s, прочитано до №%s) — "
                    "память очищена или контроллер заменён; читаю с начала", c.sn, last, cursor)
        cursor = 0
        save_cursor(cur, c.sn, 0)
        if push_enabled():
            configure_listener(c)            # после сброса адрес push пропадает — вернуть сразу, не ждать 5 минут

    end = min(last, cursor + EVENTS["batch"])
    skipped = 0
    for i in range(cursor + 1, end + 1):
        ev = c.get_event(i)
        if ev is None:
            break
        if ev.get("overwritten"):
            skipped += 1
        else:
            store_event(cur, c.sn, ev)
        save_cursor(cur, c.sn, i)
    if skipped:
        log.warning("контроллер %s: %s событий уже затёрты в памяти контроллера — пропущены", c.sn, skipped)
    return end < last


def events_worker():
    """Один поток пишет события. База недоступна — ждёт без потери: номер в курсоре не двигается."""
    conn, backoff, next_time_sync = None, 1, 0.0
    poll = EVENTS["poll_seconds"]
    if push_enabled():
        configure_listeners()           # здесь, а не в main: недоступный контроллер не задерживает запуск вебхука
    down: set = set()                       # контроллеры, о недоступности которых уже написали
    next_listener_check = time.monotonic() + 300
    while True:
        events_wake.clear()
        more = False
        try:
            conn = alive_conn(conn) if conn else connect()
            if backoff > 1:
                log.info("проходы: база снова доступна")
            backoff = 1
            with conn.cursor() as cur:
                for c in controllers:
                    try:
                        more |= fetch_new_events(cur, c)
                        if c.sn in down:
                            down.discard(c.sn)
                            log.info("проходы: контроллер %s снова отвечает", c.sn)
                            if push_enabled():
                                configure_listener(c)
                    except IOError as e:
                        if c.sn not in down:    # пишем один раз, а не при каждой попытке
                            down.add(c.sn)
                            log.warning("проходы: %s — продолжу пробовать, напишу, когда ответит", e)
        except DB_ERRORS as e:
            log.error("проходы: база недоступна (%s) — повтор через %s сек", e, backoff)
            conn = None
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if EVENTS["sync_time_hours"] and time.monotonic() >= next_time_sync:
            for c in controllers:
                try:
                    c.set_time(datetime.now())
                except IOError as e:
                    log.warning("часы контроллера %s не выставлены: %s", c.sn, e)
            log.info("часы контроллеров выставлены по серверу")
            next_time_sync = time.monotonic() + EVENTS["sync_time_hours"] * 3600

        # Контроллер заменили или сбросили — адрес push пропал. Раз в 5 минут проверяем и восстанавливаем.
        if push_enabled() and time.monotonic() >= next_listener_check:
            configure_listeners()
            next_listener_check = time.monotonic() + 300

        if not more:
            # Ждём push. Просыпаемся сами только к плановым делам с контроллерами
            # (часы, проверка адреса push) и — если включено — к страховочной сверке.
            deadlines = [next_listener_check] if push_enabled() else []
            if EVENTS["sync_time_hours"]:
                deadlines.append(next_time_sync)
            if poll:
                deadlines.append(time.monotonic() + poll)
            events_wake.wait(max(0.0, min(deadlines) - time.monotonic()) if deadlines else None)


def push_listener():
    """Принимает push-пакеты контроллеров. Сам ничего не пишет — будит events_worker,
    а тот читает события по номерам. Так у push и опроса один путь записи и нет гонок."""
    known = {c.sn for c in controllers}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", EVENTS["listen_port"]))
    log.info("проходы: жду push от контроллеров на UDP %s", EVENTS["listen_port"])
    while True:
        try:
            p, addr = s.recvfrom(1024)
        except ConnectionResetError:          # Windows
            continue
        if len(p) >= 64 and p[0] == 0x17 and p[1] == 0x20:
            sn = struct.unpack("<I", p[4:8])[0]
            if sn in known:
                last_push[sn] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                events_wake.set()


def push_enabled():
    return bool(EVENTS.get("listener_address") and EVENTS.get("listen_port"))


def configure_listener(c):
    """Указывает контроллеру, куда слать события. Перезаписывает прежнюю настройку (например, N3000)."""
    addr, port = EVENTS["listener_address"], EVENTS["listen_port"]
    try:
        before = c.get_listener()
        if before[:2] != (addr, port):
            c.set_listener(addr, port, 0)
            log.info("контроллер %s: push событий %s:%s (было %s:%s)", c.sn, addr, port, before[0], before[1])
        after = c.get_listener()
        if after[:2] != (addr, port):
            log.error("контроллер %s: push не настроился, сейчас %s:%s", c.sn, after[0], after[1])
    except IOError as e:
        log.error("контроллер %s: настройка push — %s", c.sn, e)


def configure_listeners():
    for c in controllers:
        configure_listener(c)


# ============================ ПРОВЕРКИ И ЗАПУСК ============================

def check_db():
    """Проверяет подключение, вьюху и служебные таблицы."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT VERSION() AS v, @@hostname AS h, @@port AS p")
        r = cur.fetchone()
        log.info("база: %s (подключились к %s:%s, сервер %s, внутренний порт %s)",
                 r["v"], DB["host"], DB["port"], r["h"], r["p"])
        cur.execute(f"SELECT COUNT(*) AS n, SUM(card_no > 0) AS with_card FROM {VIEW}")
        r = cur.fetchone()
        log.info("вьюха %s: %s строк, с картой %s", VIEW, r["n"], r["with_card"] or 0)
        tables = ("turnstile_synced", "turnstile_errors") + (("turnstile_events", "turnstile_event_cursor") if EVENTS else ())
        for t in tables:
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            log.info("таблица %s: %s строк", t, cur.fetchone()["n"])


def check_controllers():
    ok = True
    for c in controllers:
        try:
            log.info("контроллер %s (%s): отвечает, карт в памяти: %s", c.sn, c.ip, c.card_count())
            if EVENTS:
                st, lst = c.status(), c.get_listener()
                drift = f"{(st['clock'] - datetime.now()).total_seconds():+.0f} сек" if st["clock"] else "не прочитать"
                log.info("контроллер %s: событий в памяти до №%s, часы %s (расхождение с сервером %s), push → %s:%s",
                         c.sn, st["last_index"], st["clock"], drift, lst[0], lst[1])
        except IOError as e:
            log.error("%s", e)
            ok = False
    return ok


def selftest(card):
    """Проверка протокола на НАСТОЯЩЕМ контроллере тестовой картой, которой нет ни у кого:
    записать → прочитать и сверить → удалить → убедиться, что удалена."""
    today = date.today()
    ok = True
    for c in controllers:
        try:
            if c.has_card(card):
                log.error("контроллер %s: карта %s уже есть — выберите номер, которого нет ни у кого", c.sn, card)
                ok = False
                continue
            c.put_card(card, today, today)
            got = c.get_card(card)
            expect = (card, today.strftime("%Y%m%d"), today.strftime("%Y%m%d"), list(c.doors))
            if got != expect:
                raise IOError(f"записали {expect}, прочитали {got}")
            c.delete_card(card)
            if c.has_card(card):
                raise IOError("карта не удалилась")
            log.info("контроллер %s: запись, чтение, сверка и удаление карты %s — ок", c.sn, card)
        except IOError as e:
            log.error("контроллер %s: %s", c.sn, e)
            ok = False
    return ok


def setup_logging():
    """Лог в консоль и, если задан [log] file, в файл с ротацией (для службы Windows)."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    if LOG.get("file"):
        os.makedirs(os.path.dirname(os.path.abspath(LOG["file"])), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(LOG["file"], maxBytes=LOG.get("max_mb", 10) * 1024 * 1024,
                                                  backupCount=LOG.get("keep", 10), encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def main():
    if hasattr(sys.stdout, "reconfigure"):             # кириллица в консоли Windows и в логе службы
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(os.path.dirname(os.path.abspath(__file__)))   # служба стартует в system32 — работаем из папки проекта
    args = [a for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()]
    load_config(args[0] if args else "config.toml")
    setup_logging()

    check_db()
    ctrl_ok = check_controllers()
    if "--check" in sys.argv:
        raise SystemExit(0 if ctrl_ok else 1)
    if "--selftest" in sys.argv:
        card = int(sys.argv[sys.argv.index("--selftest") + 1])
        raise SystemExit(0 if selftest(card) else 1)
    if DRY_RUN:
        log.warning("РЕЖИМ DRY-RUN: на контроллеры ничего не отправляется, только лог")

    # При запуске — полная сверка: догоняем всё, что изменилось, пока коннектор был выключен
    # или пока вызовы вебхука не доходили.
    force = "--force" in sys.argv
    log.info("стартовая сверка%s: %s студентов в очереди",
             " (--force: переписать все карты)" if force else "", enqueue_all(force))
    supervised(worker).start()
    if RECONCILE_AT:
        supervised(nightly_reconcile).start()
        log.info("ночная сверка: каждый день в %s", RECONCILE_AT)

    if EVENTS:
        if EVENTS["listen_port"]:
            supervised(push_listener).start()
        supervised(events_worker).start()
        log.info("проходы: включены (%s)", f"страховочная сверка раз в {EVENTS['poll_seconds']} сек"
                 if EVENTS["poll_seconds"] else "только по push, без периодической сверки")

    log.info("вебхук: http://%s:%s/sync", HTTP_BIND, HTTP_PORT)
    ThreadingHTTPServer((HTTP_BIND, HTTP_PORT), Webhook).serve_forever()


if __name__ == "__main__":
    main()
