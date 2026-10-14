---
name: backup
description: Versioned machine backups with backup.py (7z, ripgrep, rclone, request.json). Use when creating, testing, or automating a backup or restore, editing request.json or the backup schema, troubleshooting delivery to Google Drive or local copy, inspecting backup contents with the HTML serve report, recording machine notes (installed software, PATH tools, command outputs), or auditing unsynced git repos (ahead/behind counts) before a backup.
license: MIT
metadata:
  author: backup-project
  version: "2.0"
---

# Backup Skill

Versioned ZIP backup suite. Entry point `backup.py` (stdlib only, `python3 >= 3.10`).
Repository docs live beside the code; this file is the agent-facing contract.

- `backup.py` — main CLI
- `request.json` — local machine request (gitignored; copy from `request.json.example`)
- `request.schema.json` — JSON schema (`notes` section included)
- `docs/machine-migration-request.json` — full-machine migration template (verified source list)
- `data/<slug>/` — archives; `.bck/slugs/<slug>/` — versions (HEAD, index.json, history.jsonl, snapshots/)
- `.env` / `.env.example` — `${VAR}` expansion (`GDRIVE_REMOTE`, `BACKUP_COPY_TARGET`)

## Dependencies (all must pass before any backup)

`check_all_dependencies()` aggregates every missing tool into one banner. Fix all, then re-run.

| Dependency | Required when | Install (Linux / macOS / Windows) |
|---|---|---|
| 7-Zip (`7zz`/`7z`/`7za`) | Always | `apt install 7zip` / `brew install sevenzip` / 7-zip.org |
| ripgrep (`rg`) | Any `sources[].respect_gitignore=true` | `apt install ripgrep` / `brew install ripgrep` / `winget install BurntSushi.ripgrep.MSVC` |
| rclone | Any enabled `delivery[].type=rclone` | `apt install rclone` or rclone.org/install.sh / `brew install rclone` / `winget install Rclone.Rclone` |

## Mandatory pre-backup git-sync audit

Before every backup, audit local git repos for unsynced work. Read-only commands only:

```bash
# for each repo root (~/Projects, /mnt/devspace/Projects, plus any new source roots):
find <root> -maxdepth 4 -name .git | while read g; do d=$(dirname "$g");
  b=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null);
  u=$(git -C "$d" rev-parse --abbrev-ref @{upstream} 2>/dev/null || echo NO-UPSTREAM);
  if [ "$u" != "NO-UPSTREAM" ]; then c=$(git -C "$d" rev-list --left-right --count HEAD...@{upstream} 2>/dev/null || echo "? ?"); else c="N/A N/A"; fi;
  dirty=$(git -C "$d" status --porcelain=v1 --untracked-files=no 2>/dev/null | wc -l);
  echo "$d | $b | $u | $c | dirty=$dirty";
done
```

Report a table: repo | branch | upstream | ahead | behind | dirty. Flag `ahead > 0` (unpushed commits),
`NO-UPSTREAM`/`NO-REMOTE`, and `dirty > 0` (uncommitted work) so the operator can push/commit first.
Unpushed work that exists only locally is at risk; the backup preserves the files but not the git history intent.

## Quick start

```bash
7z --help; rg --version; rclone version          # 0. deps
cp request.json.example request.json            # 1. init
python3 backup.py --dry-run --no-elevate        # 2. validate
python3 backup.py --list-targets --no-elevate
python3 backup.py                               # 3. run (auto-elevates if require_elevation)
python3 backup.py --only <name>                 # single target
python3 backup.py --request docs/machine-migration-request.json --dry-run --no-elevate
python3 backup.py --list-versions --list-slugs  # 4. inspect
python3 backup.py --serve --no-elevate          # 5. HTML report in browser
python3 backup.py --serve --serve-port 8080 --no-elevate   # + serve over HTTP
python3 backup.py --restore --archive data/<slug>/<file>.zip --dry-run --no-elevate
```

## Request authoring rules

- Slug/hash: derived from `backup_name + sources + files + environment` only. `delivery`, `archive`,
  `require_elevation`, `notes` never change the slug.
