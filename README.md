# 2022 Mazda CX-9 — Pricing Analysis

Scrape car-listing search results and build a statistical pricing model
using scikit-learn.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Workflow

### 1 — Collect data

Save search-result pages from any of these sites as `.html` files into `data/raw/`:

- [Cars.com](https://www.cars.com/shopping/results/?dealer_id=&keyword=mazda+cx9&list_price_max=&list_price_min=&makes[]=mazda&maximum_distance=all&mileage_max=&models[]=mazda-cx_9&stock_type=used&year_max=2022&year_min=2022&zip=)  
- [CarGurus](https://www.cargurus.com/Cars/new/nl_Mazda_CX-9_d2344#listing=)  
- [AutoTrader](https://www.autotrader.com/cars-for-sale/used-cars/mazda/cx-9/)  
- [CarMax](https://www.carmax.com/cars/mazda/cx-9)

Most browsers: **File → Save Page As → Web Page, Complete (or HTML only)**.

### 2 — Run the analysis

```bash
python analyze.py
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--html-dir` | `data/raw` | Directory containing raw HTML files |
| `--out-csv` | `data/processed/listings.csv` | Output CSV path |
| `--model` | `gbm` | `ridge` / `rf` / `gbm` |
| `--mileage` | — | Predict fair price at this mileage |
| `--save-model` | — | Save fitted model (`.joblib`) |

Example:
```bash
python analyze.py --model gbm --mileage 45000
```

### 3 — Explore in a notebook

```bash
jupyter notebook notebooks/
```

## Project layout

```
cx9/
├── data/
│   ├── raw/              ← drop .html search-result pages here
│   └── processed/        ← cleaned CSV + plot outputs
├── scraper/
│   ├── __init__.py
│   └── parser.py         ← BeautifulSoup HTML parser
├── models/
│   ├── __init__.py
│   └── pricing.py        ← sklearn pricing pipeline
├── notebooks/            ← Jupyter notebooks for exploration
├── analyze.py            ← main end-to-end script
├── requirements.txt
└── .gitignore
```
