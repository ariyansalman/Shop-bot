"""Database initialization script with default data."""

from database.db import get_db_session, init_db
from database.models import Settings, Admin, AdminRole
from config.settings import settings as app_settings


def create_default_settings():
    """Create default settings record if it doesn't exist."""
    with get_db_session() as session:
        settings = session.query(Settings).first()
        if not settings:
            settings = Settings(
                welcome_message="Welcome to our Digital Products Store!\n\nBrowse our collection of premium software keys and digital downloads.",
                support_username="",
                channel_username=""
            )
            session.add(settings)
            print("[OK] Default settings created")
        else:
            print("[OK] Settings already exist")


def seed_owner_admin():
    """Seed the .env ADMIN_TELEGRAM_ID as the first OWNER.

    Only runs when the admins table is empty, so it preserves the previous
    single-admin .env behaviour for anyone upgrading, and never fights with
    admins added later through the "Manage Admins" screen.
    """
    env_admin_id = getattr(app_settings, 'ADMIN_TELEGRAM_ID', 0)
    if not env_admin_id:
        print("[..] No ADMIN_TELEGRAM_ID configured, skipping owner seed")
        return

    with get_db_session() as session:
        if session.query(Admin).count() > 0:
            print("[OK] Admins already exist")
            return

        session.add(Admin(
            telegram_id=env_admin_id,
            username=(getattr(app_settings, 'ADMIN_TELEGRAM_USERNAME', '') or None),
            role=AdminRole.OWNER,
        ))
        print(f"[OK] Seeded owner admin from .env (telegram_id={env_admin_id})")


def initialize_database():
    """Initialize database with tables and default data."""
    print("Initializing database...")
    init_db()
    create_default_settings()
    seed_owner_admin()
    print("[OK] Database initialization complete")


if __name__ == "__main__":
    initialize_database()
