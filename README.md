# Lydia • Loop Therapy Settings Review

Reads a rolling window of Nightscout telemetry and estimates basal rate, insulin
sensitivity (ISF) and carb ratio by time of day — together with how much each
estimate can actually be trusted.

**Output is for discussion with a diabetes care team. It is not a dosing
instruction, and nothing here should be entered into a pump on its own.**

## What it does

- **Nightscout ingest** — a rolling 30 days of CGM entries, insulin delivery and
  carb entries. Temp basals are clipped by supersession, and time with no active
  temp is credited at the scheduled rate rather than as zero.
- **Three solvers** — fasting flux equilibrium for basal (5 time blocks),
  isolated correction drops for ISF, anchored meal regression for carb ratios
  (5 slots).
- **Uncertainty first** — every estimate carries a 90% bootstrap interval. A
  change is suggested only when that interval excludes the value already
  programmed, so **"no change indicated" is the normal outcome**.
- **AGP** — 24-hour percentile curves (10th/25th/50th/75th/90th).
- **Time in Range** — five-tier ATTD/ADA consensus split.

## What it deliberately does not do

- It does not claim precision it lacks. Slots with too few meals, a fit that
  explains little, or meals still unsettled at +3h are reported as such rather
  than handed a number.
- It does not predict glucose. `glucose_model.py` holds a forward model kept as
  a **recorded negative result** — it is stable but loses to a constant
  prediction beyond one hour, so therapy settings are not derived from it. Read
  its docstring before reusing it.
- The three solvers run in sequence, so ISF error propagates into basal and then
  into the carb ratios. The intervals do not capture that coupling.

## Setup

The Nightscout address is **not** stored in this repo. Supply it through the
`NIGHTSCOUT_URL` environment variable.

```bash
NIGHTSCOUT_URL=https://your-instance.example.com python3 generate_dashboard.py
```

While testing, write somewhere other than `index.html`:

```bash
NIGHTSCOUT_URL=https://your-instance.example.com \
LYDIA_OUTPUT=/tmp/preview.html \
python3 generate_dashboard.py
```

## Deployment

GitHub Actions rebuilds and redeploys to GitHub Pages every 2 hours
(`.github/workflows/update.yml`), and on every push to `main`.

Before the first run, add the Nightscout address as a repository secret:

- **Settings → Secrets and variables → Actions → New repository secret**
- Name `NIGHTSCOUT_URL`, value your Nightscout base URL, no trailing slash

Without it the generate step exits with an error. That fails the build job, so
no artifact is uploaded and no deploy runs — the published page stays at its
previous version rather than breaking.

Then under **Settings → Pages**, set **Source** to **GitHub Actions**.

## A note on privacy

The published page shows one child's glucose, insulin and meal data. Two things
are worth understanding before making this repository public:

- **A public repo is indexed and archived.** The page carries a `noindex` tag
  and the Nightscout address is kept out of the source, but a public repo is
  still searchable and gets swept by scrapers.
- **Git history is permanent.** Every build commits `index.html`, so the repo
  becomes an append-only archive of clinical data that Nightscout itself
  discards on a rolling basis. Removing a file later does not clear history,
  forks or clones.

If the Nightscout instance answers without a token, anyone holding the address
can read the underlying data directly, independent of this repo.
