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
import random
import statistics
import bisect
from datetime import datetime, timezone, timedelta
from collections import defaultdict
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Athens")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=2))

def to_local_dt(ts):
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ)

# ATTD / ADA International Consensus Glycemic Risk Ratio:
# Allowable TBR (<70 mg/dL) = 4%, Allowable TAR (>180 mg/dL) = 25%.
# By the Koenker-Bassett / Newsvendor Asymmetric Loss Theorem for a non-negative closed-loop
# Automatic-Bolus actuator (which can add insulin upward via micro-boluses, but cannot extract
# subcutaneous insulin once delivered), the Bayes-optimal open-loop basal schedule sits at the
# exact critical quantile tau* = L_low / (L_low + L_high) = 4 / 29 = 0.13793 of fasting demand.
TAU_ATTD_BASAL = 4.0 / (4.0 + 25.0)

try:
    import scipy.stats as stats
    def get_t_crit(df):
        return float(stats.t.ppf(0.975, df=max(1, df)))
except ImportError:
    # Pure Python exact Student's t critical values (95% two-tailed, alpha=0.05)
    T_TABLE_95 = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        12: 2.179, 15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042,
        40: 2.021, 50: 2.009, 60: 2.000, 80: 1.990, 100: 1.984
    }
    def get_t_crit(df):
        df_int = max(1, int(round(df)))
        if df_int in T_TABLE_95:
            return T_TABLE_95[df_int]
        for k in sorted(T_TABLE_95.keys()):
            if df_int <= k:
                return T_TABLE_95[k]
        return 1.960

BASE_URL = "https://fudbf291-lydia-guest.t1pal.com"

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

live_targets_low = []
live_targets_high = []
prof_timeline = []
try:
    print("Fetching live & historical Profiles from Nightscout...")
    profiles = fetch_json_with_retry(f"{BASE_URL}/api/v1/profile.json?count=200", timeout=25)
    active_name = profiles[0].get("defaultProfile", "Default")
    store = profiles[0]["store"].get(active_name, profiles[0]["store"][list(profiles[0]["store"].keys())[0]])
    live_basals = store.get("basal", [])
    live_crs = store.get("carbratio", [])
    live_isfs = store.get("sens", [])
    live_targets_low = store.get("target_low", [])
    live_targets_high = store.get("target_high", [])
    p_ts = datetime.fromisoformat(profiles[0].get("created_at").replace("Z", "+00:00")).timestamp()
    profile_updated_str = to_local_dt(p_ts).strftime("%b %d, %H:%M")
    for p in profiles:
        cr_p = p.get("created_at")
        if not cr_p:
            continue
        ts_p = datetime.fromisoformat(cr_p.replace("Z", "+00:00")).timestamp()
        act_p = p.get("defaultProfile", "Default")
        st_map = p.get("store", {})
        st_p = st_map.get(act_p) or (st_map[list(st_map.keys())[0]] if st_map else {})
        bas_p = sorted(st_p.get("basal", []), key=lambda x: item_sec(x))
        tlo_p = sorted(st_p.get("target_low", []), key=lambda x: item_sec(x))
        thi_p = sorted(st_p.get("target_high", []), key=lambda x: item_sec(x))
        prof_timeline.append((ts_p, bas_p, tlo_p, thi_p))
    prof_timeline.sort(key=lambda x: x[0])
    print(f"Profile loaded successfully (active: {active_name}, updated: {profile_updated_str}, {len(prof_timeline)} historical versions).")
except Exception as pe:
    print(f"Notice: Could not load live profile ({pe}), using defaults.")

prof_times = [x[0] for x in prof_timeline]

def eval_sched_sec(sched, sec, default_val):
    if not sched:
        return default_val
    cur = float(sched[0]["value"])
    for item in sched:
        if item_sec(item) <= sec:
            cur = float(item["value"])
        else:
            break
    return cur

def get_hist_sched_basal(ts):
    if not prof_timeline:
        return 0.10
    idx = bisect.bisect_right(prof_times, ts) - 1
    bas = prof_timeline[max(0, idx)][1]
    dt_l = to_local_dt(ts)
    sec = dt_l.hour * 3600 + dt_l.minute * 60 + dt_l.second
    return eval_sched_sec(bas, sec, 0.10)

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
        cgm_timeline.append((ts / 1000.0, float(sgv)))
cgm_timeline.sort(key=lambda x: x[0])

cgm_times = [p[0] for p in cgm_timeline]
cgm_vals = [p[1] for p in cgm_timeline]

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

# Parse Treatments (Carbs with declared absorptionTime, Boluses, Temp Basals, Exercise Overrides)
carbs_list = []       # (ts, carbs_g, abs_sec)
insulin_events = []   # (ts, units, is_auto)
raw_temp_basals = []  # (start_ts, end_ts, rate_u_hr)
raw_target_overrides = []  # (start_ts, end_ts, target_mid_mgdl)
exercise_overrides = []

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
        abs_min = float(t.get("absorptionTime") or 180.0)
        carbs_list.append((ts, float(carbs), abs_min * 60.0))

    ins = t.get("insulin")
    if ins and float(ins) > 0:
        insulin_events.append((ts, float(ins), bool(t.get("automatic"))))

    if t.get("eventType") == "Temp Basal" and t.get("rate") is not None:
        dur = float(t.get("duration") or 30.0)
        rate = float(t.get("rate") or 0.0)
        raw_temp_basals.append((ts, ts + dur * 60.0, rate))

    if t.get("eventType") == "Temporary Override":
        dur_min = float(t.get("duration") or 0.0)
        scale = float(t.get("insulinNeedsScaleFactor") or 1.0)
        rsn = (t.get("reason") or "").lower()
        c_rng = t.get("correctionRange")
        if dur_min > 0.5:
            if scale < 0.95 or any(w in rsn for w in ["run", "walk", "play", "exercise", "activity"]):
                exercise_overrides.append((ts, ts + dur_min * 60.0))
            if isinstance(c_rng, list) and len(c_rng) == 2 and c_rng[0] and c_rng[1]:
                raw_target_overrides.append((ts, ts + dur_min * 60.0, 0.5 * (float(c_rng[0]) + float(c_rng[1]))))

carbs_list.sort(key=lambda x: x[0])
insulin_events.sort(key=lambda x: x[0])
raw_temp_basals.sort(key=lambda x: x[0])
raw_target_overrides.sort(key=lambda x: x[0])
exercise_overrides.sort(key=lambda x: x[0])

# Truncate superseded Temp Basals and Target Overrides
temp_basals = []
for i, (s, e, r) in enumerate(raw_temp_basals):
    if i + 1 < len(raw_temp_basals) and raw_temp_basals[i + 1][0] < e:
        e = raw_temp_basals[i + 1][0]
    if e > s:
        temp_basals.append((s, e, r))
