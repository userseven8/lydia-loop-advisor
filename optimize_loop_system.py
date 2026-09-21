#!/usr/bin/env python3
"""
Honest Replay & Parameter Optimization Engine for Lydia's Loop Telemetry
Directly tests candidate therapy parameters against actual 14-day Nightscout data.
"""
import sys
import json
import urllib.request
import time
import math
from datetime import datetime, timezone, timedelta
import numpy as np
from scipy.optimize import minimize

BASE_URL = os.environ.get("NIGHTSCOUT_URL", "").strip().rstrip("/")
if not BASE_URL:
    raise SystemExit("NIGHTSCOUT_URL is not set; export it before running this script.")
TZ_OFFSET = timedelta(hours=3)

def fetch_json(url, retries=4, delay=2):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LydiaLoopAnalytics/2.0"}
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise e

# 1. Fetch live telemetry
print("Fetching 14-day telemetry from Nightscout...")
window_start_dt = datetime.now(timezone.utc) - timedelta(days=14)
min_ts = int(window_start_dt.timestamp() * 1000)
min_iso = window_start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

entries = fetch_json(f"{BASE_URL}/api/v1/entries/sgv.json?find[date][$gte]={min_ts}&count=5000")
treatments = fetch_json(f"{BASE_URL}/api/v1/treatments.json?find[created_at][$gte]={min_iso}&count=4000")
profiles = fetch_json(f"{BASE_URL}/api/v1/profile.json")

print(f"Loaded {len(entries)} CGM entries and {len(treatments)} treatments.")

# Parse active profile
active_name = profiles[0].get("defaultProfile", "Default")
store = profiles[0]["store"].get(active_name, profiles[0]["store"][list(profiles[0]["store"].keys())[0]])
active_basals = store.get("basal", [])
active_crs = store.get("carbratio", [])
active_isfs = store.get("sens", [])

# Parse CGM timeline
cgm_points = []
for e in entries:
    sgv = e.get("sgv")
    ts = e.get("date")
    if sgv and ts and 30 <= sgv <= 500:
        cgm_points.append((ts / 1000.0, float(sgv)))
cgm_points.sort(key=lambda x: x[0])

# Parse treatments
boluses = []
carbs = []
temp_basals = []

for t in treatments:
    created = t.get("created_at") or t.get("timestamp")
    if not created: continue
    try:
        ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except: continue
    
    ins = t.get("insulin")
    if ins and float(ins) > 0:
        boluses.append((ts, float(ins)))
        
    c = t.get("carbs")
    if c and float(c) > 0:
        carbs.append((ts, float(c)))
        
    rate = t.get("rate")
    dur = t.get("duration")
    if rate is not None and dur is not None:
        try:
            temp_basals.append((ts, ts + float(dur) * 60.0, float(rate)))
        except: pass

boluses.sort(key=lambda x: x[0])
carbs.sort(key=lambda x: x[0])
temp_basals.sort(key=lambda x: x[0])

def get_bg_at(t_sec, max_tol=600):
    best = None
    for ts, bg in cgm_points:
        d = abs(ts - t_sec)
        if d <= max_tol:
            if best is None or d < best[0]:
                best = (d, bg)
    return best[1] if best else None

def get_pump_delivery(t1, t2):
    """Total insulin delivered by pump (boluses + temp basals) in interval [t1, t2]."""
    u_bolus = sum(ins for ts, ins in boluses if t1 <= ts <= t2)
    u_basal = 0.0
    for st in range(int(t1), int(t2), 300):
        r = 0.10 # fallback
        for s, e, rate in temp_basals:
            if s <= st < e:
                r = rate
                break
        u_basal += r * (300.0 / 3600.0)
    return u_bolus + u_basal

# -------------------------------------------------------------------------
# IDENTIFY UNCONFOUNDED CLINICAL EPISODES FROM REAL DATA
# -------------------------------------------------------------------------

# A. Fasting Overnight Resting Windows (No meals in 4 hours, flat intervals)
resting_windows = []
t_start = cgm_points[0][0]
t_end = cgm_points[-1][0]

