"""DS588: download daily prices of all US-listed ETFs, 2026-04-01 to 2026-10-01 (both included).

Output: Data/Raw/us/us_etf_daily_<START>_<END>.csv
The file name contains the date range, so running with another range creates a new
file instead of appending to an old one.
The ETF list is today's official Nasdaq Trader list (so ETFs that closed earlier are
not included). Prices are in USD.
For returns use adj_close and do NOT add the dividends column again (it is already
inside adj_close).
Optional: START_DATE / END_DATE / BATCH_SIZE / PAUSE_SECONDS in a .env file override
the defaults.
"""
import io
import logging
import os
from pathlib import Path
import time

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume",
          "Dividends", "Stock Splits", "Capital Gains"]
COLUMNS = ["date", "ticker", "nasdaq_symbol", "fund_name", "market", "currency",
           "source", "retrieved_at_utc"] + [f.lower().replace(" ", "_") for f in FIELDS]


class RateLimitMonitor(logging.Handler):
    limited = False

    def emit(self, record):
        text = record.getMessage().lower()
        if any(s in text for s in ("429", "rate-limit", "rate limited", "too many requests", "yfratelimiterror")):
            self.limited = True


def get_etfs():
    """Merge the two official symbol lists in memory; keep ETFs only, drop test issues."""
    frames = []
    for filename, symbol in [("nasdaqlisted.txt", "Symbol"), ("otherlisted.txt", "ACT Symbol")]:
        response = requests.get(f"https://www.nasdaqtrader.com/dynamic/SymDir/{filename}", timeout=60)
        response.raise_for_status()
        data = pd.read_csv(io.StringIO(response.text), sep="|", dtype=str)
        data = data.loc[data["ETF"].eq("Y") & data["Test Issue"].eq("N"), [symbol, "Security Name"]]
        data.columns = ["nasdaq_symbol", "fund_name"]
        frames.append(data.dropna(subset=["nasdaq_symbol"]))
    result = pd.concat(frames).drop_duplicates("nasdaq_symbol")
    result["ticker"] = result["nasdaq_symbol"].str.replace(".", "-", regex=False)
    if result.empty or result["ticker"].duplicated().any():
        raise ValueError("ETF list is empty or symbols collide.")
    return result.sort_values("ticker").reset_index(drop=True)


def prepare_prices(data, instrument, start, end):
    """Check one ETF's dates and prices; never fill missing days."""
    data = data.copy()
    dates = pd.DatetimeIndex(pd.to_datetime(data.index))
    data.index = dates.tz_localize(None).normalize()
    data = data.loc[(data.index >= start) & (data.index <= end)]
    data = data.reindex(columns=FIELDS).apply(pd.to_numeric, errors="coerce")
    data = data.loc[data["Close"].gt(0)]
    data = data.loc[~data.index.duplicated(keep="last")]
    if data.empty:
        raise ValueError("No valid price in the period.")
    data.columns = [f.lower().replace(" ", "_") for f in FIELDS]
    data.insert(0, "date", data.index.strftime("%Y-%m-%d"))
    for key in ("ticker", "nasdaq_symbol", "fund_name"):
        data[key] = instrument[key]
    data["market"], data["currency"] = "US", "USD"
    data["source"] = "Yahoo Finance via yfinance"
    data["retrieved_at_utc"] = pd.Timestamp.now(tz="UTC").isoformat()
    return data[COLUMNS].reset_index(drop=True)


def save(frames, path):
    """Replace the CSV safely (write to a temp file, then rename); never leave a second copy."""
    if frames:
        data = pd.concat(frames, ignore_index=True).sort_values(["date", "ticker"])
        temporary = path.with_suffix(".tmp")
        data.to_csv(temporary, index=False, encoding="utf-8-sig")
        temporary.replace(path)


