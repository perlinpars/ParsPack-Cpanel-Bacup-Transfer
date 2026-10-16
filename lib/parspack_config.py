#!/usr/bin/env python3
"""Validate PARSPACK configuration and own the security-sensitive decisions.

Shell scripts call this program instead of sourcing parspack.conf. Secrets are
written only to the private mc config file. They are not printed and they are
not placed on a command line.
"""

from __future__ import print_function

import base64
import binascii
import datetime
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse

import parspack_state

ROOT = os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
CONF_PATH = os.path.join(ROOT, "config", "parspack.conf")

SHELL_KEYS = (
    "MODE",
    "PARSPACK_ENDPOINT",
    "PARSPACK_BUCKET",
    "PARSPACK_PREFIX",
    "MC_BIN",
    "MC_CONFIG_DIR",
    "MC_ALIAS",
    "PART_SIZE",
    "PARALLEL_PARTS",
    "RETRY_COUNT",
    "RETRY_DELAY_SECONDS",
    "RETRY_DELAY_CAP_SECONDS",
    "MIN_FILE_AGE_MINUTES",
    "STABILITY_CHECK_SECONDS",
    "UPLOAD_LOCK_WAIT_SECONDS",
    "SYSTEM_UPLOAD_LOCK_WAIT_SECONDS",
    "DELETE_LOCAL_DELAY",
    "SYSTEM_SET_STABLE_SECONDS",
    "SYSTEM_DEADLINE_SECONDS",
    "REMOTE_EXISTS_POLICY",
    "DELETE_LOCAL_AFTER_SUCCESS",
    "ALLOW_REMOTE_DELETE",
    "REMOTE_KEEP_DAYS",
    "LOG_DIR",
    "STATE_DIR",
    "REPORT_DIR",
    "LOCAL_READ_ROOTS",
    "LOCAL_WRITE_ROOTS",
    "TRANSPORT_TRACE",
    "TRANSPORT_AUTH_DISABLED",
)

SECRET_KEYS = (
    "PARSPACK_ACCESS_KEY",
    "PARSPACK_SECRET_KEY",
    "TRANSPORT_PASSWORD",
    "SMTP_PASSWORD",
)

SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._+-]+\Z")
PART_SIZE_RE = re.compile(r"\A[1-9][0-9]{0,4}(?:KiB|MiB|GiB)\Z")
EMAIL_RE = re.compile(r"\A[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+\Z")
HOST_RE = re.compile(r"\A(?:[A-Za-z0-9.-]+|\d{1,3}(?:\.\d{1,3}){3})\Z")
SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")
DATE_SEGMENT_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
VALIDATION_NAME_RE = re.compile(r"\Avalidate\.tmp-[0-9]+-[0-9]+\.txt\Z")
# cPanel reads custom-transport listings as ls -l: type, perms, links, owner,
# group, size, a stamp, then the name. This is the shape its parsers accept.
LS_LINE_RE = re.compile(
    r"\A[d-][rwxsStT-]{9} \d+ \S+ \S+ \d+ [A-Z][a-z]{2} +\d{1,2} (?:\d{2}:\d{2}|\d{4}) .+\Z"
)

SENSITIVE_PREFIXES = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/run",
    "/root",
)

MC_PREFIXES = ("/usr/local/", "/usr/bin/", "/usr/sbin/", "/bin/", "/opt/")


class ConfigError(Exception):
    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super(ConfigError, self).__init__("; ".join(self.errors))


class PathRefusal(ConfigError):
    """A local or remote path was rejected. Transport helpers exit 1.

    Invalid configuration still raises ConfigError and exits 2. Both are
    non-zero, and both print the reason on stderr.
    """


def as_path_refusal(func):
    try:
        return func()
    except PathRefusal:
        raise
    except ConfigError as exc:
        raise PathRefusal(exc.errors)


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def require_root():
    if not is_root():
        raise ConfigError("run this command as root")


def parse_value(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        end = raw.find('"', 1)
        if end < 0:
            raise ConfigError("unterminated double quote in configuration")
        return raw[1:end]
    if raw.startswith("'"):
        end = raw.find("'", 1)
        if end < 0:
            raise ConfigError("unterminated single quote in configuration")
        return raw[1:end]
    return raw.split("#", 1)[0].strip()


def load_raw(path):
    data = {}
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if lineno == 1:
                line = line.lstrip("\ufeff")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                raise ConfigError("invalid configuration syntax on line %d" % lineno)
            key, raw_value = stripped.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Z0-9_]+", key):
                raise ConfigError("invalid configuration key on line %d" % lineno)
            if key in data:
                raise ConfigError("duplicate configuration key %s" % key)
            data[key] = parse_value(raw_value)
    return data


def clean_absolute(path, label):
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise ConfigError("%s must be an absolute path" % label)
    if re.search(r"[\x00-\x1f\x7f]", path):
        raise ConfigError("%s contains a control character" % label)
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ConfigError("%s must not contain .." % label)
        parts.append(part)
    return "/" + "/".join(parts)


def under(path, root):
    return path == root or path.startswith(root + os.sep)


def parse_int(value, minimum, maximum, label, errors):
    if not re.fullmatch(r"0|[1-9][0-9]{0,8}", str(value)):
        errors.append("%s must be an integer without leading zeros" % label)
        return None
    number = int(value)
    if number < minimum or number > maximum:
        errors.append("%s must be between %d and %d" % (label, minimum, maximum))
        return None
    return str(number)


def secret_dirs():
    return (
        os.path.join(ROOT, "config"),
        os.path.join(ROOT, "mc-config"),
    )


def contains_secret_tree(path):
    for secret in secret_dirs():
        if path == secret or under(secret, path) or under(path, secret):
            return True
    return False


def is_sensitive_path(path):
    parts = [part for part in path.split("/") if part]
    if ".ssh" in parts or "parspack.conf" in parts:
        return True
    for prefix in SENSITIVE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    for secret in secret_dirs():
        if path == secret or path.startswith(secret + "/"):
            return True
    return False


def sanitize_remote(path, allow_empty):
    if path is None:
        path = ""
    if not isinstance(path, str):
        raise ConfigError("remote path is invalid")
    if re.search(r"[\x00-\x1f\x7f\\]", path):
        raise ConfigError("remote path contains an illegal character")
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ConfigError("remote path traversal was rejected")
        if part.startswith("-") or not SEGMENT_RE.fullmatch(part):
            raise ConfigError("remote path contains an unsupported segment")
        parts.append(part)
    if not parts and not allow_empty:
        raise ConfigError("empty remote path was rejected")
    relative = "/".join(parts)
    if len(relative) > 900:
        raise ConfigError("remote path is too long")
    return relative


def validate_endpoint(value, errors):
    if not value.startswith("https://") or re.search(r"[\x00-\x20\x7f]", value):
        errors.append("PARSPACK_ENDPOINT must be an https URL")
        return
    host_part = value[len("https://"):]
    if "/" in host_part.rstrip("/"):
        errors.append("PARSPACK_ENDPOINT must not include a path")
        return
    host_part = host_part.rstrip("/")
    if "@" in host_part or host_part == "" or ".." in host_part:
        errors.append("PARSPACK_ENDPOINT must not include credentials or an invalid host")
        return
    host = host_part
    port = None
    if host_part.count(":") == 1:
        host, port = host_part.split(":", 1)
    elif host_part.count(":") > 1:
        errors.append("PARSPACK_ENDPOINT host is invalid")
        return
    reserved = host == "example.invalid" or host.endswith(".example.invalid") or host.endswith(".invalid")
    if reserved or not HOST_RE.fullmatch(host):
        errors.append("PARSPACK_ENDPOINT host must be configured")
        return
    if port is not None and (not port.isdigit() or not 1 <= int(port) <= 65535):
        errors.append("PARSPACK_ENDPOINT port is invalid")


def validate_secret(value, label, minimum, errors):
    if not isinstance(value, str) or len(value) < minimum:
        errors.append("%s must be at least %d characters" % (label, minimum))
        return
    if value == "CHANGE_ME" or re.search(r"[\x00-\x20\x7f'\"\\]", value):
        errors.append("%s contains an unsupported character or is still CHANGE_ME" % label)


def validate_mc_bin(path, errors):
    try:
        cleaned = clean_absolute(path, "MC_BIN")
    except ConfigError as exc:
        errors.extend(exc.errors)
        return None
    allowed_prefixes = MC_PREFIXES + (os.path.join(ROOT, "tests") + os.sep,)
    if not any(cleaned.startswith(prefix) for prefix in allowed_prefixes):
        errors.append("MC_BIN must live under /usr, /bin, /opt, or this project's tests directory")
        return None
    if os.path.islink(cleaned):
        real_target = os.path.realpath(cleaned)
        if not os.path.isfile(real_target):
            errors.append("MC_BIN is not a regular file")
            return None
    elif not os.path.isfile(cleaned):
        errors.append("MC_BIN is not a regular file")
        return None
    real = os.path.realpath(cleaned)
    if not any(real.startswith(prefix) for prefix in allowed_prefixes):
        errors.append("MC_BIN resolves outside the allowed directories")
        return None
    mode = os.stat(real).st_mode
    if not os.access(real, os.X_OK) or mode & 0o022:
        errors.append("MC_BIN must be executable and not writable by group or others")
        return None
    return real


def directory_under_root(path, label, errors):
    try:
        cleaned = clean_absolute(path, label)
    except ConfigError as exc:
        errors.extend(exc.errors)
        return None
    if not under(cleaned, ROOT):
        errors.append("%s must stay inside %s" % (label, ROOT))
        return None
    return cleaned


def validate_roots(value, label, errors):
    if not isinstance(value, str) or value == "":
        errors.append("%s must list at least one directory" % label)
        return ""
    cleaned = []
    for item in value.split(":"):
        try:
            path = clean_absolute(item, label)
        except ConfigError as exc:
            errors.extend(exc.errors)
            continue
        if contains_secret_tree(path) or is_sensitive_path(path):
            errors.append("%s contains a protected directory" % label)
            continue
        cleaned.append(path)
    return ":".join(cleaned)


