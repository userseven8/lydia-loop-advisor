import json, urllib.request, math, time
import numpy as np
from datetime import datetime, timezone, timedelta
from scipy.optimize import minimize

BASE_URL = "https://fudbf291-lydia-guest.t1pal.com"
TZ_OFFSET = timedelta(hours=3)

# Lyumjev model (peak 45m, DIA 300m)
def loopkit_model(peak=45.0, dia=300.0):
    tau = peak * (1.0 - peak / dia) / (1.0 - 2.0 * peak / dia)
    a = 2.0 * tau / dia
    S = 1.0 / (1.0 - a + (1.0 + a) * math.exp(-dia / tau))
    def peff(t):
        if t <= 0: return 0.0
        if t >= dia: return 1.0
        return 1.0 - S * (1.0 - a) * (((t**2)/(tau*dia*(1.0-a))) + t/tau + 1.0) * math.exp(-t/tau)
    return peff

peff_lyum = loopkit_model(45.0, 300.0)

print("Fetching data from Nightscout...")
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LydiaLoopAnalytics/2.0"}
url_e = f"{BASE_URL}/api/v1/entries/sgv.json?find[dateString][$gte]=2026-09-03T00:00:00.000Z&count=5000"
url_t = f"{BASE_URL}/api/v1/treatments.json?find[created_at][$gte]=2026-09-03T00:00:00.000Z&count=3000"

entries = json.loads(urllib.request.urlopen(urllib.request.Request(url_e, headers=headers)).read().decode("utf-8"))
treatments = json.loads(urllib.request.urlopen(urllib.request.Request(url_t, headers=headers)).read().decode("utf-8"))

cgm = sorted([(e["date"]/1000.0, e["sgv"]) for e in entries if e.get("sgv") and e.get("date")], key=lambda x: x[0])

insulin_events = []
carbs_list = []
temp_basals = []

for t in treatments:
    ins = t.get("insulin")
    created = t.get("created_at") or t.get("timestamp")
    if not created: continue
    try:
        ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except: continue
    if ins and float(ins) > 0:
        insulin_events.append((ts, float(ins)))
    c = t.get("carbs")
    if c and float(c) > 0:
        carbs_list.append((ts, float(c)))
    r = t.get("rate")
    d = t.get("duration")
    if r is not None and d is not None:
        try:
            temp_basals.append((ts, ts + float(d)*60.0, float(r)))
        except: pass

insulin_events.sort(key=lambda x: x[0])
carbs_list.sort(key=lambda x: x[0])
temp_basals.sort(key=lambda x: x[0])

def get_bg_at(ts, max_delta=900):
    best = None
    for t, bg in cgm:
        if abs(t - ts) < max_delta:
            if best is None or abs(t - ts) < abs(best[0] - ts):
                best = (t, bg)
    return best[1] if best else None

# Cluster carbs into distinct meals (within 45 min)
raw_meals = sorted(carbs_list, key=lambda x: x[0])
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

print(f"Total clustered meals: {len(clustered_meals)}")

# Collect 3-hour isolated meal episodes
meal_episodes = []
for mt, carbs in clustered_meals:
    if carbs < 5: continue
    # No other meal within next 3h
    if any(0 < t - mt < 10800 for t, c in clustered_meals): continue
    
    bg0 = get_bg_at(mt, max_delta=900)
    bg3 = get_bg_at(mt + 10800, max_delta=1200) or get_bg_at(mt + 14400, max_delta=1200)
    if bg0 is None or bg3 is None: continue
    if bg0 < 75: continue # exclude rescue carbs
    
    # Calculate insulin delivered across the 3h
    bolus_u = sum(ins for ts, ins in insulin_events if mt - 900 <= ts <= mt + 10800)
    
    # Actual basal delivered across 3h
    # compute 5-min intervals
    actual_basal = 0.0
    for step_t in range(int(mt), int(mt + 10800), 300):
        # find active temp basal
        rate = 0.10 # baseline assumption if no temp basal
        for s, e, r in temp_basals:
            if s <= step_t < e:
                rate = r
                break
        actual_basal += rate * (300.0 / 3600.0)
    
    dt_l = datetime.fromtimestamp(mt, tz=timezone.utc) + TZ_OFFSET
    hm = dt_l.hour + dt_l.minute / 60.0
    
    meal_episodes.append({
        "time": dt_l.strftime("%b %d, %H:%M"),
        "hour": hm,
        "carbs": carbs,
        "bg0": bg0,
        "bg3": bg3,
        "delta_bg": bg3 - bg0,
        "bolus_u": bolus_u,
        "actual_basal": actual_basal,
        "tot_insulin": bolus_u + actual_basal
    })

print(f"Valid isolated meal episodes for mass-balance: {len(meal_episodes)}")

