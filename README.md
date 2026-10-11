# DS588 Algorithmic Portfolio Management: Phase 1

Data collection and preparation for a combined portfolio of Turkish investment funds (TEFAS) and US-listed ETFs.

Phase 1 does not choose investments. It collects and cleans daily prices and removes only the assets that cannot be used, each for a documented reason. Choosing among the eligible assets is the job of the Phase 2 algorithm.

## Files

| File | What it is |
|---|---|
| `abd_etf_download.py` | Downloads daily prices of all US-listed ETFs from Yahoo Finance |
| `phase1_preparation.ipynb` | Cleans both markets, applies the eligibility rules, writes the processed dataset, runs validation checks |
| `DATA_DICTIONARY.md` | Every column of every raw and processed file |
| `requirements.txt` | Python packages |

## How to reproduce

1. Install the packages: `pip install -r requirements.txt`
2. **US ETFs:** from the project root run `python abd_etf_download.py`. It writes `Data/Raw/us/us_etf_daily_2026-04-01_2026-10-01.csv`. If Yahoo rate-limits the download, run it again; it continues with the missing tickers.
3. **Turkish funds:** export them manually from the TEFAS website, as described in the notebook section "Raw data: where it comes from and how to reproduce it" (one month per export, all funds selected, separately for *Borsa Yatırım Fonları* and *Menkul Kıymet Yatırım Fonları*). Save the 12 CSV files in `Data/Raw/tr/` with `borsa` or `menkul` in the file name. TEFAS data could not be collected programmatically because repeated requests timed out and each request returned at most about one month of data.
4. **Notebook:** open `phase1_preparation.ipynb` from the project root and run all cells. It downloads USD/TRY once (saved to `Data/Raw/fx/`) and writes the outputs to `Data/Processed/`. The last cell stops with an error if any validation check fails.

```
DS588_project/
├── abd_etf_download.py
├── phase1_preparation.ipynb
├── Data/
│   ├── Raw/
│   │   ├── tr/   12 TEFAS exports
│   │   ├── us/   us_etf_daily_2026-04-01_2026-10-01.csv
│   │   └── fx/   usdtry_daily_2026-04-01_2026-10-01.csv
│   └── Processed/
│       ├── prices_long.csv
│       ├── universe_prices_wide.csv
│       ├── universe.csv
│       ├── instruments.csv
│       ├── fx_usdtry.csv
│       └── data_quality_report.csv
```

## Result

| | TEFAS funds | US ETFs |
|---|---|---|
| With at least one valid price | 2,085 | 5,746 |
| Eligible | 707 | 4,201 |

Window 2026-04-01 to 2026-10-01, daily, 132 dates. Prices in local currency (TRY, USD); USD/TRY in `fx_usdtry.csv`.

## Data sources and redistribution

TEFAS (tefas.gov.tr), Yahoo Finance via `yfinance`, Nasdaq Trader symbol directory. Where redistribution of the raw data is not allowed, the steps above are the retrieval instructions. Yahoo recalculates adjusted prices when new dividends are paid, so a later download can differ slightly; the `retrieved_at_utc` column records when my snapshot was taken.