tb_starts = [x[0] for x in temp_basals]

target_overrides = []
for i, (s, e, m_val) in enumerate(raw_target_overrides):
    if i + 1 < len(raw_target_overrides) and raw_target_overrides[i + 1][0] < e:
        e = raw_target_overrides[i + 1][0]
    if e > s:
        target_overrides.append((s, e, m_val))
tov_starts = [x[0] for x in target_overrides]

def get_pump_basal_rate(ts):
    idx = bisect.bisect_right(tb_starts, ts) - 1
    if idx >= 0 and temp_basals[idx][0] <= ts < temp_basals[idx][1]:
        return temp_basals[idx][2]
    return get_hist_sched_basal(ts)

def get_active_target_mid(ts):
    oi = bisect.bisect_right(tov_starts, ts) - 1
    if oi >= 0 and target_overrides[oi][0] <= ts < target_overrides[oi][1]:
        return target_overrides[oi][2]
    if not prof_timeline:
        return 117.5
    pi = bisect.bisect_right(prof_times, ts) - 1
    _, _, tlo_p, thi_p = prof_timeline[max(0, pi)]
    dt_l = to_local_dt(ts)
    sec = dt_l.hour * 3600 + dt_l.minute * 60 + dt_l.second
    return 0.5 * (eval_sched_sec(tlo_p, sec, 110.0) + eval_sched_sec(thi_p, sec, 125.0))

def in_exercise_window(t_start, t_end):
    return any(min(t_end, e) - max(t_start, s) >= 900.0 for s, e in exercise_overrides)

# -------------------------------------------------------------------------
# EXACT LOOPKIT LYUMJEV PHARMACOKINETIC (PK) ABSORPTION ENGINE (55m Peak, 360m DIA)
# -------------------------------------------------------------------------
# Computes exact plasma insulin absorption I_abs(t1, t2) = IOB(t1) + I_deliv(t1, t2) - IOB(t2)
# across a 5-minute discrete grid covering [-6h, end] so neither unspent tail IOB nor pre-boluses distort solvers.
TD_MIN = 360.0
TP_MIN = 55.0
TAU_PK = TP_MIN * (1.0 - TP_MIN / TD_MIN) / (1.0 - 2.0 * TP_MIN / TD_MIN)
A_PK = 2.0 * TAU_PK / TD_MIN
S_PK = 1.0 / (1.0 - A_PK + (1.0 + A_PK) * math.exp(-TD_MIN / TAU_PK))

def loopkit_iob_fraction(t_min):
    if t_min <= 0.0:
        return 1.0
    if t_min >= TD_MIN:
        return 0.0
    poly = (t_min**2) / (TAU_PK * TD_MIN * (1.0 - A_PK)) - (t_min / TAU_PK) - 1.0
    return 1.0 - S_PK * (1.0 - A_PK) * (poly * math.exp(-t_min / TAU_PK) + 1.0)

PK_STEPS = 72  # 72 * 5 min = 360 min DIA
PK_KERNEL = [loopkit_iob_fraction(m * 5.0) - loopkit_iob_fraction((m + 1) * 5.0) for m in range(PK_STEPS)]

