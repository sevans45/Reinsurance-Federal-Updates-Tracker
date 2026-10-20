"""Extract only the expected database and validate before replacing local state."""
from contextlib import closing
import os
import sqlite3
import sys
import zipfile
from pathlib import Path


def restore(archive, destination="reinsurance_updates.db"):
    target = Path(destination)
    temporary = target.with_suffix(".restore.db")
    try:
        with zipfile.ZipFile(archive) as zipped:
            if zipped.namelist() != ["reinsurance_updates.db"]:
                raise ValueError("Unexpected database artifact contents")
            temporary.write_bytes(zipped.read("reinsurance_updates.db"))
        with closing(sqlite3.connect(temporary)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Database integrity check failed")
            conn.execute("SELECT id, source, title, url, date_published, date_added, keywords FROM updates LIMIT 1")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    restore(sys.argv[1])
