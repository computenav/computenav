# ComputeNAV · GPU NAV Index (research project)

> **High-school student research project**
> **Indicative data only — no sold transactions yet**
> **Not investment advice**

ComputeNAV is a daily research index of the **indicative realizable net asset value (NAV, USD per card)** of used data-center GPUs, with a static dashboard published on GitHub Pages.

## Coverage

| Sub-index | Notes |
|---|---|
| H100 SXM | 80GB SXM module |
| H100 PCIe | 80GB PCIe card |
| A100 80GB | SXM / PCIe 80GB |
| A100 40GB | 40GB |
| Composite | Equal-weight mean of the four NVIDIA sub-indices above (installed-base weights to be calibrated) |
| AMD Instinct MI300X | **Separate sub-index, not in the composite** (OAM, 192 GB; methodology Section 16). Same three methods and small-sample rule; income-approach parameters are placeholders to be calibrated |

Each model's indicative NAV is the equal-weight mean of the three approaches available that day:

- **Market approach:** unweighted median of dealer listings (there are no sold transactions yet; sold = 0).
- **Income approach:** placeholder DCF of the median public on-demand / list rent (USD per GPU-hour).
- **Cost approach:** new-card anchor × remaining-value factor (midpoint for unknown age).

All income- and cost-approach parameters are **placeholders to be calibrated**; see [`docs/methodology.md`](docs/methodology.md). Missing values are `null` in the data and shown as “—” on the dashboard, never as 0.

## Small-sample rule (n < 3 is not published)

- When a method (market / income / cost) has **fewer than 3 observations** for a model on a given day, that method's value is published as `null` together with `insufficient_sample.<method> = true` and its `n`; the dashboard shows “Insufficient sample (n=X)”. Values derived directly from that method are hidden too: the rent median when income n < 3, and the new-card anchor when cost n < 3.
- **The formulas are unchanged:** the upstream pipeline still computes the indicative NAV and the composite from all methods, as the methodology specifies.
- **NAV and composite:** they are published only if, together with all other published values, they cannot be used to back out any hidden method value. Otherwise they are also `null`, with `nav_hidden = true` and a reason. `export_public.py` checks this exactly with linear algebra (can a hidden value be uniquely solved from the published NAV / composite equations?), first for each model NAV, then for the composite. Typical cases:
  - only one method of a model is hidden and the others are published → the NAV would reveal it → the NAV is hidden;
  - the NAV consists of a single method with n < 3 → the NAV equals that value → hidden;
  - the composite is the mean of the four model NAVs; if exactly one model NAV is hidden, the composite would reveal it → the composite is hidden;
  - when two or more hidden methods enter the same NAV, the NAV only reveals their combination, not any single value, so it is published.
- Every point in `history.json` is treated the same way and stores the `n` of each method. Before writing any file, `export_public.py` re-checks the latest data and every history point, and refuses to publish if it finds a value with n < 3 or a NAV / composite that would reveal one.

## Daily update

`.github/workflows/daily.yml` runs every day at **12:30 UTC** (04:30 PST / 05:30 PDT) and can also be started manually from the Actions tab (workflow_dispatch):

1. `pipeline/run_daily.py` fetches public sources; a failing source is logged and does not stop the run. Raw snapshots, the SQLite database and listing-level observations are written to the runner's temporary directory and **discarded when the job ends** — they are never committed, uploaded as artifacts, or cached.
2. `pipeline/export_public.py` turns the day's result into `docs/data/latest.json`, which contains **aggregates only**, and upserts the day's point into `docs/data/history.json` (repeated runs on the same PT date keep the last one).
3. The workflow commits these two JSON files back to `main` and deploys `docs/` to GitHub Pages.

**History continuity:** `docs/data/history.json` is itself the long-term public record. It lives in the repository and each run only adds or updates the current day, so `nav.db` does not need to be kept. The first point (2026-11-16) was produced by `pipeline/seed_from_db.py`, which reruns the upstream `run_daily.py --recompute-from-run` on a copy of the project's internal database (observation-only sources do not enter the official calculation).

