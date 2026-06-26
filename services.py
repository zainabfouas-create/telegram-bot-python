from __future__ import annotations
import asyncpg
from database import get_pool
from config import ADMIN_TELEGRAM_ID, REFERRAL_MILESTONE, REFERRAL_REWARD, STAR_TO_CREDIT
import time


class ServiceError(Exception):
    pass


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_setting(key: str) -> str | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT value FROM settings WHERE key=$1", key)
    return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
        key, value,
    )


async def delete_setting(key: str) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM settings WHERE key=$1", key)


_channel_cache: dict = {"value": None, "expires_at": 0}


async def get_required_channel() -> str | None:
    now = time.time()
    if _channel_cache["expires_at"] > now:
        return _channel_cache["value"]
    value = await get_setting("required_channel")
    _channel_cache["value"] = value
    _channel_cache["expires_at"] = now + 60
    return value


def invalidate_channel_cache() -> None:
    _channel_cache["expires_at"] = 0


# ── Users ─────────────────────────────────────────────────────────────────────

async def get_or_create_user(tg_id: int, username: str | None, first_name: str | None) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM users WHERE telegram_id=$1", tg_id)
    if row:
        return dict(row)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(987654321)")
            row = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", tg_id)
            if row:
                return dict(row)

            is_configured_admin = ADMIN_TELEGRAM_ID != "" and str(tg_id) == ADMIN_TELEGRAM_ID
            is_admin = is_configured_admin
            if not is_configured_admin and ADMIN_TELEGRAM_ID == "":
                count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_admin=TRUE")
                is_admin = count == 0

            row = await conn.fetchrow(
                """INSERT INTO users(telegram_id, username, first_name, language, is_admin)
                   VALUES($1,$2,$3,'en',$4) RETURNING *""",
                tg_id, username, first_name, is_admin,
            )
            return dict(row)


async def get_user_by_id(uid: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM users WHERE id=$1", uid)
    return dict(row) if row else None


async def get_user_by_telegram_id(tg_id: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM users WHERE telegram_id=$1", tg_id)
    return dict(row) if row else None


async def set_user_language(uid: int, lang: str) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE users SET language=$1 WHERE id=$2 RETURNING *", lang, uid
    )
    return dict(row) if row else None


async def set_user_ban(uid: int, banned: bool) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE users SET is_banned=$1 WHERE id=$2 RETURNING *", banned, uid
    )
    return dict(row) if row else None


async def list_users(limit: int = 100) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM users ORDER BY id LIMIT $1", limit)
    return [dict(r) for r in rows]


async def list_admins() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM users WHERE is_admin=TRUE")
    return [dict(r) for r in rows]


# ── Referral ──────────────────────────────────────────────────────────────────

_PENDING_REFERRAL_LIMIT = 30  # max unconfirmed referrals per referrer (anti-flood)

async def process_referral(new_user_id: int, referrer_tg_id: int) -> dict:
    """Record who referred the new user. Reward is NOT given here — it fires
    after the referred user completes their first purchase (see confirm_referral)."""
    null_result = {"rewarded": False, "reward_amount": 0, "referrer_id": None,
                   "referrer_telegram_id": None, "referrer_language": None}
    referrer = await get_user_by_telegram_id(referrer_tg_id)
    if not referrer or referrer["id"] == new_user_id:
        return null_result

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Anti-flood: count pending (unconfirmed) referrals for this referrer
        pending = await conn.fetchval(
            """SELECT COUNT(*) FROM users u
               WHERE u.referred_by=$1
               AND NOT EXISTS (
                   SELECT 1 FROM orders o
                   WHERE o.user_id = u.id AND o.status = 'completed'
               )""",
            referrer["id"],
        )
        if (pending or 0) >= _PENDING_REFERRAL_LIMIT:
            return null_result

        # Just assign referred_by; no reward yet
        assigned = await conn.fetchval(
            "UPDATE users SET referred_by=$1 WHERE id=$2 AND referred_by IS NULL RETURNING id",
            referrer["id"], new_user_id,
        )
        if not assigned:
            return null_result

        return {"rewarded": False, "reward_amount": 0,
                "referrer_id": referrer["id"],
                "referrer_telegram_id": referrer["telegram_id"],
                "referrer_language": referrer["language"]}