# Collect unconfounded fasting correction drops
corr_treatments = [t for t in insulin_events if not any(abs(t[0] - tc) < 9000 for tc, c in carbs_list)]
# Group into clusters
corr_clusters = []
cur = []
for ct, ins in corr_treatments:
    if not cur: cur.append((ct, ins))
    else:
        if ct - cur[-1][0] <= 2700: cur.append((ct, ins))
        else:
            corr_clusters.append(cur)
            cur = [(ct, ins)]
if cur: corr_clusters.append(cur)

fasting_drops = []
for cl in corr_clusters:
    t_start = cl[0][0]
    tot_i = sum(x[1] for x in cl)
    if tot_i < 0.15: continue
    bg_start = get_bg_at(t_start, max_delta=600)
    if bg_start is None or bg_start < 160: continue
    cgm_after = [(t_sec, bg) for t_sec, bg in cgm if 3600 <= t_sec - t_start <= 14400]
    if not cgm_after: continue
    t_nadir, bg_nadir = min(cgm_after, key=lambda x: x[1])
    drop = bg_start - bg_nadir
    if drop < 20: continue
    
    dt_l = datetime.fromtimestamp(t_start, tz=timezone.utc) + TZ_OFFSET
    fasting_drops.append({
        "time": dt_l.strftime("%b %d, %H:%M"),
        "hour": dt_l.hour + dt_l.minute / 60.0,
        "bg_start": bg_start,
        "bg_nadir": bg_nadir,
        "drop": drop,
        "bolus_u": tot_i
    })

print(f"Valid fasting correction episodes: {len(fasting_drops)}")

# Collect 2-hour fasting resting equilibrium blocks (no meals in 2.5h, |d(BG)/dt| < 15 mg/dL/hr)
resting_blocks = []
min_t = cgm[0][0]
max_t = cgm[-1][0]
step_size = 7200 # 2 hours
cur_t = min_t
while cur_t + step_size < max_t:
    t1 = cur_t
    t2 = cur_t + step_size
    # Check no carbs in [t1 - 9000, t2]
    if not any(t1 - 9000 <= tc <= t2 for tc, c in carbs_list):
        bg1 = get_bg_at(t1, max_delta=600)
        bg2 = get_bg_at(t2, max_delta=600)
        if bg1 and bg2 and 80 <= bg1 <= 180 and 80 <= bg2 <= 180 and abs(bg2 - bg1) <= 25:
            # Check insulin delivered in this window
            boluses = sum(ins for ts, ins in insulin_events if t1 <= ts <= t2)
            actual_basal = 0.0
            for st in range(int(t1), int(t2), 300):
                r = 0.10
                for s, e, tb_r in temp_basals:
                    if s <= st < e:
                        r = tb_r
                        break
                actual_basal += r * (300.0 / 3600.0)
            
            dt_l = datetime.fromtimestamp(t1, tz=timezone.utc) + TZ_OFFSET
            resting_blocks.append({
                "time": dt_l.strftime("%b %d, %H:%M"),
                "hour": dt_l.hour + dt_l.minute / 60.0,
                "delta_bg": bg2 - bg1,
                "hourly_rate": (boluses + actual_basal) / 2.0
            })
    cur_t += 3600 # slide by 1 hr

print(f"Valid resting equilibrium blocks: {len(resting_blocks)}")

# Now define the Global Optimization
# Parameters to solve:
# 1. ISF_night (00:00 - 08:30)
# 2. ISF_day   (08:30 - 22:00)
# 3. Basal_night (00:00 - 05:00)
# 4. Basal_dawn  (05:00 - 08:30)
# 5. Basal_day   (08:30 - 22:00)
# 6. CR_bfast (08:30 - 11:30)
# 7. CR_lunch (11:30 - 15:00)
# 8. CR_snack (15:00 - 18:30)
# 9. CR_dinner (18:30 - 22:00)

