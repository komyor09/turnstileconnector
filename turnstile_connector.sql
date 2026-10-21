-- Можно выполнять повторно: существующие таблицы не трогаются, недостающие создаются.
-- Совместимо с MariaDB 5.5 (DATETIME DEFAULT CURRENT_TIMESTAMP там нет — поэтому TIMESTAMP).

-- Что уже стоит на турникетах (коннектор сравнивает с этим вьюху).
CREATE TABLE IF NOT EXISTS turnstile_synced (
    student_id  INT          PRIMARY KEY,
    card_no     INT UNSIGNED NOT NULL,
    valid_from  DATE         NOT NULL,
    valid_to    DATE         NOT NULL,
    synced_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

-- Ошибки после 5 неудачных попыток. Можно заменить на свою таблицу (функция log_error в connector.py).
CREATE TABLE IF NOT EXISTS turnstile_errors (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    student_id  INT          NOT NULL,
    card_no     INT UNSIGNED NULL,
    action      VARCHAR(10)  NOT NULL,      -- put / delete
    error       VARCHAR(500) NOT NULL,
    attempts    INT          NOT NULL,
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at DATETIME     NULL,          -- коннектор ставит сам, когда позже получилось
    KEY idx_open (student_id, resolved_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

-- Проходы через турникеты. Одна строка = одно событие контроллера (проход, отказ, кнопка, перезагрузка).
CREATE TABLE IF NOT EXISTS turnstile_events (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    controller_sn INT UNSIGNED     NOT NULL,
    event_index   INT UNSIGNED     NOT NULL,   -- номер события в памяти контроллера
    event_time    DATETIME         NOT NULL,   -- по часам контроллера
    event_type    TINYINT UNSIGNED NOT NULL,   -- 1 проход по карте, 2 дверь/кнопка/перезагрузка, 3 тревога
    card_no       INT UNSIGNED     NOT NULL,
    student_id    INT              NULL,       -- по turnstile_synced на момент события; NULL — карты нет в списке
    door          TINYINT UNSIGNED NOT NULL,
    direction     TINYINT UNSIGNED NOT NULL,   -- 1 вход, 2 выход
    granted       TINYINT(1)       NOT NULL,   -- 1 пропущен, 0 отказ
    reason        TINYINT UNSIGNED NOT NULL,   -- код причины контроллера, как есть
    received_at   TIMESTAMP        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_event (controller_sn, event_index, event_time),   -- повтор того же события не запишется
    KEY idx_student_time (student_id, event_time),
    KEY idx_card_time (card_no, event_time),
    KEY idx_time (event_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

-- До какого номера события уже прочитан каждый контроллер.
CREATE TABLE IF NOT EXISTS turnstile_event_cursor (
    controller_sn INT UNSIGNED PRIMARY KEY,
    last_index    INT UNSIGNED NOT NULL,
    updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

-- Пользователь для коннектора: читать базу, писать свои таблицы.
-- CREATE USER 'turnstile'@'<ip коннектора>' IDENTIFIED BY '...';
-- GRANT SELECT ON isu.* TO 'turnstile'@'<ip>';
-- GRANT INSERT, UPDATE, DELETE ON isu.turnstile_synced TO 'turnstile'@'<ip>';
-- GRANT INSERT, UPDATE ON isu.turnstile_errors TO 'turnstile'@'<ip>';
-- GRANT INSERT ON isu.turnstile_events TO 'turnstile'@'<ip>';
-- GRANT INSERT, UPDATE ON isu.turnstile_event_cursor TO 'turnstile'@'<ip>';
