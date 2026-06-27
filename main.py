import asyncio
import logging
import re
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode

import config
from config import (
    TELEGRAM_BOT_TOKEN, STAR_PACKAGES, STAR_TO_CREDIT,
    REFERRAL_MILESTONE, REFERRAL_REWARD, fmt_amount,
    set_bot_username, get_bot_username,
)
from i18n import t, escape_html, delivery_type_label, order_status_label
from keyboards import main_menu_keyboard, back_button, language_keyboard, admin_menu_keyboard
from database import init_db
import services as svc
from tronscan import verify_tron_tx
from chainverify import verify_eth_tx, verify_aptos_tx
import binancepay as bp
import binanceapi as bapi

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── TRON chain config ─────────────────────────────────────────────────────────

CHAINS = {}
if config.TRON_WALLET:
    CHAINS["tron"] = {
        "key": "tron",
        "name": "TRON (TRC20/USDT)",
        "symbol": "USDT/TRX",
        "wallet_address": config.TRON_WALLET,
    }
if config.ETH_WALLET and config.ETHERSCAN_API_KEY:
    CHAINS["eth"] = {
        "key": "eth",
        "name": "Ethereum (ETH / USDT ERC20)",
        "symbol": "ETH/USDT",
        "wallet_address": config.ETH_WALLET,
    }
if config.APT_WALLET:
    CHAINS["apt"] = {
        "key": "apt",
        "name": "Aptos (APT)",
        "symbol": "APT",
        "wallet_address": config.APT_WALLET,
    }

# ── Helpers ───────────────────────────────────────────────────────────────────

async def safe_edit(update: Update, text: str, keyboard: InlineKeyboardMarkup) -> None:
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
            return
    except Exception:
        pass
    chat_id = update.effective_chat.id
    await update.get_bot().send_message(
        chat_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def notify_admins(bot, text: str) -> None:
    admins = await svc.list_admins()
    for admin in admins:
        try:
            await bot.send_message(admin["telegram_id"], text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def notify_channel(bot, text: str) -> None:
    channel = await svc.get_required_channel()
    if not channel:
        return
    try:
        await bot.send_message(channel, text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


def get_lang(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("lang", "ar")


def get_user(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get("db_user", {})


async def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    tg_user = update.effective_user
    if not tg_user:
        return None
    user = await svc.get_or_create_user(tg_user.id, tg_user.username, tg_user.first_name)
    context.user_data["db_user"] = user
    context.user_data["lang"] = user.get("language", "ar")
    return user


async def check_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict) -> bool:
    channel = await svc.get_required_channel()
    if not channel:
        return True
    lang = user.get("language", "ar")
    is_member = False
    try:
        member = await context.bot.get_chat_member(channel, user["telegram_id"])
        if member.status in ("member", "administrator", "creator"):
            is_member = True
    except Exception:
        is_member = False
    if is_member:
        return True
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "channelJoin"), url=f"https://t.me/{channel.lstrip('@')}")],
        [InlineKeyboardButton(t(lang, "channelCheck"), callback_data="channel:check")],
    ])
    msg = t(lang, "channelRequired", channel)
    if update.callback_query:
        try:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await update.effective_chat.send_message(msg, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.effective_chat.send_message(msg, parse_mode=ParseMode.HTML, reply_markup=kb)
    return False


def parse_referral_payload(payload: str | None) -> int | None:
    if not payload or not payload.startswith("ref_"):
        return None
    try:
        val = int(payload[4:])
        return val if val > 0 else None
    except ValueError:
        return None


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    if user.get("is_banned"):
        await update.message.reply_text(t(user["language"], "banned"))
        return
    lang = user.get("language", "ar")
    context.user_data.pop("awaiting", None)

    payload = context.args[0] if context.args else None
    referrer_tg_id = parse_referral_payload(payload)
    if referrer_tg_id and referrer_tg_id != user["telegram_id"]:
        await svc.process_referral(user["id"], referrer_tg_id)
        # Reward is sent to referrer only after referred user makes a purchase
        await notify_channel(
            context.bot,
            t("en", "channelReferral",
              escape_html(user.get("first_name") or "User")),
        )

    if not await check_channel(update, context, user):
        return
    name = escape_html(user.get("first_name") or t(lang, "friend"))
    await update.message.reply_html(t(lang, "welcome", name), reply_markup=main_menu_keyboard(user))


# ── Main Menu ─────────────────────────────────────────────────────────────────

async def menu_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    if user.get("is_banned"):
        await update.callback_query.answer(t(user["language"], "banned"), show_alert=True)
        return
    context.user_data.pop("awaiting", None)
    await update.callback_query.answer()
    if not await check_channel(update, context, user):
        return
    lang = user.get("language", "ar")
    name = escape_html(user.get("first_name") or t(lang, "friend"))
    await safe_edit(update, t(lang, "welcome", name), main_menu_keyboard(user))


# ── Language ──────────────────────────────────────────────────────────────────

async def menu_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    await safe_edit(update, t(lang, "chooseLanguage"), language_keyboard(lang))


async def lang_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = update.callback_query.data.split(":")[1]
    updated = await svc.set_user_language(user["id"], lang)
    if updated:
        context.user_data["db_user"] = updated
        context.user_data["lang"] = lang
        name = escape_html(updated.get("first_name") or t(lang, "friend"))
        await safe_edit(update, t(lang, "welcome", name), main_menu_keyboard(updated))
    else:
        await update.callback_query.answer(t(lang, "error"), show_alert=True)


# ── Help ──────────────────────────────────────────────────────────────────────

async def menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    await safe_edit(update, t(lang, "helpText"), InlineKeyboardMarkup([[back_button("menu:main", lang)]]))


# ── Balance ───────────────────────────────────────────────────────────────────

async def menu_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    await safe_edit(
        update,
        t(lang, "balanceText", fmt_amount(user["balance"])),
        InlineKeyboardMarkup([
            [InlineKeyboardButton(t(lang, "rechargeBtn"), callback_data="menu:recharge")],
            [back_button("menu:main", lang)],
        ]),
    )


# ── Referral ──────────────────────────────────────────────────────────────────

async def menu_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    bot_username = get_bot_username()
    ref_link = f"https://t.me/{bot_username}?start=ref_{user['telegram_id']}" if bot_username else "—"
    ref_count = await svc.get_referral_count(user["id"])
    pending_count = await svc.get_pending_referral_count(user["id"])
    claimed = user.get("referral_rewards_claimed", 0) or 0
    next_milestone = REFERRAL_MILESTONE - (ref_count % REFERRAL_MILESTONE)
    next_msg = ""
    if not (ref_count % REFERRAL_MILESTONE == 0 and ref_count > 0):
        if lang == "ar":
            next_msg = f"\n⏳ تبقى {next_milestone} مؤكد للمكافأة القادمة"
        else:
            next_msg = f"\n⏳ {next_milestone} more confirmed until next reward"

    pending_msg = ""
    if pending_count > 0:
        if lang == "ar":
            pending_msg = f"\n🕐 {pending_count} معلق (لم يشتروا بعد)"
        else:
            pending_msg = f"\n🕐 {pending_count} pending (haven't purchased yet)"

    text = (
        t(lang, "referralTitle") + "\n\n" +
        t(lang, "referralInfo", REFERRAL_MILESTONE, fmt_amount(REFERRAL_REWARD)) + "\n\n" +
        t(lang, "referralStats", ref_count, claimed) +
        pending_msg +
        next_msg + "\n\n" +
        t(lang, "referralLinkLabel") + "\n" +
        f"<code>{ref_link}</code>"
    )
    share_url = f"https://t.me/share/url?url={ref_link}&text=" + (
        "انضم+للبوت+واحصل+على+خدمات+رقمية+بأسعار+رائعة%21" if lang == "ar"
        else "Join+the+bot+and+get+digital+services+at+great+prices%21"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 " + t(lang, "referralShare"), url=share_url)],
        [back_button("menu:main", lang)],
    ])
    await safe_edit(update, text, keyboard)


# ── Shop ──────────────────────────────────────────────────────────────────────

