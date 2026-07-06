# ============================================================
# core/execution/sandbox_client.py
#
# Upstox Sandbox client — places orders in the sandbox using the
# official SDK with Configuration(sandbox=True).
#
# CONFIRMED live (03-Jul-2026): OrderApiV3.place_order returns
#   {'status':'success','data':{'order_ids':['...']}}
# so order id = resp.data.order_ids[0].
#
# Broker-agnostic on the outside: place_order() takes an OrderRequest
# (from the Order Manager) and returns a simple result dict. Going
# live later = flip sandbox=False (same SDK, same code).
#
# Requires: pip install upstox-python-sdk
#           .env: UPSTOX_SANDBOX_ACCESS_TOKEN
# ============================================================

import os
import logging
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("sandbox_client")


class SandboxClient:

    def __init__(self, sandbox: bool = True):
        self._token   = os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "")
        self._sandbox = sandbox
        self._order_api = None
        self._ready = False

        if not self._token:
            log.warning("UPSTOX_SANDBOX_ACCESS_TOKEN not set — sandbox client disabled")
            return

        try:
            import upstox_client
            cfg = upstox_client.Configuration(sandbox=sandbox)
            cfg.access_token = self._token
            self._upstox   = upstox_client
            self._order_api = upstox_client.OrderApiV3(
                upstox_client.ApiClient(cfg)
            )
            self._ready = True
        except ImportError:
            log.error("upstox-python-sdk not installed (pip install upstox-python-sdk)")
        except Exception as e:
            log.error(f"SandboxClient init failed: {e}")

    @property
    def ready(self) -> bool:
        return self._ready

    def place_order(self, order, instrument_key: str) -> dict:
        """
        Place a LIMIT order in the sandbox from an OrderRequest.

        order: OrderRequest (from OrderManager) — has side, quantity,
               price, symbol.
        instrument_key: Upstox key for the symbol (e.g. NSE_EQ|INE...).

        Returns: {"ok": bool, "order_id": str|None, "error": str|None}
        """
        if not self._ready:
            return {"ok": False, "order_id": None, "error": "sandbox client not ready"}

        try:
            body = self._upstox.PlaceOrderV3Request(
                quantity=int(order.quantity),
                product="D",
                validity="DAY",
                price=float(order.price),
                tag="paper",
                instrument_token=instrument_key,
                order_type="LIMIT",
                transaction_type=order.side,   # "BUY" / "SELL"
                disclosed_quantity=0,
                trigger_price=0.0,
                is_amo=False,
            )
            resp = self._order_api.place_order(body)

            # Confirmed response shape: resp.data.order_ids -> list
            order_id = None
            data = getattr(resp, "data", None)
            if data is not None:
                ids = getattr(data, "order_ids", None)
                if ids:
                    order_id = ids[0]
            # some SDK versions return dict-like
            if order_id is None and isinstance(resp, dict):
                order_id = (resp.get("data", {}).get("order_ids") or [None])[0]

            if order_id:
                return {"ok": True, "order_id": str(order_id), "error": None}
            return {"ok": False, "order_id": None, "error": f"no order_id in response: {resp}"}

        except Exception as e:
            # SDK raises ApiException with .status/.body; capture cleanly
            body = getattr(e, "body", None)
            status = getattr(e, "status", None)
            msg = f"{type(e).__name__}"
            if status:
                msg += f" status={status}"
            if body:
                msg += f" body={body}"
            else:
                msg += f": {e}"
            log.warning(f"place_order failed for {order.symbol}: {msg}")
            return {"ok": False, "order_id": None, "error": msg}