def validate_core(raw):
    errors = []
    conf = {}
    conf["MODE"] = raw.get("MODE", "")
    if conf["MODE"] not in ("TEST", "PRODUCTION"):
        errors.append("MODE must be TEST or PRODUCTION")

    validate_endpoint(raw.get("PARSPACK_ENDPOINT", ""), errors)
    conf["PARSPACK_ENDPOINT"] = raw.get("PARSPACK_ENDPOINT", "").rstrip("/")

    for key, minimum in (("PARSPACK_ACCESS_KEY", 8), ("PARSPACK_SECRET_KEY", 8), ("TRANSPORT_PASSWORD", 12)):
        validate_secret(raw.get(key, ""), key, minimum, errors)
        conf[key] = raw.get(key, "")
    if conf["TRANSPORT_PASSWORD"] and conf["TRANSPORT_PASSWORD"] in (conf["PARSPACK_ACCESS_KEY"], conf["PARSPACK_SECRET_KEY"]):
        errors.append("TRANSPORT_PASSWORD must be different from the S3 keys")

    bucket = raw.get("PARSPACK_BUCKET", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,61}[A-Za-z0-9]", bucket) or ".." in bucket:
        errors.append("PARSPACK_BUCKET is not a valid bucket name")
    conf["PARSPACK_BUCKET"] = bucket

    try:
        conf["PARSPACK_PREFIX"] = sanitize_remote(raw.get("PARSPACK_PREFIX", ""), allow_empty=True)
    except ConfigError as exc:
        errors.extend(exc.errors)
        conf["PARSPACK_PREFIX"] = ""

    conf["MC_BIN"] = validate_mc_bin(raw.get("MC_BIN", ""), errors) or raw.get("MC_BIN", "")
    alias = raw.get("MC_ALIAS", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", alias):
        errors.append("MC_ALIAS must contain only letters, digits, underscore, and hyphen")
    conf["MC_ALIAS"] = alias

    defaults = {
        "MC_CONFIG_DIR": os.path.join(ROOT, "mc-config"),
        "LOG_DIR": os.path.join(ROOT, "logs"),
        "STATE_DIR": os.path.join(ROOT, "state"),
        "REPORT_DIR": os.path.join(ROOT, "reports"),
        "PART_SIZE": "256MiB",
        "PARALLEL_PARTS": "4",
        "RETRY_COUNT": "5",
        "RETRY_DELAY_SECONDS": "30",
        "RETRY_DELAY_CAP_SECONDS": "300",
        "MIN_FILE_AGE_MINUTES": "15",
        "STABILITY_CHECK_SECONDS": "60",
        "UPLOAD_LOCK_WAIT_SECONDS": "120",
        "SYSTEM_UPLOAD_LOCK_WAIT_SECONDS": "600",
        "DELETE_LOCAL_DELAY": "0",
        "SYSTEM_SET_STABLE_SECONDS": "120",
        "SYSTEM_DEADLINE_SECONDS": "21600",
        "REMOTE_EXISTS_POLICY": "VERIFY",
        "DELETE_LOCAL_AFTER_SUCCESS": "0",
        "ALLOW_REMOTE_DELETE": "0",
        "REMOTE_KEEP_DAYS": "0",
        "LOCAL_READ_ROOTS": "/backup:/var/cpanel",
        "LOCAL_WRITE_ROOTS": "/backup:/var/cpanel",
    }
    for key, default in defaults.items():
        if key not in raw or raw.get(key) == "":
            raw[key] = default

    if not PART_SIZE_RE.fullmatch(raw.get("PART_SIZE", "")):
        errors.append("PART_SIZE must look like 256MiB")
    conf["PART_SIZE"] = raw.get("PART_SIZE", "")

    number_rules = (
        ("PARALLEL_PARTS", 1, 16),
        ("RETRY_COUNT", 1, 20),
        ("RETRY_DELAY_SECONDS", 0, 3600),
        ("RETRY_DELAY_CAP_SECONDS", 0, 3600),
        ("MIN_FILE_AGE_MINUTES", 0, 1440),
        ("STABILITY_CHECK_SECONDS", 0, 600),
        ("UPLOAD_LOCK_WAIT_SECONDS", 1, 240),
        ("SYSTEM_UPLOAD_LOCK_WAIT_SECONDS", 1, 3600),
        ("SYSTEM_SET_STABLE_SECONDS", 0, 3600),
        ("SYSTEM_DEADLINE_SECONDS", 60, 86400),
        ("REMOTE_KEEP_DAYS", 0, 3650),
    )
    for key, low, high in number_rules:
        parsed = parse_int(raw.get(key, ""), low, high, key, errors)
        conf[key] = parsed if parsed is not None else ""

    if raw.get("REMOTE_EXISTS_POLICY") not in ("VERIFY", "SKIP", "REPLACE"):
        errors.append("REMOTE_EXISTS_POLICY must be VERIFY, SKIP, or REPLACE")
    conf["REMOTE_EXISTS_POLICY"] = raw.get("REMOTE_EXISTS_POLICY", "")

    for key in ("DELETE_LOCAL_AFTER_SUCCESS", "ALLOW_REMOTE_DELETE", "TRANSPORT_TRACE", "TRANSPORT_AUTH_DISABLED"):
        if key not in raw or raw.get(key) == "":
            raw[key] = "0"
        if raw.get(key) not in ("0", "1"):
            errors.append("%s must be 0 or 1" % key)
        conf[key] = raw.get(key, "0")

    delay = raw.get("DELETE_LOCAL_DELAY", "")
    if delay not in ("0", "10m", "1h", "manual"):
        errors.append("DELETE_LOCAL_DELAY must be 0, 10m, 1h, or manual")
    conf["DELETE_LOCAL_DELAY"] = delay if delay in ("0", "10m", "1h", "manual") else ""

    for key, default in (("NOTIFY_ON_SUCCESS", "1"), ("NOTIFY_ON_FAILURE", "1")):
        if key not in raw or raw.get(key, "") == "":
            raw[key] = default
        if raw.get(key) not in ("0", "1"):
            errors.append("%s must be 0 or 1" % key)
        conf[key] = raw.get(key, default)

    validate_alert_raw(raw, errors)
    conf["ALERT_REMOTE_BYTES"] = raw.get("ALERT_REMOTE_BYTES", "0")
    for key in ("WEBHOOK_ENABLED", "TELEGRAM_ENABLED", "SLACK_ENABLED"):
        conf[key] = raw.get(key, "0")
    for key in ("WEBHOOK_URL", "WEBHOOK_SECRET", "TELEGRAM_CHAT", "TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL"):
        conf[key] = raw.get(key, "")

    for key in ("MC_CONFIG_DIR", "LOG_DIR", "STATE_DIR", "REPORT_DIR"):
        conf[key] = directory_under_root(raw.get(key, ""), key, errors)

    conf["LOCAL_READ_ROOTS"] = validate_roots(raw.get("LOCAL_READ_ROOTS", ""), "LOCAL_READ_ROOTS", errors)
    conf["LOCAL_WRITE_ROOTS"] = validate_roots(raw.get("LOCAL_WRITE_ROOTS", ""), "LOCAL_WRITE_ROOTS", errors)
    conf["ROOT"] = ROOT

    if errors:
        raise ConfigError(errors)
    return conf


def smtp_settings(raw):
    if raw.get("SMTP_ENABLED", "0") != "1":
        return None
    errors = []
    if raw.get("SMTP_TLS", "1") != "1":
        errors.append("SMTP_TLS must be 1; cleartext SMTP is refused")
    host = raw.get("SMTP_HOST", "")
    if not HOST_RE.fullmatch(host or ""):
        errors.append("SMTP_HOST is invalid")
    port = parse_int(raw.get("SMTP_PORT", "587"), 1, 65535, "SMTP_PORT", errors)
    user = raw.get("SMTP_USER", "")
    password = raw.get("SMTP_PASSWORD", "")
    if user and (re.search(r"[\x00-\x20\x7f]", user) or password == "" or re.search(r"[\r\n\0]", password)):
        errors.append("SMTP_USER or SMTP_PASSWORD is invalid")
    mail_to = raw.get("REPORT_TO", "")
    mail_from = raw.get("REPORT_FROM", "") or user
    if not EMAIL_RE.fullmatch(mail_to or ""):
        errors.append("REPORT_TO must be one bare email address")
    if not EMAIL_RE.fullmatch(mail_from or ""):
        errors.append("REPORT_FROM must be one bare email address")
    if errors:
        raise ConfigError(errors)
    return {
        "host": host,
        "port": int(port),
        "user": user,
        "password": password,
        "mail_to": mail_to,
        "mail_from": mail_from,
    }


def notification_wanted(raw, status):
    """True when this report result should be emailed.

    OK and WARNING are successful runs (a warning still transferred). PROBLEM
    and CRITICAL are failed runs. Missing flags default to on, so both results
    mail once SMTP itself is enabled.
    """
    success = raw.get("NOTIFY_ON_SUCCESS", "1") != "0"
    failure = raw.get("NOTIFY_ON_FAILURE", "1") != "0"
    if status in ("PROBLEM", "CRITICAL"):
        return failure
    if status in ("OK", "WARNING"):
        return success
    return False


MAIL_UPDATE_KEYS = (
    "SMTP_ENABLED",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "REPORT_FROM",
    "REPORT_TO",
    "NOTIFY_ON_SUCCESS",
    "NOTIFY_ON_FAILURE",
    "SMTP_TLS",
)

SETTINGS_KEYS = MAIL_UPDATE_KEYS + (
    "ALLOW_REMOTE_DELETE",
    "REMOTE_KEEP_DAYS",
    "DELETE_LOCAL_AFTER_SUCCESS",
    "ALERT_REMOTE_BYTES",
    "WEBHOOK_ENABLED",
    "WEBHOOK_URL",
    "WEBHOOK_SECRET",
    "TELEGRAM_ENABLED",
    "TELEGRAM_CHAT",
    "TELEGRAM_BOT_TOKEN",
    "SLACK_ENABLED",
    "SLACK_WEBHOOK_URL",
)


def quote_conf_value(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f\"'\\]", value):
        raise ConfigError("configuration value contains an unsupported character")
    return '"%s"' % value


def rewrite_conf(text, updates, allowed=None):
    allowed = set(MAIL_UPDATE_KEYS if allowed is None else allowed)
    seen = set()
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates and key in allowed:
            out.append("%s=%s" % (key, quote_conf_value(updates[key])))
            seen.add(key)
        else:
            out.append(line)
    missing = [key for key in updates if key in allowed and key not in seen]
    if missing:
        if out and out[-1] != "":
            out.append("")
        out.append("# Notification settings written from WHM.")
        for key in missing:
            out.append("%s=%s" % (key, quote_conf_value(updates[key])))
    return "\n".join(out) + "\n"


def public_https_url(value):
    """Return the URL only when it is https, has a host, and has no userinfo."""
    if not isinstance(value, str) or not value or len(value) > 300:
        return ""
    if re.search(r"[\x00-\x20\x7f]", value):
        return ""
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.startswith("127.") or host == "::1":
        return ""
    return value


def validate_alert_raw(raw, errors):
    for key, default in (
        ("WEBHOOK_ENABLED", "0"),
        ("TELEGRAM_ENABLED", "0"),
        ("SLACK_ENABLED", "0"),
    ):
        if key not in raw or raw.get(key, "") == "":
            raw[key] = default
        if raw.get(key) not in ("0", "1"):
            errors.append("%s must be 0 or 1" % key)
    if "ALERT_REMOTE_BYTES" not in raw or raw.get("ALERT_REMOTE_BYTES", "") == "":
        raw["ALERT_REMOTE_BYTES"] = "0"
    if not re.fullmatch(r"\d{1,15}", raw.get("ALERT_REMOTE_BYTES", "")):
        errors.append("ALERT_REMOTE_BYTES must be a non-negative integer")
    for key in ("WEBHOOK_URL", "WEBHOOK_SECRET", "TELEGRAM_CHAT", "TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL"):
        if key not in raw:
            raw[key] = ""
        value = raw.get(key, "")
        if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f\"'\\]", value) or len(value) > 300:
            errors.append("%s is invalid" % key)
            raw[key] = ""
    if raw.get("WEBHOOK_URL") and not public_https_url(raw.get("WEBHOOK_URL")):
        errors.append("WEBHOOK_URL must be an https URL without credentials")
    if raw.get("SLACK_WEBHOOK_URL") and not public_https_url(raw.get("SLACK_WEBHOOK_URL")):
        errors.append("SLACK_WEBHOOK_URL must be an https URL without credentials")
    chat = raw.get("TELEGRAM_CHAT", "")
    if chat and not re.fullmatch(r"-?\d{1,20}", chat):
        errors.append("TELEGRAM_CHAT must be a numeric chat id")
    token = raw.get("TELEGRAM_BOT_TOKEN", "")
    if token and not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{20,120}", token):
        errors.append("TELEGRAM_BOT_TOKEN is invalid")


