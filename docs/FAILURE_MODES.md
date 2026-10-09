# Failure modes

Risk is about losing or corrupting a backup, not about a noisy log. High means a backup can disappear from both sides or be replaced by bad bytes. Medium means the copy is incomplete and will not retry by itself. Low means the bytes stay on at least one side.

| Step | Failure | What happens today | Risk |
| --- | --- | --- | --- |
| Stability wait | File still growing after 5 watches of 60 seconds | That file’s `put` exits non-zero. cPanel does not call `put` again. The local file stays. | Medium |
| Stability wait | File is already 15 minutes old | No 60 second watch. Upload starts. | Low |
| Upload | `mc put` fails, or the temporary object is the wrong size | Temporary object removed. Same file retried up to 5 times, 30 seconds apart. Then `put` fails. Local file stays. | Low |
| Upload | Temporary object is verified, final copy or final read fails | Temporary object kept. Only the copy is retried, up to the same 5. Local file stays until the final object verifies. | Low |
| Upload | Process killed after the final copy and before the state row is `VERIFIED` | The final object can already be good. The row stays `UPLOADING` or `VERIFYING` until the next open sees a dead pid or a stale heartbeat and marks it retryable. | Low |
| Checksum mismatch | Remote final object does not match | Final name is left unchanged when the copy never verified. Local file stays. `REMOTE_EXISTS_POLICY=VERIFY` will not overwrite a different existing object. | Low |
| Local delete | Recheck or `delete-local` fails after verification | Remote copy stays. State becomes `LOCAL_CLEANUP_FAILED`. `put` exits 0. The report is a warning, not a failed backup. | Low |
| Local delete | `DELETE_LOCAL_AFTER_SUCCESS=0` | Nothing local is removed by the plugin. | Low |
| System trigger | `put` of an account returns 0, then the machine loses power before `send-system.sh` finishes | Account objects already verified stay. `system/` files not yet published stay on disk if their delete has not run. cPanel has already been told the account `put` succeeded. | Medium |
| System trigger | `setsid` or the second fork fails | The mirror is not started. Account `put` still returns 0. `system/` is not uploaded until a later account `put` or the manual button. | Medium |
| System trigger | An account `put` starts while one system file holds `upload.lock` | The account `put` waits up to `UPLOAD_LOCK_WAIT_SECONDS` (default 120). If that file is still uploading when the wait ends, this `put` fails and cPanel will not retry it. The local account archive stays. | Medium |
| Lock | `flock` holder dies | The kernel releases the lock. The pid in the lock file is only a log hint and is never used to steal the lock. | Low |
| Lock | Two cPanel backup runs overlap | The second file fails with “Another PARSPACK upload is running”. | Medium |
| Email | SMTP off, or `send-system.sh` never reaches the end | No mail. The report file is also not refreshed by the night run. | Low for bytes, medium for notice |
| Email | SMTP host certificate does not match | Report is still written. STDERR gets `Failed to send report email`. | Low |
| Bucket full | `mc put` fails | Retries, then `put` fails. Local file stays. Older remote days are not deleted. | Medium |
| Clock skew | Server date is wrong | Object date comes from the backup directory name, not from UTC now. | Low |
| Manual button | Newest day only | An older day that still has files is ignored when a newer day directory exists. | Medium |
| Report | `system/dirs/*.tar.gz` published | Counted as transferred when the log line is under `system/` and the publish line follows. They are not listed by reading `system/` on the bucket. | Low |

`TEST` mode exits `3` from the engine and creates no object. It also refuses remote delete. A production server left in `TEST` will fail every `put` and keep every local file.