cur = t_start
while cur + 7200 <= t_end:
    t1 = cur
    t2 = cur + 7200 # 2-hour window
    if not any(t1 - 10800 <= tc <= t2 for tc, c in carbs):
        bg1 = get_bg_at(t1, max_tol=450)
        bg2 = get_bg_at(t2, max_tol=450)
        if bg1 and bg2 and 70 <= bg1 <= 220 and 70 <= bg2 <= 220:
            tot_u = get_pump_delivery(t1, t2)
            dt_l = datetime.fromtimestamp(t1, tz=timezone.utc) + TZ_OFFSET
            hour_local = dt_l.hour + dt_l.minute / 60.0
            resting_windows.append({
                "time": dt_l.strftime("%b %d %H:%M"),
                "hour": hour_local,
                "bg1": bg1,
                "bg2": bg2,
                "delta_bg": bg2 - bg1,
                "insulin_delivered": tot_u,
                "hourly_rate": tot_u / 2.0
            })
    cur += 3600

print(f"Identified {len(resting_windows)} unconfounded 2-hour resting windows.")

# B. Isolated Correction Boluses (High BG >= 160, no carbs in 3h)
isolated_corrections = []
for bt, b_u in boluses:
    if b_u < 0.15: continue
    if any(abs(bt - tc) < 7200 for tc, c in carbs): continue
    bg_init = get_bg_at(bt, max_tol=450)
    if bg_init is None or bg_init < 160: continue
    
    post_cgm = [bg for ts, bg in cgm_points if 3600 <= ts - bt <= 14400]
    if not post_cgm: continue
    bg_nadir = min(post_cgm)
    drop = bg_init - bg_nadir
    if drop >= 25:
        dt_l = datetime.fromtimestamp(bt, tz=timezone.utc) + TZ_OFFSET
        isolated_corrections.append({
            "time": dt_l.strftime("%b %d %H:%M"),
            "hour": dt_l.hour + dt_l.minute / 60.0,
            "bg_init": bg_init,
            "bg_nadir": bg_nadir,
            "drop": drop,
            "bolus_u": b_u
        })

print(f"Identified {len(isolated_corrections)} isolated correction episodes.")

# C. Meal Excursions (Carbs >= 8g, 3-hour window)
meal_clusters = []
if carbs:
    ct, cc = carbs[0]
    for t, c in carbs[1:]:
        if t - ct < 1800:
            cc += c
        else:
            meal_clusters.append((ct, cc))
            ct, cc = t, c
    meal_clusters.append((ct, cc))

meal_windows = []
for mt, c_grams in meal_clusters:
    if c_grams < 8: continue
    bg0 = get_bg_at(mt, max_tol=600)
    bg3 = get_bg_at(mt + 10800, max_tol=900)
    if bg0 is None or bg3 is None or bg0 < 70: continue
    tot_u = get_pump_delivery(mt, mt + 10800)
    dt_l = datetime.fromtimestamp(mt, tz=timezone.utc) + TZ_OFFSET
    hour_local = dt_l.hour + dt_l.minute / 60.0
    meal_windows.append({
        "time": dt_l.strftime("%b %d %H:%M"),
        "hour": hour_local,
        "carbs": c_grams,
        "bg0": bg0,
        "bg3": bg3,
        "delta_bg": bg3 - bg0,
        "tot_u": tot_u
    })

print(f"Identified {len(meal_windows)} meal windows.")

# -------------------------------------------------------------------------
# GLOBAL OPTIMIZATION FUNCTION
# -------------------------------------------------------------------------
def get_basal_for_hour(h, b_early, b_deep, b_dawn, b_day, b_bed):
    if 0.0 <= h < 1.5: return b_early
    elif 1.5 <= h < 5.0: return b_deep
    elif 5.0 <= h < 8.5: return b_dawn
    elif 8.5 <= h < 22.0: return b_day
    else: return b_bed

def get_cr_for_hour(h, cr_bfast, cr_lunch, cr_snack, cr_din):
    if 8.0 <= h < 11.5: return cr_bfast
    elif 11.5 <= h < 15.0: return cr_lunch
    elif 15.0 <= h < 18.5: return cr_snack
    else: return cr_din

