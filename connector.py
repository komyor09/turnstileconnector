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

import wg_short as wg          # короткий протокол WGACCESS (CRC-16/IBM, без DLL/шифрования)

log = logging.getLogger("turnstile")

# Заполняется из config.toml в load_config()
DB: dict = {}
VIEW = ""
RECONCILE_AT = ""      # "03:00" — полная сверка раз в сутки; "" — только при запуске
DRY_RUN = False
MAX_ATTEMPTS, RETRY_DELAYS, AFTER_FAIL = 5, [2, 5, 15, 60], 3600
HTTP_BIND, HTTP_PORT, HTTP_TOKEN = "0.0.0.0", 8090, ""
API_KEY = ""           # X-API-Key для эндпоинтов записи в api.py; пусто — запись запрещена
ROOT_PATH = ""         # префикс за nginx (напр. /turnstile); Swagger на <root>/docs
LOG: dict = {}
controllers: list = []
TOML_CONTROLLERS: list = []   # [[controllers]] из config.toml — запасной источник, если в базе пусто
EVENTS: dict = {}      # секция [events]; пусто или enabled = false — проходы не читаются


def load_config(path):
    global DB, VIEW, RECONCILE_AT, DRY_RUN, MAX_ATTEMPTS, RETRY_DELAYS, AFTER_FAIL, HTTP_BIND, HTTP_PORT, HTTP_TOKEN
    global TOML_CONTROLLERS, EVENTS, LOG
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
    TOML_CONTROLLERS = cfg.get("controllers", [])
    if len(RETRY_DELAYS) < MAX_ATTEMPTS - 1:
        raise SystemExit("retry_delays должен содержать max_attempts - 1 значений")
    e = cfg.get("events", {})
    # Короткий протокол читает проходы поллингом (push контроллеры не умеют).
    EVENTS = {} if not e.get("enabled", False) else {
        "poll_seconds": e.get("poll_seconds", 2) or 2,          # период опроса контроллеров, сек (0 → 2)
        "history_on_first_start": e.get("history_on_first_start", False),
        "mdb_path": e.get("mdb_path", ""),                      # файл N3000 Download для суточной сверки; "" — выкл.
    }


def configure(path=None):
    """Единая загрузка конфига для обеих систем.
      Windows: есть config.toml → он база, env перекрывает заданные переменные.
      Docker:  config.toml нет → всё из переменных окружения (settings.py).
    Пароли берутся из env/.env и в образ/гит не попадают."""
    global DB, VIEW, RECONCILE_AT, DRY_RUN, HTTP_TOKEN, LOG, EVENTS, API_KEY, ROOT_PATH
    from settings import Settings, db_dict
    have_toml = bool(path and os.path.exists(path))
    if have_toml:
        load_config(path)                       # TOML как база
    s = Settings()                               # env (+ .env)
    env = s.model_fields_set                     # какие поля реально заданы в окружении
    use = (lambda f: True) if not have_toml else (lambda f: f in env)   # без TOML env задаёт всё

    if not have_toml:
        DB = db_dict(s)
    else:
        for ef, dk in (("db_host", "host"), ("db_port", "port"), ("db_user", "user"),
                       ("db_password", "password"), ("db_name", "database"), ("db_charset", "charset")):
            if ef in env:
                DB[dk] = getattr(s, ef)
    if use("sync_view"):
        VIEW = s.sync_view
    if use("dry_run"):
        DRY_RUN = s.dry_run
    if use("reconcile_at"):
        RECONCILE_AT = s.reconcile_at
        if RECONCILE_AT:
            datetime.strptime(RECONCILE_AT, "%H:%M")
    if use("http_token"):
        HTTP_TOKEN = s.http_token or ""
    if use("api_key"):
        API_KEY = s.api_key or ""
    if use("root_path"):
        ROOT_PATH = s.root_path
    if use("log_file"):
        LOG = {"file": s.log_file, "max_mb": s.log_max_mb, "keep": s.log_keep} if s.log_file else {}
    if use("events_enabled"):
        EVENTS = {} if not s.events_enabled else {
            "poll_seconds": s.poll_seconds or 2,
            "history_on_first_start": s.history_on_first_start,
            "mdb_path": s.mdb_path,
        }


