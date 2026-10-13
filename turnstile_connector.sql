-- Совместимо с MariaDB 5.5 (DATETIME DEFAULT CURRENT_TIMESTAMP там нет — поэтому TIMESTAMP).

-- Что уже стоит на турникетах (коннектор сравнивает с этим вьюху).
CREATE TABLE turnstile_synced (
    student_id  INT          PRIMARY KEY,
    card_no     INT UNSIGNED NOT NULL,
    valid_from  DATE         NOT NULL,
    valid_to    DATE         NOT NULL,
    synced_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

-- Ошибки после 5 неудачных попыток. Можно заменить на свою таблицу (функция log_error в connector.py).
CREATE TABLE turnstile_errors (
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

-- Пользователь для коннектора: читать базу, писать свои две таблицы, слушать binlog.
-- CREATE USER 'turnstile'@'<ip коннектора>' IDENTIFIED BY '...';
-- GRANT SELECT ON isu.* TO 'turnstile'@'<ip>';
-- GRANT INSERT, UPDATE, DELETE ON isu.turnstile_synced TO 'turnstile'@'<ip>';
-- GRANT INSERT, UPDATE ON isu.turnstile_errors TO 'turnstile'@'<ip>';
-- GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'turnstile'@'<ip>';
