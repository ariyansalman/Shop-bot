"""Database connection and session management."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from contextlib import contextmanager
from config.settings import settings
from database.models import Base

# Create database engine.
#
# pool_pre_ping=True: issues a lightweight "SELECT 1" before handing out a
# pooled connection, and transparently reconnects if it's dead. This matters
# a lot on Supabase — it closes idle Postgres connections server-side after
# a few minutes, and this bot is a long-running worker that can easily go
# that long between DB hits (e.g. overnight with no orders).
#
# pool_recycle=280: proactively recycle connections before ~5 minutes, under
# most managed Postgres idle-timeout thresholds, as a second line of defense.
#
# These options are harmless no-ops for SQLite (used in local dev).
engine_kwargs = {"echo": False}
if not settings.DATABASE_URL.startswith("sqlite"):
    engine_kwargs.update(pool_pre_ping=True, pool_recycle=280)

engine = create_engine(settings.DATABASE_URL, **engine_kwargs)

# Create session factory
SessionFactory = sessionmaker(bind=engine)
Session = scoped_session(SessionFactory)


def init_db():
    """Initialize the database: reconcile FKs, create missing tables, then migrate.

    The pre-flight step runs *before* ``create_all()`` on purpose. ``create_all``
    emits ``CREATE TABLE child (... FOREIGN KEY(user_id) REFERENCES users(id))``
    for tables that don't exist yet, and Postgres aborts with
    ``column "id" referenced in foreign key constraint does not exist`` whenever
    the live parent table's real primary key is named something else. The
    pre-flight makes every referenced column exist (additively) and aligns
    INTEGER/BIGINT widths in the model metadata first, so the CREATE TABLE
    statements are valid against the *actual* database.
    """
    from migrations.fk_preflight import migrate as fk_preflight

    # strict=True: if an FK target still cannot be made to exist, this raises
    # SchemaMismatchError with a precise diagnostic *before* create_all() can
    # abort with Postgres' opaque "column \"id\" referenced in foreign key
    # constraint does not exist". Nothing is dropped or reset either way.
    fk_preflight(engine, strict=True)
    # create_all(checkfirst=True) is additive only: it creates tables that do
    # not exist yet, in topological dependency order (parents before children),
    # and never touches, recreates or overwrites an existing table. The
    # migrations below own every change to tables that already exist.
    Base.metadata.create_all(engine, checkfirst=True)
    print("[OK] Database tables created successfully")
    _run_lightweight_migrations()


def _run_lightweight_migrations():
    """Bring an already-existing database up to date with the models.

    Base.metadata.create_all() only creates tables that don't exist yet — it
    never ALTERs an existing table's columns, never adds a missing index, and
    on Postgres it never adds values to an existing native ENUM type. Every
    schema change made after a deployment went live therefore has to be
    applied here.

    All of that now lives in migrations/run_all.py, which applies each
    historical migration plus a model-driven schema sync derived from
    Base.metadata itself. Every step is additive and idempotent:
    nothing is dropped, recreated or deleted, so this is safe to run on
    every startup against a live production database.
    """
    import os

    # Railway (and any platform with a pre-deploy step) should run the
    # migration chain ONCE per deploy via `python migrations/run_all.py`
    # instead of on every container start. Set SKIP_STARTUP_MIGRATIONS=true on
    # such a service to skip the (identical, idempotent) work here and cut
    # cold-start time. Default is unchanged: migrations run at startup.
    if os.environ.get("SKIP_STARTUP_MIGRATIONS", "").strip().lower() in ("1", "true", "yes"):
        print("[SKIP] startup migrations disabled (SKIP_STARTUP_MIGRATIONS=true); "
              "they must be applied by the pre-deploy command")
        return

    from migrations.run_all import run_all

    run_all(engine)


@contextmanager
def get_db_session():
    """Provide a transactional scope for database operations."""
    session = Session()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()
