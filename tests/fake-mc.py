#!/usr/bin/env python3
"""Offline stand-in for the mc commands this plugin uses."""
import base64
import datetime
import hashlib
import json
import os
import shutil
import sys

argv = sys.argv[1:]
if "--version" in argv or argv == ["version"]:
    sys.stdout.write("mc version RELEASE.2026-01-01T00-00-00Z\n")
    sys.exit(0)
if argv[:1] in (["put"], ["cp"]) and "--help" in argv:
    sys.stdout.write("Flags: --checksum\n")
    sys.exit(0)

log_path = os.environ.get("FAKE_ARGV_LOG", "")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(" ".join(argv) + "\n")

store = os.environ.get("FAKE_STORE", "")
if not store:
    sys.stderr.write("fake-mc: FAKE_STORE is not set\n")
    sys.exit(9)

if argv[:1] == ["--config-dir"]:
    argv = argv[2:]
if not argv:
    sys.exit(2)
command, rest = argv[0], argv[1:]
flags = []
positional = []
after_dd = False
index = 0
while index < len(rest):
    item = rest[index]
    if item == "--":
        after_dd = True
    elif not after_dd and item in ("--parallel", "--part-size", "--checksum"):
        index += 1
    elif not after_dd and item.startswith("--"):
        flags.append(item)
    else:
        positional.append(item)
    index += 1


def location(target):
    rest_path = target.split("/", 1)[1] if "/" in target else ""
    return os.path.join(store, rest_path.rstrip("/"))


def missing():
    # Real mc writes this on stderr. Keep a copy on stdout too so a caller
    # that only captured one stream still sees a missing object.
    message = '{"status":"error","error":{"message":"Object does not exist"}}\n'
    sys.stderr.write(message)
    sys.stdout.write(message)
    sys.exit(1)


def describe_file(path, name):
    is_dir = os.path.isdir(path)
    payload = {
        "type": "folder" if is_dir else "file",
        "key": name + ("/" if is_dir else ""),
        "size": 0 if is_dir else os.path.getsize(path),
        "lastModified": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if is_dir:
        return payload
    raw = open(path, "rb").read()
    mode = os.environ.get("FAKE_MC_MODE", "")
    encoded = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")
    if "multipart" in os.path.basename(path) or mode == "multipart-sha":
        encoded += "-2"
    size = len(raw)
    if mode == "mismatch":
        encoded = base64.b64encode(b"\x00" * 32).decode("ascii")
    if mode == "partial":
        size = max(0, size - 1)
    payload["size"] = size
    # After a server-side copy, real S3 often keeps size but drops put SHA256
    # and shows an ETag that is not the original multipart layout.
    if mode == "publish-etag-skew" and not path.endswith(".uploading"):
        payload["etag"] = "0" * 32
    elif mode == "etag-only":
        payload["etag"] = hashlib.md5(raw).hexdigest()
    else:
        payload["checksum"] = {"SHA256": encoded}
        if mode == "sha-and-etag":
            payload["etag"] = hashlib.md5(raw).hexdigest()
    if os.environ.get("FAKE_MC_SHRINK_AFTER_SEEN") == "1":
        seen_dir = os.path.join(store, ".seen")
        os.makedirs(seen_dir, exist_ok=True)
        token = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()
        flag = os.path.join(seen_dir, token)
        if os.path.isfile(flag):
            payload["size"] = max(0, int(payload["size"]) - 1)
        else:
            with open(flag, "w", encoding="utf-8") as handle:
                handle.write("1\n")
    return payload


def entry(path, name):
    return describe_file(path, name)


def note_noise(kind):
    # Opt-in only. A normal put or stat must not print "ok" on either stream.
    if os.environ.get("FAKE_MC_NOISE") == "1" and kind == "put":
        sys.stdout.write("ok\n")


def forced_put_failure():
    raw = os.environ.get("FAKE_MC_FAIL_PUTS", "")
    if not raw.isdigit() or int(raw) <= 0:
        return False
    counter = os.path.join(store, ".fail-puts")
    used = 0
    if os.path.isfile(counter):
        try:
            used = int(open(counter, "r", encoding="utf-8").read().strip() or "0")
        except ValueError:
            used = 0
    if used >= int(raw):
        return False
    with open(counter, "w", encoding="utf-8") as handle:
        handle.write("%d\n" % (used + 1))
    sys.stderr.write("fake-mc: forced put failure\n")
    return True


if command == "put":
    if len(positional) != 2:
        sys.stderr.write("fake-mc: put expects two paths\n")
        sys.exit(2)
    note_noise("put")
    if forced_put_failure():
        sys.exit(1)
    destination = location(positional[1])
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copyfile(positional[0], destination)
    sys.exit(0)
elif command == "cp":
    source_name, dest_name = positional[0], positional[1]
    if dest_name.startswith("/") or (len(dest_name) > 2 and dest_name[1] == ":"):
        shutil.copyfile(location(source_name), dest_name)
    else:
        destination_path = location(dest_name)
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        shutil.copyfile(location(source_name), destination_path)
elif command == "cat":
    path = location(positional[0])
    if not os.path.isfile(path):
        missing()
    with open(path, "rb") as handle:
        sys.stdout.buffer.write(handle.read())
elif command == "stat":
    path = location(positional[0])
    if not os.path.isfile(path):
        missing()
    sys.stdout.write(json.dumps(describe_file(path, os.path.basename(path))) + "\n")
    sys.exit(0)
elif command == "du":
    path = location(positional[0]) if positional else store
    total = 0
    if os.path.isdir(path):
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.isfile(full):
                    total += os.path.getsize(full)
    elif os.path.isfile(path):
        total = os.path.getsize(path)
    sys.stdout.write(json.dumps({"size": total}) + "\n")
    sys.exit(0)
elif command == "ls":
    target = positional[0]
    path = location(target)
    if "--recursive" in flags and os.path.isdir(path):
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, path).replace("\\", "/")
                sys.stdout.write(json.dumps(entry(full, rel)) + "\n")
    elif target.endswith("/"):
        if not os.path.isdir(path):
            missing()
        for name in sorted(os.listdir(path)):
            sys.stdout.write(json.dumps(entry(os.path.join(path, name), name)) + "\n")
    elif os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            sys.stdout.write(json.dumps(entry(os.path.join(path, name), name)) + "\n")
    elif os.path.isfile(path):
        sys.stdout.write(json.dumps(entry(path, os.path.basename(path))) + "\n")
    else:
        missing()
elif command == "mv":
    source_path = location(positional[0])
    destination_path = location(positional[1])
    if not os.path.isfile(source_path):
        sys.exit(1)
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    os.replace(source_path, destination_path)
elif command == "rm":
    path = location(positional[0])
    if os.path.isdir(path) and "--recursive" in flags:
        shutil.rmtree(path)
    elif os.path.isfile(path):
        os.remove(path)
    elif "--force" in flags:
        sys.exit(0)
    else:
        sys.exit(1)
else:
    sys.exit(2)
