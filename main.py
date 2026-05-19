from configs.watchlist import WATCHLIST
from configs.timeframes import TIMEFRAMES

from data.providers.yfinance_provider import YFinanceProvider

from core.indicators.rsi_indicator import RSIEngine
from core.strategies.rsi_strategy import RSIStrategy


provider = YFinanceProvider()

rsi_engine = RSIEngine()

rsi_strategy = RSIStrategy()

selected_timeframe = TIMEFRAMES["1 Hour"]


for stock in WATCHLIST:

    print(f"\nFetching data for: {stock}")

    df = provider.fetch_data(
        symbol=stock,
        interval=selected_timeframe,
        period="5d"
    )

    if df.empty:
        print("No data found")
        continue

    # Calculate RSI
    df = rsi_engine.calculate(df)

    # Latest row
    latest = df.iloc[-1]

    latest_rsi = latest["RSI"]

    signal = rsi_strategy.generate_signal(latest_rsi)

    print("\nLATEST SIGNAL")

    print(f"Stock     : {stock}")
    print(f"Close     : {round(latest['Close'], 2)}")
    print(f"RSI       : {round(latest_rsi, 2)}")
    print(f"Signal    : {signal}")