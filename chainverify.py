import aiohttp
from dataclasses import dataclass

USDT_ERC20_CONTRACT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


@dataclass
class VerifyResult:
    verified: bool
    amount: float | None = None
    symbol: str | None = None
    usd_value: float | None = None
    error: str | None = None


async def get_usd_price(coin_id: str) -> float | None:
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                return float(data[coin_id]["usd"])
    except Exception:
        return None


async def verify_eth_tx(tx_hash: str, expected_wallet: str, api_key: str) -> VerifyResult:
    expected = expected_wallet.lower()

    try:
        async with aiohttp.ClientSession() as session:
            # Get transaction receipt
            url = (
                f"https://api.etherscan.io/api"
                f"?module=proxy&action=eth_getTransactionReceipt"
                f"&txhash={tx_hash}&apikey={api_key}"
            )
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return VerifyResult(verified=False, error="Etherscan API error.")
                data = await resp.json()

            receipt = data.get("result")
            if not receipt:
                return VerifyResult(verified=False, error="Transaction not found or not confirmed yet.")
            if receipt.get("status") != "0x1":
                return VerifyResult(verified=False, error="Transaction failed on the network.")

            # Check for USDT ERC20 Transfer event
            for log in receipt.get("logs", []):
                topics = log.get("topics", [])
                if (
                    log.get("address", "").lower() == USDT_ERC20_CONTRACT
                    and len(topics) >= 3
                    and topics[0].lower() == TRANSFER_TOPIC
                ):
                    to_addr = "0x" + topics[2][-40:]
                    if to_addr.lower() == expected:
                        amount_raw = int(log.get("data", "0x0"), 16)
                        amount = amount_raw / 1_000_000
                        return VerifyResult(verified=True, amount=amount, symbol="USDT", usd_value=amount)

            # Check plain ETH transfer
            url2 = (
                f"https://api.etherscan.io/api"
                f"?module=proxy&action=eth_getTransactionByHash"
                f"&txhash={tx_hash}&apikey={api_key}"
            )
            async with session.get(url2, timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                data2 = await resp2.json()

            tx = data2.get("result")
            if tx:
                to_addr = (tx.get("to") or "").lower()
                if to_addr == expected:
                    value_wei = int(tx.get("value", "0x0"), 16)
                    amount_eth = value_wei / 10 ** 18
                    if amount_eth > 0:
                        price = await get_usd_price("ethereum")
                        usd = round(amount_eth * price, 2) if price else None
                        return VerifyResult(verified=True, amount=amount_eth, symbol="ETH", usd_value=usd)

        return VerifyResult(verified=False, error="No matching transfer found to your wallet address.")

    except Exception as e:
        return VerifyResult(verified=False, error=str(e))


async def verify_aptos_tx(tx_hash: str, expected_wallet: str) -> VerifyResult:
    expected = expected_wallet.lower()
    url = f"https://fullnode.mainnet.aptoslabs.com/v1/transactions/by_hash/{tx_hash}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 404:
                    return VerifyResult(verified=False, error="Transaction not found.")
                if resp.status != 200:
                    return VerifyResult(verified=False, error=f"Aptos API error {resp.status}.")
                data = await resp.json()

        if not data.get("success"):
            return VerifyResult(verified=False, error="Transaction failed on the network.")

        # Check function payload for direct transfers
        payload = data.get("payload", {})
        fn = payload.get("function", "")
        args = payload.get("arguments", [])

        if ("aptos_account::transfer" in fn or "coin::transfer" in fn) and len(args) >= 2:
            recipient = str(args[0]).lower()
            if recipient == expected or expected in recipient:
                amount_raw = int(args[1])
                amount = amount_raw / 10 ** 8
                price = await get_usd_price("aptos")
                usd = round(amount * price, 2) if price else None
                return VerifyResult(verified=True, amount=amount, symbol="APT", usd_value=usd)

        # Check events for deposit
        for event in data.get("events", []):
            etype = event.get("type", "")
            if "DepositEvent" in etype or "CoinDeposit" in etype:
                edata = event.get("data", {})
                amount_raw = int(edata.get("amount", 0))
                if amount_raw > 0:
                    # verify recipient from changes
                    for change in data.get("changes", []):
                        addr = (change.get("address") or "").lower()
                        if addr == expected or expected in addr:
                            amount = amount_raw / 10 ** 8
                            price = await get_usd_price("aptos")
                            usd = round(amount * price, 2) if price else None
                            return VerifyResult(verified=True, amount=amount, symbol="APT", usd_value=usd)

        return VerifyResult(verified=False, error="No matching APT transfer found to your wallet address.")

    except Exception as e:
        return VerifyResult(verified=False, error=str(e))
