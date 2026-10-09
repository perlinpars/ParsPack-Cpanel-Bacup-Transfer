# STATUS REPORT — PARSPACK Backup (read-only audit)

**Date:** 2026-10-08 (evidence gathered) / report written 2026-10-09  
**Tree:** Windows workspace `parspack-backup-plugin` (VERSION `0.3.0`)  
**Method:** repo read + local `tests/run-tests.sh` against `tests/fake-mc.py` only. No live bucket, no writes under `/root/parspack-backup` on a production host.

---

## 1. Executive summary

1. Verdict: **SAFE ONLY IN TEST MODE** (and not safe to treat as production-ready until the offline suite is green).
2. Phases 1–6 are largely implemented in this tree; Phase 7 is design-only (`docs/PHASE7.md`).
3. Phase 4 is **not** deferred in this tree: date-folder guards, `mc rm`, and `parspackctl retention` exist (`lib/parspack_retention.py`, `bin/remote-ops.sh`, `tests/phase4-tests.py`). That matches current docs (`docs/DECISIONS.md`, `docs/CPANEL_COMPLIANCE.md`) but contradicts any “Phase 4 intentionally deferred / nothing implemented” owner note.
4. Default remote delete remains off (`ALLOW_REMOTE_DELETE=0`); refusal message `refused: remote delete disabled` is implemented and tested.
5. Offline suite against fake `mc`: **31 passed / 27 failed** (full run via a `set +e` copy; unmodified `tests/run-tests.sh` aborts early under `set -e` after an expected non-zero remote-ops check).
6. Critical test blocker: after a failed `mc put`, retry path calls `retry-delay` but fake-mc dumps `"ok"` lines that poison argv parsing → `config error: usage: retry-delay ATTEMPT BASE CAP` (`bin/engine-upload.sh` + `lib/parspack_config.py:1954-1957`). PRODUCTION upload path therefore fails in this harness.
7. Phase 5 suite fails when run as root: `not_root()` expects `require_root()` to reject (`tests/phase5-tests.py:91-110`); root correctly passes, so the test is environment-coupled.
8. Version string is consistent at `0.3.0` in `VERSION` and docs that name a release; README does not pin a version number.
9. Account `put` does not wait for `system/`; pending marker + detach are implemented (`transport/parspack_transport.pl:138-216,285-292`).
10. Do not enable `ALLOW_REMOTE_DELETE=1` or `DELETE_LOCAL_AFTER_SUCCESS=1` until staging proves verify + delete paths; local suite currently cannot prove a successful fake upload end-to-end.

---

## 2. Phase table

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 (docs/baseline) | DONE | `docs/ARCHITECTURE.md`, `docs/CPANEL_COMPLIANCE.md`, `docs/DECISIONS.md` Phase 0 |
| 1 (security/correctness) | DONE / PARTIAL on tests | Transport env sanitize `transport/parspack_transport.pl:13-18`; path checks `lib/parspack_config.py:1130-1174`; argv/`--` in shell helpers; hostile remote tests exist but expect exit 1 and got 2 in suite |
| 2 (state) | DONE | `lib/parspack_state.py` SCHEMA 2, WAL, transitions, TSV import; `tests/state-tests.py` OK |
| 3 (verify/manifest/trigger) | DONE | `docs/VERIFICATION.md`; engine verify levels; `bin/send-system.sh` gate/manifest; `tests/phase3-tests.py` OK |
| 4 (remote delete/retention) | DONE in code (not deferred here) | `lib/parspack_retention.py`; `bin/remote-ops.sh:136-169`; `tests/phase4-tests.py` OK; `parspackctl retention` |
| 5 (CLI) | PARTIAL | Commands in `bin/parspackctl`; `tests/phase5-tests.py` FAIL under root |
| 6 (WHM/alerts) | DONE (mock-tested) | `whm/index.php`, `lib/parspack_ui.py`; `tests/phase6-tests.py` OK |
| 7 | NOT STARTED | `docs/PHASE7.md` line 3: “Nothing in this note is implemented” |

---

## 3. Step 1 — Inventory

### Version string