- `sources[]`: `{name, path, enabled, respect_gitignore, follow_symlinks, include, exclude,
  force_include, path_regex_*, content_regex_*, content_encoding}`. `respect_gitignore:true` uses
  `rg --files --hidden --no-require-git`. Always exclude caches (`**/.git/**`, `**/node_modules/**`,
  `**/target/**`, `**/.venv/**`, `**/__pycache__/**`, `**/.ruff_cache/**`, `**/.pytest_cache/**`).
  `force_include` bypasses gitignore (e.g. `.env`); explicit `exclude` still wins.
- `files[]`: `{name, path (glob), enabled, required}` via `glob.glob(recursive=True)` + `expand_path`.
- `environment`: capture with redaction (`redact`, `redact_regex`); `redaction_enabled:false` stores verbatim.
- `notes`: non-transferrable machine knowledge → `_backup/notes.json` + `_backup/NOTES.txt`.
  `{enabled, include_software, include_path, custom[], capture_commands[]}`. Commands run with 60s
  timeout, 20KB stdout cap, never raise. Secrets (`.pem`, `*secret*`, `*recovery*`) must never go in
  `custom`/`capture_commands` output — keep them in encrypted/off-host handling instead.
- `archive`: `{compression_level 0-9, workers 0=auto, staging_mode copy|auto|hardlink, checksum,
  keep_last, fail_on_unreadable}`. `delivery[]`: `{type: rclone|copy, ...}`; `${VAR}` and
  `${REQUEST_SLUG}` expand; slug is auto-appended as `<destination>/<slug>/`.
- Validate shape against `request.schema.json` before running. Dry-run first for migration requests.

## Receiver bootstrap (new machine / agent handoff)

The skill alone cannot create dependencies — if `python3` itself is missing, even the
dependency checker cannot run. Receiver flow:

```bash
git clone <backup-repo> && cd backup
./bootstrap.sh            # Linux/macOS — checks python>=3.10, 7z, rg, rclone; installs what's missing
# bootstrap.ps1           # Windows (winget)
python3 backup.py --list-targets --no-elevate
python3 backup.py --serve plan --no-elevate   # see live per-target counts, pick a subset
python3 backup.py --restore --archive data/<slug>/<file>.zip --dry-run --no-elevate
```

`bootstrap.sh` is idempotent (checks first, per-manager install hints for apt/dnf/pacman/brew);
`bootstrap.ps1` uses winget. Both end with the next commands to run.

## Git history decision

Unpacked archives are plain folders: `.git` object dirs are excluded by default, so history does
not ride along. Rationale: git objects are OS-sensitive (file modes, symlinks, hooks, long paths,
case-insensitive filesystems on Windows/macOS) and often huge; the working tree is what the
operator needs back. Two mechanisms replace it:

- Per-source opt-in `include_git: true` drops the `**/.git/**` exclusion for that source (all
  enumeration paths read `cfg["exclude"]`, so one normalization in `load_request` covers rg, walk,
  and pattern matching). Default `false` everywhere — enable only for small repos you truly want
  byte-identical, after weighing size.
- Always-on-cheap `git_state: {enabled}` records identity, not objects: remote URLs, branch, HEAD
  SHA, ahead/behind, dirty count per repo under in-scope sources (maxdepth 4, read-only, skips
  symlinks) → `_backup/git-state.json` + human `GIT.txt` with the re-clone recipe
  (`git clone <url> <path>`, or init + remote add + fetch + `reset --hard <sha>`), then restore
  working-tree files over it. Manifest carries `git_state={enabled, repo_count}`.

## Cross-OS restore paths

Stored archive paths are POSIX; original absolute sources are kept as strings. No third-party path
library is used or needed — stdlib only: `ntpath`/`posixpath` importable on any OS,
`pathlib.PurePosixPath`/`PureWindowsPath` for parsing, `~` via `Path.home()`/`expanduser()` at
backup time. Translation is a user-supplied longest-prefix map (surveyed `universal-pathlib`,
`pathvalidate`, `pyfakefs` — none does Linux↔Windows mount translation):

- Request `restore.path_aliases: [{from, to}]` plus repeatable CLI `--map FROM=TO`
  (e.g. `--map /mnt/devspace=D:/data`), applied to every restore destination.
- `/mnt/*` is reserved for mounts and has no Windows equivalent: an unmapped Linux mount path on
  Windows is a hard error telling the operator the exact `--map` to pass.