async def menu_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cats = await svc.get_categories(active_only=True)
    if not cats:
        await safe_edit(update, t(lang, "noCategories"), InlineKeyboardMarkup([[back_button("menu:main", lang)]]))
        return
    colors = ["🟦", "🟩", "🟥", "🟧", "🟪", "🟨", "🟫", "⬜"]
    rows = []
    for i in range(0, len(cats), 2):
        pair = cats[i:i+2]
        rows.append([
            InlineKeyboardButton(f"{colors[(i+j) % len(colors)]} {c['name']}", callback_data=f"cat:{c['id']}")
            for j, c in enumerate(pair)
        ])
    rows.append([InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")])
    await safe_edit(update, t(lang, "chooseCategory"), InlineKeyboardMarkup(rows))


async def cat_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cid = int(update.callback_query.data.split(":")[1])
    cat = await svc.get_category(cid)
    if not cat:
        await safe_edit(update, t(lang, "categoryNotFound"), InlineKeyboardMarkup([[back_button("menu:shop", lang)]]))
        return
    products = await svc.get_products(cid, active_only=True)
    if not products:
        await safe_edit(
            update,
            t(lang, "noProductsInCategory", escape_html(cat["name"])),
            InlineKeyboardMarkup([[back_button("menu:shop", lang)]]),
        )
        return
    rows = [[InlineKeyboardButton(f"{p['name']} — {fmt_amount(p['price'])}", callback_data=f"prod:{p['id']}")] for p in products]
    rows.append([back_button("menu:shop", lang)])
    await safe_edit(update, t(lang, "chooseProduct", escape_html(cat["name"])), InlineKeyboardMarkup(rows))


async def prod_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    pid = int(update.callback_query.data.split(":")[1])
    product = await svc.get_product(pid)
    if not product or not product["is_active"]:
        await safe_edit(update, t(lang, "productNotAvailable"), InlineKeyboardMarkup([[back_button("menu:shop", lang)]]))
        return
    stock = await svc.count_available_stock(pid) if product["delivery_type"] == "inventory" else None
    if stock is None:
        stock_line = t(lang, "unlimited")
    elif stock > 0:
        stock_line = f"{t(lang, 'stock')}: {stock}"
    else:
        stock_line = t(lang, "outOfStock")
    text = (
        f"🛒 <b>{escape_html(product['name'])}</b>\n\n" +
        (f"{escape_html(product['description'])}\n\n" if product.get("description") else "") +
        f"💵 {t(lang, 'price')}: <b>{fmt_amount(product['price'])}</b>\n" +
        f"📦 {stock_line}\n" +
        f"🚚 {delivery_type_label(product['delivery_type'], lang)}\n\n" +
        f"{t(lang, 'yourBalance')}: {fmt_amount(user['balance'])}"
    )
    rows = []
    can_buy = stock is None or stock > 0
    if can_buy:
        rows.append([InlineKeyboardButton(t(lang, "buyNow"), callback_data=f"buy:{pid}")])
    rows.append([back_button(f"cat:{product['category_id']}", lang)])
    await safe_edit(update, text, InlineKeyboardMarkup(rows))


async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    lang = user.get("language", "ar")
    pid = int(update.callback_query.data.split(":")[1])
    try:
        result = await svc.purchase_product(user["id"], pid)
        context.user_data["db_user"]["balance"] = result["new_balance"]
        await update.callback_query.answer("✅")
        product = await svc.get_product(pid)
        if result["pending"]:
            text = t(lang, "purchasePending", str(result["order_id"]), fmt_amount(result["new_balance"]))
            await safe_edit(update, text, InlineKeyboardMarkup([[back_button("menu:main", lang)]]))
            await notify_admins(
                context.bot,
                t(lang, "adminManualOrder", str(result["order_id"]),
                  escape_html(product["name"] if product else ""),
                  escape_html(user.get("first_name") or ""), str(user["telegram_id"])),
            )
        else:
            text = t(lang, "purchaseSuccess",
                     escape_html(product["name"] if product else ""),
                     escape_html(result["content"] or ""),
                     fmt_amount(result["new_balance"]))
            await safe_edit(update, text, InlineKeyboardMarkup([[back_button("menu:main", lang)]]))
            await notify_channel(
                context.bot,
                t("en", "channelPurchase",
                  escape_html(user.get("first_name") or "User"),
                  escape_html(product["name"] if product else "")),
            )
            # Confirm referral after first successful purchase
            try:
                ref_result = await svc.confirm_referral(user["id"])
                if ref_result["rewarded"]:
                    ref_lang = ref_result["referrer_language"] or "ar"
                    await context.bot.send_message(
                        ref_result["referrer_telegram_id"],
                        t(ref_lang, "referralRewardEarned", fmt_amount(ref_result["reward_amount"])),
                        parse_mode=ParseMode.HTML,
                    )
            except Exception:
                pass
    except svc.ServiceError as e:
        await update.callback_query.answer(str(e), show_alert=True)
    except Exception:
        await update.callback_query.answer(t(lang, "error"), show_alert=True)


# ── My Orders ─────────────────────────────────────────────────────────────────

async def menu_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    orders = await svc.get_recent_orders(user["id"], 10)
    if not orders:
        await safe_edit(update, t(lang, "noOrders"), InlineKeyboardMarkup([[back_button("menu:main", lang)]]))
        return
    text = t(lang, "yourOrders") + "\n\n"
    for o in orders:
        text += f"#{o['id']} — {escape_html(o['product_name'])} — {fmt_amount(o['price'])} — {order_status_label(o['status'], lang)}\n"
        if o.get("delivered_content"):
            text += f"   🔑 <pre>{escape_html(o['delivered_content'])}</pre>\n"
    await safe_edit(update, text, InlineKeyboardMarkup([[back_button("menu:main", lang)]]))


# ── Recharge ──────────────────────────────────────────────────────────────────

async def menu_recharge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    rows = [
        [InlineKeyboardButton(t(lang, "starsBtn"), callback_data="rc:stars")],
        [InlineKeyboardButton(t(lang, "manualBtn"), callback_data="rc:manual")],
    ]
    if config.BINANCE_PAY_MERCHANT_ID:
        rows.append([InlineKeyboardButton(t(lang, "binanceBtn"), callback_data="rc:binance")])
    for key, chain in CHAINS.items():
        rows.append([InlineKeyboardButton(f"💎 {chain['name']}", callback_data=f"rc:chain:{key}")])
    rows.append([back_button("menu:main", lang)])
    await safe_edit(
        update,
        t(lang, "rechargeTitle") + "\n\n" + t(lang, "rechargeCurrentBalance", fmt_amount(user["balance"])),
        InlineKeyboardMarkup(rows),
    )


async def rc_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    rows = [
        [InlineKeyboardButton(
            f"{s} ⭐ = {fmt_amount(round(s * STAR_TO_CREDIT))}",
            callback_data=f"rcstars:{s}",
        )]
        for s in STAR_PACKAGES
    ]
    rows.append([back_button("menu:recharge", lang)])
    await safe_edit(
        update,
        t(lang, "starsTitle") + "\n\n" + t(lang, "starsChoose"),
        InlineKeyboardMarkup(rows),
    )


async def rcstars_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    stars = int(update.callback_query.data.split(":")[1])
    credits = round(stars * STAR_TO_CREDIT)
    try:
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title=f"{t(lang, 'rechargeInvoiceTitle')} — {credits} {config.CURRENCY}",
            description=f"{t(lang, 'rechargeInvoiceDesc')} {fmt_amount(credits)}",
            payload=f"recharge_stars:{user['id']}:{stars}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"{credits} {config.CURRENCY}", amount=stars)],
        )
    except Exception:
        await context.bot.send_message(update.effective_chat.id, t(lang, "starsInvoiceError"))


async def rc_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    context.user_data["awaiting"] = {"action": "manual_recharge_amount"}
    await safe_edit(
        update,
        t(lang, "manualTitle") + "\n\n" + t(lang, "manualPrompt"),
        InlineKeyboardMarkup([[back_button("menu:recharge", lang)]]),
    )


async def rc_binance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    mid = config.BINANCE_PAY_MERCHANT_ID
    context.user_data["awaiting"] = {"action": "binance_recharge_amount"}
    await safe_edit(
        update,
        t(lang, "binanceTitle") + "\n\n" +
        t(lang, "binanceInstructions", mid) + "\n\n" +
        t(lang, "binanceAmountPrompt"),
        InlineKeyboardMarkup([[back_button("menu:recharge", lang)]]),
    )


async def check_binance_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    lang = user.get("language", "ar")
    parts = update.callback_query.data.split(":")
    # expected: binance_check:{req_id}:{start_time_s}
    if len(parts) < 3:
        await update.callback_query.answer(t(lang, "error"), show_alert=True)
        return

    req_id = int(parts[1])
    start_time_s = int(parts[2])
    start_time_ms = start_time_s * 1000

    if not config.BINANCE_API_KEY or not config.BINANCE_SECRET_KEY:
        await update.callback_query.answer(t(lang, "binanceNotPaid"), show_alert=True)
        return

    # Fetch reference and amount from DB
    recharge = await svc.get_recharge(req_id)
    if not recharge:
        await update.callback_query.answer(t(lang, "error"), show_alert=True)
        return

    reference = recharge.get("external_ref") or bapi.make_reference(req_id)
    expected_amount = float(recharge.get("amount", 0))

    # Show spinner
    await update.callback_query.answer("⏳ " + ("جاري التحقق..." if lang == "ar" else "Verifying..."))

    result = await bapi.verify_payment_by_note(
        config.BINANCE_API_KEY, config.BINANCE_SECRET_KEY,
        reference, expected_amount, start_time_ms,
    )

    if result.verified:
        approved = await svc.approve_recharge(req_id)
        if approved:
            await update.callback_query.edit_message_text(
                t(lang, "binancePaidSuccess", fmt_amount(approved["amount"])),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")]]),
            )
            await notify_channel(
                context.bot,
                t("en", "channelRecharge",
                  escape_html(user.get("first_name") or "User"),
                  fmt_amount(approved["amount"]), "🟡 Binance Pay"),
            )
        else:
            await update.callback_query.edit_message_text(
                "✅ " + ("تمت معالجة هذا الطلب مسبقاً." if lang == "ar" else "Already processed."),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")]]),
            )
    elif result.error == "geo_blocked":
        # Binance API blocked from this server — silently switch to manual flow
        context.user_data["awaiting"] = {
            "action": "binance_recharge_orderid",
            "data": {"req_id": req_id, "amount": expected_amount},
        }
        mid = config.BINANCE_PAY_MERCHANT_ID
        await update.callback_query.edit_message_text(
            t(lang, "binanceTitle") + "\n\n" +
            t(lang, "binanceInstructions", mid) + "\n\n" +
            t(lang, "binanceManualOrderPrompt", fmt_amount(expected_amount)),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")]]),
        )
    elif result.error and result.error.startswith("amount_mismatch"):
        found = result.error.split(":")[1] if ":" in result.error else "?"
        await update.callback_query.edit_message_text(
            t(lang, "binanceAmountMismatch", found, fmt_amount(expected_amount)),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(lang, "binanceCheckBtn"), callback_data=update.callback_query.data)],
                [InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")],
            ]),
        )
    else:
        await update.callback_query.edit_message_text(
            t(lang, "binanceNotPaid"),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(lang, "binanceCheckBtn"), callback_data=update.callback_query.data)],
                [InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")],
            ]),
        )


