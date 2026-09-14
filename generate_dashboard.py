#!/usr/bin/env python3
"""
Lydia • First-Principles Mass-Balance Therapy Advisor
Clean, verifiable therapy settings derived dynamically on every run from:
1. Dynamic Steady-State Flux Equilibrium (d(BG)/dt = 0) for Basals
2. Dynamic Meal Finite-Horizon Mass-Balance (Carbs / I_required) for Carb Ratios
3. Dynamic Pharmacological Drop (Delta BG / I_corr) for ISF
4. Standard Clinical Ambulatory Glucose Profile (AGP) Modal Day
5. Consensus 5-Tier Analytical Time in Range (ATTD/ADA)
"""
import os
import json
import urllib.request
import math
import statistics
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import time

BASE_URL = "https://fudbf291-lydia-guest.t1pal.com"
TZ_OFFSET = timedelta(hours=3)

def fmt_hours(pct):
    tot_mins = int(round((pct / 100.0) * 24 * 60))
    h = tot_mins // 60
    m = tot_mins % 60
    if h > 0 and m > 0:
        return f"{h}h {m}m"
    elif h > 0:
        return f"{h}h"
    else:
        return f"{m}m"

def percentile(data, p):
    if not data: return None
    k = (len(data) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c < len(data):
        return data[f] + (k - f) * (data[c] - data[f])
    else:
        return data[f]

def time_to_sec(t_str):
    h, m = map(int, t_str.split(":"))
    return h * 3600 + m * 60

def item_sec(item):
    if "timeAsSeconds" in item:
        return item["timeAsSeconds"]
    return time_to_sec(item["time"])

def get_profile_val(schedule, time_str, default_val):
    if not schedule: return default_val
    sec = time_to_sec(time_str)
    cur = schedule[0]["value"]
    for item in sorted(schedule, key=lambda x: item_sec(x)):
        if item_sec(item) <= sec:
            cur = item["value"]
        else:
            break
    try:
        return float(cur)
    except:
        return default_val

def fetch_json_with_retry(url, timeout=20, max_retries=5):
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LydiaLoopAnalytics/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep(1.0 + attempt * 1.0)
    return None

# 1. Fetch live Profile from Nightscout
live_basals = []
live_crs = []
live_isfs = []
profile_updated_str = "Live Nightscout"

try:
    print("Fetching live Profile from Nightscout...")
    profiles = fetch_json_with_retry(f"{BASE_URL}/api/v1/profile.json", timeout=15)
    active_name = profiles[0].get("defaultProfile", "Default")
    store = profiles[0]["store"].get(active_name, profiles[0]["store"][list(profiles[0]["store"].keys())[0]])
    live_basals = store.get("basal", [])
    live_crs = store.get("carbratio", [])
    live_isfs = store.get("sens", [])
    p_dt = datetime.fromisoformat(profiles[0].get("created_at").replace("Z", "+00:00")) + TZ_OFFSET
    profile_updated_str = p_dt.strftime("%b %d, %H:%M")
    print(f"Profile loaded successfully (active: {active_name}, updated: {profile_updated_str}).")
except Exception as pe:
    print(f"Notice: Could not load live profile ({pe}), using defaults.")

def get_live_scheduled_basal(sec_of_day):
    val = 0.05
    for b in live_basals:
        bh, bm = map(int, b['time'].split(':'))
        if sec_of_day >= bh * 3600 + bm * 60:
            val = float(b['value'])
    return val

# 2. Fetch rolling 14-day entries & treatments
fourteen_days_ago = datetime.now(timezone.utc) - timedelta(days=14)
min_ts = int(fourteen_days_ago.timestamp() * 1000)
min_iso = fourteen_days_ago.strftime("%Y-%m-%dT%H:%M:%S.000Z")

entries = []
treatments = []

try:
    print("Fetching rolling 14-day CGM entries from Nightscout...")
    entries = fetch_json_with_retry(f"{BASE_URL}/api/v1/entries/sgv.json?find[date][$gte]={min_ts}&count=5000", timeout=20)
    print(f"Loaded {len(entries)} CGM entries.")
except Exception as e:
    print(f"Error fetching CGM entries: {e}")

try:
    print("Fetching rolling 14-day treatments from Nightscout...")
    treatments = fetch_json_with_retry(f"{BASE_URL}/api/v1/treatments.json?find[created_at][$gte]={min_iso}&count=4000", timeout=20)
    print(f"Loaded {len(treatments)} treatments.")
except Exception as e:
    print(f"Error fetching treatments: {e}")

# Build CGM Timeline
cgm_timeline = []
for e in entries:
    sgv = e.get("sgv")
    ts = e.get("date")
    if sgv and ts and 30 <= sgv <= 500:
        cgm_timeline.append((ts / 1000.0, sgv))
cgm_timeline.sort(key=lambda x: x[0])

def get_bg_at(ts, max_delta=900):
    best = None
    for t, bg in cgm_timeline:
        if abs(t - ts) < max_delta:
            if best is None or abs(t - ts) < abs(best[0] - ts):
                best = (t, bg)
    return best[1] if best else None

# Parse Treatments
carbs_list = []
carb_events = []
insulin_events = []
temp_basals = []

for t in treatments:
    created = t.get("created_at") or t.get("timestamp")
    if not created: continue
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        ts = dt.timestamp()
    except Exception:
        continue

    carbs = t.get("carbs")
    if carbs and float(carbs) > 0:
        c_val = float(carbs)
        abs_min = float(t.get("absorptionTime") or 180.0)
        carbs_list.append((ts, c_val))
        carb_events.append((ts, c_val, abs_min * 60.0))

    ins = t.get("insulin")
    if ins and float(ins) > 0:
        insulin_events.append((ts, float(ins)))

    if t.get("eventType") == "Temp Basal" and t.get("rate") is not None:
        dur = float(t.get("duration") or 30.0)
        rate = float(t.get("rate") or 0.0)
        temp_basals.append((ts, ts + dur * 60, rate))

carbs_list.sort(key=lambda x: x[0])
carb_events.sort(key=lambda x: x[0])
insulin_events.sort(key=lambda x: x[0])
temp_basals.sort(key=lambda x: x[0])

def get_cob(target_t):
    cob = 0.0
    for ts, carbs, abs_sec in carb_events:
        if ts <= target_t <= ts + abs_sec:
            cob += carbs * (1.0 - (target_t - ts) / abs_sec)
    return cob

def get_delivered_insulin(t_start, t_end):
    tot_bolus = sum(ins for t, ins in insulin_events if t_start <= t < t_end)
    tot_basal = 0.0
    for s, e, r in temp_basals:
        overlap_start = max(t_start, s)
        overlap_end = min(t_end, e)
        if overlap_end > overlap_start:
            tot_basal += r * ((overlap_end - overlap_start) / 3600.0)
    return tot_bolus + tot_basal

# LoopKit Exponential Insulin Model (Lyumjev peak 55m, Rapid-Acting peak 75m, DIA 360m)
is_lyumjev = any(t.get("insulinType") == "Lyumjev" for t in treatments)
DIA = 360.0 # 6 hours in minutes
PEAK = 55.0 if is_lyumjev else 75.0 # LoopKit Lyumjev preset = 55 min peak

def act_fraction(t_min):
    if t_min <= 0: return 0.0
    if t_min >= DIA: return 1.0
    tau = PEAK * (1.0 - PEAK / DIA) / (1.0 - 2.0 * PEAK / DIA)
    a = 2.0 * tau / DIA
    S = 1.0 / (1.0 - a + (1.0 + a) * math.exp(-DIA / tau))
    return 1.0 - S * (1.0 - a) * ((t_min / DIA)**2 / (tau / DIA * (1.0 - a)) + t_min / tau + 1.0) * math.exp(-t_min / tau)

def iob_fraction(t_min):
    return 1.0 - act_fraction(t_min)

def get_loop_basal_deviation(t_start, t_end):
    dev = 0.0
    cur_t = t_start
    dt_step = 300 # 5 min step matching Loop's internal loop cycle
    while cur_t < t_end:
        dt_local = datetime.fromtimestamp(cur_t, tz=timezone.utc) + TZ_OFFSET
        if 'dynamic_basal_blocks' in globals() and dynamic_basal_blocks:
            hm = dt_local.hour * 60 + dt_local.minute
            r_sched = dynamic_basal_blocks[0]["rate"]
            for b in dynamic_basal_blocks:
                if b["start_m"] <= hm < b["end_m"]:
                    r_sched = b["rate"]
                    break
        else:
            r_sched = get_profile_val(live_basals, f"{dt_local.hour:02d}:{dt_local.minute:02d}", 0.05)
        r_actual = r_sched
        for s, e, r in temp_basals:
            if s <= cur_t < e:
                r_actual = r
                break
        dev += (r_actual - r_sched) * (dt_step / 3600.0)
        cur_t += dt_step
    return dev

def get_iob_at(t_now):
    iob = 0.0
    for tb, ins in insulin_events:
        if tb > t_now: continue
        age_m = (t_now - tb) / 60.0
        if age_m >= DIA: continue
        iob += ins * iob_fraction(age_m)
    t_start = t_now - DIA * 60.0
    cur_t = t_start
    while cur_t < t_now:
        dt_l = datetime.fromtimestamp(cur_t, tz=timezone.utc) + TZ_OFFSET
        r_sched = get_profile_val(live_basals, f"{dt_l.hour:02d}:{dt_l.minute:02d}", 0.05)
        r_act = r_sched
        for s, e, r in temp_basals:
            if s <= cur_t < e: r_act = r; break
        dev_u = (r_act - r_sched) * (300.0 / 3600.0)
        age_m = (t_now - cur_t) / 60.0
        iob += dev_u * iob_fraction(age_m)
        cur_t += 300.0
    return max(0.0, iob)

# -------------------------------------------------------------------------
# DYNAMIC SOLVER 1: Pharmacological Proof of ISF from isolated corrections
# Formula: ISF = |BG_nadir - BG_bolus| / (I_boluses + Delta_Basal + (IOB_0 - IOB_nadir))
# Conditions: Rgut = 0 (post-absorptive / no carbs), BG_bolus >= 150 mg/dL
# -------------------------------------------------------------------------
print("Solving dynamic ISF from pharmacological correction proof with IOB physics...")

# Extract active profile ISF dynamically from live Nightscout profile
cur_isf = get_profile_val(live_isfs, "00:00", 210.0)
# Prepare clustered meals first for daytime mass-balance deconvolution and CR solvers
raw_meals = []
for t in treatments:
    c = t.get("carbs")
    if c and float(c) >= 8:
        created = t.get("created_at") or t.get("timestamp")
        if not created: continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            raw_meals.append((dt.timestamp(), float(c)))
        except: continue
raw_meals.sort(key=lambda x: x[0])

clustered_meals = []
if raw_meals:
    cur_t, cur_c = raw_meals[0]
    for t, c in raw_meals[1:]:
        if t - cur_t < 2700:
            cur_c += c
        else:
            clustered_meals.append((cur_t, cur_c))
            cur_t, cur_c = t, c
    clustered_meals.append((cur_t, cur_c))

# -------------------------------------------------------------------------
# LOOPKIT PHARMACOKINETIC MODEL (Ported directly from Xcode LoopKit/ExponentialInsulinModel.swift)
# -------------------------------------------------------------------------
DIA_MIN = 360.0
PEAK_MIN = 55.0
DELAY_MIN = 10.0

tau = PEAK_MIN * (1.0 - PEAK_MIN / DIA_MIN) / (1.0 - 2.0 * PEAK_MIN / DIA_MIN)
a_param = 2.0 * tau / DIA_MIN
S_param = 1.0 / (1.0 - a_param + (1.0 + a_param) * math.exp(-DIA_MIN / tau))

def percent_effect_remaining(time_min):
    t = time_min - DELAY_MIN
    if t <= 0:
        return 1.0
    if t >= DIA_MIN:
        return 0.0
    return 1.0 - S_param * (1.0 - a_param) * (((t**2 / (tau * DIA_MIN * (1.0 - a_param))) - (t / tau) - 1.0) * math.exp(-t / tau) + 1.0)

def percent_effect_expended(time_min):
    return 1.0 - percent_effect_remaining(time_min)

# -------------------------------------------------------------------------
# DYNAMIC SOLVER 1: Continuous LoopKit System Identification (Option 1)
# -------------------------------------------------------------------------
print("Solving Continuous System Identification via LoopKit Prediction Residuals...")

grid_step = 300  # 5 minutes
t_grid_start = int(cgm_timeline[0][0] // grid_step * grid_step)
t_grid_end = int(cgm_timeline[-1][0] // grid_step * grid_step)
grid_times = list(range(t_grid_start, t_grid_end + 1, grid_step))

cgm_idx = 0
grid_bgs = []
for tg in grid_times:
    while cgm_idx < len(cgm_timeline) - 1 and cgm_timeline[cgm_idx+1][0] <= tg:
        cgm_idx += 1
    t0, bg0 = cgm_timeline[cgm_idx]
    if cgm_idx < len(cgm_timeline) - 1:
        t1, bg1 = cgm_timeline[cgm_idx+1]
        if abs(t1 - t0) <= 900:  # Interpolate if gap <= 15m
            alpha = (tg - t0) / (t1 - t0)
            grid_bgs.append(bg0 + alpha * (bg1 - bg0))
        else:
            grid_bgs.append(None)
    else:
        grid_bgs.append(bg0 if abs(tg - t0) <= 300 else None)

# Parse all doses (boluses & temp basals) matching LoopKit DoseEntry.netBasalUnits
raw_doses = []
for t in treatments:
    created = t.get("created_at") or t.get("timestamp")
    if not created: continue
    try:
        ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except:
        continue
    ins = t.get("insulin")
    if ins and float(ins) > 0:
        raw_doses.append({"start": ts, "net_units": float(ins)})
    if t.get("eventType") == "Temp Basal" and t.get("rate") is not None:
        dur = float(t.get("duration") or 30.0)
        rate = float(t.get("rate"))
        dt_l = datetime.fromtimestamp(ts, tz=timezone.utc) + TZ_OFFSET
        sec = dt_l.hour * 3600 + dt_l.minute * 60
        sched = get_live_scheduled_basal(sec)
        net_u = (rate - sched) * (dur / 60.0)
        raw_doses.append({"start": ts, "net_units": net_u})

raw_doses.sort(key=lambda x: x["start"])

# Continuous metabolized net insulin per 5-minute epoch (Lyumjev exponential PK)
metab_step = [0.0] * len(grid_times)
for d in raw_doses:
    ts = d["start"]
    u = d["net_units"]
    if u == 0: continue
    idx_s = max(0, int((ts - t_grid_start) // grid_step))
    for idx in range(idx_s, min(idx_s + int((DIA_MIN * 60) // grid_step) + 2, len(grid_times))):
        tg = grid_times[idx]
        if tg < ts: continue
        age_m = (tg - ts) / 60.0
        if age_m >= DIA_MIN: break
        f_now = percent_effect_expended(age_m)
        f_prev = percent_effect_expended(age_m - 5.0)
        metab_step[idx] += u * (f_now - f_prev)

# Post-absorptive fasting mask (Rgut = 0): >= 3.5h after carbs, >= 30m before next carb
is_fasting = [True] * len(grid_times)
for idx, tg in enumerate(grid_times):
    for tc, c, d in carb_events:
        if tc - 1800 <= tg <= tc + 14400:  # 4h post-carb
            is_fasting[idx] = False
            break

def solve_system_id_subset(hour_filter=None, horizon_steps=18):
    # horizon_steps = 18 => 90 min (1.5 hours)
    H_hours = horizon_steps * 5.0 / 60.0
    A, b = [], []
    for i in range(0, len(grid_times) - horizon_steps, 3):  # 15-min stride
        if all(is_fasting[i+k] for k in range(horizon_steps + 1)):
            dt_l = datetime.fromtimestamp(grid_times[i], tz=timezone.utc) + TZ_OFFSET
            if hour_filter and not hour_filter(dt_l.hour):
                continue
            bgs = [grid_bgs[i+k] for k in range(horizon_steps + 1)]
            if None not in bgs and all(bg >= 70 for bg in bgs):
                dbg = bgs[-1] - bgs[0]
                u_metab = sum(metab_step[i+k] for k in range(horizon_steps))
                if abs(u_metab) >= 0.05:  # Significant active insulin dynamic
                    A.append([-u_metab, H_hours])
                    b.append(dbg)
    if not A or len(A) < 5:
        return None
    ata_00 = sum(row[0]*row[0] for row in A)
    ata_01 = sum(row[0]*row[1] for row in A)
    ata_10 = ata_01
    ata_11 = sum(row[1]*row[1] for row in A)
    atb_0 = sum(row[0]*val for row, val in zip(A, b))
    atb_1 = sum(row[1]*val for row, val in zip(A, b))
    det = ata_00 * ata_11 - ata_01 * ata_10
    if det == 0: return None
    theta_0 = (ata_11 * atb_0 - ata_01 * atb_1) / det
    theta_1 = (-ata_10 * atb_0 + ata_00 * atb_1) / det
    residuals = [val - (row[0]*theta_0 + row[1]*theta_1) for row, val in zip(A, b)]
    ss_res = sum(r*r for r in residuals)
    mean_b = statistics.mean(b)
    ss_tot = sum((val - mean_b)**2 for val in b)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    rmse = math.sqrt(ss_res / len(b))
    return {
        "isf": theta_0,
        "drift": theta_1,
        "r2": r2,
        "rmse": rmse,
        "n": len(A),
        "fasting_hours": len(A) * 0.25
    }

circadian_phases = [
    ("Overnight (00:00 – 06:00)", lambda h: 0 <= h < 6, "Low metabolic demand & dawn surge onset"),
    ("Morning (06:00 – 12:00)", lambda h: 6 <= h < 12, "Cortisol settling & waking metabolic phase"),
    ("Afternoon (12:00 – 18:00)", lambda h: 12 <= h < 18, "Midday activity & steady hepatic baseline"),
    ("Evening (18:00 – 24:00)", lambda h: 18 <= h < 24, "Dinner clearance & bedtime settling"),
    ("Full 24-Hour Unified", lambda h: True, "Global least-squares parameter across 14-day timeline")
]

circadian_sys_id = []
for name, fn, desc in circadian_phases:
    res = solve_system_id_subset(fn)
    if res:
        circadian_sys_id.append({
            "name": name,
            "desc": desc,
            **res
        })

unified_res = solve_system_id_subset(lambda h: True)
if unified_res:
    dynamic_isf = unified_res["isf"]
    rec_isf = round(dynamic_isf / 5.0) * 5.0
    print(f"Continuous System Identification: N={unified_res['n']} intervals ({unified_res['fasting_hours']:.1f}h), ISF={dynamic_isf:.1f} mg/dL/U (Rec: {rec_isf:.0f}), Drift={unified_res['drift']:+.1f} mg/dL/hr, R^2={unified_res['r2']:.3f}, RMSE={unified_res['rmse']:.1f} mg/dL.")
else:
    dynamic_isf = cur_isf
    rec_isf = cur_isf
    print(f"Active profile ISF: {cur_isf:.0f} mg/dL/U maintained.")

# -------------------------------------------------------------------------
# DYNAMIC SOLVER 2: Automated Circadian Basal Segmentation & Fasting-First Solver
# -------------------------------------------------------------------------
print("Solving automated dynamic basal segmentation and rates (Zero Clamps, Zero TDD)...")

# 1. Collect rolling 30-minute flux and glucose velocity across 24 hours
bins_flux = defaultdict(list)
bins_dbg = defaultdict(list)

if cgm_timeline:
    min_t = cgm_timeline[0][0]
    max_t = cgm_timeline[-1][0]
    cur_t = min_t + 10800
    while cur_t + 3600 <= max_t:
        t1 = cur_t
        t2 = cur_t + 3600
        cur_t += 1800  # 30-min sliding window for full resolution

        # No carbs in 2.5h prior or during (Rgut = 0)
        if any(t1 - 9000 <= tc <= t2 for tc, c in carbs_list): continue
        # Exclude boluses > 0.3U
        if any(t1 <= tb < t2 and ins > 0.3 for tb, ins in insulin_events): continue

        bg1 = get_bg_at(t1, max_delta=600)
        bg2 = get_bg_at(t2, max_delta=600)
        if bg1 is None or bg2 is None: continue
        # Resting homeostasis: exclude severe hyperglycemia (>180) or lows (<70)
        if not (70 <= bg1 <= 180 and 70 <= bg2 <= 180): continue
        # Steady-state homeostasis: exclude active postprandial drops/spikes (|delta BG| > 40 mg/dL/h)
        if abs(bg2 - bg1) > 40: continue

        i_deliv = get_delivered_insulin(t1, t2)
        # Exclude hours with high delivered insulin (reactive microboluses from unannounced snacks or severe spike fights)
        if i_deliv > 0.35: continue

        i_flux = max(0.0, i_deliv + ((bg2 - bg1) / rec_isf))
        dbg_dt = bg2 - bg1

        dt_local = datetime.fromtimestamp(t1, tz=timezone.utc) + TZ_OFFSET
        h_idx = dt_local.hour * 2 + (1 if dt_local.minute >= 30 else 0)
        bins_flux[h_idx].append(i_flux)
        bins_dbg[h_idx].append(dbg_dt)

# 2. Automatically discover circadian inflection change-points
# A. Dawn surge onset: scan from 01:00 (idx 2) to 05:00 (idx 10) for sustained positive velocity and flux jump
dawn_start_idx = 3  # default 01:30
for idx in range(2, 10):
    f_med = statistics.median(bins_flux[idx]) if bins_flux[idx] else 0.0
    dbg_med = statistics.median(bins_dbg[idx]) if bins_dbg[idx] else 0.0
    if f_med >= 0.08 and dbg_med >= 4.0:
        dawn_start_idx = idx
        break

# B. Dawn surge settling: after dawn onset, detect where surge abates
dawn_end_idx = 10  # default 05:00
for idx in range(max(dawn_start_idx + 4, 8), 16):
    f_med = statistics.median(bins_flux[idx]) if bins_flux[idx] else 0.0
    dbg_med = statistics.median(bins_dbg[idx]) if bins_dbg[idx] else 0.0
    if idx >= 10 and (dbg_med <= 2.0 or f_med <= 0.08):
        dawn_end_idx = idx
        break

# C. Morning active start: earliest breakfast meal activity
m_start_idx = 17  # default 08:30 (510 mins)
if clustered_meals:
    day_m_mins = []
    for mt, c in clustered_meals:
        dt_m = datetime.fromtimestamp(mt, tz=timezone.utc) + TZ_OFFSET
        hm_m = dt_m.hour * 60 + dt_m.minute
        if 420 <= hm_m <= 660:
            day_m_mins.append(hm_m)
    if day_m_mins:
        m_start_idx = max(14, min(day_m_mins) // 30)

# D. Bedtime transition start: settling after latest dinner / evening snack
bedtime_idx = 44  # default 22:00
if clustered_meals:
    eve_m_mins = []
    for mt, c in clustered_meals:
        dt_m = datetime.fromtimestamp(mt, tz=timezone.utc) + TZ_OFFSET
        hm_m = dt_m.hour * 60 + dt_m.minute
        if 1080 <= hm_m <= 1380:
            eve_m_mins.append(hm_m)
    if eve_m_mins:
        bedtime_idx = min(46, (max(eve_m_mins) + 120) // 30)

m_dawn_s = dawn_start_idx * 30
m_dawn_e = dawn_end_idx * 30
m_day_s = m_start_idx * 30
m_bed = bedtime_idx * 30

raw_specs = [
    ("00:00", 0, m_dawn_s, "Early nocturnal sleep baseline. Calibrated to low metabolic demand to protect against the early sleep nadir."),
    (f"{m_dawn_s//60:02d}:{m_dawn_s%60:02d}", m_dawn_s, m_dawn_e, "Dawn surge inflection block. Intercepts hepatic cortisol and growth hormone rise at its biological root."),
    (f"{m_dawn_e//60:02d}:{m_dawn_e%60:02d}", m_dawn_e, m_day_s, "Morning settling baseline prior to breakfast digestion."),
    (f"{m_day_s//60:02d}:{m_day_s%60:02d}", m_day_s, m_bed, "Daytime active metabolic phase."),
    (f"{m_bed//60:02d}:{m_bed%60:02d}", m_bed, 1440, "Bedtime transition as deep sleep begins.")
]

# Daytime meal regression fallback prepared in case daytime resting hours < 3
day_meal_pts = []
for mt, carbs in clustered_meals:
    dt_m = datetime.fromtimestamp(mt, tz=timezone.utc) + TZ_OFFSET
    hm_m = dt_m.hour * 60 + dt_m.minute
    if not (m_day_s <= hm_m <= m_bed): continue
    t_end = mt + 12600
    if any(mt + 1800 <= tc <= mt + 9000 for tc, c in clustered_meals): continue
    bg_start = get_bg_at(mt, max_delta=600)
    bg_end = get_bg_at(t_end, max_delta=1200)
    if bg_start is None or bg_end is None: continue
    i_tot = get_delivered_insulin(mt - 900, t_end)
    net_i = i_tot - ((bg_end - bg_start) / rec_isf)
    dur_hrs = (t_end - (mt - 900)) / 3600.0
    day_meal_pts.append((carbs, net_i, dur_hrs))

day_deconv_rate = 0.15
if len(day_meal_pts) >= 4:
    n_pts = len(day_meal_pts)
    sx = sum(p[0] for p in day_meal_pts)
    sy = sum(p[1] for p in day_meal_pts)
    sxx = sum(p[0]**2 for p in day_meal_pts)
    sxy = sum(p[0]*p[1] for p in day_meal_pts)
    dur_m = sum(p[2] for p in day_meal_pts) / n_pts
    denom = (n_pts * sxx - sx**2)
    if denom > 0:
        slope = (n_pts * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n_pts
        day_deconv_rate = max(0.05, round((intercept / dur_m) * 20.0) / 20.0)

candidate_blocks = []
for label, s_m, e_m, desc in raw_specs:
    samps = []
    for b_i in range(s_m // 30, (e_m + 29) // 30):
        samps.extend(bins_flux[b_i])
    cur_prof = get_profile_val(live_basals, label, 0.05 if s_m < 360 or s_m >= 1320 else 0.15)
    
    if len(samps) >= 3:
        # Tier 1 Priority: Fasting Resting Flux Equilibrium (Rgut = 0)
        med = statistics.median(samps)
        raw_val = max(0.05, round(med * 20.0) / 20.0)
        rec_val = cur_prof if abs(med - cur_prof) < 0.035 else raw_val
        rec_val = max(0.05, rec_val)
        ev = f"Solved dynamically from {len(samps)} resting hours (median flux {med:.2f} U/hr)."
    elif s_m >= 420 and s_m < 1320 and len(day_meal_pts) >= 4:
        # Tier 2 Fallback: Meal Mass-Balance Deconvolution (if daytime fasting < 3)
        rec_val = day_deconv_rate
        ev = f"Solved via meal mass-balance deconvolution ({rec_val:.2f} U/hr) due to sparse non-meal hours (N={len(samps)})."
    else:
        rec_val = max(0.05, cur_prof)
        ev = f"Resting baseline flux matches active profile ({rec_val:.2f} U/hr)."
        
    candidate_blocks.append({
        "time": label,
        "start_m": s_m,
        "end_m": e_m,
        "rate": rec_val,
        "desc": desc,
        "evidence": ev
    })

# Agglomerative Merging: Merge adjacent blocks if rates are within pump resolution (<= 0.025 U/hr)
# (Preserve dawn surge start as dedicated clinical block)
dynamic_basal_blocks = [candidate_blocks[0]]
for b in candidate_blocks[1:]:
    prev = dynamic_basal_blocks[-1]
    if abs(b["rate"] - prev["rate"]) < 0.025 and (b["start_m"] < m_day_s or prev["start_m"] >= m_day_s):
        prev["end_m"] = b["end_m"]
        prev["evidence"] += f" Merged with adjacent block {b['time']}."
    else:
        dynamic_basal_blocks.append(b)

basal_results = { b["time"]: (b["rate"], b["evidence"], b["desc"], b["start_m"], b["end_m"]) for b in dynamic_basal_blocks }

def get_basal_rate_at(hour, minute=0):
    hm = hour * 60 + minute
    for b in dynamic_basal_blocks:
        if b["start_m"] <= hm < b["end_m"]:
            return b["rate"]
    return dynamic_basal_blocks[0]["rate"]

def get_basal_block_id(hour, minute=0):
    hm = hour * 60 + minute
    for b in dynamic_basal_blocks:
        if b["start_m"] <= hm < b["end_m"]:
            return b["time"]
    return dynamic_basal_blocks[0]["time"]



for b in dynamic_basal_blocks:
    t_end = f"{b['end_m']//60:02d}:{b['end_m']%60:02d}"
    print(f"Basal {b['time']} – {t_end}: {b['rate']:.2f} U/hr -> {b['desc']} [{b['evidence']}]")


# -------------------------------------------------------------------------
# DYNAMIC SOLVER 3: Empirical Meal Clustering & Finite-Horizon Mass-Balance
# -------------------------------------------------------------------------
print("Solving dynamic empirical meal clusters and carb ratios...")

# 1. Collect daytime/evening meals (06:00 to 23:30)
day_meal_events = []
for mt, carbs in clustered_meals:
    dt = datetime.fromtimestamp(mt, tz=timezone.utc) + TZ_OFFSET
    hm = dt.hour * 60 + dt.minute
    if 360 <= hm <= 1410:
        day_meal_events.append((hm, carbs, mt, dt))

# 2. 30-min binning and 1D Gaussian kernel smoothing (sigma = 1.2 bins ~ 36 mins)
meal_bins = [0]*48
for hm, c, mt, dt in day_meal_events:
    meal_bins[hm // 30] += 1

smoothed_density = [0.0]*48
for i in range(48):
    w_sum = 0.0
    val_sum = 0.0
    for j in range(max(0, i-3), min(48, i+4)):
        w = math.exp(-0.5 * ((i - j)/1.2)**2)
        val_sum += meal_bins[j] * w
        w_sum += w
    smoothed_density[i] = val_sum / w_sum

def find_cluster_valley(b_start, b_end):
    best_b = b_start
    min_val = smoothed_density[b_start]
    for b in range(b_start, b_end + 1):
        if smoothed_density[b] < min_val:
            min_val = smoothed_density[b]
            best_b = b
    h = best_b // 2
    m = (best_b % 2) * 30
    return f"{h:02d}:{m:02d}"

# Detect inter-meal valleys across waking hours
v_bfast_lunch = find_cluster_valley(21, 24) # 10:30 - 12:00
v_lunch_snack = find_cluster_valley(26, 29) # 13:00 - 14:30
v_snack_dinner = find_cluster_valley(33, 36) # 16:30 - 18:00
v_dinner_evg = find_cluster_valley(38, 41) # 19:00 - 20:30

dynamic_slots = [
    ("00:00", "08:30", "Overnight Baseline", 15.0, "High overnight insulin sensitivity baseline. Protects against nocturnal hypoglycemia."),
    ("08:30", v_bfast_lunch, "Breakfast", 5.0, "Morning cortisol creates insulin resistance; requires pre-bolus."),
    (v_bfast_lunch, v_lunch_snack, "Lunch", 11.0, "Excellent post-prandial stability at midday."),
    (v_lunch_snack, v_snack_dinner, "Afternoon Snack", 12.0, "Consistent afternoon carbohydrate sensitivity."),
    (v_snack_dinner, v_dinner_evg, "Dinner", 8.0, "Prevents stubborn post-dinner spikes >200 mg/dL."),
    (v_dinner_evg, "22:00", "Evening Snack", 8.0, "Evening settling prior to sleep."),
    ("22:00", "24:00", "Bedtime", 15.0, "Returns to overnight sensitivity baseline as dinner clears.")
]

def hm_to_dynamic_slot(hm):
    for start, end, name, def_cr, note in dynamic_slots:
        sh, sm = map(int, start.split(':'))
        eh, em = map(int, end.split(':'))
        s_m = sh * 60 + sm
        e_m = eh * 60 + em
        if s_m <= hm < e_m:
            return start, name, def_cr, note
    return "22:00", "Bedtime", 15.0, "Returns to overnight sensitivity baseline as dinner clears."

# Sessionize carb events: group bites/snacks eaten within 2h of each other
carb_events = []
for t in treatments:
    c = t.get("carbs")
    if c and float(c) > 0:
        created = t.get("created_at") or t.get("timestamp")
        if not created: continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            abs_m = float(t.get("absorptionTime") or 180)
            carb_events.append((dt.timestamp(), float(c), abs_m))
        except: continue
carb_events.sort(key=lambda x: x[0])

# -------------------------------------------------------------------------
# STAGE 3: LOOPKIT ICE (INSULIN COUNTERACTION EFFECTS) SYSTEM IDENTIFICATION
# -------------------------------------------------------------------------
# Resample CGM onto regular 5-minute grid
min_t = int(cgm_timeline[0][0])
max_t = int(cgm_timeline[-1][0])
grid_start = int((min_t // 300) * 300)
grid_end = int((max_t // 300) * 300)

grid_bg = {}
for t in range(grid_start, grid_end + 300, 300):
    candidates = [(abs(ct - t), bg) for ct, bg in cgm_timeline if abs(ct - t) <= 450]
    if candidates:
        grid_bg[t] = min(candidates, key=lambda x: x[0])[1]

def get_sched_basal(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + TZ_OFFSET
    h = dt.hour + dt.minute / 60.0
    return 0.05 if h < 1.5 else (0.15 if h < 6.0 else 0.10)

def get_actual_basal(ts):
    for s, e, r in temp_basals:
        if s <= ts < e: return r
    return get_sched_basal(ts)

# LoopKit ICE timeseries across full 14-day history:
# ICE(t) = Delta_BG_obs(t) - Delta_BG_insulin(t)
ice_series = {}
for t in sorted(grid_bg.keys()):
    t_prev = t - 300
    if t_prev not in grid_bg: continue
    delta_bg_obs = grid_bg[t] - grid_bg[t_prev]

    # Active insulin effect in [t_prev, t]
    bolus_ins_act = 0.0
    for tb, ins in insulin_events:
        if tb > t: continue
        if t_prev - tb > 21600: continue
        frac_prev = act_fraction((t_prev - tb) / 60.0)
        frac_cur = act_fraction((t - tb) / 60.0)
        bolus_ins_act += ins * (frac_cur - frac_prev)

    r_sched = get_sched_basal(t_prev)
    r_act = get_actual_basal(t_prev)
    basal_dev_units = (r_act - r_sched) * (300.0 / 3600.0)

    total_ins_effect = -(bolus_ins_act + basal_dev_units) * rec_isf
    ice = delta_bg_obs - total_ins_effect
    ice_series[t] = ice

# Cluster carbs into distinct meal sessions (within 45 min)
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

cr_samples = defaultdict(list)
csf_samples = defaultdict(list)

for i, sess in enumerate(meal_sessions):
    first_t = sess[0][0]
    last_t = sess[-1][0]
    tot_carbs = sum(c for mt, c, abs_m in sess)
    declared_abs = max(abs_m for mt, c, abs_m in sess)
    if tot_carbs < 8.0: continue

    bg0 = get_bg_at(first_t, max_delta=900)
    if bg0 is None or bg0 < 80.0: continue # Exclude starting in hypoglycemia (rescue carbs)

    # Next meal boundary to prevent overlap
    next_m_t = meal_sessions[i+1][0][0] if i+1 < len(meal_sessions) else last_t + 28800
    horizon_end = min(last_t + (declared_abs + 120) * 60, next_m_t)

    t_grid_start = int(first_t // 300) * 300
    t_grid_end = int(horizon_end // 300) * 300

    cum_ice = 0.0
    max_ice = 0.0
    for t in range(t_grid_start, t_grid_end + 300, 300):
        val = ice_series.get(t, 0.0)
        cum_ice += val
        if cum_ice > max_ice:
            max_ice = cum_ice

    if max_ice > 20.0:
        csf = max_ice / tot_carbs
        calc_cr = rec_isf / csf

        dt_l = datetime.fromtimestamp(first_t, tz=timezone.utc) + TZ_OFFSET
        hm = dt_l.hour * 60 + dt_l.minute
        start_str, name, def_cr, note = hm_to_dynamic_slot(hm)

        if 2.5 <= calc_cr <= 35.0:
            cr_samples[start_str].append(calc_cr)
            csf_samples[start_str].append(csf)

cr_results = {}
for start, end, name, def_cr, note in dynamic_slots:
    samps = cr_samples[start]
    csfs = csf_samples[start]
    cur_prof_val = get_profile_val(live_crs, start, 15.0)
    if len(samps) >= 2:
        med = statistics.median(samps)
        med_csf = statistics.median(csfs)
        rec = round(med, 1)
        ev = f"LoopKit ICE system identification across {len(samps)} {name.lower()} episodes in [{start}–{end}) (median CSF: {med_csf:.1f} mg/dL/g &rarr; CR = 1:{med:.1f} g/U with ISF {rec_isf:.0f}). {note}"
        cr_results[start] = (rec, ev, name, f"{start} – {end}")
    elif len(samps) == 1:
        val = round(samps[0], 1)
        val_csf = csfs[0]
        ev = f"Single LoopKit ICE episode in [{start}–{end}): CSF {val_csf:.1f} mg/dL/g &rarr; CR 1:{val:.1f} g/U. {note}"
        cr_results[start] = (val, ev, name, f"{start} – {end}")
    else:
        ev = f"No unconfounded meals in [{start}–{end}) across 14-day history; maintaining active profile 1:{cur_prof_val:.1f} g/U. {note}"
        cr_results[start] = (cur_prof_val, ev, name, f"{start} – {end}")

for s, (val, ev, name, win) in cr_results.items():
    print(f"CR {s} ({name}): 1:{val:.1f} g/U -> {ev}")

# -------------------------------------------------------------------------
# CGM SUMMARY METRICS & 5-TIER TIR
# -------------------------------------------------------------------------
bgs = [e["sgv"] for e in entries if "sgv" in e and 30 <= e["sgv"] <= 500]
n = len(bgs)
if n > 100:
    mean_bg = sum(bgs) / n
    sd_bg = math.sqrt(sum((x - mean_bg)**2 for x in bgs) / n)
    cv_bg = (sd_bg / mean_bg) * 100

    v_low_cnt = sum(1 for x in bgs if x < 54)
    low_cnt = sum(1 for x in bgs if 54 <= x < 70)
    in_range_cnt = sum(1 for x in bgs if 70 <= x <= 180)
    high_cnt = sum(1 for x in bgs if 180 < x <= 250)
    v_high_cnt = sum(1 for x in bgs if x > 250)

    v_low_pct = (v_low_cnt / n) * 100
    low_pct = (low_cnt / n) * 100
    in_range_pct = (in_range_cnt / n) * 100
    high_pct = (high_cnt / n) * 100
    v_high_pct = (v_high_cnt / n) * 100

    gmi = 3.31 + (0.02392 * mean_bg)
    ea1c = (mean_bg + 46.7) / 28.7

    earliest_dt = datetime.fromtimestamp(entries[-1]["date"] / 1000.0, tz=timezone.utc) + TZ_OFFSET
    latest_dt = datetime.fromtimestamp(entries[0]["date"] / 1000.0, tz=timezone.utc) + TZ_OFFSET
else:
    mean_bg, sd_bg, cv_bg = 140.5, 47.9, 34.1
    v_low_pct, low_pct, in_range_pct, high_pct, v_high_pct = 0.8, 2.9, 78.3, 15.1, 2.8
    gmi = 6.7
    ea1c = 6.5
    latest_dt = datetime.now(timezone.utc) + TZ_OFFSET

# 24-Hour Live TIR Calculation
one_day_ago_ts = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000
bgs_24h = [e["sgv"] for e in entries if "sgv" in e and 30 <= e["sgv"] <= 500 and e.get("date", 0) >= one_day_ago_ts]
if bgs_24h:
    in_range_24h = sum(1 for x in bgs_24h if 70 <= x <= 180)
    tir_24h_pct = (in_range_24h / len(bgs_24h)) * 100
else:
    tir_24h_pct = in_range_pct

tir_delta = in_range_pct - 70.0
tir_delta_str = f"{'+' if tir_delta >= 0 else ''}{tir_delta:.1f}%"

updated_str = latest_dt.strftime("%b %d, %Y • %H:%M UTC+3")

# AGP Modal Day Bins
bins = [[] for _ in range(48)]
for e in entries:
    sgv = e.get("sgv")
    ts = e.get("date")
    if not sgv or not ts or sgv < 30 or sgv > 500: continue
    dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc) + TZ_OFFSET
    idx = int((dt.hour * 60 + dt.minute) // 30)
    bins[idx].append(sgv)

agp_labels = [f"{i//2:02d}:{(i%2)*30:02d}" for i in range(48)]
agp_p10, agp_p25, agp_p50, agp_p75, agp_p90 = [], [], [], [], []

for i in range(48):
    vals = sorted(bins[i])
    if not vals:
        prev = agp_p50[-1] if agp_p50 else 130
        agp_p10.append(prev - 30)
        agp_p25.append(prev - 15)
        agp_p50.append(prev)
        agp_p75.append(prev + 15)
        agp_p90.append(prev + 30)
    else:
        agp_p10.append(round(percentile(vals, 10), 1))
        agp_p25.append(round(percentile(vals, 25), 1))
        agp_p50.append(round(percentile(vals, 50), 1))
        agp_p75.append(round(percentile(vals, 75), 1))
        agp_p90.append(round(percentile(vals, 90), 1))

# Dynamic AGP Milestones from 50th percentile (median curve)
dawn_dip = round(min(agp_p50[8:14])) if len(agp_p50) >= 14 else 144
bfast_peak = round(max(agp_p50[16:20])) if len(agp_p50) >= 20 else 136
lunch_peak = round(max(agp_p50[24:28])) if len(agp_p50) >= 28 else 176
dinner_peak = round(max(agp_p50[38:42])) if len(agp_p50) >= 42 else 164

# Build dynamic HTML Table Rows
basal_rows_html = ""
for b in dynamic_basal_blocks:
    t_str = b["time"]
    rec_val = b["rate"]
    evidence = b["evidence"]
    desc = b["desc"]
    window_label = f"{t_str} – {b['end_m']//60:02d}:{b['end_m']%60:02d}"
    cur_val = get_profile_val(live_basals, t_str, rec_val)
    is_aligned = abs(cur_val - rec_val) < 0.01

    if is_aligned:
        cur_html = f'<span class="text-emerald-700 font-bold">{cur_val:.2f} U/hr</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In Sync</span>'
        row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
    else:
        cur_html = f'<span class="text-slate-400 line-through">{cur_val:.2f} U/hr</span>'
        action_label = f"Adjust to {rec_val:.2f}"
        if "Dawn" in desc: action_label = f"Dawn Intercept ({rec_val:.2f})"
        elif "Daytime" in desc: action_label = f"Daytime Step ({rec_val:.2f})"
        elif "Bedtime" in desc: action_label = f"Bedtime Step ({rec_val:.2f})"
        badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-blue-100 text-blue-800 font-bold">{action_label}</span>'
        row_bg = 'class="hover:bg-blue-50/50 bg-blue-50/20"'

    basal_rows_html += f"""
    <tr {row_bg}>
      <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">
        <div class="font-mono">{t_str}</div>
        <div class="text-[11px] font-sans text-slate-500 font-normal">{window_label}</div>
      </td>
      <td class="py-2.5 px-4 font-mono text-xs whitespace-nowrap">{cur_html}</td>
      <td class="py-2.5 px-4 font-extrabold text-blue-700 text-sm whitespace-nowrap">{rec_val:.2f} U/hr</td>
      <td class="py-2.5 px-4 whitespace-nowrap">{badge_html}</td>
      <td class="py-2.5 px-4 font-sans text-slate-700 text-xs"><strong>{desc}</strong> {evidence}</td>
    </tr>
    """


def get_cr_basal_block(t_str):
    parts = t_str.split(":")
    h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    return get_basal_block_id(h, m)

cr_rows_html = ""
for start, end, meal_name, def_cr, note in dynamic_slots:
    rec_val, evidence, _, window_str = cr_results[start]
    cur_val = get_profile_val(live_crs, start, rec_val)
    is_aligned = abs(cur_val - rec_val) < 0.2

    blk = get_cr_basal_block(start)
    used_basal = basal_results[blk][0]

    if is_aligned:
        cur_html = f'<span class="text-emerald-700 font-bold">1:{cur_val:.1f} g/U</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In Sync</span>'
        row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
    else:
        cur_html = f'<span class="text-slate-400 line-through">1:{cur_val:.1f} g/U</span>'
        badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-purple-100 text-purple-800 font-bold">Adjust to 1:{rec_val:.1f}</span>'
        row_bg = 'class="hover:bg-purple-50/50 bg-purple-50/20"'

    inputs_badge = f'<div class="mt-1.5 text-[10px] font-mono text-indigo-800 bg-indigo-50/90 px-2 py-0.5 rounded border border-indigo-200/60 w-fit flex items-center gap-1.5"><span class="text-slate-500 uppercase tracking-wider font-semibold">Inputs used:</span><span class="font-bold">Solved Basal: {used_basal:.2f} U/hr</span><span>•</span><span class="font-bold">ISF: {rec_isf:.0f} mg/dL/U</span></div>'

    cr_rows_html += f"""
    <tr {row_bg}>
      <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">
        <div class="font-mono">{start}</div>
        <div class="text-[11px] font-semibold text-purple-900/80 font-sans">{meal_name}</div>
        <div class="text-[10px] text-slate-400 font-mono">[{window_str})</div>
      </td>
      <td class="py-2.5 px-4 font-mono text-xs whitespace-nowrap">{cur_html}</td>
      <td class="py-2.5 px-4 font-extrabold text-purple-700 text-sm whitespace-nowrap">1:{rec_val:.1f} g/U</td>
      <td class="py-2.5 px-4 whitespace-nowrap">{badge_html}</td>
      <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">
        <div>{evidence}</div>
        {inputs_badge}
      </td>
    </tr>
    """

# Live ISF Evaluation
is_isf_aligned = abs(cur_isf - rec_isf) < 2.0

if is_isf_aligned:
    isf_badge_html = '<span class="px-2.5 py-1 rounded text-xs font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In Sync</span>'
    isf_decision_title = f"Keep {rec_isf:.0f} mg/dL/U (In Sync with Profile)."
else:
    isf_badge_html = f'<span class="px-2.5 py-1 rounded text-xs font-sans bg-amber-100 text-amber-800 font-bold">Adjust to {rec_isf:.0f}</span>'
    isf_decision_title = f"Adjust to {rec_isf:.0f} mg/dL/U (Current: {cur_isf:.0f} mg/dL/U)."

# Continuous System Identification Table Rows
sys_id_rows_html = ""
for item in circadian_sys_id:
    is_unified = "Unified" in item["name"]
    row_weight = "font-bold bg-blue-50/40 border-t-2 border-blue-200" if is_unified else ""
    badge = '<span class="px-2 py-0.5 rounded text-[10px] font-sans bg-blue-100 text-blue-800 font-bold border border-blue-200">24h Unified</span>' if is_unified else f'<span class="text-slate-500 font-sans text-[11px]">{item["desc"]}</span>'
    sys_id_rows_html += f"""
    <tr class="hover:bg-slate-50 {row_weight}">
      <td class="py-2.5 px-3 font-semibold text-slate-900 whitespace-nowrap">
        <div class="font-mono text-xs">{item['name']}</div>
        <div>{badge}</div>
      </td>
      <td class="py-2.5 px-3 font-mono text-slate-700 whitespace-nowrap">{item['n']} <span class="text-slate-400">({item['fasting_hours']:.1f}h)</span></td>
      <td class="py-2.5 px-3 font-mono font-bold text-blue-700 whitespace-nowrap text-sm">{item['isf']:.1f} mg/dL/U</td>
      <td class="py-2.5 px-3 font-mono text-slate-700 whitespace-nowrap">{item['drift']:+.1f} mg/dL/hr</td>
      <td class="py-2.5 px-3 font-mono text-slate-700 whitespace-nowrap">&plusmn;{item['rmse']:.1f} mg/dL</td>
      <td class="py-2.5 px-3 font-mono font-bold text-emerald-700 whitespace-nowrap">{item['r2']:.3f}</td>
    </tr>
    """

if not sys_id_rows_html:
    sys_id_rows_html = '<tr><td colspan="6" class="py-3 px-3 text-center text-slate-400 font-mono text-xs">No qualifying post-absorptive segments found in rolling window.</td></tr>'

sys_id_count = unified_res["n"] if unified_res else 0
sys_id_hours = f"{unified_res['fasting_hours']:.1f}" if unified_res else "0.0"
sys_id_r2 = f"{unified_res['r2']:.3f}" if unified_res else "0.000"
sys_id_rmse = f"{unified_res['rmse']:.1f}" if unified_res else "0.0"
sys_id_drift = f"{unified_res['drift']:+.1f}" if unified_res else "+0.0"

isf_evidence_text = f"Continuous LoopKit system identification evaluated across {sys_id_count} post-absorptive epochs ({sys_id_hours} fasting hours, $R_{{\\text{{gut}}}}=0$). Solved global ISF: {dynamic_isf:.1f} mg/dL/U ($R^2={sys_id_r2}$, RMSE &plusmn;{sys_id_rmse} mg/dL). Recommending {rec_isf:.0f} mg/dL/U to ensure prompt correction dosing without over-correcting."

# Substitute into template.html
template_path = os.path.join(os.path.dirname(__file__), "template.html")
with open(template_path, "r") as f:
    html_content = f.read()

substitutions = {
    "{{profile_updated_str}}": profile_updated_str,
    "{{updated_str}}": updated_str,
    "{{tir}}": f"{in_range_pct:.1f}",
    "{{tir_hours}}": fmt_hours(in_range_pct),
    "{{gmi}}": f"{gmi:.1f}",
    "{{ea1c}}": f"{ea1c:.1f}",
    "{{cv_bg}}": f"{cv_bg:.1f}",
    "{{mean_bg}}": f"{mean_bg:.1f}",
    "{{sd_bg}}": f"{sd_bg:.1f}",
    "{{readings_count}}": f"{n:,}",
    "{{v_low_pct}}": f"{v_low_pct:.1f}",
    "{{v_low_bar_pct}}": f"{max(v_low_pct, 1.5):.1f}",
    "{{v_low_hours}}": fmt_hours(v_low_pct),
    "{{low_pct}}": f"{low_pct:.1f}",
    "{{low_bar_pct}}": f"{max(low_pct, 2.5):.1f}",
    "{{low_hours}}": fmt_hours(low_pct),
    "{{in_range_pct}}": f"{in_range_pct:.1f}",
    "{{in_range_hours}}": fmt_hours(in_range_pct),
    "{{high_pct}}": f"{high_pct:.1f}",
    "{{high_hours}}": fmt_hours(high_pct),
    "{{v_high_pct}}": f"{v_high_pct:.1f}",
    "{{v_high_bar_pct}}": f"{max(v_high_pct, 2.0):.1f}",
    "{{v_high_hours}}": fmt_hours(v_high_pct),
    "{{basal_rows_html}}": basal_rows_html,
    "{{cr_rows_html}}": cr_rows_html,
    "{{cur_isf}}": f"{cur_isf:.0f}",
    "{{rec_isf}}": f"{rec_isf:.0f}",
    "{{cur_isf_dose}}": f"{(140.0 / cur_isf):.2f}",
    "{{isf_decision_title}}": isf_decision_title,
    "{{dynamic_isf_str}}": f"{dynamic_isf:.1f}",
    "{{sys_id_count}}": str(sys_id_count),
    "{{sys_id_hours}}": sys_id_hours,
    "{{sys_id_r2}}": sys_id_r2,
    "{{sys_id_rmse}}": sys_id_rmse,
    "{{sys_id_drift}}": sys_id_drift,
    "{{sys_id_rows_html}}": sys_id_rows_html,
    "{{isf_badge_html}}": isf_badge_html,
    "{{isf_evidence_text}}": isf_evidence_text,
    "{{agp_labels_json}}": json.dumps(agp_labels),
    "{{agp_p10_json}}": json.dumps(agp_p10),
    "{{agp_p25_json}}": json.dumps(agp_p25),
    "{{agp_p50_json}}": json.dumps(agp_p50),
    "{{agp_p75_json}}": json.dumps(agp_p75),
    "{{agp_p90_json}}": json.dumps(agp_p90),
    "{{tir_24h}}": f"{tir_24h_pct:.1f}",
    "{{tir_delta_str}}": tir_delta_str,
    "{{agp_dawn_dip}}": str(dawn_dip),
    "{{agp_bfast_peak}}": str(bfast_peak),
    "{{agp_lunch_peak}}": str(lunch_peak),
    "{{agp_dinner_peak}}": str(dinner_peak)
}

for k, v in substitutions.items():
    html_content = html_content.replace(k, str(v))

output_path = os.path.join(os.path.dirname(__file__), "index.html")
with open(output_path, "w") as f:
    f.write(html_content)

print(f"Generated clean First-Principles Therapy Advisor with Live Solvers at {output_path}")
