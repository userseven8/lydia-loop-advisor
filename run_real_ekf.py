#!/usr/bin/env python3
"""
True Continuous Extended Kalman Filter (EKF) on Lydia's Real Nightscout Telemetry
Estimates hidden physiological states and augmented parameters across thousands of 5-min steps:
States:
  x[0]: S1 (subcutaneous insulin compartment 1, U)
  x[1]: S2 (subcutaneous insulin compartment 2, U)
  x[2]: X  (remote insulin action, U/min)
  x[3]: D1 (gut carb compartment 1, g)
  x[4]: D2 (gut carb compartment 2, g)
  x[5]: G  (blood glucose, mg/dL)
Augmented Parameters:
  x[6]: ISF   (mg/dL / U)
  x[7]: Basal (U/hr)
  x[8]: CR    (g/U)
"""
import sys
import numpy as np
from datetime import datetime, timezone, timedelta

# Import data already loaded in generate_dashboard
from generate_dashboard import (
    cgm_timeline, insulin_events, carbs_list, temp_basals,
    rec_isf, cur_isf, live_basals, live_crs
)

print("="*75)
print("   RUNNING REAL EXTENDED KALMAN FILTER (EKF) ON CONTINUOUS TELEMETRY")
print("="*75)

# 1. Build a strict uniform 5-minute grid across the last 14 days of telemetry
t_max = cgm_timeline[-1][0]
t_min = t_max - (14 * 86400) # 14 full continuous days

dt_sec = 300.0 # 5 minutes
dt_min = 5.0

grid_times = np.arange(t_min, t_max + dt_sec, dt_sec)
N_steps = len(grid_times)
print(f"Timeline: {N_steps} 5-minute steps ({14} days) from {datetime.fromtimestamp(t_min, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} to {datetime.fromtimestamp(t_max, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")

# Map CGM to grid (nearest reading within 3 minutes)
cgm_t = np.array([p[0] for p in cgm_timeline])
cgm_v = np.array([p[1] for p in cgm_timeline])

grid_cgm = np.full(N_steps, np.nan)
for i, t in enumerate(grid_times):
    idx = np.searchsorted(cgm_t, t)
    best_val = np.nan
    best_dist = 180.0
    for j in range(max(0, idx - 2), min(len(cgm_t), idx + 3)):
        dist = abs(cgm_t[j] - t)
        if dist < best_dist:
            best_dist = dist
            best_val = cgm_v[j]
    grid_cgm[i] = best_val

