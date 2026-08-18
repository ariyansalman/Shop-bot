"""Database models for the Telegram digital products store bot."""

from sqlalchemy import Column, Integer, String, Numeric, Boolean, DateTime, ForeignKey, Text, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

Base = declarative_base()

# All money columns use Numeric(12, 2) instead of Float. SQLAlchemy maps this
# to Python's decimal.Decimal, which avoids the binary floating-point rounding
# errors that accumulate with repeated add/subtract on a Float column (e.g.
# wallet top-ups and purchases over time). 12 total digits / 2 decimal places
# supports balances up to 9,999,999,999.99 — comfortably more than a store
# like this will ever need.
Money = lambda: Numeric(12, 2, asdecimal=True)  # noqa: E731


class ProductType(enum.Enum):
    """Enum for product types."""
    KEY = "key"
    FILE = "file"


class OrderStatus(enum.Enum):
    """Enum for order status."""
    PROCESSING = "Processing"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"


class DisputeStatus(enum.Enum):
    """Enum for dispute status."""
    NIL = "NIL"
    OPENED = "Opened"
    RESOLVED = "Resolved"


class TransactionStatus(enum.Enum):
    """Enum for transaction/payment status."""
    PENDING = "pending"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"
    # Manual balance change applied by an admin (no external payment involved).
    # Rows with this status are always already applied to the wallet — they are
    # never pending, so the payment-polling jobs skip them.
    ADMIN_ADJUSTMENT = "admin_adjustment"


class PaymentMethod(enum.Enum):
    """Enum for payment methods.

    Storage note: ``Column(Enum(PaymentMethod))`` persists the member *name*
    (``CRYPTO_WALLET``), not the member value (``crypto_wallet``). The values
    below are display/label strings only (see admin exports and receipts).
    Anything that touches the database — filters, inserts, comparisons,
    Postgres ``ALTER TYPE`` statements — must use the member or its ``.name``,
    never the ``.value``.
    """

    CRYPTO_WALLET = "crypto_wallet"
    CARD = "card"
    BINANCE_PAY = "binance_pay"
    BYBIT_PAY = "bybit_pay"
    BKASH_NAGAD = "bkash_nagad"
    # Not a real payment rail: marks a wallet credit/debit made by an admin
    # from the user detail screen, so every wallet change is auditable from
    # the transactions table.
    ADMIN_ADJUSTMENT = "admin_adjustment"
    # Not a real payment rail either: marks a wallet credit (or its reversal)
    # produced by the referral program, so referral money is visible in the
    # same transaction history as every other wallet movement.
    REFERRAL_REWARD = "referral_reward"



class AdminRole(enum.Enum):
    """Enum for admin roles.

    OWNER can do everything, including managing other admins, adjusting
    wallet balances and viewing audit data. STAFF is limited to day-to-day
    store operations (products, orders, broadcasts).
    """
    OWNER = "owner"
    STAFF = "staff"


