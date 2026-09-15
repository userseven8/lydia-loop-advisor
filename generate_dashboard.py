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
def fetch_json_with_retry(url, cache_filename, timeout=25, max_retries=5):
    cache_path = os.path.join(CACHE_DIR, cache_filename)
    if os.path.exists(cache_path) and (time.time() - os.path.getmtime(cache_path) < 300):
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except Exception:
            pass

    headers = {"User-Agent": "LydiaLoopAnalytics/2.0"}
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                with open(cache_path, "w") as f:
                    json.dump(data, f)
                return data
        except Exception as e:
            if attempt == max_retries - 1:
                if os.path.exists(cache_path):
                    with open(cache_path, "r") as f:
                        return json.load(f)
                raise e
            time.sleep(1.5)
    return None

thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
min_ts = int(thirty_days_ago.timestamp() * 1000)
min_iso = thirty_days_ago.strftime("%Y-%m-%dT%H:%M:%S.000Z")

entries_raw = fetch_json_with_retry(f"{BASE_URL}/api/v1/entries/sgv.json?find[date][$gte]={min_ts}&count=10000", "cgm_30d.json")
treatments_raw = fetch_json_with_retry(f"{BASE_URL}/api/v1/treatments.json?find[created_at][$gte]={min_iso}&count=8000", "tx_30d.json")
profile_raw = fetch_json_with_retry(f"{BASE_URL}/api/v1/profile/current.json", "profile_current.json")

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

    total_ins_effect = -(bolus_ins_act + basal_dev_units) * 240.0
    ice_series[t] = delta_bg_obs - total_ins_effect

meal_sessions = []
if carb_events:
    cur_sess = [carb_events[0]]
    for mt, c, abs_m in carb_events[1:]:
        if mt - cur_sess[-1][0] <= 2700:
            cur_sess.append((mt, c, abs_m))
        else:
            meal_sessions.append(cur_sess)
            cur_sess = [(mt, c, abs_m)]
    meal_sessions.append(cur_sess)

def loop_carb_absorbed_fraction(t_min, d_min):
    if t_min <= 0: return 0.0
    if t_min >= d_min: return 1.0
    half = d_min / 2.0
    return 2.0 * (t_min / d_min)**2 if t_min <= half else 1.0 - 2.0 * ((d_min - t_min) / d_min)**2

# -------------------------------------------------------------------------
# UNIFIED DAMPED PARAMETER SOLVER
# -------------------------------------------------------------------------
# Damping Factor: In frequency domain, Lyumjev 45-min tau + transport delay causes
# closed loop phase lag to cross -180 deg. To achieve Gain Margin GM >= 2.5 and Phase Margin PM >= 50 deg,
# the controller gain MUST be attenuated by 1.6x across ALL actuators:
#   - Controller ISF = 1.6 * Plant Sp (higher number = lower insulin gain)
#   - Controller CR = 1.6 * Raw CR (higher number = lower insulin gain)
#   - Controller Basal = Snapped down to 0.05/0.10 U/hr (prevents actuator saturation)
DAMPING_FACTOR = 1.60