def collect(etfs, path, start, end, batch_size=20, pause=3):
    frames, existing = [], set()
    if path.exists():
        data = pd.read_csv(path)
        if not set(COLUMNS).issubset(data.columns):
            raise ValueError("Existing CSV has unexpected columns; file left unchanged.")
        valid = (pd.to_datetime(data["date"], errors="coerce").between(start, end).all()
                 and pd.to_numeric(data["close"], errors="coerce").gt(0).all()
                 and data["ticker"].notna().all() and not data.duplicated(["date", "ticker"]).any())
        if not valid:
            raise ValueError("Existing CSV failed the date/price check; file left unchanged.")
        if not data.empty:
            frames.append(data)
            existing = set(data["ticker"])
    pending = etfs.loc[~etfs["ticker"].isin(existing)].to_dict("records")
    print(f"ETF list: {len(etfs)} | already in CSV: {len(existing)} | to download: {len(pending)}")
    monitor = RateLimitMonitor()
    logger = logging.getLogger("yfinance")
    logger.addHandler(monitor)
    failed = downloaded = attempted = empty_batches = 0
    dirty = False
    try:
        for offset in range(0, len(pending), batch_size):
            group = pending[offset:offset + batch_size]
            symbols = [item["ticker"] for item in group]
            print(f"Downloading {offset + 1}-{offset + len(group)} / {len(pending)}...")
            attempted += len(group)
            successful = 0
            try:
                result = yf.download(symbols, start=str(start.date()),
                                     end=str((end + pd.Timedelta(days=1)).date()),
                                     interval="1d", group_by="ticker", auto_adjust=False,
                                     actions=True, threads=False, progress=False, timeout=30,
                                     ignore_tz=True, multi_level_index=True)
            except Exception as error:
                print(f"Download stopped: {error}")
                failed += len(group)
                break
            for item in group:
                try:
                    if result is None or result.empty:
                        raise ValueError("Empty result or access error.")
                    if isinstance(result.columns, pd.MultiIndex):
                        part = result.xs(item["ticker"], axis=1, level=0)
                    elif len(group) == 1:
                        part = result
                    else:
                        raise ValueError("Unexpected data columns.")
                    frames.append(prepare_prices(part, item, start, end))
                    successful += 1
                    downloaded += 1
                    dirty = True
                except (KeyError, ValueError, TypeError) as error:
                    failed += 1
                    print(f"{item['ticker']}: {error}")
            # Save every 10 batches instead of after every ETF (fewer disk writes).
            if dirty and (offset // batch_size + 1) % 10 == 0:
                save(frames, path)
                dirty = False
            empty_batches = empty_batches + 1 if successful == 0 else 0
            if monitor.limited or empty_batches >= 3:
                print("Rate limit or repeated empty results: stopped. Run again later to continue.")
                break
            if offset + len(group) < len(pending):
                time.sleep(pause)
    except KeyboardInterrupt:
        print("Interrupted; saving what was downloaded.")
    finally:
        logger.removeHandler(monitor)
        if dirty:
            save(frames, path)
    print(f"Newly downloaded: {downloaded} | empty/error: {failed} | not tried: {len(pending) - attempted}")
    print(f"Total ETFs in CSV: {len(existing) + downloaded}")
    print(f"File: {path}" if frames else "No prices received; no empty CSV was created.")
    if frames:
        report_coverage(pd.concat(frames, ignore_index=True), start, end)


def report_coverage(data, start, end):
    """Report ETFs that do not cover the whole period (launched late, ended early, or with gaps)."""
    dates = pd.to_datetime(data["date"])
    calendar = dates.nunique()
    span = data.assign(_d=dates).groupby("ticker")["_d"].agg(["min", "max", "count"])
    late = span["min"] > dates.min()
    early_end = span["max"] < dates.max()
    gaps = span["count"] < calendar
    print(f"Date range: {dates.min().date()} to {dates.max().date()} | trading days: {calendar}")
    print(f"Started during the period (listed late): {late.sum()}")
    print(f"Data ends before the period ends: {early_end.sum()}")
    print(f"With missing days (total): {gaps.sum()} / {len(span)}")
    if dates.min() > start + pd.Timedelta(days=5) or dates.max() < end - pd.Timedelta(days=5):
        print("WARNING: downloaded dates do not cover the start/end of the requested range.")


def main():
    load_dotenv(ROOT / ".env")
    start = pd.Timestamp(os.getenv("START_DATE", "2026-04-01")).normalize()
    end = pd.Timestamp(os.getenv("END_DATE", "2026-10-01")).normalize()
    batch = int(os.getenv("BATCH_SIZE", "20"))
    pause = float(os.getenv("PAUSE_SECONDS", "3"))
    if pd.isna(start) or pd.isna(end) or start > end or not 1 <= batch <= 100 or pause < 0:
        raise ValueError("Check the dates, BATCH_SIZE or PAUSE_SECONDS settings.")
    folder = ROOT / "Data" / "Raw" / "us"
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Requested range: {start.date()} to {end.date()} (both included)")
    path = folder / f"us_etf_daily_{start.date()}_{end.date()}.csv"
    collect(get_etfs(), path, start, end, batch, pause)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(f"Error: {error}")