class Admin(Base):
    """An admin of the bot. Replaces the single ADMIN_TELEGRAM_ID model.

    The .env ADMIN_TELEGRAM_ID is seeded into this table as the first OWNER
    on startup (see database/init_data.py), so existing deployments keep
    working after upgrading without any manual step.
    """
    __tablename__ = 'admins'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    role = Column(Enum(AdminRole), nullable=False, default=AdminRole.STAFF)
    # Who added this admin. Nullable: the seeded .env owner has no adder.
    added_by = Column(Integer, ForeignKey('admins.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    adder = relationship("Admin", remote_side=[id], backref="added_admins")


class DiscountType(enum.Enum):
    """Enum for discount code types."""
    PERCENTAGE = "percentage"
    FIXED_AMOUNT = "fixed_amount"


class User(Base):
    """User model for storing customer information."""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String(255))
    wallet_balance = Column(Money(), default=0)
    is_banned = Column(Boolean, default=False)
    language_code = Column(String(10), default='en', nullable=False, server_default='en')
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    orders = relationship("Order", back_populates="user")
    cart_items = relationship("Cart", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user")
    reviews = relationship("Review", back_populates="user", cascade="all, delete-orphan")


class Category(Base):
    """Category model for product organization."""
    __tablename__ = 'categories'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    products = relationship("Product", back_populates="category")
    subcategories = relationship("Subcategory", back_populates="category")


class Subcategory(Base):
    """Subcategory model for additional product organization."""
    __tablename__ = 'subcategories'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    category = relationship("Category", back_populates="subcategories")
    products = relationship("Product", back_populates="subcategory")


class Product(Base):
    """Product model for items available for purchase."""
    __tablename__ = 'products'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    price = Column(Money(), nullable=False)
    stock_count = Column(Integer, default=0)
    # When stock_count falls to or below this value, admins get a low-stock alert.
    low_stock_threshold = Column(Integer, default=3, nullable=False, server_default='3')
    product_type = Column(Enum(ProductType), nullable=False)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    subcategory_id = Column(Integer, ForeignKey('subcategories.id'), nullable=True)
    image_path = Column(String(500), nullable=True)
    download_link = Column(String(500), nullable=True)  # For file-type products
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    category = relationship("Category", back_populates="products")
    subcategory = relationship("Subcategory", back_populates="products")
    product_keys = relationship("ProductKey", back_populates="product", cascade="all, delete-orphan")
    cart_items = relationship("Cart", back_populates="product")
    order_items = relationship("OrderItem", back_populates="product")


class ProductKey(Base):
    """SEPARATE TABLE for storing product keys inventory."""
    __tablename__ = 'product_keys'

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    key_value = Column(Text, nullable=False)
    is_sold = Column(Boolean, default=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    sold_at = Column(DateTime, nullable=True)

    # Relationships
    product = relationship("Product", back_populates="product_keys")
    order = relationship("Order", back_populates="assigned_keys")


class Cart(Base):
    """Shopping cart model for temporary product storage."""
    __tablename__ = 'cart'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    quantity = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="cart_items")
    product = relationship("Product", back_populates="cart_items")


class Order(Base):
    """Order model for purchase records."""
    __tablename__ = 'orders'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    total_amount = Column(Money(), nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.PROCESSING)
    dispute_status = Column(Enum(DisputeStatus), default=DisputeStatus.NIL)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="orders")
    order_items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    assigned_keys = relationship("ProductKey", back_populates="order")
    disputes = relationship("Dispute", back_populates="order", cascade="all, delete-orphan")
    review = relationship("Review", back_populates="order", uselist=False, cascade="all, delete-orphan")


class OrderItem(Base):
    """Order items model for individual line items in orders."""
    __tablename__ = 'order_items'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Money(), nullable=False)
    delivered_asset = Column(Text, nullable=True)  # Keys or download link
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    order = relationship("Order", back_populates="order_items")
    product = relationship("Product", back_populates="order_items")


class Transaction(Base):
    """Transaction model for wallet funding history."""
    __tablename__ = 'transactions'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    amount = Column(Money(), nullable=False)
    payment_method = Column(Enum(PaymentMethod), nullable=False)
    # Indexed: the payment webhooks look a transaction up by the provider
    # invoice id stored here as "<invoice_id>|<pay_url>", using a SQL prefix
    # match. Without the index that lookup degraded into loading every pending
    # transaction and scanning it in Python.
    crypto_address = Column(String(500), nullable=True, index=True)
    status = Column(Enum(TransactionStatus), default=TransactionStatus.PENDING, index=True)
    # User-submitted proof for Binance Pay / Bybit Pay: the Binance transactionId,
    # Bybit internal transfer txID, or an on-chain txid. Unique so the same
    # reference can never be redeemed against two different transactions
    # (blocks a user replaying one real payment across multiple top-ups).
    external_reference = Column(String(255), nullable=True, unique=True, index=True)
    # Free-text reason supplied by the admin for a manual balance adjustment
    # (ADMIN_ADJUSTMENT rows). NULL for ordinary payment-triggered rows.
    admin_note = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="transactions")