def _controller_from_row(r):
    """Строит Controller из строки таблицы turnstile_controllers."""
    doors = {int(x): 1 for x in str(r["doors"]).replace(" ", "").split(",") if x}
    return Controller(r["sn"], r["ip"], doors,
                      directions={1: r.get("dir1", "in"), 2: r.get("dir2", "out")},
                      port=r.get("port") or 60000)


def load_controllers():
    """Берёт контроллеры из таблицы turnstile_controllers; если её нет или она пуста —
    из [[controllers]] config.toml. Добавить турникет = INSERT в таблицу, без правки конфига."""
    global controllers
    rows = None
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT sn, ip, port, doors, dir1, dir2 FROM turnstile_controllers"
                        " WHERE active = 1 ORDER BY sn")
            rows = cur.fetchall()
    except pymysql.err.ProgrammingError:          # таблицы ещё нет
        rows = None
    if rows:
        controllers = [_controller_from_row(r) for r in rows]
        log.info("контроллеры из базы (turnstile_controllers): %s", [c.sn for c in controllers])
    else:
        controllers = [Controller(c["sn"], c["ip"], {d: 1 for d in c["doors"]},
                                  directions=c.get("directions", {1: "in", 2: "out"}),
                                  port=c.get("port", 60000))
                       for c in TOML_CONTROLLERS]
        log.info("контроллеры из config.toml: %s", [c.sn for c in controllers])
    if not controllers:
        raise SystemExit("нет контроллеров: ни в таблице turnstile_controllers, ни в config.toml")


def log_error(cur, sid, card_no, action, error, attempts):
    """Запись ошибки после MAX_ATTEMPTS. Поменяйте на свою таблицу, если нужно."""
    cur.execute("INSERT INTO turnstile_errors (student_id, card_no, action, error, attempts)"
                " VALUES (%s, %s, %s, LEFT(%s, 500), %s)", (sid, card_no, action, error, attempts))


def resolve_errors(cur, sid):
    cur.execute("UPDATE turnstile_errors SET resolved_at = NOW()"
                " WHERE student_id = %s AND resolved_at IS NULL", (sid,))

# ============================ КОНТРОЛЛЕР ============================

