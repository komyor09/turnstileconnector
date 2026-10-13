-- Вьюха для коннектора поверх существующей view_xobgoh_resident (её не трогаем — по ней делается выгрузка в N3000).
-- Единый источник правды: если поменяете даты или условия в view_xobgoh_resident, коннектор подхватит это сам.
CREATE OR REPLACE VIEW v_turnstile_xobgoh AS
SELECT `User ID`                     AS student_id,
       `Card NO`                     AS card_no,
       CAST(`Active Date`   AS DATE) AS valid_from,
       CAST(`Deactive Date` AS DATE) AS valid_to
FROM view_xobgoh_resident;
