import aiohttp
from config import TRONSCAN_API_KEY
from dataclasses import dataclass


@dataclass
class VerifyResult:
    verified: bool
    amount: float | None = None
    symbol: str | None = None
    error: str | None = None


async def verify_tron_tx(tx_hash: str, expected_wallet: str) -> VerifyResult:
    if not TRONSCAN_API_KEY:
        return VerifyResult(verified=False, error="TronScan API key not configured.")

    url = f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx_hash}"
    headers = {"TRON-PRO-API-KEY": TRONSCAN_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return VerifyResult(verified=False, error=f"API error {resp.status}")
                data = await resp.json()

        if not data or data.get("contractRet") != "SUCCESS":
            return VerifyResult(verified=False, error="Transaction not confirmed or failed.")

        trc20 = data.get("trc20TransferInfo", [])
        if trc20:
            transfer = trc20[0]
            to_addr = transfer.get("to_address", "").lower()
            if expected_wallet.lower() not in to_addr and to_addr not in expected_wallet.lower():
                return VerifyResult(verified=False, error="Transaction destination does not match.")
            amount_raw = float(transfer.get("amount_str", transfer.get("amount", 0)))
            decimals = int(transfer.get("decimals", 6))
            amount = amount_raw / (10 ** decimals)
            symbol = transfer.get("symbol", "TRC20")
            return VerifyResult(verified=True, amount=amount, symbol=symbol)

        contract = data.get("contractData", {})
        to_addr = contract.get("to_address", contract.get("toAddress", "")).lower()
        if not to_addr:
            return VerifyResult(verified=False, error="Cannot determine destination address.")
        if expected_wallet.lower() not in to_addr and to_addr not in expected_wallet.lower():
            return VerifyResult(verified=False, error="Transaction destination does not match.")
        amount_raw = float(contract.get("amount", 0))
        amount = amount_raw / 1_000_000
        return VerifyResult(verified=True, amount=amount, symbol="TRX")

    except Exception as e:
        return VerifyResult(verified=False, error=str(e))
