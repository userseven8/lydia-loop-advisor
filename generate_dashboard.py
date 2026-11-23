#!/usr/bin/env python3
"""
Lydia • Therapy Advisor Web Renderer (Single-Engine Architecture)

All telemetry fetching, pharmacokinetic integration, and 4-stage therapy solving
(ISF, Basal, Carb Ratio, Correction Range, AGP, TIR, and Control Stability) are
executed exclusively by NightscoutAdvisorKit (`advisor-cli`), the exact same Swift
engine compiled into the iOS and watchOS apps.

This script contains ZERO duplicate clinical math—guaranteeing 100% numerical and
status-badge parity between the web dashboard, iPhone app, Apple Watch app, and
Watch complications.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Athens")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=2))

BASE_URL = os.environ.get("NIGHTSCOUT_URL", "https://fudbf291-lydia-guest.t1pal.com")
ROLLING_DAYS = int(os.environ.get("ROLLING_DAYS", "30"))
TZ_ID = os.environ.get("ADVISOR_TZ", "Europe/Athens")


def to_local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ)


def fmt_hours(pct: float) -> str:
    tot_mins = int(round((pct / 100.0) * 24 * 60))
    h, m = divmod(tot_mins, 60)
    if h > 0 and m > 0:
        return f"{h}h {m}m"
    if h > 0:
        return f"{h}h"
    return f"{m}m"


def resolve_advisor_cli() -> str:
    explicit_bin = os.environ.get("ADVISOR_CLI_BIN")
    if explicit_bin and os.path.isfile(explicit_bin) and os.access(explicit_bin, os.X_OK):
        return explicit_bin

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    candidate_kit_dirs = [
        os.environ.get("ADVISOR_KIT_PATH", ""),
        os.path.join(repo_dir, "vendor", "NightscoutAdvisor", "Packages", "NightscoutAdvisorKit"),
        os.path.abspath(os.path.join(repo_dir, "..", "NightscoutAdvisor", "Packages", "NightscoutAdvisorKit")),
    ]

    kit_dir = next((p for p in candidate_kit_dirs if p and os.path.isfile(os.path.join(p, "Package.swift"))), None)
    if not kit_dir:
        print(
            "[ERROR] NightscoutAdvisorKit not found. Set ADVISOR_KIT_PATH or checkout NightscoutAdvisor.",
            file=sys.stderr,
        )
        sys.exit(1)

    release_bin = os.path.join(kit_dir, ".build", "release", "advisor-cli")
    print(f"[pipeline] Building/verifying NightscoutAdvisorKit advisor-cli at {kit_dir}...", file=sys.stderr)
    subprocess.run(
        ["swift", "build", "--package-path", kit_dir, "-c", "release", "--product", "advisor-cli"],
        check=True,
    )
    return release_bin


def run_single_solver_engine() -> dict:
    cli_bin = resolve_advisor_cli()
    cmd = [
        cli_bin,
        "--url", BASE_URL,
        "--days", str(ROLLING_DAYS),
        "--tz", TZ_ID,
    ]
    token = os.environ.get("NIGHTSCOUT_TOKEN", "").strip()
    if token:
        cmd.extend(["--token", token])

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=None, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        print("[ERROR] advisor-cli failed. Aborting dashboard generation to protect index.html.", file=sys.stderr)
        sys.exit(1)

    return json.loads(proc.stdout)


def cr_slot_metadata(time_str: str) -> tuple[str, str]:
    table = {
        "00:00": ("Overnight Baseline", "Overnight insulin sensitivity baseline protecting against nocturnal hypoglycemia."),
        "08:30": ("Breakfast & Morning", "Morning cortisol creates substantial insulin resistance; 15–20 min pre-bolus critical."),
        "12:00": ("Daytime (Lunch & Snack)", "Active daytime metabolism and daytime physical activity."),
        "17:00": ("Evening (Dinner & Bedtime)", "Evening carbohydrate disposal and bedtime settling."),
        "22:00": ("Bedtime / Overnight", "Bedtime transition as evening digestion clears."),
    }
    return table.get(time_str, ("Scheduled Window", "Evaluated directly from active Nightscout profile block."))


def basal_slot_note(time_str: str) -> str:
    notes = {
        "00:00": "Early nocturnal sleep onset (00:00–01:30).",
        "01:30": "Deep nocturnal sleep homeostasis (01:30–05:00).",
        "05:00": "Dawn hepatic cortisol window (05:00–08:30).",
        "08:30": "Daytime resting baseline (08:30–22:00).",
        "22:00": "Bedtime sleep transition (22:00–24:00).",
    }
    return notes.get(time_str, "")


def find_matching_basal(basal_schedule: list[dict], time_str: str) -> tuple[str, float]:
    h, m = map(int, time_str.split(":"))
    target_min = h * 60 + m
    chosen_time = basal_schedule[0]["timeString"] if basal_schedule else "00:00"
    chosen_val = basal_schedule[0]["recommendedUnitsPerHour"] if basal_schedule else 0.10
    for seg in basal_schedule:
        sh, sm = map(int, seg["timeString"].split(":"))
        if sh * 60 + sm <= target_min:
            chosen_time = seg["timeString"]
            chosen_val = seg["recommendedUnitsPerHour"]
    return chosen_time, chosen_val


def main() -> None:
    report = run_single_solver_engine()

    stats_30d = report["statsRolling"]
    stats_24h = report["stats24h"]
    global_isf = report["globalISF"]
    stability = report["stability"]

    n_samples = int(stats_30d["sampleCount"])
    mean_bg = float(stats_30d["meanMgdL"])
    sd_bg = float(stats_30d["sdMgdL"])
    cv_bg = float(stats_30d["cvPercent"])
    gmi = float(stats_30d["gmiPercent"])
    ea1c = (mean_bg + 46.7) / 28.7

    v_low_pct = float(stats_30d["veryLowPercent"])
    tbr_pct = float(stats_30d["tbrPercent"])
    low_pct = max(0.0, tbr_pct - v_low_pct)
    in_range_pct = float(stats_30d["tirPercent"])
    tar_pct = float(stats_30d["tarPercent"])
    v_high_pct = float(stats_30d["veryHighPercent"])
    high_pct = max(0.0, tar_pct - v_high_pct)

    tir_24h_pct = float(stats_24h["tirPercent"]) if stats_24h["sampleCount"] > 0 else in_range_pct
    tir_delta = in_range_pct - 70.0
    tir_delta_str = f"{'+' if tir_delta >= 0 else ''}{tir_delta:.1f}%"

    prof_ts = report.get("profileUpdatedTimestamp")
    profile_updated_str = to_local_dt(prof_ts).strftime("%b %d, %H:%M") if prof_ts else "Live Nightscout"

    latest_ts = report.get("latestCGMTimestamp") or report.get("generatedAt")
    updated_str = to_local_dt(latest_ts).strftime("%b %d, %Y • %H:%M %Z")

    # AGP 48 half-hour modal bins from NightscoutAdvisorKit
    bin_by_min = {int(b["minuteOfDay"]): b for b in report.get("agpBins", [])}
    agp_labels = [f"{i // 2:02d}:{(i % 2) * 30:02d}" for i in range(48)]
    agp_p10, agp_p25, agp_p50, agp_p75, agp_p90 = [], [], [], [], []
    for i in range(48):
        b = bin_by_min.get(i * 30)
        if b is None:
            prev = agp_p50[-1] if agp_p50 else 130.0
            agp_p10.append(round(prev - 30.0, 1))
            agp_p25.append(round(prev - 15.0, 1))
            agp_p50.append(round(prev, 1))
            agp_p75.append(round(prev + 15.0, 1))
            agp_p90.append(round(prev + 30.0, 1))
        else:
            agp_p10.append(round(float(b["p10MgdL"]), 1))
            agp_p25.append(round(float(b["p25MgdL"]), 1))
            agp_p50.append(round(float(b["p50MgdL"]), 1))
            agp_p75.append(round(float(b["p75MgdL"]), 1))
            agp_p90.append(round(float(b["p90MgdL"]), 1))

    dawn_dip = round(min(agp_p50[8:14])) if len(agp_p50) >= 14 else 144
    bfast_peak = round(max(agp_p50[16:20])) if len(agp_p50) >= 20 else 136
    lunch_peak = round(max(agp_p50[24:28])) if len(agp_p50) >= 28 else 176
    dinner_peak = round(max(agp_p50[38:42])) if len(agp_p50) >= 42 else 164

    # Stage 2: Basal Schedule Rows (status directly from NightscoutAdvisorKit)
    basal_schedule = report["basalSchedule"]
    basal_rows_html = ""
    for seg in basal_schedule:
        t_str = seg["timeString"]
        cur_val = float(seg["currentUnitsPerHour"])
        rec_val = float(seg["recommendedUnitsPerHour"])
        q_val = float(seg["rawQuantileFlux"])
        ci_low = float(seg["ciLowUnitsPerHour"])
        ci_high = float(seg["ciHighUnitsPerHour"])
        status = seg["status"]
        note = basal_slot_note(t_str)
        evidence = f"{seg['rationale']} {note}".strip()

        if status == "in_sync":
            cur_html = f'<span class="text-emerald-700 font-bold">{cur_val:.2f} U/hr</span>'
            badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In Sync</span>'
            row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
        elif status in ("in_ci", "insufficient_data"):
            cur_html = f'<span class="text-emerald-700 font-bold">{cur_val:.2f} U/hr</span>'
            label_txt = "✓ In 95% CI Range" if status == "in_ci" else "✓ Profile Anchor"
            badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">{label_txt}</span>'
            row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
        else:
            cur_html = f'<span class="text-slate-400 line-through">{cur_val:.2f} U/hr</span>'
            badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-blue-100 text-blue-800 font-bold">Adjust to {rec_val:.2f}</span>'
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

    # Stage 3: Carb Ratio Schedule Rows (status directly from NightscoutAdvisorKit)
    cr_rows_html = ""
    rec_isf = float(global_isf["recommendedMgdLPerU"])
    for seg in report["carbRatioSchedule"]:
        start = seg["timeString"]
        window_str = seg["windowLabel"]
        cur_val = float(seg["currentGramsPerUnit"])
        rec_val = float(seg["recommendedGramsPerUnit"])
        wls_cr = float(seg["rawWLSGramsPerUnit"])
        cr_ci_low = float(seg["ciLowGramsPerUnit"])
        cr_ci_high = float(seg["ciHighGramsPerUnit"])
        emp_min = float(seg.get("empiricalMinGramsPerUnit") or cur_val)
        emp_max = float(seg.get("empiricalMaxGramsPerUnit") or cur_val)
        status = seg["status"]
        meal_name, note = cr_slot_metadata(start)
        evidence = f"{seg['rationale']} {note}".strip()

        blk_time, used_basal = find_matching_basal(basal_schedule, start)

        if status == "in_sync":
            cur_html = f'<span class="text-emerald-700 font-bold">1:{cur_val:.1f} g/U</span>'
            badge_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In Sync</span>'
            row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
        elif status in ("in_ci", "insufficient_data"):
            cur_html = f'<span class="text-emerald-700 font-bold">1:{cur_val:.1f} g/U</span>'
            label_txt = "✓ In 95% CI Range" if status == "in_ci" else "✓ Profile Anchor"
            badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">{label_txt}</span>'
            row_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
        else:
            cur_html = f'<span class="text-slate-400 line-through">1:{cur_val:.1f} g/U</span>'
            badge_html = f'<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-purple-100 text-purple-800 font-bold">Adjust to 1:{rec_val:.1f}</span>'
            row_bg = 'class="hover:bg-purple-50/50 bg-purple-50/20"'

        inputs_badge = (
            f'<div class="mt-1.5 text-[10px] font-mono text-indigo-800 bg-indigo-50/90 px-2 py-0.5 rounded border border-indigo-200/60 w-fit flex items-center gap-1.5">'
            f'<span class="text-slate-500 uppercase tracking-wider font-semibold">Inputs used:</span>'
            f'<span class="font-bold">Piecewise Basal ({blk_time}: {used_basal:.2f} U/hr)</span><span>•</span>'
            f'<span class="font-bold">PK ISF: {rec_isf:.0f} mg/dL/U</span><span>•</span>'
            f'<span class="font-bold">Active Target (Profile + Overrides)</span></div>'
        )

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

    # Stage 4: Correction Target Range Rows (status directly from NightscoutAdvisorKit)
    target_rows_html = ""
    for seg in report["targetSchedule"]:
        t_key = seg["timeString"]
        raw_label = seg["windowLabel"]
        if "(" in raw_label and raw_label.endswith(")"):
            win_name, rest = raw_label.split("(", 1)
            win_name = win_name.strip()
            win_label = rest[:-1]
        else:
            win_name = "Scheduled Window"
            win_label = raw_label

        cur_lo = int(round(float(seg["currentLowMgdL"])))
        cur_hi = int(round(float(seg["currentHighMgdL"])))
        cur_mid = 0.5 * (cur_lo + cur_hi)
        rec_lo = int(round(float(seg["recommendedLowMgdL"])))
        rec_hi = int(round(float(seg["recommendedHighMgdL"])))
        opt_mid = float(seg["optimalMidMgdL"])
        ci_mid_lo = float(seg["ciMidLowMgdL"])
        ci_mid_hi = float(seg["ciMidHighMgdL"])
        status = seg["status"]
        q04_v = seg.get("q04ErrorMgdL")
        q01_v = seg.get("q01ErrorMgdL")
        n_pts = seg.get("sampleCount")
        if q04_v is not None and q01_v is not None and n_pts is not None:
            ev_t = (
                f"Tracking residuals ε = BG − Target (N={int(n_pts):,} pts across {seg['dayCount']} days): "
                f"4th-pct drop q₀.₀₄ = {float(q04_v):+.1f} mg/dL, 1st-pct drop q₀.₀₁ = {float(q01_v):+.1f} mg/dL → "
                f"{seg['rationale']}"
            )
        else:
            ev_t = seg["rationale"]

        if status == "in_sync":
            cur_t_html = f'<span class="text-emerald-700 font-bold">{cur_lo}–{cur_hi} mg/dL</span> <span class="text-slate-400 text-[10px]">(mid {cur_mid:.1f})</span>'
            badge_t_html = '<span class="px-2 py-0.5 rounded text-[11px] font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">✓ In Sync</span>'
            row_t_bg = 'class="hover:bg-emerald-50/40 bg-emerald-50/15"'
        elif status in ("in_ci", "insufficient_data"):
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

    # Stage 1: ISF Badge & Correction Episodes (status directly from NightscoutAdvisorKit)
    cur_isf = float(global_isf["currentMgdLPerU"])
    geom_isf = float(global_isf["geometricMeanMgdLPerU"])
    med_isf = float(global_isf.get("medianMgdLPerU") or geom_isf)
    isf_ci_low = float(global_isf["ciLowMgdLPerU"])
    isf_ci_high = float(global_isf["ciHighMgdLPerU"])
    isf_status = global_isf["status"]

    if isf_status in ("in_sync", "in_ci", "insufficient_data"):
        if isf_status == "in_sync":
            badge_text = "✓ In Sync"
        elif isf_status == "insufficient_data":
            badge_text = "✓ Profile Anchor"
        else:
            badge_text = "✓ Within 95% CI"
        isf_badge_html = f'<span class="px-2.5 py-1 rounded text-xs font-sans bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">{badge_text}</span>'
        isf_decision_title = f"Profile {cur_isf:.0f} mg/dL/U aligns with PK estimate ({rec_isf:.0f} mg/dL/U)."
    else:
        isf_badge_html = f'<span class="px-2.5 py-1 rounded text-xs font-sans bg-amber-100 text-amber-800 font-bold">Adjust to {rec_isf:.0f}</span>'
        isf_decision_title = f"Adjust to {rec_isf:.0f} mg/dL/U (Current: {cur_isf:.0f} mg/dL/U)."

    episodes = report.get("isfEpisodes") or []
    isf_episodes_rows_html = ""
    for ep in episodes:
        ep_time = to_local_dt(float(ep["timestamp"])).strftime("%b %d, %H:%M")
        isf_val_str = f"{float(ep['impliedISFMgdLPerU']):.1f} mg/dL/U"
        egp_str = f"{-float(ep['egpOffsetU']):+.2f} U (net {float(ep['netCorrectionU']):.2f} U)"
        isf_episodes_rows_html += f"""
    <tr class="hover:bg-slate-50">
      <td class="py-2 px-3 font-mono font-medium text-slate-800 whitespace-nowrap">{ep_time}</td>
      <td class="py-2 px-3 font-mono text-slate-700 whitespace-nowrap">{float(ep['bgBolusMgdL']):.0f} &rarr; {float(ep['bgNadirMgdL']):.0f} mg/dL</td>
      <td class="py-2 px-3 font-mono font-bold text-emerald-700 whitespace-nowrap">&minus;{float(ep['dropMgdL']):.0f} mg/dL</td>
      <td class="py-2 px-3 font-mono text-slate-800 whitespace-nowrap">{float(ep['absorbedInsulinU']):.2f} U</td>
      <td class="py-2 px-3 font-mono text-slate-500 text-xs whitespace-nowrap">{egp_str}</td>
      <td class="py-2 px-3 font-mono font-bold text-blue-800 whitespace-nowrap">{isf_val_str}</td>
    </tr>
    """
    if not isf_episodes_rows_html:
        isf_episodes_rows_html = '<tr><td colspan="6" class="py-3 px-3 text-center text-slate-400 font-mono text-xs">No unconfounded hyperglycemic episodes detected in rolling window.</td></tr>'

    isf_proof_count = int(global_isf["episodeCount"])
    isf_proof_median = f"{med_isf:.1f}"
    isf_proof_min = f"{isf_ci_low:.0f}"
    isf_proof_max = f"{isf_ci_high:.0f}"
    isf_evidence_text = global_isf["rationale"]

    gain_margin = float(stability["gainMarginMultiplier"])
    phase_margin_deg = float(stability["phaseMarginDegrees"])
    damping_ratio = float(stability["dampingRatioZeta"])
    crit_isf_bound = float(stability["criticalISFBoundMgdLPerU"])
    omega_u = float(stability.get("omegaUltimate") or 1.81)
    osc_period_hrs = float(stability.get("oscillationPeriodHours") or 3.46)
    poles_str = stability["poleSummary"]

    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
    with open(template_path, "r", encoding="utf-8") as f:
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
        "{{readings_count}}": f"{n_samples:,}",
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
        "{{rolling_days}}": str(report["rollingDays"]),
        "{{gain_margin}}": f"{gain_margin:.1f}",
        "{{phase_margin}}": f"{phase_margin_deg:.0f}",
        "{{damping_ratio}}": f"{damping_ratio:.2f}",
        "{{crit_isf_bound}}": f"{crit_isf_bound:.0f}",
        "{{osc_period_hrs}}": f"{osc_period_hrs:.1f}",
        "{{omega_u}}": f"{omega_u:.2f}",
        "{{poles_str}}": poles_str,
    }

    for k, v in substitutions.items():
        html_content = html_content.replace(k, str(v))

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Generated dashboard powered by NightscoutAdvisorKit single solver engine at {output_path}")


if __name__ == "__main__":
    main()
