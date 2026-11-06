"""Score every stored forecast whose real prices are now published, then rebuild the scoreboard.

Usage: python src/score.py
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from fetch import omie_day  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")
SCORES = os.path.join(RES, "daily_scores.csv")
LABELS = {
    "timestral": "Timestral (solo precios)",
    "timestral_previsiones": "Timestral + previsión eólica/solar/demanda",
    "timestral_todo": "Timestral + previsiones + gas y generación",
    "ridge": "Regresión ridge (referencia entrenada)",
    "repetir_hoy": "Repetir precios de hoy",
    "misma_dia_semana_pasada": "Mismo día semana pasada",
}


def battery(pred: np.ndarray, real: np.ndarray, k: int = 4, eff: float = 0.88) -> float:
    """€/MWh earned by 1 MWh of storage: charge the k cheapest hours, discharge the k dearest after them."""
    best, choice = -1e18, None
    for s in range(k, 24 - k + 1):
        c = np.argsort(pred[:s])[:k]
        d = s + np.argsort(-pred[s:])[:k]
        v = pred[d].mean() * eff - pred[c].mean()
        if v > best:
            best, choice = v, (c, d)
    c, d = choice
    return float(real[d].mean() * eff - real[c].mean()) if best > 0 else 0.0


def actual_prices(day: str) -> np.ndarray | None:
    path = os.path.join(RES, "actuals", f"{day}.csv")
    if os.path.exists(path):
        return pd.read_csv(path).price.to_numpy(float)
    recs = omie_day(dt.date.fromisoformat(day))
    if not recs:
        return None
    df = pd.DataFrame(recs, columns=["date", "hour", "price"]).groupby("hour").price.mean()
    df = df.reindex(range(24))
    if df.isna().sum() > 2:
        return None
    df = df.interpolate(limit_direction="both")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.rename("price").reset_index().to_csv(path, index=False)
    return df.to_numpy(float)


def score_all() -> pd.DataFrame:
    old = pd.read_csv(SCORES) if os.path.exists(SCORES) else pd.DataFrame(columns=["target"])
    done = set(old.target.astype(str))
    new = []
    for f in sorted(glob.glob(os.path.join(ROOT, "forecasts", "*.csv"))):
        day = os.path.basename(f)[:-4]
        if day in done:
            continue
        real = actual_prices(day)
        if real is None:
            print(day, "real prices not published yet")
            continue
        meta = json.load(open(f[:-4] + ".json")) if os.path.exists(f[:-4] + ".json") else {}
        valid = not meta.get("target_prices_already_published_at_run", False)
        F = pd.read_csv(f)
        perfect = battery(real, real)
        for name, g in F.groupby("model"):
            g = g.sort_values("hour")
            p = g.p50.to_numpy(float)
            if np.isnan(p).any():
                continue
            row = {"target": day, "model": name, "valid": valid,
                   "mae": round(float(np.mean(np.abs(p - real))), 3),
                   "rmse": round(float(np.sqrt(np.mean((p - real) ** 2))), 3),
                   "bias": round(float(np.mean(p - real)), 3),
                   "real_mean": round(float(real.mean()), 3),
                   "battery_eur": round(battery(p, real), 3), "battery_perfect_eur": round(perfect, 3),
                   "coverage80": None}
            if g.p10.notna().all():
                row["coverage80"] = round(float(np.mean((real >= g.p10.to_numpy()) & (real <= g.p90.to_numpy()))), 3)
            new.append(row)
        print(day, "scored")
    allrows = pd.concat([old, pd.DataFrame(new)], ignore_index=True) if new else old
    os.makedirs(RES, exist_ok=True)
    allrows.to_csv(SCORES, index=False)
    return allrows


def scoreboard(S: pd.DataFrame) -> None:
    if "valid" not in S.columns:
        S = S.assign(valid=[])
    S = S[S.valid.astype(str) == "True"].copy()
    if S.empty:
        md = "_Todavía no hay días puntuados._\n"
    else:
        S["target"] = S.target.astype(str)
        days = sorted(S.target.unique())
        g = S.groupby("model")
        tab = pd.DataFrame({
            "Error medio (€/MWh)": g.mae.mean(),
            "RMSE": g.rmse.mean(),
            "Sesgo": g.bias.mean(),
            "Batería: % de lo ideal": 100 * g.battery_eur.sum() / g.battery_perfect_eur.sum(),
            "Cobertura 80%": g.coverage80.mean(),
        }).sort_values("Error medio (€/MWh)")
        best = S.loc[S.groupby("target").mae.idxmin()].model.value_counts()
        tab["Días que gana"] = best.reindex(tab.index).fillna(0).astype(int)
        tab.index = [LABELS.get(i, i) for i in tab.index]
        md = (f"**Días puntuados:** {len(days)} (del {days[0]} al {days[-1]}). "
              f"Precio medio real en el periodo: {S.real_mean.mean():.1f} €/MWh.\n\n"
              + tab.round(2).to_markdown() + "\n")
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            pv = S.pivot(index="target", columns="model", values="mae").sort_index()
            fig, ax = plt.subplots(figsize=(9, 4.5))
            for c in pv.columns:
                ax.plot(pv.index, pv[c].expanding().mean(), label=LABELS.get(c, c), lw=2 if "timestral" in c else 1)
            ax.set_ylabel("Error medio acumulado (€/MWh)"); ax.set_title("Previsión de la subasta de la luz (OMIE España)")
            ax.tick_params(axis="x", rotation=45); ax.legend(fontsize=7); ax.grid(alpha=.3)
            fig.tight_layout(); fig.savefig(os.path.join(RES, "scoreboard.png"), dpi=130)
            md += "\n![Error acumulado](results/scoreboard.png)\n"
        except Exception as e:  # chart is optional
            print("chart skipped:", e)
    readme = os.path.join(ROOT, "README.md")
    txt = open(readme, encoding="utf-8").read()
    a, b = "<!-- SCOREBOARD:START -->", "<!-- SCOREBOARD:END -->"
    txt = txt.split(a)[0] + a + "\n" + md + b + txt.split(b)[1]
    open(readme, "w", encoding="utf-8").write(txt)


if __name__ == "__main__":
    scoreboard(score_all())
