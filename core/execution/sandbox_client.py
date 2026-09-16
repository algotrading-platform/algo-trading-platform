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
import time
import hashlib
import logging
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("sandbox_client")


def _fingerprint(token: str) -> str:
    """Short, non-secret stand-in for a token so logs can show whether
    the deployed secret actually changed between rebuilds/deploys,
    without ever printing the token itself."""
    if not token:
        return "<empty>"
    return f"len={len(token)} sha256[:8]={hashlib.sha256(token.encode()).hexdigest()[:8]}"

# Sep 11 -- confirmed live, twice, with real production failures
# (RAMCOCEM.NS, then BAJAJFINSV.NS): a 401 from Upstox's sandbox can
# persist for LONGER than a few seconds -- BAJAJFINSV failed 401 on 4
# straight attempts spanning ~12s (the original MAX_401_RETRIES=3
# window), then the exact same token, same process, same replica
# succeeded (got past auth to a normal response) when re-tried
# manually ~2 minutes later with zero changes on our end. This is a
# genuine intermittent reliability issue on Upstox's sandbox side, not
# a bad/expired token -- proven by the token working again shortly
# after with nothing refreshed.
#
# SHORTENED BACK DOWN Sep 16 (Om, after a full 401 depth-analysis):
# place_order() is called SYNCHRONOUSLY, once per symbol, inside
# ws_listener.py's _scan_universe_for_patterns() -- a plain for loop
# over the WHOLE ~500-symbol universe, no threading (confirmed by
# reading that loop directly). The Sep 11 widening to 7x7s=~48s meant
# a single 401 on ONE symbol blocked evaluation of every OTHER symbol
# in that same cycle for up to 48s out of the 60s cadence -- exactly
# the kind of multi-ten-second blip this file itself was built to
# survive, just inflicted on the WHOLE universe by trying to outlast
# it in-place. That tradeoff made sense on Sep 11 because there was no
# other recovery path: a failed pattern-scan signal was simply lost.
# It no longer does, because Sep 16 also fixed exactly that gap --
# _execute_trade() now registers a `RETRY` row in pending_breakouts on
# any sandbox-error outcome, which the fast breakout-watch loop
# (BREAKOUT-WATCH, 60s cadence, up to its 15-min TTL) retries against
# the live price non-blockingly, in a SEPARATE cycle, without freezing
# pattern-scan for anyone else. So this only needs to cover the FAST
# failure mode (Sep 10's stale-singleton-client case, which a rebuild
# resolves near-instantly) -- a multi-minute Upstox-side blip (Sep
# 11's case) is now the RETRY path's job, not this blocking loop's.
MAX_401_RETRIES = 2
RETRY_401_DELAY_SEC = 2


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
            log.info(f"SandboxClient token loaded: {_fingerprint(token)}")
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

        Retries on an auth-looking (401) failure, with a forced client
        rebuild before each retry (Sep 10, extended Sep 11). Two
        distinct 401 failure modes confirmed live, both now covered:
          1. (Sep 10) A long-running singleton's cached SDK client can
             start failing with 401 even though the env var's token is
             genuinely fine -- a brand-new process reading the same
             variable at the same moment succeeded. A container restart
             always fixed it. One rebuild-and-retry covers this.
          2. (Sep 11) The token itself can be rejected with 401 for a
             few minutes and then work again with NO change on our end
             -- confirmed by directly re-placing an order with the same
             token moments after a real 401, and it succeeded (got past
             auth to a normal validation error). A single immediate
             retry can still land inside that same brief window, so
             this now retries up to MAX_401_RETRIES times with a short
             delay between attempts, giving a transient blip (on
             Upstox's sandbox side, not ours) time to clear.
        """
        # Sep 16 -- ALWAYS force a fresh client (and therefore a fresh
        # urllib3 connection pool) here, not just a no-op when the token
        # is unchanged. Confirmed by reading the Upstox SDK's own source
        # (upstox_client/api_client.py, rest.py): ApiClient.__init__
        # creates a brand-new urllib3.PoolManager every time it's
        # constructed, but _rebuild_client() without force= reuses the
        # SAME one indefinitely as long as the token hasn't changed --
        # meaning ws_listener.py's long-lived process (all day, one
        # process) can keep reusing the SAME HTTP connection pool for
        # hours, versus the old 5-min scanner-job architecture, where a
        # brand-new process (and therefore brand-new pool/connections)
        # placed every single order. A long-lived, mostly-idle keep-
        # alive connection silently going stale (e.g. a load balancer's
        # idle timeout closing it without either side being told) is a
        # well-known class of bug, and would explain exactly what's
        # been observed: identical token, identical everything, works
        # again right after a forced rebuild -- because a forced
        # rebuild is precisely what creates a fresh connection. Order
        # placement is rare (a handful of real signals a day at most),
        # so there is no meaningful cost to never reusing the pool.
        self._rebuild_client(force=True)
        if not self._ready:
            return {"ok": False, "order_id": None, "error": "sandbox client not ready"}

        result = self._place_order_once(order, instrument_key)
        attempt = 1
        while not result["ok"] and "401" in (result.get("error") or "") and attempt <= MAX_401_RETRIES:
            log.warning(f"place_order 401 for {order.symbol} -- attempt {attempt}/{MAX_401_RETRIES}, "
                        f"forcing client rebuild and retrying in {RETRY_401_DELAY_SEC}s")
            time.sleep(RETRY_401_DELAY_SEC)
            self._rebuild_client(force=True)
            if not self._ready:
                return result
            result = self._place_order_once(order, instrument_key)
            attempt += 1
        if not result["ok"] and "401" in (result.get("error") or ""):
            log.error(f"place_order for {order.symbol} still 401 after {attempt - 1} rebuild-retries -- "
                      f"token used throughout: {_fingerprint(self._token)}. If this fingerprint matches "
                      f"what was last deployed to the Container App secret, the deployed token itself is "
                      f"invalid/stale (not a transient blip) and needs to be re-pushed + the app restarted.")
        return result

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