"""One-time downloads for the offline pipeline. Not imported at request time."""
from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


def download_if_missing(url: str, dest: Path, user_agent: str, force: bool = False) -> Path:
    """Fetch `url` to `dest` unless it already exists. Returns `dest`.

    Downloads to a .part file and renames on success, so an interrupted run
    cannot leave a truncated archive that a later run would treat as complete.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0 and not force:
        logger.info("cached: %s (%.1f MiB)", dest.name, dest.stat().st_size / 2**20)
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    logger.info("downloading %s -> %s", url, dest.name)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=300) as response, tmp.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    tmp.replace(dest)
    logger.info("downloaded %s (%.1f MiB)", dest.name, dest.stat().st_size / 2**20)
    return dest
