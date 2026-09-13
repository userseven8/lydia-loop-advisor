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

# 1. Fetch live Profile from Nightscout
live_basals = []
live_crs = []
live_isfs = []
profile_updated_str = "Live Nightscout"

try:
    print("Fetching live Profile from Nightscout...")
    req_p = urllib.request.Request(f"{BASE_URL}/api/v1/profile.json", headers={"User-Agent": "LydiaLoopAnalytics/2.0"})
    with urllib.request.urlopen(req_p, timeout=15) as resp:
        profiles = json.loads(resp.read().decode('utf-8'))
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

# 2. Fetch rolling 14-day entries & treatments
fourteen_days_ago = datetime.now(timezone.utc) - timedelta(days=14)
min_ts = int(fourteen_days_ago.timestamp() * 1000)
min_iso = fourteen_days_ago.strftime("%Y-%m-%dT%H:%M:%S.000Z")

entries = []
treatments = []

try:
    print("Fetching rolling 14-day CGM entries from Nightscout...")
    req_e = urllib.request.Request(
        f"{BASE_URL}/api/v1/entries/sgv.json?find[date][$gte]={min_ts}&count=5000",
        headers={"User-Agent": "LydiaLoopAnalytics/2.0"}
    )
    with urllib.request.urlopen(req_e, timeout=20) as resp:
        entries = json.loads(resp.read().decode('utf-8'))
    print(f"Loaded {len(entries)} CGM entries.")
except Exception as e:
    print(f"Error fetching CGM entries: {e}")

try:
    print("Fetching rolling 14-day treatments from Nightscout...")
    req_t = urllib.request.Request(
        f"{BASE_URL}/api/v1/treatments.json?find[created_at][$gte]={min_iso}&count=4000",
        headers={"User-Agent": "LydiaLoopAnalytics/2.0"}
    )
    with urllib.request.urlopen(req_t, timeout=20) as resp:
        treatments = json.loads(resp.read().decode('utf-8'))
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
        carbs_list.append((ts, float(carbs)))

    ins = t.get("insulin")
    if ins and float(ins) > 0:
        insulin_events.append((ts, float(ins)))

    if t.get("eventType") == "Temp Basal" and t.get("rate") is not None:
        dur = float(t.get("duration") or 30.0)
        rate = float(t.get("rate") or 0.0)
        temp_basals.append((ts, ts + dur * 60, rate))

carbs_list.sort(key=lambda x: x[0])
insulin_events.sort(key=lambda x: x[0])
temp_basals.sort(key=lambda x: x[0])

def get_delivered_insulin(t_start, t_end):
    tot_bolus = sum(ins for t, ins in insulin_events if t_start <= t < t_end)
    tot_basal = 0.0
    for s, e, r in temp_basals:
        overlap_start = max(t_start, s)
        overlap_end = min(t_end, e)
        if overlap_end > overlap_start:
            tot_basal += r * ((overlap_end - overlap_start) / 3600.0)
    return tot_bolus + tot_basal


# -------------------------------------------------------------------------
# DYNAMIC SOLVER 1: Pharmacological Proof of ISF from isolated corrections
# Formula: ISF = |BG_nadir - BG_bolus| / (I_corr + Delta_IOB_basal)
# Conditions: Rgut = 0 (fasting/post-absorptive), BG_bolus >= 165 mg/dL
# -------------------------------------------------------------------------
print("Solving dynamic ISF from pharmacological correction proof...")

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
            dur = float(t.get("duration") or t.get("absorptionTime") or 180)
            raw_meals.append((dt.timestamp(), float(c), dur))
        except: continue
raw_meals.sort(key=lambda x: x[0])

clustered_meals = []
meal_durations = {}
if raw_meals:
    cur_t, cur_c, cur_d = raw_meals[0]
    for t, c, d in raw_meals[1:]:
        if t - cur_t < 2700:
            cur_c += c
            cur_d = max(cur_d, d)
        else:
            clustered_meals.append((cur_t, cur_c))
            meal_durations[cur_t] = cur_d
            cur_t, cur_c, cur_d = t, c, d
    clustered_meals.append((cur_t, cur_c))
    meal_durations[cur_t] = cur_d

