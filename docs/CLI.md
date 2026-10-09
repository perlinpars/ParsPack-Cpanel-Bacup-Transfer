# Operational CLI

`bin/parspackctl` is root-only. A non-root caller exits 2. Arguments that do not match the usage below exit 2. Secrets are not printed. `mc` stderr is classified and discarded.

| Command | Success | Problem | Usage or state error |
| --- | --- | --- | --- |
| `doctor` | 0 | 2 | 2 |
| `connection-test` | 0 | 1 | 2 |
| `show-config` | 0 |  | 2 |
| `version` | 0 |  | 1 if the command name is unknown |
| `report` | 0 | report's own code | 2 from configuration |
| `send-remaining` | 0 after the pass is started, or after it finishes | 1 when the system marker is still present | 2 when `MODE` is not `PRODUCTION` |
| `retry` | 0 | 1 when a selected upload fails | 2 |
| `verify YYYY-MM-DD` | 0 | 1 | 2 |
| `status [--date YYYY-MM-DD] [--json]` | 0 |  | 2 |

`retry` takes exactly one selector: `--failed`, `--date YYYY-MM-DD`, or `--account NAME`. It uploads rows in `UPLOAD_FAILED` or `VERIFY_FAILED` whose local file is still a regular file. `--date` and `--account` also upload local archives that are not already verified with the same size and checksum. A second run does not upload those verified files again. `send-remaining` finds the newest local day and calls `retry --date`.

`verify` compares the local account names and the database accounts with the remote `accounts/` listing, then the local and remote system file counts, then each stored size and checksum according to `verification_level`.

`doctor` and `connection-test` report Endpoint, TLS, Auth, Bucket, PUT, HEAD, LIST, DELETE, and Latency. PUT, HEAD, and LIST use one new object under `parspack-probe/` at the bucket root, never a backup key. That object is removed only when `MODE=PRODUCTION` and `ALLOW_REMOTE_DELETE=1`. Otherwise the line is `DELETE: cleanup skipped`. The offline test double `tests/fake-mc.py` skips the live DNS and TLS sockets and still runs the `mc` checks.
