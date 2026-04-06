"""
models/pricing.py

Builds a price prediction model for the 2022 Mazda CX-9 using
mileage (and any other numeric features) as predictors.

Exposes a single class, PricingModel, that wraps a scikit-learn
pipeline so feature engineering and scaling are always applied
consistently.

Typical workflow
----------------
    import pandas as pd
    from models.pricing import PricingModel

    df = pd.read_csv("data/processed/listings.csv")
    model = PricingModel()
    model.fit(df)
    model.report()

    # Predict fair market value for a specific mileage
    print(model.predict(mileage=45000))
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OrdinalEncoder, PolynomialFeatures, StandardScaler


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Numeric feature columns (column order must match X array construction)
NUMERIC_COLS = ["year", "mileage"]
TRIM_COL = "trim"
FEATURE_COLS = NUMERIC_COLS + [TRIM_COL]
TARGET_COL = "price"

# CX-9 trims in ascending value order — used for ordinal encoding so the
# model sees trim as a meaningful numeric gradient rather than a random label.
TRIM_ORDER = [
    "sport",
    "touring",
    "touring plus",
    "carbon edition",
    "grand touring",
    "signature",
]

# Default price floor/ceiling (USD) – filters obvious data errors
MIN_PRICE = 5_000
MAX_PRICE = 60_000
MIN_YEAR = 2018
MAX_MILEAGE = 200_000


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _build_pipeline(model_type: str) -> Pipeline:
    """Return a sklearn Pipeline for the chosen estimator.

    Feature layout expected by the ColumnTransformer:
      columns 0..len(NUMERIC_COLS)-1  → numeric (year, mileage)
      column  len(NUMERIC_COLS)       → trim (ordinal string)
    """
    numeric_idx = list(range(len(NUMERIC_COLS)))      # [0, 1]
    trim_idx    = [len(NUMERIC_COLS)]                 # [2]

    preprocessor = ColumnTransformer([
        ("num", StandardScaler(), numeric_idx),
        ("trim", OrdinalEncoder(
            categories=[TRIM_ORDER],
            handle_unknown="use_encoded_value",
            unknown_value=np.nan,  # unseen trims become NaN; imputed downstream
        ), trim_idx),
    ])

    if model_type == "ridge":
        return Pipeline([
            ("pre", preprocessor),
            ("poly", PolynomialFeatures(degree=2, include_bias=False)),
            ("regressor", Ridge(alpha=1.0)),
        ])
    if model_type == "rf":
        return Pipeline([
            ("pre", preprocessor),
            ("regressor", RandomForestRegressor(
                n_estimators=200,
                max_depth=6,
                random_state=42,
            )),
        ])
    if model_type == "gbm":
        return Pipeline([
            ("pre", preprocessor),
            ("regressor", GradientBoostingRegressor(
                n_estimators=300,
                learning_rate=0.05,
                max_depth=4,
                subsample=0.8,
                random_state=42,
            )),
        ])
    raise ValueError(f"Unknown model_type '{model_type}'. Choose ridge, rf, or gbm.")


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class PricingModel:
    """
    Wrapper around a scikit-learn pipeline to predict CX-9 prices.

    Features used: year (int), mileage (int), trim (str).

    Parameters
    ----------
    model_type : {'ridge', 'rf', 'gbm'}
        Estimator to use. Defaults to 'gbm' (usually most accurate).
    """

    def __init__(self, model_type: str = "gbm"):
        self.model_type = model_type
        self.pipeline: Optional[Pipeline] = None
        self._cv_scores: Optional[np.ndarray] = None

    # ------------------------------------------------------------------
    # Data prep
    # ------------------------------------------------------------------

    def _clean(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df = df.dropna(subset=[TARGET_COL, "mileage", "year", "trim"])
        df["year"] = df["year"].astype(int)
        df["mileage"] = df["mileage"].astype(int)
        df["trim"] = df["trim"].str.lower().str.strip()
        df = df[
            (df[TARGET_COL] >= MIN_PRICE) & (df[TARGET_COL] <= MAX_PRICE)
            & (df["mileage"] >= 0) & (df["mileage"] <= MAX_MILEAGE)
            & (df["year"] >= MIN_YEAR)
            & df["trim"].isin(TRIM_ORDER)
        ]
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Fit
    # ------------------------------------------------------------------

    def fit(self, df: pd.DataFrame) -> "PricingModel":
        """
        Clean data, fit the pipeline, and store cross-validation scores.

        Parameters
        ----------
        df : DataFrame with at least 'year', 'mileage', 'trim', and 'price' columns.
        """
        df = self._clean(df)
        if len(df) < 5:
            raise ValueError(
                f"Only {len(df)} usable rows after cleaning — need at least 5 to fit."
            )

        X = df[FEATURE_COLS].values   # shape (n, 3): year, mileage, trim
        y = df[TARGET_COL].values

        self.pipeline = _build_pipeline(self.model_type)

        self._cv_scores = cross_val_score(
            self.pipeline, X, y, cv=min(5, len(df)), scoring="r2"
        )

        self.pipeline.fit(X, y)
        return self

    # ------------------------------------------------------------------
    # Predict
    # ------------------------------------------------------------------

    def predict(self, year: int, mileage: float, trim: str) -> float:
        """
        Predict price for a single vehicle.

        Parameters
        ----------
        year : int
            Model year (e.g. 2022).
        mileage : float
            Odometer reading in miles.
        trim : str
            Trim level, e.g. "touring", "grand touring", "signature".
        """
        if self.pipeline is None:
            raise RuntimeError("Call fit() before predict().")
        X = np.array([[year, mileage, trim.lower().strip()]])
        return float(self.pipeline.predict(X)[0])

    def predict_batch(self, df: pd.DataFrame) -> np.ndarray:
        """Predict prices for every row in df (must have year, mileage, trim cols)."""
        if self.pipeline is None:
            raise RuntimeError("Call fit() before predict_batch().")
        X = df[FEATURE_COLS].values
        return self.pipeline.predict(X)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def report(self, df: Optional[pd.DataFrame] = None) -> None:
        """
        Print a summary of model performance.

        If df is provided, also reports hold-out test metrics.
        """
        if self.pipeline is None:
            print("Model has not been fitted yet.")
            return

        print(f"Model      : {self.model_type}")
        print(f"Features   : {FEATURE_COLS}")

        if self._cv_scores is not None:
            print(f"CV R²      : {self._cv_scores.mean():.3f} ± {self._cv_scores.std():.3f}")

        if df is not None:
            df = self._clean(df)
            X = df[FEATURE_COLS].values
            y = df[TARGET_COL].values
            X_tr, X_te, y_tr, y_te = train_test_split(
                X, y, test_size=0.2, random_state=42
            )
            pipeline_eval = _build_pipeline(self.model_type)
            pipeline_eval.fit(X_tr, y_tr)
            y_pred = pipeline_eval.predict(X_te)
            print(f"Hold-out R²: {r2_score(y_te, y_pred):.3f}")
            print(f"Hold-out MAE: ${mean_absolute_error(y_te, y_pred):,.0f}")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        """Persist the fitted pipeline to disk via joblib."""
        import joblib
        if self.pipeline is None:
            raise RuntimeError("Nothing to save — model has not been fitted.")
        joblib.dump(self.pipeline, path)
        print(f"Model saved to {path}")

    @classmethod
    def load(cls, path: str, model_type: str = "gbm") -> "PricingModel":
        """Load a previously saved pipeline."""
        import joblib
        instance = cls(model_type=model_type)
        instance.pipeline = joblib.load(path)
        return instance