**Pipeline identical to upstream:** `pipeline/run_daily.py` is a verbatim, unmodified copy of the project's upstream script (empty diff). Upstream provides two environment-variable hooks that are no-ops by default: `COMPUTENAV_DATA_DIR` (private output directory; defaults to the upstream location) and `COMPUTENAV_SKIP_SOURCES` (skip fetching the listed sources; nothing is skipped by default). It also contains upstream's own competitor exclusion (see *Competitor data*). The pipeline does not read the private source register at runtime. Note: the upstream script contains Chinese comments and internal note strings; these never appear in the published JSON or on the site (`export_public.py` translates or drops them and refuses to publish non-English text).

Running locally:

```bash
COMPUTENAV_DATA_DIR=/tmp/computenav-private \
COMPUTENAV_SKIP_SOURCES=vast_public_feed,vast_bundles,ebay_sold,ebay_active,ebay_sold_mi300x,ebay_active_mi300x \
python3 pipeline/run_daily.py
python3 pipeline/export_public.py --private-latest /tmp/computenav-private/output/latest.json
python3 pipeline/check_public.py --private-latest /tmp/computenav-private/output/latest.json
pip install -r requirements.txt && python3 pipeline/render_methodology.py
python3 -m http.server -d docs 8000   # open http://localhost:8000
# Without COMPUTENAV_DATA_DIR the script writes to the upstream default location.
```

## Data licensing

- **Vast.ai:** its public pricing data terms state that "use in any index, benchmark, or derivative product requires a data license (data@vast.ai)". Per Section 10 of the methodology, Vast.ai is observation-only and **does not enter the official calculation** until licensed; the daily CI does not fetch it at all (`COMPUTENAV_SKIP_SOURCES`).
- **eBay:** there is no licensed API access, and web requests are blocked (403). This project does not bypass any access restriction, and the CI does not request eBay (including the MI300X searches `ebay_sold_mi300x` / `ebay_active_mi300x`).
- **Clustermelt, Mulligan (relays eBay listings):** observation-only upstream; they do not enter the official calculation. Listings from **AnchorGPU and ServerPartsDeals**, and the ServerBuyback broker range for MI300X, do not enter the official median either. None of their raw values are published.
- The public repository and site contain **only per-model aggregates** (NAV, method values, sample counts and medians, subject to the small-sample rule). They do **not** contain any listing-level or rental observation, per-source raw prices, raw snapshots or the database (see `.gitignore`).
- Other public price-list sources (RunPod, Lambda, CoreWeave, etc.) still need their terms confirmed one by one before systematic collection.

## Competitor data

Data from Silicon Data, Ornn and SemiAnalysis, and market data from Compute Exchange (a Silicon Data affiliate), is never used in the ComputeNAV calculation or dashboard data — neither as an input nor for validation or calibration. The methodology mentions them only as competitors. Enforcement, all matched by source id, URL domain and registered name only (never by free text, so a third-party page that merely mentions one of them is unaffected): the upstream pipeline skips and logs any such source and filters its observations; the CI runs `pipeline/check_sources.py` before computing and refuses if one is registered; `export_public.py` refuses to publish if any source, observation or public record matches `pipeline/denylist.py`.

## Currency

All public prices and values are in **USD only**. Prices from non-USD sources are converted to USD internally at the day's FX rate; no foreign-currency amounts or exchange rates are published. `pipeline/check_public.py` (also run by the workflow) and `export_public.py` refuse to publish if a non-USD currency amount appears in any public file.

## Disclaimer

This project was created by a high-school student for learning and research purposes. All data are indicative estimates and may be incomplete or wrong. This is not an official index and does not constitute investment, credit or valuation advice. No warranty of accuracy is given.

## Copyright

© 2026 ComputeNAV project authors. All rights reserved. No licence is granted: this repository has no open-source licence, and no permission is given to copy, modify or redistribute its contents. Being publicly visible does not grant any right of use.