async def confirm_referral(buyer_user_id: int) -> dict:
    """Called after a user completes their FIRST purchase.
    If they were referred, check milestones and reward the referrer."""
    null_result = {"rewarded": False, "reward_amount": 0, "referrer_id": None,
                   "referrer_telegram_id": None, "referrer_language": None}
    pool = await get_pool()
    async with pool.acquire() as conn:
        buyer = await conn.fetchrow("SELECT referred_by FROM users WHERE id=$1", buyer_user_id)
        if not buyer or not buyer["referred_by"]:
            return null_result

        # Only fire on first completed order
        order_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE user_id=$1 AND status='completed'",
            buyer_user_id,
        )
        if (order_count or 0) != 1:
            return null_result

        async with conn.transaction():
            ref = await conn.fetchrow(
                "SELECT * FROM users WHERE id=$1 FOR UPDATE", buyer["referred_by"]
            )
            if not ref:
                return null_result

            # Count only confirmed referrals (with at least one completed order)
            ref_count = await conn.fetchval(
                """SELECT COUNT(*) FROM users u
                   WHERE u.referred_by=$1
                   AND EXISTS (
                       SELECT 1 FROM orders o
                       WHERE o.user_id = u.id AND o.status = 'completed'
                   )""",
                ref["id"],
            )

            milestones_earned = ref_count // REFERRAL_MILESTONE
            new_batches = milestones_earned - ref["referral_rewards_claimed"]
            if new_batches <= 0:
                return {"rewarded": False, "reward_amount": 0,
                        "referrer_id": ref["id"],
                        "referrer_telegram_id": ref["telegram_id"],
                        "referrer_language": ref["language"]}

            reward_amount = new_batches * REFERRAL_REWARD
            new_balance = float(ref["balance"]) + reward_amount
            await conn.execute(
                "UPDATE users SET balance=$1, referral_rewards_claimed=$2 WHERE id=$3",
                str(new_balance), milestones_earned, ref["id"],
            )
            await conn.execute(
                """INSERT INTO balance_transactions(user_id,amount,type,description,balance_after)
                   VALUES($1,$2,'referral',$3,$4)""",
                ref["id"], str(reward_amount),
                f"Referral reward ({ref_count} confirmed referrals)", str(new_balance),
            )
            return {"rewarded": True, "reward_amount": reward_amount,
                    "referrer_id": ref["id"],
                    "referrer_telegram_id": ref["telegram_id"],
                    "referrer_language": ref["language"]}


async def get_referral_count(uid: int) -> int:
    """Return count of CONFIRMED referrals (made a purchase)."""
    pool = await get_pool()
    return await pool.fetchval(
        """SELECT COUNT(*) FROM users u
           WHERE u.referred_by=$1
           AND EXISTS (
               SELECT 1 FROM orders o
               WHERE o.user_id = u.id AND o.status = 'completed'
           )""",
        uid,
    ) or 0


async def get_pending_referral_count(uid: int) -> int:
    """Return count of pending referrals (registered but haven't purchased yet)."""
    pool = await get_pool()
    return await pool.fetchval(
        """SELECT COUNT(*) FROM users u
           WHERE u.referred_by=$1
           AND NOT EXISTS (
               SELECT 1 FROM orders o
               WHERE o.user_id = u.id AND o.status = 'completed'
           )""",
        uid,
    ) or 0


# ── Categories ────────────────────────────────────────────────────────────────