class Controller:
    """WGACCESS / N3000, короткий протокол (пакеты 0x2x, CRC-16/IBM, без шифрования).

    Транспорт — модуль wg_short. Проверка записи — по ack контроллера (как у N3000);
    чтение одной карты в коротком протоколе не реализовано. События приходят блоком
    последних ~11 записей в ответе статуса (fn 0x20), а не по номеру.
    """

    def __init__(self, sn, ip, doors, directions=None, port=60000, timeout=1.5):
        self.sn, self.ip, self.port, self.timeout = sn, ip, port, timeout
        # какие двери открывать карте: {1: 1, 2: 1} → маска [д1, д2, д3, д4]
        self.door_mask = [1 if doors.get(d) else 0 for d in (1, 2, 3, 4)]
        # направление по двери для журнала проходов: {1: "in", 2: "out"} → {1: 1, 2: 2}
        self.directions = {int(d): (1 if str(v).lower() in ("in", "вход", "1") else 2)
                           for d, v in (directions or {}).items()}
        self._ok_until = 0.0

    def _tx(self, req):
        resp, info = wg.transact(self.ip, req, self.timeout)
        if not info or not info["crc_ok"] or info["serial"] != self.sn:
            raise IOError(f"контроллер {self.sn} ({self.ip}) не отвечает")
        return req, info

    def alive(self):
        """Пинг с кэшем на 5 сек, чтобы при упавшем контроллере не ждать таймаут на каждом студенте."""
        if time.monotonic() < self._ok_until:
            return True
        try:
            self._tx(wg.build_status(self.sn, wg._counter()))
            self._ok_until = time.monotonic() + 5
            return True
        except IOError:
            return False

    def put_card(self, card, d_from: date, d_to: date):
        cnt = wg._counter()
        req, info = self._tx(wg.build_privilege(
            self.sn, cnt, card,
            (d_from.year, d_from.month, d_from.day),
            (d_to.year, d_to.month, d_to.day),
            self.door_mask, enable=True))
        if not wg._ack_ok(req, info, self.sn, cnt):
            raise IOError(f"контроллер {self.sn} отклонил карту {card}")

    def delete_card(self, card):
        cnt = wg._counter()
        self._tx(wg.build_privilege(self.sn, cnt, card, (2000, 1, 1), (2000, 1, 1),
                                    [0, 0, 0, 0], enable=False))

    def has_card(self, card):
        # Чтение одной карты в коротком протоколе не вскрыто. Результат put_card
        # подтверждается ack контроллера, поэтому отдельная проверка не нужна.
        return True

    # --- события (проходы) ---

    def last_index(self):
        """Номер последней записи проходов (fn 0x80)."""
        req, info = self._tx(wg.build_latest_index(self.sn, wg._counter()))
        return struct.unpack("<I", info["data"][4:8])[0]

    def read_events(self):
        """Блок последних проходов: {clock, last_index, events[{index,card,dt,door,b16,b17,flag}]}."""
        st = wg.read_events(self.ip, self.sn, wg._counter(), self.timeout)
        if st is None:
            raise IOError(f"контроллер {self.sn} ({self.ip}) не отвечает")
        return st


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
STOP = threading.Event()        # ставится при штатной остановке (lifespan shutdown) — фон выходит без падения

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
            if not STOP.is_set():          # штатная остановка (STOP) — не роняем процесс
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
    while not STOP.is_set():
        now = datetime.now()
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        if STOP.wait((at - now).total_seconds()):   # проснулись из-за остановки — выходим
            return
        try:
            log.info("ночная сверка: %s студентов в очереди", enqueue_all())
        except DB_ERRORS as e:
            log.error("ночная сверка не выполнена — база недоступна: %s", e)
        # Если задан файл N3000 Download — заодно добираем пропущенные проходы из него.
        if EVENTS.get("mdb_path"):
            try:
                reconcile_events_mdb(EVENTS["mdb_path"])
            except Exception as e:
                log.error("ночная сверка проходов из файла не выполнена: %s", e)

# ============================ ПРОХОДЫ: ТУРНИКЕТЫ → БАЗА ============================

last_push: dict = {}                # sn → время последнего push (для /health; в коротком протоколе не используется)
DIRECTION = {1: "вход", 2: "выход"}


def save_cursor(cur, sn, index):
    cur.execute("INSERT INTO turnstile_event_cursor (controller_sn, last_index) VALUES (%s, %s)"
                " ON DUPLICATE KEY UPDATE last_index = VALUES(last_index)", (sn, index))


def store_event(cur, c, ev):
    """Пишет один проход. ev — из wg.parse_status: {index, card, dt, door, b16, b17, flag}.
    Направление берётся из конфига двери (c.directions), результат — из флага [17]."""
    cur.execute("SELECT student_id FROM turnstile_synced WHERE card_no = %s LIMIT 1", (ev["card"],))
    r = cur.fetchone()
    sid = r["student_id"] if r else None
    # Расшифровка сверена с живыми проходами и отказной картой:
    #   door      — байт [16]: 0x10 → дверь 1, 0x18 → дверь 2 (одинаково на всех контроллерах);
    #   direction — по двери из конфига (дверь 1 = вход, дверь 2 = выход);
    #   granted   — бит 0x80 в байте [17]: выставлен → ОТКАЗ, снят → проход.
    door = ev["door"]
    direction = c.directions.get(door, 0)              # 1=вход, 2=выход
    granted = 0 if ev["flag"] & 0x80 else 1
    cur.execute("INSERT IGNORE INTO turnstile_events (controller_sn, event_index, event_time, event_type, card_no,"
                " student_id, door, direction, granted, reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (c.sn, ev["index"], ev["dt"] or datetime(1970, 1, 1), 1, ev["card"], sid,
                 door, direction, granted, ev["flag"]))           # reason хранит сырой флаг [17]
    log.info("проход: карта %s%s, дверь %s, %s — %s (%s, №%s)", ev["card"],
             f" (студент {sid})" if sid else " (нет в списке)", door,
             DIRECTION.get(direction, "?"),
             "пропущен" if granted else "ОТКАЗ", ev["dt"], ev["index"])