def mail_settings_view(raw):
    return {
        "smtp_enabled": "1" if raw.get("SMTP_ENABLED") == "1" else "0",
        "smtp_host": raw.get("SMTP_HOST", ""),
        "smtp_port": raw.get("SMTP_PORT", "") or "587",
        "smtp_user": raw.get("SMTP_USER", ""),
        "smtp_password_set": "1" if raw.get("SMTP_PASSWORD", "") else "0",
        "report_from": raw.get("REPORT_FROM", ""),
        "report_to": raw.get("REPORT_TO", ""),
        "notify_on_success": "0" if raw.get("NOTIFY_ON_SUCCESS") == "0" else "1",
        "notify_on_failure": "0" if raw.get("NOTIFY_ON_FAILURE") == "0" else "1",
        "mode": raw.get("MODE", ""),
        "endpoint": raw.get("PARSPACK_ENDPOINT", ""),
        "bucket": raw.get("PARSPACK_BUCKET", ""),
        "prefix": raw.get("PARSPACK_PREFIX", ""),
        "alert_remote_bytes": raw.get("ALERT_REMOTE_BYTES", "") or "0",
        "webhook_enabled": "1" if raw.get("WEBHOOK_ENABLED") == "1" else "0",
        "webhook_url": raw.get("WEBHOOK_URL", "") if raw.get("WEBHOOK_ENABLED") == "1" else "",
        "webhook_secret_set": "1" if raw.get("WEBHOOK_SECRET", "") else "0",
        "telegram_enabled": "1" if raw.get("TELEGRAM_ENABLED") == "1" else "0",
        "telegram_chat": raw.get("TELEGRAM_CHAT", ""),
        "telegram_token_set": "1" if raw.get("TELEGRAM_BOT_TOKEN", "") else "0",
        "slack_enabled": "1" if raw.get("SLACK_ENABLED") == "1" else "0",
        "slack_webhook_set": "1" if raw.get("SLACK_WEBHOOK_URL", "") else "0",
        "access_key": "Configured" if raw.get("PARSPACK_ACCESS_KEY") else "Missing",
        "secret_key": "Configured" if raw.get("PARSPACK_SECRET_KEY") else "Missing",
        "transport_password": "Configured" if raw.get("TRANSPORT_PASSWORD") else "Missing",
        "allow_remote_delete": "1" if raw.get("ALLOW_REMOTE_DELETE") == "1" else "0",
        "remote_keep_days": raw.get("REMOTE_KEEP_DAYS", "") or "0",
        "delete_local_after_success": "1" if raw.get("DELETE_LOCAL_AFTER_SUCCESS") == "1" else "0",
    }


def _mail_flag(payload, current, name, default):
    if name in payload:
        value = payload[name]
    else:
        value = current.get(name, default)
    if not isinstance(value, str) or value not in ("0", "1"):
        raise ConfigError("%s must be 0 or 1" % name)
    return value


def _optional_text(payload, current, name, check, label):
    if name in payload:
        value = payload[name]
    else:
        value = current.get(name, "")
    if not isinstance(value, str):
        raise ConfigError("%s is invalid" % label)
    value = value.strip()
    if value and not check(value):
        raise ConfigError("%s is invalid" % label)
    return value


def validate_mail_raw(raw):
    errors = []
    for key, default in (
        ("SMTP_ENABLED", "0"),
        ("NOTIFY_ON_SUCCESS", "1"),
        ("NOTIFY_ON_FAILURE", "1"),
        ("SMTP_TLS", "1"),
    ):
        if raw.get(key, default) not in ("0", "1"):
            errors.append("%s must be 0 or 1" % key)
    if raw.get("SMTP_TLS", "1") != "1":
        errors.append("SMTP_TLS must be 1; cleartext SMTP is refused")
    if raw.get("SMTP_ENABLED", "0") == "1":
        if errors:
            raise ConfigError(errors)
        smtp_settings(raw)
        return
    host = raw.get("SMTP_HOST", "")
    if host and not HOST_RE.fullmatch(host):
        errors.append("SMTP_HOST is invalid")
    if raw.get("SMTP_PORT", ""):
        parse_int(raw.get("SMTP_PORT", ""), 1, 65535, "SMTP_PORT", errors)
    for key in ("REPORT_TO", "REPORT_FROM"):
        value = raw.get(key, "")
        if value and not EMAIL_RE.fullmatch(value):
            errors.append("%s must be one bare email address" % key)
    user = raw.get("SMTP_USER", "")
    if user and re.search(r"[\x00-\x20\x7f]", user):
        errors.append("SMTP_USER is invalid")
    if errors:
        raise ConfigError(errors)


def update_mail_settings(payload):
    require_root()
    if not isinstance(payload, dict):
        raise ConfigError("mail settings must be a JSON object")
    allowed = set(MAIL_UPDATE_KEYS) - {"SMTP_TLS"}
    if any(key not in allowed for key in payload):
        raise ConfigError("unsupported mail setting")
    if not os.path.isfile(CONF_PATH) or os.path.islink(CONF_PATH):
        raise ConfigError("configuration file is missing")
    current = load_config_file()
    updates = {
        "SMTP_ENABLED": _mail_flag(payload, current, "SMTP_ENABLED", "0"),
        "NOTIFY_ON_SUCCESS": _mail_flag(payload, current, "NOTIFY_ON_SUCCESS", "1"),
        "NOTIFY_ON_FAILURE": _mail_flag(payload, current, "NOTIFY_ON_FAILURE", "1"),
        "SMTP_TLS": "1",
        "SMTP_HOST": _optional_text(payload, current, "SMTP_HOST", HOST_RE.fullmatch, "SMTP_HOST"),
        "SMTP_PORT": _optional_text(
            payload,
            current,
            "SMTP_PORT",
            lambda value: bool(re.fullmatch(r"[1-9][0-9]{0,4}", value) and 1 <= int(value) <= 65535),
            "SMTP_PORT",
        ) or "587",
        "SMTP_USER": _optional_text(
            payload,
            current,
            "SMTP_USER",
            lambda value: len(value) <= 200 and not re.search(r"[\x00-\x20\x7f]", value),
            "SMTP_USER",
        ),
        "REPORT_FROM": _optional_text(payload, current, "REPORT_FROM", EMAIL_RE.fullmatch, "REPORT_FROM"),
        "REPORT_TO": _optional_text(payload, current, "REPORT_TO", EMAIL_RE.fullmatch, "REPORT_TO"),
    }
    if "SMTP_PASSWORD" in payload and payload["SMTP_PASSWORD"] != "":
        password = payload["SMTP_PASSWORD"]
        if (
            not isinstance(password, str)
            or not 8 <= len(password) <= 200
            or re.search(r"[\r\n\0'\"\\]", password)
        ):
            raise ConfigError("SMTP_PASSWORD is invalid")
        updates["SMTP_PASSWORD"] = password
    with open(CONF_PATH, "r", encoding="utf-8") as handle:
        original = handle.read()
    rewritten = rewrite_conf(original, updates)
    directory = os.path.dirname(CONF_PATH)
    handle = tempfile.NamedTemporaryFile(dir=directory, prefix=".conf.", suffix=".tmp", delete=False)
    try:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(rewritten.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        candidate = load_raw(handle.name)
        validate_mail_raw(candidate)
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, CONF_PATH)
        if is_root():
            os.chown(CONF_PATH, 0, 0)
        os.chmod(CONF_PATH, 0o600)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    if stat.S_IMODE(os.stat(CONF_PATH).st_mode) & 0o077:
        raise ConfigError("configuration file is not private")


def _stored_secret(value, label):
    if not isinstance(value, str) or not 8 <= len(value) <= 300 or re.search(r"[\r\n\0'\"\\]", value):
        raise ConfigError("%s is invalid" % label)
    return value


