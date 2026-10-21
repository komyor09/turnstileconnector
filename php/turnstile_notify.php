<?php
/**
 * Вебхук для коннектора турникетов: сообщить, что список жильцов изменился.
 *
 * Подключить один раз (require_once) и вызывать ПОСЛЕ успешного сохранения (после COMMIT):
 *
 *     turnstile_notify(array($studentId));          // изменились эти жильцы (заселили, выселили, новая карта)
 *     turnstile_notify(array(101, 102, 103));       // приказ по нескольким
 *     turnstile_notify_all();                       // список перезалит целиком (TRUNCATE + INSERT и т.п.)
 *
 * Никогда не бросает исключений и не ломает основную операцию: если коннектор недоступен,
 * пишет в error_log и возвращает false. Пропущенное коннектор догонит сам — при запуске
 * и ночной сверкой.
 *
 * Совместимо с PHP 5.3+. Использует curl, если его нет — стандартные потоки PHP.
 */

if (!defined('TURNSTILE_URL')) {
    define('TURNSTILE_URL', 'http://IP-КОМПЬЮТЕРА-КОННЕКТОРА:8090/sync');
}
if (!defined('TURNSTILE_TOKEN')) {
    define('TURNSTILE_TOKEN', 'тот-же-токен-что-в-config.toml');
}
if (!defined('TURNSTILE_TIMEOUT')) {
    define('TURNSTILE_TIMEOUT', 2);   // секунд; дольше пользователь ISU ждать не будет
}

function turnstile_notify($studentIds)
{
    $ids = array();
    foreach ((array) $studentIds as $id) {
        if (is_numeric($id) && (int) $id > 0) {
            $ids[(int) $id] = true;
        }
    }
    if (!$ids) {
        return true;
    }
    return turnstile_post(array('student_ids' => array_keys($ids)));
}

function turnstile_notify_all()
{
    return turnstile_post(array('all' => true));
}

function turnstile_post($payload)
{
    $json = json_encode($payload);
    $headers = array('Content-Type: application/json', 'Authorization: Bearer ' . TURNSTILE_TOKEN);

    if (function_exists('curl_init')) {
        $ch = curl_init(TURNSTILE_URL);
        curl_setopt_array($ch, array(
            CURLOPT_POST           => true,
            CURLOPT_POSTFIELDS     => $json,
            CURLOPT_HTTPHEADER     => $headers,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => TURNSTILE_TIMEOUT,
            CURLOPT_TIMEOUT        => TURNSTILE_TIMEOUT,
        ));
        $body = curl_exec($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $err  = curl_error($ch);
        curl_close($ch);
    } else {
        $ctx = stream_context_create(array('http' => array(
            'method'        => 'POST',
            'header'        => implode("\r\n", $headers),
            'content'       => $json,
            'timeout'       => TURNSTILE_TIMEOUT,
            'ignore_errors' => true,
        )));
        $body = @file_get_contents(TURNSTILE_URL, false, $ctx);
        $code = 0;
        if (isset($http_response_header[0]) && preg_match('#\s(\d{3})\s#', $http_response_header[0], $m)) {
            $code = (int) $m[1];
        }
        $err = $body === false ? 'нет соединения' : '';
    }

    if ($code !== 202) {
        error_log('turnstile_notify: HTTP ' . $code . ' ' . ($err ? $err : $body) . ' payload=' . $json);
        return false;
    }
    return true;
}