| Location | Value | Notes |
| --- | --- | --- |
| `VERSION` | `0.3.0` | Canonical |
| `bin/parspackctl` `version` | reads `VERSION` | `bin/parspackctl:16-17` |
| `docs/ARCHITECTURE.md:3` | `0.3.0` | Match |
| `bin/send-system.sh:252-254` | from `VERSION` into manifest | Match |
| `README.md` | no numeric version | Docs vs marketing mismatch (minor) |
| Transport / WHM | no hardcoded plugin version | OK |

**Mismatches:** none for `0.3.0` vs `0.2.3` in release files. Legacy migration still references `uploads.tsv.0.2.3.bak` (`lib/parspack_state.py:278`).

### File tree (purpose vs Phase 0 architecture)

Compared to `docs/ARCHITECTURE.md` file table. Items present that Phase 0-era trees typically lacked (now documented):

| Path | Purpose |
| --- | --- |
| `VERSION` | Plugin version |
| `transport/parspack_transport.pl` | cPanel transport source |
| `bin/engine-upload.sh` | Single-file upload/verify/local-delete |
| `bin/remote-ops.sh` | ls/mkdir/chdir/get/delete/rmdir |
| `bin/send-system.sh` | Detached system mirror + manifest + report |
| `bin/send-remaining.sh` | Manual retry alias |
| `bin/parspackctl` | CLI entry |
| `bin/parspack-state` | State CLI wrapper |
| `bin/report.py` | Daily report / email |
| `lib/parspack-common.sh` | Bootstrap, flock, mc wrapper |
| `lib/parspack_config.py` | Config, path checks, mc config |
| `lib/parspack_state.py` | SQLite state machine |
| `lib/parspack_ops.py` | retry/verify/status/connection-test |
| `lib/parspack_ui.py` | WHM dashboard / alerts |
| `lib/parspack_retention.py` | Phase 4 delete/rmdir guards + dry-run |
| `whm/index.php`, `whm/parspack_backup.conf`, `whm/appconfig.json` | WHM UI + AppConfig |
| `tests/*` | Offline suite + fake-mc + phase tests |
| `docs/*` | Architecture, compliance, decisions, etc. |
| `install.sh` / `uninstall.sh` / `upgrade.sh` | Install lifecycle |
| `cron/report.cron` | Commented example only |
| `config/parspack.conf.example` | Example config |

No git history available in this audit → “added/changed since Phase 0” is **inferred from docs describing 0.3.0 features**, not from `git log`.

### Phase 4 deferred claim

**Owner query said Phase 4 was intentionally DEFERRED.** In this tree it is **implemented**:

- Guards: `lib/parspack_retention.py:37-69`
- Delete/rmdir: `bin/remote-ops.sh:136-169`
- Tests: `tests/phase4-tests.py`, suite OK for phase 4 unit tests
- Docs describe it as done: `docs/DECISIONS.md` “Phase 4”, `docs/CPANEL_COMPLIANCE.md:26-39`

Nothing was “relaxed” to always-delete: default still requires `MODE=PRODUCTION` and `ALLOW_REMOTE_DELETE=1`.

---

## 4. Checklist results (Step 2)

Legend: DONE / PARTIAL / MISSING / UNVERIFIED. Evidence is `file:line`. Tests named or `no test`.

### Phase 1

