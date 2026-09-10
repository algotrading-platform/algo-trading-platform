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
        self._sandbox = sandbox
        self._token   = ""
        self._order_api = None
        self._ready = False
        self._rebuild_client()

    def _rebuild_client(self, force: bool = False) -> None:
        """
        (Sep 10) SandboxClient used to read UPSTOX_SANDBOX_ACCESS_TOKEN
        ONCE at construction and cache it in self._token forever --
        fine for the scanner job (a fresh process every 5 min) but
        wrong for ws_listener.py, which caches ONE PaperTrader/
        SandboxClient singleton for the whole container's lifetime.
        Confirmed live, Sep 10: every single trade attempt failed with
        401 "Invalid token" for over an hour, while a brand-new process
        reading the SAME env var at the SAME time succeeded -- the
        singleton's token had simply gone stale relative to whatever
        Upstox now considers valid, and nothing ever re-read it. Only
        fix that doesn't need a container restart every time this
        recurs: re-read the env var and rebuild the API client here,
        called both at construction and defensively before every
        place_order() call below.
        """
        token = os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "")
        if not token:
            if self._token:  # only warn once per actual transition, not every call
                log.warning("UPSTOX_SANDBOX_ACCESS_TOKEN not set — sandbox client disabled")
            self._token, self._ready = "", False
            return
        if not force and token == self._token and self._ready:
            return  # unchanged and already working -- nothing to rebuild

        try:
            import upstox_client
            cfg = upstox_client.Configuration(sandbox=self._sandbox)
            cfg.access_token = token
            self._upstox   = upstox_client
            self._order_api = upstox_client.OrderApiV3(
                upstox_client.ApiClient(cfg)
            )
            self._token  = token
            self._ready  = True
        except ImportError:
            log.error("upstox-python-sdk not installed (pip install upstox-python-sdk)")
            self._ready = False
        except Exception as e:
            log.error(f"SandboxClient init failed: {e}")
            self._ready = False

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

        Retries once, with a forced client rebuild, on an auth-looking
        (401) failure (Sep 10) -- confirmed live: a long-running
        singleton's cached client can start failing with 401 even
        though the env var's token is genuinely fine (a brand-new
        process reading the same variable at the same moment
        succeeded), for over an hour, on every single trade, entirely
        silently (the caller only alerts on a successful open). A
        container restart always fixed it, meaning the failure was in
        this object's own cached SDK client state, not upstream --
        so rebuild-and-retry here removes the need for that restart.
        """
        self._rebuild_client()  # cheap no-op if the token hasn't changed and is already working
        if not self._ready:
            return {"ok": False, "order_id": None, "error": "sandbox client not ready"}

        result = self._place_order_once(order, instrument_key)
        if result["ok"] or "401" not in (result.get("error") or ""):
            return result

        log.warning(f"place_order 401 for {order.symbol} -- forcing client rebuild and retrying once")
        self._rebuild_client(force=True)
        if not self._ready:
            return result
        return self._place_order_once(order, instrument_key)

    def _place_order_once(self, order, instrument_key: str) -> dict:
        try:
            body = self._upstox.PlaceOrderV3Request(
                quantity=int(order.quantity),
                product=order.product,
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