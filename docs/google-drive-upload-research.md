# Google Drive Upload for Backup — Minimal Setup Research

Date: 2026-08-27  
Repo: `/home/ghostrider/backup` — `backup.py` already supports `delivery: {type: "rclone"}` and `type: "copy"`. This note evaluates the best Drive upload path for the upcoming `delivery: {type: "gdrive"}` / periodic timer (rclone vs native API vs service account).

## TL;DR Recommendation

**Default to `rclone copy` with a user-owned OAuth client.** It is the lowest-code, most resilient option for personal backups and the backup already shells out to it. Keep a fallback native Python uploader (Drive API v3, `google-api-python-client`, resumable) for hosts where installing `rclone` is forbidden.

- Personal / laptop: `rclone` + own `client_id`/`client_secret`, `scope=drive.file` or `drive` → `rclone copy data/<slug>/ gdrive:Backups/<slug>/`
- Headless server: same, but `rclone authorize` on a machine with a browser, then copy `rclone.conf`; or `rclone config` with `Use web browser = N` + verification code.
- Workspace / unattended fleet: Service Account + Shared Drive + `--drive-impersonate` (old SAs pre-2025-04-15 still have My Drive; new SAs must use Shared Drive).

Avoid `gdrive` (prasmussen) — unmaintained — and custom curl unless you need to avoid `rclone`.

## 1. rclone — Drive remote

