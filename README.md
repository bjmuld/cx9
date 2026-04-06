# Mazda CX-9 — Market Pricing Analysis

Scrapes used-car listing pages saved from four major marketplaces, extracts
year / mileage / trim features from each listing, and trains three scikit-learn
regression models to estimate fair-market value.  The full pipeline runs end-to-end
from raw HTML to a six-figure illustrated report in a single command.

---

## Data

### Sources

Listing pages were saved directly from the browser (*File → Save Page As →
Web Page, Complete*) for the Sacramento, CA metro area, filtered to
Mazda CX-9 model years 2018 and newer.

| Source | Listings scraped | Notes |
|---|---|---|
| Cars.com | 76 | `<fuse-card data-listing-id>` web components |
| AutoTrader | 25 | `data-cmp` attribute selectors |
| CarGurus | 18 | `data-cg-ft` attribute selectors |
| CarMax | 13 | `.kmx-car-tile` Material UI cards |
| **Total** | **132 clean** | 143 unique rows; 11 removed by quality filters |

### Features extracted

| Feature | Type | Notes |
|---|---|---|
| `year` | int | Model year; 2018–2023 in this dataset |
| `mileage` | int | Odometer miles |
| `trim` | ordinal str | One of six canonical levels (see below) |
| `price` | float (target) | Asking price in USD |

Trim levels are encoded as an ordered categorical so the model treats
higher trims as proportionally more valuable:

```
sport  <  touring  <  touring plus  <  carbon edition  <  grand touring  <  signature
```

### Coverage summary

| Dimension | Range / Breakdown |
|---|---|
| Price | $12,500 – $33,599  (mean $25,962, median $26,723) |
| Mileage | 12,433 – 168,000 mi  (mean 47,083) |
| Years | 2018 × 5 · 2019 × 13 · 2020 × 8 · 2021 × 18 · 2022 × 13 · 2023 × 75 |
| Trims | sport × 3 · touring × 45 · touring plus × 21 · carbon edition × 13 · grand touring × 34 · signature × 16 |

![Data overview — scatter, violin, box, and histogram panels](report/01_data_overview.png)

---

## Modelling approach

`report.py` runs the following steps automatically:

1. **Parse** all HTML files under `for-sale-listings/` and write
   `for-sale-listings/__processed/listings.csv`.
2. **Quality-filter** rows: price $5,000–$60,000, mileage ≤ 200,000,
   year ≥ 2018, trim must be one of the six canonical levels.
3. **Train each model type 20 times** using independent random 75 / 25
   train / validation splits (seeds 7, 20, 33 … 254).
4. **Record per-iteration metrics**: R², MAE, RMSE, and a prediction for
   the target vehicle on every run → `models/model_results.csv`.
5. **Generate six figures** summarising data diversity, model comparison,
   prediction confidence intervals, and residual analysis.

### Models

| Model | Pipeline |
|---|---|
| **Ridge (Poly-2)** | `StandardScaler` → `PolynomialFeatures(degree=2)` → `Ridge(α=1)` |
| **Random Forest** | `StandardScaler + OrdinalEncoder` → `RandomForestRegressor(200 trees, max_depth=6)` |
| **Gradient Boosting** | `StandardScaler + OrdinalEncoder` → `GradientBoostingRegressor(300 trees, lr=0.05)` |

Numeric features (year, mileage) are standardised; trim is ordinal-encoded
using the six-level order above.

---

## Results

### Model performance (20 independent 75/25 splits each)

| Model | Mean R² ± σ | R² range | Median MAE | Mean RMSE |
|---|---|---|---|---|
| Ridge (Poly-2) | **0.774 ± 0.092** | 0.534–0.895 | **$1,291** | **$1,781** |
| Random Forest | 0.762 ± 0.093 | 0.537–0.916 | $1,309 | $1,830 |
| Gradient Boosting | 0.708 ± 0.136 | 0.326–0.877 | $1,348 | $2,001 |

Ridge is the most consistent performer on this dataset, likely because the
relationship between year/mileage and price is close to quadratic and the
small sample size (132 rows) rewards a regularised parametric model.
Gradient Boosting shows the highest variance across splits, indicating it
is more sensitive to which 25 % of the data ends up in the validation set.

