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
import zipfile

from version import GITHUB_REPO, __version__

APP_DIR = os.path.dirname(os.path.abspath(__file__))
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
    zip_asset = next((a["browser_download_url"] for a in data.get("assets", [])
                      if a.get("name", "").lower().endswith(".zip")), None)
    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "notes": (data.get("body") or "").strip(),
        "url": zip_asset or data.get("zipball_url"),
        "page": data.get("html_url") or RELEASES_PAGE,
        "newer": parse_version(tag) > parse_version(__version__),
    }


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def install(info: dict, progress=lambda msg: None) -> str:
    """Download and install the release described by `info`. Returns the backup folder."""
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
    subprocess.Popen([sys.executable, os.path.join(APP_DIR, MAIN_FILE)], cwd=APP_DIR)