async def rc_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    chain_key = update.callback_query.data.split(":", 2)[2]
    chain = CHAINS.get(chain_key)
    if not chain or not chain.get("wallet_address"):
        await safe_edit(update, t(lang, "chainDisabled"), InlineKeyboardMarkup([[back_button("menu:recharge", lang)]]))
        return
    context.user_data["awaiting"] = {"action": "chain_recharge_amount", "data": {"chain_key": chain_key}}
    await safe_edit(
        update,
        t(lang, "chainTitle", chain["name"]) + "\n\n" + t(lang, "chainPrompt", chain["symbol"], chain["wallet_address"]),
        InlineKeyboardMarkup([[back_button("menu:recharge", lang)]]),
    )


# ── Telegram Stars Payment ────────────────────────────────────────────────────

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    user = await ensure_user(update, context)
    parts = query.invoice_payload.split(":")
    valid = (
        len(parts) == 3 and parts[0] == "recharge_stars" and
        query.currency == "XTR" and
        user and int(parts[1]) == user["id"] and
        query.total_amount == int(parts[2])
    )
    if valid:
        await query.answer(ok=True)
    else:
        lang = user["language"] if user else "ar"
        await query.answer(ok=False, error_message=t(lang, "preCheckoutError"))


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    lang = user.get("language", "ar")
    payment = update.message.successful_payment
    charge_id = payment.telegram_payment_charge_id
    parts = payment.invoice_payload.split(":")
    if len(parts) != 3:
        await update.message.reply_text(t(lang, "error"))
        return
    stars = int(parts[2])
    result = await svc.credit_stars_recharge(user["id"], stars, charge_id)
    if result["already_processed"]:
        await update.message.reply_text(t(lang, "paymentAlreadyProcessed", fmt_amount(result["new_balance"])))
    else:
        await update.message.reply_text(
            t(lang, "paymentSuccess", fmt_amount(result["credits"]), fmt_amount(result["new_balance"]))
        )
        await notify_channel(
            context.bot,
            t("en", "channelRecharge",
              escape_html(user.get("first_name") or "User"),
              fmt_amount(result["credits"]), "⭐ Telegram Stars"),
        )


