<?php
const PP_ROOT = '/root/parspack-backup';

function pp_h($value)
{
    return htmlspecialchars((string)$value, ENT_QUOTES, 'UTF-8');
}

function pp_python()
{
    foreach (array('/usr/bin/python3', '/usr/local/bin/python3') as $bin) {
        if (is_executable($bin)) {
            return $bin;
        }
    }
    return '';
}

function pp_exec($argv, $stdin, $timeout)
{
    if (!function_exists('proc_open')) {
        return array(127, '', 'proc_open در PHP سی‌پنل بسته است');
    }
    foreach ($argv as $arg) {
        if (!is_string($arg) || strpos($arg, "\0") !== false) {
            return array(2, '', 'آرگومان نامعتبر');
        }
    }
    $proc = proc_open($argv, array(
        0 => array('pipe', 'r'),
        1 => array('pipe', 'w'),
        2 => array('pipe', 'w'),
    ), $pipes, PP_ROOT);
    if (!is_resource($proc)) {
        return array(127, '', 'اجرای برنامه ناموفق بود');
    }
    fwrite($pipes[0], $stdin);
    fclose($pipes[0]);
    stream_set_blocking($pipes[1], false);
    stream_set_blocking($pipes[2], false);
    $stdout = '';
    $stderr = '';
    $started = time();
    $code = 1;
    while (true) {
        $stdout .= (string)stream_get_contents($pipes[1]);
        $stderr .= (string)stream_get_contents($pipes[2]);
        $status = proc_get_status($proc);
        if (!$status['running']) {
            $code = (int)$status['exitcode'];
            break;
        }
        if ((time() - $started) > $timeout) {
            proc_terminate($proc);
            $stdout .= (string)stream_get_contents($pipes[1]);
            $stderr .= (string)stream_get_contents($pipes[2]);
            fclose($pipes[1]);
            fclose($pipes[2]);
            proc_close($proc);
            return array(124, $stdout, 'زمان اجرا تمام شد');
        }
        usleep(100000);
    }
    $stdout .= (string)stream_get_contents($pipes[1]);
    $stderr .= (string)stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    proc_close($proc);
    return array($code, $stdout, $stderr);
}

function pp_run($script, $args, $stdin, $timeout)
{
    $python = pp_python();
    if ($python === '') {
        return array(127, '', 'python3 پیدا نشد');
    }
    $argv = array($python, PP_ROOT . '/' . $script);
    foreach ($args as $arg) {
        $argv[] = $arg;
    }
    return pp_exec($argv, $stdin, $timeout);
}

function pp_state($name)
{
    return PP_ROOT . '/state/' . $name;
}

function pp_token()
{
    $path = pp_state('whm-csrf');
    if (is_file($path)) {
        $token = trim((string)file_get_contents($path));
        if (preg_match('/\A[a-f0-9]{64}\z/', $token)) {
            return $token;
        }
    }
    if (!is_dir(PP_ROOT . '/state')) {
        return '';
    }
    $token = bin2hex(random_bytes(32));
    file_put_contents($path, $token, LOCK_EX);
    chmod($path, 0600);
    return $token;
}

function pp_flash_set($kind, $text)
{
    $path = pp_state('whm-flash.json');
    $json = json_encode(
        array('kind' => $kind, 'text' => substr((string)$text, 0, 2000)),
        JSON_UNESCAPED_UNICODE
    );
    if ($json === false || !is_dir(PP_ROOT . '/state')) {
        return;
    }
    file_put_contents($path, $json, LOCK_EX);
    chmod($path, 0600);
}

function pp_flash_take()
{
    $path = pp_state('whm-flash.json');
    if (!is_file($path)) {
        return null;
    }
    $data = json_decode((string)file_get_contents($path), true);
    unlink($path);
    if (!is_array($data) || !isset($data['kind'], $data['text'])) {
        return null;
    }
    if ($data['kind'] !== 'ok' && $data['kind'] !== 'bad') {
        return null;
    }
    return $data;
}

function pp_headers()
{
    if (headers_sent()) {
        return;
    }
    header('X-Content-Type-Options: nosniff');
    header('X-Frame-Options: SAMEORIGIN');
    header('Referrer-Policy: same-origin');
    // WHM::header draws the left navigation from same-origin CSS and scripts.
    // default-src 'none' blocks those files, so the plugin fills the window
    // and the sidebar does not appear.
    header("Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; form-action 'self'; base-uri 'self'; frame-ancestors 'self'");
}

function pp_self_path()
{
    $uri = isset($_SERVER['REQUEST_URI']) ? (string)$_SERVER['REQUEST_URI'] : '';
    $path = strtok($uri, '?');
    if (is_string($path) && preg_match('#\A(/cpsess[0-9]{5,32})?/cgi/parspack_backup/index\.php\z#', $path)) {
        return $path;
    }
    return '/cgi/parspack_backup/index.php';
}

function pp_allowed()
{
    $user = isset($_SERVER['REMOTE_USER']) ? (string)$_SERVER['REMOTE_USER'] : '';
    if ($user === 'root') {
        return true;
    }
    if (!preg_match('/\A[A-Za-z0-9._-]{1,32}\z/', $user) || !is_file('/var/cpanel/resellers')) {
        return false;
    }
    $lines = file('/var/cpanel/resellers', FILE_IGNORE_NEW_LINES);
    if (!is_array($lines)) {
        return false;
    }
    foreach ($lines as $line) {
        $parts = explode(':', $line, 2);
        if (count($parts) === 2 && $parts[0] === $user) {
            $acls = preg_split('/\s*,\s*/', $parts[1]);
            return is_array($acls) && in_array('all', $acls, true);
        }
    }
    return false;
}

function pp_audit_user()
{
    $user = isset($_SERVER['REMOTE_USER']) ? (string)$_SERVER['REMOTE_USER'] : '';
    if (preg_match('/\A[A-Za-z0-9._-]{1,32}\z/', $user)) {
        return $user;
    }
    return 'unknown';
}

function pp_redirect($tab, $date)
{
    if (!in_array($tab, array('dashboard', 'reports', 'settings'), true)) {
        $tab = 'dashboard';
    }
    pp_headers();
    $location = pp_self_path() . '?tab=' . rawurlencode($tab);
    if ($date !== '' && preg_match('/\A\d{4}-\d{2}-\d{2}\z/', $date)) {
        $location .= '&date=' . rawurlencode($date);
    }
    header('Location: ' . $location);
    exit;
}

function pp_size($bytes)
{
    $bytes = (int)$bytes;
    if ($bytes <= 0) {
        return '-';
    }
    $value = (float)$bytes;
    foreach (array('B', 'KiB', 'MiB', 'GiB', 'TiB') as $unit) {
        if ($value < 1024 || $unit === 'TiB') {
            if ($unit === 'B') {
                return (string)((int)$value) . ' B';
            }
            return number_format($value, 2) . ' ' . $unit;
        }
        $value /= 1024;
    }
    return (string)$bytes . ' B';
}

function pp_plain($value, $max)
{
    $value = trim((string)$value);
    if (strlen($value) > $max || preg_match('/[\x00-\x1f\x7f]/', $value)) {
        return null;
    }
    return $value;
}

function pp_check_token($token)
{
    $posted = isset($_POST['token']) ? (string)$_POST['token'] : '';
    return $token !== '' && hash_equals($token, $posted);
}

function pp_send_status()
{
    $path = PP_ROOT . '/state/send-remaining.status';
    if (!is_file($path)) {
        return null;
    }
    $allowed = array('state' => true, 'day' => true, 'total' => true, 'sent' => true, 'failed' => true, 'skipped' => true, 'current' => true, 'message' => true);
    $data = array();
    $lines = file($path, FILE_IGNORE_NEW_LINES);
    if (!is_array($lines)) {
        return null;
    }
    foreach ($lines as $line) {
        if (!preg_match('/\A([a-z_]+)=(.*)\z/', $line, $match)) {
            continue;
        }
        if (!isset($allowed[$match[1]])) {
            continue;
        }
        $data[$match[1]] = substr($match[2], 0, 300);
    }
    if (!isset($data['state']) || !in_array($data['state'], array('running', 'done', 'failed', 'critical'), true)) {
        return null;
    }
    return $data;
}

