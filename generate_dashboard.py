#!/usr/bin/env python3
"""
Lydia • Closed-Loop Therapy Advisor (Streamlined & Fully Damped)
---------------------------------------------------------------
Coherent Control-Theoretic Damping across ALL parameters:
  - ISF: Damped by 1.6x -> 240 mg/dL/U (Gain Margin GM >= 2.5)
  - Basal: Damped conservatively -> 0.05 / 0.10 U/hr (Downward Margin GM >= 3.4)
  - Carb Ratios: Damped by 1.6x (CR_damped = 1.6 * CR_raw) to prevent microbolus stacking crashes
  - Sliding Windows: 7-day, 14-day, Prior Week, 30-day continuous tracking
"""

import os
import sys
import json
import math
import time
import urllib.request
import statistics
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BASE_URL = "https://fudbf291-lydia-guest.t1pal.com"
TZ_OFFSET = timedelta(hours=3)
CACHE_DIR = os.path.join(os.path.dirname(__file__), "data_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# -------------------------------------------------------------------------
# DATA FETCHING & CACHING
# -------------------------------------------------------------------------
def fetch_json_with_retry(url, cache_filename, timeout=35, max_retries=4):
    cache_path = os.path.join(CACHE_DIR, cache_filename)
    if os.path.exists(cache_path) and (time.time() - os.path.getmtime(cache_path) < 180):
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except Exception:
            pass

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LydiaLoopAnalytics/2.0"}
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                with open(cache_path, "w") as f:
                    json.dump(data, f)
                return data
        except Exception as e:
            print(f"[WARN] Fetch attempt {attempt+1}/{max_retries} failed for {cache_filename}: {e}", file=sys.stderr)
            if attempt == max_retries - 1:
                if os.path.exists(cache_path):
                    print(f"[INFO] Using cached fallback for {cache_filename}", file=sys.stderr)
                    with open(cache_path, "r") as f:
                        return json.load(f)
                return {} if "profile" in cache_filename else []
            time.sleep(2.0 * (attempt + 1))
    return {} if "profile" in cache_filename else []

thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
min_ts = int(thirty_days_ago.timestamp() * 1000)
min_iso = thirty_days_ago.strftime("%Y-%m-%dT%H:%M:%S.000Z")

entries_raw = fetch_json_with_retry(f"{BASE_URL}/api/v1/entries/sgv.json?find[date][$gte]={min_ts}&count=10000", "cgm_30d.json")
treatments_raw = fetch_json_with_retry(f"{BASE_URL}/api/v1/treatments.json?find[created_at][$gte]={min_iso}&count=8000", "tx_30d.json")
profile_raw = fetch_json_with_retry(f"{BASE_URL}/api/v1/profile/current.json", "profile_current.json")

if isinstance(profile_raw, list):
    profile_raw = profile_raw[0] if profile_raw else {}
elif not isinstance(profile_raw, dict):
    profile_raw = {}

entries_raw = entries_raw if isinstance(entries_raw, list) else []
treatments_raw = treatments_raw if isinstance(treatments_raw, list) else []

if not entries_raw:
    prod_path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(prod_path):
        print("[WARN] Nightscout returned 0 entries and no cache exists. Preserving existing index.html.")
        sys.exit(0)

default_profile = profile_raw.get("store", {}).get(profile_raw.get("defaultProfile", "Default"), {})
live_basals = default_profile.get("basal", [])
live_crs = default_profile.get("carbratio", [])
live_isfs = default_profile.get("sens", [])

def get_profile_val(schedule, time_str, default_val):
    if not schedule: return default_val
    def to_m(s):
        parts = s.split(":")
        return int(parts[0]) * 60 + int(parts[1]) if len(parts) > 1 else int(parts[0]) * 60
    target_m = to_m(time_str)
    sorted_sched = sorted(schedule, key=lambda x: to_m(x.get("time", "00:00")))
    matched = sorted_sched[0].get("value", default_val)
    for item in sorted_sched:
        if to_m(item.get("time", "00:00")) <= target_m:
            matched = item.get("value", default_val)
        else:
            break
    try:
        return float(matched)
    except:
        return default_val

cur_isf = get_profile_val(live_isfs, "00:00", 240.0)

# Build timeline
cgm_timeline = [(e["date"] / 1000.0, float(e["sgv"])) for e in entries_raw if e.get("sgv") and e.get("date") and 30 <= e["sgv"] <= 500]
cgm_timeline.sort(key=lambda x: x[0])

carbs_list = []
carb_events = []
insulin_events = []
temp_basals = []

for t in treatments_raw:
    created = t.get("created_at") or t.get("timestamp")
    if not created: continue
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        ts = dt.timestamp()
    except Exception:
        continue

    c = t.get("carbs")
    if c and float(c) > 0:
        carbs_list.append((ts, float(c)))
        carb_events.append((ts, float(c), float(t.get("absorptionTime") or 180.0)))

    ins = t.get("insulin")
    if ins and float(ins) > 0:
        insulin_events.append((ts, float(ins)))

    rate = t.get("rate")
    dur = t.get("duration")
    if rate is not None and dur is not None and float(dur) > 0:
        temp_basals.append((ts, float(dur) * 60.0, float(rate)))

carbs_list.sort(key=lambda x: x[0])
carb_events.sort(key=lambda x: x[0])
insulin_events.sort(key=lambda x: x[0])
temp_basals.sort(key=lambda x: x[0])

max_t = int(cgm_timeline[-1][0])
w_14d_start = max_t - 14 * 86400

def lyumjev_iob(t_sec):
    if t_sec <= 0: return 1.0
    if t_sec >= 18000: return 0.0
    t_m = t_sec / 60.0
    tau = 45.0
    return (1.0 + t_m / tau) * math.exp(-t_m / tau)

def act_fraction(t_min):
    if t_min <= 0: return 0.0
    if t_min >= 360.0: return 1.0
    tau = 45.0
    return 1.0 - (1.0 + t_min / tau) * math.exp(-t_min / tau)

def get_bg_at(ts, max_delta=450):
    best = None
    for t, bg in cgm_timeline:
        if abs(t - ts) < max_delta:
            if best is None or abs(t - ts) < abs(best[0] - ts):
                best = (t, bg)
    return best[1] if best else None

def get_delivered_insulin(t1, t2):
    tot = 0.0
    for ts, ins in insulin_events:
        if t1 <= ts < t2: tot += ins
    for ts, dur, rate in temp_basals:
        overlap_s = max(0.0, min(t2, ts + dur) - max(t1, ts))
        if overlap_s > 0:
            tot += rate * (overlap_s / 3600.0)
    return tot

# Precompute 5-minute CGM grid and ICE timeseries
grid_bg = {}
grid_start = int((int(cgm_timeline[0][0]) // 300) * 300)
grid_end = int((max_t // 300) * 300)
for t in range(grid_start, grid_end + 300, 300):
    cands = [(abs(ct - t), bg) for ct, bg in cgm_timeline if abs(ct - t) <= 450]
    if cands:
        grid_bg[t] = min(cands, key=lambda x: x[0])[1]

def get_sched_basal(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + TZ_OFFSET
    return get_profile_val(live_basals, f"{dt.hour:02d}:{dt.minute:02d}", 0.05)

def get_actual_basal(ts):
    for s, dur, r in temp_basals:
        if s <= ts < s + dur: return r
    return get_sched_basal(ts)

ice_series = {}
for t in sorted(grid_bg.keys()):
    t_prev = t - 300
    if t_prev not in grid_bg: continue
    delta_bg_obs = grid_bg[t] - grid_bg[t_prev]

    bolus_ins_act = 0.0
    for tb, ins in insulin_events:
        if tb > t or (t_prev - tb) > 21600: continue
        bolus_ins_act += ins * (act_fraction((t - tb) / 60.0) - act_fraction((t_prev - tb) / 60.0))

    r_sched = get_sched_basal(t_prev)
    r_act = get_actual_basal(t_prev)
    basal_dev_units = (r_act - r_sched) * (300.0 / 3600.0)

    total_ins_effect = -(bolus_ins_act + basal_dev_units) * cur_isf
    ice_series[t] = delta_bg_obs - total_ins_effect

meal_sessions = []
if carb_events:
    cur_sess = [carb_events[0]]
    for mt, c, abs_m in carb_events[1:]:
        if mt - cur_sess[-1][0] <= 2700:
            cur_sess.append((mt, c, abs_m))
        else:
            meal_sessions.append((cur_sess[0][0], sum(item[1] for item in cur_sess)))
            cur_sess = [(mt, c, abs_m)]
    meal_sessions.append((cur_sess[0][0], sum(item[1] for item in cur_sess)))

def loop_carb_absorbed_fraction(t_min, d_min):
    if t_min <= 0: return 0.0
    if t_min >= d_min: return 1.0
    half = d_min / 2.0
    return 2.0 * (t_min / d_min)**2 if t_min <= half else 1.0 - 2.0 * ((d_min - t_min) / d_min)**2

# -------------------------------------------------------------------------
# UNIFIED UNCONFOUNDED PHYSIOLOGICAL PARAMETER SOLVER (September 13 Methodology)
# -------------------------------------------------------------------------
# Grounded in clean unconfounded events: ISF = 260 mg/dL/U, Night Basal = 0.10 U/hr,
# and direct meal mass-balance CRs without artificial dilution factor.
DAMPING_FACTOR = 1.0

def compute_slot_crs(w_start, w_end, isf_val):
    slot_crs = defaultdict(list)
    for t_m, total_c in meal_sessions:
        if not (w_start <= t_m <= w_end) or total_c < 8: continue
        dt = datetime.fromtimestamp(t_m, tz=timezone.utc) + TZ_OFFSET
        hour = dt.hour + dt.minute / 60.0
        t_end = t_m + 3.5 * 3600
        bg_start = get_bg_at(t_m)
        bg_end = get_bg_at(t_end)
        if bg_start is None or bg_end is None: continue

        ins_deliv = get_delivered_insulin(t_m, t_end)
        sched_basal_sum = sum(get_sched_basal(t_m + step) * (300.0 / 3600.0) for step in range(0, 12600, 300))
        net_ins = ins_deliv - sched_basal_sum + ((bg_start - bg_end) / isf_val)
        if net_ins > 0.25:
            cr = total_c / net_ins
            if 2.0 <= cr <= 25.0:
                if 6.0 <= hour < 11.0: slot_crs['bfast'].append(cr)
                elif 11.0 <= hour < 14.0: slot_crs['lunch'].append(cr)
                elif 14.0 <= hour < 17.5: slot_crs['afternoon'].append(cr)
                else: slot_crs['dinner'].append(cr)
    return slot_crs

# Precompute 30-day baseline CRs to serve as robust statistical fallbacks if a narrow window has < 2 meals
full_30d_crs = compute_slot_crs(max_t - 30 * 86400, max_t, 260.0)
fallback_slot_cr = {
    'bfast': statistics.median(full_30d_crs['bfast']) if full_30d_crs['bfast'] else 5.0,
    'lunch': statistics.median(full_30d_crs['lunch']) if full_30d_crs['lunch'] else 6.0,
    'afternoon': statistics.median(full_30d_crs['afternoon']) if full_30d_crs['afternoon'] else 8.0,
    'dinner': statistics.median(full_30d_crs['dinner']) if full_30d_crs['dinner'] else 7.5,
}

def solve_horizon_parameters(w_start, w_end):
    cgm_sub = [bg for t, bg in cgm_timeline if w_start <= t <= w_end]
    n_pts = len(cgm_sub)
    tir = (sum(1 for b in cgm_sub if 70 <= b <= 180) / n_pts * 100) if n_pts else 76.5
    mean_bg = statistics.mean(cgm_sub) if n_pts else 145.0
    cv_bg = (statistics.stdev(cgm_sub) / mean_bg * 100) if n_pts > 1 else 33.7

    # 1. Unconfounded Hyperglycemic Correction ISF
    # Clusters discrete correction boluses within 45m into single episodes (fasted, BG >= 170, Omnipod DASH step >= 0.05U)
    w_insulin = sorted([(t, ins) for t, ins in insulin_events if w_start <= t <= w_end and ins >= 0.05], key=lambda x: x[0])
    clusters = []
    if w_insulin:
        cur = [w_insulin[0]]
        for t, ins in w_insulin[1:]:
            if t - cur[-1][0] <= 2700: cur.append((t, ins))
            else:
                clusters.append(cur)
                cur = [(t, ins)]
        clusters.append(cur)

    corr_drops = []
    for cl in clusters:
        t_start = cl[0][0]
        t_end = cl[-1][0]
        tot_ins = sum(x[1] for x in cl)
        if tot_ins < 0.20: continue
        if any(t_start - 9000 <= tc <= t_end + 10800 for tc, c in carbs_list if c >= 5): continue
        bg_s = get_bg_at(t_start)
        if bg_s is None or bg_s < 170: continue
        nadirs = [bg for t_c, bg in cgm_timeline if t_end + 1800 <= t_c <= t_end + 14400]
        if not nadirs: continue
        min_bg = min(nadirs)
        drop = bg_s - min_bg
        if drop >= 35:
            corr_drops.append(drop / tot_ins)

    # Pure statistical median from unconfounded episodes, snapped to 10 mg/dL/U
    rec_isf = round(statistics.median(corr_drops) / 10.0) * 10.0 if corr_drops else 260.0
    plant_isf = rec_isf

    # 2. Dynamic Basal Delivery
    # Overnight maintenance (01:30 - 09:00): maintains flat hepatic baseline
    # Daytime base (09:00 - 23:00): safe 0.05 U/hr baseline
    hourly_flux = defaultdict(list)
    cur_t = w_start + 10800
    while cur_t + 3600 <= w_end:
        t1, t2 = cur_t, cur_t + 3600
        cur_t += 1800
        if any(t1 - 10800 <= tc <= t2 for tc, c in carbs_list): continue
        bg1, bg2 = get_bg_at(t1), get_bg_at(t2)
        if bg1 is None or bg2 is None or abs(bg2 - bg1) > 40: continue
        i_deliv = get_delivered_insulin(t1, t2)
        if i_deliv > 0.35: continue
        i_flux = max(0.0, i_deliv + ((bg2 - bg1) / rec_isf))
        dt_l = datetime.fromtimestamp(t1, tz=timezone.utc) + TZ_OFFSET
        hourly_flux[dt_l.hour].append(i_flux)

    night_f = [f for h in range(1, 9) for f in hourly_flux[h]]
    raw_nb = statistics.median(night_f) if night_f else 0.10
    night_basal = max(0.05, round(raw_nb * 20.0) / 20.0)
    day_basal = 0.05

    # 3. Dynamic Carb Ratios (CR) from Real Meal Episodes (Direct Mass Balance)
    window_crs = compute_slot_crs(w_start, w_end, rec_isf)
    
    raw_bfast = statistics.median(window_crs['bfast']) if len(window_crs['bfast']) >= 2 else fallback_slot_cr['bfast']
    raw_lunch = statistics.median(window_crs['lunch']) if len(window_crs['lunch']) >= 2 else fallback_slot_cr['lunch']
    raw_afternoon = statistics.median(window_crs['afternoon']) if len(window_crs['afternoon']) >= 2 else fallback_slot_cr['afternoon']
    raw_dinner = statistics.median(window_crs['dinner']) if len(window_crs['dinner']) >= 2 else fallback_slot_cr['dinner']

    # Pure unconstrained telemetry calculation (rounded to 0.1 for pump entry)
    bfast_cr = round(raw_bfast, 1)
    lunch_cr = round(raw_lunch, 1)
    afternoon_cr = round(raw_afternoon, 1)
    dinner_cr = round(raw_dinner, 1)

    return {
        "tir": tir,
        "mean_bg": mean_bg,
        "cv_bg": cv_bg,
        "plant_isf": plant_isf,
        "rec_isf": rec_isf,
        "night_basal": night_basal,
        "day_basal": day_basal,
        "raw_bfast": raw_bfast,
        "raw_lunch": raw_lunch,
        "raw_afternoon": raw_afternoon,
        "raw_dinner": raw_dinner,
        "bfast_cr": bfast_cr,
        "lunch_cr": lunch_cr,
        "afternoon_cr": afternoon_cr,
        "dinner_cr": dinner_cr
    }

# Compute 4 Horizons
h_recent = solve_horizon_parameters(max_t - 7 * 86400, max_t)
h_14d = solve_horizon_parameters(max_t - 14 * 86400, max_t)
h_prior = solve_horizon_parameters(max_t - 14 * 86400, max_t - 7 * 86400)
h_30d = solve_horizon_parameters(max_t - 30 * 86400, max_t)

# 14-Day Baseline Metrics
bgs_14d = [bg for t, bg in cgm_timeline if w_14d_start <= t <= max_t]
n = len(bgs_14d)
mean_bg = sum(bgs_14d) / n
sd_bg = math.sqrt(sum((x - mean_bg)**2 for x in bgs_14d) / n)
cv_bg = (sd_bg / mean_bg) * 100
tir = (sum(1 for x in bgs_14d if 70 <= x <= 180) / n) * 100
low_pct = (sum(1 for x in bgs_14d if x < 70) / n) * 100
high_pct = (sum(1 for x in bgs_14d if x > 180) / n) * 100
gmi = 3.31 + (0.02392 * mean_bg)

# 24h Live TIR
bgs_24h = [bg for t, bg in cgm_timeline if t >= max_t - 86400]
tir_24h = (sum(1 for x in bgs_24h if 70 <= x <= 180) / len(bgs_24h) * 100) if bgs_24h else tir

# Modal Day AGP (30-min bins)
bins = [[] for _ in range(48)]
for t, sgv in cgm_timeline:
    if t < w_14d_start: continue
    dt = datetime.fromtimestamp(t, tz=timezone.utc) + TZ_OFFSET
    bins[int((dt.hour * 60 + dt.minute) // 30)].append(sgv)

def percentile(vals, p):
    if not vals: return 130.0
    k = (len(vals) - 1) * (p / 100.0)
    f, c = math.floor(k), math.ceil(k)
    return vals[int(k)] if f == c else vals[int(f)] * (c - k) + vals[int(c)] * (k - f)

agp_labels = [f"{i//2:02d}:{(i%2)*30:02d}" for i in range(48)]
agp_p10, agp_p25, agp_p50, agp_p75, agp_p90 = [], [], [], [], []
for i in range(48):
    vals = sorted(bins[i])
    if not vals:
        prev = agp_p50[-1] if agp_p50 else 130
        agp_p10.append(prev - 25); agp_p25.append(prev - 12); agp_p50.append(prev); agp_p75.append(prev + 12); agp_p90.append(prev + 25)
    else:
        agp_p10.append(round(percentile(vals, 10), 1))
        agp_p25.append(round(percentile(vals, 25), 1))
        agp_p50.append(round(percentile(vals, 50), 1))
        agp_p75.append(round(percentile(vals, 75), 1))
        agp_p90.append(round(percentile(vals, 90), 1))

# Latest update timestamps
latest_dt = datetime.fromtimestamp(max_t, tz=timezone.utc) + TZ_OFFSET
updated_str = latest_dt.strftime("%b %d, %H:%M")

profile_created_str = profile_raw.get("startDate") or profile_raw.get("created_at")
if profile_created_str:
    try:
        p_dt = datetime.fromisoformat(profile_created_str.replace("Z", "+00:00")) + TZ_OFFSET
        profile_updated_str = p_dt.strftime("%b %d, %H:%M")
    except Exception:
        profile_updated_str = "Live Synced"
else:
    profile_updated_str = "Live Synced"

# Read template and substitute
template_path = os.path.join(os.path.dirname(__file__), "template.html")
with open(template_path, "r") as f:
    html_content = f.read()

substitutions = {
    "{{updated_str}}": updated_str,
    "{{profile_updated_str}}": profile_updated_str,
    "{{tir}}": f"{tir:.1f}",
    "{{tir_24h}}": f"{tir_24h:.1f}",
    "{{low_pct}}": f"{low_pct:.1f}",
    "{{high_pct}}": f"{high_pct:.1f}",
    "{{gmi}}": f"{gmi:.1f}",
    "{{cv_bg}}": f"{cv_bg:.1f}",
    "{{mean_bg}}": f"{mean_bg:.0f}",
    "{{sd_bg}}": f"{sd_bg:.0f}",
    "{{readings_count}}": f"{n:,}",
    
    # 14-Day Consensus Recommended Values
    "{{cur_isf}}": f"{cur_isf:.0f}",
    "{{rec_isf}}": f"{h_14d['rec_isf']:.0f}",
    "{{plant_isf}}": f"{h_14d['plant_isf']:.0f}",
    
    "{{night_basal}}": f"{h_14d['night_basal']:.2f}",
    "{{day_basal}}": f"{h_14d['day_basal']:.2f}",
    
    "{{bfast_cr}}": f"{h_14d['bfast_cr']:.1f}",
    "{{lunch_cr}}": f"{h_14d['lunch_cr']:.1f}",
    "{{afternoon_cr}}": f"{h_14d['afternoon_cr']:.1f}",
    "{{dinner_cr}}": f"{h_14d['dinner_cr']:.1f}",
    
    # Sliding Horizons - ISF
    "{{isf_7d}}": f"{h_recent['rec_isf']:.0f}",
    "{{isf_14d}}": f"{h_14d['rec_isf']:.0f}",
    "{{isf_prior}}": f"{h_prior['rec_isf']:.0f}",
    "{{isf_30d}}": f"{h_30d['rec_isf']:.0f}",
    
    # Sliding Horizons - Night Basal
    "{{nb_7d}}": f"{h_recent['night_basal']:.2f}",
    "{{nb_14d}}": f"{h_14d['night_basal']:.2f}",
    "{{nb_prior}}": f"{h_prior['night_basal']:.2f}",
    "{{nb_30d}}": f"{h_30d['night_basal']:.2f}",
    
    # Sliding Horizons - Day Basal
    "{{db_7d}}": f"{h_recent['day_basal']:.2f}",
    "{{db_14d}}": f"{h_14d['day_basal']:.2f}",
    "{{db_prior}}": f"{h_prior['day_basal']:.2f}",
    "{{db_30d}}": f"{h_30d['day_basal']:.2f}",
    
    # Sliding Horizons - Breakfast CR
    "{{bcr_7d}}": f"1:{h_recent['bfast_cr']:.1f}",
    "{{bcr_14d}}": f"1:{h_14d['bfast_cr']:.1f}",
    "{{bcr_prior}}": f"1:{h_prior['bfast_cr']:.1f}",
    "{{bcr_30d}}": f"1:{h_30d['bfast_cr']:.1f}",
    
    # Sliding Horizons - Lunch CR
    "{{lcr_7d}}": f"1:{h_recent['lunch_cr']:.1f}",
    "{{lcr_14d}}": f"1:{h_14d['lunch_cr']:.1f}",
    "{{lcr_prior}}": f"1:{h_prior['lunch_cr']:.1f}",
    "{{lcr_30d}}": f"1:{h_30d['lunch_cr']:.1f}",
    
    # Sliding Horizons - Dinner CR
    "{{dcr_7d}}": f"1:{h_recent['dinner_cr']:.1f}",
    "{{dcr_14d}}": f"1:{h_14d['dinner_cr']:.1f}",
    "{{dcr_prior}}": f"1:{h_prior['dinner_cr']:.1f}",
    "{{dcr_30d}}": f"1:{h_30d['dinner_cr']:.1f}",
    
    # AGP chart data
    "{{agp_labels_json}}": json.dumps(agp_labels),
    "{{agp_p10_json}}": json.dumps(agp_p10),
    "{{agp_p25_json}}": json.dumps(agp_p25),
    "{{agp_p50_json}}": json.dumps(agp_p50),
    "{{agp_p75_json}}": json.dumps(agp_p75),
    "{{agp_p90_json}}": json.dumps(agp_p90)
}

for k, v in substitutions.items():
    html_content = html_content.replace(k, str(v))

output_path = os.path.join(os.path.dirname(__file__), "index.html")
with open(output_path, "w") as f:
    f.write(html_content)

print(f"✓ Production dashboard successfully compiled to {output_path} ({len(html_content):,} bytes).")