# ── Text Router ───────────────────────────────────────────────────────────────

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    if user.get("is_banned"):
        return
    lang = user.get("language", "ar")
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        await update.message.reply_text(t(lang, "useStart"))
        return

    text = update.message.text.strip()
    is_admin = user.get("is_admin", False)
    action = awaiting.get("action")
    data = awaiting.get("data", {})

    try:
        if action == "binance_recharge_amount":
            try:
                amount = float(text)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(t(lang, "invalidNumber"))
                return
            context.user_data.pop("awaiting", None)
            req = await svc.create_recharge_request(user["id"], amount, "binance")
            reference = bapi.make_reference(req["id"])
            start_time_s = int(__import__("time").time())
            await svc.set_recharge_external_ref(req["id"], reference)

            mid = config.BINANCE_PAY_MERCHANT_ID
            context.user_data["awaiting"] = {
                "action": "binance_recharge_orderid",
                "data": {"req_id": req["id"], "amount": amount},
            }
            await update.message.reply_html(
                t(lang, "binanceTitle") + "\n\n" +
                t(lang, "binanceInstructions", mid) + "\n\n" +
                t(lang, "binanceManualOrderPrompt", fmt_amount(amount)),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")]]),
            )

        elif action == "binance_recharge_orderid":
            order_id_str = text.strip()
            if not order_id_str:
                await update.message.reply_text(t(lang, "invalidNumber"))
                return
            req_id = int(data.get("req_id", 0))
            amount = float(data.get("amount", 0))
            context.user_data.pop("awaiting", None)
            await svc.set_recharge_external_ref(req_id, order_id_str)
            await update.message.reply_text(t(lang, "rechargeSuccess", str(req_id), fmt_amount(amount)))
            await notify_admins(
                context.bot,
                f"🟡 <b>Binance Pay #{req_id}</b>\n"
                f"Amount: {fmt_amount(amount)}\n"
                f"Order ID: <code>{escape_html(order_id_str)}</code>\n"
                f"Client: {escape_html(user.get('first_name') or '')} ({user['telegram_id']})\n\n"
                f"Review from «Admin Panel ← Recharge Requests».",
            )

        elif action == "manual_recharge_amount":
            try:
                amount = float(text)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(t(lang, "invalidNumber"))
                return
            context.user_data.pop("awaiting", None)
            req = await svc.create_recharge_request(user["id"], amount, "manual")
            await update.message.reply_text(t(lang, "rechargeSuccess", str(req["id"]), fmt_amount(amount)))
            await notify_admins(
                context.bot,
                t(lang, "rechargeManualNotify", str(req["id"]), fmt_amount(amount),
                  f"{escape_html(user.get('first_name') or '')} ({user['telegram_id']})"),
            )

        elif action == "chain_recharge_amount":
            try:
                amount = float(text)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(t(lang, "invalidNumber"))
                return
            chain_key = data.get("chain_key", "")
            chain = CHAINS.get(chain_key)
            if not chain:
                context.user_data.pop("awaiting", None)
                await update.message.reply_text(t(lang, "chainDisabled"))
                return
            req = await svc.create_recharge_request(user["id"], amount, chain_key)
            await svc.set_recharge_external_ref(req["id"], chain["wallet_address"])
            context.user_data["awaiting"] = {"action": "chain_recharge_txhash",
                                              "data": {"req_id": req["id"], "amount": amount, "chain_key": chain_key}}
            await update.message.reply_html(
                t(lang, "chainRechargeReq", chain["name"], str(req["id"])) + "\n" +
                t(lang, "chainRechargeValue", fmt_amount(amount)) + "\n\n" +
                t(lang, "chainRechargeSendTo") + "\n" +
                f"<code>{chain['wallet_address']}</code>\n\n" +
                t(lang, "chainRechargeSendHash"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")]]),
            )

        elif action == "chain_recharge_txhash":
            req_id = int(data.get("req_id", 0))
            amount = float(data.get("amount", 0))
            chain_key = str(data.get("chain_key", ""))
            chain = CHAINS.get(chain_key)
            if not chain or not req_id:
                context.user_data.pop("awaiting", None)
                await update.message.reply_text(t(lang, "sessionExpired"))
                return
            tx_hash = text.strip()
            if len(tx_hash) < 32:
                await update.message.reply_text(t(lang, "invalidTxHash"))
                return
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(t(lang, "verifying", chain["name"]))
            try:
                if chain_key == "eth":
                    result = await verify_eth_tx(tx_hash, chain["wallet_address"], config.ETHERSCAN_API_KEY)
                elif chain_key == "apt":
                    result = await verify_aptos_tx(tx_hash, chain["wallet_address"])
                else:
                    result = await verify_tron_tx(tx_hash, chain["wallet_address"])
                if not result.verified:
                    await update.message.reply_html(
                        t(lang, "verificationFailed", result.error or "Verification failed"),
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")]]),
                    )
                    return
                symbol = result.symbol or chain["symbol"]
                is_stable = "usd" in symbol.lower()
                transferred = result.amount or 0
                if transferred < amount * 0.99:
                    key = "amountMismatchStable" if is_stable else "amountMismatch"
                    await update.message.reply_html(
                        t(lang, key, fmt_amount(amount), fmt_amount(transferred)),
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")]]),
                    )
                    return
                recharge = await svc.get_recharge(req_id)
                if not recharge or recharge["status"] != "pending":
                    await update.message.reply_text(t(lang, "alreadyProcessed"))
                    return
                existing = await svc.get_recharge_by_tx_hash(tx_hash)
                if existing and existing["id"] != req_id:
                    await update.message.reply_text(t(lang, "txHashUsed"))
                    return
                await svc.set_recharge_external_ref(req_id, tx_hash)
                approve_result = await svc.approve_recharge(req_id)
                if not approve_result:
                    await update.message.reply_text(t(lang, "processedAlready"))
                    return
                await notify_channel(
                    context.bot,
                    t("en", "channelRecharge",
                      escape_html(user.get("first_name") or "User"),
                      fmt_amount(amount), f"💎 {chain_key.upper()}"),
                )
                await update.message.reply_html(
                    t(lang, "chainRechargeSuccess") + "\n\n" +
                    t(lang, "chainRechargeAmount") + ": " + fmt_amount(amount) + "\n" +
                    t(lang, "chainRechargeReceived") + ": " + f"{transferred:g}" + " " + symbol + "\n" +
                    t(lang, "chainRechargeVerifyLink") + ": <code>" + tx_hash + "</code>\n\n" +
                    t(lang, "chainRechargeNewBalance") + ": " + fmt_amount(approve_result["new_balance"]) + "\n\n" +
                    t(lang, "chainRechargeThankYou"),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(t(lang, "shop"), callback_data="menu:shop")],
                        [InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")],
                    ]),
                )
            except Exception:
                await update.message.reply_text(t(lang, "verificationError"))

        # ── Admin text actions ────────────────────────────────────────────────

        elif action == "admin_fulfill_order" and is_admin:
            order_id = int(data.get("order_id", 0))
            context.user_data.pop("awaiting", None)
            result = await svc.fulfill_order(order_id, text)
            if not result:
                await update.message.reply_text(t(lang, "adminOrderNotPending"))
                return
            await update.message.reply_text(
                t(lang, "fulfillSuccess", str(order_id)),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminBackToPending"), callback_data="adm:pending")]]),
            )
            try:
                await context.bot.send_message(
                    result["telegram_id"],
                    t(lang, "fulfillCustomerTitle") + "\n" +
                    t(lang, "fulfillCustomerProduct") + ": " + escape_html(result["product_name"]) + "\n\n" +
                    f"<b>{t(lang, 'fulfillCustomerContent')}:</b>\n<code>{escape_html(text)}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        elif action == "admin_edit_cat_name" and is_admin:
            cid = int(data.get("id", 0))
            context.user_data.pop("awaiting", None)
            updated = await svc.update_category(cid, {"name": text})
            if not updated:
                await update.message.reply_text(t(lang, "adminCatNotFound"))
                return
            await update.message.reply_text(
                t(lang, "editCatNameSuccess", updated["name"]),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminBackToCat"), callback_data=f"adm:catview:{cid}")]]),
            )

        elif action == "admin_edit_cat_desc" and is_admin:
            cid = int(data.get("id", 0))
            context.user_data.pop("awaiting", None)
            desc = None if text == "-" else text
            updated = await svc.update_category(cid, {"description": desc})
            if not updated:
                await update.message.reply_text(t(lang, "adminCatNotFound"))
                return
            await update.message.reply_text(
                t(lang, "editCatDescSuccess"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminBackToCat"), callback_data=f"adm:catview:{cid}")]]),
            )

        elif action == "admin_edit_prod_name" and is_admin:
            pid = int(data.get("id", 0))
            context.user_data.pop("awaiting", None)
            updated = await svc.update_product(pid, {"name": text})
            if not updated:
                await update.message.reply_text(t(lang, "adminProdNotFound"))
                return
            await update.message.reply_text(
                t(lang, "editProdNameSuccess", updated["name"]),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminBackToProd"), callback_data=f"adm:prodview:{pid}")]]),
            )

        elif action == "admin_edit_prod_price" and is_admin:
            pid = int(data.get("id", 0))
            try:
                price = float(text)
                if price < 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(t(lang, "invalidPrice"))
                return
            context.user_data.pop("awaiting", None)
            updated = await svc.update_product(pid, {"price": price})
            if not updated:
                await update.message.reply_text(t(lang, "adminProdNotFound"))
                return
            await update.message.reply_text(
                t(lang, "editProdPriceSuccess", fmt_amount(price)),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminBackToProd"), callback_data=f"adm:prodview:{pid}")]]),
            )

        elif action == "admin_edit_prod_desc" and is_admin:
            pid = int(data.get("id", 0))
            context.user_data.pop("awaiting", None)
            desc = None if text == "-" else text
            updated = await svc.update_product(pid, {"description": desc})
            if not updated:
                await update.message.reply_text(t(lang, "adminProdNotFound"))
                return
            await update.message.reply_text(
                t(lang, "editProdDescSuccess"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminBackToProd"), callback_data=f"adm:prodview:{pid}")]]),
            )

        elif action == "admin_add_category_name" and is_admin:
            context.user_data["awaiting"] = {"action": "admin_add_category_desc", "data": {"name": text}}
            await update.message.reply_text(t(lang, "addCatDescPrompt"))

        elif action == "admin_add_category_desc" and is_admin:
            name = str(data.get("name", ""))
            desc = None if text == "-" else text
            context.user_data.pop("awaiting", None)
            cat = await svc.create_category(name, desc)
            await update.message.reply_text(
                t(lang, "adminCatCreated", cat["name"]),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(lang, "adminAddProd"), callback_data=f"adm:addprod:{cat['id']}")],
                    [InlineKeyboardButton(t(lang, "adminPanel"), callback_data="adm:main")],
                ]),
            )

        elif action == "admin_add_product_name" and is_admin:
            context.user_data["awaiting"] = {"action": "admin_add_product_price", "data": {**data, "name": text}}
            await update.message.reply_text(t(lang, "addProdPricePrompt"))

        elif action == "admin_add_product_price" and is_admin:
            try:
                price = float(text)
                if price < 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(t(lang, "invalidPrice"))
                return
            context.user_data["awaiting"] = {"action": "admin_add_product_desc", "data": {**data, "price": price}}
            await update.message.reply_text(t(lang, "addProdDescPrompt"))

        elif action == "admin_add_product_desc" and is_admin:
            desc = None if text == "-" else text
            context.user_data["awaiting"] = {"action": "admin_product_delivery_choice",
                                              "data": {**data, "description": desc}}
            await update.message.reply_text(
                t(lang, "chooseDeliveryType"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(lang, "deliveryAutoBtn"), callback_data="adm:newprod_delivery:inventory")],
                    [InlineKeyboardButton(t(lang, "deliveryManualBtn"), callback_data="adm:newprod_delivery:manual")],
                ]),
            )

        elif action == "admin_add_stock" and is_admin:
            product_id = int(data.get("product_id", 0))
            items = [l.strip() for l in text.split("\n") if l.strip()]
            if not items:
                await update.message.reply_text(t(lang, "noValidItems"))
                return
            context.user_data.pop("awaiting", None)
            added = await svc.add_stock(product_id, items)
            product = await svc.get_product(product_id)
            await update.message.reply_text(
                t(lang, "stockAdded", str(added), product["name"] if product else ""),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminBackToProd"), callback_data=f"adm:prodview:{product_id}")]]),
            )

        elif action == "admin_broadcast" and is_admin:
            if not text:
                await update.message.reply_text(t(lang, "broadcastEmpty"))
                return
            context.user_data.pop("awaiting", None)
            users = await svc.list_users(500)
            sent = failed = 0
            for u in users:
                try:
                    await context.bot.send_message(u["telegram_id"], text, parse_mode=ParseMode.HTML)
                    sent += 1
                except Exception:
                    failed += 1
            await update.message.reply_text(
                t(lang, "broadcastSent") + "\n" + t(lang, "broadcastSuccess") + f": {sent}\n" + t(lang, "broadcastFailed") + f": {failed}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminPanel"), callback_data="adm:main")]]),
            )

        elif action == "admin_adjust_balance" and is_admin:
            uid = int(data.get("user_id", 0))
            try:
                amount = float(text)
                if amount == 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_html(t(lang, "adjustPrompt"))
                return
            context.user_data.pop("awaiting", None)
            new_bal = await svc.adjust_balance(uid, amount, "admin_adjust",
                                               f"Manual adjustment ({'+'if amount>0 else ''}{amount})")
            await update.message.reply_text(t(lang, "adjustSuccess", fmt_amount(new_bal)))
            target = await svc.get_user_by_id(uid)
            if target:
                tgt_lang = target.get("language", "ar")
                try:
                    await context.bot.send_message(
                        target["telegram_id"],
                        t(tgt_lang, "balanceAdjusted", ("+" if amount > 0 else "") + fmt_amount(amount)) + "\n" +
                        t(tgt_lang, "currentBalance", fmt_amount(new_bal)),
                    )
                except Exception:
                    pass

        elif action == "admin_set_channel" and is_admin:
            context.user_data.pop("awaiting", None)
            if text == "-":
                await svc.delete_setting("required_channel")
                svc.invalidate_channel_cache()
                await update.message.reply_text(
                    t(lang, "adminChannelRemoved"),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:channel")]]),
                )
            else:
                channel = text if text.startswith("@") or text.startswith("-") else f"@{text}"
                await svc.set_setting("required_channel", channel)
                svc.invalidate_channel_cache()
                await update.message.reply_html(
                    t(lang, "adminChannelSetSuccess", channel),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:channel")]]),
                )
        else:
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(t(lang, "useStart"))

    except svc.ServiceError as e:
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(f"⚠️ {e}")
    except Exception:
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(t(lang, "unexpectedError"))