| Item | Status | Evidence | Test |
| --- | --- | --- | --- |
| realpath + LOCAL_READ_ROOTS; symlink/hardlink/non-regular/empty; TOCTOU | DONE | `lib/parspack_config.py:1130-1174` | `tests/run-tests.sh` symlink/empty/hardlink (expect exit 1; got 2 → harness mismatch) |
| No string-built commands; argv + `--`; hostile inputs | DONE | `pp_mc` argv `lib/parspack-common.sh:78-87`; `--` in engine/remote-ops | hostile remotes in `run-tests.sh:147-154`; self-test sanitize `parspack_config.py:1979-1988` |
| Fixed PATH, umask 077, env sanitize, root temp; config perms | DONE | Transport `13-18`; `pp_bootstrap` `34-71`; conf tighten `868-881` | doctor mode 600 OK in suite |
| No secrets in argv/logs/errors | PARTIAL | Secrets unset before mc `78-87`; show-config prints `set` `1715-1717`; WHM “Configured” | credentials argv OK; alert secret scrub `phase6-tests` |
| flock bounded timeout; system mirror per-file lock | DONE | `pp_flock` `121-160`; engine `266-267`; send-system probe `70-80` | no overlapping-cPanel integration test → UNVERIFIED live |
| RETRYABLE vs FATAL; backoff+jitter; configurable attempts | DONE | `classify_transport_error` `1234-1256`; `retry_backoff_seconds` `1259-1272`; defaults `368-370` | self-test asserts; **engine retry broken in fake-mc run** |
| Stability: size AND mtime, exists, not symlink, readable | DONE | `file_identity` / `check_file_age_and_stability` `engine-upload.sh:15-61` | offline sets `STABILITY_CHECK_SECONDS=0` |
| System-pending marker (atomic 0600 before detach; fail closed on write; setsid checked; put 0 after account verify; remove after system verified; CRITICAL leftover) | DONE | Transport `138-216,285-292`; clear `send-system.sh:83-96`; doctor/report/send-remaining CRITICAL | phase5 status pending; phase6 pending critical; **no live setsid kill test** |
| put never waits for whole system tree | DONE | Transport exits after spawn `285-292`; send-system separate | design + code |
| delete/rmdir refuse except validate.tmp probe | DONE | `pp_assert_remote_delete_allowed` `163-166`; validation path `123-133` | delete refusal OK; validation delete OK |
| ls missing → 0; mkdir `.keep`; put/get order pinned; MODE=TEST exit 3 | PARTIAL | remote-ops ls/mkdir; transport put/get order; engine exit 3 `276-290` | order/TEST exit 3 OK; **ls missing FAIL (rc=1)**; **get FAIL** |
| REMOTE_DELETE_SCOPE only in DECISIONS | DONE | Grep: no `REMOTE_DELETE_SCOPE` in code | n/a |

### Phase 2

| Item | Status | Evidence | Test |
| --- | --- | --- | --- |
| SQLite path/mode/WAL/busy_timeout/FK | DONE | `connect` `157-183` | `state-tests.py` OK |
| schema_version + migrations | DONE | `SCHEMA_VERSION=2`, `migrate` `186-272` | phase3 migrate_v1 OK |
| tables + state machine + invalid transition | DONE | `TRANSITIONS` `45-58`; `transition` `423-431` | state-tests transitions |
| idempotency / crash recovery | DONE | `same_verified` / `recover` `504-531` | state-tests crash/concurrent |
| JSONL events + uploads.tsv export + 0.2.3 bak | DONE | `_append_jsonl` `368-390`; `import_legacy_tsv` `288+` | state-tests migration |

### Phase 3

| Item | Status | Evidence | Test |
| --- | --- | --- | --- |
| Checksum levels (sha256 / etag / size-only / cat fallback) | DONE | `classify_integrity` `1412-1440`; engine `classify_remote` | phase3-tests OK |
| Local delete separate re-verify + delay config | DONE | `maybe_delete_local` / `recheck_then_delete` engine `120-189` | run-tests local delete **FAIL** (upload never succeeded) |
| manifest.json last + verified | DONE | `send-system.sh:260-270` | phase3 gates_and_manifest |
| System job when accounts terminal or deadline | DONE | `accounts-ready` / loop `send-system.sh:133-173` | phase3 gates |

### Phase 5

| Item | Status | Evidence | Test |
| --- | --- | --- | --- |
| retry / verify / status / doctor / connection-test | DONE in code | `bin/parspackctl`; `parspack_ops.py` cmds | phase5 FAIL under root; doctor OK in shell suite |

### Phase 6

| Item | Status | Evidence | Test |
| --- | --- | --- | --- |
| Dashboard, CSRF, escaping, secrets as Configured, whitelist, severities, dedupe | DONE | `whm/index.php` CSRF `90-106,234-237,313`; `pp_h`; `SETTINGS_KEYS` `509-519`; `parspack_ui` severities/dedupe | phase6-tests OK |

### Phase 7

| Item | Status | Evidence |
| --- | --- | --- |
| Implementation | NOT STARTED | `docs/PHASE7.md:3` |

---

## 5. Compliance deviations (Step 3)

Source docs: cPanel custom transport (skeleton + Script operation), pruning 300s note; WHM AppConfig guides.

