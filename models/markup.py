"""
models/markup.py

Estimates dealer markup embedded in used-car listing prices.

Approach
--------
Since we have only retail asking prices (no paired auction/wholesale data),
markup is estimated using two complementary methods:

Method A — Within-dataset premium
    The fitted Ridge pricing model gives a "market consensus value" for each
    vehicle given its year/mileage/trim.  The difference between the actual
    asking price and that consensus is the *relative premium*: positive means
    the listing is priced above the dataset average for equivalent vehicles.

Method B — Industry-benchmark wholesale reconstruction
    Published industry sources (NADA Annual Data, Cox Automotive / Manheim
    Market Reports) consistently report:
      • Average used-vehicle front-end gross profit: $2,200–$2,800 / unit
      • Wholesale-to-retail spread: ~10–14 % of asking price
        (post-2023 normalisation from pandemic-era peaks of 20–30 %)
      • CarMax fixed-margin model: ~$2,200 gross per unit (public earnings)
      • Traditional franchise dealer: variable, 8–15 %
    These benchmarks are used to back out an estimated wholesale cost basis
    and a reasonable negotiation target.

MarkupModel
    Trains a separate GBM to predict *expected markup %* as a function of
    year, mileage, trim, and source.  This lets us ask: "for this specific
    vehicle type, what markup should I expect?"

References
----------
• NADA Annual Data (2023): average used-vehicle gross profit ~$2,337/unit
• Cox Automotive / Manheim Market Report Q4 2023: wholesale-to-retail spread
  returned to ~11–13 % after post-pandemic highs
• CarMax FY2024 10-K: used-vehicle per-unit GPU ~$2,220
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OrdinalEncoder, StandardScaler
from sklearn.compose import ColumnTransformer

from models.pricing import (
    FEATURE_COLS,
    MIN_PRICE,
    MAX_PRICE,
    MIN_YEAR,
    MAX_MILEAGE,
    TARGET_COL,
    TRIM_ORDER,
    _build_pipeline,
)

# ---------------------------------------------------------------------------
# Industry benchmark constants
# (sourced from NADA 2023, Manheim Q4-2023, CarMax FY2024 10-K)
# ---------------------------------------------------------------------------

# Wholesale-to-retail margin band (fraction of retail asking price)
WHOLESALE_MARGIN_LOW  = 0.08   #  8 % — CarMax / high-volume / low-demand trims
WHOLESALE_MARGIN_MID  = 0.115  # 11.5 % — industry midpoint post-2023
WHOLESALE_MARGIN_HIGH = 0.15   # 15 % — independent dealers / desirable trims

# Source-specific adjustments based on published per-unit GPU disclosures
SOURCE_MARGIN_OVERRIDE: dict[str, float] = {
    "carmax":     0.085,  # CarMax fixed no-haggle model ~$2,200 GPU
    "autotrader": 0.115,  # franchise dealers listing on AutoTrader
    "cargurus":   0.115,
    "cars.com":   0.115,
}

# Age-based depreciation adjustment: older vehicles typically carry higher
# absolute markups due to greater reconditioning / uncertainty costs
YEAR_MARGIN_DELTA: dict[int, float] = {
    # additional markup fraction added for each year of age vs. 2023 baseline
    # based on Manheim reporting of increased gross per unit on older stock
    0: 0.000,   # 2023 (newest)
    1: 0.005,   # 2022
    2: 0.010,   # 2021
    3: 0.015,   # 2020
    4: 0.020,   # 2019
    5: 0.025,   # 2018
}

CURRENT_YEAR = 2024  # reference year for age calculation (data collected ~2024)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_premiums(df: pd.DataFrame, consensus_pipeline: Pipeline) -> pd.DataFrame:
    """
    Add markup/premium columns to a cleaned listings DataFrame.

    Parameters
    ----------
    df : DataFrame
        Must already be cleaned (same filtering as PricingModel._clean).
    consensus_pipeline : fitted sklearn Pipeline
        Used to generate market-consensus prices.

    Returns
    -------
    DataFrame with additional columns:
        consensus_price     : model's predicted fair-market value
        dollar_premium      : asking_price − consensus_price
        pct_premium         : dollar_premium / consensus_price × 100
        est_wholesale_low   : estimated dealer cost (low margin scenario)
        est_wholesale_mid   : estimated dealer cost (mid margin scenario)
        est_wholesale_high  : estimated dealer cost (high margin scenario)
        est_markup_pct      : source- and age-adjusted markup estimate (%)
        est_markup_dollars  : corresponding dollar markup
        negotiation_target  : recommended offer (consensus − half of excess premium)
    """
    df = df.copy()

    X = df[FEATURE_COLS].values
    df["consensus_price"]  = consensus_pipeline.predict(X)
    df["dollar_premium"]   = df[TARGET_COL] - df["consensus_price"]
    df["pct_premium"]      = (df["dollar_premium"] / df["consensus_price"]) * 100

    # Method B: source + age adjusted margin
    def _adj_margin(row: pd.Series) -> float:
        base  = SOURCE_MARGIN_OVERRIDE.get(str(row.get("source", "")).lower(),
                                           WHOLESALE_MARGIN_MID)
        age   = max(0, min(5, CURRENT_YEAR - int(row["year"])))
        delta = YEAR_MARGIN_DELTA.get(age, YEAR_MARGIN_DELTA[5])
        return base + delta

    df["est_markup_pct"]      = df.apply(_adj_margin, axis=1) * 100  # as %
    df["est_markup_dollars"]  = df[TARGET_COL] * df.apply(_adj_margin, axis=1)
    df["est_wholesale_low"]   = df[TARGET_COL] * (1 - WHOLESALE_MARGIN_HIGH)
    df["est_wholesale_mid"]   = df[TARGET_COL] * (1 - WHOLESALE_MARGIN_MID)
    df["est_wholesale_high"]  = df[TARGET_COL] * (1 - WHOLESALE_MARGIN_LOW)

    # Negotiation target: consensus price minus half the above-market premium
    # (retain some of the premium where justified; push back on the rest)
    excess = df["dollar_premium"].clip(lower=0)
    df["negotiation_target"] = df["consensus_price"] - excess * 0.5

    return df


class MarkupModel:
    """
    Predicts expected markup % for a vehicle based on its features + source.

    Separate from PricingModel — the target here is the *premium over consensus*
    rather than the absolute price.
    """

    # Numeric features: year, mileage
    # Categorical features: trim (ordinal), source (nominal → OrdinalEncoder)
    SOURCE_ORDER = ["carmax", "autotrader", "cargurus", "cars.com"]

    def __init__(self) -> None:
        self.pipeline: Optional[Pipeline] = None
        self._r2: Optional[float] = None
        self._mae: Optional[float] = None

    def _build(self) -> Pipeline:
        numeric_idx   = [0, 1]   # year, mileage
        trim_idx      = [2]      # trim
        source_idx    = [3]      # source

        pre = ColumnTransformer([
            ("num",    StandardScaler(),  numeric_idx),
            ("trim",   OrdinalEncoder(categories=[TRIM_ORDER],
                                      handle_unknown="use_encoded_value",
                                      unknown_value=np.nan), trim_idx),
            ("source", OrdinalEncoder(categories=[self.SOURCE_ORDER],
                                      handle_unknown="use_encoded_value",
                                      unknown_value=np.nan), source_idx),
        ])
        return Pipeline([
            ("pre", pre),
            ("reg", GradientBoostingRegressor(
                n_estimators=200, learning_rate=0.05,
                max_depth=3, subsample=0.8, random_state=42,
            )),
        ])

    def fit(self, df: pd.DataFrame) -> "MarkupModel":
        """
        Parameters
        ----------
        df : DataFrame
            Must contain year, mileage, trim, source, pct_premium columns
            (i.e. output of compute_premiums).
        """
        sub = df.dropna(subset=["year", "mileage", "trim", "source", "pct_premium"])
        X = sub[["year", "mileage", "trim", "source"]].values
        y = sub["pct_premium"].values

        X_tr, X_val, y_tr, y_val = train_test_split(X, y, test_size=0.25,
                                                      random_state=42)
        self.pipeline = self._build()
        self.pipeline.fit(X_tr, y_tr)
        y_pred = self.pipeline.predict(X_val)
        self._r2  = float(r2_score(y_val, y_pred))
        self._mae = float(mean_absolute_error(y_val, y_pred))
        return self

    def predict(self, year: int, mileage: float, trim: str,
                source: str = "cars.com") -> float:
        """Return expected premium % for a single vehicle."""
        if self.pipeline is None:
            raise RuntimeError("Call fit() first.")
        X = np.array([[year, mileage, trim.lower().strip(), source.lower()]])
        return float(self.pipeline.predict(X)[0])

    def report(self) -> None:
        print(f"  Markup model  R²={self._r2:.3f}  MAE={self._mae:.2f}%")


def summarize_by_group(df: pd.DataFrame,
                       groupby: str,
                       value_col: str = "pct_premium") -> pd.DataFrame:
    """Aggregate premium stats by a grouping column."""
    return (
        df.groupby(groupby)[value_col]
        .agg(count="count",
             mean="mean",
             median="median",
             std="std",
             p10=lambda x: np.percentile(x, 10),
             p90=lambda x: np.percentile(x, 90))
        .round(2)
        .reset_index()
    )