# ── Admin Handlers ────────────────────────────────────────────────────────────

async def adm_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    context.user_data.pop("awaiting", None)
    lang = user.get("language", "ar")
    await safe_edit(update, t(lang, "adminPanel") + "\n\n" + t(lang, "adminChoose"), admin_menu_keyboard(lang))


async def adm_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    current = await svc.get_setting("required_channel")
    status_line = t(lang, "adminChannelCurrent", current) if current else t(lang, "adminChannelNone")
    rows = [[InlineKeyboardButton(t(lang, "adminChannelSet"), callback_data="adm:channel:set")]]
    if current:
        rows.append([InlineKeyboardButton(t(lang, "adminChannelRemove"), callback_data="adm:channel:remove")])
    rows.append([InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, t(lang, "adminChannelTitle") + "\n\n" + status_line, InlineKeyboardMarkup(rows))


async def channel_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user:
        return
    lang = user.get("language", "ar")
    channel = await svc.get_required_channel()
    if not channel:
        await update.callback_query.answer()
        await menu_main(update, context)
        return
    is_member = False
    try:
        member = await context.bot.get_chat_member(channel, user["telegram_id"])
        if member.status in ("member", "administrator", "creator"):
            is_member = True
    except Exception:
        is_member = False
    if is_member:
        await update.callback_query.answer("✅")
        context.user_data.pop("awaiting", None)
        name = escape_html(user.get("first_name") or t(lang, "friend"))
        await safe_edit(update, t(lang, "welcome", name), main_menu_keyboard(user))
    else:
        await update.callback_query.answer(t(lang, "channelNotJoined"), show_alert=True)


async def adm_channel_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    context.user_data["awaiting"] = {"action": "admin_set_channel"}
    await safe_edit(
        update,
        t(lang, "adminChannelTitle") + "\n\n" + t(lang, "adminChannelSetPrompt"),
        InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]),
    )


