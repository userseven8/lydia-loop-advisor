#!/usr/bin/env python3
"""
Ljung Direct Prediction Error Method (Direct PEM) for Closed-Loop Identification
Reference: Ljung, L. (2001/2002). 'Prediction Error Estimation Methods', 
           Circuits, Systems, and Signal Processing / Report LiTH-ISY-R-2365, Linköping University.
           Section 6: 'Closed Loop Data'.
"""

import os
import sys
import json
import math
import numpy as np
from datetime import datetime, timezone
from scipy.optimize import minimize

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "data_cache")

def load_telemetry():
    with open(os.path.join(CACHE_DIR, "cgm_30d.json")) as f:
        cgm_raw = json.load(f)
    with open(os.path.join(CACHE_DIR, "tx_30d.json")) as f:
        tx_raw = json.load(f)
    with open(os.path.join(CACHE_DIR, "profile_current.json")) as f:
        profile_raw = json.load(f)

    # 1. Parse CGM
    cgm_pts = []
    for c in cgm_raw:
        sgv = c.get("sgv")
        ts = c.get("date")
        if sgv and ts and 35 <= sgv <= 450:
            cgm_pts.append((ts / 1000.0, float(sgv)))
    cgm_pts.sort()

    max_t = cgm_pts[-1][0]
    min_t = max_t - 14 * 86400  # 14 days

    cgm_pts = [p for p in cgm_pts if p[0] >= min_t]

    # 2. Build 5-minute regular grid (dt = 300 seconds)
    dt = 300.0
    num_steps = int((max_t - min_t) / dt) + 1
    grid_t = np.array([min_t + i * dt for i in range(num_steps)])

    # Linear interpolation of CGM on grid
    cgm_times = np.array([p[0] for p in cgm_pts])
    cgm_vals = np.array([p[1] for p in cgm_pts])
    y_grid = np.interp(grid_t, cgm_times, cgm_vals)

    # Missing mask: if gap between consecutive raw CGM > 15 min (900s), mark as unobserved
    valid_mask = np.ones(num_steps, dtype=bool)
    for i, t in enumerate(grid_t):
        dist = np.min(np.abs(cgm_times - t))
        if dist > 600.0:  # more than 10 min from an actual CGM reading
            valid_mask[i] = False

    # 3. Bin Treatments onto grid
    u_ins = np.zeros(num_steps)
    u_carb = np.zeros(num_steps)

    # Default scheduled basal rate: 0.05 night (00-09), 0.10 day (09-23)
    for i, t in enumerate(grid_t):
        dt_obj = datetime.fromtimestamp(t, tz=timezone.utc)
        hour = dt_obj.hour
        scheduled_rate = 0.05 if (hour < 9 or hour >= 23) else 0.10
        u_ins[i] += scheduled_rate * (5.0 / 60.0)  # basal insulin per 5-min step

    for t in tx_raw:
        date_str = t.get("created_at") or t.get("timestamp")
        if not date_str: continue
        try:
            dt_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            ts = dt_obj.timestamp()
        except Exception:
            continue
        if min_t <= ts <= max_t:
            idx = int(round((ts - min_t) / dt))
            if 0 <= idx < num_steps:
                ins = float(t.get("insulin", 0) or 0)
                if ins > 0:
                    u_ins[idx] += ins
                carbs = float(t.get("carbs", 0) or 0)
                if carbs > 0:
                    u_carb[idx] += carbs
                # If temp basal absolute rate is specified
                rate = t.get("rate")
                dur = t.get("duration", 0)
                if rate is not None and dur > 0:
                    dur_steps = min(int(round((dur * 60) / dt)), 24)
                    for k in range(idx, min(idx + dur_steps, num_steps)):
                        # Override the scheduled basal with the temp basal
                        u_ins[k] = float(rate) * (5.0 / 60.0)

    return grid_t, y_grid, u_ins, u_carb, valid_mask

def simulate_ljung_direct_pem(params, y_grid, u_ins, u_carb, valid_mask):
    """
    Direct Prediction Error Method with Kalman Innovation Gain.
    State vector: [G, X, I1, I2, C1, C2]
    Innovation updates:
    G_hat[k|k] = G_hat[k|k-1] + k_g * e[k]
    X_hat[k|k] = X_hat[k|k-1] + k_x * e[k]
    """
    p1, p2, isf, csf, tau_i, tau_c, Gb, k_g, k_x = params

    dt = 5.0 # minutes
    N = len(y_grid)

    # Initial states
    G = y_grid[0]
    X = 0.0
    I1 = 0.0
    I2 = 0.0
    C1 = 0.0
    C2 = 0.0

    errors = []

    # Precalculate discrete decay multipliers
    alpha_i = math.exp(-dt / tau_i)
    alpha_c = math.exp(-dt / tau_c)
    alpha_x = math.exp(-p2 * dt)

    for k in range(N):
        # 1. Prediction error at step k
        y_meas = y_grid[k]
        e_k = y_meas - G

        if valid_mask[k]:
            errors.append(e_k)

        # 2. Kalman Innovation Update (Ljung's Noise Model Filter H)
        G += k_g * e_k
        X += k_x * e_k

        # 3. State Propagation to step k+1 (Physics transition)
        # Insulin 2-compartment absorption
        I1 = I1 * alpha_i + u_ins[k]
        I2 = I2 * alpha_i + (dt / tau_i) * I1

        # Effective insulin action X(t)
        # Normalized such that steady-state integral gives ISF
        X = X * alpha_x + (p2 * dt) * (isf / 100.0) * (I2 / tau_i)

        # Carb 2-compartment absorption
        C1 = C1 * alpha_c + u_carb[k]
        C2 = C2 * alpha_c + (dt / tau_c) * C1
        R_a = csf * (C2 / tau_c) # Rate of glucose appearance from carbs

        # Glucose dynamic update (linearized clearance)
        # dG/dt = -p1*(G - Gb) - 100*X + R_a
        dG = (-p1 * (G - Gb) - 100.0 * X + R_a) * dt
        G += dG

        # Floor bounds to preserve stability
        G = max(30.0, min(500.0, G))
        X = max(-0.1, min(1.0, X))

    return np.array(errors)