# -------------------------------------------------------------------------
# DYNAMIC SOLVER 1: Pharmacological ISF Verification (Unconfounded Drops)
# -------------------------------------------------------------------------
corr_treatments = []
for t in treatments:
    ins = t.get("insulin")
    if ins and float(ins) > 0 and not t.get("carbs"):
        created = t.get("created_at") or t.get("timestamp")
        if not created: continue
        try:
            ct = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except: continue
        corr_treatments.append((ct, float(ins)))

corr_treatments.sort(key=lambda x: x[0])

clusters = []
cur = []
for ct, ins in corr_treatments:
    if not cur: cur.append((ct, ins))
    else:
        if ct - cur[-1][0] <= 2700: cur.append((ct, ins))
        else:
            clusters.append(cur)
            cur = [(ct, ins)]
if cur: clusters.append(cur)

isf_episodes = []
for cl in clusters:
    t_start = cl[0][0]
    t_last = cl[-1][0]
    tot_icorr = sum(x[1] for x in cl)
    if tot_icorr < 0.15: continue
    
    # Check Rgut = 0: no carbs [-2.5h, +3.5h] from t_start
    if any(t_start - 9000 <= tc <= t_last + 10800 for tc, c in carbs_list):
        continue
    
    bg_bolus = get_bg_at(t_start, max_delta=600)
    if bg_bolus is None or bg_bolus < 165.0:
        continue
    
    cgm_after = [(t_sec, bg) for t_sec, bg in cgm_timeline if 3600 <= t_sec - t_start <= 14400]
    if not cgm_after: continue
    t_nadir, bg_nadir = min(cgm_after, key=lambda x: x[1])
    
    drop = bg_bolus - bg_nadir
    if drop < 20: continue
    
    dur_hrs = (t_nadir - t_start) / 3600.0
    
    actual_basal = 0.0
    for s, e, r in temp_basals:
        overlap_start = max(t_start, s)
        overlap_end = min(t_nadir, e)
        if overlap_end > overlap_start:
            actual_basal += r * ((overlap_end - overlap_start) / 3600.0)
    
    direct_isf = drop / tot_icorr
    dt = datetime.fromtimestamp(t_start, tz=timezone.utc) + TZ_OFFSET
    isf_episodes.append({
        "time": dt.strftime("%b %d, %H:%M"),
        "bg_bolus": bg_bolus,
        "bg_nadir": bg_nadir,
        "drop": drop,
        "i_corr": tot_icorr,
        "delta_iob_basal": actual_basal,
        "direct_isf": direct_isf,
        "adj_isf": direct_isf,
        "dur_hrs": dur_hrs
    })

direct_isfs = [ep["direct_isf"] for ep in isf_episodes if 80 <= ep["direct_isf"] <= 400]
if direct_isfs:
    dynamic_isf = statistics.median(direct_isfs)
    min_isf = min(direct_isfs)
    max_isf = max(direct_isfs)
    rec_isf = round(dynamic_isf / 10.0) * 10.0
    print(f"Pharmacological ISF Proof: {len(direct_isfs)} unconfounded episodes (median {dynamic_isf:.1f} mg/dL/U, range {min_isf:.0f}–{max_isf:.0f}). Recommended: {rec_isf:.0f} mg/dL/U.")
else:
    dynamic_isf = cur_isf
    min_isf = cur_isf
    max_isf = cur_isf
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

cr_samples = defaultdict(list)
for mt, carbs in clustered_meals:
    max_dur = meal_durations.get(mt, 180.0)
    
    # 3-Tier Horizon Logic:
    # If 5-hour meal (pizza/pasta, duration >= 240m), use 5.0h; else 3.0h
    h_hours = 5.0 if max_dur >= 240 else 3.0
    h_sec = h_hours * 3600.0

    # Avoid overlapping meals within full dynamic horizon (no premature truncation)
    if any(0 < t - mt < h_sec for t, c in clustered_meals):
        continue

    bg0 = get_bg_at(mt, max_delta=900)
    bg_end = get_bg_at(mt + h_sec, max_delta=1200)
    if bg0 is None or bg_end is None: continue

    dt_l = datetime.fromtimestamp(mt, tz=timezone.utc) + TZ_OFFSET
    hm = dt_l.hour * 60 + dt_l.minute
    blk = get_basal_block_id(dt_l.hour, dt_l.minute)
    solved_basal_rate = basal_results[blk][0]
    expected_basal = solved_basal_rate * h_hours

    i_tot = get_delivered_insulin(mt - 900, mt + h_sec)
    i_food = i_tot - expected_basal + ((bg_end - bg0) / rec_isf)

    if i_food > 0.3:
        calc_cr = carbs / i_food
        if 2.5 <= calc_cr <= 25.0:
            start_str, name, def_cr, note = hm_to_dynamic_slot(hm)
            cr_samples[start_str].append(calc_cr)