def update_settings(payload):
    """Rewrite only the settings whitelist. Blank secrets keep the stored value."""
    require_root()
    if not isinstance(payload, dict):
        raise ConfigError("settings must be a JSON object")
    user = str(payload.get("audit_user") or "unknown")
    data = {key: value for key, value in payload.items() if key != "audit_user"}
    allowed = set(SETTINGS_KEYS) - {"SMTP_TLS"}
    if any(key not in allowed for key in data):
        raise ConfigError("unsupported setting")
    if not os.path.isfile(CONF_PATH) or os.path.islink(CONF_PATH):
        raise ConfigError("configuration file is missing")
    current = load_config_file()
    updates = {
        "ALLOW_REMOTE_DELETE": _mail_flag(data, current, "ALLOW_REMOTE_DELETE", "0"),
        "REMOTE_KEEP_DAYS": _optional_text(
            data,
            current,
            "REMOTE_KEEP_DAYS",
            lambda value: bool(re.fullmatch(r"0|[1-9][0-9]{0,3}", value) and int(value) <= 3650),
            "REMOTE_KEEP_DAYS",
        ) or "0",
        "DELETE_LOCAL_AFTER_SUCCESS": _mail_flag(data, current, "DELETE_LOCAL_AFTER_SUCCESS", "0"),
        "SMTP_ENABLED": _mail_flag(data, current, "SMTP_ENABLED", "0"),
        "NOTIFY_ON_SUCCESS": _mail_flag(data, current, "NOTIFY_ON_SUCCESS", "1"),
        "NOTIFY_ON_FAILURE": _mail_flag(data, current, "NOTIFY_ON_FAILURE", "1"),
        "SMTP_TLS": "1",
        "SMTP_HOST": _optional_text(data, current, "SMTP_HOST", HOST_RE.fullmatch, "SMTP_HOST"),
        "SMTP_PORT": _optional_text(
            data,
            current,
            "SMTP_PORT",
            lambda value: bool(re.fullmatch(r"[1-9][0-9]{0,4}", value) and 1 <= int(value) <= 65535),
            "SMTP_PORT",
        ) or "587",
        "SMTP_USER": _optional_text(
            data,
            current,
            "SMTP_USER",
            lambda value: len(value) <= 200 and not re.search(r"[\x00-\x20\x7f]", value),
            "SMTP_USER",
        ),
        "REPORT_FROM": _optional_text(data, current, "REPORT_FROM", EMAIL_RE.fullmatch, "REPORT_FROM"),
        "REPORT_TO": _optional_text(data, current, "REPORT_TO", EMAIL_RE.fullmatch, "REPORT_TO"),
        "WEBHOOK_ENABLED": _mail_flag(data, current, "WEBHOOK_ENABLED", "0"),
        "TELEGRAM_ENABLED": _mail_flag(data, current, "TELEGRAM_ENABLED", "0"),
        "SLACK_ENABLED": _mail_flag(data, current, "SLACK_ENABLED", "0"),
        "ALERT_REMOTE_BYTES": _optional_text(
            data,
            current,
            "ALERT_REMOTE_BYTES",
            lambda value: bool(re.fullmatch(r"\d{1,15}", value)),
            "ALERT_REMOTE_BYTES",
        ) or "0",
        "WEBHOOK_URL": _optional_text(data, current, "WEBHOOK_URL", public_https_url, "WEBHOOK_URL"),
        "TELEGRAM_CHAT": _optional_text(
            data,
            current,
            "TELEGRAM_CHAT",
            lambda value: bool(re.fullmatch(r"-?\d{1,20}", value)),
            "TELEGRAM_CHAT",
        ),
    }
    if "SMTP_PASSWORD" in data and data["SMTP_PASSWORD"] != "":
        updates["SMTP_PASSWORD"] = _stored_secret(data["SMTP_PASSWORD"], "SMTP_PASSWORD")
    if "WEBHOOK_SECRET" in data and data["WEBHOOK_SECRET"] != "":
        updates["WEBHOOK_SECRET"] = _stored_secret(data["WEBHOOK_SECRET"], "WEBHOOK_SECRET")
    if "TELEGRAM_BOT_TOKEN" in data and data["TELEGRAM_BOT_TOKEN"] != "":
        token = data["TELEGRAM_BOT_TOKEN"]
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{20,120}", token):
            raise ConfigError("TELEGRAM_BOT_TOKEN is invalid")
        updates["TELEGRAM_BOT_TOKEN"] = token
    if "SLACK_WEBHOOK_URL" in data and data["SLACK_WEBHOOK_URL"] != "":
        if not public_https_url(data["SLACK_WEBHOOK_URL"]):
            raise ConfigError("SLACK_WEBHOOK_URL is invalid")
        updates["SLACK_WEBHOOK_URL"] = data["SLACK_WEBHOOK_URL"]
    with open(CONF_PATH, "r", encoding="utf-8") as handle:
        original = handle.read()
    rewritten = rewrite_conf(original, updates, allowed=SETTINGS_KEYS)
    directory = os.path.dirname(CONF_PATH)
    handle = tempfile.NamedTemporaryFile(dir=directory, prefix=".conf.", suffix=".tmp", delete=False)
    try:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(rewritten.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        candidate = load_raw(handle.name)
        errors = []
        validate_alert_raw(candidate, errors)
        if errors:
            raise ConfigError(errors[0])
        validate_mail_raw(candidate)
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, CONF_PATH)
        if is_root():
            os.chown(CONF_PATH, 0, 0)
        os.chmod(CONF_PATH, 0o600)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    if stat.S_IMODE(os.stat(CONF_PATH).st_mode) & 0o077:
        raise ConfigError("configuration file is not private")
    state_errors = []
    state_dir = directory_under_root(current.get("STATE_DIR", "state"), "STATE_DIR", state_errors)
    if state_errors:
        state_dir = os.path.join(ROOT, "state")
    import parspack_ui
    parspack_ui.append_audit(os.path.join(state_dir, "audit.log"), user, sorted(data))


def load_config_file():
    if not os.path.isfile(CONF_PATH):
        raise ConfigError("missing %s" % CONF_PATH)
    if os.path.islink(CONF_PATH):
        raise ConfigError("configuration file must not be a symlink")
    info = os.lstat(CONF_PATH)
    if is_root():
        if info.st_uid != 0:
            raise ConfigError("configuration file must be owned by root")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(CONF_PATH, 0o600)
            print("warning: tightened configuration file permissions to 600", file=sys.stderr)
    elif stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigError("configuration file permissions are too open")
    return load_raw(CONF_PATH)


def load_valid():
    return validate_core(load_config_file())


def ensure_private_dir(path):
    cleaned = clean_absolute(path, "directory")
    if not under(cleaned, ROOT):
        raise ConfigError("directory escapes the project root")
    os.makedirs(cleaned, mode=0o700, exist_ok=True)
    real = os.path.realpath(cleaned)
    if os.path.islink(cleaned) or not under(real, ROOT):
        raise ConfigError("directory escapes the project root")
    os.chmod(real, 0o700)
    if hasattr(os, "chown") and is_root():
        os.chown(real, 0, 0)
    info = os.stat(real)
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigError("could not keep %s private" % cleaned)
    return real


def prepare_runtime(conf):
    require_root()
    for key in ("LOG_DIR", "STATE_DIR", "REPORT_DIR", "MC_CONFIG_DIR"):
        ensure_private_dir(conf[key])
    ensure_private_dir(os.path.join(ROOT, "run"))
    ensure_private_dir(os.path.join(ROOT, "tmp"))
    ensure_private_dir(os.path.join(ROOT, "config"))
    ensure_private_dir(os.path.join(conf["STATE_DIR"], "history"))
    os.chmod(CONF_PATH, 0o600)


def render_shell(conf):
    lines = []
    for key in SHELL_KEYS:
        if key in SECRET_KEYS:
            raise ConfigError("refusing to export a secret")
        lines.append("%s=%s" % (key, shlex.quote(str(conf[key]))))
    return "\n".join(lines) + "\n"


def write_mc_config(conf):
    require_root()
    directory = ensure_private_dir(conf["MC_CONFIG_DIR"])
    destination = os.path.join(directory, "config.json")
    if os.path.islink(destination):
        os.unlink(destination)
    payload = {
        "version": "10",
        "aliases": {
            conf["MC_ALIAS"]: {
                "url": conf["PARSPACK_ENDPOINT"],
                "accessKey": conf["PARSPACK_ACCESS_KEY"],
                "secretKey": conf["PARSPACK_SECRET_KEY"],
                "api": "S3v4",
                "path": "auto",
                "license": "",
                "apiKey": "",
                "secretToken": "",
            }
        },
    }
    handle = tempfile.NamedTemporaryFile(dir=directory, prefix=".config.", suffix=".tmp", delete=False)
    try:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(payload, indent=2).encode("utf-8"))
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, destination)
        if hasattr(os, "chown") and is_root():
            os.chown(destination, 0, 0)
        os.chmod(destination, 0o600)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    if os.path.islink(destination) or stat.S_IMODE(os.stat(destination).st_mode) & 0o077:
        raise ConfigError("mc configuration file is not private")


def is_validation_name(path):
    return VALIDATION_NAME_RE.fullmatch(path or "") is not None


def remote_cwd_base(cwd):
    """Return the remote directory carried from chdir.

    An absolute path that exists locally is cPanel's local working directory,
    not a remote prefix. `/` and an empty value mean the prefix root.
    """
    if cwd is None:
        return ""
    cwd = str(cwd).strip()
    if cwd in ("", "/"):
        return ""
    if cwd.startswith("/") and os.path.isdir(cwd) and not os.path.islink(cwd):
        return ""
    return sanitize_remote(cwd, allow_empty=True)


def _date_in_path(path):
    for part in str(path or "").split("/"):
        if DATE_SEGMENT_RE.fullmatch(part):
            return part
    return ""


def compose_remote(cwd, path, local_hint="", dated=False):
    """Join cPanel's remote directory with this command's path.

    Backup objects keep the Gregorian YYYY-MM-DD directory cPanel already put
    in the remote path, in the directory printed by chdir, or in the local
    backup path. The transport does not invent a date from the clock, so a
    server at 23:50 cannot land the object in the next UTC day. A validation
    file stays at the prefix root.
    """
    raw = "" if path is None else str(path).strip()
    if raw in ("", "/"):
        relative = ""
    else:
        relative = sanitize_remote(raw, allow_empty=False)
    if is_validation_name(relative):
        return relative
    base = remote_cwd_base(cwd)
    if relative and base and (relative == base or relative.startswith(base + "/")):
        joined = relative
    elif relative and base:
        joined = base + "/" + relative
    else:
        joined = relative or base
    if is_validation_name(joined):
        return joined
    if dated and not _date_in_path(joined):
        day = _date_in_path(local_hint)
        if day:
            joined = day + ("/" + joined if joined else "")
    if not joined:
        raise ConfigError("empty remote path was rejected")
    return sanitize_remote(joined, allow_empty=False)


def under_prefix(prefix, key):
    """True only for key itself or a child of prefix, never for a longer name."""
    bound = str(prefix).rstrip("/") + "/"
    return key == bound[:-1] or str(key).startswith(bound)


def object_target(conf, relative, allow_empty=False, require_child=False):
    alias = conf["MC_ALIAS"]
    bucket = conf["PARSPACK_BUCKET"]
    if "/" in alias or "/" in bucket or alias.startswith("-") or bucket.startswith("-"):
        raise ConfigError("alias or bucket is invalid")
    prefix = sanitize_remote(conf.get("PARSPACK_PREFIX", ""), allow_empty=True)
    relative = sanitize_remote(relative, allow_empty=allow_empty and not require_child)
    if require_child and relative == "":
        raise ConfigError("refusing to operate on the bucket root or the bare prefix")
    parts = [alias, bucket]
    if prefix:
        parts.append(prefix)
    if relative:
        parts.append(relative)
    target = "/".join(parts)
    if len(target) > 1024 or target.startswith("-"):
        raise ConfigError("remote target is invalid")
    return target


def split_roots(value):
    return [item for item in value.split(":") if item]