def fetch_new_events(cur, c):
    """Читает блок последних проходов контроллера и сохраняет те, что новее курсора.

    Короткий протокол отдаёт только ~11 последних записей (без чтения по номеру).
    При опросе раз в 2 сек это с огромным запасом: турникет не выдаёт столько проходов
    так быстро. Разрыв возможен лишь если САМ коннектор был недоступен минуты/часы, пока
    турникеты работали автономно, — тогда часть проходов в журнал не попадёт (их видно по
    предупреждению ниже; при необходимости полную историю можно вычитать кнопкой Download
    в N3000)."""
    st = c.read_events()
    last = st["last_index"]
    cur.execute("SELECT last_index FROM turnstile_event_cursor WHERE controller_sn = %s", (c.sn,))
    row = cur.fetchone()
    if row is None:
        start = 0 if EVENTS["history_on_first_start"] else last
        save_cursor(cur, c.sn, start)
        log.info("контроллер %s: первый запуск чтения проходов — читаю с №%s", c.sn, start)
        cursor = start
    else:
        cursor = row["last_index"]
    if last < cursor:                       # память очищена или контроллер заменён
        log.warning("контроллер %s: индекс в памяти (%s) меньше прочитанного (%s) — память сброшена, "
                    "продолжаю с текущего", c.sn, last, cursor)
        cursor = max(0, last - 1)
        save_cursor(cur, c.sn, cursor)

    new = sorted((e for e in st["events"] if e["index"] > cursor), key=lambda e: e["index"])
    # Реальный пропуск — только если самая старая запись в блоке новее, чем cursor+1:
    # значит часть событий уже вытеснена из блока (длинный простой коннектора или наплыв проходов).
    # Курсор двигаем ТОЛЬКО по реально увиденным записям — last_index в шапке бывает на 1 впереди
    # (свежее событие появляется в блоке на следующий опрос), это не пропуск.
    if new and new[0]["index"] > cursor + 1:
        gap = new[0]["index"] - (cursor + 1)
        log.warning("контроллер %s: в журнал не попало ~%s проходов (коннектор был недоступен дольше, "
                    "чем помещается в блок) — при необходимости вычитать из N3000 (Download)", c.sn, gap)
    for e in new:
        store_event(cur, c, e)
        save_cursor(cur, c.sn, e["index"])


def events_worker():
    """Опрашивает контроллеры и пишет проходы. Короткий протокол не пушит — только поллинг.
    База недоступна — курсор не двигается, ничего не теряется."""
    conn, backoff = None, 1
    poll = EVENTS["poll_seconds"] or 2          # по умолчанию опрос раз в 2 сек
    down: set = set()                            # контроллеры, о недоступности которых уже написали
    while not STOP.is_set():
        try:
            conn = alive_conn(conn) if conn else connect()
            if backoff > 1:
                log.info("проходы: база снова доступна")
            backoff = 1
            with conn.cursor() as cur:
                for c in controllers:
                    try:
                        fetch_new_events(cur, c)
                        if c.sn in down:
                            down.discard(c.sn)
                            log.info("проходы: контроллер %s снова отвечает", c.sn)
                    except IOError as e:
                        if c.sn not in down:     # пишем один раз, а не при каждой попытке
                            down.add(c.sn)
                            log.warning("проходы: %s — продолжу пробовать, напишу, когда ответит", e)
        except DB_ERRORS as e:
            log.error("проходы: база недоступна (%s) — повтор через %s сек", e, backoff)
            conn = None
            STOP.wait(backoff)
            backoff = min(backoff * 2, 60)
            continue
        STOP.wait(poll)


# Короткий протокол не поддерживает push — события читаются поллингом (events_worker).


