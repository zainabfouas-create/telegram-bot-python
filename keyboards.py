from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from i18n import t


def main_menu_keyboard(user: dict) -> InlineKeyboardMarkup:
    lang = user.get("language", "ar")
    rows = [
        [InlineKeyboardButton(t(lang, "shop"), callback_data="menu:shop")],
        [
            InlineKeyboardButton(t(lang, "balance"), callback_data="menu:balance"),
            InlineKeyboardButton(t(lang, "recharge"), callback_data="menu:recharge"),
        ],
        [
            InlineKeyboardButton(t(lang, "orders"), callback_data="menu:orders"),
            InlineKeyboardButton(t(lang, "referralMenu"), callback_data="menu:referral"),
        ],
        [
            InlineKeyboardButton(t(lang, "help"), callback_data="menu:help"),
            InlineKeyboardButton(t(lang, "language"), callback_data="menu:lang"),
        ],
    ]
    if user.get("is_admin"):
        rows.append([InlineKeyboardButton(t(lang, "admin"), callback_data="adm:main")])
    return InlineKeyboardMarkup(rows)


def back_button(target: str = "menu:main", lang: str = "ar") -> InlineKeyboardButton:
    return InlineKeyboardButton(t(lang, "back"), callback_data=target)


def language_keyboard(lang: str = "ar") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇸🇦 العربية", callback_data="lang:ar")],
        [InlineKeyboardButton("🇺🇸 English", callback_data="lang:en")],
        [back_button("menu:main", lang)],
    ])


def admin_menu_keyboard(lang: str = "ar") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "adminCategories"), callback_data="adm:cats"),
            InlineKeyboardButton(t(lang, "adminProducts"), callback_data="adm:prods"),
        ],
        [
            InlineKeyboardButton(t(lang, "adminStock"), callback_data="adm:stockmenu"),
            InlineKeyboardButton(t(lang, "adminOrders"), callback_data="adm:orders"),
        ],
        [InlineKeyboardButton(t(lang, "adminPending"), callback_data="adm:pending")],
        [
            InlineKeyboardButton(t(lang, "adminRecharges"), callback_data="adm:recharges"),
            InlineKeyboardButton(t(lang, "adminUsers"), callback_data="adm:users"),
        ],
        [
            InlineKeyboardButton(t(lang, "adminStats"), callback_data="adm:stats"),
            InlineKeyboardButton(t(lang, "adminChannelMenu"), callback_data="adm:channel"),
        ],
        [InlineKeyboardButton(t(lang, "adminBroadcast"), callback_data="adm:broadcast")],
        [InlineKeyboardButton(t(lang, "mainMenu"), callback_data="menu:main")],
    ])