| Command | Contract (docs) | This plugin | New vs Phase 0? |
| --- | --- | --- | --- |
| put | local, remote, host, user; STDERR on fail | Skeleton order `transport:270-272`; path check; engine; pending+detach | Pending/marker/detach are post-0.2.3 behaviors; documented in `CPANEL_COMPLIANCE.md:21` |
| get | remote, local | Skeleton `317-323`; stricter checksum | Stricter than sample (documented) |
| ls | ls-like STDOUT | Missing dir → empty + 0 (`remote-ops:46-49`) | Documented deviation |
| mkdir | create dir + recurse flag | Ignores recurse; writes `.keep` | Documented |
| chdir | print next local dir | Prints composed remote; no create | Documented |
| rmdir / delete | remove path | Default refuse; with flag date-folder guard | Phase 4 implementation is the change vs “always refuse” soft plan |
| 300s | process limit | Stability/retries can consume budget (`CPANEL_COMPLIANCE.md:21`) | Documented risk |

**WHM registration** (`install.sh:53-54`, `whm/parspack_backup.conf`):

- `service=whostmgr`, `user=root`, `acls=all` — matches [AppConfig Configuration File](https://api.docs.cpanel.net/guides/guide-to-whm-plugins/guide-to-whm-plugins-appconfig-configuration-file/) (`user` defaults to root for WHM; `acls` required).
- `register_appconfig` — matches [AppConfig Registration](https://api.docs.cpanel.net/guides/guide-to-whm-plugins/guide-to-whm-plugins-appconfig-registration).
- Runtime ACL: `pp_allowed()` checks root or reseller `all` (`whm/index.php:160-180`) — aligns with [Access Control Lists](https://api.docs.cpanel.net/guides/guide-to-whm-plugins/guide-to-whm-plugins-access-control-lists) guidance to require `all`.
- CSRF: app-level `state/whm-csrf` + `hash_equals`; session path `/cpsess…/` preserved in redirects (`150-157`). Not a substitute for cPanel session tokens; complementary.

---

## 6. Test results (Step 4)

### Command

- Environment: WSL root, fake-mc only.
- Unmodified `tests/run-tests.sh`: aborts mid-suite under `set -e` after expected non-zero remote-ops (incomplete counts).
- Full accounting: copy of suite with `set +e` (script not modified in repo): **passed=31 failed=27**.

### Failures (every FAIL name)

1. phase 5 tests — non-root check fails when suite runs as root  
2. TEST mode stored data — cascade from earlier put leftovers / failed cleanup (harness)  
3. PRODUCTION upload (rc=2) — `retry-delay` usage error after fake-mc `"ok"` on stderr/stdout  
4. prefix not applied  
5. VERIFY skips matching checksum (rc=2)  
6. VERIFY refuses different size (rc=2 want 1)  
7. remote object was overwritten (cascade; object missing)  
8. REPLACE overwrites (rc=2)  
9. SKIP leaves existing object (rc=2)  
10. SKIP overwrote the object (cascade)  
11–18. Hostile/path refusals — correct rejection message but **exit 2 want 1** (ConfigError path)  
19. multipart checksum fallback (rc=2)  
20. content verification was not logged  
21. local file removed after verified upload (rc=2)  
22. local/remote delete result wrong  
23. ls missing rc=1 out= (want 0)  
24. get (rc=1) — checksum compare bug: `stat-fields` returns `size\tsha\tetag`; shell takes `${fields#*$'\t'}` as `sha\tetag`, so compare fails (`remote-ops.sh:92-114`, `stat_fields` `1496-1504`)  
25. get contents were wrong  

Passed highlights: python self-test, state, phase3/4/6, doctor, delete refusals, validation delete, Phase 4 rmdir guards, mkdir/.keep, put/get argument-order greps, TEST exit 3 string present, credentials not on mc argv.

### What mock cannot prove

- Real ParsPack S3 semantics, multipart ETag from live `mc`, TLS/DNS to real endpoint, cPanel 300s kill, real `setsid` detach, overlapping live cPanel puts, WHM AppConfig registration on a host, true remote pruning with `ALLOW_REMOTE_DELETE=1`.

### Linters

- **shellcheck:** not installed (`NO_SHELLCHECK`).  
- **ruff/flake8/pylint:** not found.  
- Manual: `python3 -m py_compile` on main lib/bin modules → OK.  
- Suspicious constructs (manual): fake-mc prints `"ok"` confusing retry-delay stdin/argv; `get` field split ignores third etag column; phase5 `not_root` unsuitable for root CI; `send-remaining.sh:154-161` may clear pending marker after system retry without full send-system completion path — review carefully on staging.

---

## 7. Ranked risk list (Step 5) — max 15

1. **Critical — Offline PRODUCTION upload path fails in harness (`retry-delay` poisoned)**  
   Where: `bin/engine-upload.sh` retry after put fail; `lib/parspack_config.py:1954-1957`; `tests/fake-mc.py` prints `ok`.  
   Impact: cannot trust CI green; if similar stdout bleed occurred with real tools, retries misbehave.  
   Fix: isolate `retry-delay` argv from mc stdout; make fake-mc quiet; assert exit codes.

2. **Critical — `get` checksum compare broken when etag present**  
   Where: `bin/remote-ops.sh:92-114` vs `stat_fields` three-column output.  
   Impact: restore/`get` fails even when object is correct → cPanel restore broken.  
   Fix: parse fields as size / sha / etag separately (same as engine).

3. **High — Suite `set -e` aborts before remote-ops completion**  
   Where: `tests/run-tests.sh:3` + expected non-zero without `set +e` wrappers on all checks.  
   Impact: false confidence / incomplete CI.  
   Fix: wrap expected-failure calls; never rely on naked non-zero under `set -e`.

4. **High — Phase 5 “non-root” test fails when run as root**  
   Where: `tests/phase5-tests.py:91-110`.  
   Impact: root-required suite always fails phase5.  
   Fix: skip `not_root` when `geteuid()==0` or drop privileges in subprocess.

5. **High — Local delete after “success” without suite proof**  
   Where: `engine-upload.sh:120-189`, `delete_local` `1207-1220`.  
   Impact: if verify falsely passes, local archive gone. Defaults delete off — keep off until staging.  
   Fix: staging verify; keep `DELETE_LOCAL_AFTER_SUCCESS=0`.

6. **High — put exit 0 while system mirror never started (spawn fail)**  
   Where: `transport/parspack_transport.pl:211-214,285-292`.  
   Impact: cPanel thinks account OK; system incomplete; marker CRITICAL — accounts may be remote but day incomplete.  
   Fix: staging kill/setsid tests; alert on leftover markers (already CRITICAL).

7. **High — Remote delete when flag on**  
   Where: `bin/remote-ops.sh:136-169`; `mc rm`.  
   Impact: irreversible bucket data loss if guards wrong or newest_verified unknown.  
   Fix: keep flag 0; dry-run only; staging with disposable prefix.

8. **Medium — TOCTOU between check and `mc put`**  
   Where: documented `DECISIONS.md:18`; `check_local` then mc by path.  
   Impact: replaced file could upload.  
   Fix: accept residual risk or pass verified fd if mc supported (it does not).

9. **Medium — Lock wait expiry fails put (no cPanel retry)**  
   Where: `FAILURE_MODES.md`; engine `266-267`.  
   Impact: account missing from destination for that night.  
   Fix: monitor; use retry CLI; tune `UPLOAD_LOCK_WAIT_SECONDS` ≤240.

10. **Medium — Crash / kill -9 mid-upload**  
    Where: state recover stale → FAILED `504-531`; `.uploading` may remain.  
    Impact: incomplete object; next run should retry.  
    Fix: staging kill -9; confirm recover + retry.

11. **Medium — Secrets: transport password printable via non-TTY `get`**  
    Where: `parspack_config.py:1806-1809`.  
    Impact: scripts can dump password to logs if misused.  
    Fix: never call from cron without scrubbing; WHM does not echo it.

12. **Medium — Docs said Phase 4 deferred; code implements it**  
    Where: owner note vs `parspack_retention.py` / DECISIONS Phase 4.  
    Impact: operator may enable delete thinking guards absent or vice versa.  
    Fix: align owner plan with DECISIONS; explicit go/no-go for flag.

13. **Medium — Behavior change from 0.2.3 without operator checklist**  
    Where: SQLite state, system-pending, verification levels, Phase 4.  
    Impact: upgrade surprise.  
    Fix: follow `docs/STATE.md` migration/rollback; run doctor/connection-test.

14. **Low — ls missing directory exit non-zero in failing suite**  
    Where: `remote-ops.sh` ls + `stat-missing`; suite saw rc=1.  
    Impact: cPanel may treat empty remote listing oddly.  
    Fix: reproduce with fake-mc after upload path fixed; verify `stat-missing`.

15. **Low — README lacks version pin**  
    Where: `README.md` vs `VERSION`.  
    Impact: support confusion only.

**Paths called out by owner:**

- Local delete without verified remote: only after verify + recheck (`recheck_then_delete`); still risk if verify wrong — defaults off.  
- cPanel success without data: TEST exit 3 (intentional fail); PRODUCTION put 0 after verify of that file; system detach failure still 0 for account — documented.  
- Remote delete code paths: validation + gated delete/rmdir + discard `.uploading` + connection-test probe cleanup when flag on.

---

## 8. Docs vs code mismatches

| Claim | Reality |
| --- | --- |
| “Phase 4 deferred / nothing implemented” (owner query) | Code + DECISIONS + CPANEL_COMPLIANCE implement Phase 4 |
| `docs/PHASE7.md` “Phases 1–6 are in the Windows tree” | Consistent with this audit |
| Offline suite proves engine upload | Currently **does not** (27 failures) |
| `docs/CLI.md` doctor/connection-test DELETE skip | Matches `parspack_ops.py:596-600` |
| Architecture file list | Matches; plus `parspack_retention.py` / phase4 tests |
| Hostile refusal exit code 1 in tests | Code returns ConfigError exit **2** |

---

## 9. Open questions for the owner

1. Is Phase 4 **authorized for production** with `ALLOW_REMOTE_DELETE` still default 0, or should retention code be feature-flagged off entirely?  
2. Should `get` be hotfixed before any restore drill?  
3. Accept exit code **2** for path/config refusals, or force exit **1** for cPanel?  
4. Is this Windows tree the deploy source of truth, or is a server tree behind?  
5. Target retention count / whether remote pruning will ever be enabled?  
6. Keep `DELETE_LOCAL_AFTER_SUCCESS=0` through first N production nights?  

---

## 10. Staging checklist (manual)

Run on a **staging** cPanel host with disposable bucket/prefix. Do **not** point at production data.

| # | Action | Expected |
| --- | --- | --- |
| 1 | Install to `/root/parspack-backup`, `MODE=TEST`, `parspackctl doctor` | Warnings OK; no upload |
| 2 | WHM Validate destination | Probe `validate.tmp-…` creatable/deletable; backup put exits 3 |
| 3 | Set `MODE=PRODUCTION`, `ALLOW_REMOTE_DELETE=0`, `DELETE_LOCAL_AFTER_SUCCESS=0`; run one account backup | Account object under `prefix/YYYY-MM-DD/accounts/`; `put` returns before system finishes; `state/system-pending-YYYY-MM-DD` appears then clears after mirror |
| 4 | Overlapping puts / hold `upload.lock` | Second put waits ≤ `UPLOAD_LOCK_WAIT_SECONDS`; clear message if timeout; no immediate crash |
| 5 | Kill -9 during `mc put` | Row recovers to FAILED; `.uploading` may remain; retry succeeds; final object verified |
| 6 | Force setsid/spawn failure (break send-system path briefly) | Marker remains; account put exit 0; doctor/report CRITICAL |
| 7 | Leave marker on disk | `parspackctl doctor`, report, send-remaining treat CRITICAL |
| 8 | Corrupt remote after upload; re-verify | VERIFY_FAILED / put fail; local kept |
| 9 | Fill/quota or deny put | FATAL or RETRYABLE per classify; no false VERIFIED |
| 10 | `remote-ops delete` / transport delete with flag 0 | STDERR `refused: remote delete disabled`, exit 1 |
| 11 | Optional flag 1 on **old** date only | Newest verified + today refused; older date removable; events.jsonl `REMOTE_DELETED` |
| 12 | `parspackctl connection-test` | PUT/HEAD/LIST; DELETE skipped unless flag 1 |
| 13 | `parspackctl verify DATE` | Matches local vs remote |
| 14 | Restore via cPanel `get` after get-fix | Object downloads and verifies |

---

*End of report. Only file created for this audit: `docs/STATUS_REPORT.md`.*