def loss(params):
    isf_night, isf_day, b_night, b_dawn, b_day, cr_bfast, cr_lunch, cr_snack, cr_din = params
    err = 0.0
    
    # 1. Loss on fasting drops: predicted drop = ISF * bolus_u
    for ep in fasting_drops:
        isf = isf_night if (ep["hour"] < 8.5 or ep["hour"] >= 22) else isf_day
        pred_drop = isf * ep["bolus_u"]
        err += ((ep["drop"] - pred_drop) / 30.0)**2
    
    # 2. Loss on resting equilibrium: predicted basal = hourly_rate + delta_bg / (2 * ISF)
    for b in resting_blocks:
        h = b["hour"]
        if h < 5.0 or h >= 22.0: target_b = b_night
        elif 5.0 <= h < 8.5: target_b = b_dawn
        else: target_b = b_day
        isf = isf_night if (h < 8.5 or h >= 22) else isf_day
        eq_basal = b["hourly_rate"] - (b["delta_bg"] / (2.0 * isf))
        err += ((target_b - eq_basal) / 0.05)**2
    
    # 3. Loss on meal mass balance:
    # Expected insulin for meal: I_food = Carbs / CR
    # Actual insulin for food = bolus_u + (actual_basal - target_basal * 3h) + delta_bg / ISF
    for m in meal_episodes:
        h = m["hour"]
        if 8.0 <= h < 11.5: cr = cr_bfast
        elif 11.5 <= h < 15.0: cr = cr_lunch
        elif 15.0 <= h < 18.5: cr = cr_snack
        else: cr = cr_din
        
        isf = isf_day if 8.5 <= h < 22.0 else isf_night
        base_rate = b_day if 8.5 <= h < 22.0 else (b_dawn if 5.0 <= h < 8.5 else b_night)
        
        expected_food_insulin = m["carbs"] / cr
        actual_food_insulin = m["bolus_u"] + (m["actual_basal"] - base_rate * 3.0) + (m["delta_bg"] / isf)
        
        # Penalize difference between expected food insulin and actual food insulin
        err += ((expected_food_insulin - actual_food_insulin) / 0.2)**2
        
    return err

# Initial guess
p0 = [260.0, 220.0, 0.08, 0.12, 0.15, 4.5, 8.0, 11.0, 6.5]
bounds = [
    (150.0, 350.0), # isf_night
    (150.0, 300.0), # isf_day
    (0.04, 0.20),   # b_night
    (0.05, 0.25),   # b_dawn
    (0.08, 0.30),   # b_day
    (3.0, 8.0),     # cr_bfast
    (5.0, 15.0),    # cr_lunch
    (6.0, 18.0),    # cr_snack
    (4.0, 12.0)     # cr_din
]

res = minimize(loss, p0, bounds=bounds, method="L-BFGS-B")

print("\n=== GLOBAL MASS-BALANCE OPTIMIZATION RESULTS (14 DAYS) ===")
print(f"Convergence: {res.success}, Message: {res.message}")
p = res.x
print(f"ISF Overnight (00:00 - 08:30) : {p[0]:.1f} mg/dL/U")
print(f"ISF Daytime   (08:30 - 22:00) : {p[1]:.1f} mg/dL/U")
print(f"Basal Overnight (00:00 - 05:00): {p[2]:.2f} U/hr")
print(f"Basal Dawn      (05:00 - 08:30): {p[3]:.2f} U/hr")
print(f"Basal Daytime   (08:30 - 22:00): {p[4]:.2f} U/hr")
print(f"CR Breakfast    (08:30 - 11:30): 1:{p[5]:.1f} g/U")
print(f"CR Lunch        (11:30 - 15:00): 1:{p[6]:.1f} g/U")
print(f"CR Snack        (15:00 - 18:30): 1:{p[7]:.1f} g/U")
print(f"CR Dinner       (18:30 - 22:00): 1:{p[8]:.1f} g/U")


print("\n--- DETAILED BREAKDOWN OF FASTING CORRECTION DROPS (N=11) ---")
for i, ep in enumerate(fasting_drops):
    obs_isf = ep["drop"] / ep["bolus_u"]
    t_str = ep["time"]
    b0 = ep["bg_start"]
    bn = ep["bg_nadir"]
    dr = ep["drop"]
    bu = ep["bolus_u"]
    print(f"{i+1:2d}. {t_str} | BG: {b0:3.0f} -> {bn:3.0f} (drop {dr:3.0f} mg/dL) | Bolus: {bu:.2f} U | Observed ISF: {obs_isf:5.1f} mg/dL/U")

print("\n--- SAMPLE OF ISOLATED MEAL MASS-BALANCE EPISODES (N=18) ---")
for i, m in enumerate(meal_episodes[:8]):
    t_str = m["time"]
    cb = m["carbs"]
    b0 = m["bg0"]
    b3 = m["bg3"]
    dbg = m["delta_bg"]
    bu = m["bolus_u"]
    ba = m["actual_basal"]
    ti = m["tot_insulin"]
    print(f"{i+1:2d}. {t_str} | Carbs: {cb:4.1f}g | BG: {b0:3.0f} -> {b3:3.0f} (d={dbg:+3.0f}) | Bolus: {bu:.2f}U, Basal: {ba:.2f}U | Tot Ins: {ti:.2f}U")

init_loss = loss(p0)
opt_loss = res.fun
print(f"\nOptimization Summary: Initial Loss: {init_loss:.2f} -> Optimized Loss: {opt_loss:.2f} (Iterations: {res.nit})")