# Map insulin inputs to grid (sum of all boluses, SMBs, and basal delivery in each 5-min slice)
grid_insulin = np.zeros(N_steps)
# Temp basals
for s, e, r in temp_basals:
    # Overlap with grid steps
    i_start = max(0, int((s - t_min) // dt_sec))
    i_end = min(N_steps, int((e - t_min) // dt_sec) + 1)
    for i in range(i_start, i_end):
        step_s = grid_times[i]
        step_e = step_s + dt_sec
        overlap = max(0.0, min(step_e, e) - max(step_s, s))
        if overlap > 0:
            grid_insulin[i] += r * (overlap / 3600.0)

# Boluses & SMBs
for t, ins in insulin_events:
    if t_min <= t <= t_max:
        i = int((t - t_min) // dt_sec)
        if 0 <= i < N_steps:
            grid_insulin[i] += ins

# Map carbs to grid
grid_carbs = np.zeros(N_steps)
for t, c in carbs_list:
    if t_min <= t <= t_max:
        i = int((t - t_min) // dt_sec)
        if 0 <= i < N_steps:
            grid_carbs[i] += c

valid_cgm_count = np.sum(~np.isnan(grid_cgm))
print(f"Data density: {valid_cgm_count}/{N_steps} CGM points ({valid_cgm_count/N_steps*100:.1f}%), Total Insulin={np.sum(grid_insulin):.1f}U, Total Carbs={np.sum(grid_carbs):.1f}g")

# =========================================================================
# EKF MATHEMATICAL FORMULATION
# =========================================================================
TAU_S = 50.0  # Lyumjev subcutaneous time constant (min)
TAU_A = 40.0  # Remote interstitial insulin action constant (min)
TAU_D = 45.0  # Gut absorption constant (min)

# Initial state: [S1, S2, X, D1, D2, G, ISF, Basal, CR]
x = np.zeros(9)
x[0] = 0.05       # S1
x[1] = 0.05       # S2
x[2] = 0.05 / 60. # X
x[3] = 0.0        # D1
x[4] = 0.0        # D2
x[5] = grid_cgm[0] if not np.isnan(grid_cgm[0]) else 140.0 # G
x[6] = 230.0      # ISF prior
x[7] = 0.08       # Basal prior (U/hr)
x[8] = 7.0        # CR prior (g/U)

# Covariance matrix P
P = np.diag([
    0.1**2,     # S1
    0.1**2,     # S2
    (0.01/60)**2,# X
    2.0**2,     # D1
    2.0**2,     # D2
    20.0**2,    # G
    40.0**2,    # ISF
    0.05**2,    # Basal
    2.0**2      # CR
])

# Process noise covariance Q (rates per 5-min step)
# Parameters are assumed to wander slowly as random walks
Q = np.diag([
    (0.01)**2,        # S1 noise
    (0.01)**2,        # S2 noise
    (0.001/60)**2,    # X noise
    (0.5)**2,         # D1 noise
    (0.5)**2,         # D2 noise
    (3.0)**2,         # G noise (unmodeled metabolic flux)
    (0.3)**2,         # ISF drift (slow drift over days)
    (0.001)**2,       # Basal drift
    (0.02)**2         # CR drift
])

# Measurement noise R (CGM variance)
R_cgm = 15.0**2 # 15 mg/dL standard deviation

# Measurement matrix H: y = G
H = np.zeros((1, 9))
H[0, 5] = 1.0

# Non-linear continuous derivative function: dx/dt = f(x, u, d)
def state_derivatives(state, u_rate, d_rate):
    s1, s2, x_act, d1, d2, g, isf_val, basal_val, cr_val = state
    
    ds1 = u_rate - (s1 / TAU_S)
    ds2 = (s1 - s2) / TAU_S
    u_i = s2 / TAU_S
    dx = (u_i - x_act) / TAU_A
    
    dd1 = d_rate - (d1 / TAU_D)
    dd2 = (d1 - d2) / TAU_D
    u_c = d2 / TAU_D
    
    # Blood glucose dynamics
    # Net appearance = (u_c / CR) * ISF
    # Net clearance = (x_act - basal_val / 60.0) * ISF
    dg = isf_val * ( (u_c / max(1.0, cr_val)) - (x_act - basal_val / 60.0) )
    
    # Parameters assumed constant locally
    return np.array([ds1, ds2, dx, dd1, dd2, dg, 0.0, 0.0, 0.0])

# RK4 Integrator for 5-minute step
def rk4_step(state, u_in, d_in, dt):
    u_rate = u_in / dt # U/min
    d_rate = d_in / dt # g/min
    
    k1 = state_derivatives(state, u_rate, d_rate)
    k2 = state_derivatives(state + 0.5 * dt * k1, u_rate, d_rate)
    k3 = state_derivatives(state + 0.5 * dt * k2, u_rate, d_rate)
    k4 = state_derivatives(state + dt * k3, u_rate, d_rate)
    
    return state + (dt / 6.0) * (k1 + 2*k2 + 2*k3 + k4)

# Numerical Jacobian of f
def compute_jacobian(state, u_in, d_in, dt):
    n = len(state)
    F = np.zeros((n, n))
    base = rk4_step(state, u_in, d_in, dt)
    eps = 1e-5
    for j in range(n):
        perturbed = np.copy(state)
        delta = eps * max(1.0, abs(state[j]))
        perturbed[j] += delta
        stepped = rk4_step(perturbed, u_in, d_in, dt)
        F[:, j] = (stepped - base) / delta
    return F

# =========================================================================
# RUN THE FILTER OVER ALL 14 DAYS
# =========================================================================
history_time = []
history_cgm = []
history_g_pred = []
history_isf = []
history_basal = []
history_cr = []

print("Running EKF recursion through 4,032 consecutive steps...")

for k in range(N_steps):
    u_k = grid_insulin[k]
    d_k = grid_carbs[k]
    
    # 1. PREDICT STEP
    x_pred = rk4_step(x, u_k, d_k, dt_min)
    F_k = compute_jacobian(x, u_k, d_k, dt_min)
    P_pred = F_k @ P @ F_k.T + Q
    
    # Ensure parameter bounds remain physiologically sound
    x_pred[6] = np.clip(x_pred[6], 100.0, 400.0) # ISF
    x_pred[7] = np.clip(x_pred[7], 0.00, 0.25)   # Basal
    x_pred[8] = np.clip(x_pred[8], 3.0, 25.0)    # CR
    
    # 2. UPDATE STEP (if CGM reading exists)
    y_k = grid_cgm[k]
    if not np.isnan(y_k):
        # Innovation
        y_innov = y_k - x_pred[5]
        S_k = (H @ P_pred @ H.T)[0, 0] + R_cgm
        K_k = (P_pred @ H.T) / S_k
        
        # State & Parameter Update
        x = x_pred + K_k.flatten() * y_innov
        
        # Clip updated parameters
        x[6] = np.clip(x[6], 100.0, 400.0)
        x[7] = np.clip(x[7], 0.00, 0.25)
        x[8] = np.clip(x[8], 3.0, 25.0)
        
        # Covariance update (Joseph form for numerical stability)
        I_KH = np.eye(9) - K_k @ H
        P = I_KH @ P_pred @ I_KH.T + (K_k * R_cgm) @ K_k.T
    else:
        x = x_pred
        P = P_pred
        
    history_time.append(grid_times[k])
    history_cgm.append(y_k)
    history_g_pred.append(x[5])
    history_isf.append(x[6])
    history_basal.append(x[7])
    history_cr.append(x[8])

print("EKF run complete!")

# =========================================================================
# ANALYZE CONVERGENCE & ACCURACY
# =========================================================================
valid_mask = ~np.isnan(np.array(history_cgm))
cgm_arr = np.array(history_cgm)[valid_mask]
pred_arr = np.array(history_g_pred)[valid_mask]

rmse = np.sqrt(np.mean((cgm_arr - pred_arr)**2))
mae = np.mean(np.abs(cgm_arr - pred_arr))

isf_hist = np.array(history_isf)
basal_hist = np.array(history_basal)
cr_hist = np.array(history_cr)

# Separate Day vs Night
hours = np.array([(datetime.fromtimestamp(t, tz=timezone.utc) + timedelta(hours=3)).hour for t in history_time])
night_mask = (hours >= 0) & (hours < 8)
day_mask = (hours >= 8) & (hours < 22)

print("\n" + "="*75)
print("              EXTENDED KALMAN FILTER (EKF) RESULTS")
print("="*75)
print(f"Tracking Performance: RMSE = {rmse:.1f} mg/dL | MAE = {mae:.1f} mg/dL across {len(cgm_arr)} CGM points")
print(f"\n1. ESTIMATED ISF (Insulin Sensitivity Factor):")
print(f"   • Initial Prior      : 230.0 mg/dL/U")
print(f"   • EKF 14-Day Median  : {np.median(isf_hist):.1f} mg/dL/U")
print(f"   • EKF Mean ± SD      : {np.mean(isf_hist):.1f} ± {np.std(isf_hist):.1f} mg/dL/U")
print(f"   • EKF Interquartile  : [{np.percentile(isf_hist, 25):.1f} – {np.percentile(isf_hist, 75):.1f}] mg/dL/U")
print(f"   • Mass-Balance Solver: {rec_isf:.0f} mg/dL/U")

print(f"\n2. ESTIMATED BASAL RESTING DEMAND (U/hr):")
print(f"   • Initial Prior      : 0.080 U/hr")
print(f"   • EKF Overall Median : {np.median(basal_hist):.3f} U/hr")
print(f"   • EKF Night (00–08)  : {np.median(basal_hist[night_mask]):.3f} U/hr (Mean: {np.mean(basal_hist[night_mask]):.3f})")
print(f"   • EKF Day   (08–22)  : {np.median(basal_hist[day_mask]):.3f} U/hr (Mean: {np.mean(basal_hist[day_mask]):.3f})")
print(f"   • Mass-Balance Basals: Night ~ 0.05–0.10 U/hr | Day ~ 0.05 U/hr")

print(f"\n3. ESTIMATED CARB RATIO (CR, g/U):")
print(f"   • Initial Prior      : 1:7.0 g/U")
print(f"   • EKF Overall Median : 1:{np.median(cr_hist):.1f} g/U")
print(f"   • Morning (08–12)    : 1:{np.median(cr_hist[(hours >= 8) & (hours < 12)]):.1f} g/U")
print(f"   • Afternoon (12–17)  : 1:{np.median(cr_hist[(hours >= 12) & (hours < 17)]):.1f} g/U")
print(f"   • Evening (17–22)    : 1:{np.median(cr_hist[(hours >= 17) & (hours < 22)]):.1f} g/U")
print("="*75 + "\n")
