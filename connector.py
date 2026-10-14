"""
Коннектор «база → турникеты». Отдельный сервис, код проекта не трогает.

Входы:  binlog MariaDB/MySQL (изменения строк приходят сразу) + HTTP-вебхук POST /sync
Выход:  контроллеры WG3000 / ACB по UDP 60000
Ошибки: после max_attempts неудач — строка в turnstile_errors, дальше повтор раз в after_fail сек.

Запуск:
    uv run connector.py config.test.toml            # работать
    uv run connector.py config.test.toml --check    # только проверить базу и контроллеры
    uv run connector.py config.toml --force         # при старте переписать все карты на контроллерах заново
    uv run connector.py config.toml --selftest 99999999   # проверить протокол на настоящем контроллере тестовой картой
"""
import heapq, importlib.metadata, itertools, json, logging, os, socket, struct, sys, threading, time, tomllib
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymysql
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.event import QueryEvent
from pymysqlreplication.row_event import DeleteRowsEvent, UpdateRowsEvent, WriteRowsEvent

log = logging.getLogger("turnstile")

# Заполняется из config.toml в load_config()
DB: dict = {}
VIEW = ""
WATCH: dict = {}
RESYNC_ON: list = []   # имена таблиц/вьюх: TRUNCATE/ALTER/CREATE по ним → полная сверка
DRY_RUN = False
MAX_ATTEMPTS, RETRY_DELAYS, AFTER_FAIL = 5, [2, 5, 15, 60], 3600
HTTP_PORT, HTTP_TOKEN = 8090, ""
controllers: list = []


