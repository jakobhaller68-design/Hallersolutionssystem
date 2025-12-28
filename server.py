from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import pandas as pd
import math

app = FastAPI(title="Hallersolutions Benchmark API")

# ✅ CORS: gör så att din hemsida får anropa API:t
# För produktion: byt "*" till din domän, t.ex. "https://hallersolutions.se"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Ladda benchmark-data
# -----------------------------
def load_benchmark() -> pd.DataFrame:
    # Leta fil i samma mapp som server.py
    if Path("benchmark_master_clean.parquet").exists():
        df = pd.read_parquet("benchmark_master_clean.parquet")
    elif Path("benchmark_master_clean.csv").exists():
        df = pd.read_csv("benchmark_master_clean.csv")
    elif Path("benchmark_master.csv").exists():
        df = pd.read_csv("benchmark_master.csv")
    else:
        raise FileNotFoundError(
            "Hittar ingen benchmark-fil (benchmark_master_clean.parquet / benchmark_master_clean.csv / benchmark_master.csv) "
            "i samma mapp som server.py"
        )

    # Map SCB ContentsCode -> interna namn (om du använder rå-export)
    rename_map = {
        "0000028K": "antal_foretag",
        "0000032G": "rorelsemarginal_pct",
        "0000032H": "nettomarginal_pct",
        "0000033Z": "personalkostnad_netto_pct",
        "00000355": "personalkostnad_per_anst_tkr",
    }
    df = df.rename(columns=rename_map)

    # Harmoniserings-fallbacks
    if "size_class" not in df.columns and "Storleksklass" in df.columns:
        df["size_class"] = df["Storleksklass"]
    if "year" not in df.columns and "Tid" in df.columns:
        df["year"] = df["Tid"]

    # Validera
    required = [
        "year", "sni_3", "size_class",
        "antal_foretag", "rorelsemarginal_pct", "nettomarginal_pct",
        "personalkostnad_netto_pct", "personalkostnad_per_anst_tkr"
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Saknar kolumner: {missing}. Har: {list(df.columns)}")

    # Normalisera typer
    df["year"] = df["year"].astype(str)
    df["sni_3"] = df["sni_3"].astype(str)
    df["size_class"] = df["size_class"].astype(str).replace({"001": "0"})

    for c in ["antal_foretag", "rorelsemarginal_pct", "nettomarginal_pct", "personalkostnad_netto_pct", "personalkostnad_per_anst_tkr"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    return df

DF = load_benchmark()

# -----------------------------
# Potentialmodell (samma som du kör i Streamlit)
# -----------------------------
def estimate_potential_frictionless(revenue_sek: float, bench_op_margin_pct: float):
    # basgap = 30% av medianmarginalen, cap 0.5–6.0 pp
    base = max(0.5, min(6.0, bench_op_margin_pct * 0.30))
    gaps = {
        "low": base * 0.75,   # konservativ
        "mid": base,          # realistisk
        "high": base * 1.5,   # aggressiv
    }
    pot = {k: max(0.0, revenue_sek * (v / 100.0)) for k, v in gaps.items()}
    return pot, gaps

def to_num(x):
    if x is None:
        return None
    try:
        x = float(x)
        if math.isnan(x):
            return None
        return x
    except Exception:
        return None

# -----------------------------
# API endpoint: POST /berakna
# -----------------------------
@app.post("/berakna")
def berakna(payload: dict):
    """
    Förväntar input:
    {
      "year": "2024",
      "sni_3": "432",
      "size_class": "5-9",
      "revenue": 5000000
    }
    """
    year = str(payload.get("year", "2024"))
    sni_3 = str(payload.get("sni_3", "")).strip()
    size_class = str(payload.get("size_class", "")).strip()
    revenue = to_num(payload.get("revenue"))

    if not sni_3 or not size_class or revenue is None:
        return {"error": "Missing required fields: sni_3, size_class, revenue (and optional year)"}

    sub = DF[
        (DF["year"] == year) &
        (DF["sni_3"] == sni_3) &
        (DF["size_class"] == size_class)
    ].copy()

    if sub.empty:
        return {"error": f"No benchmark for year={year}, sni_3={sni_3}, size_class={size_class}"}

    # Median (om flera rader finns, ta median)
    bench = sub.median(numeric_only=True)

    antal = to_num(bench.get("antal_foretag"))
    opm = to_num(bench.get("rorelsemarginal_pct"))
    npm = to_num(bench.get("nettomarginal_pct"))
    staff_pct = to_num(bench.get("personalkostnad_netto_pct"))
    staff_per = to_num(bench.get("personalkostnad_per_anst_tkr"))

    if opm is None or npm is None or staff_pct is None:
        return {"error": "Benchmark data incomplete for selected segment."}

    median_ebit_kr = revenue * (opm / 100.0)
    median_personal_kr = revenue * (staff_pct / 100.0)

    pot, gaps = estimate_potential_frictionless(revenue, opm)

    # Returnera exakt de fält HTML:en använder
    return {
        "antal_foretag": int(round(antal)) if antal is not None else None,
        "rorelsemarginal_pct": round(opm, 1),
        "nettomarginal_pct": round(npm, 1),
        "personalkostnad_netto_pct": round(staff_pct, 1),
        "personalkostnad_per_anst_tkr": int(round(staff_per)) if staff_per is not None else None,

        "median_ebit_kr": round(median_ebit_kr),
        "median_personal_kr": round(median_personal_kr),

        "gap_low_pp": round(gaps["low"], 1),
        "gap_high_pp": round(gaps["high"], 1),

        "potential_low_kr": round(pot["low"]),
        "potential_mid_kr": round(pot["mid"]),
        "potential_high_kr": round(pot["high"]),
    }
