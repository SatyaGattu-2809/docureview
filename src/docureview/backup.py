"""Create a consistent SQLite snapshot without copying a live WAL file."""

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def backup(source: Path, destination: Path):
    if not source.is_file():
        raise ValueError("Source database must exist")
    # Exclusive creation prevents accidental replacement, including symlink targets.
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Snapshot integrity check failed")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    backup(args.source, args.destination)
    print("Snapshot created and integrity checked; protect it as sensitive data.")


if __name__ == "__main__":
    main()