![Model comparison — R², MAE, and RMSE box plots across 20 splits](report/02_model_comparison.png)

### Target vehicle prediction

**2022 Mazda CX-9 Grand Touring @ 43,500 miles**

| Model | Mean prediction | 10th %ile | 90th %ile | CI width |
|---|---|---|---|---|
| Ridge (Poly-2) | $27,577 | $27,413 | $27,734 | **$322** |
| Random Forest | $27,582 | $27,133 | $28,243 | $1,110 |
| Gradient Boosting | $27,426 | $26,712 | $28,830 | $2,118 |

All three models converge on a fair-market value of roughly **$27,400–$27,600**.
The confidence intervals are derived from the spread of predictions across the
20 independent training runs and reflect how sensitive each model type is to
the particular training sample drawn.

![Target prediction — violin plots and summary table with 10%/90% CI bounds](report/03_target_prediction.png)

### Price vs. mileage with confidence bands

Mean model lines are drawn across the full mileage range for the target year (2022)
and trim (Grand Touring).  Shaded bands show the 10%–90% prediction interval
derived from the 20 cached trained models.  ★ marks the mean prediction for the
target vehicle.

![Price vs. mileage — all data points with CI bands from 20 trained models](report/04_price_vs_mileage_ci.png)

### Residual analysis

Actual vs. predicted and residuals vs. predicted for each model on a single
representative 75/25 split (seed 42).  A perfectly calibrated model would
have all points on the diagonal (top row) and a flat residual cloud centred
on zero (bottom row).

![Residual analysis — actual vs. predicted and residual scatter for each model](report/05_residuals.png)

### Metric stability across iterations

Each point is one independently trained model on a fresh random split.
The dashed line is the running mean.  Flat running means indicate stable
generalisation; a rising or falling trend would indicate data-order sensitivity.

![Metric evolution — R² and MAE per iteration with running mean](report/06_metric_evolution.png)

---

## Setup & usage

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Full report (parses all HTML, trains 60 models, writes 6 figures)
python report.py

# Or run the lighter interactive script
python analyze.py --model gbm --mileage 43500 --year 2022 --trim "grand touring"
```

### Adding new data

1. Save a search-result page from any supported site as an HTML file into
   the appropriate subfolder of `for-sale-listings/`.
2. Re-run `python report.py` — the parser auto-detects the site and the
   CSV and all figures regenerate automatically.

Supported sites: **Cars.com**, **AutoTrader**, **CarGurus**, **CarMax**.

### `analyze.py` CLI reference

| Flag | Default | Description |
|---|---|---|
| `--html-dir` | `for-sale-listings` | Root directory to search for HTML files |
| `--out-csv` | `for-sale-listings/__processed/listings.csv` | Output CSV path |
| `--model` | `gbm` | `ridge` / `rf` / `gbm` |
| `--year` | `2022` | Model year to predict for |
| `--trim` | `signature` | Trim level to predict for |
| `--mileage` | — | Print a fair-price estimate at this mileage |
| `--save-model` | — | Save the fitted pipeline to a `.joblib` file |

---

## Project layout

```
cx9/
├── for-sale-listings/            ← saved HTML search-result pages
│   ├── autotrader/
│   ├── cargurus/
│   ├── carmax/
│   ├── cars_com/
│   └── __processed/
│       └── listings.csv          ← cleaned, deduplicated listing data
├── models/
│   ├── __init__.py
│   ├── pricing.py                ← sklearn pipeline definitions
│   └── model_results.csv         ← per-iteration metrics from report.py
├── scraper/
│   ├── __init__.py
│   └── parser.py                 ← BeautifulSoup parsers for each site
├── report/                       ← generated figures (report.py output)
│   ├── 01_data_overview.png
│   ├── 02_model_comparison.png
│   ├── 03_target_prediction.png
│   ├── 04_price_vs_mileage_ci.png
│   ├── 05_residuals.png
│   └── 06_metric_evolution.png
├── report.py                     ← end-to-end report generator
├── analyze.py                    ← interactive CLI script
└── requirements.txt
```
