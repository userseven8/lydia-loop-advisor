# Lydia • Closed-Loop Telemetry Analytics & First-Principles Profile Optimizer

An automated data assimilation, clinical telemetry audit, and first-principles profile optimizer for pediatric Type 1 Diabetes closed-loop insulin delivery (LoopKit + Omnipod DASH + Lyumjev), connected directly to Nightscout (`fudbf291-lydia-guest.t1pal.com`).

**Live Production Dashboard**: [https://userseven8.github.io/lydia-loop-advisor/](https://userseven8.github.io/lydia-loop-advisor/)

---

## The First-Principles 4-Stage Mathematical Pipeline

Rather than relying on empirical trial-and-error or heuristic total daily dose (TDD) rules of thumb, this engine models closed-loop glycemic dynamics through a **decoupled, 4-stage mass-balance pipeline**:

### 1. Stage 1: Pharmacological ISF Proof (Unconfounded Drops)
* Isolates clean nocturnal correction episodes occurring with zero active carbs ($R_{\text{gut}} = 0$) and no active overrides.
* Proves that unmodeled basal delivery ($I_{\text{basal}} \cdot \text{ISF}$) and endogenous hepatic glucose output ($\text{EGP}_{\text{liver}}$) push in opposing directions and naturally cancel during resting fasting conditions:
  $$\Delta\text{BG} = - \text{ISF} \cdot I_{\text{corr}} - \text{ISF} \cdot I_{\text{basal}} + \text{EGP}_{\text{liver}} \implies \text{ISF}_{\text{physio}} \approx \frac{|\Delta\text{BG}|}{I_{\text{corr}}}$$
* Calibrates true sensitivity directly from measured nadirs, establishing the bedrock for all downstream basal and meal solvers.

### 2. Stage 2: Decoupled Fasting Basal Flux Equilibrium
* Solves resting background demand from clean fasting intervals without circular coupling to meal ratios:
  $$B_{\text{basal}} = \frac{\int r_{\text{pump}}(t) \, dt}{\Delta t} + \frac{\Delta\text{BG}}{\text{ISF}_{\text{physio}} \cdot \Delta t}$$
* **Postprandial Contamination Guard (Bedtime/Overnight)**: Identifies bimodal dinner digestion spilling into late evening, preventing nocturnal over-basaling.
* **Activity Variance Guard (08:30–22:00)**: Toddler physical play intermittently suppresses insulin demand to ~0.00–0.05 U/hr (which Loop manages via real-time temp basal suspensions). The solver isolates quiet resting homeostasis (0.10 U/hr) so daytime basal is not erroneously downgraded by exercise-depressed median flux, preventing creeping glucose rises during sedentary rest.

### 3. Stage 3: Settled Target Meal Carb Ratio Regression
* Evaluates 3-hour postprandial windows using the independently measured resting basal baseline:
  $$I_{\text{meal}}^{\text{target}} = I_{\text{delivered}} - (B_{\text{resting}} \times 3\text{h}) + \frac{\Delta\text{BG}}{\text{ISF}_{\text{physio}}}$$
* **Settled Target Filtering ($|\Delta\text{BG}| \le 60\text{ mg/dL}$)**: Excludes severe under-bolus spikes ($+112, +165\text{ mg/dL}$) and rebound crashes ($-90\text{ mg/dL}$) from the regression slope ($\text{CR} = \sum C_i^2 / \sum C_i I_{\text{meal}, i}^{\text{target}}$) to isolate the true upfront ratio she should have had to achieve flat glycemic return ($\Delta\text{BG} = 0$).

### 4. Stage 4: Closed-Loop Stability Proof (Lyumjev 55m Peak Damping)
* LoopKit Lyumjev preset dynamics: 55-minute peak activity ($\tau_p = 0.9167\text{ hr}$) and 6-hour duration of action ($\text{DIA} = 6.0\text{ hr}$).
* Verifies Nyquist closed-loop stability, gain margin, and phase margin to guarantee second-order damping ($\zeta \ge 1.0$), ensuring real stable poles and zero oscillatory ringing.

---

## Automated Architecture & Cloud Deployment

* **Data Assimilation**: Connects to Nightscout REST API with a rolling 30-day lookback window across CGM entries, treatments (boluses, temp basals, carb entries), and live profiles.
* **Continuous Integration**: Powered by GitHub Actions cron (`.github/workflows/update_dashboard.yml`), running every 6 hours and on every push to `main`.
* **Static Hosting**: Recompiles `index.html` and deploys automatically to GitHub Pages with zero external backend dependencies.

---

## Local Development & Testing

```bash
# Clone the repository
git clone https://github.com/userseven8/lydia-loop-advisor.git
cd lydia-loop-advisor

# Run the live analytics solver and compile index.html
python3 generate_dashboard.py

# Run continuous 9D Extended Kalman Filter state estimator
python3 run_real_ekf.py

# Push changes to live GitHub Pages
git add .
git commit -m "Update analytics"
git push origin main
```