- `~`-rooted and relative locations need no mapping (`expand_path` resolves per-OS).

## Serve (`--serve report|plan`)

Two subcommands, both write a single-file HTML report under `data/<slug>/` and open it in the
default browser (`--no-browser` skips auto-open, `--serve-port PORT` also serves over HTTP on
127.0.0.1, `--version N` pins an older snapshot for `report`):

- `report` → `<slug>-report.html`: version header + history, file table (size, source) with search
  box, per-target filter, checkbox selection, select-shown/clear, copy-selected, download-JSON
  (first 10k rows rendered). Full detail (software inventory, notes, environment) is inside the
  archive under `_backup/`.
- `plan` → `<slug>-plan.html`: live planner over the request file itself. Every source is
  enumerated with its own gitignore/rg filters (same code path as backup, read-only, no lock) and
  every `files[]` glob resolved, so counts show what a backup would actually capture. Per-target
  checkboxes (pre-checked = enabled) drive: copyable `--only ...` command and a downloadable
  trimmed `request.selection.json` with `enabled` flags set to the selection. Honors `--only` to
  preview a subset: `--serve plan --only <name>`.

## Automation (unattended runs)

`systemd/` ships user units (no root needed). Cron alternative is one line.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/bck-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bck-backup.timer
systemctl --user list-timers | grep bck
journalctl --user -u bck-backup.service --since today   # logs
```

Rules: unattended runs cannot prompt — set `passphrase_from` to `env` (put
`BACKUP_PASSPHRASE` in an `EnvironmentFile`, mode 600) or `keyring` (stored beforehand
via `--store-passphrase`). Cron alternative:
`0 2 * * * /usr/bin/python3 $HOME/backup/backup.py --request $HOME/backup/docs/machine-migration-request.json >> $HOME/backup/backup-cron.log 2>&1`.
Retention is `archive.keep_last`; delivery failures exit 2 without rolling back the archive.

## Encryption (`archive.encryption`)

Optional WinZip AES-256 via the already-required 7z (`-mem=AES256`). No new system dependencies.
`zstd` is compression-only and cannot encrypt, so it is deliberately not in this path.
`{enabled, method: "7z-aes", passphrase_from: prompt|env|keyring, keyring_service, keyring_user}`.
Rules the agent must enforce:

- Passphrase sources: `prompt` (getpass, needs a tty — resolved once at startup *before* sudo
  elevation); `env` (`BACKUP_PASSPHRASE`, wins in every mode); `keyring` (OS store via the optional
  `keyring` package; `python backup.py --store-passphrase` saves it). Never log it, never put it in
  `custom`/`capture_commands`, never commit it.
- The secret is excluded from `selected_environment_names()` and `capture_environment()`, carried
  to the elevated child via sudo `--preserve-env` only (memory), and only the flag
  `manifest.encryption={enabled, method}` is persisted — never the secret.
- Enabling/disabling encryption never changes the slug (archive section is hash-excluded).
- Restore auto-detects encryption (manifest flag, else stdlib "password required" error) and
  extracts via `7z x -p`, so encrypted restores need 7z plus the same passphrase sources.
  Even `--restore --dry-run` needs the passphrase (file contents, hence the tree, are encrypted).
- Caveat: the passphrase is briefly visible in the process list while 7z runs (7z has no
  pass-file option). On shared machines prefer `prompt` over `env`.
- Never enable encryption on the migration template by default — the operator enables it only
  after a passphrase path exists, and verifies with `--dry-run` first (dry-run never prompts).

## Conventions

- Keep `.env` loading (`_load_dotenv`, existing env wins) and `atomic_write` for HEAD/index/history.
- Lock file at `.bck/slugs/<slug>/lock`. Never enumerate `data/` or `.bck/` (`is_internal_output`).
- `write_metadata` always writes `_backup/manifest.json`, `tree.json`, `RESTORE.txt`,
  `software_inventory.json`/`SOFTWARE.txt`, `environment.json` (when enabled), `notes.json`/`NOTES.txt`
  (when notes enabled).
- Delivery errors never roll back the local archive; exit 2 = local ok, delivery failed.
- Test with `--dry-run` first; full migration is multi-GB / tens of thousands of files.
