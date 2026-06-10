import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL: str = os.environ["DATABASE_URL"]
TRONSCAN_API_KEY: str = os.getenv("TRONSCAN_API_KEY", "")
ADMIN_TELEGRAM_ID: str = os.getenv("ADMIN_TELEGRAM_ID", "")

CURRENCY: str = os.getenv("CURRENCY", "USD")
STAR_TO_CREDIT: float = float(os.getenv("STAR_TO_CREDIT", "0.013"))
STAR_PACKAGES: list[int] = [50, 100, 200, 500, 1000]
REFERRAL_MILESTONE: int = int(os.getenv("REFERRAL_MILESTONE", "10"))
REFERRAL_REWARD: float = float(os.getenv("REFERRAL_REWARD", "0.30"))

TRON_WALLET: str = os.getenv("TRON_WALLET_ADDRESS", "")

_bot_username: str = ""

def set_bot_username(username: str) -> None:
    global _bot_username
    _bot_username = username

def get_bot_username() -> str:
    return _bot_username

def fmt_amount(value) -> str:
    try:
        n = float(value)
        if n == int(n):
            return f"{int(n)} {CURRENCY}"
        return f"{n:.2f} {CURRENCY}"
    except (TypeError, ValueError):
        return f"{value} {CURRENCY}"
