"""Raw external-payload archive. Every API/WS payload goes through here BEFORE parsing."""

import gzip
import time
from pathlib import Path

ARCHIVE_ROOT = Path("data/raw")


def archive_raw(category: str, payload: bytes, ext: str = "json") -> Path:
    """Write payload to data/raw/{category}/{YYYY-MM-DD}/{ts_ms}.{ext}.gz.

    Returns the path written.
    """
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    ts_ms = int(now * 1000)
    out_dir = ARCHIVE_ROOT / category / day
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ts_ms}.{ext}.gz"
    with gzip.open(out_path, "wb") as f:
        f.write(payload)
    return out_path
