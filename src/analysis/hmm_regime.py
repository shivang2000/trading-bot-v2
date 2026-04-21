"""HMM-based Market Regime Detector.

Uses a Gaussian Hidden Markov Model to classify market conditions into
3 states: Trending, Ranging, Volatile. Based on the approach from
xaubot-ai (63.9% WR, 2.64 PF with HMM regime filtering).

Features used:
- Log returns (direction + magnitude)
- Realized volatility (rolling std of returns)
- Return magnitude (absolute returns)

The HMM learns state transitions probabilistically, so it can detect
when a regime is ABOUT to change — unlike simple EMA/ADX thresholds.
"""

from __future__ import annotations

import logging
import warnings
from enum import Enum

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from hmmlearn.hmm import GaussianHMM
    HMM_AVAILABLE = True
except ImportError:
    HMM_AVAILABLE = False
    logger.warning("hmmlearn not installed — HMM regime detector unavailable")


class HMMRegime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


class HMMRegimeDetector:
    """Gaussian HMM regime detector with 3 states.

    Fits on recent price data and classifies the current bar's regime.
    Re-fits periodically (every `refit_interval` bars) to adapt.

    Usage:
        detector = HMMRegimeDetector()
        regime = detector.detect(ohlc_df)
        # regime is HMMRegime.TRENDING / RANGING / VOLATILE
    """

    def __init__(
        self,
        n_states: int = 3,
        lookback: int = 200,
        vol_window: int = 20,
        refit_interval: int = 50,
    ) -> None:
        self._n_states = n_states
        self._lookback = lookback
        self._vol_window = vol_window
        self._refit_interval = refit_interval
        self._model: GaussianHMM | None = None
        self._bars_since_fit: int = 0
        self._state_map: dict[int, HMMRegime] = {}

    def detect(self, ohlc: pd.DataFrame) -> HMMRegime:
        """Detect current market regime from OHLC data."""
        if not HMM_AVAILABLE:
            return HMMRegime.UNKNOWN

        if ohlc is None or len(ohlc) < self._lookback:
            return HMMRegime.UNKNOWN

        # Use last N bars
        window = ohlc.tail(self._lookback).copy()

        # Build features
        features = self._build_features(window)
        if features is None or len(features) < 50:
            return HMMRegime.UNKNOWN

        # Refit model periodically
        self._bars_since_fit += 1
        if self._model is None or self._bars_since_fit >= self._refit_interval:
            self._fit(features)
            self._bars_since_fit = 0

        if self._model is None:
            return HMMRegime.UNKNOWN

        # Predict current state
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                states = self._model.predict(features)
            current_state = int(states[-1])
            return self._state_map.get(current_state, HMMRegime.UNKNOWN)
        except Exception:
            logger.debug("HMM predict failed", exc_info=True)
            return HMMRegime.UNKNOWN

    def _build_features(self, ohlc: pd.DataFrame) -> np.ndarray | None:
        """Build feature matrix: [returns, volatility, magnitude]."""
        close = ohlc["close"].values.astype(float)

        if len(close) < self._vol_window + 5:
            return None

        # Log returns
        returns = np.diff(np.log(close))

        # Realized volatility (rolling std)
        vol = pd.Series(returns).rolling(self._vol_window).std().values

        # Absolute return magnitude
        magnitude = np.abs(returns)

        # Align arrays (vol has NaN from rolling)
        valid_start = self._vol_window
        returns = returns[valid_start:]
        vol = vol[valid_start:]
        magnitude = magnitude[valid_start:]

        # Remove any NaN
        mask = ~(np.isnan(returns) | np.isnan(vol) | np.isnan(magnitude))
        returns = returns[mask]
        vol = vol[mask]
        magnitude = magnitude[mask]

        if len(returns) < 50:
            return None

        features = np.column_stack([returns, vol, magnitude])
        return features

    def _fit(self, features: np.ndarray) -> None:
        """Fit the HMM on features and map states to regime labels."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = GaussianHMM(
                    n_components=self._n_states,
                    covariance_type="full",
                    n_iter=100,
                    random_state=42,
                )
                model.fit(features)

            self._model = model

            # Map states to regimes based on mean volatility per state
            # State with lowest volatility = Ranging
            # State with highest volatility = Volatile
            # Middle = Trending
            states = model.predict(features)
            state_vols = {}
            for s in range(self._n_states):
                mask = states == s
                if mask.sum() > 0:
                    state_vols[s] = features[mask, 1].mean()  # col 1 = volatility
                else:
                    state_vols[s] = 0.0

            sorted_states = sorted(state_vols.keys(), key=lambda s: state_vols[s])

            self._state_map = {
                sorted_states[0]: HMMRegime.RANGING,
                sorted_states[1]: HMMRegime.TRENDING,
                sorted_states[2]: HMMRegime.VOLATILE,
            }

            logger.debug(
                "HMM fitted: state vols=%s, map=%s",
                {s: f"{v:.6f}" for s, v in state_vols.items()},
                self._state_map,
            )

        except Exception:
            logger.debug("HMM fit failed", exc_info=True)
            self._model = None