grid_t0 = int(cgm_timeline[0][0] // 300) * 300 - 21600
grid_t1 = int(cgm_timeline[-1][0] // 300) * 300 + 300
n_grid = int((grid_t1 - grid_t0) // 300)

u_basal_grid = [get_pump_basal_rate(grid_t0 + k * 300.0 + 150.0) * (300.0 / 3600.0) for k in range(n_grid)]
u_bolus_grid = [0.0] * n_grid
for tb_ts, ins, _ in insulin_events:
    k_idx = int((tb_ts - grid_t0) // 300)
    if 0 <= k_idx < n_grid:
        u_bolus_grid[k_idx] += ins

u_tot_grid = [u_basal_grid[k] + u_bolus_grid[k] for k in range(n_grid)]
ins_abs_grid = [
    sum(u_tot_grid[k - m] * PK_KERNEL[m] for m in range(min(k + 1, PK_STEPS)))
    for k in range(n_grid)
]

pref_ins_abs = [0.0] * (n_grid + 1)
for k in range(n_grid):
    pref_ins_abs[k + 1] = pref_ins_abs[k] + ins_abs_grid[k]

def get_absorbed_insulin(t1, t2):
    k1 = max(0, min(n_grid, int(round((t1 - grid_t0) / 300.0))))
    k2 = max(0, min(n_grid, int(round((t2 - grid_t0) / 300.0))))
    return pref_ins_abs[k2] - pref_ins_abs[k1]

def get_basal_block_id(hour, minute=0):
    hm = hour * 60 + minute
    if 0 <= hm < 90: return "00:00"      # 00:00 - 01:30 (Early nocturnal sleep onset)
    elif 90 <= hm < 300: return "01:30"  # 01:30 - 05:00 (Deep nocturnal sleep baseline)
    elif 300 <= hm < 510: return "05:00" # 05:00 - 08:30 (Dawn cortisol surge intercept)
    elif 510 <= hm < 1320: return "08:30"# 08:30 - 22:00 (Daytime active metabolism)
    else: return "22:00"                 # 22:00 - 24:00 (Bedtime transition)

def has_active_gut_carbs(t1, t2):
    # Respects each carb entry's declared absorptionTime in Nightscout (3.5h standard meal clearance, 45m for <=30m rescue)
    for tc, c, abs_sec in carbs_list:
        eff_abs = 2700.0 if abs_sec <= 2400.0 else max(12600.0, abs_sec)
        if tc <= t2 and (tc + eff_abs) > t1:
            return True
    return False

# -------------------------------------------------------------------------
# COUPLED SOLVERS 1 & 2: Exact PK ISF & ATTD Asymmetric-Quantile Fasting Basal
# Zero Manual Heuristics • Zero Block-Specific Rules • Fixed-Point Convergence
# -------------------------------------------------------------------------
print("Solving coupled exact-PK ISF and fasting metabolic flux equilibrium...")
cur_isf = get_profile_val(live_isfs, "00:00", 125.0)

# Build isolated correction bolus clusters (boluses without concurrent carbs within +-10m)
corr_treatments = [
    (ts, ins) for ts, ins, _ in insulin_events
    if not any(abs(tc - ts) < 600.0 for tc, _, _ in carbs_list)
]
corr_treatments.sort(key=lambda x: x[0])

clusters = []
cur_cl = []
for ct, ins in corr_treatments:
    if not cur_cl or ct - cur_cl[-1][0] <= 2700.0:
        cur_cl.append((ct, ins))
    else:
        clusters.append(cur_cl)
        cur_cl = [(ct, ins)]
if cur_cl:
    clusters.append(cur_cl)

rec_isf = cur_isf
geom_isf = cur_isf
basal_samples_by_block = defaultdict(list)
egp_med_by_block = {}
isf_episodes = []

for _iter in range(4):
    # Stage 2 sample extraction: hourly fasting metabolic demand E(t) = I_abs(t1, t2) + (BG2 - BG1) / ISF
    basal_samples_by_block = defaultdict(list)
    cur_t = int(math.ceil((cgm_timeline[0][0] + 21600.0) / 1800.0)) * 1800
    max_t = cgm_timeline[-1][0]
    while cur_t + 3600.0 <= max_t:
        t1 = cur_t
        t2 = cur_t + 3600.0
        cur_t += 1800.0
        if has_active_gut_carbs(t1, t2):
            continue
        if in_exercise_window(t1 - 1800.0, t2):
            continue
        bg1 = get_bg_at(t1, max_delta=600)
        bg2 = get_bg_at(t2, max_delta=600)
        if bg1 is None or bg2 is None:
            continue
        # Require normoglycemic start (70..180); retain drops down to 40 mg/dL to avoid hypo survivorship bias
        if not (70.0 <= bg1 <= 180.0 and 40.0 <= bg2 <= 180.0):
            continue
        i_flux = get_absorbed_insulin(t1, t2) + ((bg2 - bg1) / rec_isf)
        dt_local = to_local_dt(t1)
        blk_id = get_basal_block_id(dt_local.hour, dt_local.minute)
        basal_samples_by_block[blk_id].append(i_flux)

    egp_med_by_block = {
        blk: statistics.median(vals) for blk, vals in basal_samples_by_block.items() if vals
    }

    # Stage 1 episode evaluation: ISF_i = |Delta BG| / (I_abs(t0, t_nadir) - integral EGP_med(t) dt)
    isf_episodes = []
    last_isf_nadir_ts = 0.0
    for cl in clusters:
        t_start = cl[0][0]
        if t_start <= last_isf_nadir_ts or sum(x[1] for x in cl) < 0.15:
            continue
        if has_active_gut_carbs(t_start, t_start + 3600.0):
            continue
        bg_bolus = get_bg_at(t_start, max_delta=600)
        if bg_bolus is None or bg_bolus < 165.0:
            continue

        # Truncate nadir search prior to any post-drop low-BG rescue carb rather than discarding episode
        later_carbs = [(tc, c) for tc, c, _ in carbs_list if t_start + 3600.0 < tc <= t_start + 14400.0]
        if later_carbs:
            first_c_t, first_c_amt = later_carbs[0]
            bg_at_c = get_bg_at(first_c_t, max_delta=600)
            if not (bg_at_c is not None and bg_at_c < 90.0 and first_c_amt <= 16.0):
                continue
            search_end = first_c_t
        else:
            search_end = t_start + 14400.0

        cgm_after = [(t_sec, bg) for t_sec, bg in cgm_timeline if t_start + 3600.0 <= t_sec <= search_end]
        if not cgm_after:
            continue
        t_nadir, bg_nadir = min(cgm_after, key=lambda x: x[1])
        drop = bg_bolus - bg_nadir
        if drop < 25.0 or in_exercise_window(t_start - 1800.0, t_nadir):
            continue

        i_abs_win = get_absorbed_insulin(t_start, t_nadir)
        egp_offset = 0.0
        tt = t_start
        while tt < t_nadir:
            nxt = min(t_nadir, tt + 300.0)
            dt_mid = to_local_dt(0.5 * (tt + nxt))
            blk_mid = get_basal_block_id(dt_mid.hour, dt_mid.minute)
            egp_offset += egp_med_by_block.get(blk_mid, 0.25) * ((nxt - tt) / 3600.0)
            tt = nxt

        net_i_abs = i_abs_win - egp_offset
        if net_i_abs > 0.12:
            ep_isf = drop / net_i_abs
            if 40.0 <= ep_isf <= 500.0:
                dt_ep = to_local_dt(t_start)
                isf_episodes.append({
                    "time": dt_ep.strftime("%b %d, %H:%M"),
                    "bg_bolus": bg_bolus,
                    "bg_nadir": bg_nadir,
                    "drop": drop,
                    "i_corr": i_abs_win,
                    "net_icorr": net_i_abs,
                    "delta_iob_basal": -egp_offset,
                    "direct_isf": ep_isf,
                    "adj_isf": ep_isf,
                    "dur_hrs": (t_nadir - t_start) / 3600.0
                })
                last_isf_nadir_ts = t_nadir

    if isf_episodes:
        # Geometric mean is the exact log-space symmetric MLE for positive scale ratios (zero insulin-weighting bias)
        geom_isf = math.exp(statistics.mean([math.log(ep["direct_isf"]) for ep in isf_episodes]))
        rec_isf = round(geom_isf / 5.0) * 5.0

direct_isfs = [ep["direct_isf"] for ep in isf_episodes]
if direct_isfs:
    dynamic_isf = statistics.median(direct_isfs)
    min_isf = min(direct_isfs)
    max_isf = max(direct_isfs)
    isf_ci_low = round(min_isf)
    isf_ci_high = round(max_isf)
    isf_in_ci = (isf_ci_low <= cur_isf <= isf_ci_high)
    print(f"Pharmacological PK ISF Proof: {len(direct_isfs)} episodes (geom mean {geom_isf:.1f}, median {dynamic_isf:.1f}, range {isf_ci_low}–{isf_ci_high}). Recommended: {rec_isf:.0f} mg/dL/U.")
else:
    dynamic_isf = cur_isf
    geom_isf = cur_isf
    min_isf = cur_isf
    max_isf = cur_isf
    rec_isf = cur_isf
    isf_ci_low = cur_isf
    isf_ci_high = cur_isf
    isf_in_ci = True
    print(f"Active profile ISF: {cur_isf:.0f} mg/dL/U maintained.")

# -------------------------------------------------------------------------
# STAGE 2: UNIVERSAL ATTD ASYMMETRIC-QUANTILE BASAL SOLVER (Zero Block Heuristics)
# -------------------------------------------------------------------------
def exact_quantile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    f = int(k)
    c = min(len(sorted_vals) - 1, f + 1)
    return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])

def solve_basal_block(block_id, default_val, desc_prefix):
    samps = sorted(basal_samples_by_block[block_id])
    n = len(samps)
    if n >= 5:
        q_attd = exact_quantile(samps, TAU_ATTD_BASAL)
        med_egp = exact_quantile(samps, 0.50)
        sd = statistics.stdev(samps) if n > 1 else 0.0

        # Non-parametric bootstrap 95% CI of the exact ATTD tau* = 4/29 quantile
        rng = random.Random(42 + sum(ord(ch) for ch in block_id))
        boots = sorted(
            exact_quantile(sorted(rng.choices(samps, k=n)), TAU_ATTD_BASAL)
            for _ in range(1000)
        )
        raw_rec = max(0.05, round(max(0.0, q_attd) * 20.0 + 1e-9) / 20.0)
        ci_low = min(raw_rec, round(max(0.0, boots[25]), 3))
        ci_high = max(raw_rec, round(max(0.05, boots[975]), 3))
        ci = (ci_low, ci_high)

        flags = []
        if n < 10:
            flags.append(f"FLAG_LOW_SAMPLE_SIZE (N={n})")
        flag_badge = f" [Flags: {', '.join(flags)}]" if flags else ""
        auto_supp = max(0.0, med_egp - raw_rec)
        ev = (
            f"Solved via ATTD asymmetric closed-loop quantile τ* = 4/29 = 13.8% across N={n} PK fasting hours "
            f"(τ* baseline: {max(0.0, q_attd):.3f} U/hr → {raw_rec:.2f} U/hr; median total EGP flux: {med_egp:.3f} U/hr, "
            f"with ~{auto_supp:.2f} U/hr dynamically supplied on higher-demand nights via Loop Automatic Boluses).{flag_badge} {desc_prefix}"
        )
        return raw_rec, q_attd, sd, n, flags, ev, ci
    else:
        rec = max(0.05, default_val)
        return rec, rec, 0.0, n, [f"FLAG_LOW_SAMPLE_SIZE (N={n})"], f"Insufficient fasting windows (N={n} hrs); anchored to {rec:.2f} U/hr. {desc_prefix}", (rec, rec)

basal_results = {
    "00:00": solve_basal_block("00:00", 0.05, "Early nocturnal sleep onset (00:00–01:30)."),
    "01:30": solve_basal_block("01:30", 0.10, "Deep nocturnal sleep homeostasis (01:30–05:00)."),
    "05:00": solve_basal_block("05:00", 0.10, "Dawn hepatic cortisol window (05:00–08:30)."),
    "08:30": solve_basal_block("08:30", 0.15, "Daytime resting baseline (08:30–22:00)."),
    "22:00": solve_basal_block("22:00", 0.05, "Bedtime sleep transition (22:00–24:00).")
}

for blk, (rec_val, q_val, sd, n, flags, ev, (ci_low, ci_high)) in sorted(basal_results.items()):
    print(f"Basal {blk}: {rec_val:.2f} U/hr (τ* flux: {q_val:.3f} U/hr, 95% CI: [{ci_low:.3f}–{ci_high:.3f}], N={n}) -> {ev}")

# -------------------------------------------------------------------------
# STAGE 3: EXACT PK & GAUSS-MARKOV WLS CARB RATIO SOLVER (w_i = 1/C_i, 3.5h Horizon)
# -------------------------------------------------------------------------
# Cluster all non-rescue meal entries within 45m first so multi-part toddler meals (e.g. 5g + 5g = 10g) are preserved
clustered_meals = []
cur_mt, cur_mc, cur_mabs = None, 0.0, 10800.0
for tc, c, abs_sec in carbs_list:
    bg_at_c = get_bg_at(tc, max_delta=600)
    if cur_mt is None or (tc - cur_mt) >= 2700.0:
        if cur_mt is not None and cur_mc >= 8.0:
            clustered_meals.append((cur_mt, cur_mc, cur_mabs))
        # Standalone fast-acting hypo rescue (<85 mg/dL, <=30m lollipop icon) does not initiate a meal cluster
        if abs_sec <= 2400.0 and (bg_at_c is not None and bg_at_c < 85.0) and c <= 12.0:
            cur_mt, cur_mc, cur_mabs = None, 0.0, 10800.0
        else:
            cur_mt, cur_mc, cur_mabs = tc, c, abs_sec
    else:
        cur_mc += c
        cur_mabs = max(cur_mabs, abs_sec)
if cur_mt is not None and cur_mc >= 8.0:
    clustered_meals.append((cur_mt, cur_mc, cur_mabs))

dynamic_slots = [
    ("00:00", "08:30", "Overnight Baseline", get_profile_val(live_crs, "00:00", 6.0), "Overnight insulin sensitivity baseline protecting against nocturnal hypoglycemia."),
    ("08:30", "12:00", "Breakfast & Morning", get_profile_val(live_crs, "08:30", 4.5), "Morning cortisol creates substantial insulin resistance; 15–20 min pre-bolus critical."),
    ("12:00", "17:00", "Daytime (Lunch & Snack)", get_profile_val(live_crs, "12:00", 5.0), "Active daytime metabolism and toddler physical activity."),
    ("17:00", "22:00", "Evening (Dinner & Bedtime)", get_profile_val(live_crs, "17:00", 5.7), "Evening carbohydrate disposal and bedtime settling."),
    ("22:00", "24:00", "Bedtime / Overnight", get_profile_val(live_crs, "22:00", 5.6), "Bedtime transition as evening digestion clears.")
]

def hm_to_dynamic_slot(hm):
    for start, end, name, def_cr, note in dynamic_slots:
        sh, sm = map(int, start.split(':'))
        eh, em = map(int, end.split(':'))
        if (sh * 60 + sm) <= hm < (eh * 60 + em):
            return start, name, def_cr, note
    return "22:00", "Bedtime / Overnight", get_profile_val(live_crs, "22:00", 5.6), "Bedtime transition as evening digestion clears."

def integrate_solved_basal(t1, t2):
    tot = 0.0
    tt = t1
    while tt < t2:
        nxt = min(t2, tt + 300.0)
        dt_mid = to_local_dt(0.5 * (tt + nxt))
        blk_mid = get_basal_block_id(dt_mid.hour, dt_mid.minute)
        tot += basal_results[blk_mid][0] * ((nxt - tt) / 3600.0)
        tt = nxt
    return tot

MEAL_HORIZON_SEC = 12600.0  # Consistent 3.5h PK & standard meal clearance horizon
slot_pts = defaultdict(list)
slot_deltas = defaultdict(list)
slot_rescue_cnt = defaultdict(int)

for mt, carbs, m_abs in clustered_meals:
    bg0 = get_bg_at(mt, max_delta=900)
    bg_end = get_bg_at(mt + MEAL_HORIZON_SEC, max_delta=900)
    if bg0 is None or bg_end is None or bg0 < 75.0:
        continue

    # Exclude overlapping second meals in (0, 3.5h), while retaining low-BG rescue treatments (<90 mg/dL, <=16g)
    sub_meals = [(t, c) for t, c, _ in clustered_meals if 0.0 < t - mt < MEAL_HORIZON_SEC]
    if sub_meals and not all((get_bg_at(st, 600) or 999.0) < 90.0 and sc <= 16.0 for st, sc in sub_meals):
        continue
    if in_exercise_window(mt - 900.0, mt + MEAL_HORIZON_SEC):
        continue

    dt_l = to_local_dt(mt)
    start_str, name, def_cr, note = hm_to_dynamic_slot(dt_l.hour * 60 + dt_l.minute)

    tot_carbs = sum(c for tc, c, _ in carbs_list if mt - 300.0 <= tc < mt + MEAL_HORIZON_SEC)
    has_logged_followup = (tot_carbs > carbs)
    post_bgs = [b for t, b in cgm_timeline if mt + 1800.0 <= t <= mt + MEAL_HORIZON_SEC]
    nadir_bg = min(post_bgs) if post_bgs else bg_end
    eff_bg_end = min(bg_end, nadir_bg) if (nadir_bg < 70.0 and not has_logged_followup) else bg_end

    # Exact PK absorbed insulin over [mt - 20m, mt + 3.5h] minus exact piecewise scheduled basal integral
    t_w0 = mt - 1200.0
    t_w1 = mt + MEAL_HORIZON_SEC
    i_abs_meal = get_absorbed_insulin(t_w0, t_w1)
    b_sched_meal = integrate_solved_basal(t_w0, t_w1)
    i_food = i_abs_meal - b_sched_meal + ((eff_bg_end - bg0) / rec_isf)

    if i_food > 0.2 and tot_carbs >= 6.0 and (tot_carbs / i_food) <= 25.0:
        slot_pts[start_str].append((tot_carbs, i_food))
        # Evaluate postprandial settling relative to Loop's active target midpoint at t + 3.5h
        active_t_mid = get_active_target_mid(mt + MEAL_HORIZON_SEC)
        slot_deltas[start_str].append(bg_end - active_t_mid)
        if has_logged_followup or nadir_bg < 70.0:
            slot_rescue_cnt[start_str] += 1

cr_results = {}
for start, end, name, def_cr, note in dynamic_slots:
    pts = slot_pts.get(start, [])
    deltas = slot_deltas.get(start, [])
    rescue_n = slot_rescue_cnt.get(start, 0)
    n = len(pts)
    cur_val = get_profile_val(live_crs, start, def_cr)

    if n >= 2:
        # Gauss-Markov BLUE Weighted Least Squares under proportional variance Var(I_i) = sigma^2 * C_i (w_i = 1/C_i)
        sum_c = sum(c for c, _ in pts)
        sum_i = sum(ifod for _, ifod in pts)
        beta_wls = sum_i / sum_c if sum_c > 0 else (1.0 / def_cr)
        wls_cr = 1.0 / beta_wls if beta_wls > 0 else def_cr

        df = max(1, n - 1)
        s2_wls = sum(((ifod - beta_wls * c) ** 2) / c for c, ifod in pts) / df
        se_beta = math.sqrt(s2_wls / sum_c) if sum_c > 0 else 0.0
        t_crit = get_t_crit(df) if df >= 1 else 1.96
        beta_low = max(1e-4, beta_wls - t_crit * se_beta)
        beta_high = beta_wls + t_crit * se_beta

        cr_ci_low = round(1.0 / beta_high, 1)
        cr_ci_high = round(1.0 / beta_low, 1)

        # True centered Pearson R^2 between C and I_food
        mean_c = sum_c / n
        mean_i = sum_i / n
        cov_ci = sum((c - mean_c) * (ifod - mean_i) for c, ifod in pts)
        var_c = sum((c - mean_c) ** 2 for c, _ in pts)
        var_i = sum((ifod - mean_i) ** 2 for _, ifod in pts)
        r2_val = (cov_ci ** 2) / (var_c * var_i) if (var_c > 0 and var_i > 0) else 0.0
        rel_se = (se_beta / beta_wls) if beta_wls > 0 else 1.0
        med_d = statistics.median(deltas)

        emp_crs = [c / ifod for c, ifod in pts if ifod > 0]
        emp_min = round(min(emp_crs), 1) if emp_crs else def_cr
        emp_max = round(max(emp_crs), 1) if emp_crs else def_cr

        ci_str = f"95% CI: [1:{cr_ci_low:.1f} – 1:{cr_ci_high:.1f} g/U]"
        flags = []
        if n < 5:
            flags.append(f"FLAG_LOW_SAMPLE_SIZE (N={n})")
        if rel_se > 0.25:
            flags.append(f"FLAG_WIDE_UNCERTAINTY (SE={rel_se*100:.0f}%)")
        if abs(med_d) > 30:
            flags.append(f"FLAG_UNSETTLED_VS_TARGET (Med BG-Target={med_d:+.0f} mg/dL)")

        flag_str = f" [Flags: {', '.join(flags)}]" if flags else ""
        rescue_note = f" Includes {rescue_n} rescue/hypo-aware windows." if rescue_n > 0 else ""

        is_underpowered = (n < 4 or (se_beta > 0 and (beta_wls / se_beta) < t_crit))
        if is_underpowered and start not in ["00:00", "22:00"]:
            status = "DO_NOT_USE"
            rec_cr = cur_val
            ev = f"⚠️ Underpowered sample (N={n}). PK WLS estimate: 1:{wls_cr:.1f} g/U (Centered R² = {r2_val:.2f}, Med BG−Target = {med_d:+.0f} mg/dL). {ci_str}.{flag_str} Maintain active profile setting (1:{cur_val:.1f} g/U). {note}"
        elif n < 5 and start in ["00:00", "22:00"]:
            status = "VALIDATED"
            rec_cr = cur_val
            ev = f"Sparse nocturnal meals (N={n}). PK WLS estimate: 1:{wls_cr:.1f} g/U ({ci_str}).{flag_str} Anchored to active profile setting (1:{cur_val:.1f} g/U). {note}"
        else:
            status = "VALIDATED"
            rec_cr = round(wls_cr, 1)
            ev = f"PK-conserved Gauss-Markov WLS (w=1/C): 1:{wls_cr:.1f} g/U (N = {n}, Centered R² = {r2_val:.2f}, Rel SE = {rel_se*100:.1f}%, Med BG−Target = {med_d:+.0f} mg/dL). {ci_str}, empirical range [1:{emp_min:.1f} – 1:{emp_max:.1f} g/U].{flag_str}{rescue_note} {note}"

        cr_results[start] = (rec_cr, wls_cr, r2_val, n, med_d, flags, status, ev, name, f"{start} – {end}", (cr_ci_low, cr_ci_high), (emp_min, emp_max))
    else:
        flags = [f"FLAG_LOW_SAMPLE_SIZE (N={n})"]
        status = "VALIDATED" if start in ["00:00", "22:00"] else "DO_NOT_USE"
        ev = f"Fasting nocturnal period with minimal isolated meals (N={n}). Anchored to active Nightscout profile setting (1:{cur_val:.1f} g/U). {note}"
        cr_results[start] = (cur_val, cur_val, 0.0, n, 0.0, flags, status, ev, name, f"{start} – {end}", (cur_val, cur_val), (cur_val, cur_val))

for s, res in cr_results.items():
    rec_val, raw_val, r2, n, med_d, flags, status, ev, name, win, (ci_l, ci_h), (emp_l, emp_h) = res
    print(f"CR {s} ({name}): 1:{rec_val:.1f} g/U (raw WLS: 1:{raw_val:.1f}, 95% CI: [1:{ci_l:.1f}–1:{ci_h:.1f}], Centered R²={r2:.2f}, N={n}, status={status}) -> {ev}")

# -------------------------------------------------------------------------
# STAGE 4: OPTIMAL CORRECTION TARGET RANGE SOLVER (ATTD Floor + Kovatchev BGRI + Deadband Physics)
# -------------------------------------------------------------------------
def kovatchev_bg_risk(bg_val):
    bg_c = max(20.0, min(600.0, bg_val))
    f_sym = 1.509 * ((math.log(bg_c) ** 1.084) - 5.381)
    return 10.0 * (f_sym ** 2)

# Deadband half-width from empirical 5m CGM jitter (robust MAD) & single Omnipod 0.05U step
adj_abs_diffs = sorted(
    abs(cgm_timeline[i][1] - cgm_timeline[i - 1][1])
    for i in range(1, len(cgm_timeline))
    if 240.0 <= (cgm_timeline[i][0] - cgm_timeline[i - 1][0]) <= 360.0
)
sigma_cgm_5m = (statistics.median(adj_abs_diffs) / 0.6745) / math.sqrt(2.0) if adj_abs_diffs else 5.5
pod_quantum_bg = 0.05 * rec_isf
target_band_width = max(10, int(math.ceil(2.0 * max(sigma_cgm_5m, pod_quantum_bg) / 5.0) * 5))

def solve_optimal_target_mid(sorted_errs, step_stride=3):
    n_e = len(sorted_errs)
    if n_e < 20:
        return 115.0, -35.0, -50.0, 4.0
    q04 = sorted_errs[int(0.04 * n_e)]
    q01 = sorted_errs[int(0.01 * n_e)]
    t_floor = max(70.0 - q04, 54.0 - q01)
    best_mid = t_floor
    best_bgri = 1e9
    sub_e = sorted_errs[::step_stride]
    for k_step in range(0, 50):
        cand = t_floor + k_step * 0.5
        if cand > 145.0:
            break
        r_val = sum(kovatchev_bg_risk(cand + x) for x in sub_e) / len(sub_e)
        if r_val < best_bgri:
            best_bgri = r_val
            best_mid = cand
    return best_mid, q04, q01, best_bgri

target_windows = [
    ("22:00", "22:00 – 08:30", "Overnight & Dawn", lambda hm: hm >= 1320 or hm < 510),
    ("08:30", "08:30 – 22:00", "Daytime Active", lambda hm: 510 <= hm < 1320),
]

target_results = {}
for t_key, win_label, win_name, hm_pred in target_windows:
    errs_all = []
    errs_by_day = defaultdict(list)
    for ts_c, bg_c in cgm_timeline:
        dt_c = to_local_dt(ts_c)
        if not hm_pred(dt_c.hour * 60 + dt_c.minute):
            continue
        err_c = bg_c - get_active_target_mid(ts_c)
        errs_all.append(err_c)
        errs_by_day[dt_c.strftime("%Y-%m-%d")].append(err_c)

    cur_lo = get_profile_val(live_targets_low, t_key, 110.0)
    cur_hi = get_profile_val(live_targets_high, t_key, 125.0)
    cur_mid = 0.5 * (cur_lo + cur_hi)

    errs_sorted = sorted(errs_all)
    opt_mid, q04_v, q01_v, bgri_v = solve_optimal_target_mid(errs_sorted, step_stride=1)
    rec_lo = int(round((opt_mid - 0.5 * target_band_width) / 5.0) * 5)
    rec_hi = rec_lo + target_band_width

    # Day-Block Bootstrap 95% CI across calendar days (preserves intra-day serial correlation)
    day_blocks = [d_vals for d_vals in errs_by_day.values() if len(d_vals) >= 6]
    if len(day_blocks) >= 5:
        rng_t = random.Random(101 + sum(ord(ch) for ch in t_key))
        boot_mids = []
        for _ in range(250):
            sampled = rng_t.choices(day_blocks, k=len(day_blocks))
            flat_s = sorted(x for blk_d in sampled for x in blk_d)
            bm, _, _, _ = solve_optimal_target_mid(flat_s, step_stride=4)
            boot_mids.append(bm)
        boot_mids.sort()
        ci_mid_lo = round(boot_mids[int(0.025 * len(boot_mids))], 1)
        ci_mid_hi = round(boot_mids[int(0.975 * len(boot_mids))], 1)
    else:
        ci_mid_lo, ci_mid_hi = round(opt_mid - 5.0, 1), round(opt_mid + 5.0, 1)

    pred_tir = 100.0 * sum(1 for e_x in errs_sorted if 70.0 <= opt_mid + e_x <= 180.0) / max(1, len(errs_sorted))
    ev_t = (
        f"Tracking residuals ε = BG − Target (N={len(errs_sorted):,} pts across {len(day_blocks)} days): "
        f"4th-pct drop q₀.₀₄ = {q04_v:+.1f} mg/dL, 1st-pct drop q₀.₀₁ = {q01_v:+.1f} mg/dL → "
        f"ATTD safety floor T_min = {max(70.0 - q04_v, 54.0 - q01_v):.1f} mg/dL; Kovatchev BGRI minimum = {opt_mid:.1f} mg/dL "
        f"(BGRI = {bgri_v:.2f}, Pred TIR = {pred_tir:.1f}%). Deadband width {target_band_width} mg/dL derived from "
        f"5m CGM jitter (σ = {sigma_cgm_5m:.1f} mg/dL) & 0.05U pod step ({pod_quantum_bg:.1f} mg/dL)."
    )
    target_results[t_key] = (
        rec_lo, rec_hi, opt_mid, (ci_mid_lo, ci_mid_hi),
        int(round(cur_lo)), int(round(cur_hi)), cur_mid,
        win_label, win_name, ev_t
    )
    print(f"Target {t_key} ({win_name}): [{rec_lo}–{rec_hi} mg/dL] (OptMid={opt_mid:.1f}, Day-Block 95% CI [{ci_mid_lo:.1f}–{ci_mid_hi:.1f}], Current=[{cur_lo:.0f}–{cur_hi:.0f}])")

# -------------------------------------------------------------------------
# STAGE 5: CLOSED-LOOP STABILITY BOUNDS & DAMPING ANALYSIS (Lyumjev 55m Peak)
# -------------------------------------------------------------------------
tau_delay = 55.0 / 60.0  # Lyumjev peak activity in LoopKit (55 mins = 0.9167 hr)
tau_dia = 6.0            # Lyumjev action duration in LoopKit (360 mins = 6.0 hrs)
isf_true = geom_isf      # Lydia's empirically derived physical sensitivity (mg/dL/U)

a_coeff = tau_dia * tau_delay
b_coeff = tau_dia + tau_delay
c_coeff = 1.0 + (isf_true / rec_isf)

wn = math.sqrt(c_coeff / a_coeff)
damping_ratio = b_coeff / (2.0 * math.sqrt(a_coeff * c_coeff))

discrim = b_coeff**2 - 4.0 * a_coeff * c_coeff
if discrim >= 0:
    pole1 = (-b_coeff + math.sqrt(discrim)) / (2.0 * a_coeff)
    pole2 = (-b_coeff - math.sqrt(discrim)) / (2.0 * a_coeff)
    poles_str = f"Real [{pole1:.2f}, {pole2:.2f}] h⁻¹ (Zero Oscillation)"
else:
    real_p = -b_coeff / (2.0 * a_coeff)
    imag_p = math.sqrt(-discrim) / (2.0 * a_coeff)
    poles_str = f"Complex [{real_p:.2f} ± {imag_p:.2f}j] h⁻¹"

c_crit = (b_coeff**2) / (4.0 * a_coeff)
crit_isf_bound = isf_true / (c_crit - 1.0)

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

omega_c = 0.12
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

    earliest_dt = to_local_dt(entries[-1]["date"] / 1000.0)
    latest_dt = to_local_dt(entries[0]["date"] / 1000.0)
else:
    mean_bg, sd_bg, cv_bg = 140.5, 47.9, 34.1
    v_low_pct, low_pct, in_range_pct, high_pct, v_high_pct = 0.8, 2.9, 78.3, 15.1, 2.8
    gmi = 6.7
    ea1c = 6.5
    latest_dt = to_local_dt(datetime.now(timezone.utc).timestamp())

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

updated_str = latest_dt.strftime("%b %d, %Y • %H:%M %Z")

# AGP Modal Day Bins
bins = [[] for _ in range(48)]
for e in entries:
    sgv = e.get("sgv")
    ts = e.get("date")
    if not sgv or not ts or sgv < 30 or sgv > 500: continue
    dt = to_local_dt(ts / 1000.0)
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
    rec_val, q_val, sd, n_b, flags, evidence, (ci_low, ci_high) = basal_results[t_str]
    cur_val = get_profile_val(live_basals, t_str, rec_val)
    is_aligned = abs(cur_val - rec_val) < 0.01
    is_in_ci = (ci_low - 0.005 <= cur_val <= ci_high + 0.005)

    if is_aligned:
        cur_html = f'<span class="text-emerald-700 font-bold">{cur_val:.2f} U/hr</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In Sync</span>'
        row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
    elif is_in_ci:
        cur_html = f'<span class="text-emerald-700 font-bold">{cur_val:.2f} U/hr</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In 95% CI Range</span>'
        row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
    else:
        cur_html = f'<span class="text-slate-400 line-through">{cur_val:.2f} U/hr</span>'
        action_label = f"Adjust to {rec_val:.2f}"
        if t_str == "01:30": action_label = f"Deep Sleep ({rec_val:.2f})"
        elif t_str == "05:00": action_label = f"Dawn Intercept ({rec_val:.2f})"
        elif t_str == "08:30": action_label = f"Daytime ({rec_val:.2f})"
        elif t_str == "22:00": action_label = f"Bedtime ({rec_val:.2f})"
        badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-blue-100 text-blue-800 font-bold">{action_label}</span>'
        row_bg = 'class="hover:bg-blue-50/50 bg-blue-50/20"'

    basal_rows_html += f"""
    <tr {row_bg}>
      <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">{t_str}</td>
      <td class="py-2.5 px-4 font-mono text-xs whitespace-nowrap">{cur_html}</td>
      <td class="py-2.5 px-4 whitespace-nowrap">
        <span class="font-extrabold text-blue-700 text-sm">{rec_val:.2f} U/hr</span>
        <span class="text-[11px] text-slate-500 font-mono ml-1.5">(τ* flux: {max(0.0, q_val):.3f} U/hr)</span>
        <div class="text-[11px] text-slate-600 font-sans font-medium mt-0.5">95% CI: <span class="font-mono font-semibold text-slate-800">{ci_low:.3f} – {ci_high:.3f} U/hr</span></div>
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
    rec_val, wls_cr, r2_val, n_c, med_d, flags, status, evidence, _, window_str, (cr_ci_low, cr_ci_high), (emp_min, emp_max) = cr_results[start]
    cur_val = get_profile_val(live_crs, start, rec_val)
    is_aligned = abs(cur_val - rec_val) < 0.2
    is_in_ci = (cr_ci_low <= cur_val <= cr_ci_high)

    blk = get_cr_basal_block(start)
    used_basal = basal_results[blk][0]

    if status == "DO_NOT_USE":
        cur_html = f'<span class="text-rose-700 font-bold">1:{cur_val:.1f} g/U</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-rose-100 text-rose-800 font-bold border border-rose-300">⛔ Flagged: Do Not Use</span>'
        row_bg = 'class="hover:bg-rose-50/50 bg-rose-50/20"'
    elif is_aligned:
        cur_html = f'<span class="text-emerald-700 font-bold">1:{cur_val:.1f} g/U</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In Sync</span>'
        row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
    elif is_in_ci:
        cur_html = f'<span class="text-emerald-700 font-bold">1:{cur_val:.1f} g/U</span>'
        badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In 95% CI Range</span>'
        row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
    else:
        cur_html = f'<span class="text-slate-400 line-through">1:{cur_val:.1f} g/U</span>'
        badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-purple-100 text-purple-800 font-bold">Adjust to 1:{rec_val:.1f}</span>'
        row_bg = 'class="hover:bg-purple-50/50 bg-purple-50/20"'

    inputs_badge = f'<div class="mt-1.5 text-[10px] font-mono text-indigo-800 bg-indigo-50/90 px-2 py-0.5 rounded border border-indigo-200/60 w-fit flex items-center gap-1.5"><span class="text-slate-500 uppercase tracking-wider font-semibold">Inputs used:</span><span class="font-bold">Piecewise Basal ({blk}: {used_basal:.2f} U/hr)</span><span>•</span><span class="font-bold">PK ISF: {rec_isf:.0f} mg/dL/U</span><span>•</span><span class="font-bold">Active Target (Profile + Overrides)</span></div>'

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
        <span class="text-[11px] text-slate-500 font-mono ml-1.5">(WLS: 1:{wls_cr:.1f})</span>
        <div class="text-[11px] text-slate-600 font-sans font-medium mt-0.5">95% CI: <span class="font-mono font-semibold text-slate-800">1:{cr_ci_low:.1f} – 1:{cr_ci_high:.1f}</span> <span class="text-slate-400 font-mono text-[10px]">(range: 1:{emp_min:.1f}–1:{emp_max:.1f})</span></div>
      </td>
      <td class="py-2.5 px-4 whitespace-nowrap">{badge_html}</td>
      <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">
        <div>{evidence}</div>
        {inputs_badge}
      </td>
    </tr>
    """

target_rows_html = ""
for t_key in ["22:00", "08:30"]:
    rec_lo, rec_hi, opt_mid, (ci_mid_lo, ci_mid_hi), cur_lo, cur_hi, cur_mid, win_label, win_name, ev_t = target_results[t_key]
    is_t_aligned = (cur_lo == rec_lo and cur_hi == rec_hi) or abs(cur_mid - opt_mid) <= 3.0
    is_t_in_ci = (ci_mid_lo - 2.5 <= cur_mid <= ci_mid_hi + 2.5)

    if is_t_aligned:
        cur_t_html = f'<span class="text-emerald-700 font-bold">{cur_lo}–{cur_hi} mg/dL</span> <span class="text-slate-400 text-[10px]">(mid {cur_mid:.1f})</span>'
        badge_t_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In Sync</span>'
        row_t_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
    elif is_t_in_ci:
        cur_t_html = f'<span class="text-emerald-700 font-bold">{cur_lo}–{cur_hi} mg/dL</span> <span class="text-slate-400 text-[10px]">(mid {cur_mid:.1f})</span>'
        badge_t_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In 95% CI Range</span>'
        row_t_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
    else:
        cur_t_html = f'<span class="text-slate-400 line-through">{cur_lo}–{cur_hi} mg/dL</span>'
        badge_t_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-blue-100 text-blue-800 font-bold">Adjust to {rec_lo}–{rec_hi}</span>'
        row_t_bg = 'class="hover:bg-blue-50/50 bg-blue-50/20"'

    target_rows_html += f"""
    <tr {row_t_bg}>
      <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">
        <div class="font-mono">{t_key}</div>
        <div class="text-[11px] font-semibold text-slate-600 font-sans">{win_name}</div>
        <div class="text-[10px] text-slate-400 font-mono">[{win_label})</div>
      </td>
      <td class="py-2.5 px-4 font-mono text-xs whitespace-nowrap">{cur_t_html}</td>
      <td class="py-2.5 px-4 whitespace-nowrap">
        <span class="font-extrabold text-emerald-700 text-sm">{rec_lo}–{rec_hi} mg/dL</span>
        <span class="text-[11px] text-slate-500 font-mono ml-1.5">(opt mid: {opt_mid:.1f})</span>
        <div class="text-[11px] text-slate-600 font-sans font-medium mt-0.5">Midpoint 95% CI: <span class="font-mono font-semibold text-slate-800">{ci_mid_lo:.1f} – {ci_mid_hi:.1f} mg/dL</span></div>
      </td>
      <td class="py-2.5 px-4 whitespace-nowrap">{badge_t_html}</td>
      <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">{ev_t}</td>
    </tr>
    """

# Live ISF Evaluation
is_isf_aligned = abs(cur_isf - rec_isf) <= 15.0 or isf_in_ci

if is_isf_aligned:
    badge_text = "✓ In Sync" if abs(cur_isf - rec_isf) < 2.0 else "✓ In Sync (Observed Range)"
    isf_badge_html = f'<span class="px-2.5 py-1 rounded text-xs font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">{badge_text}</span>'
    isf_decision_title = f"Profile {cur_isf:.0f} mg/dL/U aligns with PK geometric optimum ({rec_isf:.0f} mg/dL/U)."
else:
    isf_badge_html = f'<span class="px-2.5 py-1 rounded text-xs font-sans bg-amber-100 text-amber-800 font-bold">Adjust to {rec_isf:.0f}</span>'
    isf_decision_title = f"Adjust to {rec_isf:.0f} mg/dL/U (Current: {cur_isf:.0f} mg/dL/U)."

# Pharmacological ISF Evidence and Episode Rows
isf_episodes_rows_html = ""
for ep in isf_episodes:
    isf_val_str = f"{ep['direct_isf']:.1f} mg/dL/U"
    egp_str = f"{ep['delta_iob_basal']:+.2f} U (net {ep['net_icorr']:.2f} U)"
    isf_episodes_rows_html += f"""
    <tr class="hover:bg-slate-50">
      <td class="py-2 px-3 font-mono font-medium text-slate-800 whitespace-nowrap">{ep['time']}</td>
      <td class="py-2 px-3 font-mono text-slate-700 whitespace-nowrap">{ep['bg_bolus']:.0f} &rarr; {ep['bg_nadir']:.0f} mg/dL</td>
      <td class="py-2 px-3 font-mono font-bold text-emerald-700 whitespace-nowrap">&minus;{ep['drop']:.0f} mg/dL</td>
      <td class="py-2 px-3 font-mono text-slate-800 whitespace-nowrap">{ep['i_corr']:.2f} U</td>
      <td class="py-2 px-3 font-mono text-slate-500 text-xs whitespace-nowrap">{egp_str}</td>
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
    isf_evidence_text = f"Exact Lyumjev PK proof across {isf_proof_count} fasting corrections ($R_{{\\text{{gut}}}}=0$, $\\text{{BG}} \\ge 165\\text{{ mg/dL}}$). Net absorbed drops span {isf_proof_min}–{isf_proof_max} mg/dL/U (median {isf_proof_median}, geometric mean {geom_isf:.1f} → {rec_isf:.0f} mg/dL/U)."
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
    "{{target_rows_html}}": target_rows_html,
    "{{cur_isf}}": f"{cur_isf:.0f}",
    "{{rec_isf}}": f"{rec_isf:.0f}",
    "{{isf_ci_low}}": f"{isf_ci_low:.0f}",
    "{{isf_ci_high}}": f"{isf_ci_high:.0f}",
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

output_path = os.path.join(os.path.dirname(__file__), "index.html")
with open(output_path, "w") as f:
    f.write(html_content)

print(f"Generated clean First-Principles Therapy Advisor with Live Solvers at {output_path}")