class DiscountCode(Base):
    """Discount code model for promotional codes redeemable at checkout."""
    __tablename__ = 'discount_codes'

    id = Column(Integer, primary_key=True)
    # Always stored uppercase; callers uppercase buyer input before querying
    # so lookups are effectively case-insensitive without a citext column.
    code = Column(String(50), unique=True, nullable=False, index=True)
    discount_type = Column(Enum(DiscountType), nullable=False)
    value = Column(Money(), nullable=False)
    max_uses = Column(Integer, nullable=True)  # None = unlimited
    times_used = Column(Integer, default=0, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Review(Base):
    """A star rating submitted by a customer for a completed order."""
    __tablename__ = 'reviews'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, unique=True, index=True)
    rating = Column(Integer, nullable=False)
    reviewer_username = Column(String(255), nullable=True)
    is_published = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="reviews")
    order = relationship("Order", back_populates="review")


DEFAULT_FAQ_TEXT = (
    "❓ Frequently Asked Questions\n\n"
    "💳 Which payment methods do you accept?\n"
    "Wallet top-ups can be made with CryptoBot, card, Binance Pay, Bybit Pay "
    "and bKash / Nagad / Rocket, depending on what is enabled in the store.\n\n"
    "⏱ How long does delivery take?\n"
    "Digital products are delivered instantly in chat right after your order "
    "is paid from your wallet balance. If something goes wrong, the order "
    "stays visible in your Order History.\n\n"
    "↩️ What is your refund policy?\n"
    "Delivered keys and downloads are non-refundable once revealed. If a key "
    "or file does not work, open a dispute on the order and support will "
    "review it and refund your wallet where appropriate.\n\n"
    "🔎 How does top-up verification work?\n"
    "After you pay, the bot checks the payment with the provider. Automatic "
    "methods credit your wallet within a few minutes; manual methods need the "
    "payment reference you submit and are confirmed by an admin."
)


class Settings(Base):
    """Settings model for store configuration (single row table)."""
    __tablename__ = 'settings'

    id = Column(Integer, primary_key=True)
    welcome_message = Column(Text, default="Welcome to our digital store!")
    store_logo_path = Column(String(500), nullable=True)
    support_username = Column(String(255), nullable=True)
    channel_username = Column(String(255), nullable=True)
    faq_text = Column(Text, default=DEFAULT_FAQ_TEXT)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Broadcast(Base):
    """Broadcast model for tracking broadcast messages."""
    __tablename__ = 'broadcasts'

    id = Column(Integer, primary_key=True)
    message_text = Column(Text, nullable=False)
    image_path = Column(String(500), nullable=True)
    sent_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Dispute(Base):
    """Dispute model for order disputes."""
    __tablename__ = 'disputes'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    reason = Column(Text, nullable=False)
    status = Column(Enum(DisputeStatus), default=DisputeStatus.OPENED)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
    admin_notes = Column(Text, nullable=True)

    # Relationships
    order = relationship("Order", back_populates="disputes")
    user = relationship("User")


class AdminActionLog(Base):
    """Audit trail of state-changing actions performed by admins.

    One row per admin-triggered change (ban, order cancellation, product
    edit, broadcast, settings change, ...). Written by
    utils.helpers.log_admin_action() right after the change itself commits,
    so a row here always describes a change that actually landed.

    admin_telegram_id is stored as a plain integer rather than a FK to
    admins.id on purpose: the log must survive an admin being removed from
    the admins table, and the .env-seeded owner is not guaranteed to have a
    row at the time an action runs.
    """
    __tablename__ = 'admin_action_logs'

    id = Column(Integer, primary_key=True)
    admin_telegram_id = Column(Integer, nullable=False, index=True)
    # Short machine-readable verb, e.g. "ban_user", "cancel_order",
    # "adjust_balance". Kept stable so the audit screen can filter on it.
    action = Column(String(64), nullable=False, index=True)
    # What kind of thing was acted on: "user", "order", "product",
    # "category", "subcategory", "settings", "broadcast", ...
    target_type = Column(String(32), nullable=True)
    # Primary key of the target row (or the Telegram ID for user targets
    # reached by Telegram ID). Nullable for targets with no single row.
    target_id = Column(Integer, nullable=True)
    # Free-form JSON or text describing what changed.
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ---------------------------------------------------------------------------
# Referral program
# ---------------------------------------------------------------------------