function pp_error_text($stderr)
{
    $text = trim((string)$stderr);
    if ($text === '') {
        return 'عملیات ناموفق بود';
    }
    $lines = preg_split("/\r\n|\n|\r/", $text);
    $kept = array();
    foreach ($lines as $line) {
        if (stripos($line, 'password') !== false && stripos($line, 'invalid') === false && stripos($line, 'must') === false) {
            continue;
        }
        $kept[] = $line;
    }
    $text = trim(implode("\n", $kept));
    if ($text === '') {
        return 'عملیات ناموفق بود';
    }
    return substr($text, 0, 2000);
}

function pp_reason_fa($text, $rules)
{
    $text = trim((string)$text);
    if ($text === '') {
        return '';
    }
    if (!is_array($rules)) {
        return $text;
    }
    foreach ($rules as $needle => $persian) {
        if ($needle !== '' && stripos($text, $needle) !== false) {
            return $persian;
        }
    }
    return $text;
}

$token = pp_token();
pp_headers();
if (!pp_allowed()) {
    if (is_file('/usr/local/cpanel/php/WHM.php')) {
        require_once '/usr/local/cpanel/php/WHM.php';
        WHM::header('PARSPACK Backup', 0, 0);
        echo '<p>Access denied.</p>';
        WHM::footer();
    } else {
        echo 'Access denied.';
    }
    exit;
}
if (isset($_SERVER['REQUEST_METHOD']) && $_SERVER['REQUEST_METHOD'] === 'POST') {
    $action = isset($_POST['action']) ? (string)$_POST['action'] : '';
    $returnDate = '';
    if (isset($_POST['date']) && preg_match('/\A\d{4}-\d{2}-\d{2}\z/', (string)$_POST['date'])) {
        $returnDate = (string)$_POST['date'];
    }
    $returnTab = 'dashboard';
    if ($action === 'save-settings' || $action === 'save-mail' || $action === 'test-connection') {
        $returnTab = 'settings';
    } elseif ($action === 'refresh-report' || $action === 'send-remaining') {
        $returnTab = 'reports';
    }
    if (!pp_check_token($token)) {
        pp_flash_set('bad', 'درخواست رد شد. صفحه را یک‌بار تازه کنید و دوباره ذخیره کنید.');
        pp_redirect($returnTab, $returnDate);
    }
    $rateName = 'save';
    if ($action === 'retry-account') {
        $rateName = 'retry';
    } elseif ($action === 'refresh-report') {
        $rateName = 'report';
    } elseif ($action === 'test-connection') {
        $rateName = 'connection';
    } elseif ($action === 'send-remaining') {
        $rateName = 'send';
    }
    list($rateCode) = pp_run('lib/parspack_ui.py', array('rate-limit', $rateName), '', 15);
    if ($rateCode !== 0) {
        pp_flash_set('bad', 'درخواست‌ها خیلی پشت سر هم بودند. یک دقیقه صبر کنید.');
        pp_redirect($returnTab, $returnDate);
    }
    if ($action === 'save-settings' || $action === 'save-mail') {
        $host = pp_plain(isset($_POST['smtp_host']) ? $_POST['smtp_host'] : '', 253);
        $port = pp_plain(isset($_POST['smtp_port']) ? $_POST['smtp_port'] : '', 5);
        $user = pp_plain(isset($_POST['smtp_user']) ? $_POST['smtp_user'] : '', 200);
        $from = pp_plain(isset($_POST['report_from']) ? $_POST['report_from'] : '', 254);
        $to = pp_plain(isset($_POST['report_to']) ? $_POST['report_to'] : '', 254);
        $password = isset($_POST['smtp_password']) ? (string)$_POST['smtp_password'] : '';
        if ($host === null || $port === null || $user === null || $from === null || $to === null
            || strlen($password) > 200 || preg_match('/[\r\n\0]/', $password)) {
            pp_flash_set('bad', 'یکی از مقدارهای فرم نامعتبر است.');
            pp_redirect('settings', '');
        }
        if ($port === '') {
            $port = '587';
        }
        $payload = array(
            'SMTP_ENABLED' => isset($_POST['smtp_enabled']) ? '1' : '0',
            'SMTP_HOST' => $host,
            'SMTP_PORT' => $port,
            'SMTP_USER' => $user,
            'REPORT_FROM' => $from,
            'REPORT_TO' => $to,
            'NOTIFY_ON_SUCCESS' => isset($_POST['notify_on_success']) ? '1' : '0',
            'NOTIFY_ON_FAILURE' => isset($_POST['notify_on_failure']) ? '1' : '0',
        );
        if ($password !== '') {
            $payload['SMTP_PASSWORD'] = $password;
        }
        $command = 'update-mail';
        if ($action === 'save-settings') {
            $command = 'update-settings';
            $webhook = pp_plain(isset($_POST['webhook_url']) ? $_POST['webhook_url'] : '', 300);
            $chat = pp_plain(isset($_POST['telegram_chat']) ? $_POST['telegram_chat'] : '', 20);
            $bytes = pp_plain(isset($_POST['alert_remote_bytes']) ? $_POST['alert_remote_bytes'] : '', 15);
            $keepDays = pp_plain(isset($_POST['remote_keep_days']) ? $_POST['remote_keep_days'] : '0', 4);
            $webhookSecret = isset($_POST['webhook_secret']) ? (string)$_POST['webhook_secret'] : '';
            $telegramToken = isset($_POST['telegram_token']) ? (string)$_POST['telegram_token'] : '';
            $slackUrl = isset($_POST['slack_webhook']) ? (string)$_POST['slack_webhook'] : '';
            if ($keepDays === '') {
                $keepDays = '0';
            }
            if ($webhook === null || $chat === null || $bytes === null || $keepDays === null
                || !preg_match('/\A(0|[1-9][0-9]{0,3})\z/', $keepDays) || (int)$keepDays > 3650
                || strlen($webhookSecret) > 300 || strlen($telegramToken) > 200 || strlen($slackUrl) > 300
                || preg_match('/[\r\n\0]/', $webhookSecret . $telegramToken . $slackUrl)) {
                pp_flash_set('bad', 'یکی از مقدارهای فرم نامعتبر است.');
                pp_redirect('settings', '');
            }
            $payload['audit_user'] = pp_audit_user();
            $payload['ALLOW_REMOTE_DELETE'] = isset($_POST['remote_delete']) ? '1' : '0';
            $payload['REMOTE_KEEP_DAYS'] = $keepDays;
            $payload['DELETE_LOCAL_AFTER_SUCCESS'] = isset($_POST['local_delete']) ? '1' : '0';
            $payload['ALERT_REMOTE_BYTES'] = $bytes === '' ? '0' : $bytes;
            $payload['WEBHOOK_ENABLED'] = isset($_POST['webhook_enabled']) ? '1' : '0';
            $payload['WEBHOOK_URL'] = $webhook;
            $payload['TELEGRAM_ENABLED'] = isset($_POST['telegram_enabled']) ? '1' : '0';
            $payload['TELEGRAM_CHAT'] = $chat;
            $payload['SLACK_ENABLED'] = isset($_POST['slack_enabled']) ? '1' : '0';
            if ($webhookSecret !== '') {
                $payload['WEBHOOK_SECRET'] = $webhookSecret;
            }
            if ($telegramToken !== '') {
                $payload['TELEGRAM_BOT_TOKEN'] = $telegramToken;
            }
            if ($slackUrl !== '') {
                $payload['SLACK_WEBHOOK_URL'] = $slackUrl;
            }
        }
        $json = json_encode($payload, JSON_UNESCAPED_UNICODE);
        if ($json === false) {
            pp_flash_set('bad', 'تنظیمات قابل ذخیره نبود.');
            pp_redirect('settings', '');
        }
        list($code, $stdout, $stderr) = pp_run('lib/parspack_config.py', array($command), $json, 30);
        if ($code === 0 && strpos($stdout, 'OK') !== false) {
            pp_flash_set('ok', 'تنظیمات ذخیره شد. رمزها در این صفحه نشان داده نمی‌شوند.');
        } else {
            pp_flash_set('bad', pp_error_text($stderr));
        }
        pp_redirect('settings', '');
    }
    if ($action === 'refresh-report') {
        set_time_limit(220);
        $args = array();
        if ($returnDate !== '') {
            $args[] = $returnDate;
        }
        list($code, $stdout, $stderr) = pp_run('bin/report.py', $args, '', 190);
        if ($code === 0) {
            $note = 'گزارش این روز ساخته شد.';
            if (strpos($stdout, 'alert skipped: duplicate') !== false) {
                $note .= ' هشدار تکراری فرستاده نشد.';
            } elseif (strpos($stdout, 'email sent') !== false) {
                $note .= ' ایمیل فرستاده شد.';
            } elseif (strpos($stdout, 'email skipped') !== false) {
                $note .= ' ایمیل این نتیجه در تنظیمات خاموش است.';
            } elseif (strpos($stderr, 'Failed to send') !== false || strpos($stderr, 'SMTP is not usable') !== false) {
                $note .= ' گزارش ذخیره شد، ولی ایمیل فرستاده نشد.';
            } else {
                $note .= ' ارسال ایمیل خاموش است.';
            }
            pp_flash_set('ok', $note);
        } else {
            pp_flash_set('bad', pp_error_text($stderr));
        }
        pp_redirect('reports', $returnDate);
    }
    if ($action === 'send-remaining') {
        list($code, $stdout, $stderr) = pp_exec(array('/bin/bash', PP_ROOT . '/bin/send-remaining.sh'), '', 20);
        if ($code === 0 && strpos($stdout, 'started') !== false) {
            pp_flash_set('ok', 'ارسال فایل‌های مانده شروع شد. چند لحظه بعد صفحه را تازه کنید.');
        } else {
            pp_flash_set('bad', pp_error_text($stderr !== '' ? $stderr : $stdout));
        }
        pp_redirect('reports', $returnDate);
    }
    if ($action === 'retry-account') {
        $account = isset($_POST['account']) ? (string)$_POST['account'] : '';
        if (!preg_match('/\A[A-Za-z0-9._+-]{1,64}\z/', $account)) {
            pp_flash_set('bad', 'نام حساب نامعتبر است.');
            pp_redirect('dashboard', $returnDate);
        }
        set_time_limit(220);
        list($code, $stdout, $stderr) = pp_run('lib/parspack_ops.py', array('retry', '--account', $account), '', 190);
        if ($code === 0 || $code === 1) {
            pp_flash_set('ok', 'تلاش دوباره برای این حساب انجام شد.');
        } else {
            pp_flash_set('bad', pp_error_text($stderr));
        }
        pp_redirect('dashboard', $returnDate);
    }
    if ($action === 'test-connection') {
        set_time_limit(90);
        list($code, $stdout, $stderr) = pp_run('lib/parspack_ops.py', array('connection-test'), '', 70);
        $text = trim(str_replace(array("\r", "\n"), ' ', $code === 0 ? $stdout : $stderr));
        $text = substr($text, 0, 500);
        if ($code === 0) {
            pp_flash_set('ok', $text !== '' ? $text : 'اتصال برقرار است.');
        } else {
            pp_flash_set('bad', $text !== '' ? $text : 'آزمایش اتصال ناموفق بود.');
        }
        pp_redirect('settings', '');
    }
    pp_flash_set('bad', 'درخواست ناشناخته بود.');
    pp_redirect('dashboard', '');
}

