"""
Regular Binance account API (binance.com) — Pay transaction verification.
Requires API key with 'Binance Pay' read permission enabled.

Verification strategy: user adds a unique reference code (e.g. RC1234) in the
Note/Remarks field of their Binance Pay transfer.  The bot searches the merchant's
recent transactions for that reference and verifies the amount matches.
"""
import hmac
import hashlib
import time
import httpx
from dataclasses import dataclass

BASE_URL = "https://api.binance.com"
VERIFY_WINDOW_MS = 3 * 60 * 60 * 1000  # 3 hours


@dataclass
class VerifyResult:
    verified: bool
    transaction_id: str = ""
    amount_found: float = 0.0
    error: str = ""


def _sign(secret_key: str, query: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def make_reference(req_id: int) -> str:
    """Short, easy-to-type reference code the user writes in the Note field."""
    return f"RC{req_id}"


async def verify_payment_by_note(
    api_key: str,
    secret_key: str,
    reference: str,
    expected_amount: float,
    start_time_ms: int,
) -> VerifyResult:
    """
    Search Binance Pay transaction history for a transaction whose
    note/remarks field contains `reference` and whose amount matches
    expected_amount (±2% to cover minor fees/rounding).

    Binance Pay /sapi/v1/pay/transactions response per item:
      transactionStatus, currency, orderAmount, note, remarks, transactionId …
    """
    ts = int(time.time() * 1000)
    params = f"startTime={start_time_ms}&endTime={ts}&limit=100&timestamp={ts}"
    sig = _sign(secret_key, params)
    url = f"{BASE_URL}/sapi/v1/pay/transactions?{params}&signature={sig}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"X-MBX-APIKEY": api_key})
            data = resp.json()

        if str(data.get("code", "")) != "000000":
            err = data.get("msg") or str(data.get("code", "Unknown"))
            geo_blocked = "restricted location" in err.lower() or "eligibility" in err.lower()
            return VerifyResult(verified=False, error="geo_blocked" if geo_blocked else err)

        ref_lower = reference.lower()

        for tx in data.get("data", []):
            status = (tx.get("transactionStatus") or "").upper()
            if status and status != "SUCCESS":
                continue

            # Check note / remarks / memo field (Binance uses different names)
            note_value = ""
            for field in ("note", "remarks", "remark", "memo", "description"):
                val = tx.get(field) or ""
                if val:
                    note_value = str(val)
                    break

            if ref_lower not in note_value.lower():
                continue

            # Reference found — verify currency and amount
            currency = (tx.get("currency") or "").upper()
            if currency not in ("USDT", "BUSD", "USD", ""):
                # Check fundsDetail fallback
                funds = tx.get("fundsDetail") or []
                if not isinstance(funds, list):
                    funds = [funds]
                matched = any(
                    (f.get("currency") or "").upper() in ("USDT", "BUSD", "USD")
                    for f in funds
                )
                if not matched:
                    continue

            try:
                amt = float(tx.get("orderAmount", 0))
            except (TypeError, ValueError):
                amt = 0.0

            # Allow ±2% tolerance for fees
            if expected_amount > 0 and abs(amt - expected_amount) / expected_amount > 0.02:
                return VerifyResult(
                    verified=False,
                    amount_found=amt,
                    error=f"amount_mismatch:{amt}",
                )

            return VerifyResult(
                verified=True,
                transaction_id=tx.get("transactionId", ""),
                amount_found=amt,
            )

        return VerifyResult(verified=False, error="not_found")

    except Exception as e:
        return VerifyResult(verified=False, error=str(e))
