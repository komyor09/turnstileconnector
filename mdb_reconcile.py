"""
Суточная сверка проходов из файла N3000 Download (iCCard3000.mdb) → turnstile_events.

N3000 по кнопке Download выгружает весь журнал в Access-файл (таблица t_d_SwipeRecord).
Этот модуль читает его и добавляет в базу проходы, которых там ещё нет; существующие
пропускает. Так закрывается «длинный простой» коннектора без разбора сырой памяти.

Соответствие полей (сверено с реал-тайм разбором и отказной картой):
    f_ControllerSN     → controller_sn
    f_RecordFlashLoc    → event_index   (совпадает с номером из реал-тайма — дедуп по ключу)
    f_ReadDate          → event_time
    f_CardNO            → card_no
    f_RecOption (байт)  → дверь:   0x10 → 1, 0x18 → 2
    f_Status   (байт)   → результат: бит 0x80 выставлен → ОТКАЗ, иначе проход
    f_Character         → тип: 1 проход по карте, иначе служебное событие
Направление берётся по двери из конфига контроллера (как в реал-тайме).

Чтение Access-файла — через ODBC (драйвер «Microsoft Access Driver (*.mdb, *.accdb)»,
он стоит там же, где N3000). pyodbc ставится из requirements. Соединение с MySQL
(pymysql) передаётся снаружи — модуль от него не зависит напрямую.
"""

DOOR_BY_BYTE = {0x10: 1, 0x18: 2}
SWIPE_SQL = ("SELECT f_ControllerSN, f_RecordFlashLoc, f_ReadDate, f_CardNO,"
             " f_RecOption, f_Status, f_Character FROM t_d_SwipeRecord")


def read_swipes_odbc(mdb_path):
    """Читает t_d_SwipeRecord из Access-файла через ODBC. Отдаёт словари по строке."""
    import pyodbc
    cs = r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=%s;" % mdb_path
    conn = pyodbc.connect(cs, readonly=True)
    try:
        cur = conn.cursor()
        cur.execute(SWIPE_SQL)
        for sn, floc, read_date, card, recopt, status, char in cur.fetchall():
            yield {"sn": int(sn), "index": int(floc), "time": read_date,
                   "card": int(card), "recopt": int(recopt) & 0xFF,
                   "status": int(status) & 0xFF, "char": int(char)}
    finally:
        conn.close()


def reconcile(db, rows, directions_by_sn, batch=500, known_sns=None):
    """
    Пишет недостающие проходы в turnstile_events.
      db                — открытое соединение pymysql (autocommit)
      rows              — итератор строк из read_swipes_odbc()
      directions_by_sn  — {sn: {1: 1|2, 2: 1|2}} — направление по двери (из контроллеров)
      known_sns         — если задано, строки по другим контроллерам пропускаются
    Существующие строки отсекаются уникальным ключом (controller_sn, event_index, event_time) —
    тем же, что у реал-тайма, поэтому дубликатов между сверкой и поллингом нет.
    Возвращает (добавлено, уже_было).
    """
    # карта «номер карты → студент» на момент сверки — один запрос вместо тысяч
    with db.cursor() as cur:
        cur.execute("SELECT card_no, student_id FROM turnstile_synced")
        card2student = {r["card_no"]: r["student_id"] for r in cur.fetchall()}

    added = processed = 0
    buf = []
    sql = ("INSERT IGNORE INTO turnstile_events (controller_sn, event_index, event_time,"
           " event_type, card_no, student_id, door, direction, granted, reason)"
           " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")

    def flush():
        nonlocal added
        if buf:
            with db.cursor() as cur:
                added += cur.executemany(sql, buf)   # число реально вставленных (новых)
            buf.clear()

    for r in rows:
        if known_sns is not None and r["sn"] not in known_sns:
            continue
        door = DOOR_BY_BYTE.get(r["recopt"], 0)
        direction = directions_by_sn.get(r["sn"], {}).get(door, 0)
        granted = 0 if r["status"] & 0x80 else 1
        etype = 1 if r["char"] == 1 else 2
        sid = card2student.get(r["card"])
        buf.append((r["sn"], r["index"], r["time"], etype, r["card"], sid,
                    door, direction, granted, r["status"]))
        processed += 1
        if len(buf) >= batch:
            flush()
    flush()
    return added, processed - added           # (добавлено, уже было)
