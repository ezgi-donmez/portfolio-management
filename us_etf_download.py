"""01.04.2026–01.10.2026 (iki tarih dahil).
Cikti: Data/Raw/us/us_etf_daily_<START>_<END>.csv
Dosya adi tarih araligini icerir; farkli aralikla calistirmak eski dosyaya
eklemez, yeni dosya acar (eski 1 aylik dosyayla karismaz).
Guncel ABD ETF listesi kullanilir; USD fiyatlandirmasi varsayilir.
Getiri icin adj_close kullanirken temettuyu ikinci kez eklemeyin.
Not: .env icinde START_DATE / END_DATE varsa varsayilanlarin yerine onlar kullanilir.
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
    """Iki resmi listeyi bellekte birlestir; hisse ve test kayitlarini ele."""
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
        raise ValueError("ETF listesi bos veya semboller cakisti.")
    return result.sort_values("ticker").reset_index(drop=True)


def prepare_prices(data, instrument, start, end):
    """Tek ETF'nin tarihlerini ve fiyatlarini kontrol et; eksikleri doldurma."""
    data = data.copy()
    dates = pd.DatetimeIndex(pd.to_datetime(data.index))
    data.index = dates.tz_localize(None).normalize()
    data = data.loc[(data.index >= start) & (data.index <= end)]
    data = data.reindex(columns=FIELDS).apply(pd.to_numeric, errors="coerce")
    data = data.loc[data["Close"].gt(0)]
    data = data.loc[~data.index.duplicated(keep="last")]
    if data.empty:
        raise ValueError("Donemde gecerli fiyat donmedi.")
    data.columns = [f.lower().replace(" ", "_") for f in FIELDS]
    data.insert(0, "date", data.index.strftime("%Y-%m-%d"))
    for key in ("ticker", "nasdaq_symbol", "fund_name"):
        data[key] = instrument[key]
    data["market"], data["currency"] = "US", "USD"
    data["source"] = "Yahoo Finance via yfinance"
    data["retrieved_at_utc"] = pd.Timestamp.now(tz="UTC").isoformat()
    return data[COLUMNS].reset_index(drop=True)


def save(frames, path):
    """Ayni CSV'yi guvenli degistir; kalici ikinci dosya olusturma."""
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
            raise ValueError("Mevcut CSV sutunlari uyumsuz; dosya korunuyor.")
        valid = (pd.to_datetime(data["date"], errors="coerce").between(start, end).all()
                 and pd.to_numeric(data["close"], errors="coerce").gt(0).all()
                 and data["ticker"].notna().all() and not data.duplicated(["date", "ticker"]).any())
        if not valid:
            raise ValueError("Mevcut CSV tarih/fiyat kontrolunu gecemedi; dosya korunuyor.")
        if not data.empty:
            frames.append(data)
            existing = set(data["ticker"])
    pending = etfs.loc[~etfs["ticker"].isin(existing)].to_dict("records")
    print(f"ETF listesi: {len(etfs)} | CSV'de kayitli: {len(existing)} | Indirilecek: {len(pending)}")
    monitor = RateLimitMonitor()
    logger = logging.getLogger("yfinance")
    logger.addHandler(monitor)
    failed = downloaded = attempted = empty_batches = 0
    dirty = False
    try:
        for offset in range(0, len(pending), batch_size):
            group = pending[offset:offset + batch_size]
            symbols = [item["ticker"] for item in group]
            print(f"{offset + 1}–{offset + len(group)} / {len(pending)} indiriliyor...")
            attempted += len(group)
            successful = 0
            try:
                result = yf.download(symbols, start=str(start.date()),
                                     end=str((end + pd.Timedelta(days=1)).date()),
                                     interval="1d", group_by="ticker", auto_adjust=False,
                                     actions=True, threads=False, progress=False, timeout=30,
                                     ignore_tz=True, multi_level_index=True)
            except Exception as error:
                print(f"Indirme durdu: {error}")
                failed += len(group)
                break
            for item in group:
                try:
                    if result is None or result.empty:
                        raise ValueError("Bos sonuc veya erisim hatasi.")
                    if isinstance(result.columns, pd.MultiIndex):
                        part = result.xs(item["ticker"], axis=1, level=0)
                    elif len(group) == 1:
                        part = result
                    else:
                        raise ValueError("Beklenmeyen veri sutunlari.")
                    frames.append(prepare_prices(part, item, start, end))
                    successful += 1
                    downloaded += 1
                    dirty = True
                except (KeyError, ValueError, TypeError) as error:
                    failed += 1
                    print(f"{item['ticker']}: {error}")
            # Her ETF yerine 10 grupta bir yaz: daha az disk islemi.
            if dirty and (offset // batch_size + 1) % 10 == 0:
                save(frames, path)
                dirty = False
            empty_batches = empty_batches + 1 if successful == 0 else 0
            if monitor.limited or empty_batches >= 3:
                print("Erisim siniri veya art arda bos sonuc: durduruldu. Daha sonra tekrar calistirin.")
                break
            if offset + len(group) < len(pending):
                time.sleep(pause)
    except KeyboardInterrupt:
        print("Durduruldu; tamamlanan veriler kaydediliyor.")
    finally:
        logger.removeHandler(monitor)
        if dirty:
            save(frames, path)
    print(f"Yeni indirilen: {downloaded} | Bos/hata: {failed} | Denenmeyen: {len(pending) - attempted}")
    print(f"CSV'deki toplam ETF: {len(existing) + downloaded}")
    print(f"Dosya: {path}" if frames else "Fiyat alinamadi; bos CSV olusturulmadi.")
    if frames:
        report_coverage(pd.concat(frames, ignore_index=True), start, end)


def report_coverage(data, start, end):
    """Donemi tam kapsamayan ETF'leri raporla (donem icinde kurulan / kapanan / bosluklu)."""
    dates = pd.to_datetime(data["date"])
    calendar = dates.nunique()
    span = data.assign(_d=dates).groupby("ticker")["_d"].agg(["min", "max", "count"])
    late = span["min"] > dates.min()
    early_end = span["max"] < dates.max()
    gaps = span["count"] < calendar
    print(f"Tarih araligi: {dates.min().date()} – {dates.max().date()} | islem gunu: {calendar}")
    print(f"Donem icinde baslayan (gec listelenen): {late.sum()}")
    print(f"Donem bitmeden veri kesilen: {early_end.sum()}")
    print(f"Eksik gunu olan (toplam): {gaps.sum()} / {len(span)}")
    if dates.min() > start + pd.Timedelta(days=5) or dates.max() < end - pd.Timedelta(days=5):
        print("UYARI: Indirilen tarihler istenen araligin basini/sonunu kapsamiyor.")


def main():
    load_dotenv(ROOT / ".env")
    start = pd.Timestamp(os.getenv("START_DATE", "2026-04-01")).normalize()
    end = pd.Timestamp(os.getenv("END_DATE", "2026-10-01")).normalize()
    batch = int(os.getenv("BATCH_SIZE", "20"))
    pause = float(os.getenv("PAUSE_SECONDS", "3"))
    if pd.isna(start) or pd.isna(end) or start > end or not 1 <= batch <= 100 or pause < 0:
        raise ValueError("Tarih, BATCH_SIZE veya PAUSE_SECONDS ayarini kontrol edin.")
    folder = ROOT / "Data" / "Raw" / "us"
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Istenen aralik: {start.date()} – {end.date()} (iki tarih dahil)")
    path = folder / f"us_etf_daily_{start.date()}_{end.date()}.csv"
    collect(get_etfs(), path, start, end, batch, pause)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(f"Hata: {error}")
