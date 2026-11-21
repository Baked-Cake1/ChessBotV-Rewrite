import hashlib
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(SCRIPT_DIR, "new.py")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if shutil.which("git") is None:
        print("Git is not installed or not in PATH.")
        print("Please install Git from https://git-scm.com/downloads to enable automatic updates.")
        return 1

    if not os.path.isdir(os.path.join(SCRIPT_DIR, ".git")):
        print("This folder is not a git repository; skipping auto-update.")
        return 0

    print("Checking for updates to new.py...")
    fetch = subprocess.run(
        ["git", "fetch", "origin", "main"],
        cwd=SCRIPT_DIR,
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        print(f"Update check failed (git fetch): {fetch.stderr.strip()}")
        return 0

    remote_blob = subprocess.run(
        ["git", "show", "origin/main:new.py"],
        cwd=SCRIPT_DIR,
        capture_output=True,
    )
    if remote_blob.returncode != 0 or not remote_blob.stdout:
        print("Could not read origin/main:new.py; skipping update.")
        return 0

    remote_bytes = remote_blob.stdout
    remote_hash = _sha256_bytes(remote_bytes)
    local_hash = _sha256_file(TARGET_FILE) if os.path.isfile(TARGET_FILE) else ""

    if local_hash == remote_hash:
        print(f"new.py is up to date (SHA-256: {local_hash[:12]}...).")
        return 0

    print("Update found for new.py! Updating...")
    with open(TARGET_FILE, "wb") as f:
        f.write(remote_bytes)

    verified_hash = _sha256_file(TARGET_FILE)
    if verified_hash != remote_hash:
        print(
            f"ERROR: Hash verification failed after update! "
            f"(expected {remote_hash[:12]}, got {verified_hash[:12]})"
        )
        return 1

    print(f"new.py updated and verified (SHA-256: {verified_hash[:12]}...).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
