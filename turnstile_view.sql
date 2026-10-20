-- Вьюха для коннектора поверх существующей view_xobgoh_resident (её не трогаем — по ней делается выгрузка в N3000).
-- Единый источник правды: если поменяете даты или условия в view_xobgoh_resident, коннектор подхватит это сам.
CREATE OR REPLACE VIEW v_turnstile_xobgoh AS
SELECT `User ID`                     AS student_id,
       `Card NO`                     AS card_no,
       CAST(`Active Date`   AS DATE) AS valid_from,
       CAST(`Deactive Date` AS DATE) AS valid_to
FROM view_xobgoh_resident;

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
LEFT JOIN _____list_xobgoh l ON l.student_id = e.student_id
WHERE e.event_type = 1;