async def adm_channel_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    await svc.delete_setting("required_channel")
    svc.invalidate_channel_cache()
    await safe_edit(update, t(lang, "adminChannelRemoved"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_cats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cats = await svc.get_categories(active_only=False)
    text = t(lang, "adminCatsTitle") + "\n\n"
    if not cats:
        text += t(lang, "adminNoCats") + "\n"
    rows = [[InlineKeyboardButton(f"{'🚫 ' if not c['is_active'] else ''}{c['name']}", callback_data=f"adm:catview:{c['id']}")] for c in cats]
    rows.append([InlineKeyboardButton(t(lang, "adminAddCat"), callback_data="adm:addcat")])
    rows.append([InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, text, InlineKeyboardMarkup(rows))


async def adm_addcat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    context.user_data["awaiting"] = {"action": "admin_add_category_name"}
    await safe_edit(update, t(lang, "adminAddCatPrompt"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_catview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cid = int(update.callback_query.data.split(":")[2])
    cat = await svc.get_category(cid)
    if not cat:
        await safe_edit(update, t(lang, "adminCatNotFound"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    status = t(lang, "adminCatStatusActive") if cat["is_active"] else t(lang, "adminCatStatusInactive")
    text = f"📂 <b>{escape_html(cat['name'])}</b>\n{t(lang, 'adminUserStatus')}: {status}"
    await safe_edit(update, text, InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "adminEditName"), callback_data=f"adm:editcatname:{cid}"),
            InlineKeyboardButton(t(lang, "adminEditDesc"), callback_data=f"adm:editcatdesc:{cid}"),
        ],
        [
            InlineKeyboardButton(t(lang, "adminToggle", cat["is_active"]), callback_data=f"adm:togglecat:{cid}"),
            InlineKeyboardButton(t(lang, "adminDeleteCat"), callback_data=f"adm:deletecat:{cid}"),
        ],
        [InlineKeyboardButton(t(lang, "adminAddProd"), callback_data=f"adm:addprod:{cid}")],
        [InlineKeyboardButton(t(lang, "adminCategories"), callback_data="adm:cats")],
        [InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")],
    ]))


async def adm_editcatname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cid = int(update.callback_query.data.split(":")[2])
    context.user_data["awaiting"] = {"action": "admin_edit_cat_name", "data": {"id": cid}}
    await safe_edit(update, t(lang, "adminEditName"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_editcatdesc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cid = int(update.callback_query.data.split(":")[2])
    context.user_data["awaiting"] = {"action": "admin_edit_cat_desc", "data": {"id": cid}}
    await safe_edit(update, t(lang, "adminEditDesc"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_togglecat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    lang = user.get("language", "ar")
    cid = int(update.callback_query.data.split(":")[2])
    updated = await svc.toggle_category(cid)
    await update.callback_query.answer(t(lang, "adminToggleSuccess", updated["is_active"] if updated else False))
    if not updated:
        return
    status = t(lang, "adminCatStatusActive") if updated["is_active"] else t(lang, "adminCatStatusInactive")
    text = f"📂 <b>{escape_html(updated['name'])}</b>\n{t(lang, 'adminUserStatus')}: {status}"
    await safe_edit(update, text, InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "adminEditName"), callback_data=f"adm:editcatname:{cid}"),
            InlineKeyboardButton(t(lang, "adminEditDesc"), callback_data=f"adm:editcatdesc:{cid}"),
        ],
        [
            InlineKeyboardButton(t(lang, "adminToggle", updated["is_active"]), callback_data=f"adm:togglecat:{cid}"),
            InlineKeyboardButton(t(lang, "adminDeleteCat"), callback_data=f"adm:deletecat:{cid}"),
        ],
        [InlineKeyboardButton(t(lang, "adminAddProd"), callback_data=f"adm:addprod:{cid}")],
        [InlineKeyboardButton(t(lang, "adminCategories"), callback_data="adm:cats")],
        [InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")],
    ]))


async def adm_deletecat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cid = int(update.callback_query.data.split(":")[2])
    await svc.delete_category(cid)
    await safe_edit(update, t(lang, "adminCatNotFound"), InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "adminCategories"), callback_data="adm:cats"),
         InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")],
    ]))


