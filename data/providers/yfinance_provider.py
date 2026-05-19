import yfinance as yf
import pandas as pd

from data.providers.base_provider import BaseDataProvider


class YFinanceProvider(BaseDataProvider):

    def fetch_data(self, symbol, interval="1h", period="1mo"):

        df = yf.download(
            tickers=symbol,
            interval=interval,
            period=period,
            auto_adjust=True,
            progress=False
        )

        if df.empty:
            return pd.DataFrame()

        # Flatten multi-index columns
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.reset_index(inplace=True)

        # Convert UTC to IST
        if "Datetime" in df.columns:
            df["Datetime"] = (
                pd.to_datetime(df["Datetime"], utc=True)
                .dt.tz_convert("Asia/Kolkata")
            )

        return df