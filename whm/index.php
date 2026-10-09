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
    header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'self'");
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
            $webhookSecret = isset($_POST['webhook_secret']) ? (string)$_POST['webhook_secret'] : '';
            $telegramToken = isset($_POST['telegram_token']) ? (string)$_POST['telegram_token'] : '';
            $slackUrl = isset($_POST['slack_webhook']) ? (string)$_POST['slack_webhook'] : '';
            if ($webhook === null || $chat === null || $bytes === null
                || strlen($webhookSecret) > 300 || strlen($telegramToken) > 200 || strlen($slackUrl) > 300
                || preg_match('/[\r\n\0]/', $webhookSecret . $telegramToken . $slackUrl)) {
                pp_flash_set('bad', 'یکی از مقدارهای فرم نامعتبر است.');
                pp_redirect('settings', '');
            }
            $payload['audit_user'] = pp_audit_user();
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
            pp_flash_set('ok', 'تنظیمات ایمیل ذخیره شد. رمزها در این صفحه نشان داده نمی‌شوند.');
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
?>
<div class="pp" dir="rtl">
<style>
.pp{font-family:Tahoma,Arial,sans-serif;color:#1f2933}
.pp-tabs{display:flex;gap:8px;margin:0 0 16px}
.pp-tabs a{padding:8px 14px;border-radius:6px;text-decoration:none;background:#eef2f5;color:#1f2933}
.pp-tabs a.on{background:#1d4f91;color:#fff}
.pp-note,.pp-bad{padding:10px 12px;border-radius:6px;margin:0 0 14px}
.pp-note{background:#e8f5e9}
.pp-bad{background:#ffebee}
.pp-card{background:#f7f9fb;border:1px solid #d9e2ec;border-radius:8px;padding:12px 14px;margin:0 0 14px}
.pp-ok{background:#e8f5e9}.pp-warning{background:#fff8e1}.pp-problem,.pp-critical{background:#ffebee}.pp-empty{background:#f4f6f8}
.pp table{border-collapse:collapse;width:100%;background:#fff}
.pp th,.pp td{border:1px solid #d9e2ec;padding:8px;text-align:right;vertical-align:top}
.pp tr.bad td{background:#fff5f5}
.pp tr.good td{background:#f6fff6}
.pp label{display:block;margin:10px 0 4px}
.pp input[type=text],.pp input[type=password]{width:100%;max-width:420px;padding:6px 8px}
.pp .check{margin:8px 0}
.pp button{margin-top:12px;padding:8px 14px}
.pp .days a{margin-left:8px}
.pp .muted{color:#52606d;font-size:13px}
</style>
<div class="pp-tabs">
  <a class="<?php echo $tab === 'dashboard' ? 'on' : ''; ?>" href="?tab=dashboard">داشبورد</a>
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
?>
  <div class="pp-card">
    <strong>روز <?php echo pp_h(is_array($dashboard) && !empty($dashboard['date']) ? $dashboard['date'] : '-'); ?></strong>
    <div class="muted">
      وضعیت اجرا: <?php echo pp_h(is_array($dashboard) && isset($dashboard['run_state']) && $dashboard['run_state'] !== '' ? $dashboard['run_state'] : '-'); ?>
      — system: <?php echo pp_h(is_array($dashboard) && isset($dashboard['system_status']) ? $dashboard['system_status'] : '-'); ?>
      <?php if (is_array($dashboard) && isset($dashboard['report_status']) && $dashboard['report_status'] !== '') { ?>
        — آخرین گزارش: <?php echo pp_h($dashboard['report_status']); ?>
      <?php } ?>
    </div>
    <div class="muted">
      شروع: <?php echo pp_h(is_array($dashboard) && !empty($dashboard['started']) ? $dashboard['started'] : '-'); ?>
      — پایان: <?php echo pp_h(is_array($dashboard) && !empty($dashboard['finished']) ? $dashboard['finished'] : '-'); ?>
    </div>
    <div class="muted">
      حساب‌های سالم: <?php echo (int)(is_array($dashboard) && isset($dashboard['accounts_ok']) ? $dashboard['accounts_ok'] : 0); ?>
      — ناموفق: <?php echo (int)(is_array($dashboard) && isset($dashboard['accounts_failed']) ? $dashboard['accounts_failed'] : 0); ?>
    </div>
    <div class="muted">
      حجم ثبت‌شده در مقصد: <?php echo pp_h(pp_size(is_array($dashboard) && isset($dashboard['remote_bytes']) ? $dashboard['remote_bytes'] : 0)); ?>
      — فضای محلی آزادشده: <?php echo pp_h(pp_size(is_array($dashboard) && isset($dashboard['local_freed_bytes']) ? $dashboard['local_freed_bytes'] : 0)); ?>
    </div>
    <div class="muted">
      آخرین تأیید: <?php echo pp_h(is_array($dashboard) && !empty($dashboard['verification']['last']) ? $dashboard['verification']['last'] : '-'); ?>
      — sha256: <?php echo (int)(isset($levels['sha256']) ? $levels['sha256'] : 0); ?>
      — etag: <?php echo (int)(isset($levels['etag']) ? $levels['etag'] : 0); ?>
      — size-only: <?php echo (int)(isset($levels['size-only']) ? $levels['size-only'] : 0); ?>
    </div>
  </div>
  <?php if (!$warnings) { ?>
    <div class="pp-card">هشداری برای این روز نیست.</div>
  <?php } else { ?>
    <ul>
      <?php foreach ($warnings as $warning) {
          if (!is_array($warning)) {
              continue;
          }
      ?>
        <li><?php echo pp_h(isset($warning['severity']) ? $warning['severity'] : ''); ?>: <?php echo pp_h(isset($warning['text']) ? $warning['text'] : ''); ?></li>
      <?php } ?>
    </ul>
  <?php } ?>
  <table>
    <tr><th>حساب</th><th>وضعیت</th><th>دلیل</th><th></th></tr>
    <?php if (!$failedRows) { ?>
      <tr><td colspan="4">حساب ناموفقی در پایگاه وضعیت نیست.</td></tr>
    <?php } ?>
    <?php foreach ($failedRows as $row) {
        if (!is_array($row) || empty($row['account']) || !preg_match('/\A[A-Za-z0-9._+-]{1,64}\z/', (string)$row['account'])) {
            continue;
        }
    ?>
      <tr class="bad">
        <td><?php echo pp_h($row['account']); ?></td>
        <td><?php echo pp_h(isset($row['status']) ? $row['status'] : ''); ?></td>
        <td><?php echo pp_h(isset($row['reason']) && $row['reason'] !== '' ? $row['reason'] : '-'); ?></td>
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
<?php } elseif ($tab === 'reports') { ?>
  <?php if (is_array($dashboard) && isset($dashboard['mode']) && $dashboard['mode'] === 'TEST') { ?>
    <div class="pp-card">حالت فعلی TEST است. در این حالت فایلی به مقصد فرستاده نمی‌شود.</div>
  <?php } ?>
  <?php if (is_array($sendStatus)) {
      $sendState = $sendStatus['state'];
      $sendClass = ($sendState === 'failed' || $sendState === 'critical') ? 'pp-bad' : 'pp-card';
  ?>
    <div class="<?php echo pp_h($sendClass); ?>">
      <strong>
        <?php if ($sendState === 'running') { ?>
          ارسال دستی در حال انجام است
        <?php } elseif ($sendState === 'failed') { ?>
          ارسال دستی ناتمام ماند
        <?php } elseif ($sendState === 'critical') { ?>
          روز بکاپ ناقص است چون انتقال system تمام نشده
        <?php } else { ?>
          آخرین ارسال دستی تمام شد
        <?php } ?>
      </strong>
      <div class="muted">
        روز: <?php echo pp_h(isset($sendStatus['day']) && $sendStatus['day'] !== '' ? $sendStatus['day'] : '-'); ?>
        — فایل‌ها: <?php echo (int)(isset($sendStatus['total']) ? $sendStatus['total'] : 0); ?>
        — موفق: <?php echo (int)(isset($sendStatus['sent']) ? $sendStatus['sent'] : 0); ?>
        — ناموفق: <?php echo (int)(isset($sendStatus['failed']) ? $sendStatus['failed'] : 0); ?>
        — قبلاً ثبت‌شده: <?php echo (int)(isset($sendStatus['skipped']) ? $sendStatus['skipped'] : 0); ?>
        <?php if (!empty($sendStatus['current'])) { ?>
          — فایل جاری: <?php echo pp_h($sendStatus['current']); ?>
        <?php } ?>
      </div>
      <?php if (!empty($sendStatus['message'])) { ?>
        <div class="muted"><?php echo pp_h($sendStatus['message']); ?></div>
      <?php } ?>
    </div>
  <?php } ?>
  <form method="post" action="">
    <input type="hidden" name="token" value="<?php echo pp_h($token); ?>">
    <input type="hidden" name="action" value="send-remaining">
    <button type="submit">ارسال بکاپ‌های مانده</button>
    <div class="muted">آرشیو کاربران و فایل‌های پوشه system از آخرین روزی که هنوز روی سرور است فرستاده می‌شود. هر فایل حداکثر ۵ بار تلاش می‌شود و این دکمه خودش را تکرار نمی‌کند. اگر حذف فایل محلی روشن باشد، بعد از آپلود موفق همان فایل از سرور پاک می‌شود.</div>
  </form>
  <?php
    $days = array();
    if (is_array($dashboard) && isset($dashboard['days']) && is_array($dashboard['days'])) {
        foreach ($dashboard['days'] as $day) {
            if (is_string($day) && preg_match('/\A\d{4}-\d{2}-\d{2}\z/', $day)) {
                $days[] = $day;
            }
        }
    }
  ?>
  <?php if ($days) { ?>
    <p class="days">روزها:
      <?php foreach ($days as $day) { ?>
        <a href="?tab=reports&amp;date=<?php echo pp_h($day); ?>"><?php echo pp_h($day); ?></a>
      <?php } ?>
    </p>
  <?php } ?>
  <?php if (!$report) { ?>
    <div class="pp-card">هنوز بکاپی در لاگ یا تاریخچه پیدا نشده است.</div>
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
  ?>
    <div class="pp-card pp-<?php echo pp_h(strtolower($status)); ?>">
      <strong>آخرین وضعیت <?php echo pp_h(isset($report['date']) ? $report['date'] : ''); ?>: <?php echo pp_h($statusText[$status]); ?></strong>
      <div class="muted">
        کاربران: <?php echo count($rows); ?>
        — موفق: <?php echo (int)$okCount; ?>
        — ناموفق یا ناتمام: <?php echo (int)$badCount; ?>
        — حجم منتقل‌شده: <?php echo pp_h(pp_size(isset($report['transferred_bytes']) ? $report['transferred_bytes'] : 0)); ?>
      </div>
      <div class="muted">
        شروع: <?php echo pp_h(isset($report['started']) && $report['started'] !== '' ? $report['started'] : '-'); ?>
        — پایان: <?php echo pp_h(isset($report['finished']) && $report['finished'] !== '' ? $report['finished'] : '-'); ?>
        <?php if (!empty($report['checked_at'])) { ?>
          — زمان بررسی: <?php echo pp_h($report['checked_at']); ?>
        <?php } ?>
      </div>
      <div class="muted">
        <?php if ($source === 'log') { ?>
          این جدول از لاگ محلی است و هنوز با مقصد چک نشده.
        <?php } else { ?>
          این جدول از آخرین گزارش ذخیره‌شده است.
        <?php } ?>
      </div>
    </div>
    <form method="post" action="">
      <input type="hidden" name="token" value="<?php echo pp_h($token); ?>">
      <input type="hidden" name="action" value="refresh-report">
      <input type="hidden" name="date" value="<?php echo pp_h(isset($report['date']) ? $report['date'] : ''); ?>">
      <button type="submit">بررسی مجدد مقصد</button>
      <div class="muted">به فضای ابری وصل می‌شود. اگر ایمیل فعال باشد، نتیجهٔ موفق یا ناموفق همین روز فرستاده می‌شود.</div>
    </form>
    <table>
      <tr><th>کاربر</th><th>وضعیت انتقال</th><th>حجم مقصد</th><th>حجم مبدأ</th><th>زمان</th><th>توضیح</th></tr>
      <?php if (!$rows) { ?>
        <tr><td colspan="6">کاربری برای این روز پیدا نشد.</td></tr>
      <?php } ?>
      <?php foreach ($rows as $row) {
          if (!is_array($row)) {
              continue;
          }
          $when = '';
          if (!empty($row['end'])) {
              $when = (string)$row['end'];
          } elseif (!empty($row['start'])) {
              $when = (string)$row['start'];
          }
      ?>
        <tr class="<?php echo !empty($row['ok']) ? 'good' : 'bad'; ?>">
          <td><?php echo pp_h(isset($row['user']) ? $row['user'] : ''); ?></td>
          <td><?php echo pp_h(isset($row['state']) ? $row['state'] : ''); ?></td>
          <td><?php echo pp_h(pp_size(isset($row['remote_size']) ? $row['remote_size'] : 0)); ?></td>
          <td><?php echo pp_h(pp_size(isset($row['local_size']) ? $row['local_size'] : 0)); ?></td>
          <td><?php echo pp_h($when !== '' ? $when : '-'); ?></td>
          <td><?php echo pp_h(isset($row['reason']) && $row['reason'] !== '' ? $row['reason'] : '-'); ?></td>
        </tr>
      <?php } ?>
    </table>
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