def objective(params):
    isf, b_early, b_deep, b_dawn, b_day, b_bed, cr_bfast, cr_lunch, cr_snack, cr_din = params
    total_loss = 0.0
    
    # 1. Prediction error on isolated corrections:
    # Expected drop = ISF * bolus_u
    for c in isolated_corrections:
        pred_drop = isf * c["bolus_u"]
        err = c["drop"] - pred_drop
        total_loss += (err / 25.0) ** 2
        
    # 2. Prediction error on resting windows:
    # req_u = basal_rate * 2.0 - delta_bg / ISF
    for w in resting_windows:
        b_rate = get_basal_for_hour(w["hour"], b_early, b_deep, b_dawn, b_day, b_bed)
        expected_u = b_rate * 2.0 - (w["delta_bg"] / isf)
        err = w["insulin_delivered"] - expected_u
        total_loss += (err / 0.10) ** 2
        
    # 3. Prediction error on meal windows:
    # expected_u = (carbs / CR) + (basal_rate * 3.0) - (delta_bg / ISF)
    for m in meal_windows:
        b_rate = get_basal_for_hour(m["hour"], b_early, b_deep, b_dawn, b_day, b_bed)
        cr = get_cr_for_hour(m["hour"], cr_bfast, cr_lunch, cr_snack, cr_din)
        expected_u = (m["carbs"] / cr) + (b_rate * 3.0) - (m["delta_bg"] / isf)
        err = m["tot_u"] - expected_u
        total_loss += (err / 0.25) ** 2
        
    return total_loss

p_init = [230.0, 0.05, 0.15, 0.05, 0.15, 0.05, 6.0, 6.5, 9.5, 9.0]
bounds = [
    (160.0, 320.0), # ISF
    (0.02, 0.15),   # b_early
    (0.05, 0.25),   # b_deep
    (0.02, 0.15),   # b_dawn
    (0.05, 0.25),   # b_day
    (0.02, 0.15),   # b_bed
    (4.0, 9.0),     # cr_bfast
    (4.5, 11.0),    # cr_lunch
    (6.0, 15.0),    # cr_snack
    (6.0, 14.0)     # cr_din
]

print("\nRunning L-BFGS-B Optimization against 14 days of real events...")
res = minimize(objective, p_init, bounds=bounds, method="L-BFGS-B")

isf_opt, b_early_opt, b_deep_opt, b_dawn_opt, b_day_opt, b_bed_opt, cr_bfast_opt, cr_lunch_opt, cr_snack_opt, cr_din_opt = res.x

print("\n" + "="*65)
print("     HONEST GLOBAL REPLAY OPTIMIZATION RESULTS (14 DAYS)")
print("="*65)
print(f"Optimization Status : {'SUCCESS (Converged)' if res.success else 'Did not converge'}")
print(f"Objective Loss Score: Initial = {objective(p_init):.1f}  ->  Optimized = {res.fun:.1f}")

print("\n--- 1. INSULIN SENSITIVITY FACTOR (ISF) ---")
print(f"Active Pump Profile   : 230 mg/dL/U")
print(f"Mathematically Optimal: {isf_opt:.1f} mg/dL/U  (snaps to {round(isf_opt/5.0)*5:.0f} mg/dL/U)")

print("\n--- 2. BASAL RATES (U/hr) ---")
print(f"Early Sleep (00:00–01:30): Active=0.05 | Optimal={b_early_opt:.3f} -> Snap={round(b_early_opt/0.05)*0.05:.2f} U/hr")
print(f"Deep Sleep  (01:30–05:00): Active=0.15 | Optimal={b_deep_opt:.3f} -> Snap={round(b_deep_opt/0.05)*0.05:.2f} U/hr")
print(f"Dawn Surge  (05:00–08:30): Active=0.05 | Optimal={b_dawn_opt:.3f} -> Snap={round(b_dawn_opt/0.05)*0.05:.2f} U/hr")
print(f"Daytime     (08:30–22:00): Active=0.15 | Optimal={b_day_opt:.3f} -> Snap={round(b_day_opt/0.05)*0.05:.2f} U/hr")
print(f"Bedtime     (22:00–24:00): Active=0.05 | Optimal={b_bed_opt:.3f} -> Snap={round(b_bed_opt/0.05)*0.05:.2f} U/hr")

print("\n--- 3. CARB RATIOS (g/U) ---")
print(f"Breakfast (08:30–11:30): Active=1:6.0  | Optimal=1:{cr_bfast_opt:.1f} -> Snap=1:{round(cr_bfast_opt*2)/2:.1f} g/U")
print(f"Lunch     (11:30–15:00): Active=1:6.5  | Optimal=1:{cr_lunch_opt:.1f} -> Snap=1:{round(cr_lunch_opt*2)/2:.1f} g/U")
print(f"Afternoon (15:00–18:30): Active=1:9.8  | Optimal=1:{cr_snack_opt:.1f} -> Snap=1:{round(cr_snack_opt*2)/2:.1f} g/U")
print(f"Dinner    (18:30–22:00): Active=1:9.1  | Optimal=1:{cr_din_opt:.1f} -> Snap=1:{round(cr_din_opt*2)/2:.1f} g/U")

print("\n" + "="*65)
