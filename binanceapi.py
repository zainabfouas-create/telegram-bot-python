"""
Regular Binance account API (binance.com) — Pay transaction verification.
Requires API key with 'Binance Pay' read permission enabled.
"""
import hmac
import hashlib
import time
import random
import httpx
from dataclasses import dataclass

BASE_URL = "https://api.binance.com"
VERIFY_WINDOW_MS = 60 * 60 * 1000  # 1 hour


@dataclass
class VerifyResult:
    verified: bool
    transaction_id: str = ""
    error: str = ""


def _sign(secret_key: str, query: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_unique_amount(base_amount: float) -> float:
    """Add random cents (01–99) to make the amount uniquely identifiable."""
    cents = random.randint(1, 99)
    return round(int(base_amount) + cents / 100, 2)


async def verify_payment(
    api_key: str,
    secret_key: str,
    expected_amount: float,
    start_time_ms: int,
) -> VerifyResult:
    """
    Query Binance Pay transaction history and look for a received USDT/BUSD
    payment matching expected_amount (±0.005) since start_time_ms.
    """
    ts = int(time.time() * 1000)
    end_time_ms = ts
    params = f"startTime={start_time_ms}&endTime={end_time_ms}&limit=100&timestamp={ts}"
    sig = _sign(secret_key, params)
    url = f"{BASE_URL}/sapi/v1/pay/transactions?{params}&signature={sig}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"X-MBX-APIKEY": api_key})
            data = resp.json()

        if str(data.get("code", "")) != "000000":
            err = data.get("msg") or str(data.get("code", "Unknown"))
            return VerifyResult(verified=False, error=err)

        for tx in data.get("data", []):
            funds = tx.get("fundsDetail") or []
            if not isinstance(funds, list):
                funds = [funds]

            for fund in funds:
                currency = (fund.get("currency") or "").upper()
                if currency not in ("USDT", "BUSD", "USD"):
                    continue
                try:
                    amt = float(fund.get("amount", 0))
                except (TypeError, ValueError):
                    continue

                if abs(amt - expected_amount) <= 0.005:
                    return VerifyResult(
                        verified=True,
                        transaction_id=tx.get("transactionId", ""),
                    )

        return VerifyResult(verified=False, error="No matching transaction found")

    except Exception as e:
        return VerifyResult(verified=False, error=str(e))
