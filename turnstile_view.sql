-- Вьюхи коннектора живут в turnstile_db, а источник берут из базы ISU (kbtut_project) кросс-базово.
-- Если база ISU называется иначе — замените kbtut_project на её имя в двух местах ниже.
-- view_xobgoh_resident и _____list_xobgoh остаются в базе ISU — их не трогаем (по ним же идёт выгрузка в N3000).
USE turnstile_db;

-- Источник правды по доступу: кто, какая карта, с какой по какую дату.
-- Поменяете даты/условия в kbtut_project.view_xobgoh_resident — коннектор подхватит сам.
CREATE OR REPLACE VIEW v_turnstile_xobgoh AS
SELECT `User ID`                     AS student_id,
       `Card NO`                     AS card_no,
       CAST(`Active Date`   AS DATE) AS valid_from,
       CAST(`Deactive Date` AS DATE) AS valid_to
FROM kbtut_project.view_xobgoh_resident;

-- Проходы жителей с именем и группой — для отчётов. Только события «проход по карте» (тип 1).
CREATE OR REPLACE VIEW v_turnstile_passes AS
SELECT e.event_time,
       e.student_id,
       l.fio,
       l.gr_name,
       e.card_no,
       CASE e.direction WHEN 1 THEN 'вход' WHEN 2 THEN 'выход' ELSE e.direction END AS direction,
       e.granted,
       e.reason,
       e.door,
       e.controller_sn
FROM turnstile_events e
LEFT JOIN kbtut_project.`_____list_xobgoh` l ON l.student_id = e.student_id
WHERE e.event_type = 1;
