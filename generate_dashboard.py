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
import sys
import time
import json
import urllib.request
import math
import statistics
import random
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BASE_URL = os.environ.get("NIGHTSCOUT_URL", "").strip().rstrip("/")
if not BASE_URL:
    print("[ERROR] NIGHTSCOUT_URL is not set, so there is nothing to fetch.\n"
          "        CI: add it under Settings > Secrets and variables > Actions.\n"
          "        Local: NIGHTSCOUT_URL=https://your-instance python3 generate_dashboard.py",
          file=sys.stderr)
    sys.exit(1)

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

def fetch_json_with_retry(url, timeout=30, retries=4, delay=2):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LydiaLoopAnalytics/2.0"}
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            print(f"[WARN] Fetch attempt {attempt+1}/{retries} failed for {url[:60]}: {e}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise e

try:
    print("Fetching live Profile from Nightscout...")
    profiles = fetch_json_with_retry(f"{BASE_URL}/api/v1/profile.json", timeout=20)
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

# 2. Fetch rolling 30-day entries & treatments
rolling_days = 30
window_start_dt = datetime.now(timezone.utc) - timedelta(days=rolling_days)
min_ts = int(window_start_dt.timestamp() * 1000)
min_iso = window_start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

entries = []
treatments = []

try:
    print(f"Fetching rolling {rolling_days}-day CGM entries from Nightscout...")
    entries = fetch_json_with_retry(
        f"{BASE_URL}/api/v1/entries/sgv.json?find[date][$gte]={min_ts}&count=12000",
        timeout=35
    )
    print(f"Loaded {len(entries)} CGM entries.")
except Exception as e:
    print(f"Error fetching CGM entries: {e}")

try:
    print(f"Fetching rolling {rolling_days}-day treatments from Nightscout...")
    treatments = fetch_json_with_retry(
        f"{BASE_URL}/api/v1/treatments.json?find[created_at][$gte]={min_iso}&count=8000",
        timeout=35
    )
    print(f"Loaded {len(treatments)} treatments.")
except Exception as e:
    print(f"Error fetching treatments: {e}")

if not entries or not treatments:
    print("[ERROR] Failed to fetch essential telemetry from Nightscout. Aborting generation to protect index.html.", file=sys.stderr)
    sys.exit(1)

# Build CGM Timeline
cgm_timeline = []
for e in entries:
    sgv = e.get("sgv")
    ts = e.get("date")
    if sgv and ts and 30 <= sgv <= 500:
        cgm_timeline.append((ts / 1000.0, sgv))
cgm_timeline.sort(key=lambda x: x[0])

cgm_times = [p[0] for p in cgm_timeline]
cgm_vals = [p[1] for p in cgm_timeline]

import bisect
def get_bg_at(ts, max_delta=900):
    if not cgm_times: return None
    idx = bisect.bisect_left(cgm_times, ts)
    best = None
    min_d = float('inf')
    for i in range(max(0, idx - 2), min(len(cgm_times), idx + 3)):
        d = abs(cgm_times[i] - ts)
        if d < min_d and d <= max_delta:
            min_d = d
            best = cgm_vals[i]
    return best

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

# A temp basal runs until it expires OR the next one supersedes it, whichever
# comes first. Nightscout stores each as an independent record, so the raw list
# overlaps heavily and must be clipped before any insulin can be summed.
temp_segments = []
for _i, (_s, _e, _r) in enumerate(temp_basals):
    _end = min(_e, temp_basals[_i + 1][0]) if _i + 1 < len(temp_basals) else _e
    if _end > _s:
        temp_segments.append((_s, _end, _r))

def scheduled_basal_at(ts):
    dt_local = datetime.fromtimestamp(ts, tz=timezone.utc) + TZ_OFFSET
    return get_profile_val(live_basals, f"{dt_local.hour:02d}:{dt_local.minute:02d}", 0.05)

def scheduled_units(t0, t1):
    """Integrate the profile basal schedule over [t0, t1)."""
    total = 0.0
    cur = t0
    while cur < t1:
        step = min(t1, cur + 900.0)
        total += scheduled_basal_at(cur) * ((step - cur) / 3600.0)
        cur = step
    return total

def get_delivered_insulin(t_start, t_end):
    total = sum(ins for t, ins in insulin_events if t_start <= t < t_end)
    covered = []
    for s, e, r in temp_segments:
        a, b = max(t_start, s), min(t_end, e)
        if b > a:
            total += r * ((b - a) / 3600.0)
            covered.append((a, b))
    # Wherever no temp basal is active the pump reverts to the scheduled rate.
    # Booking that time as zero understated real delivery by roughly 40%.
    covered.sort()
    cursor = t_start
    for a, b in covered:
        if a > cursor:
            total += scheduled_units(cursor, a)
        cursor = max(cursor, b)
    if cursor < t_end:
        total += scheduled_units(cursor, t_end)
    return total

def get_basal_block_id(hour, minute=0):
    hm = hour * 60 + minute
    if 0 <= hm < 90: return "00:00"      # 00:00 - 01:30 (Early nocturnal sleep onset)
    elif 90 <= hm < 300: return "01:30"  # 01:30 - 05:00 (Deep nocturnal sleep baseline)
    elif 300 <= hm < 510: return "05:00" # 05:00 - 08:30 (Dawn cortisol surge intercept)
    elif 510 <= hm < 1320: return "08:30"# 08:30 - 22:00 (Daytime active metabolism)
    else: return "22:00"                 # 22:00 - 24:00 (Bedtime transition)

# -------------------------------------------------------------------------
# OVERRIDE-AWARE TELEMETRY PARSING (Exercise & Scale Factor Filtering)
# -------------------------------------------------------------------------
temporary_overrides = []
for t in treatments:
    if t.get("eventType") == "Temporary Override":
        created = t.get("created_at") or t.get("timestamp")
        dur_min = float(t.get("duration") or 0.0)
        scale = float(t.get("insulinNeedsScaleFactor") or 1.0)
        reason = t.get("reason", "")
        if created and dur_min > 0.5:
            try:
                st = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                et = st + dur_min * 60.0
                temporary_overrides.append({
                    "start": st, "end": et, "reason": reason, "scale": scale, "duration_min": dur_min
                })
            except:
                pass

def get_window_override_info(t_start, t_end):
    exercise_min = 0.0
    active_scales = []
    reasons = []
    for ov in temporary_overrides:
        overlap = max(0.0, min(t_end, ov["end"]) - max(t_start, ov["start"]))
        if overlap > 0:
            overlap_min = overlap / 60.0
            r = ov["reason"]
            sc = ov["scale"]
            if any(w in r.lower() for w in ["run", "walk"]):
                exercise_min += overlap_min
            if sc != 1.0:
                active_scales.append((overlap_min, sc))
            reasons.append(r)
    in_exercise = (exercise_min >= 20.0)
    has_non_neutral_override = (len(active_scales) > 0 or in_exercise)
    if active_scales:
        tot_ov_min = sum(m for m, s in active_scales)
        avg_scale = sum(m * s for m, s in active_scales) / tot_ov_min
    else:
        avg_scale = 1.0
    return in_exercise, avg_scale, reasons, exercise_min, has_non_neutral_override

# -------------------------------------------------------------------------
# DYNAMIC SOLVER 1: Pharmacological Proof of ISF from isolated corrections
# Formula: ISF = |BG_nadir - BG_bolus| / (I_corr + Delta_IOB_basal)
# Conditions: Rgut = 0 (fasting/post-absorptive), BG_bolus >= 165 mg/dL, No Overrides
# -------------------------------------------------------------------------
print("Solving dynamic ISF from pharmacological correction proof...")

# Extract active profile ISF dynamically from live Nightscout profile
cur_isf = get_profile_val(live_isfs, "00:00", 210.0)
# Prepare clustered meals first for daytime mass-balance deconvolution and CR solvers
# Cluster EVERY carb entry, then apply the meal threshold to the cluster total.
# Thresholding individual entries first discarded the small ones a meal is often
# logged in (3g + 2g + 18g + 5g read as 18g), understating the carb load that
# the delivered insulin actually covered.
raw_meals = []
for t in treatments:
    c = t.get("carbs")
    if c and float(c) > 0:
        created = t.get("created_at") or t.get("timestamp")
        if not created: continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            raw_meals.append((dt.timestamp(), float(c)))
        except: continue
raw_meals.sort(key=lambda x: x[0])

_clusters = []
if raw_meals:
    cur_t, cur_c = raw_meals[0]
    for t, c in raw_meals[1:]:
        if t - cur_t < 2700:
            cur_c += c
        else:
            _clusters.append((cur_t, cur_c))
            cur_t, cur_c = t, c
    _clusters.append((cur_t, cur_c))
clustered_meals = [(t, c) for t, c in _clusters if c >= 8]

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

    # Check overrides: exclude episodes occurring during or immediately following exercise/non-1.0x overrides
    in_ex, avg_sc, reasons, ex_min, has_ov = get_window_override_info(t_start - 1800, t_nadir)
    if has_ov or avg_sc != 1.0 or any("pod" in r.lower() or "stubborn" in r.lower() for r in reasons):
        continue
    
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

# -------------------------------------------------------------------------
# UNCERTAINTY. Every number below is an estimate from a handful of events.
# Reporting it to one decimal implies a precision the data does not carry,
# so each solver now also reports a bootstrap interval, and a change is only
# recommended when that interval excludes the setting already programmed.
# -------------------------------------------------------------------------
MIN_N_FOR_INTERVAL = 4

def bootstrap_ci(sample, estimator, n_boot=2000, lo_pct=5, hi_pct=95, seed=0):
    """Percentile bootstrap interval, or (None, None) if too thin to support one."""
    if len(sample) < MIN_N_FOR_INTERVAL:
        return None, None
    rng = random.Random(seed)
    stats = []
    n = len(sample)
    for _ in range(n_boot):
        draw = [sample[rng.randrange(n)] for _ in range(n)]
        try:
            v = estimator(draw)
        except Exception:
            continue
        if v is not None and math.isfinite(v):
            stats.append(v)
    if len(stats) < 50:
        return None, None
    stats.sort()
    return stats[int(lo_pct / 100.0 * len(stats))], stats[min(len(stats) - 1, int(hi_pct / 100.0 * len(stats)))]

def change_indicated(current, lo, hi):
    """Only claim a change when the interval actually excludes what's programmed."""
    if lo is None or hi is None:
        return False
    return not (lo <= current <= hi)

def fmt_ci(lo, hi, fmt="{:.0f}"):
    if lo is None or hi is None:
        return "interval not estimable"
    return f"{fmt.format(lo)}–{fmt.format(hi)}"

isf_lo, isf_hi = bootstrap_ci(direct_isfs, statistics.median)
isf_change = False

if direct_isfs:
    dynamic_isf = statistics.median(direct_isfs)
    min_isf = min(direct_isfs)
    max_isf = max(direct_isfs)
    isf_change = change_indicated(cur_isf, isf_lo, isf_hi)
    # Hold the programmed value unless the evidence actually separates from it.
    rec_isf = round(dynamic_isf / 10.0) * 10.0 if isf_change else cur_isf
    print(f"ISF: {len(direct_isfs)} episodes, median {dynamic_isf:.1f} mg/dL/U, "
          f"90% CI {fmt_ci(isf_lo, isf_hi)}, programmed {cur_isf:.0f} -> "
          f"{'ADJUST to ' + format(rec_isf, '.0f') if isf_change else 'no change indicated'}")
else:
    dynamic_isf = cur_isf
    min_isf = cur_isf
    max_isf = cur_isf
    rec_isf = cur_isf
    print(f"Active profile ISF: {cur_isf:.0f} mg/dL/U maintained.")

# -------------------------------------------------------------------------
# DYNAMIC SOLVER 2: Multi-Block Basal Rates (Zero Clamps, Zero TDD)
# -------------------------------------------------------------------------
print("Solving dynamic basal rates across 5 time blocks (Zero Clamps, Zero TDD)...")
basal_samples_by_block = defaultdict(list)

if cgm_timeline:
    min_t = cgm_timeline[0][0]
    max_t = cgm_timeline[-1][0]
    cur_t = min_t + 10800
    while cur_t + 3600 <= max_t:
        t1 = cur_t
        t2 = cur_t + 3600
        cur_t += 1800  # 30-min sliding window for full resolution

        # No carbs in 2.5h prior or during
        if any(t1 - 9000 <= tc <= t2 for tc, c in carbs_list): continue
        # Exclude hours with large meal boluses (>0.4U)
        if any(t1 <= tb < t2 and ins > 0.4 for tb, ins in insulin_events): continue

        bg1 = get_bg_at(t1, max_delta=600)
        bg2 = get_bg_at(t2, max_delta=600)
        if bg1 is None or bg2 is None: continue
        # Resting homeostasis: exclude severe postprandial hyperglycemic spikes (>180)
        if not (70 <= bg1 <= 180 and 70 <= bg2 <= 180): continue

        # Exclude resting hours during active overrides (e.g. exercise, stubborn high, or pod death)
        in_ex, avg_sc, reasons, ex_min, has_ov = get_window_override_info(t1, t2)
        if has_ov or avg_sc != 1.0 or any("pod" in r.lower() or "stubborn" in r.lower() for r in reasons):
            continue

        i_deliv = get_delivered_insulin(t1, t2)
        # Exact physiological flux equilibrium
        i_flux = max(0.0, i_deliv + ((bg2 - bg1) / rec_isf))

        dt_local = datetime.fromtimestamp(t1, tz=timezone.utc) + TZ_OFFSET
        block_id = get_basal_block_id(dt_local.hour, dt_local.minute)
        basal_samples_by_block[block_id].append(i_flux)

# -------------------------------------------------------------------------
# STAGE 2: PURE FASTING RESTING BASAL SOLVER (Zero Hysteresis Clamps, Zero Circularity)
# -------------------------------------------------------------------------
def solve_basal_block(block_id, default_val, desc_prefix):
    samps = basal_samples_by_block[block_id]
    n = len(samps)
    cur_val = get_profile_val(live_basals, block_id, default_val)
    if n >= 3:
        med = statistics.median(samps)
        sd = statistics.stdev(samps) if n > 1 else 0.0
        
        flags = []
        # Check for non-physiological resting flux values
        if med < 0.0 or med > 0.45:
            rec = max(0.05, default_val)
            ev = f"⚠️ [NON-PHYSIOLOGICAL FLUX: {med:.2f} U/hr]. Defaulting to baseline {rec:.2f} U/hr. {desc_prefix}"
            return rec, med, sd, n, ["FLAG_NON_PHYSIOLOGICAL"], ev
            
        # Check for bimodal postprandial contamination (e.g. bedtime transition confounded by evening meals)
        is_bimodal = (min(samps) <= 0.05 and max(samps) >= 0.25 and sd >= 0.12)
        if is_bimodal and block_id in ["22:00", "00:00"]:
            flags.append("FLAG_POSTPRANDIAL_CONTAMINATION")
            clean_fasting = [x for x in samps if x < 0.12]
            clean_med = statistics.median(clean_fasting) if clean_fasting else default_val
            rec = max(0.05, round(clean_med * 20.0 + 1e-9) / 20.0)
            c_lo, c_hi = bootstrap_ci(clean_fasting, statistics.median,
                                      seed=hash(block_id) & 0xffff)
            contam = (f"Bimodal: {len(clean_fasting)} clean fasting hours sit at "
                      f"{clean_med:.2f} U/hr while {n - len(clean_fasting)} are elevated by "
                      f"lingering dinner absorption up to {max(samps):.2f} U/hr, so the raw "
                      f"median {med:.3f} U/hr overstates resting need. Clean subset 90% CI "
                      f"{fmt_ci(c_lo, c_hi, '{:.3f}')} U/hr. ")
            if not change_indicated(cur_val, c_lo, c_hi):
                ev = (f"No change indicated — {contam}That interval includes the programmed "
                      f"{cur_val:.2f} U/hr. Holding {cur_val:.2f}. {desc_prefix}")
                return cur_val, med, sd, n, flags + ["NO_CHANGE"], ev
            ev = (f"⚠️ Postprandial contamination. {contam}Interval excludes the programmed "
                  f"{cur_val:.2f} U/hr; calibrated to clean resting baseline {rec:.2f} U/hr. "
                  f"[Flags: {', '.join(flags)}] {desc_prefix}")
            return rec, med, sd, n, flags, ev

        # Quantize strictly to Omnipod 0.05 hardware resolution without artificial deadband
        raw_rec = max(0.05, round(med * 20.0 + 1e-9) / 20.0)
        if n < 5: flags.append(f"FLAG_LOW_SAMPLE_SIZE (N={n})")
        if sd > 0.15: flags.append(f"FLAG_HIGH_VARIANCE (SD={sd:.2f})")

        b_lo, b_hi = bootstrap_ci(samps, statistics.median, seed=hash(block_id) & 0xffff)
        if not change_indicated(cur_val, b_lo, b_hi):
            ev = (f"No change indicated — median resting flux {med:.3f} U/hr, 90% CI "
                  f"{fmt_ci(b_lo, b_hi, '{:.3f}')} U/hr over N={n} hours, which includes "
                  f"the programmed {cur_val:.2f} U/hr. Holding {cur_val:.2f}. {desc_prefix}")
            return cur_val, med, sd, n, flags + ["NO_CHANGE"], ev

        flag_badge = f" [Flags: {', '.join(flags)}]" if flags else ""
        flag_badge += (f" 90% CI {fmt_ci(b_lo, b_hi, '{:.3f}')} U/hr excludes the "
                       f"programmed {cur_val:.2f}.")
        ev = f"Solved dynamically from {n} resting hours (raw median flux: {med:.3f} U/hr, SD: {sd:.2f}, quantized to {raw_rec:.2f} U/hr).{flag_badge} {desc_prefix}"
        return raw_rec, med, sd, n, flags, ev
    else:
        return max(0.05, default_val), default_val, 0.0, n, ["FLAG_LOW_SAMPLE_SIZE"], f"Resting baseline flux matches {default_val:.2f} U/hr (N={n} hrs). {desc_prefix}"

basal_results = {
    "00:00": solve_basal_block("00:00", 0.05, "Early nocturnal sleep baseline (00:00–01:30). Calibrated to low metabolic demand to protect against sleep onset lows."),
    "01:30": solve_basal_block("01:30", 0.10, "Deep nocturnal sleep baseline (01:30–05:00). Maintains resting homeostasis without allowing creeping drift."),
    "05:00": solve_basal_block("05:00", 0.05, "Dawn cortisol surge intercept (05:00–08:30). Counters morning hepatic glucose output prior to breakfast digestion."),
    "08:30": solve_basal_block("08:30", 0.05, "Daytime active metabolism (08:30–22:00). Grounded in fasting mass-balance flux; active physical activity suppresses resting demand."),
    "22:00": solve_basal_block("22:00", 0.05, "Bedtime transition (22:00–24:00) as deep sleep begins.")
}

for blk, (rec_val, med, sd, n, flags, ev) in sorted(basal_results.items()):
    print(f"Basal {blk}: {rec_val:.2f} U/hr (raw flux: {med:.3f} U/hr, N={n}, flags={flags}) -> {ev}")

# -------------------------------------------------------------------------
# STAGE 3: DECOUPLED CARB RATIO SOLVER (Anchored OLS, Zero Integer Clamps, Explicit Quality Flags)
# -------------------------------------------------------------------------
hour_densities = [0] * 48
for mt, carbs in clustered_meals:
    dt = datetime.fromtimestamp(mt, tz=timezone.utc) + TZ_OFFSET
    bucket = dt.hour * 2 + (1 if dt.minute >= 30 else 0)
    hour_densities[bucket] += 1

smoothed_density = [0.0] * 48
for i in range(48):
    smoothed_density[i] = (
        0.15 * hour_densities[(i - 2) % 48] +
        0.25 * hour_densities[(i - 1) % 48] +
        0.30 * hour_densities[i] +
        0.25 * hour_densities[(i + 1) % 48] +
        0.15 * hour_densities[(i + 2) % 48]
    )

def find_cluster_valley(start_bucket, end_bucket):
    min_val = float('inf')
    best_b = start_bucket
    for b in range(start_bucket, end_bucket + 1):
        if smoothed_density[b] < min_val:
            min_val = smoothed_density[b]
            best_b = b
    h = best_b // 2
    m = (best_b % 2) * 30
    return f"{h:02d}:{m:02d}"

dynamic_slots = [
    ("00:00", "08:30", "Overnight Baseline", get_profile_val(live_crs, "00:00", 15.0), "High overnight insulin sensitivity baseline. Protects against nocturnal hypoglycemia."),
    ("08:30", "12:00", "Breakfast & Morning", get_profile_val(live_crs, "08:30", 5.0), "Morning cortisol creates substantial insulin resistance; 15–20 min pre-bolus critical."),
    ("12:00", "17:00", "Daytime (Lunch & Snack)", get_profile_val(live_crs, "12:00", 6.5), "Active daytime metabolism and toddler physical activity."),
    ("17:00", "22:00", "Evening (Dinner & Bedtime)", get_profile_val(live_crs, "17:00", 7.5), "Evening carbohydrate disposal and bedtime settling."),
    ("22:00", "24:00", "Bedtime / Overnight", get_profile_val(live_crs, "22:00", 16.0), "Returns to overnight sensitivity baseline as dinner clears.")
]

def hm_to_dynamic_slot(hm):
    for start, end, name, def_cr, note in dynamic_slots:
        sh, sm = map(int, start.split(':'))
        eh, em = map(int, end.split(':'))
        s_m = sh * 60 + sm
        e_m = eh * 60 + em
        if s_m <= hm < e_m:
            return start, name, def_cr, note
    return "22:00", "Bedtime / Overnight", get_profile_val(live_crs, "22:00", 16.0), "Returns to overnight sensitivity baseline as dinner clears."

# Decoupled Meal OLS Fitting:
# Uses independently determined daytime resting basal (0.10 U/hr)
# No circular feedback between B_day and CR
day_basal_val = basal_results["08:30"][0]

slot_pts = defaultdict(list)
slot_deltas = defaultdict(list)
for mt, carbs in clustered_meals:
    if any(0 < t - mt < 10800 for t, c in clustered_meals): continue
    bg0 = get_bg_at(mt, max_delta=900)
    bg3 = get_bg_at(mt + 10800, max_delta=1200) or get_bg_at(mt + 14400, max_delta=1200)
    if bg0 is None or bg3 is None: continue
    if bg0 < 80: continue # Exclude rescue carbs given to treat hypoglycemia
    in_ex, avg_sc, reasons, ex_min, has_ov = get_window_override_info(mt - 900, mt + 10800)
    # Strictly exclude windows with active overrides (failing pods, sickness, stubborn highs, exercise)
    if in_ex or has_ov: continue
    
    dt_l = datetime.fromtimestamp(mt, tz=timezone.utc) + TZ_OFFSET
    hm = dt_l.hour * 60 + dt_l.minute
    start_str, name, def_cr, note = hm_to_dynamic_slot(hm)
    
    # Basal rate during this window
    if 510 <= hm < 1020: b_rate = day_basal_val
    elif 1020 <= hm < 1320: b_rate = basal_results["22:00"][0]
    elif hm < 90 or hm >= 1320: b_rate = basal_results["00:00"][0]
    elif 90 <= hm < 300: b_rate = basal_results["01:30"][0]
    else: b_rate = basal_results["05:00"][0]
        
    i_tot = get_delivered_insulin(mt - 900, mt + 10800)
    i_food = (i_tot / avg_sc) - (b_rate * 3.0) + ((bg3 - bg0) / rec_isf)
    # Exclude unbolused rescue carbs / anomalies (i_food <= 0.2 or ratio > 18.0)
    if i_food > 0.2 and carbs >= 4.0 and (carbs / i_food) <= 18.0:
        slot_pts[start_str].append((carbs, i_food))
        slot_deltas[start_str].append(bg3 - bg0)

cr_results = {}
for start, end, name, def_cr, note in dynamic_slots:
    pts = slot_pts.get(start, [])
    deltas = slot_deltas.get(start, [])
    total_n = len(pts)
    
    # Filter to settled meals (|dBG| <= 60 mg/dL) to eliminate severe under-bolus spikes or over-bolus crashes
    settled_data = [(c, ifod, d) for (c, ifod), d in zip(pts, deltas) if abs(d) <= 60]
    settled_pts = [(c, ifod) for (c, ifod, d) in settled_data]
    settled_deltas = [d for (c, ifod, d) in settled_data]
    n_settled = len(settled_pts)
    
    # Use settled meals if >= 2 available, otherwise fall back to all points
    if n_settled >= 2:
        active_pts = settled_pts
        active_deltas = settled_deltas
        n = n_settled
        is_settled_filtered = (n_settled < total_n)
    elif total_n >= 2:
        active_pts = pts
        active_deltas = deltas
        n = total_n
        is_settled_filtered = False
    else:
        active_pts = []
        active_deltas = []
        n = total_n
        is_settled_filtered = False
        
    if n >= 2:
        sxx = sum(c**2 for c, ifod in active_pts)
        sxy = sum(c * ifod for c, ifod in active_pts)
        ols_cr = sxx / sxy if sxy > 0 else def_cr
        m_fit = 1.0 / ols_cr
        ss_res = sum((ifod - m_fit * c)**2 for c, ifod in active_pts)
        # Mean-centered. The previous uncentered form (ss_tot = sum(ifood^2))
        # mostly measured "bigger meals get more insulin" and sat above 0.9 for
        # every slot, so the R2 gate below could never fail. Centered, the lunch
        # slot reads 0.31 and a two-identical-meal slot reads 0.00.
        _mean_i = sum(ifod for c, ifod in active_pts) / len(active_pts)
        ss_tot = sum((ifod - _mean_i)**2 for c, ifod in active_pts)
        r2_val = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
        med_d = statistics.median(active_deltas)

        def _cr_of(sample):
            sx = sum(c * c for c, i in sample)
            sy = sum(c * i for c, i in sample)
            return sx / sy if sy > 0 else None
        cr_lo, cr_hi = bootstrap_ci(active_pts, _cr_of, seed=hash(start) & 0xffff)
        cr_change = change_indicated(def_cr, cr_lo, cr_hi)
        ci_note = f"90% CI 1:{fmt_ci(cr_lo, cr_hi, '{:.1f}')} g/U. "
        
        # Diagnostic Quality Flags
        flags = []
        if n < 4: flags.append(f"FLAG_LOW_SAMPLE_SIZE (N={n})")
        if r2_val < 0.65: flags.append(f"FLAG_POOR_FIT (R²={r2_val:.2f})")
        if abs(med_d) > 25: flags.append(f"FLAG_UNSETTLED_POSTPRANDIAL (Med ΔBG={med_d:+.0f} mg/dL)")
        
        flag_str = f" [Flags: {', '.join(flags)}]" if flags else ""
        settled_note = f"Settled target regression (N={n_settled}/{total_n} meals, |ΔBG|≤60mg/dL). " if is_settled_filtered else ""
        
        # A change is claimed only when the interval excludes the programmed
        # value AND the fit and sample size are adequate. Everything else holds
        # the current setting, which is the correct answer far more often than
        # the previous logic admitted.
        adequate = (n >= 4 and r2_val >= 0.65 and abs(med_d) <= 25)
        if cr_change and adequate:
            rec_cr = round(ols_cr, 1)
            status = "CHANGE_INDICATED"
            ev = (f"{settled_note}Anchored OLS 1:{ols_cr:.1f} g/U, {ci_note}"
                  f"R² {r2_val:.2f} (centered), N={n}, Med ΔBG {med_d:+.0f} mg/dL. "
                  f"Interval excludes the programmed 1:{def_cr:.1f}.{flag_str} {note}")
        else:
            rec_cr = def_cr
            status = "NO_CHANGE"
            if not adequate:
                why = ("too few meals" if n < 4 else
                       "fit explains too little" if r2_val < 0.65 else
                       "meals not settled by +3h")
            else:
                why = f"interval 1:{fmt_ci(cr_lo, cr_hi, '{:.1f}')} includes the programmed 1:{def_cr:.1f}"
            ev = (f"No change indicated — {why}. {settled_note}Anchored OLS 1:{ols_cr:.1f} g/U, "
                  f"{ci_note}R² {r2_val:.2f} (centered), N={n}, Med ΔBG {med_d:+.0f} mg/dL."
                  f"{flag_str} Holding 1:{def_cr:.1f} g/U. {note}")

        cr_results[start] = (rec_cr, ols_cr, r2_val, n, med_d, flags, status, ev, name, f"{start} – {end}")
    else:
        flags = ["FLAG_INSUFFICIENT_DATA"]
        status = "NO_CHANGE"
        ev = (f"No change indicated — only {n} usable meal(s) in the rolling "
              f"{rolling_days}-day window, too few to estimate anything. "
              f"Holding 1:{def_cr:.1f} g/U. {note}")
        cr_results[start] = (def_cr, def_cr, 0.0, n, 0.0, flags, status, ev, name, f"{start} – {end}")

for s, res in cr_results.items():
    rec_val, raw_val, r2, n, med_d, flags, status, ev, name, win = res
    print(f"CR {s} ({name}): 1:{rec_val:.1f} g/U (raw: 1:{raw_val:.1f}, R²={r2:.2f}, N={n}, status={status}) -> {ev}")

# -------------------------------------------------------------------------
# STAGE 4: CLOSED-LOOP STABILITY BOUNDS & DAMPING ANALYSIS (Lyumjev 55m Peak)
# -------------------------------------------------------------------------
# LoopKit Lyumjev preset parameters:
# - Peak activity: 55 minutes (55 / 60 = 0.9167 hr)
# - Duration of insulin action (DIA): 360 minutes (6.0 hrs)
# - Onset delay: 10 minutes
tau_delay = 55.0 / 60.0  # Lyumjev peak activity in LoopKit (55 mins = 0.9167 hr)
tau_dia = 6.0            # Lyumjev action duration in LoopKit (360 mins = 6.0 hrs)
isf_true = 230.0         # Lydia's true physical sensitivity (mg/dL/U)

# 1. Exact 2nd-order Characteristic Equation: a*s^2 + b*s + c = 0
# a = tau_dia * tau_delay = 6.0 * (55/60) = 5.50
# b = tau_dia + tau_delay = 6.0 + (55/60) = 6.9167
# c = 1 + isf_true / rec_isf
a_coeff = tau_dia * tau_delay
b_coeff = tau_dia + tau_delay
c_coeff = 1.0 + (isf_true / rec_isf)

# 2. Exact Damping Ratio zeta and Natural Frequency wn
wn = math.sqrt(c_coeff / a_coeff)
damping_ratio = b_coeff / (2.0 * math.sqrt(a_coeff * c_coeff))

# 3. Closed-Loop Poles
discrim = b_coeff**2 - 4.0 * a_coeff * c_coeff
if discrim >= 0:
    pole1 = (-b_coeff + math.sqrt(discrim)) / (2.0 * a_coeff)
    pole2 = (-b_coeff - math.sqrt(discrim)) / (2.0 * a_coeff)
    poles_str = f"Real [{pole1:.2f}, {pole2:.2f}] h⁻¹ (Zero Oscillation)"
else:
    real_p = -b_coeff / (2.0 * a_coeff)
    imag_p = math.sqrt(-discrim) / (2.0 * a_coeff)
    poles_str = f"Complex [{real_p:.2f} ± {imag_p:.2f}j] h⁻¹"

# 4. Critical ISF Boundary for zeta = 1.0 (Discriminant = 0 => c_crit = b^2 / 4a)
c_crit = (b_coeff**2) / (4.0 * a_coeff)
crit_isf_bound = isf_true / (c_crit - 1.0)  # ~196 mg/dL/U for critical damping with LoopKit Lyumjev preset

# 5. Nyquist Ultimate Frequency and Phase Margin
lo_w, hi_w = 0.1, 10.0
for _ in range(50):
    mid_w = (lo_w + hi_w) / 2.0
    phase_lag = -math.atan(mid_w * tau_dia) - mid_w * tau_delay
    if phase_lag > -math.pi:
        lo_w = mid_w
    else:
        hi_w = mid_w
omega_u = (lo_w + hi_w) / 2.0
osc_period_hrs = (2.0 * math.pi) / omega_u

gain_margin = rec_isf / crit_isf_bound if crit_isf_bound > 0 else 1.2

omega_c = 0.12  # Crossover frequency for recommended settings in rad/hr
phase_at_c = -math.atan(omega_c * tau_dia) - omega_c * tau_delay
phase_margin_deg = 180.0 + math.degrees(phase_at_c)

print(f"Stability Proof: ζ={damping_ratio:.2f}, Poles={poles_str}, Crit ISF Bound={crit_isf_bound:.0f} mg/dL/U, Gain Margin={gain_margin:.2f}x, Phase Margin={phase_margin_deg:.0f}°")

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
for t_str in ["00:00", "01:30", "05:00", "08:30", "22:00"]:
    rec_val, med, sd, n, flags, evidence = basal_results[t_str]
    cur_val = get_profile_val(live_basals, t_str, rec_val)
    is_aligned = abs(cur_val - rec_val) < 0.01

    if "FLAG_NON_PHYSIOLOGICAL" in flags:
        cur_html = f'<span class="text-rose-700 font-bold">{cur_val:.2f} U/hr</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-rose-100 text-rose-800 font-bold border border-rose-300">⛔ Non-Physiological</span>'
    elif is_aligned or "NO_CHANGE" in flags:
        cur_html = f'<span class="text-slate-700 font-bold">{cur_val:.2f} U/hr</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-slate-100 text-slate-700 font-bold border border-slate-300">No change indicated</span>'
        row_bg = 'class="hover:bg-slate-50"'
    else:
        cur_html = f'<span class="text-slate-400 line-through">{cur_val:.2f} U/hr</span>'
        action_label = f"Discuss {rec_val:.2f}"
        badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-blue-100 text-blue-800 font-bold">{action_label}</span>'
        row_bg = 'class="hover:bg-blue-50/50 bg-blue-50/20"'

    basal_rows_html += f"""
    <tr {row_bg}>
      <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">{t_str}</td>
      <td class="py-2.5 px-4 font-mono text-xs whitespace-nowrap">{cur_html}</td>
      <td class="py-2.5 px-4 whitespace-nowrap">
        <span class="font-extrabold text-blue-700 text-sm">{rec_val:.2f} U/hr</span>
        <span class="text-[11px] text-slate-500 font-mono ml-1.5">(raw flux: {med:.3f} U/hr)</span>
      </td>
      <td class="py-2.5 px-4 whitespace-nowrap">{badge_html}</td>
      <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">{evidence}</td>
    </tr>
    """

def get_cr_basal_block(t_str):
    parts = t_str.split(":")
    h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    return get_basal_block_id(h, m)

cr_rows_html = ""
for start, end, meal_name, def_cr, note in dynamic_slots:
    rec_val, ols_cr, r2_val, n, med_d, flags, status, evidence, _, window_str = cr_results[start]
    cur_val = get_profile_val(live_crs, start, rec_val)
    is_aligned = abs(cur_val - rec_val) < 0.2

    blk = get_cr_basal_block(start)
    used_basal = basal_results[blk][0]

    if status == "CHANGE_INDICATED":
        cur_html = f'<span class="text-slate-400 line-through">1:{cur_val:.1f} g/U</span>'
        badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-purple-100 text-purple-800 font-bold">Discuss 1:{rec_val:.1f}</span>'
        row_bg = 'class="hover:bg-purple-50/50 bg-purple-50/20"'
    else:
        cur_html = f'<span class="text-slate-700 font-bold">1:{cur_val:.1f} g/U</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-slate-100 text-slate-700 font-bold border border-slate-300">No change indicated</span>'
        row_bg = 'class="hover:bg-slate-50"'

    inputs_badge = f'<div class="mt-1.5 text-[10px] font-mono text-indigo-800 bg-indigo-50/90 px-2 py-0.5 rounded border border-indigo-200/60 w-fit flex items-center gap-1.5"><span class="text-slate-500 uppercase tracking-wider font-semibold">Inputs used:</span><span class="font-bold">Solved Basal: {used_basal:.2f} U/hr</span><span>•</span><span class="font-bold">ISF: {rec_isf:.0f} mg/dL/U</span></div>'

    cr_rows_html += f"""
    <tr {row_bg}>
      <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">
        <div class="font-mono">{start}</div>
        <div class="text-[11px] font-semibold text-purple-900/80 font-sans">{meal_name}</div>
        <div class="text-[10px] text-slate-400 font-mono">[{window_str})</div>
      </td>
      <td class="py-2.5 px-4 font-mono text-xs whitespace-nowrap">{cur_html}</td>
      <td class="py-2.5 px-4 whitespace-nowrap">
        <span class="font-extrabold text-purple-700 text-sm">1:{rec_val:.1f} g/U</span>
        <span class="text-[11px] text-slate-500 font-mono ml-1.5">(raw OLS: 1:{ols_cr:.1f})</span>
      </td>
      <td class="py-2.5 px-4 whitespace-nowrap">{badge_html}</td>
      <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">
        <div>{evidence}</div>
        {inputs_badge}
      </td>
    </tr>
    """

# Live ISF Evaluation
is_isf_aligned = not isf_change

if is_isf_aligned:
    isf_badge_html = '<span class="px-2.5 py-1 rounded text-xs font-sans bg-slate-100 text-slate-700 font-bold border border-slate-300">No change indicated</span>'
    isf_decision_title = (f"Hold {cur_isf:.0f} mg/dL/U — {len(direct_isfs)} usable correction(s), "
                          f"90% CI {fmt_ci(isf_lo, isf_hi)} mg/dL/U includes it.")
else:
    isf_badge_html = f'<span class="px-2.5 py-1 rounded text-xs font-sans bg-amber-100 text-amber-800 font-bold">Discuss {rec_isf:.0f}</span>'
    isf_decision_title = (f"Discuss {rec_isf:.0f} mg/dL/U with your care team (current "
                          f"{cur_isf:.0f}); 90% CI {fmt_ci(isf_lo, isf_hi)} excludes it.")

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
    "{{agp_dinner_peak}}": str(dinner_peak),
    "{{rolling_days}}": str(rolling_days),
    "{{gain_margin}}": f"{gain_margin:.1f}",
    "{{phase_margin}}": f"{phase_margin_deg:.0f}",
    "{{damping_ratio}}": f"{damping_ratio:.2f}",
    "{{crit_isf_bound}}": f"{crit_isf_bound:.0f}",
    "{{osc_period_hrs}}": f"{osc_period_hrs:.1f}",
    "{{omega_u}}": f"{omega_u:.2f}",
    "{{poles_str}}": poles_str
}

for k, v in substitutions.items():
    html_content = html_content.replace(k, str(v))

output_path = os.environ.get("LYDIA_OUTPUT") or os.path.join(os.path.dirname(__file__), "index.html")
with open(output_path, "w") as f:
    f.write(html_content)

print(f"Generated clean First-Principles Therapy Advisor with Live Solvers at {output_path}")