async def get_categories(active_only: bool = True) -> list[dict]:
    pool = await get_pool()
    if active_only:
        rows = await pool.fetch(
            "SELECT * FROM categories WHERE is_active=TRUE ORDER BY sort_order, id"
        )
    else:
        rows = await pool.fetch("SELECT * FROM categories ORDER BY sort_order, id")
    return [dict(r) for r in rows]


async def get_category(cid: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM categories WHERE id=$1", cid)
    return dict(row) if row else None


async def create_category(name: str, description: str | None) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO categories(name,description) VALUES($1,$2) RETURNING *", name, description
    )
    return dict(row)


async def update_category(cid: int, fields: dict) -> dict | None:
    pool = await get_pool()
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k}=${len(vals)+1}")
        vals.append(v)
    vals.append(cid)
    row = await pool.fetchrow(
        f"UPDATE categories SET {', '.join(sets)} WHERE id=${len(vals)} RETURNING *", *vals
    )
    return dict(row) if row else None


async def toggle_category(cid: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE categories SET is_active = NOT is_active WHERE id=$1 RETURNING *", cid
    )
    return dict(row) if row else None


async def delete_category(cid: int) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM categories WHERE id=$1", cid)


# ── Products ──────────────────────────────────────────────────────────────────

async def get_products(category_id: int, active_only: bool = True) -> list[dict]:
    pool = await get_pool()
    if active_only:
        rows = await pool.fetch(
            "SELECT * FROM products WHERE category_id=$1 AND is_active=TRUE ORDER BY id",
            category_id,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM products WHERE category_id=$1 ORDER BY id", category_id
        )
    return [dict(r) for r in rows]


async def get_product(pid: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM products WHERE id=$1", pid)
    return dict(row) if row else None


async def create_product(category_id: int, name: str, description: str | None,
                         price: float, delivery_type: str) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO products(category_id,name,description,price,delivery_type)
           VALUES($1,$2,$3,$4,$5) RETURNING *""",
        category_id, name, description, str(price), delivery_type,
    )
    return dict(row)


async def update_product(pid: int, fields: dict) -> dict | None:
    pool = await get_pool()
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k}=${len(vals)+1}")
        vals.append(str(v) if k == "price" else v)
    vals.append(pid)
    row = await pool.fetchrow(
        f"UPDATE products SET {', '.join(sets)} WHERE id=${len(vals)} RETURNING *", *vals
    )
    return dict(row) if row else None


async def delete_product(pid: int) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM products WHERE id=$1", pid)


async def toggle_product(pid: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE products SET is_active = NOT is_active WHERE id=$1 RETURNING *", pid
    )
    return dict(row) if row else None


# ── Inventory ─────────────────────────────────────────────────────────────────

async def count_available_stock(product_id: int) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT COUNT(*) FROM inventory WHERE product_id=$1 AND is_sold=FALSE", product_id
    ) or 0


async def add_stock(product_id: int, items: list[str]) -> int:
    if not items:
        return 0
    pool = await get_pool()
    await pool.executemany(
        "INSERT INTO inventory(product_id, content) VALUES($1, $2)",
        [(product_id, item) for item in items],
    )
    return len(items)


# ── Orders / Purchase ─────────────────────────────────────────────────────────

async def purchase_product(user_id: int, product_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE id=$1 FOR UPDATE", user_id
            )
            if not user:
                raise ServiceError("User not found.")
            product = await conn.fetchrow(
                "SELECT * FROM products WHERE id=$1", product_id
            )
            if not product or not product["is_active"]:
                raise ServiceError("This product is no longer available.")

            total_price = float(product["price"])
            if float(user["balance"]) < total_price:
                raise ServiceError("Insufficient balance. Please recharge your account.")

            inventory_id = None
            content = None
            status = "completed"

            if product["delivery_type"] == "inventory":
                item = await conn.fetchrow(
                    "SELECT * FROM inventory WHERE product_id=$1 AND is_sold=FALSE LIMIT 1 FOR UPDATE SKIP LOCKED",
                    product_id,
                )
                if not item:
                    raise ServiceError("This product is out of stock. Please try again later.")
                inventory_id = item["id"]
                content = item["content"]
            else:
                status = "pending"

            new_balance = float(user["balance"]) - total_price
            await conn.execute(
                "UPDATE users SET balance=$1 WHERE id=$2", str(new_balance), user_id
            )

            order = await conn.fetchrow(
                """INSERT INTO orders(user_id,product_id,product_name,price,inventory_id,delivered_content,status)
                   VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
                user_id, product_id, product["name"], str(total_price),
                inventory_id, content, status,
            )

            if inventory_id is not None:
                await conn.execute(
                    "UPDATE inventory SET is_sold=TRUE, order_id=$1, sold_at=NOW() WHERE id=$2",
                    order["id"], inventory_id,
                )

            await conn.execute(
                """INSERT INTO balance_transactions(user_id,amount,type,description,balance_after)
                   VALUES($1,$2,'purchase',$3,$4)""",
                user_id, str(-total_price),
                f"Purchase: {product['name']}", str(new_balance),
            )

            return {
                "order_id": order["id"],
                "pending": status != "completed",
                "content": content,
                "new_balance": str(new_balance),
            }


