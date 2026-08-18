"""Pre-deploy migration entry point (Railway `preDeployCommand`).

Runs exactly the same additive, idempotent sequence the bot runs at startup:
FK pre-flight -> create_all(checkfirst=True) -> migrations.run_all, in strict
mode. Nothing is dropped, truncated or recreated.

Unlike database.db.init_db(), this ignores SKIP_STARTUP_MIGRATIONS: it is the
place the work is supposed to happen. Exits non-zero on failure so Railway
aborts the deploy and keeps the previous version running.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO)


def main() -> int:
    from database.db import engine
    from database.models import Base
    from migrations.fk_preflight import migrate as fk_preflight
    from migrations.run_all import run_all, MigrationError

    try:
        fk_preflight(engine, strict=True)
        Base.metadata.create_all(engine, checkfirst=True)
        print("[OK] tables present (additive create_all)")
        run_all(engine, strict=True)
    except MigrationError as exc:
        print(f"[FATAL] {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        logging.exception("pre-deploy migration failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
