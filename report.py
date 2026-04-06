#!/usr/bin/env python3
"""
report.py — Comprehensive model comparison and pricing report for the Mazda CX-9.

Steps performed:
  1. Parse ALL raw HTML sources → refresh data/processed/listings.csv
  2. Analyse training-data diversity and print coverage summary
  3. Train each model type (ridge, rf, gbm) 20× with independent 75/25 random splits
  4. Each iteration: record val R², MAE, RMSE + predict 2022 Grand Touring @ 43,500 mi
  5. Aggregate: 10%–90% prediction CI from cross-run spread; full per-model stats
  6. Save all 60 iteration results → data/processed/model_results.csv
  7. Generate 6 detailed figures → data/processed/report/

Usage:
    python report.py
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

# Ensure project root on sys.path when run directly
sys.path.insert(0, str(Path(__file__).parent))

import matplotlib
matplotlib.use("Agg")   # non-interactive; saves to files
import matplotlib.colors as mcolors
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split

from models.pricing import (
    FEATURE_COLS,
    MAX_MILEAGE,
    MAX_PRICE,
    MIN_PRICE,
    MIN_YEAR,
    TARGET_COL,
    TRIM_ORDER,
    _build_pipeline,
)
from models.markup import (
    SOURCE_MARGIN_OVERRIDE,
    WHOLESALE_MARGIN_HIGH,
    WHOLESALE_MARGIN_LOW,
    WHOLESALE_MARGIN_MID,
    MarkupModel,
    compute_premiums,
    summarize_by_group,
)
from scraper.parser import parse_listings_from_file

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HTML_DIR    = Path("for-sale-listings")
OUT_CSV     = Path("for-sale-listings/__processed/listings.csv")
RESULTS_CSV = Path("models/model_results.csv")
REPORT_DIR  = Path("report")

N_ITERATIONS  = 20
TRAIN_FRAC    = 0.75
MODEL_TYPES   = ["ridge", "rf", "gbm"]
MODEL_LABELS  = {
    "ridge": "Ridge (Poly-2)",
    "rf":    "Random Forest",
    "gbm":   "Gradient Boosting",
}
MODEL_COLORS = {
    "ridge": "#4e8cff",
    "rf":    "#2ecc71",
    "gbm":   "#e74c3c",
}

TARGET_YEAR    = 2022
TARGET_MILEAGE = 43_500
TARGET_TRIM    = "grand touring"
CI_LO, CI_HI   = 10, 90  # percentile bounds for prediction interval

# Formatter helpers
FMT_DOLLAR  = mticker.FuncFormatter(lambda x, _: f"${x:,.0f}")
FMT_DOLLAR_K = mticker.FuncFormatter(lambda x, _: f"${x/1000:.0f}K")
FMT_MILES_K  = mticker.FuncFormatter(lambda x, _: f"{x/1000:.0f}K")


# ---------------------------------------------------------------------------
# Step 1 — Refresh the combined CSV from every raw HTML file
# ---------------------------------------------------------------------------

def refresh_csv() -> pd.DataFrame:
    print("=" * 64)
    print("STEP 1 — Refreshing listings CSV from all raw HTML sources")
    print("=" * 64)

    html_files = sorted(HTML_DIR.rglob("*.html")) + sorted(HTML_DIR.rglob("*.htm"))
    if not html_files:
        raise FileNotFoundError(f"No HTML files found under {HTML_DIR}")

    all_rows: list[dict] = []
    for f in html_files:
        rows = parse_listings_from_file(f)   # no year filter → all CX-9 years
        rel  = f.relative_to(HTML_DIR)
        print(f"  {str(rel):<70s} {len(rows):3d} listings")
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    before = len(df)
    df = df.drop_duplicates(subset=["year", "price", "mileage", "trim"])
    after  = len(df)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    print(f"\n  Raw rows: {before}  →  Unique: {after}")
    print(f"  Saved → {OUT_CSV}\n")
    return df


# ---------------------------------------------------------------------------
# Shared cleaning (mirrors PricingModel._clean)
# ---------------------------------------------------------------------------

def clean_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = df.dropna(subset=[TARGET_COL, "mileage", "year", "trim"])
    df["year"]    = df["year"].astype(int)
    df["mileage"] = df["mileage"].astype(int)
    df["trim"]    = df["trim"].str.lower().str.strip()
    df = df[
        (df[TARGET_COL] >= MIN_PRICE) & (df[TARGET_COL] <= MAX_PRICE)
        & (df["mileage"] >= 0)        & (df["mileage"] <= MAX_MILEAGE)
        & (df["year"]    >= MIN_YEAR)
        & df["trim"].isin(TRIM_ORDER)
    ]
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Step 2 — Data diversity check
# ---------------------------------------------------------------------------

def check_diversity(df: pd.DataFrame) -> None:
    print("=" * 64)
    print("STEP 2 — Training-data diversity check")
    print("=" * 64)

    clean = clean_df(df)
    n = len(clean)
    print(f"\n  Clean rows usable for modelling: {n}\n")

    # ---- Year coverage ---------------------------------------------------
    yr_counts = clean["year"].value_counts().sort_index()
    print("  Year distribution:")
    for yr, cnt in yr_counts.items():
        bar = "█" * cnt
        print(f"    {yr}  {bar}  ({cnt})")

    # ---- Trim coverage ---------------------------------------------------
    trim_counts = clean["trim"].value_counts().reindex(TRIM_ORDER).fillna(0).astype(int)
    print("\n  Trim distribution:")
    for tm, cnt in trim_counts.items():
        bar = "█" * cnt
        coverage = "✓" if cnt >= 3 else "⚠ low"
        print(f"    {tm:<20s} {bar}  ({cnt}) {coverage}")

    # ---- Source coverage --------------------------------------------------
    src_counts = clean["source"].value_counts()
    print("\n  Listings per source:")
    for src, cnt in src_counts.items():
        print(f"    {src:<15s} {cnt}")

    # ---- Continuous feature ranges ---------------------------------------
    print(f"\n  Mileage range:  {clean['mileage'].min():>8,} – {clean['mileage'].max():>8,} mi")
    print(f"  Price range:    ${clean['price'].min():>8,.0f} – ${clean['price'].max():>8,.0f}")
    print(f"  Year range:     {clean['year'].min()} – {clean['year'].max()}\n")

    # ---- Diversity verdict -----------------------------------------------
    issues: list[str] = []
    if yr_counts.nunique() < 3:
        issues.append("Fewer than 3 distinct years represented")
    if (trim_counts > 0).sum() < 3:
        issues.append("Fewer than 3 trim levels represented")
    if n < 30:
        issues.append(f"Only {n} clean rows — model may be unreliable")

    if issues:
        print("  ⚠  Diversity concerns:")
        for iss in issues:
            print(f"     • {iss}")
    else:
        print(f"  ✓  Data covers {yr_counts.nunique()} years, "
              f"{(trim_counts > 0).sum()} trim levels, {n} clean samples — "
              f"sufficient for reliable modelling.")
    print()


# ---------------------------------------------------------------------------
# Step 3 + 4 — 20 iterations per model: fit, evaluate, predict target
# ---------------------------------------------------------------------------

def run_trials(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, list]]:
    """
    Returns
    -------
    results : DataFrame — per-iteration metrics + target prediction
    fitted_pipes : dict[model_type -> list of 20 fitted pipelines]
    """
    print("=" * 64)
    print(f"STEP 3+4 — Training {len(MODEL_TYPES)} models × {N_ITERATIONS} iterations "
          f"({int(TRAIN_FRAC*100)}% train / {int((1-TRAIN_FRAC)*100)}% val)\n")
    print(f"  Target vehicle: {TARGET_YEAR} Mazda CX-9 {TARGET_TRIM.title()} "
          f"@ {TARGET_MILEAGE:,} miles")
    print("=" * 64)

    clean = clean_df(df)
    X_all = clean[FEATURE_COLS].values
    y_all = clean[TARGET_COL].values
    target_X = np.array([[TARGET_YEAR, TARGET_MILEAGE, TARGET_TRIM]])

    records: list[dict]           = []
    fitted_pipes: dict[str, list]  = {m: [] for m in MODEL_TYPES}

    for model_type in MODEL_TYPES:
        label = MODEL_LABELS[model_type]
        print(f"\n  {label}")
        print(f"  {'─' * (len(label))}")

        preds_target: list[float] = []

        for i in range(N_ITERATIONS):
            seed = i * 13 + 7
            X_tr, X_val, y_tr, y_val = train_test_split(
                X_all, y_all,
                test_size=1 - TRAIN_FRAC,
                random_state=seed,
            )

            pipe = _build_pipeline(model_type)
            pipe.fit(X_tr, y_tr)
            fitted_pipes[model_type].append(pipe)   # cache for figure reuse

            y_pred = pipe.predict(X_val)
            r2   = r2_score(y_val, y_pred)
            mae  = mean_absolute_error(y_val, y_pred)
            rmse = float(np.sqrt(mean_squared_error(y_val, y_pred)))

            target_price = float(pipe.predict(target_X)[0])
            preds_target.append(target_price)

            records.append({
                "model":        model_type,
                "model_label":  label,
                "iteration":    i + 1,
                "seed":         seed,
                "n_train":      len(X_tr),
                "n_val":        len(X_val),
                "r2":           round(r2,   4),
                "mae":          round(mae,  2),
                "rmse":         round(rmse, 2),
                "target_price": round(target_price, 2),
            })

            print(f"    [{i+1:02d}/20]  R²={r2:+.3f}  MAE=${mae:6,.0f}  "
                  f"RMSE=${rmse:6,.0f}  Target=${target_price:,.0f}")

        arr = np.array(preds_target)
        print(f"    ──►  Mean=${np.mean(arr):,.0f}  "
              f"[{CI_LO}%: ${np.percentile(arr, CI_LO):,.0f}  "
              f"{CI_HI}%: ${np.percentile(arr, CI_HI):,.0f}]")

    results = pd.DataFrame(records)
    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(RESULTS_CSV, index=False)
    print(f"\n  All 60 iteration results saved → {RESULTS_CSV}\n")
    return results, fitted_pipes


# ---------------------------------------------------------------------------
# Plot helpers
# ---------------------------------------------------------------------------

def _savefig(fig: plt.Figure, name: str) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / name
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved → {out}")


# ---------------------------------------------------------------------------
# Figure 1 — Data overview / diversity
# ---------------------------------------------------------------------------

def plot_data_overview(df: pd.DataFrame) -> None:
    clean = clean_df(df)
    years = sorted(clean["year"].unique())
    trim_present = [t for t in TRIM_ORDER if t in clean["trim"].unique()]

    try:
        cmap = plt.colormaps["plasma"].resampled(len(years))
    except AttributeError:
        cmap = plt.cm.get_cmap("plasma", len(years))

    fig = plt.figure(figsize=(17, 12))
    fig.suptitle(
        "Training Data Overview — Mazda CX-9 Listings",
        fontsize=15, fontweight="bold", y=0.99,
    )
    gs = gridspec.GridSpec(3, 3, figure=fig, hspace=0.50, wspace=0.38)

    # ── 1. Price vs Mileage (main scatter) ────────────────────────────────
    ax0 = fig.add_subplot(gs[0, :2])
    for i, yr in enumerate(years):
        s = clean[clean["year"] == yr]
        ax0.scatter(s["mileage"], s["price"], color=cmap(i), alpha=0.72,
                    edgecolors="k", linewidths=0.3, s=55, label=str(yr))
    ax0.xaxis.set_major_formatter(FMT_MILES_K)
    ax0.yaxis.set_major_formatter(FMT_DOLLAR_K)
    ax0.set_xlabel("Mileage")
    ax0.set_ylabel("Price")
    ax0.set_title("Price vs. Mileage (coloured by model year)")
    ax0.legend(title="Year", fontsize=7, ncol=2, framealpha=0.8)
    ax0.grid(True, alpha=0.25)

    # ── 2. Listings per source ─────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, 2])
    src_counts = clean["source"].value_counts()
    bar_colors = ["#4e8cff", "#2ecc71", "#e74c3c", "#f39c12", "#9b59b6"]
    bars = ax1.barh(src_counts.index, src_counts.values,
                    color=bar_colors[:len(src_counts)], edgecolor="k", linewidth=0.5)
    for bar, v in zip(bars, src_counts.values):
        ax1.text(v + 0.3, bar.get_y() + bar.get_height() / 2,
                 str(v), va="center", fontsize=9)
    ax1.set_xlabel("Number of listings")
    ax1.set_title("Listings per Source")

    # ── 3. Price by trim (violin) ──────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, :2])
    trim_data = [clean[clean["trim"] == t]["price"].values for t in trim_present]
    parts = ax2.violinplot(trim_data, positions=range(len(trim_present)),
                           showmedians=True, showextrema=True)
    for pc in parts["bodies"]:
        pc.set_facecolor("#4e8cff")
        pc.set_alpha(0.55)
    parts["cmedians"].set_color("black")
    ax2.set_xticks(range(len(trim_present)))
    ax2.set_xticklabels([t.title() for t in trim_present], rotation=18, ha="right")
    ax2.yaxis.set_major_formatter(FMT_DOLLAR_K)
    ax2.set_ylabel("Price")
    ax2.set_title("Price Distribution by Trim Level")
    ax2.grid(True, axis="y", alpha=0.25)

    # ── 4. Sample count by trim (bar) ─────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 2])
    trim_counts = clean["trim"].value_counts().reindex(TRIM_ORDER).fillna(0).astype(int)
    ax3.bar(range(len(TRIM_ORDER)),
            [trim_counts.get(t, 0) for t in TRIM_ORDER],
            color="#2ecc71", edgecolor="k", linewidth=0.5)
    ax3.set_xticks(range(len(TRIM_ORDER)))
    ax3.set_xticklabels([t.title() for t in TRIM_ORDER], rotation=30, ha="right")
    ax3.set_ylabel("Count")
    ax3.set_title("Sample Count by Trim Level")
    ax3.grid(True, axis="y", alpha=0.25)

    # ── 5. Price by year (box) ─────────────────────────────────────────────
    ax4 = fig.add_subplot(gs[2, :2])
    year_groups = [clean[clean["year"] == yr]["price"].values for yr in years]
    bp = ax4.boxplot(year_groups, labels=[str(y) for y in years], patch_artist=True,
                     medianprops=dict(color="black", linewidth=2))
    for patch, i in zip(bp["boxes"], range(len(years))):
        patch.set_facecolor(cmap(i))
        patch.set_alpha(0.6)
    ax4.yaxis.set_major_formatter(FMT_DOLLAR_K)
    ax4.set_xlabel("Model Year")
    ax4.set_ylabel("Price")
    ax4.set_title("Price Distribution by Model Year")
    ax4.grid(True, axis="y", alpha=0.25)

    # ── 6. Mileage histogram ───────────────────────────────────────────────
    ax5 = fig.add_subplot(gs[2, 2])
    ax5.hist(clean["mileage"], bins=20, color="#9b59b6", edgecolor="white", alpha=0.85)
    ax5.xaxis.set_major_formatter(FMT_MILES_K)
    ax5.set_xlabel("Mileage")
    ax5.set_ylabel("Count")
    ax5.set_title("Mileage Distribution")
    ax5.grid(True, axis="y", alpha=0.25)

    _savefig(fig, "01_data_overview.png")


# ---------------------------------------------------------------------------
# Figure 2 — Model validation-metric comparison (box plots)
# ---------------------------------------------------------------------------

def plot_model_comparison(results: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle(
        f"Model Performance — {N_ITERATIONS} Independent 75/25 Random Splits per Model",
        fontsize=13, fontweight="bold",
    )

    metric_specs = [
        ("r2",   "R² Score",     False),
        ("mae",  "MAE ($)",      True),
        ("rmse", "RMSE ($)",     True),
    ]

    for ax, (col, label, is_dollar) in zip(axes, metric_specs):
        groups = [results[results["model"] == m][col].values for m in MODEL_TYPES]
        bp = ax.boxplot(groups, patch_artist=True, notch=False, widths=0.5,
                        medianprops=dict(color="black", linewidth=2.5),
                        whiskerprops=dict(linewidth=1.5),
                        capprops=dict(linewidth=1.5))
        for patch, m in zip(bp["boxes"], MODEL_TYPES):
            patch.set_facecolor(MODEL_COLORS[m])
            patch.set_alpha(0.70)
        ax.set_xticks(range(1, len(MODEL_TYPES) + 1))
        ax.set_xticklabels([MODEL_LABELS[m] for m in MODEL_TYPES], rotation=12, ha="right")
        ax.set_title(label)
        ax.set_ylabel(label)
        if is_dollar:
            ax.yaxis.set_major_formatter(FMT_DOLLAR)
        ax.grid(True, axis="y", alpha=0.30)

        # Annotate median values above/below each box
        for i, vals in enumerate(groups):
            med = float(np.median(vals))
            fmt = f"${med:,.0f}" if is_dollar else f"{med:.3f}"
            ax.text(i + 1, med, f" {fmt}", va="bottom", fontsize=8, color="#222222")

    fig.tight_layout()
    _savefig(fig, "02_model_comparison.png")


# ---------------------------------------------------------------------------
# Figure 3 — Target vehicle prediction distributions + summary table
# ---------------------------------------------------------------------------

def plot_target_prediction(results: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    fig.suptitle(
        f"Price Prediction: {TARGET_YEAR} Mazda CX-9 {TARGET_TRIM.title()} "
        f"@ {TARGET_MILEAGE:,} miles",
        fontsize=13, fontweight="bold",
    )

    # ── Left: violin per model ─────────────────────────────────────────────
    ax = axes[0]
    for i, m in enumerate(MODEL_TYPES):
        vals = results[results["model"] == m]["target_price"].values
        parts = ax.violinplot(vals, positions=[i], showmedians=True,
                              showextrema=True, widths=0.65)
        for pc in parts["bodies"]:
            pc.set_facecolor(MODEL_COLORS[m])
            pc.set_alpha(0.60)
        parts["cmedians"].set_color("black")
        parts["cmedians"].set_linewidth(2)

        # 10%/90% dashed lines
        lo = np.percentile(vals, CI_LO)
        hi = np.percentile(vals, CI_HI)
        ax.hlines([lo, hi], i - 0.35, i + 0.35,
                  colors=MODEL_COLORS[m], linestyles="--", linewidth=2)
        ax.text(i + 0.38, lo, f"${lo:,.0f}", va="center", fontsize=8,
                color=MODEL_COLORS[m], fontweight="bold")
        ax.text(i + 0.38, hi, f"${hi:,.0f}", va="center", fontsize=8,
                color=MODEL_COLORS[m], fontweight="bold")

    ax.set_xticks(range(len(MODEL_TYPES)))
    ax.set_xticklabels([MODEL_LABELS[m] for m in MODEL_TYPES], fontsize=10)
    ax.yaxis.set_major_formatter(FMT_DOLLAR)
    ax.set_ylabel("Predicted Price ($)")
    ax.set_title(f"Prediction Spread ({N_ITERATIONS} runs)\n"
                 f"Dashed = {CI_LO}th / {CI_HI}th percentile bounds")
    ax.grid(True, axis="y", alpha=0.30)

    # ── Right: summary table ───────────────────────────────────────────────
    ax2 = axes[1]
    ax2.axis("off")

    col_labels = ["Model", "Mean", "Median", "Std Dev",
                  f"{CI_LO}% lower", f"{CI_HI}% upper", "Width"]
    rows_data  = []
    for m in MODEL_TYPES:
        vals = results[results["model"] == m]["target_price"].values
        lo   = np.percentile(vals, CI_LO)
        hi   = np.percentile(vals, CI_HI)
        rows_data.append([
            MODEL_LABELS[m],
            f"${np.mean(vals):,.0f}",
            f"${np.median(vals):,.0f}",
            f"${np.std(vals):,.0f}",
            f"${lo:,.0f}",
            f"${hi:,.0f}",
            f"${hi - lo:,.0f}",
        ])

    table = ax2.table(
        cellText=rows_data,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9.5)
    table.scale(1.1, 2.4)

    # Header row styling
    for j in range(len(col_labels)):
        cell = table[0, j]
        cell.set_facecolor("#2c3e50")
        cell.set_text_props(color="white", fontweight="bold")

    # Data row styling — tinted per model
    for row_i, m in enumerate(MODEL_TYPES):
        rgba = mcolors.to_rgba(MODEL_COLORS[m], alpha=0.18)
        for j in range(len(col_labels)):
            table[row_i + 1, j].set_facecolor(rgba)

    ax2.set_title(
        f"Prediction Summary\n(target: {TARGET_YEAR} GT @ {TARGET_MILEAGE:,} mi, "
        f"{N_ITERATIONS} bootstrap iterations)",
        pad=20, fontsize=10,
    )

    fig.tight_layout()
    _savefig(fig, "03_target_prediction.png")


# ---------------------------------------------------------------------------
# Figure 4 — Price vs Mileage with CI bands (reuses cached pipelines)
# ---------------------------------------------------------------------------

def plot_price_vs_mileage_with_ci(
    df: pd.DataFrame,
    fitted_pipes: dict[str, list],
) -> None:
    clean = clean_df(df)
    years = sorted(clean["year"].unique())

    try:
        cmap = plt.colormaps["plasma"].resampled(len(years))
    except AttributeError:
        cmap = plt.cm.get_cmap("plasma", len(years))

    mileage_sweep = np.linspace(
        max(0, clean["mileage"].min() - 2_000),
        clean["mileage"].max() + 5_000,
        250,
    )
    sweep_X = np.array([
        [TARGET_YEAR, mi, TARGET_TRIM] for mi in mileage_sweep
    ])
    target_X_pt = np.array([[TARGET_YEAR, TARGET_MILEAGE, TARGET_TRIM]])

    fig, ax = plt.subplots(figsize=(15, 8))
    ax.set_title(
        f"Price vs. Mileage — All Training Data + Model Projections\n"
        f"Model year: {TARGET_YEAR}  |  Trim: {TARGET_TRIM.title()}  |  "
        f"{CI_LO}%–{CI_HI}% CI shading from {N_ITERATIONS} cached trained models",
        fontsize=11,
    )

    # Scatter all training data coloured by year
    for i, yr in enumerate(years):
        s = clean[clean["year"] == yr]
        ax.scatter(s["mileage"], s["price"], color=cmap(i), alpha=0.45,
                   edgecolors="k", linewidths=0.2, s=38, zorder=2, label=str(yr))

    # Per-model CI using the 20 cached pipelines from run_trials
    print("  Building sweep CI bands from cached trained models …")
    for m in MODEL_TYPES:
        preds_mat = np.vstack([pipe.predict(sweep_X) for pipe in fitted_pipes[m]])
        lo_line   = np.percentile(preds_mat, CI_LO, axis=0)
        hi_line   = np.percentile(preds_mat, CI_HI, axis=0)
        mean_line = np.mean(preds_mat, axis=0)

        ax.plot(mileage_sweep, mean_line,
                color=MODEL_COLORS[m], linewidth=2.5,
                label=MODEL_LABELS[m], zorder=6)
        ax.fill_between(mileage_sweep, lo_line, hi_line,
                        color=MODEL_COLORS[m], alpha=0.14, zorder=3)

        pt_preds = [pipe.predict(target_X_pt)[0] for pipe in fitted_pipes[m]]
        pt_mean  = float(np.mean(pt_preds))
        ax.scatter([TARGET_MILEAGE], [pt_mean],
                   color=MODEL_COLORS[m], marker="*", s=320,
                   zorder=10, edgecolors="black", linewidths=0.8)

    # Vertical dashed line at target mileage
    ax.axvline(TARGET_MILEAGE, color="gray", linestyle=":", linewidth=1.5, alpha=0.7)
    ax.text(TARGET_MILEAGE + 500, clean["price"].min() + 500,
            f"{TARGET_MILEAGE:,} mi", fontsize=8, color="gray")

    # Legend: first group (years) + second group (models)
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles, labels, fontsize=8, ncol=4,
              loc="upper right", title="Year (scatter) / Model (line)", framealpha=0.9)

    ax.xaxis.set_major_formatter(FMT_MILES_K)
    ax.yaxis.set_major_formatter(FMT_DOLLAR_K)
    ax.set_xlabel("Mileage", fontsize=11)
    ax.set_ylabel("Price ($)", fontsize=11)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    _savefig(fig, "04_price_vs_mileage_ci.png")


# ---------------------------------------------------------------------------
# Figure 5 — Actual vs Predicted + residuals (one representative split)
# ---------------------------------------------------------------------------

def plot_residuals(df: pd.DataFrame) -> None:
    clean = clean_df(df)
    X_all = clean[FEATURE_COLS].values
    y_all = clean[TARGET_COL].values
    X_tr, X_val, y_tr, y_val = train_test_split(X_all, y_all,
                                                  test_size=0.25, random_state=42)

    fig, axes = plt.subplots(2, 3, figsize=(16, 10))
    fig.suptitle("Residual Analysis — Representative 75/25 Split (seed=42)",
                 fontsize=13, fontweight="bold")

    for j, m in enumerate(MODEL_TYPES):
        pipe = _build_pipeline(m)
        pipe.fit(X_tr, y_tr)
        y_pred    = pipe.predict(X_val)
        residuals = y_val - y_pred
        r2_v  = r2_score(y_val, y_pred)
        mae_v = mean_absolute_error(y_val, y_pred)
        rmse_v = float(np.sqrt(mean_squared_error(y_val, y_pred)))

        # Row 0: Actual vs Predicted
        ax0 = axes[0, j]
        lim0 = min(y_val.min(), y_pred.min()) - 1000
        lim1 = max(y_val.max(), y_pred.max()) + 1000
        ax0.scatter(y_val, y_pred, color=MODEL_COLORS[m], alpha=0.75,
                    edgecolors="k", linewidths=0.3, s=55)
        ax0.plot([lim0, lim1], [lim0, lim1], "k--", linewidth=1, alpha=0.6, label="Perfect fit")
        ax0.set_xlim(lim0, lim1)
        ax0.set_ylim(lim0, lim1)
        ax0.xaxis.set_major_formatter(FMT_DOLLAR_K)
        ax0.yaxis.set_major_formatter(FMT_DOLLAR_K)
        ax0.set_xlabel("Actual Price")
        ax0.set_ylabel("Predicted Price")
        ax0.set_title(f"{MODEL_LABELS[m]}\n"
                      f"R²={r2_v:.3f}  MAE=${mae_v:,.0f}  RMSE=${rmse_v:,.0f}")
        ax0.grid(True, alpha=0.25)

        # Row 1: Residuals vs Predicted
        ax1 = axes[1, j]
        ax1.scatter(y_pred, residuals, color=MODEL_COLORS[m], alpha=0.75,
                    edgecolors="k", linewidths=0.3, s=55)
        ax1.axhline(0, color="black", linestyle="--", linewidth=1.5)
        ax1.xaxis.set_major_formatter(FMT_DOLLAR_K)
        ax1.yaxis.set_major_formatter(FMT_DOLLAR)
        ax1.set_xlabel("Predicted Price")
        ax1.set_ylabel("Residual (Actual − Predicted)")
        ax1.set_title(f"Residuals vs. Predicted\nMean residual=${np.mean(residuals):+,.0f}")
        ax1.grid(True, alpha=0.25)

    fig.tight_layout()
    _savefig(fig, "05_residuals.png")


# ---------------------------------------------------------------------------
# Figure 6 — Metric evolution across iterations (stability chart)
# ---------------------------------------------------------------------------

def plot_metric_evolution(results: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle(
        f"Metric Stability — {N_ITERATIONS} Independent Splits per Model\n"
        f"(each point is a freshly trained model on a different random 75/25 partition)",
        fontsize=12, fontweight="bold",
    )

    specs = [
        ("r2",  "R² Score",  False),
        ("mae", "MAE ($)",   True),
    ]

    for ax, (col, label, is_dollar) in zip(axes, specs):
        for m in MODEL_TYPES:
            sub = results[results["model"] == m].sort_values("iteration")
            ax.plot(sub["iteration"], sub[col],
                    marker="o", markersize=5,
                    color=MODEL_COLORS[m], label=MODEL_LABELS[m],
                    linewidth=1.8, alpha=0.85)
            # Running mean
            running_mean = sub[col].expanding().mean()
            ax.plot(sub["iteration"], running_mean,
                    color=MODEL_COLORS[m], linestyle="--",
                    linewidth=1.0, alpha=0.45)

        ax.set_xlabel("Iteration")
        ax.set_ylabel(label)
        ax.set_title(label + " per Iteration  (dashed = running mean)")
        ax.legend(fontsize=9, framealpha=0.8)
        ax.grid(True, alpha=0.28)
        ax.set_xticks(range(1, N_ITERATIONS + 1))
        if is_dollar:
            ax.yaxis.set_major_formatter(FMT_DOLLAR)

    fig.tight_layout()
    _savefig(fig, "06_metric_evolution.png")


# ---------------------------------------------------------------------------
# Step 6 — Markup analysis (consensus premium + industry-benchmark cost basis)
# ---------------------------------------------------------------------------

def run_markup_analysis(
    df_raw: pd.DataFrame,
) -> tuple[pd.DataFrame, MarkupModel]:
    """
    Trains a full-data consensus Ridge model, computes per-listing premiums
    (Method A), applies industry-benchmark wholesale estimates (Method B), and
    fits a MarkupModel to predict expected premium % from vehicle features.

    Returns
    -------
    df_markup : DataFrame — cleaned listings augmented with markup columns
    markup_model : fitted MarkupModel
    """
    print("=" * 64)
    print("STEP 6 — Dealer markup estimation")
    print("=" * 64)

    clean = clean_df(df_raw)

    # Consensus pipeline trained on ALL clean data — the goal here is
    # "market consensus value for each listing", not held-out prediction.
    consensus_pipe = _build_pipeline("ridge")
    consensus_pipe.fit(clean[FEATURE_COLS].values, clean[TARGET_COL].values)

    df_markup = compute_premiums(clean, consensus_pipe)

    # Fit markup model on the enriched data
    markup_model = MarkupModel()
    markup_model.fit(df_markup)

    # ------------------------------------------------------------------
    # Console summary
    # ------------------------------------------------------------------
    overall_mean  = df_markup["pct_premium"].mean()
    overall_med   = df_markup["pct_premium"].median()
    overall_std   = df_markup["pct_premium"].std()

    print(f"\n  Listings analysed : {len(df_markup)}")
    print(f"  Premium vs. consensus — "
          f"mean {overall_mean:+.1f}%  "
          f"median {overall_med:+.1f}%  "
          f"σ={overall_std:.1f}%")

    src_stats = summarize_by_group(df_markup, "source")
    print("\n  By source:")
    for _, row in src_stats.iterrows():
        src_adj = SOURCE_MARGIN_OVERRIDE.get(str(row["source"]).lower(),
                                             WHOLESALE_MARGIN_MID) * 100
        print(f"    {str(row['source']):<12s}  "
              f"median premium {row['median']:+.1f}%  "
              f"NADA/Manheim benchmark markup {src_adj:.1f}%")

    # Markup prediction for the target vehicle
    print(f"\n  Target vehicle markup estimate "
          f"({TARGET_YEAR} {TARGET_TRIM.title()} @ {TARGET_MILEAGE:,} mi):")
    for src, margin in [("autotrader", 0.115), ("cargurus", 0.115),
                        ("cars.com",   0.115), ("carmax",   0.085)]:
        expected_premium = markup_model.predict(
            TARGET_YEAR, TARGET_MILEAGE, TARGET_TRIM, src
        )
        # Method A: consensus from the trials
        # Method B: industry margin
        method_b_markup = margin * 100
        print(f"    [{src:<12s}]  "
              f"expected premium over consensus = {expected_premium:+.1f}%  "
              f"|  benchmark markup = {method_b_markup:.1f}%")

    markup_model.report()
    print()
    return df_markup, markup_model


# ---------------------------------------------------------------------------
# Figure 7 — Markup distribution (premiums by source / trim / year)
# ---------------------------------------------------------------------------

def plot_markup_analysis(
    df_markup: pd.DataFrame,
    markup_model: MarkupModel,
) -> None:
    fig = plt.figure(figsize=(17, 11))
    fig.suptitle(
        "Dealer Markup Analysis — Listing Premium vs. Market Consensus",
        fontsize=14, fontweight="bold", y=1.00,
    )
    gs = gridspec.GridSpec(2, 3, figure=fig, hspace=0.52, wspace=0.38)

    # ── A. Violin: dollar_premium by source ───────────────────────────────
    ax0 = fig.add_subplot(gs[0, :2])
    sources_ord = [s for s in ["cars.com", "autotrader", "cargurus", "carmax"]
                   if s in df_markup["source"].unique()]
    premium_by_src = [df_markup[df_markup["source"] == s]["pct_premium"].values
                      for s in sources_ord]
    src_colors = ["#4e8cff", "#2ecc71", "#e74c3c", "#f39c12"]

    parts = ax0.violinplot(premium_by_src, positions=range(len(sources_ord)),
                           showmedians=True, showextrema=True, widths=0.65)
    for i, (pc, col) in enumerate(zip(parts["bodies"], src_colors)):
        pc.set_facecolor(col)
        pc.set_alpha(0.55)
    parts["cmedians"].set_color("black")
    parts["cmedians"].set_linewidth(2)

    # Method B benchmark markup lines
    for i, src in enumerate(sources_ord):
        bm = SOURCE_MARGIN_OVERRIDE.get(src.lower(), WHOLESALE_MARGIN_MID) * 100
        ax0.hlines(bm, i - 0.35, i + 0.35,
                   colors="black", linestyles=":", linewidth=1.8)
        ax0.text(i + 0.38, bm, f"↑ {bm:.1f}% NADA\nbenchmark",
                 va="center", fontsize=7.5, color="black")

    ax0.axhline(0, color="gray", linestyle="--", linewidth=1, alpha=0.6)
    ax0.set_xticks(range(len(sources_ord)))
    ax0.set_xticklabels([s.title() for s in sources_ord], fontsize=10)
    ax0.set_ylabel("Premium over consensus (%)")
    ax0.set_title("Price Premium by Source\n"
                  "(positive = priced above model consensus; dotted = NADA/Manheim benchmark)")
    ax0.yaxis.set_major_formatter(mticker.PercentFormatter(decimals=0))
    ax0.grid(True, axis="y", alpha=0.25)

    # ── B. Per-source Method B breakdown table ─────────────────────────────
    ax1 = fig.add_subplot(gs[0, 2])
    ax1.axis("off")
    src_table_data = []
    for src in sources_ord:
        sub = df_markup[df_markup["source"] == src]
        bm  = SOURCE_MARGIN_OVERRIDE.get(src.lower(), WHOLESALE_MARGIN_MID) * 100
        src_table_data.append([
            src.title(),
            f"{sub['pct_premium'].median():+.1f}%",
            f"{bm:.1f}%",
            f"${sub['est_markup_dollars'].median():,.0f}",
            f"${sub['negotiation_target'].median():,.0f}",
        ])
    tbl = ax1.table(
        cellText=src_table_data,
        colLabels=["Source", "Median\nPremium", "Benchmark\nMarkup",
                   "Est. Dealer\nProfit", "Suggested\nOffer"],
        loc="center", cellLoc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8.5)
    tbl.scale(1.0, 2.2)
    for j in range(5):
        tbl[0, j].set_facecolor("#2c3e50")
        tbl[0, j].set_text_props(color="white", fontweight="bold")
    for row_i, col in enumerate(src_colors[:len(sources_ord)], start=1):
        rgba = mcolors.to_rgba(col, alpha=0.18)
        for j in range(5):
            tbl[row_i, j].set_facecolor(rgba)
    ax1.set_title("Source Markup Summary\n(NADA/Manheim benchmarks)", pad=14, fontsize=9)

    # ── C. Median premium by trim (bar with error band) ───────────────────
    ax2 = fig.add_subplot(gs[1, :2])
    trim_present = [t for t in TRIM_ORDER if t in df_markup["trim"].unique()]
    medians = [df_markup[df_markup["trim"] == t]["pct_premium"].median()
               for t in trim_present]
    stds    = [df_markup[df_markup["trim"] == t]["pct_premium"].std()
               for t in trim_present]
    bar_cols = ["#c0392b" if m > 0 else "#27ae60" for m in medians]
    ax2.bar(range(len(trim_present)), medians, yerr=stds, capsize=4,
            color=bar_cols, edgecolor="k", linewidth=0.5, alpha=0.75,
            error_kw={"elinewidth": 1.2, "alpha": 0.6})
    ax2.axhline(0, color="gray", linestyle="--", linewidth=1.2, alpha=0.7)
    ax2.set_xticks(range(len(trim_present)))
    ax2.set_xticklabels([t.title() for t in trim_present], rotation=18, ha="right")
    ax2.yaxis.set_major_formatter(mticker.PercentFormatter(decimals=0))
    ax2.set_ylabel("Median premium over consensus (%)")
    ax2.set_title("Median Listing Premium by Trim Level  (error bar = ±1 σ)\n"
                  "Red = priced above consensus  |  Green = below")
    ax2.grid(True, axis="y", alpha=0.25)

    # ── D. Premium scatter by year ─────────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 2])
    years = sorted(df_markup["year"].unique())
    try:
        cmap = plt.colormaps["plasma"].resampled(len(years))
    except AttributeError:
        cmap = plt.cm.get_cmap("plasma", len(years))
    for i, yr in enumerate(years):
        sub = df_markup[df_markup["year"] == yr]
        jitter = np.random.default_rng(yr).uniform(-0.2, 0.2, len(sub))
        ax3.scatter(np.full(len(sub), yr) + jitter,
                    sub["pct_premium"],
                    color=cmap(i), alpha=0.6, edgecolors="k",
                    linewidths=0.3, s=35)
        ax3.plot([yr - 0.3, yr + 0.3],
                 [sub["pct_premium"].median(), sub["pct_premium"].median()],
                 color=cmap(i), linewidth=3, zorder=5)
    ax3.axhline(0, color="gray", linestyle="--", linewidth=1.2, alpha=0.7)
    ax3.yaxis.set_major_formatter(mticker.PercentFormatter(decimals=0))
    ax3.set_xlabel("Model Year")
    ax3.set_ylabel("Premium (%)")
    ax3.set_title("Premium by Model Year\n(thick bar = median)")
    ax3.grid(True, alpha=0.25)

    _savefig(fig, "07_markup_analysis.png")


# ---------------------------------------------------------------------------
# Figure 8 — Cost basis scatter + target vehicle negotiation summary
# ---------------------------------------------------------------------------

def plot_cost_basis(
    df_markup: pd.DataFrame,
    markup_model: MarkupModel,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(16, 7))
    fig.suptitle(
        "Estimated Cost Basis & Negotiation Targets\n"
        "(industry benchmark: NADA / Cox Automotive / Manheim Q4-2023)",
        fontsize=12, fontweight="bold",
    )

    # ── Left: Asking price vs estimated wholesale (Method B mid) ──────────
    ax = axes[0]
    sources_ord = [s for s in ["cars.com", "autotrader", "cargurus", "carmax"]
                   if s in df_markup["source"].unique()]
    src_colors  = ["#4e8cff", "#2ecc71", "#e74c3c", "#f39c12"]

    all_prices = df_markup[TARGET_COL].values
    price_min  = all_prices.min() * 0.95
    price_max  = all_prices.max() * 1.05

    # Zero-profit reference line
    ax.plot([price_min, price_max], [price_min, price_max],
            "k--", linewidth=1, alpha=0.4, label="Zero markup (reference)")

    for i, src in enumerate(sources_ord):
        sub = df_markup[df_markup["source"] == src]
        ax.scatter(sub[TARGET_COL], sub["est_wholesale_mid"],
                   color=src_colors[i], alpha=0.65,
                   edgecolors="k", linewidths=0.3, s=50,
                   label=src.title(), zorder=4)

    # Low/high wholesale bands (shaded)
    xs = np.array([price_min, price_max])
    mid_margin = 1 - WHOLESALE_MARGIN_MID
    lo_margin  = 1 - WHOLESALE_MARGIN_HIGH
    hi_margin  = 1 - WHOLESALE_MARGIN_LOW
    ax.fill_between(xs, xs * lo_margin, xs * hi_margin,
                    color="gray", alpha=0.10, label="8–15% markup band")
    ax.plot(xs, xs * mid_margin, "gray", linewidth=1.2,
            linestyle="-.", alpha=0.6, label=f"{WHOLESALE_MARGIN_MID*100:.1f}% midpoint")

    # Negotiation target markers for each listing
    ax.scatter(df_markup[TARGET_COL], df_markup["negotiation_target"],
               marker="v", color="gold", edgecolors="k", linewidths=0.5,
               s=40, alpha=0.55, zorder=3, label="Suggested offer")

    # Target vehicle annotation
    target_ask = float(df_markup[
        (df_markup["year"] == TARGET_YEAR) &
        (df_markup["trim"] == TARGET_TRIM)
    ][TARGET_COL].median()) if len(df_markup[
        (df_markup["year"] == TARGET_YEAR) &
        (df_markup["trim"] == TARGET_TRIM)
    ]) else None

    ax.set_xlim(price_min, price_max)
    ax.set_ylim(price_min * 0.90, price_max)
    ax.xaxis.set_major_formatter(FMT_DOLLAR_K)
    ax.yaxis.set_major_formatter(FMT_DOLLAR_K)
    ax.set_xlabel("Asking Price ($)")
    ax.set_ylabel("Estimated Wholesale Cost Basis ($)")
    ax.set_title("Asking Price vs. Estimated Wholesale Cost\n"
                 "(▼ = suggested offer = consensus − ½ × excess premium)")
    ax.legend(fontsize=8, framealpha=0.9, loc="upper left")
    ax.grid(True, alpha=0.25)

    # ── Right: Target vehicle negotiation table ────────────────────────────
    ax2 = axes[1]
    ax2.axis("off")

    gt_sub = df_markup[
        (df_markup["year"] == TARGET_YEAR) & (df_markup["trim"] == TARGET_TRIM)
    ]

    if len(gt_sub) == 0:
        ax2.text(0.5, 0.5,
                 f"No {TARGET_YEAR} {TARGET_TRIM.title()} listings in dataset",
                 ha="center", va="center", fontsize=11)
        _savefig(fig, "08_cost_basis.png")
        return

    src_analysis = []
    for src in sources_ord:
        sub = gt_sub[gt_sub["source"] == src]
        if len(sub) == 0:
            continue
        ask_med   = sub[TARGET_COL].median()
        cons_med  = sub["consensus_price"].median()
        prem_med  = sub["pct_premium"].median()
        whole_med = sub["est_wholesale_mid"].median()
        deal_med  = sub["est_markup_dollars"].median()
        neg_med   = sub["negotiation_target"].median()
        markup_pm = markup_model.predict(TARGET_YEAR, TARGET_MILEAGE, TARGET_TRIM, src)
        src_analysis.append([
            src.title(),
            f"${ask_med:,.0f}",
            f"${cons_med:,.0f}",
            f"{prem_med:+.1f}%",
            f"${whole_med:,.0f}",
            f"${deal_med:,.0f}",
            f"${neg_med:,.0f}",
            f"{markup_pm:+.1f}%",
        ])

    if not src_analysis:
        # Fall back to all sources combined
        ask_med   = gt_sub[TARGET_COL].median()
        cons_med  = gt_sub["consensus_price"].median()
        prem_med  = gt_sub["pct_premium"].median()
        whole_med = gt_sub["est_wholesale_mid"].median()
        deal_med  = gt_sub["est_markup_dollars"].median()
        neg_med   = gt_sub["negotiation_target"].median()
        markup_pm = markup_model.predict(TARGET_YEAR, TARGET_MILEAGE, TARGET_TRIM, "cars.com")
        src_analysis.append([
            "All sources",
            f"${ask_med:,.0f}",
            f"${cons_med:,.0f}",
            f"{prem_med:+.1f}%",
            f"${whole_med:,.0f}",
            f"${deal_med:,.0f}",
            f"${neg_med:,.0f}",
            f"{markup_pm:+.1f}%",
        ])

    col_labels = [
        "Source", "Median Ask", "Consensus\nValue", "Premium",
        "Est. Wholesale", "Est. Dealer\nProfit", "Suggested\nOffer",
        "Model\nMarkup%",
    ]
    tbl = ax2.table(
        cellText=src_analysis,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8)
    tbl.scale(1.05, 2.4)
    for j in range(len(col_labels)):
        tbl[0, j].set_facecolor("#2c3e50")
        tbl[0, j].set_text_props(color="white", fontweight="bold")
    src_colors_map = dict(zip(
        [s.title() for s in sources_ord],
        mcolors.to_rgba_array(["#4e8cff", "#2ecc71", "#e74c3c", "#f39c12"])
    ))
    for row_i, row_data in enumerate(src_analysis, start=1):
        src_key = row_data[0]
        rgba = src_colors_map.get(src_key, mcolors.to_rgba("#aaaaaa"))
        for j in range(len(col_labels)):
            tbl[row_i, j].set_facecolor((*rgba[:3], 0.18))

    ax2.set_title(
        f"Target: {TARGET_YEAR} Mazda CX-9 {TARGET_TRIM.title()}\n"
        f"@ {TARGET_MILEAGE:,} miles — Markup Breakdown by Source",
        pad=18, fontsize=9,
    )

    fig.tight_layout()
    _savefig(fig, "08_cost_basis.png")


# ---------------------------------------------------------------------------
# Step 5 — Console summary report
# ---------------------------------------------------------------------------

def print_final_report(
    df: pd.DataFrame,
    results: pd.DataFrame,
    df_markup: pd.DataFrame | None = None,
    markup_model: MarkupModel | None = None,
) -> None:
    clean = clean_df(df)
    print("\n" + "=" * 64)
    print("FINAL SUMMARY REPORT")
    print("=" * 64)
    print(f"\nTraining corpus   : {len(clean)} clean listings")
    print(f"Year range        : {clean['year'].min()} – {clean['year'].max()}")
    print(f"Mileage range     : {clean['mileage'].min():,} – {clean['mileage'].max():,} mi")
    print(f"Price range       : ${clean['price'].min():,.0f} – ${clean['price'].max():,.0f}")
    print(f"Trims covered     : {', '.join(clean['trim'].unique())}")
    print(f"\nTarget vehicle    : {TARGET_YEAR} Mazda CX-9 {TARGET_TRIM.title()} "
          f"@ {TARGET_MILEAGE:,} miles")
    print(f"Confidence bounds : {CI_LO}th – {CI_HI}th percentile across "
          f"{N_ITERATIONS} training runs per model\n")

    col_w = [22, 16, 12, 12, 14, 14, 8]
    headers = ["Model", "Mean R² (±σ)", "Median MAE", "Mean RMSE",
               f"Price {CI_LO}%–CI", f"Price {CI_HI}%–CI", "Width"]
    sep = "  ".join("─" * w for w in col_w)
    header_line = "  ".join(h.ljust(w) for h, w in zip(headers, col_w))
    print(header_line)
    print(sep)

    for m in MODEL_TYPES:
        sub   = results[results["model"] == m]
        preds = sub["target_price"].values
        lo    = np.percentile(preds, CI_LO)
        hi    = np.percentile(preds, CI_HI)
        cols = [
            MODEL_LABELS[m],
            f"{sub['r2'].mean():.3f} ± {sub['r2'].std():.3f}",
            f"${sub['mae'].median():,.0f}",
            f"${sub['rmse'].mean():,.0f}",
            f"${lo:,.0f}",
            f"${hi:,.0f}",
            f"${hi - lo:,.0f}",
        ]
        print("  ".join(c.ljust(w) for c, w in zip(cols, col_w)))

    # ------------------------------------------------------------------
    # Markup / negotiation section
    # ------------------------------------------------------------------
    if df_markup is not None and markup_model is not None:
        print()
        print("─" * 64)
        print("MARKUP ANALYSIS (target vehicle)")
        print("─" * 64)

        gt_sub = df_markup[
            (df_markup["year"] == TARGET_YEAR) &
            (df_markup["trim"] == TARGET_TRIM)
        ]

        if len(gt_sub) > 0:
            ask_med   = gt_sub[TARGET_COL].median()
            cons_med  = gt_sub["consensus_price"].median()
            prem_med  = gt_sub["pct_premium"].median()
            whole_med = gt_sub["est_wholesale_mid"].median()
            deal_med  = gt_sub["est_markup_dollars"].median()
            neg_med   = gt_sub["negotiation_target"].median()

            print(f"\n  {TARGET_YEAR} Mazda CX-9 {TARGET_TRIM.title()} listings: {len(gt_sub)}")
            print(f"  Median asking price  : ${ask_med:,.0f}")
            print(f"  Model consensus value: ${cons_med:,.0f}")
            print(f"  Premium over mkt avg : {prem_med:+.1f}%")
            print(f"  Est. wholesale basis : ${whole_med:,.0f}  "
                  f"(using {WHOLESALE_MARGIN_MID*100:.1f}% NADA/Manheim midpoint)")
            print(f"  Est. dealer profit   : ${deal_med:,.0f}")
            print(f"  Suggested offer      : ${neg_med:,.0f}  "
                  f"(consensus − ½ × above-market premium)")

            # Model prediction for target spec
            for src in ["autotrader", "cargurus", "cars.com", "carmax"]:
                predicted_prem = markup_model.predict(
                    TARGET_YEAR, TARGET_MILEAGE, TARGET_TRIM, src
                )
                print(f"  Expected markup vs consensus [{src:<12s}]: "
                      f"{predicted_prem:+.1f}%")
        else:
            print(f"  No {TARGET_YEAR} {TARGET_TRIM.title()} listings found in cleaned data.")

    print(f"\nFiles written:")
    print(f"  {OUT_CSV}")
    print(f"  {RESULTS_CSV}")
    for p in sorted(REPORT_DIR.glob("*.png")):
        print(f"  {p}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    df_raw = refresh_csv()
    check_diversity(df_raw)
    results, fitted_pipes = run_trials(df_raw)

    print("=" * 64)
    print("STEP 5 — Generating report figures")
    print("=" * 64)
    plot_data_overview(df_raw)
    plot_model_comparison(results)
    plot_target_prediction(results)
    plot_price_vs_mileage_with_ci(df_raw, fitted_pipes)
    plot_residuals(df_raw)
    plot_metric_evolution(results)

    df_markup, markup_model = run_markup_analysis(df_raw)

    print("=" * 64)
    print("STEP 7 — Generating markup figures")
    print("=" * 64)
    plot_markup_analysis(df_markup, markup_model)
    plot_cost_basis(df_markup, markup_model)

    print_final_report(df_raw, results, df_markup, markup_model)


if __name__ == "__main__":
    main()
