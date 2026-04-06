"""
analyze.py

End-to-end script: parse all HTML files in data/raw/, clean and
consolidate the listings, fit a pricing model, and print a report.

Run from the project root:
    python analyze.py

Optional flags:
    --html-dir   Path to directory containing raw HTML files  (default: data/raw)
    --out-csv    Path to save the processed listings CSV      (default: data/processed/listings.csv)
    --model      Model type: ridge | rf | gbm                 (default: gbm)
    --save-model Path to save the fitted model (joblib)       (optional)
    --mileage    Predict fair price for this mileage          (optional)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

# Ensure the project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).parent))

from models.pricing import PricingModel
from scraper.parser import parse_listings_from_file


# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------

def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="2022 Mazda CX-9 pricing analysis")
    p.add_argument("--html-dir",   default="for-sale-listings",                          help="Root directory to search (recursively) for HTML files")
    p.add_argument("--out-csv",    default="for-sale-listings/__processed/listings.csv", help="Where to write the cleaned listings CSV")
    p.add_argument("--model",      default="gbm", choices=["ridge", "rf", "gbm"], help="Estimator type")
    p.add_argument("--save-model", default=None,                              help="Save fitted model to this path (e.g. cx9_model.joblib)")
    p.add_argument("--mileage",    type=float, default=None,                  help="Predict fair price at this mileage")
    p.add_argument("--year",       type=int,   default=2022,                  help="Model year to predict for (default: 2022)")
    p.add_argument("--trim",       default="signature",                       help="Trim level to predict for (default: signature)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Scraping step
# ---------------------------------------------------------------------------

def collect_listings(html_dir: str | Path) -> pd.DataFrame:
    html_dir = Path(html_dir)
    # Recursively find all HTML files, including subdirectories
    html_files = list(html_dir.rglob("*.html")) + list(html_dir.rglob("*.htm"))

    if not html_files:
        print(f"[warn] No HTML files found under '{html_dir}'.")
        print("       Drop saved search-result pages there and re-run.")
        return pd.DataFrame(columns=["title", "year", "trim", "price", "mileage", "source"])

    all_listings: list[dict] = []
    for f in html_files:
        listings = parse_listings_from_file(f)
        print(f"  {f.relative_to(html_dir)}: {len(listings)} listings")
        all_listings.extend(listings)

    df = pd.DataFrame(all_listings)
    df = df.drop_duplicates(subset=["price", "mileage", "year", "trim"])
    return df


# ---------------------------------------------------------------------------
# Visualization helpers
# ---------------------------------------------------------------------------

def plot_price_vs_mileage(df: pd.DataFrame, model: PricingModel,
                          target_year: int = 2022, target_trim: str = "signature") -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle(f"Mazda CX-9 — Market Analysis (model line: {target_year} {target_trim.title()})", fontsize=13)

    clean = df.dropna(subset=["price", "mileage", "year", "trim"])

    # Scatter coloured by year
    years = sorted(clean["year"].unique())
    cmap = plt.cm.get_cmap("viridis", len(years))
    for i, yr in enumerate(years):
        subset = clean[clean["year"] == yr]
        axes[0].scatter(subset["mileage"], subset["price"],
                        alpha=0.65, edgecolors="k", linewidths=0.3,
                        color=cmap(i), label=str(yr), s=50)

    if len(clean) >= 5:
        mileage_vals = range(int(clean["mileage"].min()), int(clean["mileage"].max()) + 1, 1000)
        sweep = pd.DataFrame({
            "year":    [target_year] * len(list(mileage_vals)),
            "mileage": list(mileage_vals),
            "trim":    [target_trim] * len(list(mileage_vals)),
        })
        pred_prices = model.predict_batch(sweep)
        axes[0].plot(sweep["mileage"], pred_prices, color="red", linewidth=2,
                     label=f"{target_year} {target_trim.title()} fit")

    axes[0].legend(fontsize=7, ncol=2)
    axes[0].set_xlabel("Mileage")
    axes[0].set_ylabel("Price ($)")
    axes[0].set_title("Price vs. Mileage (by year)")
    axes[0].xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    axes[0].yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))

    # Price distribution
    axes[1].hist(clean["price"], bins=20, color="steelblue", edgecolor="white")
    axes[1].set_xlabel("Price ($)")
    axes[1].set_ylabel("Count")
    axes[1].set_title("Price Distribution (all years)")
    axes[1].xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))

    plt.tight_layout()
    out = Path("data/processed/price_vs_mileage.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out, dpi=150)
    print(f"\nPlot saved → {out}")
    plt.show()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _args()

    print("=== Collecting listings ===")
    df = collect_listings(args.html_dir)
    print(f"Total rows: {len(df)}")

    if not df.empty:
        out_csv = Path(args.out_csv)
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_csv, index=False)
        print(f"Listings saved → {out_csv}\n")

    # Summary stats
    if not df.empty and "price" in df.columns:
        clean = df.dropna(subset=["price"])
        print("=== Price summary ===")
        print(clean["price"].describe().apply(lambda x: f"${x:,.0f}" if x > 100 else f"{x:.1f}"))

    # Model
    if len(df.dropna(subset=["price", "mileage", "year", "trim"])) >= 5:
        print(f"\n=== Fitting {args.model} model ===")
        model = PricingModel(model_type=args.model)
        model.fit(df)
        model.report(df)

        if args.mileage is not None:
            fair_price = model.predict(year=args.year, mileage=args.mileage, trim=args.trim)
            print(f"\nFair market estimate @ {args.year} {args.trim.title()} {args.mileage:,.0f} mi: ${fair_price:,.0f}")

        if args.save_model:
            model.save(args.save_model)

        plot_price_vs_mileage(df, model, target_year=args.year, target_trim=args.trim)
    else:
        print("\n[info] Not enough clean data to fit a model yet.")
        print("       Add HTML files to data/raw/ and re-run.")


if __name__ == "__main__":
    main()
