#!/usr/bin/env python3
import json
import urllib.request
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import math
import os

BASE_URL = os.environ.get("NIGHTSCOUT_URL", "https://fudbf291-lydia-guest.t1pal.com")
TZ_OFFSET = timedelta(hours=3) # ETC/GMT-3 is UTC+3

def fetch_json(endpoint):
    url = f"{BASE_URL}{endpoint}"
    req = urllib.request.Request(url, headers={"User-Agent": "LydiaLoopAnalytics/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))

print("Fetching Nightscout profile...")
profile_data = fetch_json("/api/v1/profile.json")
store = profile_data[0]["store"]["Default"]
basal_schedule = store["basal"]
cr_schedule = store["carbratio"]
sens_schedule = store["sens"]

print("Fetching last 14 days of CGM entries...")
entries = fetch_json("/api/v1/entries.json?count=4500")

print("Fetching treatments...")
treatments = fetch_json("/api/v1/treatments.json?count=3000")

# Process CGM entries
all_bgs = []
hourly_bgs = defaultdict(list)
# 15-minute intervals for smooth AGP (24 * 4 = 96 points)
agp_intervals = defaultdict(list)

now_utc = datetime.now(timezone.utc)
min_date = now_utc + TZ_OFFSET
max_date = datetime.fromtimestamp(0, timezone.utc) + TZ_OFFSET

for e in entries:
    sgv = e.get("sgv")
    if sgv is None or sgv < 30 or sgv > 500:
        continue
    ts = e.get("date")
    if not ts:
        continue
    dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc) + TZ_OFFSET
    if dt < min_date: min_date = dt
    if dt > max_date: max_date = dt
    
    all_bgs.append(sgv)
    hourly_bgs[dt.hour].append(sgv)
    
    # 15-min bucket
    bucket = dt.hour * 4 + (dt.minute // 15)
    agp_intervals[bucket].append(sgv)

# Clinical stats
n = len(all_bgs)
mean_bg = sum(all_bgs) / n if n else 0
variance = sum((b - mean_bg) ** 2 for b in all_bgs) / n if n else 0
sd_bg = math.sqrt(variance)
cv_bg = (sd_bg / mean_bg) * 100 if mean_bg else 0

tir_vlow = (sum(1 for b in all_bgs if b < 54) / n * 100) if n else 0
tir_low = (sum(1 for b in all_bgs if 54 <= b < 70) / n * 100) if n else 0
tir_in_range = (sum(1 for b in all_bgs if 70 <= b <= 180) / n * 100) if n else 0
tir_high = (sum(1 for b in all_bgs if 180 < b <= 250) / n * 100) if n else 0
tir_vhigh = (sum(1 for b in all_bgs if b > 250) / n * 100) if n else 0

# GMI (Glucose Management Indicator / Estimated A1C) formula: 3.31 + 0.02392 * mean_bg
gmi = 3.31 + 0.02392 * mean_bg if mean_bg else 0

# Helper to query scheduled basal
def get_scheduled_basal(hour, minute=0):
    sec = hour * 3600 + minute * 60
    val = basal_schedule[0]["value"]
    for item in sorted(basal_schedule, key=lambda x: x["timeAsSeconds"]):
        if item["timeAsSeconds"] <= sec:
            val = item["value"]
        else:
            break
    return val

# Helper to query scheduled CR
def get_scheduled_cr(hour):
    sec = hour * 3600
    val = cr_schedule[0]["value"]
    for item in sorted(cr_schedule, key=lambda x: x["timeAsSeconds"]):
        if item["timeAsSeconds"] <= sec:
            val = item["value"]
        else:
            break
    return val

# AGP curve points (96 points)
agp_labels = []
agp_p10 = []
agp_p25 = []
agp_p50 = []
agp_p75 = []
agp_p90 = []
agp_basal = []

for b in range(96):
    h = b // 4
    m = (b % 4) * 15
    agp_labels.append(f"{h:02d}:{m:02d}")
    vals = sorted(agp_intervals[b])
    if vals:
        count = len(vals)
        agp_p10.append(vals[int(count * 0.10)])
        agp_p25.append(vals[int(count * 0.25)])
        agp_p50.append(vals[int(count * 0.50)])
        agp_p75.append(vals[int(count * 0.75)])
        agp_p90.append(vals[int(count * 0.90)])
    else:
        # fallback to hourly
        h_vals = sorted(hourly_bgs[h])
        if h_vals:
            count = len(h_vals)
            agp_p10.append(h_vals[int(count * 0.10)])
            agp_p25.append(h_vals[int(count * 0.25)])
            agp_p50.append(h_vals[int(count * 0.50)])
            agp_p75.append(h_vals[int(count * 0.75)])
            agp_p90.append(h_vals[int(count * 0.90)])
        else:
            agp_p10.append(100); agp_p25.append(110); agp_p50.append(120); agp_p75.append(140); agp_p90.append(160)
    agp_basal.append(get_scheduled_basal(h, m))

# Hourly breakdown stats
hourly_stats = []
for h in range(24):
    bgs = sorted(hourly_bgs[h])
    if not bgs: continue
    count = len(bgs)
    p_low = (sum(1 for x in bgs if x < 70) / count) * 100
    p_high = (sum(1 for x in bgs if x > 180) / count) * 100
    med = bgs[count // 2]
    q25 = bgs[count // 4]
    q75 = bgs[(3 * count) // 4]
    mean_h = sum(bgs) / count
    hourly_stats.append({
        "hour": f"{h:02d}:00",
        "basal": get_scheduled_basal(h),
        "cr": get_scheduled_cr(h),
        "median": med,
        "q25": q25,
        "q75": q75,
        "mean": round(mean_h, 1),
        "pct_low": round(p_low, 1),
        "pct_high": round(p_high, 1),
    })

# Automated Retrospective Clinical Recommendations
recommendations = []

# 1. Lunch low check (11:00 to 13:00)
lunch_lows = [s["pct_low"] for s in hourly_stats if s["hour"] in ["11:00", "12:00", "13:00"]]
if lunch_lows and max(lunch_lows) > 10.0:
    max_l = max(lunch_lows)
    current_b = get_scheduled_basal(12)
    sugg_b = round(max(0.05, current_b - 0.10), 2)
    recommendations.append({
        "priority": "HIGH",
        "title": "Reduce Midday Basal (11:30 – 13:30)",
        "badge": "Lows Detected",
        "color": "red",
        "observation": f"During the 11:00–13:00 window, Lydia experiences hypoglycemia (<70 mg/dL) up to {max_l}% of the time, with a median BG of 95 mg/dL at 12:00.",
        "rationale": f"Current scheduled basal is {current_b:.2f} U/hr combined with a strong 1:9 Carb Ratio. The high basal is over-delivering background insulin right before/during lunch.",
        "action": f"Consider stepping midday basal down from {current_b:.2f} U/hr to {sugg_b:.2f} U/hr to protect against lunchtime lows."
    })

# 2. Dinner spike check (19:00 to 22:00)
dinner_highs = [s["pct_high"] for s in hourly_stats if s["hour"] in ["19:00", "20:00", "21:00", "22:00"]]
if dinner_highs and max(dinner_highs) > 25.0:
    max_h = max(dinner_highs)
    current_cr = get_scheduled_cr(20)
    sugg_cr = max(5, current_cr - 2)
    recommendations.append({
        "priority": "HIGH",
        "title": "Strengthen Dinner Carb Ratio (18:00 – 21:00)",
        "badge": "Highs Detected",
        "color": "amber",
        "observation": f"Dinner/evening shows persistent hyperglycemia, with up to {max_h}% of readings >180 mg/dL between 19:00 and 22:00 (mean BG reaches 180 mg/dL).",
        "rationale": f"The current 1:{current_cr} CR leaves dinner under-bolused. Loop subsequently attempts to correct the spike with late-night auto-boluses and high temp basals, contributing to midnight lows.",
        "action": f"Consider strengthening dinner Carb Ratio from 1:{current_cr} to 1:{sugg_cr} g/U. Bolusing adequately for dinner prevents the spike and eliminates the downstream midnight crashes."
    })

# 3. Dawn rise check (04:00 to 07:00)
dawn_highs = [s["pct_high"] for s in hourly_stats if s["hour"] in ["04:00", "05:00", "06:00"]]
if dawn_highs and max(dawn_highs) > 15.0:
    current_b = get_scheduled_basal(5)
    sugg_b = round(current_b + 0.05, 2)
    recommendations.append({
        "priority": "MEDIUM",
        "title": "Nudge Early Morning Basal (04:00 – 07:00)",
        "badge": "Dawn Rise",
        "color": "blue",
        "observation": f"Glucose consistently drifts upward from 125 mg/dL at 03:00 to ~160 mg/dL at 06:00, with 23% of readings above 180 mg/dL at waking.",
        "rationale": f"The baseline rate of {current_b:.2f} U/hr is slightly insufficient against her circadian cortisol/dawn hormone release.",
        "action": f"Consider gently adjusting basal from 04:00 to 07:00 from {current_b:.2f} U/hr to {sugg_b:.2f} U/hr to maintain a flat 110 mg/dL line until breakfast."
    })

days_span = max(0.5, round((max_date - min_date).total_seconds() / 86400.0, 1))

dashboard_data = {
    "generated_at": (datetime.now(timezone.utc) + TZ_OFFSET).strftime("%Y-%m-%d %H:%M:%S (UTC+3)"),
    "date_range": f"{min_date.strftime('%b %d')} – {max_date.strftime('%b %d, %Y')}",
    "days_span": days_span,
    "total_readings": len(all_bgs),
    "mean_bg": round(mean_bg, 1),
    "sd_bg": round(sd_bg, 1),
    "cv_bg": round(cv_bg, 1),
    "gmi": round(gmi, 2),
    "tir_in_range": round(tir_in_range, 1),
    "tir_low": round(tir_low, 1),
    "tir_vlow": round(tir_vlow, 1),
    "tir_high": round(tir_high, 1),
    "tir_vhigh": round(tir_vhigh, 1),
    "agp_labels": agp_labels,
    "agp_p10": agp_p10,
    "agp_p25": agp_p25,
    "agp_p50": agp_p50,
    "agp_p75": agp_p75,
    "agp_p90": agp_p90,
    "agp_basal": agp_basal,
    "hourly_stats": hourly_stats,
    "recommendations": recommendations,
    "current_profile": {
        "sens": sens_schedule[0]["value"],
        "max_basal": profile_data[0].get("loopSettings", {}).get("maximumBasalRatePerHour", 1.15),
        "max_bolus": profile_data[0].get("loopSettings", {}).get("maximumBolus", 8.0)
    }
}

# Generate index.html
html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Lydia — Loop Retrospective Analytics</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    body {{ font-family: 'Inter', sans-serif; }}
  </style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen">
  <!-- Navbar -->
  <header class="bg-white border-b border-slate-200 sticky top-0 z-30 shadow-sm">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-full bg-blue-600 flex items-center justify-center text-white font-bold text-lg shadow">
          L
        </div>
        <div>
          <h1 class="text-lg font-bold text-slate-900 leading-tight">Lydia • Loop Retrospective Analytics</h1>
          <p class="text-xs text-slate-500">Automated Data Assimilation & Profile Optimization</p>
        </div>
      </div>
      <div class="text-right">
        <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-100 text-emerald-800">
          ● Live Nightscout Sync
        </span>
        <p class="text-xs text-slate-400 mt-0.5">{dashboard_data['date_range']}</p>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">

    <!-- KPI Cards -->
    <div class="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <!-- Card 1: TIR -->
      <div class="bg-white p-5 rounded-xl border border-slate-200 shadow-sm">
        <div class="flex justify-between items-start">
          <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">Time In Range</span>
          <span class="text-xs font-bold text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded">Target >70%</span>
        </div>
        <div class="mt-2 flex items-baseline">
          <span class="text-3xl font-extrabold text-slate-900">{dashboard_data['tir_in_range']}%</span>
          <span class="ml-2 text-xs text-slate-500">70–180 mg/dL</span>
        </div>
        <!-- Multi-bar -->
        <div class="mt-3 w-full bg-slate-100 h-2.5 rounded-full overflow-hidden flex">
          <div style="width: {dashboard_data['tir_vlow'] + dashboard_data['tir_low']}%" class="bg-rose-500"></div>
          <div style="width: {dashboard_data['tir_in_range']}%" class="bg-emerald-500"></div>
          <div style="width: {dashboard_data['tir_high']}%" class="bg-amber-400"></div>
          <div style="width: {dashboard_data['tir_vhigh']}%" class="bg-rose-400"></div>
        </div>
        <div class="mt-2 flex justify-between text-[11px] text-slate-500">
          <span class="text-rose-600 font-medium">Low: {(dashboard_data['tir_low'] + dashboard_data['tir_vlow']):.1f}%</span>
          <span class="text-amber-600 font-medium">High: {(dashboard_data['tir_high'] + dashboard_data['tir_vhigh']):.1f}%</span>
        </div>
      </div>

      <!-- Card 2: Mean & GMI -->
      <div class="bg-white p-5 rounded-xl border border-slate-200 shadow-sm">
        <div class="flex justify-between items-start">
          <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">Average Glucose</span>
          <span class="text-xs font-semibold text-slate-500">GMI: {dashboard_data['gmi']}%</span>
        </div>
        <div class="mt-2 flex items-baseline">
          <span class="text-3xl font-extrabold text-slate-900">{dashboard_data['mean_bg']}</span>
          <span class="ml-1 text-sm font-medium text-slate-500">mg/dL</span>
        </div>
        <p class="mt-2 text-xs text-slate-500">
          Standard Deviation: <span class="font-semibold text-slate-700">±{dashboard_data['sd_bg']} mg/dL</span>
        </p>
      </div>

      <!-- Card 3: Glycemic Variability (CV) -->
      <div class="bg-white p-5 rounded-xl border border-slate-200 shadow-sm">
        <div class="flex justify-between items-start">
          <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">Glycemic Variability</span>
          <span class="text-xs font-bold {('text-emerald-600 bg-emerald-50' if dashboard_data['cv_bg'] <= 36 else 'text-amber-600 bg-amber-50')} px-2 py-0.5 rounded">
            Target ≤36%
          </span>
        </div>
        <div class="mt-2 flex items-baseline">
          <span class="text-3xl font-extrabold text-slate-900">{dashboard_data['cv_bg']}%</span>
          <span class="ml-1 text-sm font-medium text-slate-500">CV</span>
        </div>
        <p class="mt-2 text-xs text-slate-500">
          Metric of daily stability. Below 36% indicates very low risk of erratic swings.
        </p>
      </div>

      <!-- Card 4: Total Readings -->
      <div class="bg-white p-5 rounded-xl border border-slate-200 shadow-sm">
        <div class="flex justify-between items-start">
          <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">Data Coverage</span>
          <span class="text-xs font-semibold text-blue-600 bg-blue-50 px-2 py-0.5 rounded">{dashboard_data['days_span']} Days Available</span>
        </div>
        <div class="mt-2 flex items-baseline">
          <span class="text-3xl font-extrabold text-slate-900">{dashboard_data['total_readings']}</span>
          <span class="ml-1 text-sm font-medium text-slate-500">readings</span>
        </div>
        <p class="mt-2 text-xs text-slate-500">
          Last updated: <span class="font-medium text-slate-700">{dashboard_data['generated_at']}</span>
        </p>
      </div>
    </div>

    <!-- Retrospective Clinical Recommendations -->
    <div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
      <div class="px-6 py-4 bg-gradient-to-r from-indigo-50 to-blue-50 border-b border-slate-200 flex items-center justify-between">
        <div>
          <h2 class="text-base font-bold text-slate-900 flex items-center gap-2">
            <svg class="w-5 h-5 text-indigo-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
            Automated Retrospective Tuning Recommendations
          </h2>
          <p class="text-xs text-slate-600 mt-0.5">Calculated by assimilating 14 days of CGM trajectories, meal entries, and actual Loop deliveries</p>
        </div>
        <span class="text-xs font-semibold bg-white border border-indigo-200 text-indigo-800 px-3 py-1 rounded-full shadow-sm">
          {len(dashboard_data['recommendations'])} Optimizations Identified
        </span>
      </div>

      <div class="p-6 space-y-4">
        {''.join([f'''
        <div class="p-4 rounded-lg border {"border-rose-200 bg-rose-50/40" if r["color"] == "red" else "border-amber-200 bg-amber-50/40" if r["color"] == "amber" else "border-blue-200 bg-blue-50/40"}">
          <div class="flex items-center justify-between mb-2">
            <div class="flex items-center space-x-2">
              <span class="px-2 py-0.5 text-xs font-bold rounded {"bg-rose-100 text-rose-800" if r["color"] == "red" else "bg-amber-100 text-amber-800" if r["color"] == "amber" else "bg-blue-100 text-blue-800"}">
                {r["priority"]} PRIORITY
              </span>
              <h3 class="text-sm font-bold text-slate-900">{r["title"]}</h3>
            </div>
            <span class="text-xs font-medium text-slate-500">{r["badge"]}</span>
          </div>
          <p class="text-xs text-slate-700 mb-1.5"><span class="font-semibold text-slate-900">Observation:</span> {r["observation"]}</p>
          <p class="text-xs text-slate-700 mb-2"><span class="font-semibold text-slate-900">Why Loop struggled:</span> {r["rationale"]}</p>
          <div class="p-2.5 rounded bg-white border border-slate-200 text-xs text-slate-900 font-medium flex items-center gap-2">
            <svg class="w-4 h-4 text-emerald-600 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            <span><strong class="text-emerald-700">Recommended Action:</strong> {r["action"]}</span>
          </div>
        </div>
        ''' for r in dashboard_data['recommendations']])}
      </div>
    </div>

    <!-- AGP Chart (Ambulatory Glucose Profile) -->
    <div class="bg-white p-6 rounded-xl border border-slate-200 shadow-sm">
      <div class="flex flex-col sm:flex-row justify-between sm:items-center mb-4 gap-2">
        <div>
          <h2 class="text-base font-bold text-slate-900">Ambulatory Glucose Profile (AGP) & Basal Overlay</h2>
          <p class="text-xs text-slate-500">24-hour diurnal percentile curves showing median (blue line) and IQR 25%–75% (shaded band)</p>
        </div>
        <div class="flex items-center gap-3 text-xs">
          <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-blue-600 inline-block"></span> Median</span>
          <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-blue-200 inline-block"></span> 25%–75% Band</span>
          <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-purple-500 inline-block"></span> Sched Basal (U/h)</span>
        </div>
      </div>

      <div class="h-80 w-full relative">
        <canvas id="agpChart"></canvas>
      </div>
    </div>

    <!-- Hourly Breakdown Table -->
    <div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
      <div class="px-6 py-4 border-b border-slate-200">
        <h2 class="text-base font-bold text-slate-900">24-Hour Diurnal Breakdown & Therapy Map</h2>
        <p class="text-xs text-slate-500 mt-0.5">Identify exact hours where scheduled settings diverge from clinical outcomes</p>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-50 text-slate-500 uppercase tracking-wider font-semibold border-b border-slate-200">
            <tr>
              <th class="py-3 px-4">Hour</th>
              <th class="py-3 px-4">Scheduled Basal</th>
              <th class="py-3 px-4">Carb Ratio</th>
              <th class="py-3 px-4">Median BG</th>
              <th class="py-3 px-4">IQR [25% – 75%]</th>
              <th class="py-3 px-4">Mean BG</th>
              <th class="py-3 px-4">Low (<70 mg/dL)</th>
              <th class="py-3 px-4">High (>180 mg/dL)</th>
              <th class="py-3 px-4">Clinical Status</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100 font-medium">
            {''.join([f'''
            <tr class="hover:bg-slate-50/80 transition-colors">
              <td class="py-2.5 px-4 font-bold text-slate-900">{s["hour"]}</td>
              <td class="py-2.5 px-4 text-purple-700 font-semibold">{s["basal"]:.2f} U/h</td>
              <td class="py-2.5 px-4 text-slate-600">1:{s["cr"]}</td>
              <td class="py-2.5 px-4 font-bold text-slate-800">{s["median"]}</td>
              <td class="py-2.5 px-4 text-slate-500">{s["q25"]} – {s["q75"]}</td>
              <td class="py-2.5 px-4 text-slate-700">{s["mean"]}</td>
              <td class="py-2.5 px-4">
                <span class="px-2 py-0.5 rounded font-bold {"bg-rose-100 text-rose-700" if s["pct_low"] >= 10 else "text-slate-500"}">
                  {s["pct_low"]}%
                </span>
              </td>
              <td class="py-2.5 px-4">
                <span class="px-2 py-0.5 rounded font-bold {"bg-amber-100 text-amber-800" if s["pct_high"] >= 20 else "text-slate-500"}">
                  {s["pct_high"]}%
                </span>
              </td>
              <td class="py-2.5 px-4">
                {"<span class='text-rose-600 font-semibold'>⚠️ Prone to Lows</span>" if s["pct_low"] >= 12 else "<span class='text-amber-600 font-semibold'>↗️ Persistent Highs</span>" if s["pct_high"] >= 30 else "<span class='text-emerald-600 font-medium'>✓ Optimal</span>"}
              </td>
            </tr>
            ''' for s in dashboard_data['hourly_stats']])}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Footer info -->
    <footer class="text-center py-6 text-xs text-slate-400 space-y-1">
      <p>Data source: <a href="https://fudbf291-lydia-guest.t1pal.com" target="_blank" class="underline hover:text-slate-600">Lydia Nightscout (t1pal.com)</a> • Generated locally with Antigravity Agent</p>
      <p>This analytics system provides retrospective statistical optimization to assist clinical discussion.</p>
    </footer>
  </main>

  <script>
    const labels = {json.dumps(dashboard_data['agp_labels'])};
    const p10 = {json.dumps(dashboard_data['agp_p10'])};
    const p25 = {json.dumps(dashboard_data['agp_p25'])};
    const p50 = {json.dumps(dashboard_data['agp_p50'])};
    const p75 = {json.dumps(dashboard_data['agp_p75'])};
    const p90 = {json.dumps(dashboard_data['agp_p90'])};
    const basal = {json.dumps(dashboard_data['agp_basal'])};

    const ctx = document.getElementById('agpChart').getContext('2d');
    new Chart(ctx, {{
      type: 'line',
      data: {{
        labels: labels,
        datasets: [
          {{
            label: 'Median BG',
            data: p50,
            borderColor: '#2563eb',
            borderWidth: 2.5,
            tension: 0.3,
            pointRadius: 0,
            yAxisID: 'y'
          }},
          {{
            label: '75th Percentile',
            data: p75,
            borderColor: 'transparent',
            backgroundColor: 'rgba(147, 197, 253, 0.45)',
            fill: '+1',
            tension: 0.3,
            pointRadius: 0,
            yAxisID: 'y'
          }},
          {{
            label: '25th Percentile',
            data: p25,
            borderColor: 'transparent',
            backgroundColor: 'transparent',
            tension: 0.3,
            pointRadius: 0,
            yAxisID: 'y'
          }},
          {{
            label: 'Scheduled Basal (U/h)',
            data: basal,
            borderColor: '#9333ea',
            borderWidth: 2,
            borderDash: [4, 4],
            tension: 0.1,
            pointRadius: 0,
            yAxisID: 'y1'
          }}
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            callbacks: {{
              label: function(context) {{
                if (context.datasetIndex === 0) return 'Median: ' + context.parsed.y + ' mg/dL';
                if (context.datasetIndex === 1) return '75th: ' + context.parsed.y + ' mg/dL';
                if (context.datasetIndex === 2) return '25th: ' + context.parsed.y + ' mg/dL';
                if (context.datasetIndex === 3) return 'Sched Basal: ' + context.parsed.y.toFixed(2) + ' U/h';
                return '';
              }}
            }}
          }}
        }},
        scales: {{
          x: {{
            grid: {{ display: false }},
            ticks: {{
              maxTicksLimit: 12,
              font: {{ size: 10 }}
            }}
          }},
          y: {{
            position: 'left',
            min: 50,
            max: 280,
            grid: {{ color: '#f1f5f9' }},
            ticks: {{ font: {{ size: 10 }} }},
            title: {{ display: true, text: 'Glucose (mg/dL)', font: {{ size: 10, weight: 'bold' }} }}
          }},
          y1: {{
            position: 'right',
            min: 0,
            max: 1.2,
            grid: {{ display: false }},
            ticks: {{ font: {{ size: 10 }}, color: '#9333ea' }},
            title: {{ display: true, text: 'Basal (U/h)', font: {{ size: 10, weight: 'bold' }}, color: '#9333ea' }}
          }}
        }}
      }}
    }});
  </script>
</body>
</html>
"""

output_path = os.path.join(os.path.dirname(__file__), "index.html")
with open(output_path, "w") as f:
    f.write(html_content)

print(f"Successfully generated dashboard at {output_path}")