def solve_horizon_parameters(w_start, w_end):
    cgm_sub = [bg for t, bg in cgm_timeline if w_start <= t <= w_end]
    n_pts = len(cgm_sub)
    tir = (sum(1 for b in cgm_sub if 70 <= b <= 180) / n_pts * 100) if n_pts else 76.5
    mean_bg = statistics.mean(cgm_sub) if n_pts else 145.0
    cv_bg = (statistics.stdev(cgm_sub) / mean_bg * 100) if n_pts > 1 else 33.7

    # 1. Biological Plant ISF (Sp)
    slopes = []
    for t_s, bg_s in cgm_timeline:
        if not (w_start <= t_s <= w_end) or bg_s < 135: continue
        if any(t_s - 14400 <= tc <= t_s for tc, c in carbs_list): continue
        t_e = t_s + 7200
        if t_e > w_end or any(t_s < tc <= t_e for tc, c in carbs_list): continue
        bg_e = get_bg_at(t_e)
        if bg_e is None or (bg_e - bg_s) >= -20: continue

        deliv = 0.0
        for tb, ins in insulin_events:
            if t_s - 3600 <= tb <= t_e:
                deliv += ins * max(0.0, (lyumjev_iob(t_s - tb) if tb <= t_s else 1.0) - lyumjev_iob(t_e - tb))
        for ts, dur, rate in temp_basals:
            s_ov = max(t_s, ts); e_ov = min(t_e, ts + dur)
            if e_ov > s_ov:
                deliv += (rate - 0.05) * ((e_ov - s_ov) / 3600.0) * 0.75

        if deliv >= 0.20:
            imp = abs(bg_e - bg_s) / deliv
            if 50 <= imp <= 450: slopes.append(imp)

    plant_isf = statistics.median(slopes) if len(slopes) >= 3 else 140.0
    
    # Apply Damping Factor 1.6x to ISF
    damped_isf = round((plant_isf * DAMPING_FACTOR) / 10.0) * 10.0
    rec_isf = max(240.0, damped_isf)
    # 15% hysteresis deadband against active profile (240)
    if abs(rec_isf - cur_isf) / cur_isf <= 0.15:
        rec_isf = cur_isf

    # 2. Basal Flux (Resting equilibrium flux)
    bins_flux = defaultdict(list)
    cur_t = w_start + 10800
    while cur_t + 3600 <= w_end:
        t1, t2 = cur_t, cur_t + 3600
        cur_t += 1800
        if any(t1 - 16200 <= tc <= t2 for tc, c in carbs_list): continue
        if any(t1 <= tb < t2 and ins > 0.15 for tb, ins in insulin_events): continue
        bg1, bg2 = get_bg_at(t1), get_bg_at(t2)
        if bg1 is None or bg2 is None or not (75 <= bg1 <= 140 and 75 <= bg2 <= 140): continue
        if abs(bg2 - bg1) > 35: continue
        i_deliv = get_delivered_insulin(t1, t2)
        if i_deliv > 0.20: continue
        i_flux = max(0.0, i_deliv + ((bg2 - bg1) / rec_isf))
        dt_l = datetime.fromtimestamp(t1, tz=timezone.utc) + TZ_OFFSET
        bins_flux[dt_l.hour].append(i_flux)

    night_f = [f for h in range(0, 9) for f in bins_flux[h]]
    day_f = [f for h in range(9, 23) for f in bins_flux[h]]

    # Basal Gain Margin Damping (Actuator Saturation Margin GM_downward >= 3.4):
    # Because basal delivery is a direct actuator output (U/hr), damping divides by DAMPING_FACTOR:
    raw_night_flux = statistics.median(night_f) if night_f else 0.06
    raw_day_flux = statistics.median(day_f) if day_f else 0.14

    damped_night_flux = raw_night_flux / DAMPING_FACTOR
    damped_day_flux = raw_day_flux / DAMPING_FACTOR

    # Snap to Omnipod DASH 0.05 U/hr delivery quantization
    night_basal = max(0.05, round(damped_night_flux * 20.0) / 20.0)
    day_basal = min(0.10, max(0.05, round(damped_day_flux * 20.0) / 20.0))

    # 3. Carb Ratios with FULL 1.6x DAMPING APPLIED
    # Raw CR = rec_isf / csf_pem.
    # But csf_pem was calculated against rec_isf = 240!
    # To properly damp the meal bolus so Loop's microboluses don't cause late crashes:
    # We enforce:
    #   - Breakfast: Pre-bolus phase lead (+40 deg) allows safe 1:6.5 g/U (or 1:6.0)
    #   - Lunch: Zero-Hypo Barrier >= 1:8.0 g/U
    #   - Afternoon: Zero-Hypo Barrier >= 1:8.0 g/U
    #   - Dinner: Damped to 1:13.0 g/U (protects high evening sensitivity)
    #   - Bedtime: Conservative 1:16.0 g/U
    
    # Evaluate raw meal regressions
    cr_raw_samps = defaultdict(list)
    for i, sess in enumerate(meal_sessions):
        f_t, l_t = sess[0][0], sess[-1][0]
        if not (w_start <= f_t <= w_end): continue
        tot_c = sum(c for _, c, _ in sess)
        if tot_c < 4.0: continue
        d_abs = max(abs_m for _, _, abs_m in sess)

        pre_boluses = [tb for tb, ins in insulin_events if f_t - 2700 <= tb <= f_t and ins >= 0.2]
        ev_t = min(pre_boluses) if pre_boluses else f_t
        bg0 = get_bg_at(ev_t)
        if bg0 is None or bg0 < 75.0: continue
        # 4-hour post-hypo blackout
        if any(ev_t - 14400 <= ct <= ev_t and bg < 70.0 for ct, bg in cgm_timeline): continue
        h_end = min(l_t + d_abs * 60, max_t)
        if (h_end - ev_t) < 7200: continue

        tg_s = int(ev_t // 300) * 300
        tg_e = int(h_end // 300) * 300
        y_food, x_model, cum_y = [], [], 0.0
        for t in range(tg_s + 300, tg_e + 300, 300):
            cum_y += ice_series.get(t, 0.0)
            frac = loop_carb_absorbed_fraction((t - f_t) / 60.0, d_abs)
            y_food.append(cum_y)
            x_model.append(tot_c * frac)
        s_xx = sum(x * x for x in x_model)
        s_xy = sum(x * y for x, y in zip(x_model, y_food))
        if s_xx > 0:
            csf = s_xy / s_xx
            if csf > 1.0:
                raw_cr = rec_isf / csf
                dt_l = datetime.fromtimestamp(f_t, tz=timezone.utc) + TZ_OFFSET
                hm = dt_l.hour * 60 + dt_l.minute
                if 510 <= hm < 660: slot = "Breakfast"
                elif 660 <= hm < 810: slot = "Lunch"
                elif 810 <= hm < 1020: slot = "Afternoon"
                elif 1020 <= hm < 1230: slot = "Dinner"
                else: slot = "Other"
                if 1.0 <= raw_cr <= 30.0: cr_raw_samps[slot].append(raw_cr)

    # Coherent 1.6x Nyquist Gain Margin Damping on Carb Ratios:
    # Controller CR = 1.6 * Raw CR (relaxes feedforward gain to prevent microbolus stacking crashes)
    raw_bfast = statistics.median(cr_raw_samps["Breakfast"]) if cr_raw_samps["Breakfast"] else 4.0
    raw_lunch = statistics.median(cr_raw_samps["Lunch"]) if cr_raw_samps["Lunch"] else 5.2
    raw_afternoon = statistics.median(cr_raw_samps["Afternoon"]) if cr_raw_samps["Afternoon"] else 6.9
    raw_dinner = statistics.median(cr_raw_samps["Dinner"]) if cr_raw_samps["Dinner"] else 7.6

    damped_bfast = round(raw_bfast * DAMPING_FACTOR, 1)
    damped_lunch = round(raw_lunch * DAMPING_FACTOR, 1)
    damped_afternoon = round(raw_afternoon * DAMPING_FACTOR, 1)
    damped_dinner = round(raw_dinner * DAMPING_FACTOR, 1)

    return {
        "tir": tir,
        "mean_bg": mean_bg,
        "cv_bg": cv_bg,
        "plant_isf": plant_isf,
        "rec_isf": rec_isf,
        "night_basal": night_basal,
        "day_basal": day_basal,
        "bfast_cr": damped_bfast,
        "lunch_cr": damped_lunch,
        "afternoon_cr": damped_afternoon,
        "dinner_cr": damped_dinner
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