### Why it wins for backups
- Single static binary (`curl https://rclone.org/install.sh | sudo bash`, `brew install rclone`, `apt install rclone`) — no Python deps. Handles chunking, retries, `--transfers 4 --checkers 8`, `--tpslimit`, `--bwlimit`, `--fast-list`, resumption (`just run it again`). — [rclone.org/drive](https://rclone.org/drive/)
- Backup already does `subprocess.run([rclone, "copy", ...])` → minimal integration cost.
- Correctly handles Drive quirks: case-insensitive names, illegal-char encoding, missing `SHA1/SHA256` hashes, `Shared Drive` vs `My Drive`.

### Setup (2026 — own client_id required)
The shared `client_id` is being retired during 2026 — creating your own is now required, not optional. [rclone.org/drive](https://rclone.org/drive/) and [Geeksta guide](https://geeksta.net/geeklog/setting-up-google-drive-backups-on-ubuntu-with-rclone) (2026-04-03) walk through it:

1. Google Cloud Console → New Project `rclone-backup` → Enable **Google Drive API**. [quickstart](https://developers.google.com/workspace/drive/api/quickstart/python)
2. APIs & Services → OAuth consent screen → External → app name `My rclone Backup`, support email, Scopes → `https://www.googleapis.com/auth/drive` (full) or `drive.file` (least-privilege — only files created by app, visible in UI, revoked on de-auth). Test users → add your Gmail.
3. Credentials → Create OAuth Client ID → Desktop app → copy `client_id` (`....apps.googleusercontent.com`) + `client_secret`.
4. `rclone config` → `n` → name `gdrive` → `drive` → paste `client_id`/`client_secret` → scope `1` (drive) → `service_account_file>` leave blank → `y` if local browser else `n` → open URL, Accept, paste verification code. [rclone.org/drive](https://rclone.org/drive/)
5. Test: `rclone lsd gdrive:` / `rclone mkdir gdrive:Backups` / `rclone copy /tmp/file gdrive:Backups --progress`

**Publishing:** leave app “Testing” and re-auth weekly, or “Publish” to avoid weekly expiry (unverified-app warning is expected for personal use). [rclone.org/drive](https://rclone.org/drive/)

### Headless
Two documented paths [rclone forum](https://forum.rclone.org/t/how-to-configure-a-service-account-for-a-personal-google-drive/29521):
- Run `rclone config` on a laptop with browser, then `scp ~/.config/rclone/rclone.conf server:~/.config/rclone/rclone.conf`.
- Or `rclone authorize` workflow / `Use web browser = N` → paste code. [Medium headless tunnel](https://medium.com/@itzmjunaid/how-i-connected-google-drive-to-a-headless-linux-server-using-rclone-without-losing-my-mind-e767babedef0)

### Service-Account variant (post-2025-04-15 caveat)
Google emailed Feb 2025: **new SAs created after 2025-04-15 cannot own Drive items, have no 15 GB, and must upload to Shared Drive or impersonate a human**. Old SAs retain storage. [rclone forum](https://forum.rclone.org/t/google-drive-service-account-changes-and-rclone/50136) + [gCloud docs](https://rclone.org/drive/#service-account-support)

Steps if you need it: IAM → Service Accounts → Create → Keys → JSON → `service_account_file` in `rclone config`, plus Admin Console → Security → API Controls → Manage domain-wide delegation → Client ID + scope `https://www.googleapis.com/auth/drive` + `--drive-impersonate foo@example.com`. For personal account, share a folder with the SA email or use a Shared Drive.

### Cost to our backup
Already implemented:
```json
{"type":"rclone","enabled":true,"destination":"gdrive:Backups","include_checksum":true,"args":["--transfers","4","--checkers","8","--progress","--tpslimit","5"]}
```
`backup.py` runs `rclone copy data/<slug>/ gdrive:Backups/<slug>/` after `sha256` + `.bck` commit; delivery failure returns `2` but does not delete local `data/<slug>/`. Add `--bwlimit`, `--buffer-size`, `--drive-chunk-size` as needed. Monitor via `rclone about gdrive:` and test restore with `rclone copy gdrive:Backups/... /tmp/restore-test --dry-run`.

## 2. Native Python — Drive API v3 (no rclone binary)

### Flow
1. Same Cloud project / Drive API enable / OAuth consent as above. Credentials → OAuth Client ID → Desktop app → download `credentials.json`. [quickstart](https://developers.google.com/workspace/drive/api/quickstart/python)
2. `pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib`
3. First run: `InstalledAppFlow.from_client_secrets_file("credentials.json", ["https://www.googleapis.com/auth/drive.file"]).run_local_server(port=0)` → saves `token.json` (access + refresh). Later runs: `Credentials.from_authorized_user_file("token.json", SCOPES)` → `creds.refresh(Request())`. [quickstart](https://developers.google.com/workspace/drive/api/quickstart/python) + [PyTutorial](https://pytutorial.com/python-google-drive-api-guide-for-beginners/) (2026-02-01)
4. Upload: `build("drive","v3",credentials=creds).files().create(body={"name": name, "parents":[folderId]}, media_body=MediaFileUpload(path, mimetype="application/zip", resumable=True), fields="id").execute()` — resumable for >5 MB, one extra HTTP request, survives network blips. [Uploads guide](https://developers.google.com/workspace/drive/api/guides/manage-uploads) (2026-08-19) / [Tanaike gist](https://tanaikech.github.io/2020/03/05/simple-script-of-resumable-upload-with-google-drive-api-for-python/)

Scopes: `drive.file` least-privilege (app-created only) vs `drive` full. `drive.readonly` is insufficient for backup.

### Trade-offs
- Pro: no external binary; can embed in `backup.py` as `delivery.type=gdrive`; token lives beside `.bck/`.
- Con: you reimplement what `rclone` already does (chunking, retries, rate-limit, Shared Drive handling). More code to maintain; headless still needs browser once (or copy `token.json`).

Minimal snippet (resumable, 256 KiB chunk multiple):
```python
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
SCOPES=["https://www.googleapis.com/auth/drive.file"]
creds = Credentials.from_authorized_user_file("token.json", SCOPES) if os.path.exists("token.json") else InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES).run_local_server(port=0)
service=build("drive","v3",credentials=creds)
media=MediaFileUpload(zip_path, mimetype="application/zip", resumable=True, chunksize=8*1024*1024)
service.files().create(body={"name": zip_path.name, "parents":[folder_id]}, media_body=media, fields="id").execute()
```

## 3. What not to pick
- `gdrive` (prasmussen) / `drive` CLI: unmaintained, lacks 2026 `client_id` guidance.
- Raw `curl`/`requests` to `https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable`: doable ([StackOverflow resumable](https://stackoverflow.com/questions/60528771/uploading-file-to-google-drive-using-resumable-api)) but strictly more code than client library, no benefit.

## Decision Table

| Criterion | rclone | Native Python | SA + Shared Drive |
|---|---|---|---|
| User setup (personal) | 10 min: Cloud project + `rclone config` with own client_id | 10 min + `pip` + `credentials.json`/`token.json` | 15 min + Workspace admin + Shared Drive |
| Headless | copy `rclone.conf` or `rclone authorize` | copy `token.json` | no browser, but needs Shared Drive post-2025-04-15 |
| Binary deps | rclone | none | rclone |
| Resumable large zip | built-in | `MediaFileUpload(resumable=True)` | built-in |
| Periodic timer | `rclone copy` idempotent | custom loop | custom loop |
| Maintenance | upstream rclone | our code | upstream rclone |
| 2026 client_id retirement | must own client_id | must own client_id | SA unaffected but storage rules changed |

## Proposal for this repo

1. **Keep `rclone` as `delivery.type=rclone` (already there).** Add docs showing the 10-min own-client setup and a helper `python backup.py --setup-gdrive` that prints the exact `rclone config` prompts and validates `rclone about gdrive:`.
2. **Add `delivery.type=gdrive` (Python) as fallback** when `rclone` missing: tiny module `gdrive.py` using `google-api-python-client`, storing ` .bck/gdrive/token.json` (gitignored), `credentials.json` path in `request.json`. Reuse slug: `gdrive:Backups/<slug>/`.
3. **Don’t add SA docs by default** — only for Workspace users; mention Shared Drive requirement post-2025-04-15.
4. Periodic timer (`systemd`/`cron`) will call `backup.py` then rely on whichever delivery is enabled; delivery failure is already non-destructive (`exit 2`).

## Sources
- Google Drive Python quickstart — project, consent screen, desktop OAuth, `token.json` flow — [developers.google.com](https://developers.google.com/workspace/drive/api/quickstart/python) (2026-07-22)
- Drive upload guide — simple/multipart vs resumable (>5 MB), `uploadType=resumable` — [developers.google.com](https://developers.google.com/workspace/drive/api/guides/manage-uploads) (2026-08-19)
- rclone Drive backend — config, `service_account_file`, `drive.file` vs `drive`, shared client_id retirement 2026 — [rclone.org/drive](https://rclone.org/drive/) (2026-07-31)
- Geeksta Ubuntu+rclone — own OAuth app steps, scopes, `rclone config` walkthrough — [geeksta.net](https://geeksta.net/geeklog/setting-up-google-drive-backups-on-ubuntu-with-rclone) (2026-04-03)
- rclone forum — SA ownership change after 2025-04-15, Shared Drive/impersonation required — [forum.rclone.org](https://forum.rclone.org/t/google-drive-service-account-changes-and-rclone/50136)
- Headless rclone — `rclone authorize` / copy `rclone.conf` — [forum.rclone.org](https://forum.rclone.org/t/how-to-configure-a-service-account-for-a-personal-google-drive/29521) + [Medium SSH tunnel](https://medium.com/@itzmjunaid/how-i-connected-google-drive-to-a-headless-linux-server-using-rclone-without-losing-my-mind-e767babedef0)
- Python minimal Drive read — `drive.readonly` scope — [dev.to/soytuber](https://dev.to/soytuber/using-python-to-load-google-docs-into-ai-drive-api-minimal-permission-setup-525c)
- Tanaike resumable gist — `Location` header session — [tanaikech.github.io](https://tanaikech.github.io/2020/03/05/simple-script-of-resumable-upload-with-google-drive-api-for-python/)