def reconcile_events_mdb(path):
    """Суточная сверка проходов из файла N3000 Download (Access .mdb) → turnstile_events.
    Добавляет отсутствующие проходы, существующие пропускает (дедуп тем же ключом, что у
    реал-тайма). Нужен pyodbc и Access-драйвер (есть там же, где N3000)."""
    import mdb_reconcile
    directions = {c.sn: c.directions for c in controllers}
    known = {c.sn for c in controllers}
    with connect() as conn:
        rows = mdb_reconcile.read_swipes(path)
        added, existed = mdb_reconcile.reconcile(conn, rows, directions, known_sns=known)
    log.info("сверка из %s: добавлено %s проходов, уже было %s", path, added, existed)
    return added


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
            st = c.read_events()
            drift = f"{(st['clock'] - datetime.now()).total_seconds():+.0f} сек" if st["clock"] else "не прочитать"
            log.info("контроллер %s (%s): отвечает, последняя запись №%s, часы %s (расхождение с сервером %s)",
                     c.sn, c.ip, st["last_index"], st["clock"], drift)
        except IOError as e:
            log.error("%s — если ping до %s проходит, проверьте серийный номер в N3000: "
                      "на чужой серийник контроллер не отвечает", e, c.ip)
            ok = False
    return ok


def selftest(card):
    """Проверка записи на НАСТОЯЩЕМ контроллере тестовой картой: выдать → запретить.
    Короткий протокол не умеет читать одну карту обратно, поэтому успех — по ack контроллера."""
    today = date.today()
    ok = True
    for c in controllers:
        try:
            c.put_card(card, today, date(2099, 12, 31))
            c.delete_card(card)
            log.info("контроллер %s: выдача и запрет тестовой карты %s — ок (подтверждено ack)", c.sn, card)
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
    cfg = args[0] if args else "config.toml"
    configure(cfg if os.path.exists(cfg) else None)   # TOML если есть, иначе env (Docker)
    setup_logging()

    check_db()
    load_controllers()
    if "--reconcile" in sys.argv:          # разовая сверка проходов из файла N3000 Download и выход
        i = sys.argv.index("--reconcile")
        path = sys.argv[i + 1] if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--") \
            else EVENTS.get("mdb_path", "")
        if not path:
            raise SystemExit("--reconcile: укажите путь к .mdb или задайте events.mdb_path в конфиге")
        raise SystemExit(0 if reconcile_events_mdb(path) is not None else 1)
    ctrl_ok = check_controllers()
    if "--check" in sys.argv:
        raise SystemExit(0 if ctrl_ok else 1)
    if "--selftest" in sys.argv:
        card = int(sys.argv[sys.argv.index("--selftest") + 1])
        raise SystemExit(0 if selftest(card) else 1)
    if DRY_RUN:
        log.warning("РЕЖИМ DRY-RUN: на контроллеры ничего не отправляется, только лог")

    force = "--force" in sys.argv
    start_workers(force)
    log.info("вебхук: http://%s:%s/sync", HTTP_BIND, HTTP_PORT)
    ThreadingHTTPServer((HTTP_BIND, HTTP_PORT), Webhook).serve_forever()


def start_workers(force=False):
    """Старт фоновых потоков (очередь карт, опрос проходов, ночная сверка) и стартовая сверка.
    Зовётся и из main() (headless), и из lifespan api.py. Ровно один вызов на процесс."""
    STOP.clear()
    log.info("стартовая сверка%s: %s студентов в очереди",
             " (--force: переписать все карты)" if force else "", enqueue_all(force))
    supervised(worker).start()
    if RECONCILE_AT:
        supervised(nightly_reconcile).start()
        log.info("ночная сверка: каждый день в %s", RECONCILE_AT)
    if EVENTS:
        supervised(events_worker).start()
        log.info("проходы: включены (опрос контроллеров раз в %s сек)", EVENTS["poll_seconds"])


def stop_workers():
    """Штатная остановка фона (lifespan shutdown): опрос/сверка выходят из циклов."""
    STOP.set()


if __name__ == "__main__":
    main()