require_once '/usr/local/cpanel/php/WHM.php';
WHM::header('PARSPACK Backup', 0, 0);

$tab = 'dashboard';
if (isset($_GET['tab']) && in_array($_GET['tab'], array('dashboard', 'reports', 'settings'), true)) {
    $tab = (string)$_GET['tab'];
}
$viewDate = '';
if (isset($_GET['date']) && preg_match('/\A\d{4}-\d{2}-\d{2}\z/', (string)$_GET['date'])) {
    $viewDate = (string)$_GET['date'];
}
$flash = pp_flash_take();
$dashboard = null;
$settings = null;
$audit = array();
$loadError = '';
if ($tab === 'settings') {
    list($code, $stdout, $stderr) = pp_run('lib/parspack_config.py', array('mail-settings'), '', 20);
    $decoded = json_decode($stdout, true);
    if ($code === 0 && is_array($decoded)) {
        $settings = $decoded;
    } else {
        $loadError = pp_error_text($stderr);
    }
    list($auditCode, $auditOut) = pp_run('lib/parspack_ui.py', array('audit'), '', 15);
    $auditDecoded = json_decode($auditOut, true);
    if ($auditCode === 0 && is_array($auditDecoded)) {
        $audit = $auditDecoded;
    }
} elseif ($tab === 'reports') {
    $args = array('dashboard');
    if ($viewDate !== '') {
        $args[] = $viewDate;
    }
    list($code, $stdout, $stderr) = pp_run('bin/report.py', $args, '', 30);
    $decoded = json_decode($stdout, true);
    if ($code === 0 && is_array($decoded)) {
        $dashboard = $decoded;
    } else {
        $loadError = pp_error_text($stderr);
    }
} else {
    $args = array('dashboard');
    if ($viewDate !== '') {
        $args[] = $viewDate;
    }
    list($code, $stdout, $stderr) = pp_run('lib/parspack_ui.py', $args, '', 20);
    $decoded = json_decode($stdout, true);
    if ($code === 0 && is_array($decoded)) {
        $dashboard = $decoded;
    } else {
        $loadError = pp_error_text($stderr);
    }
}