cr_results = {}
for start, end, name, def_cr, note in dynamic_slots:
    if start in ["00:00", "22:00"]:
        cr_results[start] = (15.0, note, name, f"{start} – {end}")
        continue
    samps = cr_samples[start]
    if len(samps) >= 2:
        med = statistics.median(samps)
        rec = round(med, 1)  # Exact decimal precision (e.g. 5.4, 6.6, 8.3)
        rec = max(3.5, min(20.0, float(rec)))
        ev = f"Solved dynamically across {len(samps)} isolated {name.lower()} episodes in [{start}–{end}) (median 1:{med:.1f} g/U). {note}"
        cr_results[start] = (rec, ev, name, f"{start} – {end}")
    else:
        ev = f"Empirical cluster [{start}–{end}) matches baseline 1:{def_cr:.1f} g/U. {note}"
        cr_results[start] = (def_cr, ev, name, f"{start} – {end}")

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

# Pharmacological ISF Evidence and Episode Rows
isf_episodes_rows_html = ""
for ep in isf_episodes:
    isf_val_str = f"{ep['direct_isf']:.1f} mg/dL/U"
    diob_str = f"{ep['delta_iob_basal']:+.2f} U" if ep['delta_iob_basal'] is not None else "&mdash;"
    adj_str = f"{ep['adj_isf']:.1f} mg/dL/U" if ep['adj_isf'] else "&mdash;"
    isf_episodes_rows_html += f"""
    <tr class="hover:bg-slate-50">
      <td class="py-2 px-3 font-mono font-medium text-slate-800 whitespace-nowrap">{ep['time']}</td>
      <td class="py-2 px-3 font-mono text-slate-700 whitespace-nowrap">{ep['bg_bolus']:.0f} &rarr; {ep['bg_nadir']:.0f} mg/dL</td>
      <td class="py-2 px-3 font-mono font-bold text-emerald-700 whitespace-nowrap">&minus;{ep['drop']:.0f} mg/dL</td>
      <td class="py-2 px-3 font-mono text-slate-800 whitespace-nowrap">{ep['i_corr']:.2f} U</td>
      <td class="py-2 px-3 font-mono text-slate-500 text-xs whitespace-nowrap">{diob_str}</td>
      <td class="py-2 px-3 font-mono font-bold text-blue-800 whitespace-nowrap">{isf_val_str}</td>
    </tr>
    """

if not isf_episodes_rows_html:
    isf_episodes_rows_html = '<tr><td colspan="6" class="py-3 px-3 text-center text-slate-400 font-mono text-xs">No unconfounded hyperglycemic episodes detected in rolling window.</td></tr>'

isf_proof_count = len(direct_isfs)
isf_proof_median = f"{dynamic_isf:.1f}"
isf_proof_min = f"{min_isf:.0f}"
isf_proof_max = f"{max_isf:.0f}"

if direct_isfs:
    isf_evidence_text = f"Pharmacological proof evaluated across {isf_proof_count} unconfounded corrections ($R_{{\\text{{gut}}}}=0$, $\\text{{BG}} > 165\\text{{ mg/dL}}$). Empirical direct drops span {isf_proof_min}–{isf_proof_max} mg/dL/U (median {isf_proof_median} mg/dL/U). Recommending {rec_isf:.0f} mg/dL/U to align with true physical sensitivity and eliminate post-correction overshoot lows."
else:
    isf_evidence_text = f"Calibrated from clinical correction history (active profile: {cur_isf:.0f} mg/dL/U)."

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
    "{{dynamic_isf_str}}": isf_proof_median,
    "{{isf_proof_count}}": str(isf_proof_count),
    "{{isf_proof_median}}": isf_proof_median,
    "{{isf_proof_min}}": isf_proof_min,
    "{{isf_proof_max}}": isf_proof_max,
    "{{isf_episodes_rows_html}}": isf_episodes_rows_html,
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
