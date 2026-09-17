# September 13 Production Baseline (92% TIR)

This directory contains the exact snapshot of the September 13 production code and generated dashboard (commit `963859e` / `06dbd6a`) which achieved 92% Time In Range (TIR).

## Contained Files
- `generate_dashboard.py`: Staged orthogonal deconvolution algorithm (ISF direct drops, 5-block basal with daytime meal regression intercept, 6-slot empirical meal CRs).
- `template.html`: Clean production HTML template.
- `index.html`: Compiled production dashboard deployed to GitHub Pages.

## How to Revert to this Baseline
To restore production to this exact baseline at any time:
```bash
cp archive/september_13_92tir/generate_dashboard.py ./
cp archive/september_13_92tir/template.html ./
cp archive/september_13_92tir/index.html ./
git add generate_dashboard.py template.html index.html
git commit -m "Revert to September 13 92% TIR production baseline"
git push origin main
```