$report = (is_array($dashboard) && isset($dashboard['report']) && is_array($dashboard['report'])) ? $dashboard['report'] : null;
$sendStatus = ($tab === 'reports') ? pp_send_status() : null;
$statusText = array(
    'OK' => 'موفق',
    'WARNING' => 'هشدار',
    'PROBLEM' => 'ناموفق',
    'CRITICAL' => 'بحرانی',
    'EMPTY' => 'بدون انتقال',
);
$warningText = array(
    'system backup is incomplete' => 'پوشه system و manifest ناقص است',
    'accounts transferred but system backup incomplete' => 'حساب‌ها منتقل شده‌اند؛ پوشه system و manifest هنوز تمام نشده',
    'destination storage is full' => 'فضای مقصد پر است',
    'remote storage was not readable' => 'فضای مقصد خوانده نشد',
    'verification failed' => 'تأیید مقصد ناموفق بود',
    'some accounts failed' => 'بعضی حساب‌ها ناموفق بودند',
    'local cleanup failed' => 'حذف فایل محلی ناموفق بود',
    'remote keep window did not finish' => 'پاک‌سازی بکاپ‌های قدیمی مقصد تمام نشد',
    'recorded remote usage is over the threshold' => 'حجم ثبت‌شده مقصد از آستانه گذشته است',
    'the daily report is incomplete' => 'گزارش روزانه ناقص است',
);
$statusCauseText = array(
    'OK' => '',
    'WARNING' => 'انتقال اصلی انجام شد ولی هشدارهایی وجود دارد.',
    'PROBLEM' => 'برخی حساب‌ها منتقل نشدند یا فهرست مقصد خوانده نشد.',
    'CRITICAL' => 'حساب‌ها ممکن است منتقل شده باشند، ولی پوشه system و manifest.json هنوز تمام و تأیید نشده‌اند.',
    'EMPTY' => 'برای این روز هنوز انتقالی ثبت نشده است.',
);
$dbStatusText = array(
    'UPLOAD_FAILED' => 'آپلود ناموفق',
    'VERIFY_FAILED' => 'تأیید مقصد ناموفق',
    'LOCAL_CLEANUP_FAILED' => 'حذف فایل محلی ناموفق',
    'VERIFIED' => 'تأیید شده',
    'LOCAL_DELETED' => 'فایل محلی حذف شد',
    'SKIPPED' => 'رد شده',
);
$transferStateText = array(
    'ok' => 'موفق',
    'success' => 'موفق',
    'verified' => 'موفق',
    'failed' => 'ناموفق',
    'error' => 'ناموفق',
    'missing' => 'ناتمام',
    'pending' => 'ناتمام',
    'incomplete' => 'ناتمام',
    'partial' => 'ناتمام',
);
$reasonFaRules = array(
    'Destination storage is full' => 'فضای مقصد پر است',
    'storage is full' => 'فضای مقصد پر است',
    'Copied object failed verification' => 'بعد از کپی به نام نهایی، تأیید مقصد ناموفق بود',
    'Remote metadata verification mismatch' => 'متادیتای مقصد با فایل محلی جور نبود',
    'Remote content checksum mismatch' => 'checksum محتوای مقصد با فایل محلی یکی نبود',
    'Remote size mismatch' => 'اندازه فایل در مقصد با مبدأ یکی نبود',
    'Could not verify remote object content' => 'خواندن محتوا از مقصد برای تأیید ممکن نشد',
    'Upload failed after' => 'آپلود بعد از چند تلاش ناموفق ماند',
    'Upload lock was still held' => 'قفل آپلود آزاد نشد و این فایل منتظر ماند',
    'system mirror failed' => 'ارسال فایل system ناموفق بود',
    'remote content did not match' => 'محتوای مقصد با فایل محلی یکی نبود',
    'Temporary upload failed verification' => 'فایل موقت آپلود تأیید نشد و حذف شد',
    'Verified temporary object could not be copied' => 'کپی از فایل موقت تأییدشده به نام نهایی انجام نشد',
);
$runStateText = array(
    'open' => 'باز',
    'system_running' => 'در حال ارسال system',
    'complete' => 'کامل',
    'partial' => 'ناقص',
);
$systemStateText = array(
    'none' => 'ندارد',
    'pending' => 'ناتمام',
    'partial' => 'ناقص',
    'complete' => 'کامل',
    'open' => 'باز',
    'system_running' => 'در حال ارسال',
);
?>
<div class="pp" dir="rtl">
<style>
.pp{font-family:Tahoma,Arial,sans-serif;color:#1f2933;line-height:1.6;max-width:1100px}
.pp-tabs{display:flex;gap:6px;margin:0 0 18px;flex-wrap:wrap;border-bottom:1px solid #d9e2ec;padding-bottom:10px}
.pp-tabs a{padding:8px 16px;border-radius:6px;text-decoration:none;background:#eef2f5;color:#243b53;font-size:14px}
.pp-tabs a.on{background:#1d4f91;color:#fff}
.pp-note,.pp-bad{padding:11px 14px;border-radius:8px;margin:0 0 14px;font-size:14px}
.pp-note{background:#e8f5e9;border:1px solid #c8e6c9}
.pp-bad{background:#ffebee;border:1px solid #ffcdd2}
.pp-card{background:#fff;border:1px solid #d9e2ec;border-radius:10px;padding:14px 16px;margin:0 0 14px}
.pp-ok{background:#e8f5e9;border-color:#c8e6c9}
.pp-warning{background:#fff8e1;border-color:#ffe082}
.pp-problem,.pp-critical{background:#ffebee;border-color:#ffcdd2}
.pp-empty{background:#f7f9fb}
.pp-grid{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 14px}
.pp-stat{flex:1 1 160px;background:#fff;border:1px solid #d9e2ec;border-radius:10px;padding:12px 14px;min-width:140px}
.pp-stat .k{display:block;color:#627d98;font-size:12px;margin-bottom:4px}
.pp-stat .v{display:block;font-size:18px;font-weight:bold;color:#102a43}
.pp-stat .s{display:block;color:#627d98;font-size:12px;margin-top:4px}
.pp-bar{height:8px;background:#eef2f5;border-radius:99px;overflow:hidden;margin-top:8px}
.pp-bar > span{display:block;height:100%;background:#1d4f91;border-radius:99px}
.pp-bar.warn > span{background:#f0b429}
.pp-bar.bad > span{background:#d64545}
.pp h3{margin:0 0 10px;font-size:15px;color:#102a43}
.pp table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;border:1px solid #d9e2ec;border-radius:8px;overflow:hidden}
.pp th,.pp td{border-bottom:1px solid #e4e7eb;padding:9px 11px;text-align:right;vertical-align:top;word-break:break-word}
.pp th{background:#f0f4f8;font-weight:bold;color:#243b53;border-bottom:1px solid #d9e2ec}
.pp tr:last-child td{border-bottom:0}
.pp tr.bad td{background:#fff5f5}
.pp tr.good td{background:#f6ffed}
.pp label{display:block;margin:10px 0 4px;font-size:13px;color:#243b53}
.pp input[type=text],.pp input[type=password]{width:100%;max-width:420px;padding:8px 10px;border:1px solid #bcccdc;border-radius:6px;font-size:14px}
.pp .check{margin:8px 0}
.pp button,.pp .btn{display:inline-block;margin:0;padding:8px 14px;border:0;border-radius:6px;background:#1d4f91;color:#fff;font-size:13px;cursor:pointer;text-decoration:none;font-family:inherit}
.pp button.secondary,.pp .btn.secondary{background:#627d98}
.pp button:hover,.pp .btn:hover{opacity:.92}
.pp .actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 14px}
.pp .days{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}
.pp .days a{padding:5px 10px;background:#eef2f5;border-radius:999px;text-decoration:none;color:#243b53;font-size:12px}
.pp .days a.on{background:#1d4f91;color:#fff}
.pp .muted{color:#627d98;font-size:12px}
.pp .lead{margin:4px 0 0;font-size:14px;color:#243b53}
.pp details{background:#fff;border:1px solid #d9e2ec;border-radius:10px;margin:0 0 14px;padding:0 14px 8px}
.pp summary{cursor:pointer;font-weight:bold;padding:12px 0;color:#243b53;font-size:14px}
.pp details .muted{margin-top:0}
.pp .pager{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0 12px}
.pp .pager a{padding:5px 10px;background:#eef2f5;border-radius:6px;text-decoration:none;color:#243b53;font-size:12px}
.pp .reason{max-width:26em}
.pp .warn-list{margin:0;padding:0 0 0 18px}
.pp .warn-list li{margin:0 0 6px}
.pp .tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#eef2f5;color:#243b53}
.pp .tag.bad{background:#ffe3e3;color:#9b1c1c}
.pp .tag.warn{background:#fff3c4;color:#8d6b00}
.pp .tag.ok{background:#d3f9d8;color:#2b8a3e}
</style>
<div class="pp-tabs">
  <a class="<?php echo $tab === 'dashboard' ? 'on' : ''; ?>" href="?tab=dashboard">پیشخوان</a>
  <a class="<?php echo $tab === 'reports' ? 'on' : ''; ?>" href="?tab=reports">گزارش‌ها</a>
  <a class="<?php echo $tab === 'settings' ? 'on' : ''; ?>" href="?tab=settings">تنظیمات</a>
</div>
<?php if (is_array($flash)) { ?>
  <div class="<?php echo $flash['kind'] === 'ok' ? 'pp-note' : 'pp-bad'; ?>"><?php echo pp_h($flash['text']); ?></div>
<?php } ?>
<?php if ($loadError !== '') { ?>
  <div class="pp-bad"><?php echo pp_h($loadError); ?></div>
<?php } ?>
<?php if ($tab === 'dashboard') {
    $failedRows = (is_array($dashboard) && isset($dashboard['failed']) && is_array($dashboard['failed'])) ? $dashboard['failed'] : array();
    $levels = array();
    if (is_array($dashboard) && isset($dashboard['verification']['levels']) && is_array($dashboard['verification']['levels'])) {
        $levels = $dashboard['verification']['levels'];
    }
    $warnings = (is_array($dashboard) && isset($dashboard['warnings']) && is_array($dashboard['warnings'])) ? $dashboard['warnings'] : array();
    $viewDay = (is_array($dashboard) && !empty($dashboard['date'])) ? (string)$dashboard['date'] : '-';
    $runRaw = (is_array($dashboard) && isset($dashboard['run_state'])) ? (string)$dashboard['run_state'] : '';
    $sysRaw = (is_array($dashboard) && isset($dashboard['system_status'])) ? (string)$dashboard['system_status'] : '';
    $runLabel = ($runRaw !== '' && isset($runStateText[$runRaw])) ? $runStateText[$runRaw] : ($runRaw !== '' ? $runRaw : '-');
    $sysLabel = ($sysRaw !== '' && isset($systemStateText[$sysRaw])) ? $systemStateText[$sysRaw] : ($sysRaw !== '' ? $sysRaw : '-');
    $repSt = (is_array($dashboard) && isset($dashboard['report_status'])) ? (string)$dashboard['report_status'] : '';
    $repLabel = ($repSt !== '' && isset($statusText[$repSt])) ? $statusText[$repSt] : ($repSt !== '' ? $repSt : '-');
    $okN = (int)(is_array($dashboard) && isset($dashboard['accounts_ok']) ? $dashboard['accounts_ok'] : 0);
    $badN = (int)(is_array($dashboard) && isset($dashboard['accounts_failed']) ? $dashboard['accounts_failed'] : 0);
    $dayRemote = (int)(is_array($dashboard) && isset($dashboard['remote_bytes']) ? $dashboard['remote_bytes'] : 0);
    $freed = (int)(is_array($dashboard) && isset($dashboard['local_freed_bytes']) ? $dashboard['local_freed_bytes'] : 0);
    $storage = (is_array($dashboard) && isset($dashboard['storage']) && is_array($dashboard['storage'])) ? $dashboard['storage'] : array();
    $localStore = (isset($storage['local']) && is_array($storage['local'])) ? $storage['local'] : array();
    $remoteStore = (isset($storage['remote']) && is_array($storage['remote'])) ? $storage['remote'] : array();
    $localTotal = (int)(isset($localStore['total']) ? $localStore['total'] : 0);
    $localUsed = (int)(isset($localStore['used']) ? $localStore['used'] : 0);
    $localFree = (int)(isset($localStore['free']) ? $localStore['free'] : 0);
    $localOk = !empty($localStore['ok']);
    $localPct = ($localOk && $localTotal > 0) ? (int)round(($localUsed * 100) / $localTotal) : 0;
    $localBar = ($localPct >= 90) ? 'bad' : (($localPct >= 75) ? 'warn' : '');
    $remoteUsed = (int)(isset($remoteStore['used']) ? $remoteStore['used'] : 0);
    $remoteRec = (int)(isset($remoteStore['recorded']) ? $remoteStore['recorded'] : 0);
    $remoteThr = (int)(isset($remoteStore['threshold']) ? $remoteStore['threshold'] : 0);
    $remoteOk = !empty($remoteStore['ok']);
    $dayLocalBytes = (int)(isset($storage['local_day_bytes']) ? $storage['local_day_bytes'] : 0);
    $dashDays = array();
    if (is_array($dashboard) && isset($dashboard['dates']) && is_array($dashboard['dates'])) {
        foreach ($dashboard['dates'] as $d) {
            if (is_string($d) && preg_match('/\A\d{4}-\d{2}-\d{2}\z/', $d)) {
                $dashDays[] = $d;
            }
        }
    }
?>
  <?php if ($dashDays) { ?>
    <div class="days" title="۱۰ روز اخیر">
      <?php foreach (array_slice($dashDays, 0, 10) as $d) { ?>
        <a class="<?php echo ($d === $viewDay) ? 'on' : ''; ?>" href="?tab=dashboard&amp;date=<?php echo pp_h($d); ?>"><?php echo pp_h($d); ?></a>
      <?php } ?>
    </div>
  <?php } ?>

  <div class="pp-grid">
    <div class="pp-stat">
      <span class="k">روز انتخاب‌شده</span>
      <span class="v"><?php echo pp_h($viewDay); ?></span>
      <span class="s">اجرا: <?php echo pp_h($runLabel); ?> — system: <?php echo pp_h($sysLabel); ?></span>
    </div>
    <div class="pp-stat">
      <span class="k">حساب‌ها</span>
      <span class="v"><?php echo (int)$okN; ?> / <?php echo (int)($okN + $badN); ?></span>
      <span class="s">موفق <?php echo (int)$okN; ?> — ناموفق <?php echo (int)$badN; ?></span>
    </div>
    <div class="pp-stat">
      <span class="k">حجم این روز در مقصد</span>
      <span class="v"><?php echo pp_h(pp_size($dayRemote)); ?></span>
      <span class="s">آزادشده محلی: <?php echo pp_h(pp_size($freed)); ?></span>
    </div>
    <div class="pp-stat">
      <span class="k">آخرین گزارش</span>
      <span class="v"><?php echo pp_h($repLabel); ?></span>
      <span class="s"><?php echo pp_h(is_array($dashboard) && !empty($dashboard['finished']) ? $dashboard['finished'] : '-'); ?></span>
    </div>
  </div>

  <div class="pp-grid">
    <div class="pp-stat">
      <span class="k">دیسک مبدأ (سرور)</span>
      <?php if ($localOk) { ?>
        <span class="v"><?php echo pp_h(pp_size($localFree)); ?> آزاد</span>
        <span class="s">مصرف‌شده <?php echo pp_h(pp_size($localUsed)); ?> از <?php echo pp_h(pp_size($localTotal)); ?> (<?php echo (int)$localPct; ?>٪)</span>
        <div class="pp-bar <?php echo pp_h($localBar); ?>"><span style="width:<?php echo (int)min(100, max(0, $localPct)); ?>%"></span></div>
        <?php if ($dayLocalBytes > 0) { ?>
          <span class="s">بکاپ همین روز روی دیسک: <?php echo pp_h(pp_size($dayLocalBytes)); ?></span>
        <?php } ?>
      <?php } else { ?>
        <span class="v">نامشخص</span>
        <span class="s">مسیر مبدأ خوانده نشد</span>
      <?php } ?>
    </div>
    <div class="pp-stat">
      <span class="k">فضای مقصد (ابری)</span>
      <?php if ($remoteOk || $remoteRec > 0) { ?>
        <span class="v"><?php echo pp_h(pp_size($remoteUsed > 0 ? $remoteUsed : $remoteRec)); ?> مصرف</span>
        <span class="s">
          ثبت‌شده در وضعیت: <?php echo pp_h(pp_size($remoteRec)); ?>
          <?php if ($remoteThr > 0) { ?>
            — آستانه هشدار: <?php echo pp_h(pp_size($remoteThr)); ?>
          <?php } else { ?>
            — سهمیهٔ آزاد مقصد معمولاً از S3 گزارش نمی‌شود
          <?php } ?>
        </span>
        <?php if ($remoteThr > 0 && ($remoteUsed > 0 || $remoteRec > 0)) {
            $u = $remoteUsed > 0 ? $remoteUsed : $remoteRec;
            $rp = (int)min(100, round(($u * 100) / $remoteThr));
            $rb = ($rp >= 90) ? 'bad' : (($rp >= 75) ? 'warn' : '');
        ?>
          <div class="pp-bar <?php echo pp_h($rb); ?>"><span style="width:<?php echo (int)$rp; ?>%"></span></div>
          <span class="s">نسبت به آستانه هشدار: <?php echo (int)$rp; ?>٪</span>
        <?php } ?>
      <?php } else { ?>
        <span class="v">نامشخص</span>
        <span class="s">هنوز حجمی از مقصد خوانده/ثبت نشده</span>
      <?php } ?>
    </div>
  </div>

  <?php if ($warnings) { ?>
    <div class="pp-card pp-warning">
      <h3>هشدارها</h3>
      <ul class="warn-list">
        <?php foreach ($warnings as $warning) {
            if (!is_array($warning)) {
                continue;
            }
            $wText = isset($warning['text']) ? (string)$warning['text'] : '';
            if (isset($warningText[$wText])) {
                $wText = $warningText[$wText];
            }
            $wSev = isset($warning['severity']) ? (string)$warning['severity'] : '';
            $tagClass = 'tag';
            if ($wSev === 'critical') {
                $wSev = 'بحرانی';
                $tagClass .= ' bad';
            } elseif ($wSev === 'warning') {
                $wSev = 'هشدار';
                $tagClass .= ' warn';
            } else {
                $wSev = 'اطلاع';
            }
        ?>
          <li><span class="<?php echo pp_h($tagClass); ?>"><?php echo pp_h($wSev); ?></span> <?php echo pp_h($wText); ?></li>
        <?php } ?>
      </ul>
    </div>
  <?php } ?>

  <div class="pp-card">
    <h3>حساب‌های ناموفق</h3>
    <table>
      <tr><th>حساب</th><th>وضعیت</th><th>دلیل</th><th></th></tr>
      <?php if (!$failedRows) { ?>
        <tr><td colspan="4">حساب ناموفقی نیست.</td></tr>
      <?php } ?>
      <?php foreach ($failedRows as $row) {
          if (!is_array($row) || empty($row['account']) || !preg_match('/\A[A-Za-z0-9._+-]{1,64}\z/', (string)$row['account'])) {
              continue;
          }
      ?>
        <tr class="bad">
          <td><?php echo pp_h($row['account']); ?></td>
          <td><?php
            $dbSt = isset($row['status']) ? (string)$row['status'] : '';
            echo pp_h(isset($dbStatusText[$dbSt]) ? $dbStatusText[$dbSt] : $dbSt);
          ?></td>
          <td><?php
            $failReason = isset($row['reason']) ? (string)$row['reason'] : '';
            $failReason = pp_reason_fa($failReason, $reasonFaRules);
            echo pp_h($failReason !== '' ? $failReason : '-');
          ?></td>
          <td>
            <form method="post" action="">
              <input type="hidden" name="token" value="<?php echo pp_h($token); ?>">
              <input type="hidden" name="action" value="retry-account">
              <input type="hidden" name="account" value="<?php echo pp_h($row['account']); ?>">
              <input type="hidden" name="date" value="<?php echo pp_h(isset($row['date']) ? $row['date'] : ''); ?>">
              <button type="submit">تلاش دوباره</button>
            </form>
          </td>
        </tr>
      <?php } ?>
    </table>
    <p class="muted" style="margin:10px 0 0">تأییدها — sha256: <?php echo (int)(isset($levels['sha256']) ? $levels['sha256'] : 0); ?>، etag: <?php echo (int)(isset($levels['etag']) ? $levels['etag'] : 0); ?>، size-only: <?php echo (int)(isset($levels['size-only']) ? $levels['size-only'] : 0); ?></p>
  </div>
<?php } elseif ($tab === 'reports') { ?>
  <?php if (is_array($dashboard) && isset($dashboard['mode']) && $dashboard['mode'] === 'TEST') { ?>
    <div class="pp-card pp-warning">حالت TEST است؛ فایلی به مقصد فرستاده نمی‌شود.</div>
  <?php } ?>
  <?php
    $days = array();
    if (is_array($dashboard) && isset($dashboard['days']) && is_array($dashboard['days'])) {
        foreach ($dashboard['days'] as $day) {
            if (is_string($day) && preg_match('/\A\d{4}-\d{2}-\d{2}\z/', $day)) {
                $days[] = $day;
            }
        }
    }
    $activeReportDay = (is_array($report) && isset($report['date'])) ? (string)$report['date'] : $viewDate;
  ?>
  <?php if ($days) {
      // Newest first for chips; backend already capped at 10.
      $dayChips = array_slice(array_reverse($days), 0, 10);
  ?>
    <div class="days" title="۱۰ روز اخیر">
      <?php foreach ($dayChips as $day) { ?>
        <a class="<?php echo ($day === $activeReportDay) ? 'on' : ''; ?>" href="?tab=reports&amp;date=<?php echo pp_h($day); ?>"><?php echo pp_h($day); ?></a>
      <?php } ?>
    </div>
  <?php } ?>

  <div class="actions">
    <form method="post" action="" style="margin:0">
      <input type="hidden" name="token" value="<?php echo pp_h($token); ?>">
      <input type="hidden" name="action" value="send-remaining">
      <button type="submit">ارسال مانده‌ها</button>
    </form>
    <?php if (is_array($report) && !empty($report['date'])) { ?>
      <form method="post" action="" style="margin:0">
        <input type="hidden" name="token" value="<?php echo pp_h($token); ?>">
        <input type="hidden" name="action" value="refresh-report">
        <input type="hidden" name="date" value="<?php echo pp_h($report['date']); ?>">
        <button type="submit" class="secondary">بررسی مجدد مقصد</button>
      </form>
    <?php } ?>
  </div>
  <details>
    <summary>راهنمای کوتاه ارسال دستی</summary>
    <p class="muted">آخرین روزی که هنوز روی سرور است ارسال می‌شود. هر فایل تا ۵ بار تلاش می‌شود. اگر حذف محلی روشن باشد، بعد از تأیید موفق همان فایل از مبدأ پاک می‌شود. دکمه در صورت اجرای موازی تکرار نمی‌شود.</p>
  </details>

  <?php if (is_array($sendStatus)) {
      $sendState = $sendStatus['state'];
      $sendClass = 'pp-card';
      if ($sendState === 'failed' || $sendState === 'critical') {
          $sendClass = 'pp-card pp-critical';
      } elseif ($sendState === 'running') {
          $sendClass = 'pp-card pp-warning';
      } elseif ($sendState === 'done') {
          $sendClass = 'pp-card pp-ok';
      }
  ?>
    <div class="<?php echo pp_h($sendClass); ?>">
      <strong>
        <?php if ($sendState === 'running') { ?>
          ارسال دستی در حال انجام است
        <?php } elseif ($sendState === 'failed') { ?>
          ارسال دستی ناتمام ماند
        <?php } elseif ($sendState === 'critical') { ?>
          روز ناقص است؛ انتقال system تمام نشده
        <?php } else { ?>
          آخرین ارسال دستی تمام شد
        <?php } ?>
      </strong>
      <div class="muted">
        روز <?php echo pp_h(isset($sendStatus['day']) && $sendStatus['day'] !== '' ? $sendStatus['day'] : '-'); ?>
        — موفق <?php echo (int)(isset($sendStatus['sent']) ? $sendStatus['sent'] : 0); ?>
        — ناموفق <?php echo (int)(isset($sendStatus['failed']) ? $sendStatus['failed'] : 0); ?>
        — ردشده <?php echo (int)(isset($sendStatus['skipped']) ? $sendStatus['skipped'] : 0); ?>
      </div>
      <?php if (!empty($sendStatus['message'])) { ?>
        <div class="lead"><?php echo pp_h($sendStatus['message']); ?></div>
      <?php } ?>
    </div>
  <?php } ?>

  <?php if (!$report) { ?>
    <div class="pp-card">هنوز گزارشی برای نمایش نیست.</div>
  <?php } else {
      $status = isset($report['status']) ? (string)$report['status'] : 'EMPTY';
      if (!isset($statusText[$status])) {
          $status = 'EMPTY';
      }
      $rows = (isset($report['rows']) && is_array($report['rows'])) ? $report['rows'] : array();
      $okCount = 0;
      $badCount = 0;
      foreach ($rows as $row) {
          if (is_array($row) && !empty($row['ok'])) {
              $okCount++;
          } elseif (is_array($row)) {
              $badCount++;
          }
      }
      $source = isset($report['source']) ? (string)$report['source'] : '';
      $statusNote = '';
      if (!empty($report['status_note']) && is_string($report['status_note'])) {
          $statusNote = trim($report['status_note']);
      } elseif (!empty($report['db_note']) && is_string($report['db_note'])) {
          $statusNote = trim($report['db_note']);
      }
      if ($statusNote === '' && isset($statusCauseText[$status])) {
          $statusNote = $statusCauseText[$status];
      }
      $tagClass = 'tag';
      if ($status === 'CRITICAL' || $status === 'PROBLEM') {
          $tagClass .= ' bad';
      } elseif ($status === 'WARNING') {
          $tagClass .= ' warn';
      } elseif ($status === 'OK') {
          $tagClass .= ' ok';
      }
  ?>
    <div class="pp-card pp-<?php echo pp_h(strtolower($status)); ?>">
      <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between">
        <strong>روز <?php echo pp_h(isset($report['date']) ? $report['date'] : ''); ?></strong>
        <span class="<?php echo pp_h($tagClass); ?>"><?php echo pp_h($statusText[$status]); ?></span>
      </div>
      <?php if ($statusNote !== '') { ?>
        <p class="lead"><?php echo pp_h($statusNote); ?></p>
      <?php } ?>
    </div>
    <?php
      $acctOk = isset($report['account_transferred']) ? (int)$report['account_transferred'] : (int)$okCount;
      $acctTotal = isset($report['account_expected']) ? (int)$report['account_expected'] : (int)count($rows);
      $acctBad = isset($report['account_failed']) ? (int)$report['account_failed'] : (int)$badCount;
      $filesOk = isset($report['files_transferred']) ? (int)$report['files_transferred'] : $acctOk;
      $filesTotal = isset($report['files_expected']) ? (int)$report['files_expected'] : $acctTotal;
      $sysOk = isset($report['system_transferred']) ? (int)$report['system_transferred'] : 0;
      $sysTotal = isset($report['system_expected']) ? (int)$report['system_expected'] : 0;
    ?>
    <div class="pp-grid">
      <div class="pp-stat">
        <span class="k">حساب‌های منتقل‌شده</span>
        <span class="v"><?php echo (int)$acctOk; ?> / <?php echo (int)$acctTotal; ?></span>
        <span class="s">accounts — ناموفق: <?php echo (int)$acctBad; ?></span>
      </div>
      <div class="pp-stat">
        <span class="k">کل فایل‌ها</span>
        <span class="v"><?php echo (int)$filesOk; ?> / <?php echo (int)$filesTotal; ?></span>
        <span class="s">accounts + system (system: <?php echo (int)$sysOk; ?> / <?php echo (int)$sysTotal; ?>)</span>
      </div>
      <div class="pp-stat">
        <span class="k">حجم منتقل‌شده</span>
        <span class="v"><?php echo pp_h(pp_size(isset($report['transferred_bytes']) ? $report['transferred_bytes'] : 0)); ?></span>
        <span class="s"><?php echo pp_h(isset($report['started']) && $report['started'] !== '' ? $report['started'] : '-'); ?> تا <?php echo pp_h(isset($report['finished']) && $report['finished'] !== '' ? $report['finished'] : '-'); ?></span>
      </div>
    </div>
    <?php
      $pageSize = 20;
      $totalRows = count($rows);
      $page = 1;
      if (isset($_GET['page']) && preg_match('/\A[1-9][0-9]{0,4}\z/', (string)$_GET['page'])) {
          $page = (int)$_GET['page'];
      }
      $pageCount = $totalRows > 0 ? (int)ceil($totalRows / $pageSize) : 1;
      if ($page > $pageCount) {
          $page = $pageCount;
      }
      $offset = ($page - 1) * $pageSize;
      $pageRows = array_slice($rows, $offset, $pageSize);
      $fromRow = $totalRows === 0 ? 0 : $offset + 1;
      $toRow = $totalRows === 0 ? 0 : min($offset + $pageSize, $totalRows);
      $reportDate = isset($report['date']) ? (string)$report['date'] : '';
      $pageBase = '?tab=reports';
      if ($reportDate !== '' && preg_match('/\A\d{4}-\d{2}-\d{2}\z/', $reportDate)) {
          $pageBase .= '&amp;date=' . rawurlencode($reportDate);
      }
    ?>
    <div class="pager">
      <span class="muted">نمایش <?php echo (int)$fromRow; ?>–<?php echo (int)$toRow; ?> از <?php echo (int)$totalRows; ?></span>
      <?php if ($page > 1) { ?>
        <a href="<?php echo $pageBase; ?>&amp;page=<?php echo (int)($page - 1); ?>">قبلی</a>
      <?php } ?>
      <?php if ($page < $pageCount) { ?>
        <a href="<?php echo $pageBase; ?>&amp;page=<?php echo (int)($page + 1); ?>">بعدی</a>
      <?php } ?>
    </div>
    <table>
      <tr><th>کاربر</th><th>وضعیت انتقال</th><th>حجم مقصد</th><th>حجم مبدأ</th><th>زمان</th><th>توضیح</th></tr>
      <?php if (!$pageRows) { ?>
        <tr><td colspan="6">کاربری برای این روز پیدا نشد.</td></tr>
      <?php } ?>
      <?php foreach ($pageRows as $row) {
          if (!is_array($row)) {
              continue;
          }
          $when = '';
          if (!empty($row['end'])) {
              $when = (string)$row['end'];
          } elseif (!empty($row['start'])) {
              $when = (string)$row['start'];
          }
          $stateRaw = isset($row['state']) ? (string)$row['state'] : '';
          $stateKey = strtolower($stateRaw);
          if (!empty($row['ok'])) {
              $stateLabel = 'موفق';
          } elseif (isset($transferStateText[$stateKey])) {
              $stateLabel = $transferStateText[$stateKey];
          } elseif ($stateRaw === '') {
              $stateLabel = 'ناتمام';
          } else {
              $stateLabel = $stateRaw;
          }
          $reasonFull = pp_reason_fa(isset($row['reason']) ? (string)$row['reason'] : '', $reasonFaRules);
          $reasonShort = $reasonFull;
          if (function_exists('mb_strlen') && mb_strlen($reasonShort) > 80) {
              $reasonShort = mb_substr($reasonShort, 0, 77) . '...';
          } elseif (strlen($reasonShort) > 80) {
              $reasonShort = substr($reasonShort, 0, 77) . '...';
          }
      ?>
        <tr class="<?php echo !empty($row['ok']) ? 'good' : 'bad'; ?>">
          <td><?php echo pp_h(isset($row['user']) ? $row['user'] : ''); ?></td>
          <td><?php echo pp_h($stateLabel); ?></td>
          <td><?php echo pp_h(pp_size(isset($row['remote_size']) ? $row['remote_size'] : 0)); ?></td>
          <td><?php echo pp_h(pp_size(isset($row['local_size']) ? $row['local_size'] : 0)); ?></td>
          <td><?php echo pp_h($when !== '' ? $when : '-'); ?></td>
          <td class="reason" title="<?php echo pp_h($reasonFull !== '' ? $reasonFull : '-'); ?>"><?php echo pp_h($reasonShort !== '' ? $reasonShort : '-'); ?></td>
        </tr>
      <?php } ?>
    </table>
    <?php if ($pageCount > 1) { ?>
      <div class="pager">
        <span class="muted">صفحه <?php echo (int)$page; ?> از <?php echo (int)$pageCount; ?></span>
        <?php if ($page > 1) { ?>
          <a href="<?php echo $pageBase; ?>&amp;page=<?php echo (int)($page - 1); ?>">قبلی</a>
        <?php } ?>
        <?php if ($page < $pageCount) { ?>
          <a href="<?php echo $pageBase; ?>&amp;page=<?php echo (int)($page + 1); ?>">بعدی</a>
        <?php } ?>
      </div>
    <?php } ?>
    <?php
      $leftovers = (isset($report['leftovers']) && is_array($report['leftovers'])) ? $report['leftovers'] : array();
      if ($leftovers) {
    ?>
      <h3>فایل‌های اضافی مقصد</h3>
      <ul>
        <?php foreach ($leftovers as $item) { ?>
          <li><?php echo pp_h(is_string($item) ? $item : ''); ?></li>
        <?php } ?>
      </ul>
    <?php } ?>
  <?php } ?>
<?php } else { ?>
  <?php if (is_array($settings)) { ?>
    <div class="pp-card">
      <div>حالت انتقال: <?php echo pp_h(isset($settings['mode']) ? $settings['mode'] : ''); ?></div>
      <div>مقصد: <?php echo pp_h(isset($settings['endpoint']) ? $settings['endpoint'] : ''); ?></div>
      <div>باکت: <?php echo pp_h(isset($settings['bucket']) ? $settings['bucket'] : ''); ?>
        — پیشوند: <?php echo pp_h(isset($settings['prefix']) && $settings['prefix'] !== '' ? $settings['prefix'] : '(ندارد)'); ?></div>
      <div class="muted">کلید فضای ابری: <?php echo pp_h(isset($settings['access_key']) ? $settings['access_key'] : 'Missing'); ?>
        — کلید محرمانه: <?php echo pp_h(isset($settings['secret_key']) ? $settings['secret_key'] : 'Missing'); ?>
        — رمز انتقال: <?php echo pp_h(isset($settings['transport_password']) ? $settings['transport_password'] : 'Missing'); ?></div>
    </div>
    <form method="post" action="">
      <input type="hidden" name="token" value="<?php echo pp_h($token); ?>">
      <input type="hidden" name="action" value="save-settings">
      <details open>
        <summary>انتقال و حذف</summary>
        <div class="check"><label><input type="checkbox" name="remote_delete" value="1" <?php echo (isset($settings['allow_remote_delete']) && $settings['allow_remote_delete'] === '1') ? 'checked' : ''; ?>> حذف از باکت (مقصد) مجاز باشد</label></div>
        <p class="muted">خاموش، پیش‌فرض. در MODE=TEST هیچ حذفی انجام نمی‌شود. امروز و جدیدترین روز تأییدشده همیشه می‌مانند.</p>
        <label for="remote_keep_days">چند روز از امروز به عقب نگه داشته شود</label>
        <input id="remote_keep_days" type="text" name="remote_keep_days" value="<?php echo pp_h(isset($settings['remote_keep_days']) ? $settings['remote_keep_days'] : '0'); ?>">
        <p class="muted">مثلاً ۱۰ یعنی از امروز تا ۱۰ روز قبل می‌ماند و پوشهٔ قدیمی‌تر، بعد از انتقال موفق همان روز، پاک می‌شود. ۲۰ یعنی تا ۲۰ روز قبل. صفر یعنی پاک‌سازی بر اساس سن انجام نمی‌شود و فقط درخواست حذف خود cPanel، برای یک پوشهٔ قدیمی خارج از امروز و جدیدترین روز تأییدشده، بررسی می‌شود.</p>
        <div class="check"><label><input type="checkbox" name="local_delete" value="1" <?php echo (isset($settings['delete_local_after_success']) && $settings['delete_local_after_success'] === '1') ? 'checked' : ''; ?>> حذف از سرور (مبدأ) بعد از تأیید مقصد مجاز باشد</label></div>
        <p class="muted">خاموش، پیش‌فرض. اگر روشن باشد، فایل محلی فقط بعد از آپلود موفق و یک بررسی دوبارهٔ اندازه روی مقصد پاک می‌شود. فایل روی باکت می‌ماند.</p>
      </details>
      <details>
        <summary>گزارش ایمیل</summary>
      <div class="check"><label><input type="checkbox" name="smtp_enabled" value="1" <?php echo (isset($settings['smtp_enabled']) && $settings['smtp_enabled'] === '1') ? 'checked' : ''; ?>> ارسال ایمیل فعال باشد</label></div>
      <div class="check"><label><input type="checkbox" name="notify_on_success" value="1" <?php echo (!isset($settings['notify_on_success']) || $settings['notify_on_success'] !== '0') ? 'checked' : ''; ?>> برای بکاپ موفق ایمیل برود</label></div>
      <div class="check"><label><input type="checkbox" name="notify_on_failure" value="1" <?php echo (!isset($settings['notify_on_failure']) || $settings['notify_on_failure'] !== '0') ? 'checked' : ''; ?>> برای بکاپ ناموفق ایمیل برود</label></div>
      <p class="muted">هشدار هم همراه نتیجهٔ موفق فرستاده می‌شود. هر ایمیل فهرست کاربران موفق و ناموفق همان روز را دارد. اتصال فقط با STARTTLS است.</p>
      <label for="report_to">ایمیل دریافت گزارش</label>
      <input id="report_to" type="text" name="report_to" value="<?php echo pp_h(isset($settings['report_to']) ? $settings['report_to'] : ''); ?>">
      <label for="report_from">ایمیل فرستنده</label>
      <input id="report_from" type="text" name="report_from" value="<?php echo pp_h(isset($settings['report_from']) ? $settings['report_from'] : ''); ?>">
      <p class="muted">اگر فرستنده خالی باشد، از نام کاربری SMTP استفاده می‌شود و آن نام باید خودش ایمیل باشد.</p>
      <label for="smtp_host">سرور SMTP</label>
      <input id="smtp_host" type="text" name="smtp_host" value="<?php echo pp_h(isset($settings['smtp_host']) ? $settings['smtp_host'] : ''); ?>">
      <label for="smtp_port">پورت</label>
      <input id="smtp_port" type="text" name="smtp_port" value="<?php echo pp_h(isset($settings['smtp_port']) ? $settings['smtp_port'] : '587'); ?>">
      <label for="smtp_user">نام کاربری SMTP</label>
      <input id="smtp_user" type="text" name="smtp_user" value="<?php echo pp_h(isset($settings['smtp_user']) ? $settings['smtp_user'] : ''); ?>" autocomplete="off">
      <label for="smtp_password">رمز SMTP</label>
      <input id="smtp_password" type="password" name="smtp_password" value="" autocomplete="new-password">
      <p class="muted"><?php echo (isset($settings['smtp_password_set']) && $settings['smtp_password_set'] === '1') ? 'رمز ذخیره شده است. خالی بگذارید تا همان بماند.' : 'هنوز رمزی ذخیره نشده است.'; ?></p>
      </details>
      <details>
        <summary>هشدارها</summary>
      <label for="alert_remote_bytes">آستانه حجم ثبت‌شده مقصد (بایت، صفر یعنی خاموش)</label>
      <input id="alert_remote_bytes" type="text" name="alert_remote_bytes" value="<?php echo pp_h(isset($settings['alert_remote_bytes']) ? $settings['alert_remote_bytes'] : '0'); ?>">
      <div class="check"><label><input type="checkbox" name="webhook_enabled" value="1" <?php echo (isset($settings['webhook_enabled']) && $settings['webhook_enabled'] === '1') ? 'checked' : ''; ?>> وب‌هوک فعال باشد</label></div>
      <label for="webhook_url">آدرس وب‌هوک</label>
      <input id="webhook_url" type="text" name="webhook_url" value="<?php echo pp_h(isset($settings['webhook_url']) ? $settings['webhook_url'] : ''); ?>">
      <label for="webhook_secret">رمز وب‌هوک</label>
      <input id="webhook_secret" type="password" name="webhook_secret" value="" autocomplete="new-password">
      <p class="muted"><?php echo (isset($settings['webhook_secret_set']) && $settings['webhook_secret_set'] === '1') ? 'Configured' : 'Missing'; ?></p>
      <div class="check"><label><input type="checkbox" name="telegram_enabled" value="1" <?php echo (isset($settings['telegram_enabled']) && $settings['telegram_enabled'] === '1') ? 'checked' : ''; ?>> تلگرام فعال باشد</label></div>
      <label for="telegram_chat">شناسه گفتگوی تلگرام</label>
      <input id="telegram_chat" type="text" name="telegram_chat" value="<?php echo pp_h(isset($settings['telegram_chat']) ? $settings['telegram_chat'] : ''); ?>">
      <label for="telegram_token">توکن ربات تلگرام</label>
      <input id="telegram_token" type="password" name="telegram_token" value="" autocomplete="new-password">
      <p class="muted"><?php echo (isset($settings['telegram_token_set']) && $settings['telegram_token_set'] === '1') ? 'Configured' : 'Missing'; ?></p>
      <div class="check"><label><input type="checkbox" name="slack_enabled" value="1" <?php echo (isset($settings['slack_enabled']) && $settings['slack_enabled'] === '1') ? 'checked' : ''; ?>> اسلک فعال باشد</label></div>
      <label for="slack_webhook">آدرس وب‌هوک اسلک</label>
      <input id="slack_webhook" type="password" name="slack_webhook" value="" autocomplete="new-password">
      <p class="muted"><?php echo (isset($settings['slack_webhook_set']) && $settings['slack_webhook_set'] === '1') ? 'Configured' : 'Missing'; ?></p>
      </details>
      <button type="submit">ذخیره تنظیمات</button>
    </form>
    <form method="post" action="">
      <input type="hidden" name="token" value="<?php echo pp_h($token); ?>">
      <input type="hidden" name="action" value="test-connection">
      <button type="submit">آزمایش اتصال</button>
    </form>
    <?php if ($audit) { ?>
      <h3>تغییرات تنظیمات</h3>
      <table>
        <tr><th>زمان</th><th>کاربر</th><th>کلیدها</th></tr>
        <?php foreach ($audit as $item) {
            if (!is_array($item)) {
                continue;
            }
            $keys = isset($item['keys']) && is_array($item['keys']) ? implode(', ', $item['keys']) : '';
        ?>
          <tr>
            <td><?php echo pp_h(isset($item['ts']) ? $item['ts'] : ''); ?></td>
            <td><?php echo pp_h(isset($item['user']) ? $item['user'] : ''); ?></td>
            <td><?php echo pp_h($keys); ?></td>
          </tr>
        <?php } ?>
      </table>
    <?php } ?>
    <p class="muted">ایمیل بعد از پایان انتقال پوشه system فرستاده می‌شود. هشدار تکراری تا ۱۲ ساعت دوباره نمی‌رود، مگر شدت آن بالاتر باشد. cron جداگانه‌ای برای آپلود لازم نیست.</p>
  <?php } ?>
<?php } ?>
</div>
<?php WHM::footer(); ?>
