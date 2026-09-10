# Lydia • Loop Retrospective Analytics & Profile Optimizer

Automated data assimilation and clinical profile tuning engine for Loop AID, connected to Nightscout (`fudbf291-lydia-guest.t1pal.com`).

## Features
- **Continuous Nightscout Assimilation**: Pulls and digests 14 days of CGM records, actual insulin deliveries, and meal logs.
- **Ambulatory Glucose Profile (AGP)**: 24-hour diurnal percentile curves (10th, 25th, 50th/median, 75th, 90th) overlaid with scheduled basal rates.
- **Automated Retrospective Clinical Tuning**:
  - Detects midday over-basal / lunch low traps.
  - Detects evening carb ratio deficits causing dinner spikes and midnight rebound crashes.
  - Detects early morning dawn hormone drift.
- **Automated 24/7 Cloud Hosting**:
  - Powered by GitHub Actions (runs every 6 hours and every morning at 7:00 AM).
  - Deploys automatically to free GitHub Pages.

## How to Deploy to Your GitHub Account (One-time setup)
1. In terminal, navigate to this folder:
   ```bash
   cd /Users/panvar/.gemini/antigravity/scratch/lydia-loop-analytics
   ```
2. Initialize and push to a new GitHub repository:
   ```bash
   git init
   git add .
   git commit -m "Initial commit of Lydia Loop Analytics"
   gh repo create lydia-loop-analytics --public --source=. --push
   ```
3. In your GitHub repository **Settings -> Pages**:
   - Under **Build and deployment -> Source**, select **GitHub Actions**.
4. That's it! Your dashboard will be live at `https://<your-username>.github.io/lydia-loop-analytics/` and will update automatically every day in the cloud with zero maintenance.