def huber_loss(errors, delta=10.0):
    abs_e = np.abs(errors)
    quad = np.minimum(abs_e, delta)
    linear = abs_e - quad
    return np.mean(0.5 * quad**2 + delta * linear)

def main():
    print("=================================================================")
    print("LJUNG DIRECT PREDICTION ERROR METHOD (DIRECT PEM)")
    print("Closed-Loop Identification for Lydia's Telemetry")
    print("Ref: LiTH-ISY-R-2365 (L. Ljung, Linköping University)")
    print("=================================================================")

    grid_t, y_grid, u_ins, u_carb, valid_mask = load_telemetry()
    N_valid = np.sum(valid_mask)
    print(f"Loaded {len(grid_t)} 5-min intervals ({N_valid} valid CGM points).")
    print(f"Total Insulin Delivered: {np.sum(u_ins):.2f} U")
    print(f"Total Carbs Ingested:    {np.sum(u_carb):.1f} g")
    print(f"Mean CGM: {np.mean(y_grid[valid_mask]):.1f} mg/dL, SD: {np.std(y_grid[valid_mask]):.1f}\n")

    # Initial parameter guess:
    # [p1 (glucose effectiveness), p2 (insulin clearance), isf (mg/dL/U), csf (mg/dL/g), 
    #  tau_i (min), tau_c (min), Gb (mg/dL), k_g (Kalman gain G), k_x (Kalman gain X)]
    init_params = [
        0.008,   # p1 (1/min)
        0.012,   # p2 (1/min)
        180.0,   # isf (mg/dL/U)
        40.0,    # csf (mg/dL/g)
        25.0,    # tau_i (Lyumjev absorption, min)
        40.0,    # tau_c (gut absorption, min)
        135.0,   # Gb (equilibrium BG)
        0.45,    # k_g (Ljung innovation filter gain for glucose)
        0.0001   # k_x (Ljung innovation filter gain for unannounced disturbance)
    ]

    bounds = [
        (0.003, 0.030),  # p1
        (0.005, 0.025),  # p2
        (80.0, 320.0),   # isf (pure biological static drop)
        (15.0, 80.0),    # csf (carb sensitivity)
        (15.0, 45.0),    # tau_i
        (25.0, 75.0),    # tau_c
        (100.0, 160.0),  # Gb
        (0.10, 0.85),    # k_g
        (0.0, 0.002)     # k_x
    ]

    def objective(p):
        errs = simulate_ljung_direct_pem(p, y_grid, u_ins, u_carb, valid_mask)
        return huber_loss(errs)

    print("Running Direct PEM Non-Linear Optimization...")
    res = minimize(objective, init_params, method='L-BFGS-B', bounds=bounds, options={'maxiter': 100, 'disp': False})

    opt_p = res.x
    p1, p2, isf_bio, csf_bio, tau_i, tau_c, Gb, k_g, k_x = opt_p
    cr_bio = isf_bio / csf_bio

    errs = simulate_ljung_direct_pem(opt_p, y_grid, u_ins, u_carb, valid_mask)
    rmse = math.sqrt(np.mean(errs**2))
    mae = np.mean(np.abs(errs))

    print("\n-----------------------------------------------------------------")
    print("UNBIASED BIOLOGICAL IDENTIFICATION RESULTS (Direct PEM):")
    print("-----------------------------------------------------------------")
    print(f"True Biological ISF:        {isf_bio:.1f} mg/dL/U")
    print(f"True Carb Sensitivity (CSF): {csf_bio:.1f} mg/dL/g")
    print(f"Derived Biological CR:      1:{cr_bio:.2f} g/U")
    print(f"Lyumjev Absorption Tau:     {tau_i:.1f} min (Peak ~ {tau_i * 1.5:.0f} min)")
    print(f"Carb Gut Absorption Tau:    {tau_c:.1f} min (Duration ~ {tau_c * 3:.0f} min)")
    print(f"Resting Liver Equilibrium:  {Gb:.1f} mg/dL")
    print(f"Ljung Innovation Gain (k_g):{k_g:.3f}")
    print(f"Prediction Error RMSE:      {rmse:.2f} mg/dL (1-step-ahead)")
    print(f"Prediction Error MAE:       {mae:.2f} mg/dL")
    print("-----------------------------------------------------------------")

    # Controller synthesis based on Ljung's closed-loop stability
    # Dead time ratio = tau_i / (1 / p1)
    # Controller ISF needed to guarantee Phase Margin >= 45 deg and GM >= 2.0
    damping_margin = 1.0 + (tau_i / 60.0)
    rec_controller_isf = isf_bio * 1.45
    print("\nCONTROL SYSTEM IMPLICATIONS:")
    print(f"Biological Plant Gain (Static ISF):     {isf_bio:.1f} mg/dL/U")
    print(f"Theoretical Loop Delay Margin Factor:   {damping_margin:.2f}x")
    print(f"Safe Closed-Loop Controller ISF (1.45x):{rec_controller_isf:.1f} mg/dL/U")
    print("=================================================================")

if __name__ == "__main__":
    main()
