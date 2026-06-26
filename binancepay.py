import hashlib
import hmac
import base64
import time
import random
import string
import json
from dataclasses import dataclass
import httpx

BASE_URL = "https://bpay.binanceapi.com"


@dataclass
class OrderResult:
    success: bool
    checkout_url: str = ""
    merchant_trade_no: str = ""
    error: str = ""


@dataclass
class QueryResult:
    paid: bool
    status: str = ""
    amount: float = 0.0
    error: str = ""


def _nonce(length: int = 32) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def _sign(secret_key: str, timestamp: str, nonce: str, body: str) -> str:
    payload = f"{timestamp}\n{nonce}\n{body}\n"
    sig = hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha512).digest()
    return base64.b64encode(sig).decode()


def _headers(api_key: str, secret_key: str, body_str: str) -> dict:
    ts = str(int(time.time() * 1000))
    n = _nonce()
    return {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": ts,
        "BinancePay-Nonce": n,
        "BinancePay-Certificate-SN": api_key,
        "BinancePay-Signature": _sign(secret_key, ts, n, body_str),
    }


async def create_order(api_key: str, secret_key: str, trade_no: str, amount_usd: float) -> OrderResult:
    body = {
        "env": {"terminalType": "APP"},
        "merchantTradeNo": trade_no,
        "orderAmount": round(amount_usd, 2),
        "currency": "USDT",
        "description": "Balance recharge",
        "goodsDetails": [{
            "goodsType": "02",
            "goodsCategory": "Z000",
            "referenceGoodsId": "recharge",
            "goodsName": "Balance Recharge",
            "goodsDetail": f"Recharge {amount_usd:.2f} USD",
        }],
    }
    body_str = json.dumps(body, separators=(",", ":"))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{BASE_URL}/binancepay/openapi/v2/order",
                headers=_headers(api_key, secret_key, body_str),
                content=body_str,
            )
            data = resp.json()
        if data.get("status") == "SUCCESS" and data.get("data"):
            return OrderResult(
                success=True,
                checkout_url=data["data"]["checkoutUrl"],
                merchant_trade_no=trade_no,
            )
        return OrderResult(success=False, error=data.get("errorMessage") or str(data))
    except Exception as e:
        return OrderResult(success=False, error=str(e))


async def query_order(api_key: str, secret_key: str, trade_no: str) -> QueryResult:
    body = {"merchantTradeNo": trade_no}
    body_str = json.dumps(body, separators=(",", ":"))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{BASE_URL}/binancepay/openapi/v2/order/query",
                headers=_headers(api_key, secret_key, body_str),
                content=body_str,
            )
            data = resp.json()
        if data.get("status") == "SUCCESS" and data.get("data"):
            order_data = data["data"]
            status = order_data.get("status", "")
            amount = float(order_data.get("orderAmount", 0))
            return QueryResult(paid=(status == "PAID"), status=status, amount=amount)
        return QueryResult(paid=False, status="ERROR", error=data.get("errorMessage") or str(data))
    except Exception as e:
        return QueryResult(paid=False, status="ERROR", error=str(e))
