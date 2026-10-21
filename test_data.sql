-- Тестовая база для своего ПК. Повторяет структуру «таблица студентов + вьюха».
-- Когда будете тестировать на настоящей структуре — замените students/вьюху дампом из ISU.

CREATE DATABASE IF NOT EXISTS isu_test DEFAULT CHARACTER SET utf8;
USE isu_test;

CREATE TABLE students (
    id               INT PRIMARY KEY,
    full_name        VARCHAR(200) NOT NULL,
    card_no          INT UNSIGNED NULL,
    status           VARCHAR(20)  NOT NULL,     -- active / expelled / academic_leave
    enrollment_date  DATE NULL,
    graduation_date  DATE NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

INSERT INTO students VALUES
 (1, 'Студент Один',  13158327, 'active',   '2026-09-01', '2030-06-01'),
 (2, 'Студент Два',   20030837, 'active',   '2025-09-01', '2029-06-01'),
 (3, 'Студент Три',   NULL,     'active',   '2026-09-01', '2030-06-01'),   -- карты ещё нет
 (4, 'Студент Четыре',19341237, 'expelled', '2024-09-01', '2028-06-01');   -- отчислен

CREATE VIEW v_campus_students AS
SELECT id AS student_id, card_no, enrollment_date AS valid_from, graduation_date AS valid_to
FROM students
WHERE status = 'active';

-- Пользователь коннектора. В тесте — права на всю тестовую базу,
-- на бою права уже по таблицам (см. turnstile_connector.sql).
GRANT SELECT, INSERT, UPDATE, DELETE ON isu_test.* TO 'turnstile'@'%' IDENTIFIED BY 'turnstile';
FLUSH PRIVILEGES;
