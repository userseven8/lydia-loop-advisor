#!/usr/bin/env python3
"""Forward glucose model with self-regulation.

The EKF's model had no dependence of dG/dt on G, making it a pure integrator:
any parameter error accumulated without bound, so it only stayed on track
because the Kalman update pulled G back to the CGM reading every 5 minutes.
That hides model error inside the parameter random walk and makes multi-hour
prediction — and therefore any honest validation — impossible.

This adds glucose effectiveness: a term -k_G * (G - G_b) that pulls glucose
toward the level it settles at when delivery equals basal. The model is then
stable open-loop and can be scored on held-out data.

MEASURED RESULT - READ BEFORE BUILDING ON THIS.

Stability is fixed: the legacy model rails at the 600 mg/dL clamp within 6h,
this one stays bounded. But it is NOT a usable predictor of this child's
glucose. Scored open-loop against real CGM, median |error| in mg/dL, with
parameters fitted on the first half of a 14-day window and scored on the
held-out second half:

            1h     3h     6h
  persistence     28.0   37.0   59.0
  pump params     38.0   84.8   94.0
  fitted params   24.2   45.2   48.3
  constant 133    32.0   34.0   30.0     <-- beats the model at 3h and 6h

A constant that ignores insulin, carbs and all physiology outperforms the
fitted model beyond one hour, and the fit drove ISF and basal to the edges of
the search grid. At these horizons the residual (unannounced carbs, absorption
variability, activity, illness, site variation) dominates the deterministic
dynamics, so parameters read off this model would be fitting noise.

Kept because the stability fix is real and the scoring harness is reusable.
Do not derive therapy settings from it.

Clinical parameters keep their meaning:
  ISF   mg/dL per U
  CR    g per U
  basal U/hr that holds G at G_b
Each may be a scalar or a per-step array, so a time-of-day profile can be
passed in without changing the integrator.
"""
import math

DEFAULTS = {
    "tau_S": 50.0,   # subcutaneous insulin absorption (min)
    "tau_A": 40.0,   # remote insulin action (min)
    "tau_D": 45.0,   # gut carb absorption (min)
    "k_G": 0.010,    # glucose effectiveness (1/min) - the restoring force
    "G_b": 120.0,    # glucose the system settles to when delivery == basal
}


def _at(v, i):
    """Accept a scalar or a per-step sequence for any parameter."""
    if isinstance(v, (int, float)):
        return float(v)
    return float(v[i] if i < len(v) else v[-1])


def simulate(insulin, carbs, params, g_start, start, horizon,
             warmup=72, g_observed=None, substeps=5):
    """Integrate forward and return the predicted G trajectory.

    insulin  : U delivered per step (5-min steps)
    carbs    : g entered per step
    params   : dict with ISF, CR, basal (+ optional overrides of DEFAULTS)
    start    : index at which free-running prediction begins
    horizon  : number of steps to predict
    warmup   : steps before `start` used to fill the insulin/carb compartments.
               If g_observed is given, G is held to the observation during
               warmup so only the compartments are primed, not the glucose.
    """
    p = dict(DEFAULTS)
    p.update(params)
    tau_S, tau_A, tau_D = p["tau_S"], p["tau_A"], p["tau_D"]
    k_G, G_b = p["k_G"], p["G_b"]
    dt = 5.0 / substeps

    i0 = max(0, start - warmup)
    s1 = s2 = 0.0
    d1 = d2 = 0.0
    # seed remote action at the basal rate so we don't start from an insulin debt
    x = _at(p["basal"], i0) / 60.0
    g = float(g_start)

    out = []
    for i in range(i0, min(len(insulin), start + horizon)):
        isf = _at(p["ISF"], i)
        cr = max(1.0, _at(p["CR"], i))
        bas = _at(p["basal"], i)
        u_rate = insulin[i] / 5.0
        d_rate = carbs[i] / 5.0

        for _ in range(substeps):
            u_i = s2 / tau_S
            u_c = d2 / tau_D
            s1 += dt * (u_rate - s1 / tau_S)
            s2 += dt * ((s1 - s2) / tau_S)
            x += dt * ((u_i - x) / tau_A)
            d1 += dt * (d_rate - d1 / tau_D)
            d2 += dt * ((d1 - d2) / tau_D)
            dg = isf * (u_c / cr) - isf * (x - bas / 60.0) - k_G * (g - G_b)
            g += dt * dg
            g = min(max(g, 20.0), 600.0)

        if i < start:
            # priming the compartments only; keep G pinned to reality
            if g_observed is not None and i < len(g_observed):
                obs = g_observed[i]
                if obs == obs:          # not NaN
                    g = float(obs)
        else:
            out.append(g)
    return out


def simulate_legacy(insulin, carbs, params, g_start, start, horizon,
                    warmup=72, g_observed=None, substeps=5):
    """The previous model (no glucose-dependent term), for comparison."""
    p = dict(DEFAULTS)
    p.update(params)
    p["k_G"] = 0.0
    return simulate(insulin, carbs, p, g_start, start, horizon,
                    warmup=warmup, g_observed=g_observed, substeps=substeps)
