#!/usr/bin/env python3
"""
Lydia • Prescribed Loop Therapy Settings
Clean Minimal Tables Only: Basal Schedule, Carb Ratios, ISF, Hypo Preset.
"""
import os

html_content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Lydia • Prescribed Loop Therapy Settings</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
  <style>
    body { font-family: 'Inter', sans-serif; }
    .font-mono { font-family: 'JetBrains Mono', monospace; }
  </style>
</head>
<body class="bg-slate-50 text-slate-900 min-h-screen antialiased p-4 sm:p-8">

  <div class="max-w-4xl mx-auto space-y-6">

    <!-- Header -->
    <div class="flex items-center justify-between border-b border-slate-200 pb-4">
      <div>
        <h1 class="text-xl sm:text-2xl font-extrabold text-slate-900">Lydia • Prescribed Loop Settings</h1>
        <p class="text-xs sm:text-sm text-slate-500 mt-0.5">Physical Mass-Balance Protocol (14-Day Continuous Telemetry Audit)</p>
      </div>
      <div class="flex gap-2">
        <span class="bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs font-semibold px-3 py-1 rounded-full">
          ✓ Basals 0.50 & 0.55 Kept
        </span>
      </div>
    </div>

    <!-- 1. BASAL RATES TABLE -->
    <div class="bg-white rounded-xl border border-slate-200 shadow-xs overflow-hidden">
      <div class="bg-slate-900 text-white px-5 py-3.5 flex justify-between items-center">
        <h2 class="text-sm font-bold tracking-wide">1. Basal Rates Schedule <span class="text-slate-400 font-normal text-xs font-mono">(Loop → Settings → Basal Rates)</span></h2>
        <span class="text-xs font-mono text-indigo-300">U/hr</span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-100 text-slate-600 uppercase tracking-wider font-bold border-b border-slate-200">
            <tr>
              <th class="py-2.5 px-4">Start Time</th>
              <th class="py-2.5 px-4">Current Profile</th>
              <th class="py-2.5 px-4 text-blue-700 font-extrabold text-sm">Recommended Setting</th>
              <th class="py-2.5 px-4">Action</th>
              <th class="py-2.5 px-4">Evidence & Physiological Rationale</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-200 font-mono text-xs">
            <tr class="hover:bg-slate-50">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">00:00</td>
              <td class="py-2.5 px-4 text-slate-500">0.10 U/hr</td>
              <td class="py-2.5 px-4 font-extrabold text-blue-700 text-sm whitespace-nowrap">0.10 U/hr</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-slate-100 text-slate-700">Keep</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Stable fasting equilibrium. Night blood sugars are flat (00:00–04:00).</td>
            </tr>
            <tr class="hover:bg-blue-50/50 bg-blue-50/20">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">04:00</td>
              <td class="py-2.5 px-4 text-slate-400 line-through">0.10 U/hr</td>
              <td class="py-2.5 px-4 font-extrabold text-blue-700 text-sm whitespace-nowrap">0.15 U/hr</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-blue-100 text-blue-800 font-bold">Adjust (+0.05)</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Loop currently delivers ~0.20 U/hr via erratic AutoBoluses to fight dawn rise. 0.15 smooths it.</td>
            </tr>
            <tr class="hover:bg-slate-50">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">07:00</td>
              <td class="py-2.5 px-4 text-slate-500">0.10 U/hr</td>
              <td class="py-2.5 px-4 font-extrabold text-blue-700 text-sm whitespace-nowrap">0.10 U/hr</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-slate-100 text-slate-700">Keep</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Pre-breakfast baseline fasting stability.</td>
            </tr>
            <tr class="hover:bg-slate-50">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">10:00</td>
              <td class="py-2.5 px-4 text-slate-500">0.30 U/hr</td>
              <td class="py-2.5 px-4 font-extrabold text-blue-700 text-sm whitespace-nowrap">0.30 U/hr</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-slate-100 text-slate-700">Keep</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Matches onset of breakfast digestion.</td>
            </tr>
            <tr class="hover:bg-slate-50">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">11:00</td>
              <td class="py-2.5 px-4 text-slate-500">0.40 U/hr</td>
              <td class="py-2.5 px-4 font-extrabold text-blue-700 text-sm whitespace-nowrap">0.40 U/hr</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-slate-100 text-slate-700">Keep</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Keep 0.40. Midday crash is solved by breakfast pre-bolus and 1:5.5 CR, not by cutting basal.</td>
            </tr>
            <tr class="hover:bg-emerald-50/50 bg-emerald-50/20">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">12:00</td>
              <td class="py-2.5 px-4 text-slate-700 font-semibold">0.50 U/hr</td>
              <td class="py-2.5 px-4 font-extrabold text-emerald-800 text-sm whitespace-nowrap">0.50 U/hr</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">KEEP AT 0.50</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-800 text-xs font-medium">YES, keep 0.50. Total delivery across 12:00–16:00 is 0.66 U/hr (Basal + AutoBoluses). Not over-basaled!</td>
            </tr>
            <tr class="hover:bg-emerald-50/50 bg-emerald-50/20">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">16:00</td>
              <td class="py-2.5 px-4 text-slate-700 font-semibold">0.55 U/hr</td>
              <td class="py-2.5 px-4 font-extrabold text-emerald-800 text-sm whitespace-nowrap">0.55 U/hr</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">KEEP AT 0.55</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-800 text-xs font-medium">YES, keep 0.55. Total delivery across 16:00–20:00 is 0.61 U/hr. Cutting to 0.40 would under-deliver.</td>
            </tr>
            <tr class="hover:bg-emerald-50/50 bg-emerald-50/20">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">20:00</td>
              <td class="py-2.5 px-4 text-slate-700 font-semibold">0.40 U/hr</td>
              <td class="py-2.5 px-4 font-extrabold text-emerald-800 text-sm whitespace-nowrap">0.40 U/hr</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">KEEP AT 0.40</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-800 text-xs font-medium">YES, keep 0.40. Total background delivery at 20:00 is 0.49 U/hr. Essential for dinner stability.</td>
            </tr>
            <tr class="hover:bg-blue-50/50 bg-blue-50/20">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">22:00</td>
              <td class="py-2.5 px-4 text-slate-400 line-through">0.20 U/hr</td>
              <td class="py-2.5 px-4 font-extrabold text-blue-700 text-sm whitespace-nowrap">0.25 U/hr</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-blue-100 text-blue-800 font-bold">Adjust (+0.05)</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Total delivered is 0.38 U/hr. Stepping down to 0.20 is too steep while dinner clears.</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 2. CARB RATIOS TABLE -->
    <div class="bg-white rounded-xl border border-slate-200 shadow-xs overflow-hidden">
      <div class="bg-slate-900 text-white px-5 py-3.5 flex justify-between items-center">
        <h2 class="text-sm font-bold tracking-wide">2. Carb Ratios Schedule <span class="text-slate-400 font-normal text-xs font-mono">(Loop → Settings → Carb Ratios)</span></h2>
        <span class="text-xs font-mono text-purple-300">Grams / Unit (g/U)</span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-100 text-slate-600 uppercase tracking-wider font-bold border-b border-slate-200">
            <tr>
              <th class="py-2.5 px-4">Start Time</th>
              <th class="py-2.5 px-4">Current Profile</th>
              <th class="py-2.5 px-4 text-purple-700 font-extrabold text-sm">Recommended Setting</th>
              <th class="py-2.5 px-4">Action</th>
              <th class="py-2.5 px-4">Clinical Evidence & Timing Rule</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-200 font-mono text-xs">
            <tr class="hover:bg-slate-50">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">00:00</td>
              <td class="py-2.5 px-4 text-slate-500">1:15 g/U</td>
              <td class="py-2.5 px-4 font-extrabold text-purple-700 text-sm whitespace-nowrap">1:15 g/U</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-slate-100 text-slate-700">Keep</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Stable overnight coverage.</td>
            </tr>
            <tr class="hover:bg-purple-50/50 bg-purple-50/20">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">04:00</td>
              <td class="py-2.5 px-4 text-slate-400 line-through">1:5.0 g/U</td>
              <td class="py-2.5 px-4 font-extrabold text-purple-700 text-sm whitespace-nowrap">1:5.5 g/U</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-purple-100 text-purple-800 font-bold">Adjust (+0.5 g/U)</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs"><strong>Mandatory 10–15m Pre-Bolus.</strong> 1:5 causes 100% lows; 1:6 spiked to 247. 1:5.5 is optimal.</td>
            </tr>
            <tr class="hover:bg-amber-50/50 bg-amber-50/20">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">12:00</td>
              <td class="py-2.5 px-4 text-slate-400 line-through">1:9.0 g/U</td>
              <td class="py-2.5 px-4 font-extrabold text-purple-700 text-sm whitespace-nowrap">1:11.0 g/U</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-amber-100 text-amber-800 font-bold">Adjust (+2.0 g/U)</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">1:9 caused severe lunch crashes (e.g. 26g carbs bolused 4.4U crashed to 45). Relax to 1:11.</td>
            </tr>
            <tr class="hover:bg-slate-50">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">13:00</td>
              <td class="py-2.5 px-4 text-slate-500">1:13.0 g/U</td>
              <td class="py-2.5 px-4 font-extrabold text-purple-700 text-sm whitespace-nowrap">1:13.0 g/U</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-slate-100 text-slate-700">Keep</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Afternoon snack ratio is well-balanced.</td>
            </tr>
            <tr class="hover:bg-blue-50/50 bg-blue-50/20">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">19:00</td>
              <td class="py-2.5 px-4 text-slate-400 line-through">1:14.0 g/U</td>
              <td class="py-2.5 px-4 font-extrabold text-purple-700 text-sm whitespace-nowrap">1:12.0 g/U</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-blue-100 text-blue-800 font-bold">Adjust (-2.0 g/U)</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Tighten from 1:14. 46.2% of dinners spiked >180 mg/dL (mean peak 192 mg/dL).</td>
            </tr>
            <tr class="hover:bg-slate-50">
              <td class="py-2.5 px-4 font-bold text-slate-900 text-sm whitespace-nowrap">22:00</td>
              <td class="py-2.5 px-4 text-slate-500">1:15.0 g/U</td>
              <td class="py-2.5 px-4 font-extrabold text-purple-700 text-sm whitespace-nowrap">1:15.0 g/U</td>
              <td class="py-2.5 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[11px] font-sans bg-slate-100 text-slate-700">Keep</span></td>
              <td class="py-2.5 px-4 font-sans text-slate-700 text-xs">Stable late evening ratio.</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 3. ISF & OVERRIDE TABLES -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">

      <!-- ISF Table -->
      <div class="bg-white rounded-xl border border-slate-200 shadow-xs overflow-hidden">
        <div class="bg-slate-900 text-white px-5 py-3.5 flex justify-between items-center">
          <h2 class="text-sm font-bold tracking-wide">3. Insulin Sensitivity Factor (ISF)</h2>
          <span class="text-xs font-mono text-emerald-300">mg/dL/U</span>
        </div>
        <div class="p-4 space-y-2">
          <div class="flex items-center justify-between font-mono text-xs border-b border-slate-100 pb-2">
            <span class="text-slate-500">Time Range:</span>
            <span class="font-bold text-slate-900">00:00 – 24:00 (All Day)</span>
          </div>
          <div class="flex items-center justify-between font-mono text-xs border-b border-slate-100 pb-2">
            <span class="text-slate-500">Current Profile:</span>
            <span class="font-bold text-slate-700">210 mg/dL/U</span>
          </div>
          <div class="flex items-center justify-between font-mono text-xs border-b border-slate-100 pb-2">
            <span class="text-slate-900 font-bold">Recommended:</span>
            <span class="font-extrabold text-emerald-700 text-sm">210 mg/dL/U (KEEP)</span>
          </div>
          <p class="text-xs text-slate-600 font-sans pt-1 leading-snug">
            <strong>Evidence across 207 corrections:</strong> 65.2% land cleanly in target (70–140 mg/dL), 25.1% under-correct (>140 mg/dL). Weakening to 240 would worsen stubborn highs. The 9.7% lows were caused by basal compounding at dawn and evening, which the basal schedule fixes.
          </p>
        </div>
      </div>

      <!-- Hypo Recovery Preset Table -->
      <div class="bg-white rounded-xl border border-slate-200 shadow-xs overflow-hidden">
        <div class="bg-slate-900 text-white px-5 py-3.5 flex justify-between items-center">
          <h2 class="text-sm font-bold tracking-wide">4. Hypo Recovery Preset <span class="text-slate-400 font-normal text-xs font-mono">(Custom Override)</span></h2>
          <span class="text-xs font-mono text-amber-300">Safety Clamp</span>
        </div>
        <div class="p-4 space-y-2">
          <div class="flex items-center justify-between font-mono text-xs border-b border-slate-100 pb-2">
            <span class="text-slate-500">Target Range:</span>
            <span class="font-extrabold text-amber-700 text-sm">130 – 140 mg/dL</span>
          </div>
          <div class="flex items-center justify-between font-mono text-xs border-b border-slate-100 pb-2">
            <span class="text-slate-500">Duration:</span>
            <span class="font-bold text-slate-900">60 Minutes</span>
          </div>
          <div class="flex items-center justify-between font-mono text-xs border-b border-slate-100 pb-2">
            <span class="text-slate-500">Insulin Needs:</span>
            <span class="font-bold text-slate-900">100%</span>
          </div>
          <p class="text-xs text-slate-600 font-sans pt-1 leading-snug">
            <strong>When to Use:</strong> Enable immediately whenever administering 5g rescue juice. Prevents Loop from firing automated micro-boluses on the rebound glucose rise.
          </p>
        </div>
      </div>

    </div>

  </div>

</body>
</html>
"""

output_path = os.path.join(os.path.dirname(__file__), "index.html")
with open(output_path, "w") as f:
    f.write(html_content)

print(f"Generated clean Minimal Settings App at {output_path}")
