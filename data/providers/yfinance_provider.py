# ============================================================
# data/providers/yfinance_provider.py
#
# Changes vs previous version:
#   - Handles both "Datetime" (intraday) and "Date" (daily/weekly)
#     index columns after reset_index — prevents KeyError on 1d/1wk
#   - Strips timezone from Date column (daily data has no TZ)
#   - Converts intraday UTC → IST correctly
#   - Handles commodity symbols (GC=F, SI=F, etc.) without crash
#   - Returns empty DataFrame cleanly on any error
# ============================================================

import yfinance as yf
import pandas as pd
from data.providers.base_provider import BaseDataProvider


class YFinanceProvider(BaseDataProvider):

    def fetch_data(
        self,
        symbol: str,
        interval: str = "1h",
        period: str = "1mo",
    ) -> pd.DataFrame:

        try:
            df = yf.download(
                tickers=symbol,
                interval=interval,
                period=period,
                auto_adjust=True,
                progress=False,
                threads=False,
            )
        except Exception:
            return pd.DataFrame()

        if df is None or df.empty:
            return pd.DataFrame()

        # --------------------------------------------------
        # Flatten MultiIndex columns (yfinance >=0.2.x bug)
        # --------------------------------------------------
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.reset_index(inplace=True)

        # --------------------------------------------------
        # Normalise the datetime column
        # yfinance uses "Datetime" for intraday intervals
        # and "Date" for 1d / 1wk
        # --------------------------------------------------
        if "Datetime" in df.columns:
            # Intraday: convert UTC → IST
            df["Datetime"] = (
                pd.to_datetime(df["Datetime"], utc=True)
                .dt.tz_convert("Asia/Kolkata")
            )

        elif "Date" in df.columns:
            # Daily / Weekly: no timezone, just normalise type
            df["Date"] = pd.to_datetime(df["Date"])
            df.rename(columns={"Date": "Datetime"}, inplace=True)

        # --------------------------------------------------
        # Ensure Close column is numeric (scalar, not Series)
        # --------------------------------------------------
        if "Close" in df.columns:
            df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
            df.dropna(subset=["Close"], inplace=True)

        return df
