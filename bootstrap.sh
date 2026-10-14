#!/usr/bin/env bash
# Receiver bootstrap (Linux/macOS): ensure python3>=3.10, 7z, rg, rclone.
# Idempotent: checks first, installs only what is missing. Then prints next steps.
set -u

missing=()
have() { command -v "$1" >/dev/null 2>&1; }

have python3 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null \
  || missing+=("python3>=3.10")
have 7z || have 7zz || have 7za || missing+=("7z")
have rg || missing+=("rg")
have rclone || missing+=("rclone")

if [ ${#missing[@]} -eq 0 ]; then
  echo "All dependencies present: $(python3 --version 2>&1), $(7z i 2>/dev/null | head -n1), rg $(rg --version 2>/dev/null | head -n1 | cut -d' ' -f2), $(rclone version 2>/dev/null | head -n1)"
else
  echo "Missing: ${missing[*]}"
  os="$(uname -s)"
  if [ "$os" = "Darwin" ]; then
    echo "Install with: brew install ${missing[*]//python3>=3.10/python@3.12}"
  elif have apt-get; then
    echo "Install with: sudo apt install python3 7zip ripgrep rclone"
  elif have dnf; then
    echo "Install with: sudo dnf install python3 7zip ripgrep rclone"
  elif have pacman; then
    echo "Install with: sudo pacman -S python 7zip ripgrep rclone"
  else
    echo "No supported package manager found; install Python>=3.10, 7-Zip, ripgrep, rclone manually."
    exit 1
  fi
  exit 2
fi

echo "Next:"
echo "  python3 backup.py --list-targets --no-elevate"
echo "  python3 backup.py --dry-run --no-elevate"
echo "  python3 backup.py --serve plan --no-elevate   # pick targets in the browser"