class ReferralStatus(enum.Enum):
    """Lifecycle of a referral relationship."""
    # Referred user joined through the link but has not qualified yet.
    PENDING = "pending"
    # At least one qualifying purchase produced a credited reward.
    QUALIFIED = "qualified"
    # Relationship disabled by an admin (kept for audit, never deleted).
    REVOKED = "revoked"


class ReferralRewardStatus(enum.Enum):
    """Lifecycle of a single referral reward payment."""
    CREDITED = "credited"
    REVOKED = "revoked"


class ReferralRewardType(enum.Enum):
    """How the reward value is interpreted."""
    FIXED = "fixed"
    PERCENTAGE = "percentage"


class Referral(Base):
    """One referrer -> referred-user relationship.

    ``referred_user_id`` is UNIQUE: this is the database-level guarantee that
    a user can only ever have a single referrer and that a later referral
    link can never overwrite the first one.
    """
    __tablename__ = 'referrals'

    id = Column(Integer, primary_key=True)
    referrer_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    referred_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, unique=True, index=True)
    status = Column(Enum(ReferralStatus), default=ReferralStatus.PENDING, nullable=False, index=True)
    # First order that qualified this referral (informational).
    qualifying_order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    # Atomic "first purchase" claim. The order that won the race to become
    # this referral's first qualifying purchase. UNIQUE + a conditional
    # UPDATE (... WHERE first_reward_order_id IS NULL) make first_purchase_only
    # enforceable by the database instead of by a Python-side count, so two
    # concurrent order completions can never both pay a first-purchase reward.
    first_reward_order_id = Column(Integer, ForeignKey('orders.id'),
                                   nullable=True, unique=True, index=True)
    # Running total of rewards currently credited for this relationship.
    total_rewarded = Column(Money(), default=0, nullable=False, server_default='0')
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    qualified_at = Column(DateTime, nullable=True)


class ReferralReward(Base):
    """A single reward payment produced by a qualifying order.

    ``order_id`` is UNIQUE: one qualifying event can never pay out twice, no
    matter how many duplicate callbacks or webhook retries arrive.
    """
    __tablename__ = 'referral_rewards'

    id = Column(Integer, primary_key=True)
    referral_id = Column(Integer, ForeignKey('referrals.id'), nullable=False, index=True)
    referrer_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    referred_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, unique=True, index=True)
    amount = Column(Money(), nullable=False)
    status = Column(Enum(ReferralRewardStatus), default=ReferralRewardStatus.CREDITED,
                    nullable=False, index=True)
    # Wallet transaction created for this reward (audit link).
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)
    revoke_reason = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    revoked_at = Column(DateTime, nullable=True)


class ReferralSettings(Base):
    """Admin-configurable referral program settings (single row table)."""
    __tablename__ = 'referral_settings'

    id = Column(Integer, primary_key=True)
    is_enabled = Column(Boolean, default=False, nullable=False, server_default='0')
    reward_type = Column(Enum(ReferralRewardType), default=ReferralRewardType.FIXED,
                         nullable=False)
    # Fixed: an absolute amount. Percentage: percent of the qualifying order.
    reward_value = Column(Money(), default=0, nullable=False, server_default='0')
    # Orders below this total never qualify.
    min_purchase_amount = Column(Money(), default=0, nullable=False, server_default='0')
    # True: only the referred user's first completed order pays a reward.
    first_purchase_only = Column(Boolean, default=True, nullable=False, server_default='1')
    # Optional cap; NULL means no cap.
    max_reward_amount = Column(Money(), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