async def adm_prods(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cats = await svc.get_categories(active_only=False)
    if not cats:
        await safe_edit(update, t(lang, "adminNoProducts"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    rows = [[InlineKeyboardButton(c["name"], callback_data=f"adm:prodcat:{c['id']}")] for c in cats]
    rows.append([InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, t(lang, "adminCatsTitle"), InlineKeyboardMarkup(rows))


async def adm_prodcat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cat_id = int(update.callback_query.data.split(":")[2])
    cat = await svc.get_category(cat_id)
    if not cat:
        await safe_edit(update, t(lang, "adminCatNotFound"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    products = await svc.get_products(cat_id, active_only=False)
    rows = [
        [InlineKeyboardButton(
            f"{'🚫 ' if not p['is_active'] else ''}{p['name']} — {fmt_amount(p['price'])}",
            callback_data=f"adm:prodview:{p['id']}",
        )]
        for p in products
    ]
    rows.append([InlineKeyboardButton(t(lang, "adminAddProd"), callback_data=f"adm:addprod:{cat_id}")])
    rows.append([InlineKeyboardButton(t(lang, "adminProducts"), callback_data="adm:prods"),
                 InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, f"📂 <b>{escape_html(cat['name'])}</b>", InlineKeyboardMarkup(rows))


async def adm_addprod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    cat_id = int(update.callback_query.data.split(":")[2])
    context.user_data["awaiting"] = {"action": "admin_add_product_name", "data": {"category_id": cat_id}}
    await safe_edit(update, t(lang, "adminAddProdPrompt"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_prodview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    pid = int(update.callback_query.data.split(":")[2])
    product = await svc.get_product(pid)
    if not product:
        await safe_edit(update, t(lang, "adminProdNotFound"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    stock = await svc.count_available_stock(pid) if product["delivery_type"] == "inventory" else None
    status = t(lang, "adminProdStatusActive") if product["is_active"] else t(lang, "adminProdStatusInactive")
    text = (
        f"🛒 <b>{escape_html(product['name'])}</b>\n" +
        f"{t(lang, 'adminPrice')}: {fmt_amount(product['price'])}\n" +
        f"{t(lang, 'adminUserStatus')}: {status}\n" +
        f"🚚 {delivery_type_label(product['delivery_type'], lang)}" +
        (f"\n📦 {t(lang, 'stock')}: {stock}" if stock is not None else "")
    )
    rows = [
        [
            InlineKeyboardButton(t(lang, "adminEditName"), callback_data=f"adm:editprodname:{pid}"),
            InlineKeyboardButton(t(lang, "adminPrice"), callback_data=f"adm:editprodprice:{pid}"),
        ],
        [InlineKeyboardButton(t(lang, "adminEditDesc"), callback_data=f"adm:editproddesc:{pid}")],
        [
            InlineKeyboardButton(t(lang, "adminToggle", product["is_active"]), callback_data=f"adm:toggleprod:{pid}"),
            InlineKeyboardButton(t(lang, "adminDeleteProd"), callback_data=f"adm:deleteprod:{pid}"),
        ],
    ]
    if product["delivery_type"] == "inventory":
        rows.append([InlineKeyboardButton(t(lang, "adminAddProd"), callback_data=f"adm:stock:{pid}")])
    rows.append([InlineKeyboardButton(t(lang, "adminBackToCat"), callback_data=f"adm:prodcat:{product['category_id']}")])
    rows.append([InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, text, InlineKeyboardMarkup(rows))


async def adm_editprodname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    pid = int(update.callback_query.data.split(":")[2])
    context.user_data["awaiting"] = {"action": "admin_edit_prod_name", "data": {"id": pid}}
    await safe_edit(update, t(lang, "adminEditName"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_editprodprice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    pid = int(update.callback_query.data.split(":")[2])
    context.user_data["awaiting"] = {"action": "admin_edit_prod_price", "data": {"id": pid}}
    await safe_edit(update, t(lang, "adminPrice"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_editproddesc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    pid = int(update.callback_query.data.split(":")[2])
    context.user_data["awaiting"] = {"action": "admin_edit_prod_desc", "data": {"id": pid}}
    await safe_edit(update, t(lang, "adminEditDesc"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_toggleprod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    lang = user.get("language", "ar")
    pid = int(update.callback_query.data.split(":")[2])
    updated = await svc.toggle_product(pid)
    await update.callback_query.answer(t(lang, "adminToggleSuccess", updated["is_active"] if updated else False))
    if not updated:
        return
    stock = await svc.count_available_stock(pid) if updated["delivery_type"] == "inventory" else None
    status = t(lang, "adminProdStatusActive") if updated["is_active"] else t(lang, "adminProdStatusInactive")
    text = (
        f"🛒 <b>{escape_html(updated['name'])}</b>\n" +
        f"{t(lang, 'adminPrice')}: {fmt_amount(updated['price'])}\n" +
        f"{t(lang, 'adminUserStatus')}: {status}\n" +
        f"🚚 {delivery_type_label(updated['delivery_type'], lang)}" +
        (f"\n📦 {t(lang, 'stock')}: {stock}" if stock is not None else "")
    )
    rows = [
        [
            InlineKeyboardButton(t(lang, "adminEditName"), callback_data=f"adm:editprodname:{pid}"),
            InlineKeyboardButton(t(lang, "adminPrice"), callback_data=f"adm:editprodprice:{pid}"),
        ],
        [InlineKeyboardButton(t(lang, "adminEditDesc"), callback_data=f"adm:editproddesc:{pid}")],
        [
            InlineKeyboardButton(t(lang, "adminToggle", updated["is_active"]), callback_data=f"adm:toggleprod:{pid}"),
            InlineKeyboardButton(t(lang, "adminDeleteProd"), callback_data=f"adm:deleteprod:{pid}"),
        ],
    ]
    if updated["delivery_type"] == "inventory":
        rows.append([InlineKeyboardButton(t(lang, "adminAddProd"), callback_data=f"adm:stock:{pid}")])
    rows.append([InlineKeyboardButton(t(lang, "adminBackToCat"), callback_data=f"adm:prodcat:{updated['category_id']}")])
    rows.append([InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, text, InlineKeyboardMarkup(rows))


async def adm_deleteprod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    pid = int(update.callback_query.data.split(":")[2])
    product = await svc.get_product(pid)
    cat_id = product["category_id"] if product else None
    await svc.delete_product(pid)
    await update.callback_query.answer(t(lang, "adminProdDeleted"), show_alert=True)
    rows = []
    if cat_id:
        rows.append([InlineKeyboardButton(t(lang, "adminBackToCat"), callback_data=f"adm:prodcat:{cat_id}")])
    rows.append([InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, t(lang, "adminProdDeleted"), InlineKeyboardMarkup(rows))


async def adm_stockmenu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    await safe_edit(update, t(lang, "adminStockMenu"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    pid = int(update.callback_query.data.split(":")[2])
    context.user_data["awaiting"] = {"action": "admin_add_stock", "data": {"product_id": pid}}
    await safe_edit(update, t(lang, "adminAddStockPrompt"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    orders = await svc.get_all_recent_orders(15)
    if not orders:
        await safe_edit(update, t(lang, "adminNoOrders"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    text = t(lang, "adminRecentOrders") + "\n\n"
    for o in orders:
        text += f"#{o['id']} — {escape_html(o['product_name'])} — {fmt_amount(o['price'])} — {order_status_label(o['status'], lang)}\n"
    await safe_edit(update, text, InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    pending = await svc.get_pending_manual_orders(20)
    if not pending:
        await safe_edit(update, t(lang, "adminNoPending"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    rows = [[InlineKeyboardButton(f"#{o['id']} — {o['product_name']}", callback_data=f"adm:fulfill:{o['id']}")] for o in pending]
    rows.append([InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, t(lang, "adminPendingTitle"), InlineKeyboardMarkup(rows))


async def adm_fulfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    order_id = int(update.callback_query.data.split(":")[2])
    order = await svc.get_order(order_id)
    if not order or order["status"] != "pending":
        await safe_edit(update, t(lang, "adminOrderNotPending"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminBackToPending"), callback_data="adm:pending")]]))
        return
    context.user_data["awaiting"] = {"action": "admin_fulfill_order", "data": {"order_id": order_id}}
    await safe_edit(
        update,
        t(lang, "adminFulfill", str(order_id)) + "\n\n" + t(lang, "adminFulfillPrompt"),
        InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminBackToPending"), callback_data="adm:pending")]]),
    )


async def adm_recharges(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    lst = await svc.get_pending_recharges()
    if not lst:
        await safe_edit(update, t(lang, "adminNoRecharges"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    rows = [[InlineKeyboardButton(f"#{r['id']} — {fmt_amount(r['amount'])} — {r['method']}", callback_data=f"adm:recharge:{r['id']}")] for r in lst]
    rows.append([InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, t(lang, "adminRechargeTitle"), InlineKeyboardMarkup(rows))


async def adm_recharge_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    rid = int(update.callback_query.data.split(":")[2])
    req = await svc.get_recharge(rid)
    if not req:
        await safe_edit(update, t(lang, "adminNoRecharges"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    req_user = await svc.get_user_by_id(req["user_id"])
    text = t(lang, "adminRechargeDetail", str(req["id"]), fmt_amount(req["amount"]), req["method"],
             escape_html(req_user["first_name"] or "") + f" ({req_user['telegram_id']})" if req_user else "—")
    await safe_edit(update, text, InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "adminRechargeApprove"), callback_data=f"adm:rechapprove:{rid}"),
            InlineKeyboardButton(t(lang, "adminRechargeReject"), callback_data=f"adm:rechreject:{rid}"),
        ],
        [InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")],
    ]))


async def adm_rechapprove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    lang = user.get("language", "ar")
    rid = int(update.callback_query.data.split(":")[2])
    result = await svc.approve_recharge(rid)
    await update.callback_query.answer(t(lang, "adminRechargeApproved") if result else t(lang, "adminError"), show_alert=True)
    if result:
        user_lang = result.get("language", "ar")
        try:
            await context.bot.send_message(
                result["telegram_id"],
                t(user_lang, "rechargeApproved", fmt_amount(result["amount"]), fmt_amount(result["new_balance"])),
            )
        except Exception:
            pass
        await notify_channel(
            context.bot,
            t("en", "channelRecharge",
              escape_html(result.get("first_name") or "User"),
              fmt_amount(result["amount"]), "👤 Manual Transfer"),
        )
        await safe_edit(update, t(lang, "adminRechargeApproved"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_rechreject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    lang = user.get("language", "ar")
    rid = int(update.callback_query.data.split(":")[2])
    result = await svc.reject_recharge(rid)
    await update.callback_query.answer(t(lang, "adminRechargeRejected") if result else t(lang, "adminError"), show_alert=True)
    if result:
        user_lang = result.get("language", "ar")
        try:
            await context.bot.send_message(result["telegram_id"], t(user_lang, "rechargeRejected", fmt_amount(result["amount"])))
        except Exception:
            pass
        await safe_edit(update, t(lang, "adminRechargeRejected"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    users = await svc.list_users(50)
    if not users:
        await safe_edit(update, t(lang, "adminNoUsers"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    rows = [
        [InlineKeyboardButton(
            f"{'🚫 ' if u['is_banned'] else ''}{u.get('first_name') or ''} (@{u.get('username') or '—'}) — {fmt_amount(u['balance'])}",
            callback_data=f"adm:userdetail:{u['id']}",
        )]
        for u in users
    ]
    rows.append([InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")])
    await safe_edit(update, t(lang, "adminUsersTitle"), InlineKeyboardMarkup(rows))


async def adm_userdetail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    uid = int(update.callback_query.data.split(":")[2])
    target = await svc.get_user_by_id(uid)
    if not target:
        await safe_edit(update, t(lang, "adminUserNotFound"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))
        return
    text = (
        f"👤 <b>{escape_html(target.get('first_name') or '')}</b>\n" +
        f"{t(lang, 'adminUserId')}: {target['telegram_id']}\n" +
        f"{t(lang, 'adminUserUsername')}: @{target.get('username') or '—'}\n" +
        f"{t(lang, 'adminUserBalance')}: {fmt_amount(target['balance'])}\n" +
        f"{t(lang, 'adminUserAdmin')}: {'✅' if target['is_admin'] else '❌'}\n" +
        f"{t(lang, 'adminUserStatus')}: {t(lang, 'adminUserBanned') if target['is_banned'] else t(lang, 'adminUserActive')}"
    )
    ban_action = "unban" if target["is_banned"] else "ban"
    await safe_edit(update, text, InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "adminUnban" if target["is_banned"] else "adminBan"), callback_data=f"adm:ban:{uid}:{ban_action}")],
        [InlineKeyboardButton(t(lang, "adminAdjustBalance"), callback_data=f"adm:adjbal:{uid}")],
        [InlineKeyboardButton(t(lang, "adminBackToUsers"), callback_data="adm:users")],
    ]))


async def adm_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    lang = user.get("language", "ar")
    parts = update.callback_query.data.split(":")
    uid = int(parts[2])
    action = parts[3]
    updated = await svc.set_user_ban(uid, action == "ban")
    success_key = "adminBanSuccess" if action == "ban" else "adminUnbanSuccess"
    await update.callback_query.answer(t(lang, success_key) if updated else t(lang, "adminError"), show_alert=True)
    if updated:
        ban_action = "unban" if updated["is_banned"] else "ban"
        text = (
            f"👤 <b>{escape_html(updated.get('first_name') or '')}</b>\n" +
            f"{t(lang, 'adminUserId')}: {updated['telegram_id']}\n" +
            f"{t(lang, 'adminUserStatus')}: {t(lang, 'adminUserBanned') if updated['is_banned'] else t(lang, 'adminUserActive')}"
        )
        await safe_edit(update, text, InlineKeyboardMarkup([
            [InlineKeyboardButton(t(lang, "adminUnban" if updated["is_banned"] else "adminBan"), callback_data=f"adm:ban:{uid}:{ban_action}")],
            [InlineKeyboardButton(t(lang, "adminAdjustBalance"), callback_data=f"adm:adjbal:{uid}")],
            [InlineKeyboardButton(t(lang, "adminBackToUsers"), callback_data="adm:users")],
        ]))


async def adm_adjbal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    uid = int(update.callback_query.data.split(":")[2])
    context.user_data["awaiting"] = {"action": "admin_adjust_balance", "data": {"user_id": uid}}
    await safe_edit(update, t(lang, "adjustPrompt"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    context.user_data["awaiting"] = {"action": "admin_broadcast"}
    await safe_edit(update, t(lang, "adminBroadcastPrompt"),
                    InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "adminCancel"), callback_data="adm:users")]]))


async def adm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    s = await svc.get_stats()
    text = (
        t(lang, "adminStatsTitle") + "\n\n" +
        f"{t(lang, 'adminStatsUsers')}: {s['users']}\n" +
        f"{t(lang, 'adminStatsCategories')}: {s['categories']}\n" +
        f"{t(lang, 'adminStatsProducts')}: {s['products']}\n" +
        f"{t(lang, 'adminStatsAvailableStock')}: {s['available_stock']}\n" +
        f"{t(lang, 'adminStatsCompletedOrders')}: {s['completed_orders']}\n" +
        f"{t(lang, 'adminStatsRevenue')}: {fmt_amount(s['revenue'])}"
    )
    await safe_edit(update, text, InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "back"), callback_data="adm:main")]]))


async def adm_newprod_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_user(update, context)
    if not user or not user.get("is_admin"):
        return
    await update.callback_query.answer()
    lang = user.get("language", "ar")
    awaiting = context.user_data.get("awaiting")
    if not awaiting or awaiting.get("action") != "admin_product_delivery_choice":
        await safe_edit(update, t(lang, "sessionExpired"), admin_menu_keyboard(lang))
        return
    delivery_type = update.callback_query.data.split(":")[2]
    d = awaiting.get("data", {})
    product = await svc.create_product(
        category_id=int(d.get("category_id", 0)),
        name=str(d.get("name", "")),
        description=d.get("description"),
        price=float(d.get("price", 0)),
        delivery_type=delivery_type,
    )
    context.user_data.pop("awaiting", None)
    text = t(lang, "adminProdCreated", product["name"])
    if delivery_type == "inventory":
        text += "\n\n" + t(lang, "adminAddStockNow")
    rows = []
    if delivery_type == "inventory":
        rows.append([InlineKeyboardButton(t(lang, "adminAddProd"), callback_data=f"adm:stock:{product['id']}")])
    rows.append([InlineKeyboardButton(t(lang, "adminBackToCat"), callback_data=f"adm:prodcat:{product['category_id']}")])
    await safe_edit(update, text, InlineKeyboardMarkup(rows))


# ── Application Setup ─────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    await init_db()
    me = await application.bot.get_me()
    set_bot_username(me.username or "")
    logger.info(f"Bot started: @{me.username}")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(menu_main, pattern="^menu:main$"))
    app.add_handler(CallbackQueryHandler(menu_lang, pattern="^menu:lang$"))
    app.add_handler(CallbackQueryHandler(lang_set, pattern="^lang:(ar|en)$"))
    app.add_handler(CallbackQueryHandler(menu_help, pattern="^menu:help$"))
    app.add_handler(CallbackQueryHandler(menu_balance, pattern="^menu:balance$"))
    app.add_handler(CallbackQueryHandler(menu_referral, pattern="^menu:referral$"))
    app.add_handler(CallbackQueryHandler(menu_shop, pattern="^menu:shop$"))
    app.add_handler(CallbackQueryHandler(cat_view, pattern=r"^cat:\d+$"))
    app.add_handler(CallbackQueryHandler(prod_view, pattern=r"^prod:\d+$"))
    app.add_handler(CallbackQueryHandler(buy_product, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(menu_orders, pattern="^menu:orders$"))
    app.add_handler(CallbackQueryHandler(menu_recharge, pattern="^menu:recharge$"))
    app.add_handler(CallbackQueryHandler(rc_stars, pattern="^rc:stars$"))
    app.add_handler(CallbackQueryHandler(rcstars_amount, pattern=r"^rcstars:\d+$"))
    app.add_handler(CallbackQueryHandler(rc_manual, pattern="^rc:manual$"))
    app.add_handler(CallbackQueryHandler(rc_binance, pattern="^rc:binance$"))
    app.add_handler(CallbackQueryHandler(check_binance_payment, pattern=r"^binance_check:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(rc_chain, pattern=r"^rc:chain:\w+$"))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    app.add_handler(CallbackQueryHandler(channel_check, pattern="^channel:check$"))
    app.add_handler(CallbackQueryHandler(adm_main, pattern="^adm:main$"))
    app.add_handler(CallbackQueryHandler(adm_channel, pattern="^adm:channel$"))
    app.add_handler(CallbackQueryHandler(adm_channel_set, pattern="^adm:channel:set$"))
    app.add_handler(CallbackQueryHandler(adm_channel_remove, pattern="^adm:channel:remove$"))
    app.add_handler(CallbackQueryHandler(adm_cats, pattern="^adm:cats$"))
    app.add_handler(CallbackQueryHandler(adm_addcat, pattern="^adm:addcat$"))
    app.add_handler(CallbackQueryHandler(adm_catview, pattern=r"^adm:catview:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_editcatname, pattern=r"^adm:editcatname:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_editcatdesc, pattern=r"^adm:editcatdesc:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_togglecat, pattern=r"^adm:togglecat:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_deletecat, pattern=r"^adm:deletecat:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_prods, pattern="^adm:prods$"))
    app.add_handler(CallbackQueryHandler(adm_prodcat, pattern=r"^adm:prodcat:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_addprod, pattern=r"^adm:addprod:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_prodview, pattern=r"^adm:prodview:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_editprodname, pattern=r"^adm:editprodname:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_editprodprice, pattern=r"^adm:editprodprice:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_editproddesc, pattern=r"^adm:editproddesc:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_toggleprod, pattern=r"^adm:toggleprod:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_deleteprod, pattern=r"^adm:deleteprod:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_stockmenu, pattern="^adm:stockmenu$"))
    app.add_handler(CallbackQueryHandler(adm_stock, pattern=r"^adm:stock:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_orders, pattern="^adm:orders$"))
    app.add_handler(CallbackQueryHandler(adm_pending, pattern="^adm:pending$"))
    app.add_handler(CallbackQueryHandler(adm_fulfill, pattern=r"^adm:fulfill:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_recharges, pattern="^adm:recharges$"))
    app.add_handler(CallbackQueryHandler(adm_recharge_detail, pattern=r"^adm:recharge:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_rechapprove, pattern=r"^adm:rechapprove:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_rechreject, pattern=r"^adm:rechreject:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_users, pattern="^adm:users$"))
    app.add_handler(CallbackQueryHandler(adm_userdetail, pattern=r"^adm:userdetail:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_ban, pattern=r"^adm:ban:\d+:(ban|unban)$"))
    app.add_handler(CallbackQueryHandler(adm_adjbal, pattern=r"^adm:adjbal:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_broadcast, pattern="^adm:broadcast$"))
    app.add_handler(CallbackQueryHandler(adm_stats, pattern="^adm:stats$"))
    app.add_handler(CallbackQueryHandler(adm_newprod_delivery, pattern=r"^adm:newprod_delivery:(inventory|manual)$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Starting bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
