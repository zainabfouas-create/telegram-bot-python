import asyncpg
from config import DATABASE_URL

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                language TEXT NOT NULL DEFAULT 'ar',
                balance NUMERIC(12,2) NOT NULL DEFAULT 0,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                referred_by INTEGER REFERENCES users(id),
                referral_rewards_claimed INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT,
                price NUMERIC(10,2) NOT NULL,
                delivery_type TEXT NOT NULL DEFAULT 'inventory',
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS inventory (
                id SERIAL PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                is_sold BOOLEAN NOT NULL DEFAULT FALSE,
                order_id INTEGER,
                sold_at TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id),
                product_name TEXT NOT NULL,
                price NUMERIC(10,2) NOT NULL,
                inventory_id INTEGER,
                delivered_content TEXT,
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS balance_transactions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount NUMERIC(10,2) NOT NULL,
                type TEXT NOT NULL,
                description TEXT,
                balance_after NUMERIC(10,2) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS recharge_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount NUMERIC(10,2) NOT NULL,
                method TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                external_ref TEXT,
                telegram_payment_charge_id TEXT UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