async def adjust_balance(user_id: int, amount: float, type_: str, description: str) -> float:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE id=$1 FOR UPDATE", user_id
            )
            if not user:
                raise ServiceError("User not found.")
            new_balance = float(user["balance"]) + amount
            if new_balance < 0:
                raise ServiceError("Balance cannot be negative.")
            await conn.execute(
                "UPDATE users SET balance=$1 WHERE id=$2", str(new_balance), user_id
            )
            await conn.execute(
                """INSERT INTO balance_transactions(user_id,amount,type,description,balance_after)
                   VALUES($1,$2,$3,$4,$5)""",
                user_id, str(amount), type_, description, str(new_balance),
            )
            return new_balance


async def get_recent_orders(user_id: int, limit: int = 10) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM orders WHERE user_id=$1 ORDER BY id DESC LIMIT $2", user_id, limit
    )
    return [dict(r) for r in rows]


async def get_all_recent_orders(limit: int = 15) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM orders ORDER BY id DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def get_order(oid: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM orders WHERE id=$1", oid)
    return dict(row) if row else None


async def get_pending_manual_orders(limit: int = 20) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM orders WHERE status='pending' ORDER BY id LIMIT $1", limit
    )
    return [dict(r) for r in rows]


async def fulfill_order(order_id: int, content: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow(
                "SELECT * FROM orders WHERE id=$1 FOR UPDATE", order_id
            )
            if not order or order["status"] != "pending":
                return None
            await conn.execute(
                "UPDATE orders SET status='completed', delivered_content=$1 WHERE id=$2",
                content, order_id,
            )
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE id=$1", order["user_id"]
            )
            if not user:
                return None
            return {"telegram_id": user["telegram_id"], "product_name": order["product_name"]}


# ── Recharge Requests ─────────────────────────────────────────────────────────

async def create_recharge_request(user_id: int, amount: float, method: str) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO recharge_requests(user_id,amount,method) VALUES($1,$2,$3) RETURNING *",
        user_id, str(amount), method,
    )
    return dict(row)


async def set_recharge_external_ref(rid: int, external_ref: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE recharge_requests SET external_ref=$1 WHERE id=$2", external_ref, rid
    )


async def cancel_pending_recharge(rid: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE recharge_requests SET status='cancelled' WHERE id=$1 AND status='pending'", rid
    )


async def get_pending_recharges() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM recharge_requests WHERE status='pending' ORDER BY id DESC"
    )
    return [dict(r) for r in rows]


async def get_recharge(rid: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM recharge_requests WHERE id=$1", rid)
    return dict(row) if row else None


async def get_recharge_by_tx_hash(tx_hash: str) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM recharge_requests WHERE external_ref=$1", tx_hash
    )
    return dict(row) if row else None


