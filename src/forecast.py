"""Forecast tomorrow's 24 hourly Spanish day-ahead prices before the 12:00 CET auction.

Run in the morning (Madrid time). Writes forecasts/<target-date>.csv and .json.
Usage: python src/forecast.py [--target YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd
import torch

sys.path.insert(0, os.path.dirname(__file__))
from fetch import TZ, build_panel, today_madrid  # noqa: E402

H = 24
WEEKS = 8
CTX = 24 * 7 * WEEKS
# Only covariates that are really published before the auction:
# demand forecast (Energy-Charts) + weather forecast proxies for wind, solar and temperature (Open-Meteo).
KNOWN = ["fc_load", "wx_wind100", "wx_radiation", "wx_temp"]
PAST = ["gas", "gen_gas", "gen_hydro", "gen_nuclear", "gen_imports"]
MODEL = os.environ.get("TIMESTRAL_MODEL", "mistralai/Timestral-209M")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ridge_forecast(A: dict, T: int) -> np.ndarray:
    """Per-hour ridge regression trained on the 365 days before day index T."""
    p = A["price"]

    def feats(t, h):
        s = t * 24 + h
        prev = p[t * 24 - 24:t * 24]
        return [p[s - 24], p[s - 48], p[s - 168], np.nanmean(prev), np.nanmax(prev), np.nanmin(prev),
                A["fc_load"][s], A["wx_wind100"][s], A["wx_radiation"][s], A["wx_temp"][s], A["gas"][s - 48],
                float(A["dow"][s] == 5), float(A["dow"][s] == 6)]

    out = np.full(H, np.nan)
    for h in range(H):
        X, Y = [], []
        for t in range(max(8, T - 365), T):
            x = feats(t, h); yv = p[t * 24 + h]
            if np.isnan(x).any() or np.isnan(yv):
                continue
            X.append(x); Y.append(yv)
        x0 = np.array(feats(T, h), float)
        if len(X) < 45 or np.isnan(x0).any():
            continue
        X = np.array(X, float); Y = np.array(Y)
        mu, sd = X.mean(0), X.std(0) + 1e-9
        Xs = np.c_[np.ones(len(X)), (X - mu) / sd]
        w = np.linalg.solve(Xs.T @ Xs + np.eye(Xs.shape[1]), Xs.T @ Y)
        out[h] = np.r_[1.0, (x0 - mu) / sd] @ w
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="target delivery day (default: tomorrow, Madrid)")
    args = ap.parse_args()
    target = dt.date.fromisoformat(args.target) if args.target else today_madrid() + dt.timedelta(days=1)
    first = target - dt.timedelta(days=int(os.environ.get("HISTORY_DAYS", "400")))
    run_utc = dt.datetime.now(dt.timezone.utc)
    print(f"target {target}, panel {first}..{target}", flush=True)

    P = build_panel(first, target)
    dates = P.index.get_level_values(0).unique().tolist()
    T = dates.index(str(target))
    A = {c: P[c].to_numpy(float, copy=True) for c in P.columns}
    A["dow"] = np.repeat(pd.to_datetime(dates).dayofweek.values, 24).astype(float)

    s0 = T * 24
    already_published = bool(np.isfinite(A["price"][s0:s0 + H]).sum() > 12)
    A["price"][s0:] = np.nan  # never use target-day prices, even if already published
    ctx = A["price"][s0 - CTX:s0]
    known = np.stack([A[c][s0 - CTX:s0 + H] for c in KNOWN])
    past = np.stack([A[c][s0 - CTX:s0].copy() for c in PAST])
    past[:, -24:] = np.nan  # realized data for today is incomplete at bidding time

    from timestral import TimestralForecaster
    torch.set_num_threads(os.cpu_count() or 2)
    m = TimestralForecaster.from_pretrained(MODEL, device="cpu")
    levels = list(m.quantile_levels)
    i10, i50, i90 = levels.index(0.1), levels.index(0.5), levels.index(0.9)

    rows = []

    def add(name, p50, p10=None, p90=None):
        for h in range(H):
            rows.append({"target": str(target), "model": name, "hour": h,
                         "p10": None if p10 is None else round(float(p10[h]), 2),
                         "p50": None if np.isnan(p50[h]) else round(float(p50[h]), 2),
                         "p90": None if p90 is None else round(float(p90[h]), 2)})

    for name, kw in [("timestral", {}),
                     ("timestral_previsiones", {"known_covariates": known[None]}),
                     ("timestral_todo", {"known_covariates": known[None], "past_covariates": past[None]})]:
        with torch.no_grad():
            f = m.forecast(ctx[None], prediction_length=H, **kw)
        q = np.asarray(f.quantiles).reshape(H, -1)
        add(name, q[:, i50], q[:, i10], q[:, i90])
        print(name, "ok", flush=True)

    add("repetir_hoy", A["price"][s0 - 24:s0])
    add("misma_dia_semana_pasada", A["price"][s0 - 168:s0 - 144])
    add("ridge", ridge_forecast(A, T))

    os.makedirs(os.path.join(ROOT, "forecasts"), exist_ok=True)
    pd.DataFrame(rows).to_csv(os.path.join(ROOT, "forecasts", f"{target}.csv"), index=False)
    meta = {"target": str(target), "run_utc": run_utc.isoformat(timespec="seconds"),
            "run_madrid": run_utc.astimezone(TZ).isoformat(timespec="seconds"),
            "target_prices_already_published_at_run": already_published,
            "context_hours": CTX, "model": MODEL,
            "known_covariates_available_hours": {c: int(np.isfinite(A[c][s0:s0 + H]).sum()) for c in KNOWN},
            "context_missing_hours": int(np.isnan(ctx).sum())}
    json.dump(meta, open(os.path.join(ROOT, "forecasts", f"{target}.json"), "w"), indent=1)
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
