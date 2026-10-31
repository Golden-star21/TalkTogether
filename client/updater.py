"""TalkTogether updater for Windows.

Checks GitHub prereleases, verifies the SHA-256 digest advertised by GitHub,
then stages the update and delegates file replacement to a separate BAT file.
The updater never overwrites .env or local virtual environments.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "updater_config.json"
VERSION_PATH = APP_DIR / "version.txt"
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "TalkTogether-Updater/0.6.0-beta.1",
    "X-GitHub-Api-Version": "2026-03-10",
}


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def version_key(value: str) -> tuple:
    """Compare x.y.z and alpha/beta/rc versions without extra packages."""
    match = re.fullmatch(
        r"v?(\d+)\.(\d+)\.(\d+)(?:[-.]?(alpha|a|beta|b|rc)(?:[.-]?(\d+))?)?",
        value.strip(), re.IGNORECASE
    )
    if not match:
        return (0, 0, 0, 0, 0)
    major, minor, patch, label, serial = match.groups()
    rank = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2, None: 3}
    return (int(major), int(minor), int(patch), rank[label.lower() if label else None], int(serial or 0))


def request_json(url: str) -> dict | list:
    request = urllib.request.Request(url, headers=API_HEADERS)
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def get_release(repo: str, channel: str) -> dict | None:
    url = f"https://api.github.com/repos/{repo}/releases?per_page=20"
    releases = request_json(url)
    if not isinstance(releases, list):
        raise RuntimeError("GitHub ni vrnil seznama izdaj.")
    for release in releases:
        if release.get("draft"):
            continue
        if channel == "beta" and release.get("prerelease"):
            return release
        if channel == "stable" and not release.get("prerelease"):
            return release
    return None


def safe_extract(zip_path: Path, destination: Path) -> None:
    blocked = {".env", ".venv", "venv", "__pycache__", "updates"}
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            posix = PurePosixPath(member.filename)
            if posix.is_absolute() or ".." in posix.parts or not posix.parts:
                raise RuntimeError("Posodobitev vsebuje neveljavno pot.")
            if any(part in blocked for part in posix.parts):
                continue
            if posix.name == ".env" or posix.name.endswith(".pyc"):
                continue
            target = (destination / Path(*posix.parts)).resolve()
            if destination_resolved not in target.parents and target != destination_resolved:
                raise RuntimeError("Posodobitev poskuša zapisati zunaj mape aplikacije.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def write_apply_script() -> Path:
    script = APP_DIR / "apply_update.bat"
    content = r'''@echo off
setlocal
cd /d "%~dp0"
timeout /t 2 /nobreak >nul
set "APP_DIR=%~dp0"
set "STAGE_DIR=%APP_DIR%updates\stage"
if not exist "%STAGE_DIR%\main.py" (
  echo Posodobitev ni popolna. Stare datoteke ostanejo nespremenjene.
  pause
  exit /b 1
)
robocopy "%STAGE_DIR%" "%APP_DIR%" /E /R:2 /W:1 /XD "%APP_DIR%updates" "%APP_DIR%.venv" "%APP_DIR%venv" >nul
if errorlevel 8 (
  echo Pri kopiranju posodobitve je prislo do napake.
  pause
  exit /b 1
)
rmdir /s /q "%STAGE_DIR%"
del "%~f0"
start "" "%APP_DIR%run.bat"
exit /b 0
'''
    script.write_text(content, encoding="utf-8")
    return script


def main() -> int:
    config = read_json(CONFIG_PATH)
    repo = str(config.get("github_repository", "")).strip()
    channel = str(config.get("channel", "beta")).strip().lower()
    asset_name = str(config.get("asset_name", "TalkTogether-Client.zip")).strip()

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or repo.upper() == "OWNER/REPOSITORY":
        print("Samodejni posodabljalnik še ni povezan z GitHub repozitorijem.")
        print("Aplikacija se bo zagnala z nameščeno različico.")
        return 0
    if channel not in ("beta", "stable"):
        print("Neveljaven kanal posodobitev v updater_config.json.")
        return 0

    try:
        local_version = VERSION_PATH.read_text(encoding="utf-8").strip()
        release = get_release(repo, channel)
        if release is None:
            print("Na izbranem kanalu še ni objavljene izdaje.")
            return 0
        remote_version = str(release.get("tag_name", "")).lstrip("v")
        if not remote_version or version_key(remote_version) <= version_key(local_version):
            print(f"TalkTogether je posodobljen. Trenutna različica: {local_version}.")
            return 0

        asset = next((item for item in release.get("assets", []) if item.get("name") == asset_name), None)
        if asset is None:
            print(f"Nova različica {remote_version} obstaja, vendar manjka paket {asset_name}.")
            return 0
        digest = str(asset.get("digest") or "")
        if not digest.startswith("sha256:"):
            print("GitHub za paket ni vrnil SHA-256 zgoščene vrednosti. Posodobitev je prekinjena.")
            return 0

        print(f"Na voljo je TalkTogether {remote_version} (trenutno {local_version}).")
        answer = input("Ali želiš namestiti posodobitev? (D/N): ").strip().lower()
        if answer not in ("d", "da", "y", "yes"):
            print("Posodobitev je preskočena.")
            return 0

        download_url = asset.get("browser_download_url")
        if not download_url:
            raise RuntimeError("Povezava za prenos izdaje manjka.")

        updates_dir = APP_DIR / "updates"
        stage_dir = updates_dir / "stage"
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)
        archive_path = updates_dir / "download.zip"
        request = urllib.request.Request(download_url, headers={"User-Agent": API_HEADERS["User-Agent"]})
        with urllib.request.urlopen(request, timeout=60) as response, archive_path.open("wb") as output:
            shutil.copyfileobj(response, output)

        hasher = hashlib.sha256()
        with archive_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest().lower() != digest.split(":", 1)[1].lower():
            raise RuntimeError("SHA-256 preverjanje prenosa ni uspelo. Posodobitev ni nameščena.")

        safe_extract(archive_path, stage_dir)
        if not (stage_dir / "main.py").is_file() or not (stage_dir / "version.txt").is_file():
            raise RuntimeError("Paket posodobitve ne vsebuje main.py ali version.txt.")
        archive_path.unlink(missing_ok=True)
        script = write_apply_script()
        print("Paket je preverjen. Aplikacija se bo zaprla in namestila posodobitev.")
        subprocess.Popen(["cmd.exe", "/c", str(script)], cwd=str(APP_DIR), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return 10
    except urllib.error.HTTPError as error:
        print(f"GitHub je vrnil HTTP napako {error.code}. Aplikacija se bo zagnala brez posodobitve.")
    except urllib.error.URLError as error:
        print(f"Internetna povezava za preverjanje posodobitev ni na voljo: {error.reason}")
    except Exception as error:
        print(f"Posodobitev ni uspela: {type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
