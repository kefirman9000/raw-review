"""
In-app updater for RAW Review.

* check_latest() asks GitHub for the newest version (read-only, no download):
  the latest GitHub Release if there is one, otherwise version.py on the
  main branch (release notes then come from CHANGELOG.md).
* install() runs ONLY after the user clicks "Install" in the app. It downloads
  the release zip, checks it, backs up the current app files to
  .update_backup/<old version>/, then replaces the app's own files.

The updater only ever writes inside the app's folder, and only these file
types: .py .txt .md .bat. It never looks at or touches your photos.

The Windows program (RAW Review.exe, built by the release workflow) updates
itself from the Windows zip of the newest Release instead: the download is
checked against SHA256SUMS, unpacked next to the app, and a small script
copies it over the program folder once the app has closed, then restarts it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import hashlib
import zipfile

from version import GITHUB_REPO, __version__

FROZEN = bool(getattr(sys, "frozen", False))        # running as RAW Review.exe (no Python needed)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
EXE_NAME = "RAW Review.exe"
API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
REPO_PAGE = f"https://github.com/{GITHUB_REPO}"
RAW_MAIN = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/"
MAIN_ZIP = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
ALLOWED_EXTS = {".py", ".txt", ".md", ".bat"}
MAIN_FILE = "raw_review_app.py"


def parse_version(s: str) -> tuple:
    nums = [int(n) for n in re.findall(r"\d+", s or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"RAW-Review-Updater/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def _changelog_section(text: str, version: str) -> str:
    """The '## v1.2.3' section of CHANGELOG.md for this version."""
    m = re.search(r"^##\s*v?" + re.escape(version) + r"\b.*?$(.*?)(?=^##\s|\Z)", text or "", re.M | re.S)
    return m.group(1).strip() if m else ""


def _from_main(timeout: float) -> dict:
    with _open(RAW_MAIN + "version.py", timeout) as r:
        vtext = r.read().decode("utf-8", "replace")
    m = re.search(r"__version__\s*=\s*[\'\"]([^\'\"]+)", vtext)
    if not m:
        raise RuntimeError("Couldn't read the version on GitHub.")
    ver = m.group(1)
    notes = ""
    try:
        with _open(RAW_MAIN + "CHANGELOG.md", timeout) as r:
            notes = _changelog_section(r.read().decode("utf-8", "replace"), ver)
    except Exception:  # noqa: BLE001 - notes are optional
        pass
    return {"version": ver, "tag": "v" + ver, "notes": notes, "url": MAIN_ZIP,
            "page": REPO_PAGE, "newer": parse_version(ver) > parse_version(__version__)}


def check_latest(timeout: float = 15) -> dict:
    """Newest version on GitHub: a published Release, or the main branch - whichever is newer."""
    found = []
    try:
        found.append(_from_release(timeout))
    except urllib.error.HTTPError as ex:
        if ex.code != 404:              # 404 = no release published yet
            raise
    if FROZEN:                          # the program can only install a built release, not the source code
        if not found:
            raise RuntimeError("No release is published yet.")
        if found[0]["newer"] and not found[0]["url"]:
            raise RuntimeError(f"v{found[0]['version']} is out, but its Windows program isn't ready yet. "
                               "Try again in a few minutes.")
        return found[0]
    try:
        found.append(_from_main(timeout))
    except Exception:
        if not found:
            raise
    return max(found, key=lambda c: parse_version(c["version"]))   # ties keep the Release


def _from_release(timeout: float) -> dict:
    with _open(API_LATEST, timeout) as r:
        data = json.load(r)
    tag = data.get("tag_name") or ""
    assets = data.get("assets", [])
    zips = [a for a in assets if a.get("name", "").lower().endswith(".zip")]
    windows = [a for a in zips if "windows" in a["name"].lower()]
    source = [a for a in zips if "windows" not in a["name"].lower()]
    pick = windows if FROZEN else source
    zip_asset = pick[0]["browser_download_url"] if pick else None
    sums = next((a["browser_download_url"] for a in assets if a.get("name") == "SHA256SUMS"), None)
    return {
        "asset_name": pick[0]["name"] if pick else "",
        "sums": sums,
        "version": tag.lstrip("vV"),
        "tag": tag,
        "notes": (data.get("body") or "").strip(),
        "url": zip_asset or (None if FROZEN else data.get("zipball_url")),
        "page": data.get("html_url") or RELEASES_PAGE,
        "newer": parse_version(tag) > parse_version(__version__),
    }


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _check_sum(path: str, info: dict, timeout: float = 30) -> None:
    """Compare the download with the SHA256SUMS file published with the release (when there is one)."""
    if not info.get("sums") or not info.get("asset_name"):
        return
    with _open(info["sums"], timeout) as r:
        listed = r.read().decode("utf-8", "replace")
    want = next((ln.split()[0].lower() for ln in listed.splitlines()
                 if ln.strip().endswith(info["asset_name"])), None)
    if not want:
        return
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    if h.hexdigest() != want:
        raise RuntimeError("The download doesn't match its published fingerprint, so it wasn't installed.")


_pending = {"script": None}


def _install_program(info: dict, progress) -> str:
    """RAW Review.exe: unpack the new Windows build beside the app; restart() puts it in place."""
    work = os.path.join(APP_DIR, ".update_download")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    progress("Downloading update…")
    zpath = os.path.join(work, "update.zip")
    with _open(info["url"], 300) as r, open(zpath, "wb") as f:
        shutil.copyfileobj(r, f)
    progress("Checking update…")
    _check_sum(zpath, info)
    new = os.path.join(work, "new")
    with zipfile.ZipFile(zpath) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError(f"The download is damaged ({bad}).")
        for n in z.namelist():                        # nothing may land outside the download folder
            if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/"):
                raise RuntimeError("The download doesn't look like a RAW Review release.")
        z.extractall(new)
    root = next((r for r, _d, files in os.walk(new) if EXE_NAME in files), None)
    if root is None:
        raise RuntimeError("The download doesn't look like a RAW Review release.")
    exe = os.path.join(APP_DIR, EXE_NAME)
    log = os.path.join(work, "update_failed.txt")
    _pending["script"] = "\r\n".join([
        "@echo off", "chcp 65001 >nul",
        ":wait",                                          # until RAW Review has closed
        f'tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul',
        "if not errorlevel 1 ( ping -n 2 127.0.0.1 >nul & goto wait )",
        f'robocopy "{root}" "{APP_DIR}" /E /R:5 /W:1 /NFL /NDL /NJH /NJS /NP >nul',
        f'if errorlevel 8 ( echo The update could not be copied into "{APP_DIR}". > "{log}" & goto start )',
        f'rd /s /q "{work}"',
        ":start",
        f'start "" "{exe}"',
        '(goto) 2>nul & del "%~f0"', ""])
    progress("Ready: RAW Review will restart to finish.")
    return work


def install(info: dict, progress=lambda msg: None) -> str:
    """Download and install the release described by `info`. Returns the backup folder."""
    if FROZEN:
        return _install_program(info, progress)
    tmp = tempfile.mkdtemp(prefix="rawreview-update-")
    try:
        progress("Downloading update…")
        zpath = os.path.join(tmp, "update.zip")
        with _open(info["url"], 120) as r, open(zpath, "wb") as f:
            shutil.copyfileobj(r, f)

        progress("Checking update…")
        staged = os.path.join(tmp, "new")
        os.makedirs(staged)
        files = []
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            main = next((n for n in names if n.rsplit("/", 1)[-1] == MAIN_FILE), None)
            if main is None:
                raise RuntimeError("The download doesn't look like a RAW Review release.")
            prefix = main[: -len(MAIN_FILE)]
            for n in names:
                if not n.startswith(prefix) or n.endswith("/"):
                    continue
                rel = n[len(prefix):]
                if "/" in rel or "\\" in rel or rel.startswith(".") or ".." in rel:
                    continue                      # top-level app files only
                if os.path.splitext(rel)[1].lower() not in ALLOWED_EXTS:
                    continue
                with z.open(n) as src, open(os.path.join(staged, rel), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                files.append(rel)

        for rel in files:                         # refuse broken Python files
            if rel.endswith(".py"):
                compile(_read(os.path.join(staged, rel)) or "", rel, "exec")

        old_req = _read(os.path.join(APP_DIR, "requirements.txt"))

        progress("Backing up current version…")
        backup = os.path.join(APP_DIR, ".update_backup", __version__)
        os.makedirs(backup, exist_ok=True)
        for rel in files:
            cur = os.path.join(APP_DIR, rel)
            if os.path.isfile(cur):
                shutil.copy2(cur, os.path.join(backup, rel))

        progress("Installing…")
        for rel in files:
            dst = os.path.join(APP_DIR, rel)
            shutil.copy2(os.path.join(staged, rel), dst + ".new")   # same folder -> atomic swap
            os.replace(dst + ".new", dst)

        new_req = _read(os.path.join(APP_DIR, "requirements.txt"))
        if new_req and new_req != old_req:
            progress("Installing new libraries…")
            flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
            subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                            os.path.join(APP_DIR, "requirements.txt")],
                           check=False, creationflags=flags)
        return backup
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restart():
    """Start the (updated) app again. The caller then closes the current window."""
    if FROZEN:
        script = _pending["script"]
        if script:                                    # copy the new program in once this one has closed
            path = os.path.join(os.path.dirname(APP_DIR), "raw_review_update.cmd")
            if not os.access(os.path.dirname(path), os.W_OK):
                path = os.path.join(tempfile.gettempdir(), "raw_review_update.cmd")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(script)
            subprocess.Popen(["cmd", "/c", path], creationflags=0x08000000 | 0x00000200,
                             close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen([sys.executable], cwd=APP_DIR)
        return
    subprocess.Popen([sys.executable, os.path.join(APP_DIR, MAIN_FILE)], cwd=APP_DIR)