def load_config(path):
    global DB, VIEW, WATCH, RESYNC_ON, DRY_RUN, MAX_ATTEMPTS, RETRY_DELAYS, AFTER_FAIL, HTTP_PORT, HTTP_TOKEN, controllers
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    DB = cfg["db"]
    s = cfg["sync"]
    VIEW, DRY_RUN = s["view"], s.get("dry_run", False)
    MAX_ATTEMPTS = s.get("max_attempts", 5)
    RETRY_DELAYS = s.get("retry_delays", [2, 5, 15, 60])
    AFTER_FAIL = s.get("after_fail", 3600)
    WATCH = cfg["watch"]
    RESYNC_ON = [n.lower() for n in list(WATCH) + s.get("resync_on", [])]
    HTTP_PORT, HTTP_TOKEN = cfg["http"]["port"], cfg["http"]["token"]
    controllers = [Controller(c["sn"], c["ip"], {d: 1 for d in c["doors"]}, c.get("port", 60000))
                   for c in cfg["controllers"]]
    if len(RETRY_DELAYS) < MAX_ATTEMPTS - 1:
        raise SystemExit("retry_delays должен содержать max_attempts - 1 значений")


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
    return f"delete {have[0]}"


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
    а не оставили «живой» сервис без worker или binlog."""
    def run():
        try:
            target(*args)
        except BaseException:
            log.exception("поток %s упал — перезапуск процесса", target.__name__)
        finally:
            os._exit(1)
    return threading.Thread(target=run, name=target.__name__, daemon=True)


# ============================ ВХОД 1: BINLOG ============================

def binlog_listener(log_file, log_pos):
    """Слушает binlog как реплика. Падает — переподключается с того же места."""
    lookup = None
    while True:
        stream = None
        try:
            stream = BinLogStreamReader(
                connection_settings={k: DB[k] for k in ("host", "port", "user", "password")},
                server_id=4242,                                  # любой номер, не равный server-id базы
                only_schemas=[DB["database"]], only_tables=list(WATCH),
                only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent, QueryEvent],
                log_file=log_file, log_pos=log_pos, resume_stream=True, blocking=True)
            log.info("binlog: слушаю с %s:%s", log_file, log_pos)
            for ev in stream:
                if isinstance(ev, QueryEvent):
                    # В режиме ROW сюда попадают только DDL и TRUNCATE (BEGIN/COMMIT пропускаем).
                    # TRUNCATE не даёт построчных событий, поэтому по нему — полная сверка.
                    q = ev.query.strip().lower()
                    if q not in ("begin", "commit") and any(n in q for n in RESYNC_ON):
                        log.info("binlog: «%s» — полная сверка через 5 сек", ev.query.strip()[:120])
                        enqueue_all(delay=5)
                    log_file, log_pos = stream.log_file, stream.log_pos
                    continue
                if ev.table not in WATCH:
                    continue
                rule = WATCH[ev.table]
                for row in ev.rows:
                    versions = [row["before_values"], row["after_values"]] if "after_values" in row else [row["values"]]
                    for values in versions:
                        if " " not in rule:
                            sids = [values.get(rule)]
                        else:
                            lookup = alive_conn(lookup) if lookup else connect()
                            with lookup.cursor() as cur:
                                cur.execute(rule, values)
                                sids = [next(iter(r.values())) for r in cur.fetchall()]
                        for sid in filter(None, sids):
                            log.debug("binlog: %s.%s → студент %s", ev.schema, ev.table, sid)
                            queue.add(int(sid))
                log_file, log_pos = stream.log_file, stream.log_pos
        except Exception as e:
            log.error("binlog: %s — переподключение через 5 сек", e)
            time.sleep(5)
        finally:
            if stream:
                stream.close()

# ============================ ВХОД 2: ВЕБХУК ============================

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
                                    "controllers": {c.sn: c.alive() for c in controllers}})
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
        if body.get("all"):
            n = enqueue_all(force=bool(body.get("force")))
        else:
            ids = [int(x) for x in body.get("student_ids", [])]
            for sid in ids:
                queue.add(sid, force=bool(body.get("force")))
            n = len(ids)
        self._send(202, {"queued": n})

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

# ============================ ПРОВЕРКИ И ЗАПУСК ============================

def check_db():
    """Проверяет binlog, права, вьюху и таблицы. Возвращает позицию binlog."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT VERSION() AS v, @@hostname AS h, @@port AS p")
        r = cur.fetchone()
        log.info("база: %s (подключились к %s:%s, сервер %s, внутренний порт %s)",
                 r["v"], DB["host"], DB["port"], r["h"], r["p"])
        cur.execute("SHOW VARIABLES LIKE 'log_bin'")
        if cur.fetchone()["Value"] != "ON":
            raise SystemExit("binlog выключен на этом сервере: нужен log_bin (my.ini или --log-bin)")
        cur.execute("SHOW MASTER STATUS")
        pos = cur.fetchone()
        if not pos:
            raise SystemExit(f"binlog включён, но у пользователя {DB['user']} нет права REPLICATION CLIENT")
        cur.execute("SHOW VARIABLES LIKE 'binlog_format'")
        if cur.fetchone()["Value"] != "ROW":
            raise SystemExit("нужен binlog_format=ROW в my.ini")
        cur.execute("SHOW VARIABLES LIKE 'binlog_row_metadata'")
        meta = cur.fetchone()
        cur.execute(f"SELECT COUNT(*) AS n, SUM(card_no > 0) AS with_card FROM {VIEW}")
        r = cur.fetchone()
        log.info("вьюха %s: %s строк, с картой %s", VIEW, r["n"], r["with_card"] or 0)
        for t in ("turnstile_synced", "turnstile_errors"):
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            log.info("таблица %s: %s строк", t, cur.fetchone()["n"])
    lib = int(importlib.metadata.version("mysql-replication").split(".")[0])
    if lib >= 1 and (not meta or meta["Value"] != "FULL"):
        raise SystemExit("mysql-replication 1.x без binlog_row_metadata=FULL не видит колонки. "
                         "Для MariaDB < 10.5: uv add mysql-replication==0.45.1 setuptools")
    log.info("binlog: %s:%s, формат ROW — ок", pos["File"], pos["Position"])
    return pos


def check_controllers():
    ok = True
    for c in controllers:
        try:
            log.info("контроллер %s (%s): отвечает, карт в памяти: %s", c.sn, c.ip, c.card_count())
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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    args = [a for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()]
    load_config(args[0] if args else "config.toml")

    pos = check_db()
    ctrl_ok = check_controllers()
    if "--check" in sys.argv:
        raise SystemExit(0 if ctrl_ok else 1)
    if "--selftest" in sys.argv:
        card = int(sys.argv[sys.argv.index("--selftest") + 1])
        raise SystemExit(0 if selftest(card) else 1)
    if DRY_RUN:
        log.warning("РЕЖИМ DRY-RUN: на контроллеры ничего не отправляется, только лог")

    # Сначала запоминаем позицию binlog, потом сверяем всё — так ничего не теряется между ними,
    # даже если коннектор был выключен (при старте он догоняет всё сам).
    force = "--force" in sys.argv
    log.info("стартовая сверка%s: %s студентов в очереди",
             " (--force: переписать все карты)" if force else "", enqueue_all(force))
    supervised(worker).start()
    supervised(binlog_listener, pos["File"], pos["Position"]).start()

    log.info("вебхук: http://0.0.0.0:%s/sync", HTTP_PORT)
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Webhook).serve_forever()


if __name__ == "__main__":
    main()