async def approve_recharge(rid: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            req = await conn.fetchrow(
                "SELECT * FROM recharge_requests WHERE id=$1 FOR UPDATE", rid
            )
            if not req or req["status"] != "pending":
                return None
            await conn.execute(
                "UPDATE recharge_requests SET status='completed' WHERE id=$1", rid
            )
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE id=$1 FOR UPDATE", req["user_id"]
            )
            if not user:
                return None
            new_balance = float(user["balance"]) + float(req["amount"])
            await conn.execute(
                "UPDATE users SET balance=$1 WHERE id=$2", str(new_balance), req["user_id"]
            )
            await conn.execute(
                """INSERT INTO balance_transactions(user_id,amount,type,description,balance_after)
                   VALUES($1,$2,'recharge',$3,$4)""",
                req["user_id"], req["amount"],
                f"Recharge balance ({req['method']})", str(new_balance),
            )
            return {
                "user_id": req["user_id"], "amount": str(req["amount"]),
                "telegram_id": user["telegram_id"], "language": user["language"],
                "new_balance": str(new_balance),
            }


async def reject_recharge(rid: int) -> dict | None:
    pool = await get_pool()
    req = await pool.fetchrow(
        "UPDATE recharge_requests SET status='rejected' WHERE id=$1 AND status='pending' RETURNING *",
        rid,
    )
    if not req:
        return None
    user = await get_user_by_id(req["user_id"])
    if not user:
        return None
    return {"telegram_id": user["telegram_id"], "amount": str(req["amount"]), "language": user["language"]}


# ── Stars Payments ────────────────────────────────────────────────────────────

async def credit_stars_recharge(user_id: int, stars: int, charge_id: str) -> dict:
    credits = round(stars * STAR_TO_CREDIT)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchrow(
                """INSERT INTO recharge_requests(user_id,amount,method,status,external_ref,telegram_payment_charge_id)
                   VALUES($1,$2,'stars','completed',$3,$4)
                   ON CONFLICT(telegram_payment_charge_id) DO NOTHING RETURNING id""",
                user_id, str(credits), f"stars:{stars}", charge_id,
            )
            if not inserted:
                user = await conn.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
                return {"new_balance": float(user["balance"] if user else 0),
                        "credits": credits, "already_processed": True}

            user = await conn.fetchrow(
                "SELECT * FROM users WHERE id=$1 FOR UPDATE", user_id
            )
            if not user:
                raise ServiceError("User not found.")
            new_balance = float(user["balance"]) + credits
            await conn.execute(
                "UPDATE users SET balance=$1 WHERE id=$2", str(new_balance), user_id
            )
            await conn.execute(
                """INSERT INTO balance_transactions(user_id,amount,type,description,balance_after)
                   VALUES($1,$2,'recharge',$3,$4)""",
                user_id, str(credits),
                f"Recharge via Telegram Stars ({stars}⭐)", str(new_balance),
            )
            return {"new_balance": new_balance, "credits": credits, "already_processed": False}


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats() -> dict:
    pool = await get_pool()
    users = await pool.fetchval("SELECT COUNT(*) FROM users") or 0
    cats = await pool.fetchval("SELECT COUNT(*) FROM categories") or 0
    prods = await pool.fetchval("SELECT COUNT(*) FROM products") or 0
    stock = await pool.fetchval("SELECT COUNT(*) FROM inventory WHERE is_sold=FALSE") or 0
    completed = await pool.fetchval("SELECT COUNT(*) FROM orders WHERE status='completed'") or 0
    revenue = await pool.fetchval(
        "SELECT COALESCE(SUM(price),0) FROM orders WHERE status='completed'"
    ) or 0
    return {
        "users": users, "categories": cats, "products": prods,
        "available_stock": stock, "completed_orders": completed,
        "revenue": float(revenue),
    }
