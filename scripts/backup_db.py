"""Create a compressed dump of the bot's database.

Usage (standalone, e.g. from a Railway scheduled job or local cron):

    python scripts/backup_db.py                 # writes into STORAGE_DIR/backups
    python scripts/backup_db.py --output-dir /tmp

Postgres (production): shells out to `pg_dump` against DATABASE_URL and
gzips the result. `pg_dump` must be available on PATH — on Railway's default
Nixpacks Python image it is not installed by default, so add it via a
`nixpacks.toml` with `nixPkgs = ["...", "postgresql"]`, or use the Docker
image of your choice that includes the Postgres client tools.

SQLite (local dev): uses SQLite's own online backup API (safe to run while
the bot is writing) and gzips the copy.

The bot also calls create_backup() from a daily JobQueue job in bot.py and
sends the resulting file to the admin's DMs, so Telegram doubles as the
off-site storage. Nothing here implements retention or incremental backups
by design — it's one full dump per run.
"""

import argparse
import gzip
import os
import shutil
import subprocess
import sys
from datetime import datetime

# Allow running this file directly as `python scripts/backup_db.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings  # noqa: E402


class BackupError(RuntimeError):
    """Raised when a backup could not be produced."""


def _timestamp() -> str:
    return datetime.utcnow().strftime('%Y%m%d-%H%M%S')


def default_backup_dir() -> str:
    """Backups live under STORAGE_DIR so they land on the Railway volume."""
    return settings.BACKUPS_DIR


def _gzip_file(source_path: str, dest_path: str) -> None:
    with open(source_path, 'rb') as src, gzip.open(dest_path, 'wb') as dst:
        shutil.copyfileobj(src, dst)


def _backup_sqlite(output_dir: str) -> str:
    import sqlite3

    # 'sqlite:///bot_database.db' -> 'bot_database.db'
    db_path = settings.DATABASE_URL.split('///', 1)[-1]
    if not os.path.exists(db_path):
        raise BackupError(f"SQLite database not found at {db_path}")

    raw_copy = os.path.join(output_dir, f"backup-{_timestamp()}.sqlite")
    # Online backup API: consistent snapshot even with the bot running.
    source = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(raw_copy)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    final_path = raw_copy + '.gz'
    _gzip_file(raw_copy, final_path)
    os.remove(raw_copy)
    return final_path


def _backup_postgres(output_dir: str) -> str:
    if shutil.which('pg_dump') is None:
        raise BackupError(
            "pg_dump is not installed or not on PATH. Install the Postgres "
            "client tools in the deployment image (see this file's docstring)."
        )

    raw_dump = os.path.join(output_dir, f"backup-{_timestamp()}.sql")
    # pg_dump understands the libpq URL directly. SQLAlchemy-style drivers
    # ('postgresql+psycopg2://') are not valid libpq URLs, so strip them.
    url = settings.DATABASE_URL
    if url.startswith('postgresql+'):
        url = 'postgresql://' + url.split('://', 1)[1]

    with open(raw_dump, 'wb') as out:
        proc = subprocess.run(
            ['pg_dump', '--no-owner', '--no-privileges', '--dbname', url],
            stdout=out,
            stderr=subprocess.PIPE,
            timeout=600,
        )
    if proc.returncode != 0:
        if os.path.exists(raw_dump):
            os.remove(raw_dump)
        stderr = proc.stderr.decode('utf-8', 'replace').strip()
        raise BackupError(f"pg_dump failed (exit {proc.returncode}): {stderr}")

    final_path = raw_dump + '.gz'
    _gzip_file(raw_dump, final_path)
    os.remove(raw_dump)
    return final_path


def create_backup(output_dir: str | None = None) -> str:
    """Create a gzipped dump and return the path to the created file.

    Raises BackupError if the dump could not be produced.
    """
    output_dir = output_dir or default_backup_dir()
    os.makedirs(output_dir, exist_ok=True)

    if settings.DATABASE_URL.startswith('sqlite'):
        return _backup_sqlite(output_dir)
    return _backup_postgres(output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the bot database.")
    parser.add_argument(
        '--output-dir',
        default=None,
        help="Directory to write the dump into (default: STORAGE_DIR/backups)",
    )
    args = parser.parse_args()

    try:
        path = create_backup(args.output_dir)
    except BackupError as exc:
        print(f"[ERROR] Backup failed: {exc}", file=sys.stderr)
        return 1

    size_mb = os.path.getsize(path) / (1024 * 1024)
    print(f"[OK] Backup written to {path} ({size_mb:.2f} MB)")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