def dir_trusted(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        return False
    if info.st_uid != 0:
        return False
    if info.st_mode & 0o022:
        return False
    return True


def trusted_root_for(path, configured_roots):
    matches = []
    extra = os.environ.get("PARSPACK_LOCAL_DIR", "")
    roots = list(configured_roots)
    if extra:
        roots.append(extra)
    for root in roots:
        try:
            cleaned = clean_absolute(root, "local root")
        except ConfigError:
            continue
        if not os.path.isdir(cleaned) or os.path.islink(cleaned):
            continue
        real = os.path.realpath(cleaned)
        if is_sensitive_path(real) or contains_secret_tree(real) or not dir_trusted(real):
            continue
        if under(path, real):
            matches.append(real)
    if not matches:
        return None
    return max(matches, key=len)


def assert_directory_chain(start, root):
    current = start
    seen = set()
    while True:
        if current in seen:
            raise ConfigError("local directory chain is invalid")
        seen.add(current)
        if not dir_trusted(current):
            raise ConfigError("local directory must be owned by root and not writable by group or others")
        if current == root:
            return
        parent = os.path.dirname(current)
        if parent == current:
            raise ConfigError("local directory chain escaped the allowed root")
        current = parent


def safe_leaf_name(name):
    return bool(name) and name not in (".", "..") and not name.startswith("-") and bool(SEGMENT_RE.fullmatch(name))


def resolve_local_path(path, cwd):
    if not isinstance(path, str) or re.search(r"[\x00-\x1f\x7f]", path):
        raise ConfigError("local path contains an illegal character")
    if path.startswith("/"):
        return path
    if path == "" or path.startswith("-") or "\\" in path or ".." in path.split("/"):
        raise ConfigError("relative local path is invalid")
    if not isinstance(cwd, str) or not cwd.startswith("/") or re.search(r"[\x00-\x1f\x7f]", cwd):
        raise ConfigError("relative local path without a valid directory")
    if os.path.islink(cwd) or not os.path.isdir(cwd):
        raise ConfigError("relative local path without a valid directory")
    return cwd.rstrip("/") + "/" + path.lstrip("/")


def check_local(kind, path, conf, cwd=None):
    if kind not in ("read", "write"):
        raise ConfigError("local check kind is invalid")
    path = resolve_local_path(path, cwd)
    if not isinstance(path, str) or re.search(r"[\x00-\x1f\x7f]", path):
        raise ConfigError("local path contains an illegal character")
    try:
        requested = clean_absolute(path, "local path")
    except ConfigError:
        raise
    if os.path.islink(requested):
        raise ConfigError("local path must not be a symlink")
    roots = split_roots(conf["LOCAL_READ_ROOTS"] if kind == "read" else conf["LOCAL_WRITE_ROOTS"])
    if kind == "read":
        if not os.path.isfile(requested):
            raise ConfigError("local file is not a regular file")
        real = os.path.realpath(requested)
        if os.path.islink(real) or not stat.S_ISREG(os.lstat(real).st_mode):
            raise ConfigError("local file is not a regular file")
        info = os.lstat(real)
        if info.st_uid != 0:
            raise ConfigError("local file must be owned by root")
        if info.st_size <= 0:
            raise ConfigError("local file is empty")
        if is_sensitive_path(real):
            raise ConfigError("local file is in a protected path")
        root = trusted_root_for(real, roots)
        if root is None:
            raise ConfigError("local file is outside the allowed root-owned directories")
        assert_directory_chain(os.path.dirname(real), root)
        # cPanel may hard-link account/system archives under the backup roots.
        # Allow extra links only after the path is already inside a trusted
        # root-owned tree. Symlinks remain refused above.
        # Open the path we already resolved. mc later opens that same path
        # again; this inode check only covers the check itself.
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            again = os.lstat(real)
            if (opened.st_dev, opened.st_ino) != (again.st_dev, again.st_ino):
                raise ConfigError("local file changed while it was being checked")
            if not stat.S_ISREG(opened.st_mode) or opened.st_size <= 0:
                raise ConfigError("local file is not a regular file")
            if opened.st_uid != 0:
                raise ConfigError("local file must be owned by root")
        finally:
            os.close(fd)
        return real

    parent = os.path.dirname(requested)
    name = os.path.basename(requested)
    if not safe_leaf_name(name) or not os.path.isdir(parent):
        raise ConfigError("local destination is invalid")
    real_parent = os.path.realpath(parent)
    real = os.path.join(real_parent, name)
    if is_sensitive_path(real):
        raise ConfigError("local destination is in a protected path")
    root = trusted_root_for(real, roots)
    if root is None:
        raise ConfigError("local destination is outside the allowed root-owned directories")
    assert_directory_chain(real_parent, root)
    if os.path.lexists(real):
        if os.path.islink(real) or not os.path.isfile(real):
            raise ConfigError("local destination must be a regular file")
        if os.lstat(real).st_uid != 0:
            raise ConfigError("local destination must be owned by root")
    return real


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def delete_local(path, expected_sha, conf):
    require_root()
    if conf["MODE"] == "TEST" or conf["DELETE_LOCAL_AFTER_SUCCESS"] != "1":
        raise ConfigError("local deletion is disabled")
    if not SHA_RE.fullmatch(expected_sha):
        raise ConfigError("checksum is invalid")
    real = check_local("read", path, conf)
    if sha256_file(real) != expected_sha:
        raise ConfigError("local file changed after the verified upload; it was kept")
    if not hasattr(os, "O_NOFOLLOW"):
        raise ConfigError("this system cannot delete files without following symlinks")
    fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
    os.close(fd)
    os.remove(real)


def tighten_file(path, conf):
    real = check_local("write", path, conf)
    if not hasattr(os, "O_NOFOLLOW"):
        raise ConfigError("this system cannot change file mode without following symlinks")
    fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


STORAGE_FULL_MARKERS = (
    "quota",
    "disk full",
    "nospace",
    "no space",
    "insufficient",
    "storagereached",
    "storage limit",
    "xminiostoragefull",
    "bucketisfull",
    "bucket full",
    "entitytoobig",
    "capacity",
)


def is_storage_full_error(text):
    """True when the transport error means the destination has no room."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in STORAGE_FULL_MARKERS)


def classify_transport_error(text):
    """Map an mc/S3 error to FATAL or RETRYABLE.

    Unknown transport text stays RETRYABLE so a transient message we do not
    recognize still uses the existing attempt limit. Permission, bucket, and
    storage-full errors stop on the first attempt.
    """
    lowered = (text or "").lower()
    if is_storage_full_error(lowered):
        return "FATAL"
    fatal_markers = (
        "403",
        "accessdenied",
        "invalidaccesskeyid",
        "signaturedoesnotmatch",
        "nosuchbucket",
        "invalidbucket",
        "permission denied",
        "invalid path",
        "illegal location",
    )
    for marker in fatal_markers:
        if marker in lowered:
            return "FATAL"
    return "RETRYABLE"


def retry_delay_argv(args):
    """Delay from three decimal arguments. Never reads stdin or tool stdout.

    A leading -- is only a separator. Extra tokens, including a stray "ok"
    from mc, are rejected instead of being parsed as the delay.
    """
    values = list(args)
    if values[:1] == ["--"]:
        values = values[1:]
    if len(values) != 3 or any(re.fullmatch(r"[0-9]{1,6}", item) is None for item in values):
        raise ConfigError("usage: retry-delay ATTEMPT BASE CAP")
    return retry_backoff_seconds(int(values[0]), int(values[1]), int(values[2]))


def retry_backoff_seconds(attempt, base, cap):
    """Exponential delay. attempt is 1 on the first failure. Base 0 stays 0."""
    attempt = int(attempt)
    base = int(base)
    cap = int(cap)
    if base <= 0 or attempt < 1:
        return 0
    delay = base * (2 ** (attempt - 1))
    if cap > 0:
        delay = min(delay, cap)
    spread = delay // 5
    if spread <= 0:
        return delay
    return delay - spread + random.randint(0, spread)


def decide_upload(mode, policy, exists, local_size, remote_size, local_sha, remote_sha):
    if mode not in ("TEST", "PRODUCTION") or policy not in ("VERIFY", "SKIP", "REPLACE"):
        raise ConfigError("upload policy arguments are invalid")
    if str(exists) not in ("0", "1"):
        raise ConfigError("upload existence flag is invalid")
    if not SHA_RE.fullmatch(local_sha):
        raise ConfigError("local checksum is invalid")
    if str(exists) == "0":
        return "upload"
    effective = "VERIFY" if mode == "TEST" and policy == "REPLACE" else policy
    if mode == "TEST" and effective == "REPLACE":
        effective = "VERIFY"
    size_match = str(local_size) == str(remote_size) and str(local_size) not in ("", "-")
    hash_present = bool(remote_sha) and remote_sha != "-"
    hash_match = hash_present and remote_sha.lower() == local_sha.lower()
    if effective == "REPLACE" and mode == "PRODUCTION":
        return "upload"
    if size_match and hash_match:
        return "skip"
    if effective == "VERIFY" and size_match and not hash_present:
        return "verify-content"
    # Same size with a wrong checksum: replace that one object in PRODUCTION.
    # Different sizes still fail unless REPLACE is set.
    if (
        effective == "VERIFY"
        and mode == "PRODUCTION"
        and size_match
        and hash_present
        and not hash_match
    ):
        return "reupload"
    if effective == "SKIP" and size_match and not hash_present:
        return "skip-legacy"
    return "fail"


def parse_stat_document(text):
    text = (text or "").strip()
    if not text:
        raise ConfigError("empty remote stat response")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
        for line in text.splitlines():
            line = line.strip()
            if line:
                data = json.loads(line)
    if isinstance(data, list):
        data = data[-1] if data else {}
    if not isinstance(data, dict):
        raise ConfigError("remote stat response is invalid")
    return data


def metadata_map(data):
    for key in ("metadata", "meta", "userMetadata", "user_metadata"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def find_sha(meta):
    for key, value in meta.items():
        if "sha256" in str(key).lower() and value is not None:
            return str(value).strip().lower()
    return ""


def checksum_sha256(data):
    checksums = data.get("checksum")
    if not isinstance(checksums, dict):
        return ""
    raw = checksums.get("SHA256")
    if raw is None:
        raw = checksums.get("sha256")
    if raw is None:
        return ""
    text = str(raw).strip()
    # Multipart checksums look like "<base64>-<part count>" and are not a
    # whole-file SHA256. Do not strip the suffix and compare it anyway.
    if re.search(r"-\d+\Z", text):
        return ""
    if SHA_RE.fullmatch(text.lower()):
        return text.lower()
    try:
        decoded = base64.b64decode(text)
    except (binascii.Error, ValueError, TypeError):
        return ""
    if len(decoded) != 32:
        return ""
    return binascii.hexlify(decoded).decode("ascii")


def object_etag(data):
    for key in ("etag", "ETag", "eTag"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().strip('"').lower()
    return ""


def part_size_bytes(text):
    match = re.fullmatch(r"([1-9][0-9]{0,4})(KiB|MiB|GiB)", text or "")
    if not match:
        return 0
    scale = {"KiB": 1024, "MiB": 1024 * 1024, "GiB": 1024 * 1024 * 1024}[match.group(2)]
    return int(match.group(1)) * scale


def etag_for_file(path, part_size):
    """S3 ETag for a known part size.

    One part is the MD5 of the whole file. More than one part is the MD5 of
    the concatenated part MD5s, plus the part count. That is not the MD5 of
    the file, and a multipart SHA256 suffix is not used here.
    """
    if part_size <= 0:
        return ""
    parts = []
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(part_size)
            if not chunk:
                break
            parts.append(hashlib.md5(chunk).digest())
    if not parts:
        return ""
    if len(parts) == 1:
        return binascii.hexlify(parts[0]).decode("ascii")
    return "%s-%d" % (hashlib.md5(b"".join(parts)).hexdigest(), len(parts))


def sample_sha256(path, sample_bytes=1024 * 1024):
    """Hash of the size plus the first and last sample. Not a whole-file SHA256."""
    size = os.path.getsize(path)
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with open(path, "rb") as handle:
        digest.update(handle.read(sample_bytes))
        if size > sample_bytes:
            handle.seek(max(0, size - sample_bytes))
            digest.update(handle.read(sample_bytes))
    return digest.hexdigest()


def classify_integrity(local_size, remote_size, local_sha, remote_sha, etag, part_size, local_path, remote_sample=""):
    """Return ok LEVEL, mismatch, or need-content.

    Size is always compared. A SHA256 with a part-count suffix is ignored.
    """
    try:
        if int(local_size) != int(remote_size):
            return "mismatch"
    except (TypeError, ValueError):
        return "mismatch"
    remote_sha = (remote_sha or "").lower()
    if remote_sha in ("", "-"):
        remote_sha = ""
    if SHA_RE.fullmatch(remote_sha or ""):
        if remote_sha == (local_sha or "").lower():
            return "ok sha256"
        return "mismatch"
    etag = (etag or "").strip().strip('"').lower()
    if etag and local_path and part_size_bytes(part_size) > 0 and os.path.isfile(local_path):
        expected = etag_for_file(local_path, part_size_bytes(part_size))
        if expected and expected == etag:
            return "ok etag"
        if re.fullmatch(r"[0-9a-f]{32}(?:-\d+)?", etag):
            return "mismatch"
    if remote_sample and local_path and os.path.isfile(local_path):
        if sample_sha256(local_path) == remote_sample.lower():
            return "ok size-only"
        return "mismatch"
    return "need-content"


def stat_reported_name(data):
    for key in ("name", "key", "Key"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
    return ""


def object_leaf(path):
    return str(path or "").rstrip("/").split("/")[-1]


def stat_names_exact_object(target, text):
    """False when mc stat returned a longer key with the requested name as its prefix."""
    reported = stat_reported_name(parse_stat_document(text))
    if not reported:
        return True
    wanted = object_leaf(target)
    return wanted != "" and object_leaf(reported) == wanted


def exact_stat_line(text, target):
    """Size of the listed object whose name is exactly target, never a longer key."""
    wanted = object_leaf(target)
    if not wanted:
        return "missing"
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        rows = item if isinstance(item, list) else [item]
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("type", "")).lower() == "folder":
                continue
            reported = stat_reported_name(row) or str(row.get("key") or "")
            if object_leaf(reported) != wanted:
                continue
            size = row.get("size")
            if not isinstance(size, int):
                raise ConfigError("remote listing did not include a size")
            sha = find_sha(metadata_map(row))
            if not SHA_RE.fullmatch(sha or ""):
                sha = checksum_sha256(row)
            return "%d\t%s\t%s" % (size, sha or "-", object_etag(row))
    return "missing"


def stat_fields(text):
    data = parse_stat_document(text)
    size = data.get("size")
    if not isinstance(size, int):
        raise ConfigError("remote stat did not include a size")
    sha = find_sha(metadata_map(data))
    if not SHA_RE.fullmatch(sha or ""):
        sha = checksum_sha256(data)
    return "%d\t%s\t%s" % (size, sha or "-", object_etag(data))


def missing_needles(folded):
    needles = (
        "object does not exist",
        "does not exist",
        "nosuchkey",
        "no such key",
        "the specified key does not exist",
    )
    return any(needle in folded for needle in needles)


def is_missing_message(text):
    stripped = (text or "").strip()
    if not stripped:
        return False
    parsed = None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                break
            except ValueError:
                parsed = None
    if isinstance(parsed, dict):
        if parsed.get("status") == "success" or isinstance(parsed.get("size"), int):
            return False
        return missing_needles(json.dumps(parsed).lower())
    return missing_needles(stripped.lower())


def safe_ls_name(name):
    if not isinstance(name, str) or name == "":
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return None
    if "/" in name or "\\" in name or name in (".", ".."):
        return None
    return name


def format_ls(text):
    import datetime
    import locale

    try:
        locale.setlocale(locale.LC_TIME, "C")
    except locale.Error:
        pass
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            raise ConfigError("remote listing contained invalid JSON")
        if isinstance(data, dict) and data.get("status") == "error":
            raise ConfigError("remote listing failed")
        if not isinstance(data, dict):
            continue
        key = data.get("key", "")
        name = safe_ls_name(str(key).rstrip("/").split("/")[-1] if key else "")
        if name is None or name == ".keep":
            if name is None:
                print("skipped an object whose name cannot be listed safely", file=sys.stderr)
            continue
        is_dir = data.get("type") == "folder" or str(key).endswith("/")
        size = data.get("size", 0) or 0
        if not isinstance(size, int):
            size = 0
        stamp = data.get("lastModified", "") or ""
        try:
            when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else datetime.datetime.now()
        except ValueError:
            when = datetime.datetime.now()
        perm = "drwxr-xr-x" if is_dir else "-rw-r--r--"
        # GNU ls uses a space-padded day: "Sep  1 14:05", not "Sep 01".
        shown = "%s %2d %s" % (when.strftime("%b"), when.day, when.strftime("%H:%M"))
        lines.append("%s 1 root root %d %s %s" % (perm, size, shown, name))
    return "\n".join(lines) + ("\n" if lines else "")


def clean_child_env():
    env = os.environ.copy()
    for key in list(SECRET_KEYS) + ["PASSWORD", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV", "PYTHONPATH", "PYTHONHOME"]:
        env.pop(key, None)
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return env


def mc_supports_checksum(mc_bin):
    for command in ("put", "cp"):
        try:
            completed = subprocess.run(
                [mc_bin, command, "--help"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=30,
                env=clean_child_env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "--checksum" in completed.stdout:
            return True
    return False


def command_exists(name):
    return shutil.which(name) is not None


def pending_system_days(state_dir):
    """Dates whose system tree was requested and has not been verified."""
    if not state_dir or os.path.islink(state_dir) or not os.path.isdir(state_dir):
        return []
    days = []
    for name in os.listdir(state_dir):
        match = re.fullmatch(r"system-pending-(\d{4}-\d{2}-\d{2})", name)
        if match is None:
            continue
        path = os.path.join(state_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        days.append(match.group(1))
    return sorted(days)


def retention_failed_days(state_dir):
    """Dates where remote keep-window prune was attempted and did not finish."""
    if not state_dir or os.path.islink(state_dir) or not os.path.isdir(state_dir):
        return []
    days = []
    for name in os.listdir(state_dir):
        match = re.fullmatch(r"retention-failed-(\d{4}-\d{2}-\d{2})", name)
        if match is None:
            continue
        path = os.path.join(state_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        days.append(match.group(1))
    return sorted(days)


def retention_failed_marker(state_dir, day):
    return os.path.join(state_dir, "retention-failed-%s" % day)


def write_retention_failed_marker(state_dir, day):
    """Mark that age prune failed after a verified system day. Mode 0600."""
    if not DATE_SEGMENT_RE.fullmatch(day or ""):
        raise ConfigError("backup date must look like YYYY-MM-DD")
    ensure_private_dir(state_dir)
    path = retention_failed_marker(state_dir, day)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("day=%s\n" % day)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def clear_retention_failed_marker(state_dir, day):
    if not DATE_SEGMENT_RE.fullmatch(day or ""):
        return False
    path = retention_failed_marker(state_dir, day)
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def doctor():
    require_root()
    raw = load_config_file()
    conf = validate_core(raw)
    prepare_runtime(conf)
    failures = []
    for tool in ("flock", "stat", "find", "sha256sum"):
        if not command_exists(tool):
            failures.append("%s is missing" % tool)
    try:
        import sqlite3
        if sqlite3.sqlite_version_info < (3, 7, 0):
            failures.append("sqlite is too old for WAL mode")
    except ImportError:
        failures.append("python sqlite3 module is missing; the state store cannot be opened")
    if conf["MODE"] == "TEST":
        print("WARNING: MODE=TEST uploads nothing and the transport exits 3. Set MODE=PRODUCTION before a real backup.", file=sys.stderr)
    if conf["TRANSPORT_AUTH_DISABLED"] == "1":
        print("WARNING: TRANSPORT_AUTH_DISABLED=1, so the WHM destination password is not checked.", file=sys.stderr)
    if not mc_supports_checksum(conf["MC_BIN"]):
        failures.append("mc does not support put --checksum, which is required for SHA256 verification")
    for label, roots in (("read", conf["LOCAL_READ_ROOTS"]), ("write", conf["LOCAL_WRITE_ROOTS"])):
        usable = []
        for root in split_roots(roots):
            if os.path.isdir(root) and not os.path.islink(root) and dir_trusted(os.path.realpath(root)):
                usable.append(root)
        if not usable:
            print("WARNING: no usable %s directory exists yet (%s)" % (label, roots), file=sys.stderr)
    try:
        smtp_settings(raw)
    except ConfigError as exc:
        print("WARNING: SMTP reporting is enabled but not usable: %s" % "; ".join(exc.errors), file=sys.stderr)
    pending = pending_system_days(conf["STATE_DIR"])
    for day in pending:
        print("CRITICAL: backup day %s is incomplete; system transfer marker is still present" % day, file=sys.stderr)
    try:
        import parspack_ops
        for line, failed in parspack_ops.doctor_lines(conf):
            print(line)
            if failed:
                failures.append(line)
    except ConfigError:
        raise
    except Exception:
        failures.append("remote probe failed")
    if failures or pending:
        for item in failures:
            print("FAIL: %s" % item, file=sys.stderr)
        return 2
    print("OK: dependencies, permissions, and configuration")
    return 0


def show_config():
    require_root()
    conf = load_valid()
    overwrite = conf["MODE"] == "PRODUCTION" and conf["REMOTE_EXISTS_POLICY"] == "REPLACE"
    local_delete = conf["MODE"] == "PRODUCTION" and conf["DELETE_LOCAL_AFTER_SUCCESS"] == "1"
    remote_delete = conf["MODE"] == "PRODUCTION" and conf["ALLOW_REMOTE_DELETE"] == "1"
    rows = (
        ("MODE", conf["MODE"]),
        ("ENDPOINT", conf["PARSPACK_ENDPOINT"]),
        ("BUCKET", conf["PARSPACK_BUCKET"]),
        ("PREFIX", conf["PARSPACK_PREFIX"] or "(none)"),
        ("MC_BIN", conf["MC_BIN"]),
        ("REMOTE_EXISTS_POLICY", conf["REMOTE_EXISTS_POLICY"]),
        ("OVERWRITE", "allowed" if overwrite else "disabled"),
        ("LOCAL_DELETE", "allowed" if local_delete else "disabled"),
        ("REMOTE_DELETE", "allowed" if remote_delete else "disabled"),
        ("REMOTE_KEEP_DAYS", conf["REMOTE_KEEP_DAYS"]),
        ("TRANSPORT_TRACE", "on" if conf["TRANSPORT_TRACE"] == "1" else "off"),
        ("TRANSPORT_AUTH", "disabled" if conf["TRANSPORT_AUTH_DISABLED"] == "1" else "required"),
        ("LOCAL_READ_ROOTS", conf["LOCAL_READ_ROOTS"]),
        ("LOCAL_WRITE_ROOTS", conf["LOCAL_WRITE_ROOTS"]),
        ("TRANSPORT_PASSWORD", "set"),
        ("ACCESS_KEY", "set"),
        ("SECRET_KEY", "set"),
    )
    for key, value in rows:
        print("%s=%s" % (key, value))
    return 0


def take_operand(args):
    if "--" not in args:
        raise ConfigError("missing -- before the path")
    values = args[args.index("--") + 1:]
    if len(values) != 1:
        raise ConfigError("expected exactly one path")
    return values[0]


def take_check_local(args):
    if not args or args[0] not in ("read", "write"):
        raise ConfigError("usage: check-local read|write [--cwd DIR] -- PATH")
    kind = args[0]
    rest = args[1:]
    cwd = None
    if rest[:1] == ["--cwd"]:
        if len(rest) < 2:
            raise ConfigError("usage: check-local read|write [--cwd DIR] -- PATH")
        cwd = rest[1]
        rest = rest[2:]
    return kind, cwd, take_operand(rest)


def path_is_private_temp(path):
    temp_root = os.path.realpath(os.path.join(ROOT, "tmp"))
    real = os.path.realpath(path)
    return under(real, temp_root) and not os.path.islink(path)


def dispatch(argv):
    if len(argv) < 2:
        print("usage: parspack_config.py COMMAND", file=sys.stderr)
        return 2
    command = argv[1]
    args = argv[2:]
    if command == "doctor":
        return doctor()
    if command == "show-config":
        return show_config()
    if command == "mail-settings":
        require_root()
        sys.stdout.write(json.dumps(mail_settings_view(load_config_file()), ensure_ascii=False) + "\n")
        return 0
    if command == "update-mail":
        require_root()
        blob = sys.stdin.read(8192)
        if sys.stdin.read(1):
            raise ConfigError("mail settings payload is too large")
        try:
            payload = json.loads(blob or "{}")
        except ValueError:
            raise ConfigError("mail settings must be JSON")
        update_mail_settings(payload)
        sys.stdout.write("OK\n")
        return 0
    if command == "update-settings":
        require_root()
        blob = sys.stdin.read(16384)
        if sys.stdin.read(1):
            raise ConfigError("settings payload is too large")
        try:
            payload = json.loads(blob or "{}")
        except ValueError:
            raise ConfigError("settings must be JSON")
        update_settings(payload)
        sys.stdout.write("OK\n")
        return 0
    if command == "prepare-runtime":
        prepare_runtime(load_valid())
        return 0
    if command == "shell-exports":
        sys.stdout.write(render_shell(load_valid()))
        return 0
    if command == "write-mc-config":
        write_mc_config(load_valid())
        return 0
    if command == "get":
        require_root()
        if len(args) != 1:
            raise ConfigError("unsupported key")
        conf = load_valid()
        key = args[0]
        if key == "TRANSPORT_PASSWORD":
            if sys.stdout.isatty():
                raise ConfigError("refusing to print the transport password on a terminal")
            sys.stdout.write(conf["TRANSPORT_PASSWORD"] + "\n")
            return 0
        if key in ("TRANSPORT_TRACE", "TRANSPORT_AUTH_DISABLED"):
            sys.stdout.write(conf[key] + "\n")
            return 0
        raise ConfigError("unsupported key")
    if command == "validation-name":
        return 0 if is_validation_name(take_operand(args)) else 1
    if command == "remote-compose":
        cwd = ""
        hint = ""
        dated = False
        rest = list(args)
        while rest and rest[0] != "--":
            flag = rest.pop(0)
            if flag == "--cwd" and rest:
                cwd = rest.pop(0)
            elif flag == "--dated" and rest and not rest[0].startswith("--"):
                dated = True
                hint = rest.pop(0)
            elif flag == "--dated":
                dated = True
            else:
                raise ConfigError("usage: remote-compose [--cwd DIR] [--dated LOCAL] -- PATH")
        path = take_operand(rest)
        sys.stdout.write(as_path_refusal(lambda: compose_remote(cwd, path, hint, dated)) + "\n")
        return 0
    if command == "object-key":
        conf = load_valid()
        operand = take_operand(args)
        target = as_path_refusal(lambda: object_target(
            conf,
            operand,
            allow_empty="--allow-empty" in args,
            require_child="--require-child" in args,
        ))
        sys.stdout.write(target + "\n")
        return 0
    if command == "check-local":
        kind, cwd, path = take_check_local(args)
        conf = load_valid()
        real = as_path_refusal(lambda: check_local(kind, path, conf, cwd))
        sys.stdout.write(real + "\n")
        return 0
    if command == "sha256":
        require_root()
        conf = load_valid()
        operand = take_operand(args)
        real = as_path_refusal(lambda: check_local("read", operand, conf))
        sys.stdout.write(sha256_file(real) + "\n")
        return 0
    if command == "tighten-file":
        tighten_file(take_operand(args), load_valid())
        return 0
    if command == "delete-local":
        if "--" not in args or len(args[args.index("--") + 1:]) != 2:
            raise ConfigError("usage: delete-local -- PATH SHA256")
        path, digest = args[args.index("--") + 1:]
        delete_local(path, digest, load_valid())
        return 0
    if command == "verify-decision":
        flags = {}
        rest = list(args)
        while rest:
            item = rest.pop(0)
            if not item.startswith("--") or not rest:
                raise ConfigError("usage: verify-decision --local-size N --remote-size N --local-sha HEX --remote-sha HEX --etag ETAG --part-size SIZE --file PATH")
            flags[item[2:]] = rest.pop(0)
        sys.stdout.write(classify_integrity(
            flags.get("local-size", ""),
            flags.get("remote-size", ""),
            flags.get("local-sha", ""),
            flags.get("remote-sha", ""),
            flags.get("etag", ""),
            flags.get("part-size", ""),
            flags.get("file", ""),
            flags.get("remote-sample", ""),
        ) + "\n")
        return 0
    if command == "decide":
        if args and args[0] == "--":
            args = args[1:]
        if len(args) != 7:
            raise ConfigError("decide expects 7 arguments")
        sys.stdout.write(decide_upload(*args) + "\n")
        return 0
    if command == "mirror-needed":
        require_root()
        day = take_operand(args)
        if not DATE_SEGMENT_RE.fullmatch(day or ""):
            raise ConfigError("backup date must look like YYYY-MM-DD")
        conf = load_valid()
        conn = parspack_state.connect(os.path.join(conf["STATE_DIR"], "parspack.db"))
        try:
            row = conn.execute(
                "SELECT run_state FROM backup_days WHERE backup_date = ?",
                (day,),
            ).fetchone()
        finally:
            conn.close()
        state = (row["run_state"] if row else "") or ""
        # partial/complete: do not detach another send-system from account puts.
        # open/system_running/missing: a mirror may still be needed.
        needed = "0" if state in ("partial", "complete") else "1"
        sys.stdout.write(needed + "\n")
        return 0
    if command == "exact-stat":
        sys.stdout.write(exact_stat_line(sys.stdin.read(), take_operand(args)) + "\n")
        return 0
    if command == "stat-fields":
        text = sys.stdin.read()
        if "--" in args:
            target = take_operand(args)
            if not stat_names_exact_object(target, text):
                sys.stdout.write("missing\n")
                return 0
        sys.stdout.write(stat_fields(text) + "\n")
        return 0
    if command == "stat-missing":
        path = take_operand(args)
        if not path_is_private_temp(path):
            raise ConfigError("stat error file is outside the private temp directory")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(4096)
        return 0 if is_missing_message(text) else 1
    if command == "format-ls":
        sys.stdout.write(format_ls(sys.stdin.read()))
        return 0
    if command == "classify-error":
        sys.stdout.write(classify_transport_error(sys.stdin.read()) + "\n")
        return 0
    if command == "storage-full":
        return 0 if is_storage_full_error(sys.stdin.read()) else 1
    if command in ("retention-check", "retention-confirm", "retention-log"):
        require_root()
        import parspack_retention
        conf = load_valid()
        try:
            if command == "retention-check":
                if len(args) != 2 or args[0] not in ("delete", "rmdir"):
                    raise parspack_retention.RetentionError("refused: unsupported delete command")
                parspack_retention.cmd_check(conf, args[1], args[0])
            elif command == "retention-confirm":
                relative = take_operand(args)
                keys = parspack_retention.confirm_listing(relative, sys.stdin.read())
                sys.stdout.write("\n".join(keys) + "\n")
            else:
                relative = take_operand(args)
                day = parspack_retention.authorize(
                    conf,
                    relative,
                    "rmdir" if "/" not in relative else "delete",
                    datetime.date.today().isoformat(),
                    parspack_retention.newest_verified_day(os.path.join(conf["STATE_DIR"], "parspack.db")),
                )
                keys = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
                if not keys:
                    keys = [relative]
                for key in keys:
                    cleaned = sanitize_remote(key, allow_empty=False)
                    if cleaned != day and not cleaned.startswith(day + "/"):
                        raise parspack_retention.RetentionError("refused: path is not a date folder")
                parspack_retention.log_deleted(
                    os.path.join(conf["STATE_DIR"], "parspack.db"), day, keys
                )
        except parspack_retention.RetentionError as exc:
            print(exc.message, file=sys.stderr)
            return 1
        except ConfigError:
            print("refused: path is not a date folder", file=sys.stderr)
            return 1
        return 0
    if command == "retry-delay":
        # argv only. stdin is left unread so a piped mc "ok" cannot change the delay.
        sys.stdout.write(str(retry_delay_argv(args)) + "\n")
        return 0
    if command == "self-test":
        return self_test()
    print("unknown command", file=sys.stderr)
    return 2


def expect_error(func):
    try:
        func()
    except ConfigError:
        return
    raise AssertionError("expected a configuration error")


def self_test():
    assert not (set(SHELL_KEYS) & set(SECRET_KEYS))
    assert parse_value('"TEST"  # comment') == "TEST"
    assert parse_value("'a#b'") == "a#b"
    assert sanitize_remote("foo//bar", allow_empty=False) == "foo/bar"
    assert sanitize_remote("/weekly/file.tar.gz", allow_empty=False) == "weekly/file.tar.gz"
    expect_error(lambda: sanitize_remote("../x", allow_empty=False))
    expect_error(lambda: sanitize_remote("foo/../../etc", allow_empty=False))
    expect_error(lambda: sanitize_remote("foo/-rf", allow_empty=False))
    expect_error(lambda: sanitize_remote("bad name", allow_empty=False))
    expect_error(lambda: sanitize_remote("$(id)", allow_empty=False))
    expect_error(lambda: sanitize_remote("`id`", allow_empty=False))
    expect_error(lambda: sanitize_remote("foo\nbar", allow_empty=False))
    expect_error(lambda: sanitize_remote("café", allow_empty=False))
    expect_error(lambda: sanitize_remote("a" * 901, allow_empty=False))
    expect_error(lambda: sanitize_remote("-rf", allow_empty=False))
    assert classify_transport_error("NoSuchBucket") == "FATAL"
    assert classify_transport_error("403 AccessDenied") == "FATAL"
    assert classify_transport_error("InvalidAccessKeyId") == "FATAL"
    assert classify_transport_error("XMinioStorageFull") == "FATAL"
    assert classify_transport_error("QuotaExceeded: disk full") == "FATAL"
    assert is_storage_full_error("XMinioStorageFull: Please reduce your disk usage")
    assert classify_transport_error("connection reset by peer") == "RETRYABLE"
    assert classify_transport_error("429 SlowDown") == "RETRYABLE"
    assert classify_transport_error("503 Service Unavailable") == "RETRYABLE"
    assert classify_transport_error("temporary failure in name resolution") == "RETRYABLE"
    assert classify_transport_error("unexpected EOF") == "RETRYABLE"
    assert retry_delay_argv(["--", "1", "0", "300"]) == 0
    assert retry_delay_argv(["2", "0", "300"]) == 0
    expect_error(lambda: retry_delay_argv(["--", "1", "ok", "300"]))
    expect_error(lambda: retry_delay_argv(["ok"]))
    expect_error(lambda: retry_delay_argv(["--", "1", "0", "300", "ok"]))
    noisy = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "retry-delay", "--", "1", "0", "300"],
        input=b"ok\nok\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert noisy.returncode == 0 and noisy.stdout.strip() == b"0", noisy.stderr
    poisoned = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "retry-delay", "--", "1", "ok", "300"],
        input=b"1 0 300\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert poisoned.returncode != 0 and b"usage: retry-delay" in poisoned.stderr
    assert retry_backoff_seconds(1, 0, 300) == 0
    assert 24 <= retry_backoff_seconds(1, 30, 300) <= 30
    assert retry_backoff_seconds(4, 30, 40) <= 40
    pending_dir = tempfile.mkdtemp()
    try:
        with open(os.path.join(pending_dir, "system-pending-2026-10-08"), "w", encoding="utf-8") as handle:
            handle.write("date=2026-10-08\n")
        with open(os.path.join(pending_dir, "system-pending-not-a-date"), "w", encoding="utf-8") as handle:
            handle.write("no\n")
        assert pending_system_days(pending_dir) == ["2026-10-08"]
        assert pending_system_days(os.path.join(pending_dir, "missing")) == []
        with open(os.path.join(pending_dir, "retention-failed-2026-10-09"), "w", encoding="utf-8") as handle:
            handle.write("day=2026-10-09\n")
        assert retention_failed_days(pending_dir) == ["2026-10-09"]
        assert clear_retention_failed_marker(pending_dir, "2026-10-09")
        assert retention_failed_days(pending_dir) == []
    finally:
        shutil.rmtree(pending_dir)
    assert sanitize_remote("/", allow_empty=True) == ""

    conf = {
        "MC_ALIAS": "parspack",
        "PARSPACK_BUCKET": "backups",
        "PARSPACK_PREFIX": "cpanel",
    }
    assert object_target(conf, "weekly/file.tar.gz") == "parspack/backups/cpanel/weekly/file.tar.gz"
    assert is_validation_name("validate.tmp-123-456.txt")
    assert not is_validation_name("validate.tmp-foo-foo.txt")
    assert not is_validation_name("dir/validate.tmp-123-456.txt")
    assert not is_validation_name("validate.tmp-123-456.txt/evil")
    assert compose_remote("/", "accounts/prmi.tar.gz") == "accounts/prmi.tar.gz"
    assert compose_remote("2026-09-28", "prmi.tar.gz") == "2026-09-28/prmi.tar.gz"
    assert compose_remote("2026-09-28", "accounts") == "2026-09-28/accounts"
    assert compose_remote("/", "2026-09-26/accounts/prmi.tar.gz", dated=True) == "2026-09-26/accounts/prmi.tar.gz"
    assert compose_remote("/", "prmi.tar.gz", "/backup/2026-09-26/accounts/prmi.tar.gz", True) == "2026-09-26/prmi.tar.gz"
    assert compose_remote("2026-09-28/accounts", "prmi.tar.gz", dated=True) == "2026-09-28/accounts/prmi.tar.gz"
    assert compose_remote("/", "validate.tmp-9-9.txt", dated=True) == "validate.tmp-9-9.txt"
    assert compose_remote("/", "prmi.tar.gz", dated=True) == "prmi.tar.gz"
    assert under_prefix("cpanel/foo", "cpanel/foo/a.tar.gz")
    assert under_prefix("cpanel/foo", "cpanel/foo")
    assert not under_prefix("cpanel/foo", "cpanel/foobar/a.tar.gz")
    expect_error(lambda: object_target(conf, "/", require_child=True))
    expect_error(lambda: object_target(conf, "..", require_child=True))

    digest = "a" * 64
    assert decide_upload("PRODUCTION", "VERIFY", "0", "10", "-", digest, "-") == "upload"
    assert decide_upload("PRODUCTION", "VERIFY", "1", "10", "10", digest, digest) == "skip"
    assert decide_upload("PRODUCTION", "VERIFY", "1", "10", "11", digest, digest) == "fail"
    assert decide_upload("PRODUCTION", "VERIFY", "1", "10", "10", digest, "b" * 64) == "reupload"
    assert decide_upload("TEST", "VERIFY", "1", "10", "10", digest, "b" * 64) == "fail"
    assert decide_upload("PRODUCTION", "VERIFY", "1", "10", "10", digest, "-") == "verify-content"
    assert decide_upload("PRODUCTION", "SKIP", "1", "10", "10", digest, "-") == "skip-legacy"
    assert decide_upload("PRODUCTION", "REPLACE", "1", "10", "11", digest, "-") == "upload"
    assert decide_upload("TEST", "REPLACE", "1", "10", "11", digest, "-") == "fail"
    assert decide_upload("TEST", "REPLACE", "1", "10", "10", digest, digest) == "skip"

    sample_digest = hashlib.sha256(b"parspack").digest()
    encoded = base64.b64encode(sample_digest).decode("ascii")
    stat_body = json.dumps({"size": 8, "checksum": {"SHA256": encoded}})
    assert stat_names_exact_object(
        "parspack/bucket/cpanel/2026-09-28/accounts/nadirisite.tar.gz",
        json.dumps({"name": "nadirisite.tar.gz", "size": 8}),
    )
    assert not stat_names_exact_object(
        "parspack/bucket/cpanel/2026-09-28/accounts/nadirisite.tar.gz",
        json.dumps({"name": "nadirisite.tar.gz.parspack-test", "size": 13}),
    )
    assert stat_names_exact_object("parspack/bucket/cpanel/file.tar.gz", stat_body)
    listing = "\n".join((
        json.dumps({"key": "nadirisite.tar.gz.parspack-test", "type": "file", "size": 13}),
        json.dumps({"key": "nadirisite.tar.gz", "type": "file", "size": 37036}),
    ))
    assert exact_stat_line(
        listing,
        "parspack/bucket/cpanel/2026-09-28/accounts/nadirisite.tar.gz",
    ) == "37036\t-\t"
    assert exact_stat_line(listing, "parspack/bucket/cpanel/2026-09-28/accounts/missing.tar.gz") == "missing"
    assert stat_fields(stat_body) == "8\t%s\t" % sample_digest.hex()
    multipart = json.dumps({"size": 8, "checksum": {"SHA256": encoded + "-24"}})
    assert stat_fields(multipart) == "8\t-\t"
    assert classify_integrity(8, 8, sample_digest.hex(), sample_digest.hex(), "", "256MiB", "") == "ok sha256"
    assert classify_integrity(8, 9, sample_digest.hex(), sample_digest.hex(), "", "256MiB", "") == "mismatch"
    assert classify_integrity(8, 8, sample_digest.hex(), "b" * 64, "", "256MiB", "") == "mismatch"
    assert classify_integrity(8, 8, sample_digest.hex(), "-", "", "256MiB", "") == "need-content"
    assert resolve_local_path("/backup/a.tar.gz", "/ignored") == "/backup/a.tar.gz"
    kept = json.dumps({"key": "accounts/", "type": "folder", "size": 0, "lastModified": "2026-01-01T00:00:00Z"})
    hidden = json.dumps({"key": ".keep", "type": "file", "size": 0, "lastModified": "2026-01-01T00:00:00Z"})
    listing = format_ls(kept + "\n" + hidden)
    assert "accounts" in listing and ".keep" not in listing
    sample_ls = format_ls(json.dumps({
        "key": "backup-123.tar.gz",
        "type": "file",
        "size": 1234,
        "lastModified": "2026-09-01T12:00:00Z",
    }))
    assert sample_ls.endswith(" backup-123.tar.gz\n")
    assert " 1234 " in sample_ls and "Sep  1 " in sample_ls
    assert LS_LINE_RE.fullmatch(sample_ls.strip())
    assert is_missing_message('{"status":"error","error":{"message":"Object does not exist"}}')
    assert not is_missing_message(stat_body)

    sample = {key: "value" for key in SHELL_KEYS}
    sample["MODE"] = "TEST"
    sample["PARSPACK_SECRET_KEY"] = "super-secret-value"
    rendered = render_shell(sample)
    assert "super-secret-value" not in rendered
    assert "MODE=TEST" in rendered
    example = os.path.join(ROOT, "config", "parspack.conf.example")
    if os.path.isfile(example):
        raw = load_raw(example)
        assert raw["MODE"] == "TEST"
        assert raw["PART_SIZE"] == "256MiB"
        assert raw["PARSPACK_PREFIX"] == "cpanel"
        expect_error(lambda: validate_core(raw))
    assert notification_wanted({}, "OK")
    assert notification_wanted({}, "PROBLEM")
    assert notification_wanted({}, "CRITICAL")
    assert notification_wanted({"NOTIFY_ON_SUCCESS": "0"}, "OK") is False
    assert notification_wanted({"NOTIFY_ON_SUCCESS": "0"}, "WARNING") is False
    assert notification_wanted({"NOTIFY_ON_FAILURE": "0"}, "PROBLEM") is False
    assert notification_wanted({"NOTIFY_ON_FAILURE": "0"}, "CRITICAL") is False
    assert notification_wanted({"NOTIFY_ON_FAILURE": "0"}, "OK")
    sample_conf = 'MODE="TEST"\nSMTP_PASSWORD="keep-this-secret"\nREPORT_TO=""\n'
    rewritten = rewrite_conf(sample_conf, {"REPORT_TO": "ops@example.com", "NOTIFY_ON_SUCCESS": "0", "SMTP_TLS": "1"})
    assert 'SMTP_PASSWORD="keep-this-secret"' in rewritten
    assert 'REPORT_TO="ops@example.com"' in rewritten
    assert 'NOTIFY_ON_SUCCESS="0"' in rewritten
    assert 'SMTP_TLS="1"' in rewritten
    assert rewritten.count("keep-this-secret") == 1
    expect_error(lambda: quote_conf_value('bad"value'))
    print("OK")
    return 0


def main(argv=None):
    os.umask(0o077)
    if sys.version_info < (3, 7):
        print("python 3.7 or newer is required", file=sys.stderr)
        return 1
    try:
        return dispatch(sys.argv if argv is None else argv)
    except PathRefusal as exc:
        for item in exc.errors:
            print("config error: %s" % item, file=sys.stderr)
        return 1
    except ConfigError as exc:
        for item in exc.errors:
            print("config error: %s" % item, file=sys.stderr)
        return 2
    except OSError as exc:
        print("error: %s" % exc.__class__.__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
