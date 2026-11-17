# ComputeNAV / AI NAV Index Methodology v0

> **Public copy note:** This document is an English translation of the project's internal methodology_v0.md (2026-11-16 PT). To respect data licences, this public version removes the Vast.ai figures, the eBay asking prices relayed by Mulligan, and private storage paths. All prices are shown in USD only (non-USD sources are converted at the day's FX rate), and individual listing / price-list values in Section 4 are replaced by USD ranges over at least 3 observations. Everything else is unchanged in substance. The public dashboard's daily calculation follows the "source admission criteria" in Section 10 and **does not use Vast.ai data**. In addition, the public data does not publish any method value with fewer than 3 observations (see the repository README). High-school student research project. Indicative data only. Not investment advice.


**Document status:** Draft for review; not an effective benchmark document  
**Version:** v0.1-draft  
**Date:** 2026-11-16 (Pacific Time, PT)  
**Preparation note:** This document is based on same-day tests of public data sources from an isolated environment (HTTP GET/POST, no login and no circumvention). All prices and counts are the results captured that day. Listing prices not verified as transactions are always labelled "listed" and are never treated as realized transactions. Parameters that have not been calibrated are always labelled **to-be-calibrated**; no values are invented.

---

## 1. Index definition

**ComputeNAV** (also called the AI NAV Index) is a daily index of **realizable net asset value (NAV)** for in-service GPUs treated as assets, by "model × age". Its core question is:

> Roughly how many US dollars could one in-service data-center GPU of a given model and age (or one standardized system) be realized for today in an orderly secondary market?

NAV here is on a **liquidation basis**, closer to the appraisal concept of **Orderly Liquidation Value (OLV)**. It is not:

- what a rental / compute-rate index answers ("how much does one hour cost");
- an "income-approach fair value" obtained only by discounting future rents;
- and certainly not an accounting carrying amount / book value.

The official reading of the index is **USD per GPU card**; where necessary a "system-derived per-card price" and a "quick-sale floor" are published alongside. Transaction observations and listing observations are **stored separately, labelled separately and disclosed separately**; they must never be blended into one unlabelled average.

**Design principles:**

1. **Market approach first.** Observable secondary-market sold transactions are preferred; listings are only a secondary input and must always be labelled.  
2. **Three-approach cross-check.** The market approach, the income approach (discounting net cash flows from public rents over the remaining life) and the cost approach (new-card reference price × age depreciation) are computed side by side. When their divergence exceeds a threshold a quality flag is raised, instead of silently substituting a single source.  
3. **Auditable.** Daily raw snapshots carry a timestamp and SHA-256; parsing is separated from fetching; after a methodology change the full history can be recomputed from snapshots.  
4. **Neutral.** The index administrator does not trade, make markets, underwrite, or offer residual-value swaps or loan underwriting.  

---

## 2. Scope — v0

| Item | v0 scope | Explicitly out of v0 scope |
|---|---|---|
| Models | NVIDIA H100 (SXM / PCIe; NVL is a separate observation bucket and is not yet in the composite; whether to merge it into H100 is **to-be-calibrated**) and NVIDIA A100 (40GB / 80GB; SXM / PCIe in separate buckets) | H200, B200, B300, GB200/GB300, AMD Instinct, consumer GPUs |
| Unit | Realizable value per card (USD/card) | Unsplit racks, non-standardized full-rack NVL72 |
| Geography | Mainly publicly available USD quotes or quotes convertible at the day's FX rate; original currency and tax basis are recorded | No claim of covering transactions in all jurisdictions worldwide |
| Frequency | Target: one official reading per business day; the v0 data collector runs daily | Intraday real-time NAV |
| Age | Output by "model × form factor × memory × age bucket"; when age is missing the observation goes into an "age unknown" bucket — **age is never guessed** | Treating unknown age as new or as average age |
| Condition | New, refurbished, used-tested, untested, parts only; bucketed separately | Blending conditions without disclosure |

The composite index is weighted by installed base; the weight source and values are **to-be-calibrated**. Until the weights are set, only per-model sub-indices are published, not a falsely precise composite level.

---

## 3. Competitor comparison

**Residual value / NAV is no longer an empty space.** In 2026 at least three firms have extended from rental indices into residual value, collateral or hedging. ComputeNAV's differentiation is not "being the first to talk about residual value" but: **an asset-level, auditable NAV with a public methodology, based primarily on observable secondary-market liquidation (OLV), whose administrator neither trades nor underwrites.**

All statements below were checked against the original pages opened on 2026-11-16, not second-hand summaries.

### 3.1 Silicon Data — GPU Residual Value

- **Launch:** 2026-08-24 (press release time 13:30 GMT). Source: [EIN Presswire press release](https://www.einpresswire.com/article/936056292/silicon-data-launches-monthly-gpu-residual-value-benchmarks-for-ai-infrastructure-finance).  
- **Coverage:** Monthly fair values for H100, A100 and B200, for asset-backed financing, fleet planning and mark-to-market. Same press release.  
- **Method:** The product page describes a **continuous DCF**: months 0–36 use the GPU Forward Curve rents; after month 36 a compounding tail decay is applied to the last forward value; utilization decays exponentially as \(k \cdot e^{-rt}\); operating cost uses a global blended fixed ratio; residual values are recomputed when the forward curve moves. Source: [product page](https://www.silicondata.com/products/gpu-residual-value).  
- **Nature:** "Fair value implied by future rents", not a realizable NAV based mainly on secondary transactions. The press release says each estimate represents a GPU's "depreciated value implied by the net rental income it is expected to generate over its remaining useful life".  
- **Governance:** The rental-index governance document states alignment with the IOSCO Principles for Financial Benchmarks. Source: [governance PDF](https://www.silicondata.com/documents/silicon-data-index-governance.pdf). Some residual-model details are public in the product FAQ, but parameters needed for a fully reproducible formula (decay rate, initial utilization, operating-cost ratio, etc.) are portal-only; this document does not guess them.

### 3.2 Ornn — Residual Value Swap and Depreciation Curves

- **GPU Value Protection / Residual Value Swap (RVS):** Page dated 2026-01-25. The product guarantees a minimum sale price for a GPU at a future date; the holder may choose to sell at the agreed price. The text uses both "quarterly premium" and "upfront premium", an internal inconsistency this document does not reconcile. Source: [Ornn insights page](https://ornn.com/insights/residualvalue). This is a **risk-underwriting / swap product**, not a public liquidation NAV index.  
- **Depreciation Curves:** On 2026-11-10 Ornn's official account posted that it is "modeling GPU hardware values from A100 to GB300 using real transaction data", and that the curves can give lenders and fleet operators a basis for estimating residual value. Source: [original post on X](https://x.com/OrnnExchange/status/2120258550488338902) (timestamp: 2026-11-10, 8:53 PM; the post links to [data.ornn.com/analytics](https://data.ornn.com/analytics)).  
- **Transparency:** On opening the analytics and methodology pages, **no** independently reproducible Depreciation Curves formula, sample definition or rule separating transactions from listings was found. Ornn only states that it uses "real transaction data"; the methodology is not fully public. The OCPI rental-transaction index itself has a public methodology ([data.ornn.com/methodology](https://data.ornn.com/methodology)); OCPI's transparency should not be extrapolated to the Depreciation Curves.

### 3.3 Liquid Compute — LCI puts and portfolio marks

- An October 2026 insight article says **cash-settled puts on the LCI (Liquid Compute Index)** can set a floor on compute rates and thereby price residual / re-leasing risk, and says "If you're a lender with a GPU book, we can put a mark on it and a hedge around it." Source: [Hedging when credit tightens](https://liquidcompute.com/insights/hedging-when-credit-tightens).  
- The "advance rate raised from 65% to 85%" in the same article is **illustrative**, not a disclosed market statistic. LCI values, history and methodology are not visible on public pages.  
- The article states that Liquid Compute is not the counterparty to these OTC options, and that the derivatives exchange (PMEX) is awaiting CFTC DCM/DCO designation. This is a **hedging and marking service**, not an independent, public, auditable realizable-NAV index.

### 3.4 How ComputeNAV differs from the three

| | Silicon Data Residual Value | Ornn Depreciation Curves / RVS | Liquid Compute LCI put / mark | ComputeNAV (this index) |
|---|---|---|---|---|
| Main input | DCF of rental forward curve | Claimed transaction data (curve method not public); RVS is a floor contract | Rental-index puts + internal quotes/trade logs | **Secondary transactions and listings (kept separate)**; rents and depreciation are cross-checks only |
| Value basis | Income-approach fair value | Non-public hardware value curve; RVS is a contractual floor | Rate floor / portfolio mark | **Realizable / OLV (liquidation-basis NAV)** |
| Granularity | Monthly estimate by generation | Curves (gated page) | Portfolio / trade level | **Per asset: model × form factor × memory × age** |
| Methodology | DCF framework public on product page; some parameters not fully given | Depreciation Curves method not public | LCI method not public | **Fully public; recomputable from snapshots** |
| Conflict-of-interest structure | Index company; sister company Compute Exchange trades | Runs index, marketplace and residual-value swaps (takes risk) | Runs index, matching, financing and hedging services (says it is not a principal counterparty) | **Does not trade, make markets, underwrite or lend** |

**Regulatory context (not an application status of this index):** The CFTC request for comment on compute derivatives (RIN 3038-AF77, 91 FR 54259) closed on 2026-10-20. That proceeding concerns rental-price contracts and does not cover benchmarks referencing liquidation / collateral value. This leaves room for a NAV index but does not constitute any regulatory endorsement.

---

## 4. Data sources and the 2026-11-16 survey

Survey principle: report only what actual requests returned; if blocked, say blocked; if the sample is thin, say thin. Raw bytes, HTTP status and SHA-256, the source register and the day's price samples are kept in the project's private storage (not published with the public repository).

Fetch window approx. 2026-11-16 19:45–20:30 PT (2026-11-17 03:45–04:30 UTC). All prices in this public document are shown in USD only. Non-USD source prices are converted to USD internally at the day's FX rate (open.er-api.com, last updated 2026-11-17 00:02:31 UTC); FX conversion is used only for comparison and is **not the valuation itself**.

### 4.1 Market-approach sources (main weight, but zero sold transactions today)

**Sold transactions obtained today: 0.** This is the most important fact at v0 launch and must appear as a quality flag in every trial calculation.

| Source | Access result | Relevant data points today | Suggested role |
|---|---|---|---|
| eBay active / sold pages | HTTP 403 | 0 | Target primary source (sold); requires the official Browse / partner API; captcha circumvention is forbidden |
| eBay Browse API (no key) | HTTP 403 | 0 | Same; retest after obtaining developer credentials |
| Genius HPC secondary catalog | 200, HTML with fixed ex-VAT prices in a non-USD currency (converted to USD) | 6 listed SKUs (incl. 1 DGX system) + 6 "New From" new-card prices on the same page | The more usable dealer listings in v0; still asks, not transactions |
| Creative Compute (UK) | 200; product pages with ex-VAT prices in a non-USD currency (converted to USD) | 3 (H100 card, A100 card, 8×A100 system) | Regional listings; enquiry, not an order |
| AnchorGPU | 200 | 3 prices; page states **orders are not open yet** | Reference listings only; weight should be very low |
| ServerPartsDeals 8×H100 system | 200 (product page); `available=false` | 1 delisted / unavailable listing | Usable as a worked example of splitting a system into cards; **not in the calculation for now** |
| Clustermelt monthly ranges | 200 | Low–high ranges for 6 models (main reading dated 2026-10-01) | Cross-check only; zero weight in the v0 official calculation |
| Mulligan (eBay listing tracker) | 200 | "Lowest ask" for 3 models (updated 2026-11-16; self-described as not transaction prices) | Watch only; not re-verified on eBay from this environment; zero official weight |
| ServerMonkey / Newegg | 403 | 0 | Blocked |
| TechMikeNY, CDW, Exxact, Thinkmate, etc. | 200 but no usable prices in static HTML | 0 | Needs a licensed API or a later permitted collection method |
| GovDeals / GovLiquidation | 403 | 0 | Liquidation auctions are the ideal OLV source; unreachable today |
| IronPlanet | 405 | 0 | Blocked |
| HiBid / Public Surplus | No relevant lots | 0 | No H100/A100 lots found that day |
| americancompute.com | Parked page | 0 | Do not use |
| Compute Exchange | 200, only navigation for reserved rental products | 0 secondary sale prices | Keep watching; not a current public transaction tape |

**Dealer listings captured that day (all listed, not sold), USD ranges only:**

To stay consistent with the public small-sample rule, individual listing prices are not published. The ranges below are per-card dealer listings (Genius HPC used, Creative Compute refurbished, AnchorGPU) from the 2026-11-16 pipeline run (run 20261116T212104_PT, 21:21 PT), converted to USD at the day's FX rate and rounded outward to the nearest $1,000. Groups with fewer than 3 listings are not shown.

| Bucket | Listings (n) | USD range per card |
|---|---|---|
| H100 SXM | 2 | not shown (n < 3) |
| H100 PCIe | 3 | $12,000 – $21,000 |
| A100 80GB | 4 | $5,000 – $10,000 |
| A100 40GB | 0 | — |
| H100 NVL 94GB | 1 | not shown (n < 3) |

- 8-GPU systems (Genius HPC DGX H100, Creative Compute 8×A100, ServerPartsDeals Dell XE9680 8×H100 — the last one **currently unavailable**): system prices are not published. Simply dividing a system price by 8 is not a per-card price: CPUs, memory (e.g. 32×64GB DDR5), chassis and power supplies must be deducted before a system can enter a per-card bucket — **this document does not give an uncalibrated deduction**.  
- eBay lowest asks relayed by Mulligan: **removed from the public version** (the data derives from eBay listings and inherits eBay's and Mulligan's terms of use; not published without authorization).  

On the same day, under the same "used H100 PCIe / SXM" label, European dealer ex-VAT listings, UK enquiry prices, US reference prices and third-party eBay asks are **extremely dispersed**. v0 must not squeeze these into one "market price" by simple averaging. This is exactly why observations must be separated by source, by currency and tax basis, and by executability, while waiting for transaction samples.

### 4.2 Income-approach sources (public rents)

Rents are an upstream input to NAV, not a competing substitute. Today this layer is **clearly thicker than the market approach**.

| Source | Result | H100/A100 observations today | Role and limits |
|---|---|---|---|
| Vast.ai public pricing feed | 200, JSON, updated 2026-11-17T03:30:23Z | min / p10 / median / available count for 5 models, each with up to 90 days of history | Market-style listed rental statistics. **Licence statement: use in an index, benchmark or derivative data product requires a data licence from data@vast.ai.** Research citation requires attribution. Also: the feed labels A100 SXM4 vram as 80GB, but some offers in bundles have `gpu_ram=40960`; offer-level memory must take precedence. |
| Vast.ai bundles API (keyless POST) | 200; GET 400, PUT 404 | (Vast.ai details removed from the public version: licence pending) | Provides per-offer rent, memory and location. H100 is thin in this sample. Subject to the same data licence. |
| RunPod GraphQL `gpuTypes` (usable without a key this time) | 200 | 6 relevant gpuTypes, incl. Community / Secure / term prices | Cloud provider price list. Community and Secure must be kept separate. Keyless access is not guaranteed in future. |
| Lambda public pricing page | 200 (official instance-types API returns 401) | Multiple instance and 1-Click Cluster prices | Price list; reserved cluster unit prices are higher than single-card instances and must be separated by term. |
| DigitalOcean, CoreWeave, Crusoe, Nebius, Together, Hyperstack, Modal, Voltage Park, Massed Compute | Mostly 200 | ~20+ parseable H100/A100 list prices | Forms the neocloud panel. 8-card node prices must be divided by card count and recorded. Some pages show "from / reserved / spot" at the same time; prices whose term is **unconfirmed** must not be mixed. |
| AWS p5 / GCP / Azure pages | 200 but no usable USD unit price parsed | 0 usable unit prices (AWS only parsed p5 specs: p5.4xlarge = 1×H100) | Important but incomplete; switch to each cloud's official pricing API; hyperscaler unit prices **must not be invented**. |
| GPUHosted prices.json | 200, but file updated 2026-08-25 | No usable same-day model prices | Zero official weight |

**Rental samples (all list prices or market asks, not executed long-term contract transactions unless the source says otherwise):**

- Vast.ai medians: **removed from the public version** (use of Vast.ai data in an index / redistribution requires a data licence, data@vast.ai).  
- Individual price-list values are not published. The ranges below cover the on-demand / list rents admitted to the income approach in the 2026-11-16 pipeline run (run 20261116T212104_PT; Vast.ai excluded; reserved, spot and unclear-term prices excluded), normalized to USD per GPU-hour (8-GPU node prices divided by 8; per-second prices converted to hours) and rounded outward to $0.10.

| Bucket | Rents (n) | USD range per GPU-hour |
|---|---|---|
| H100 SXM | 11 | $1.80 – $6.20 |
| H100 PCIe | 3 | $1.90 – $2.80 |
| A100 80GB | 9 | $1.10 – $3.20 |
| A100 40GB | 4 | $1.00 – $2.10 |

Public asks for the same H100 SXM differ several-fold between sources and source types (see the range above). The income approach must be stratified by **source type (market-style vs price list vs reserved)** and must not be merged into one unlabelled "rent".

### 4.3 Cost-approach sources (new-card anchors, depreciation lives, credit terms)

| Source | Result | Use |
|---|---|---|
| Genius HPC "New From" | Same page as used: one ex-VAT "New From" price each for H100 PCIe, H100 SXM, A100 80GB PCIe and A100 80GB SXM, in a non-USD currency (converted to USD; individual values not published, n = 1 per bucket) | **Interim** new-card reference. This is one European dealer's "new from" price, not NVIDIA's official MSRP and not a US channel price. |
| CDW / Newegg and other US channels | No usable unit price obtained (403 or unparseable) | Official US new-card reference **still to be added** |
| CoreWeave Form 10-K (FY2025, filed 2026-03-02, accession no. 0001769628-26-000104) | "Technology equipment" straight-line depreciation over **6 years** (changed from 5 to 6 years effective 2023-01-01). DDTL facilities finance purchases of compute equipment, secured by first-priority pledges of subsidiary equity and substantially all assets. **No** numeric LTV / advance rate was found in the excerpts searched. | Anchor for accounting useful life and equipment-financing structure. 6 years is a book life, not an economic residual curve. |
| IREN Ltd Form 10-K (fiscal year ended 2026-06-30, filed 2026-08-27, accession no. 0001878848-26-000052) | Pledged GPU assets are depreciated straight-line over an estimated useful life of **5 years**. GPU Financing: financing for **100%** of purchase price, structured as 24- and 36-month leases. Net carrying amount of GPUs and related infrastructure held and pledged by IE US Hardware 3, LLC at 2026-06-30: **$1,487.1 million**. | Another issuer's 5-year life anchor, plus public GPU-secured financing terms. 100%-of-purchase-price lease financing is **not the same as** an ordinary secured-loan LTV. |
| Applied Digital 10-K | PPE table shows networking/electrical/software at 3–5 years etc., **no separate GPU useful-life line** | Not used as a GPU life default |

All cost-approach parameters (new-card price, residual rate, life, residual floor) are **to-be-calibrated**. CoreWeave's 6 years and IREN's 5 years only show that "issuer accounting policies are not uniform"; they cannot be written in as the index's official life.

---

## 5. Valuation approaches and formulas

Each day, for every standard bucket \( b = \) (model, form factor, memory, age bucket, condition bucket), three candidate values are computed in parallel and then combined into the official NAV under the rules of Section 8. The currency unit is always **USD/card**. Foreign currencies are first converted at the FX source published for the valuation date (v0 suggests an archivable public mid rate, with source and time stated in the release notes), and the original-currency amount is retained.

### 5.1 Market approach — main weight

Observations in bucket \( b \) are first normalized per Section 6, then outliers are handled per Section 7, and then the following are computed separately:

\[
M^{sold}_{b,t} = \mathrm{WeightedMedian}\bigl(\{p_{i,t}^{sold}\}\bigr)
\]

\[
M^{list}_{b,t} = \mathrm{WeightedMedian}\bigl(\{p_{j,t}^{list}\}\bigr)
\]

\[
M_{b,t} = \frac{w^{sold} M^{sold}_{b,t} + w^{list} M^{list}_{b,t}}{w^{sold} + w^{list}}
\]

where \( w^{sold} > w^{list} \ge 0 \); the exact values are **to-be-calibrated**. If the number of transactions on the day (or in the rolling window; window length **to-be-calibrated**) is 0, \( M^{sold} \) is recorded as missing, and listing prices must not be passed off as transaction prices. \( M_{b,t} \) may temporarily equal \( M^{list}_{b,t} \), but the bucket must then carry a **"listings-only"** quality flag and its weight in the composite must be reduced (down-weighting rule **to-be-calibrated**).

Weights may consider source credibility, executability, whether tax/shipping is included, and sample size; the scheme is **to-be-calibrated**. v0 suggests at least separating "non-executable reference prices" (such as catalog prices for which ordering is not yet open) from "dealer listings available on enquiry".

### 5.2 Income approach — cross-check

\[
I_{b,t} = \sum_{k=1}^{K_b} \frac{r_{b,t,k}\cdot H_k\cdot u_{b,t,k} - c_{b,t,k}}{(1 + d_{t,k})^{\tau_k}} + \frac{TV_{b,t}}{(1+d)^{T_b}}
\]

- \( r_{b,t,k} \): expected realizable rent in period \( k \) (USD/GPU-hour), from the public rental panel, stratified by source type. The forward shape is **to-be-calibrated** (a term structure, or a decay applied to observed rents, may be used, but other vendors' non-public parameters must not be copied before calibration).  
- \( H_k \): hours in the period (calendar hours or rentable hours; definition **to-be-calibrated**).  
- \( u_{b,t,k} \): utilization, **to-be-calibrated**. Must not be assumed to be 100%.  
- \( c_{b,t,k} \): operating costs such as power, rack space and operations, **to-be-calibrated**. The assumption that "a single global ratio is necessarily right" is not adopted; a disclosable base scenario may be used first, together with sensitivity ranges.  
- \( d \): discount rate, **to-be-calibrated**.  
- \( T_b \) / \( K_b \): remaining economic life, **to-be-calibrated** (accounting lives are reference only).  
- \( TV \): terminal value, **to-be-calibrated**; it may also be set to zero with a shorter life, but this must be fixed and disclosed in the methodology.

In ComputeNAV the income approach serves as a **cross-check and supplement when samples are insufficient**, not as the primary definition of the official NAV. If the divergence between \( I_{b,t} \) and \( M_{b,t} \) exceeds a threshold (threshold **to-be-calibrated**), both are disclosed on publication together with possible reasons (e.g. a new generation launching so that used prices lag, or different liquidity in the rental and secondary markets).

### 5.3 Cost approach — anchor and bounds

\[
C_{b,t} = P^{new}_{m(b),t} \cdot D_m(a_b) \cdot \gamma^{cond}_b \cdot \gamma^{complete}_b
\]

- \( P^{new} \): new-card reference price for the same model, form factor and memory. Source priority **to-be-calibrated**; until US channel prices are available, a single dealer's "New From" price may only be used as a scenario anchor, not as the official \( P^{new} \).  
- \( D_m(a) \): depreciation function of age \( a \); its form (straight-line, declining balance, or a piecewise market-calibrated curve) is **to-be-calibrated**. Accounting lives (e.g. one issuer's 5 or 6 years) are only used to set scenarios and do not automatically equal \( D \).  
- \( \gamma^{cond} \), \( \gamma^{complete} \): adjustments for condition and for missing modules/accessories, **to-be-calibrated**. They must not silently default to 1 without disclosure.

The cost approach is mainly used to give a reasonable range, to stop a few abnormal listings from pulling the index away, and to provide an explainable placeholder when the market approach is completely empty (placeholders must carry a flag).

### 5.4 Combination within a bucket

\[
\mathrm{NAV}_{b,t} = w^M_{b,t} M_{b,t} + w^I_{b,t} I_{b,t} + w^C_{b,t} C_{b,t}
\]

Constraint: \( w^M + w^I + w^C = 1 \), and when transaction samples are sufficient \( w^M \) should be significantly larger than the other two. Weights may adjust dynamically with sample size; the adjustment formula is **to-be-calibrated**. During the launch period with zero transactions, the administrator may:

- publish only an "indicative NAV", or  
- publish the three component values without combining them into a single point, or  
- combine them into a single point but state "no transactions, indicative" at the top of the page.

v0 suggests that until continuous transaction samples reach the minimum standard set by the committee (**to-be-calibrated**), **all three components M / I / C are published daily**, and any combined point that is published is always labelled indicative.

---

## 6. Normalization

Each raw observation is mapped to at least the following fields (missing fields are explicitly recorded as unknown; speculative back-filling is forbidden):

1. **Model and architecture** (H100 / A100, excluding mismatched accessories, adapters and picture-only listings).  
2. **Form factor**: SXM / SXM5, PCIe, NVL; if undeterminable, a separate bucket.  
3. **Memory**: 40 / 80 / 94 (NVL) GB; when sources conflict, the finer-grained field (e.g. `gpu_ram`) prevails.  
4. **Condition**: new / refurbished / used-tested / used-unspecified / for-parts.  
5. **Sold vs listed vs price list vs reference**.  
6. **Time**: transaction date or fetch date, recorded in UTC; external reports also give PT.  
7. **Currency, whether VAT is included, whether shipping/duties are included, whether executable**.  
8. **Unit**: single card, multiple cards, full system, full node.  
9. **Age or year placed in service** (if available); power-on hours, burn-in report (if available).

**Splitting a system into cards:**

\[
p^{card}_{i,t} = \frac{P^{system}_{i,t} - \widehat{V}^{host}_{i,t}}{N^{gpu}_i}
\]

The method for estimating the host portion (CPU, memory, drives, NICs, chassis, power supplies, remaining warranty value) is **to-be-calibrated**. Until calibrated, system observations may go into a "system bucket" or an appendix only; **"total price / number of cards" must not be written directly into the official per-card NAV**. The ServerPartsDeals and Creative Compute / Genius HPC system samples in this document are exactly why this deduction is needed.

**Rent normalization:** Always USD per GPU-hour; 8-card node prices divided by 8; state on-demand / reserved / spot / community / secure; reserved terms must not be mixed with on-demand prices without adjustment.

**Duplicates and abnormal units:** Repeated fetches of the same SKU from the same merchant are de-duplicated; obvious accessories, software licences, rental deposits and prices inconsistent with the title are marked reject and kept in the raw store.

---

## 7. Outlier rules

Rules are **to-be-calibrated**. The v0 intent (not yet frozen) is as follows and must be confirmed in writing by the index committee before implementation:

- Within each bucket's rolling window, estimate robust dispersion of log prices (e.g. interquartile range or median absolute deviation).  
- Observations beyond the median ± \( k \) times the dispersion are down-weighted or removed; \( k \) is **to-be-calibrated**.  
- Removal or down-weighting must be reversible: raw points remain archived and can be restored on recomputation.  
- Points must not be deleted by hand because they "look too cheap or too expensive"; if human judgement is used it must be recorded as expert judgement per Section 10.  
- Structural differences across sources (e.g. European ex-VAT dealer prices vs US asks including platform fees) should be normalized first before judging outliers, so that basis differences are not mistaken for anomalies.

---

## 8. Composite index, residual value curve and quick-sale floor

### 8.1 Composite index

\[
\mathrm{NAV}^{comp}_t = \sum_{b \in \mathcal{B}_0} s_{b,t}\, \mathrm{NAV}_{b,t}
\]

\(\mathcal{B}_0\) is the set of buckets included in the v0 composite (the officially defined H100 SXM/PCIe and A100 40G/80G buckets; exact list **to-be-calibrated**). \( s_{b,t} \) is the in-service installed base or a publicly available proxy weight; source and update frequency **to-be-calibrated**. Until the weights are ready, no composite level is published, only sub-indices.

### 8.2 Residual value curve

For each model \( m \), the \(\mathrm{NAV}_{m,a,t}\) values of the age buckets are joined into a curve, and the ratio to the new-card price may be published alongside:

\[
R_{m}(a,t) = \frac{\mathrm{NAV}_{m,a,t}}{P^{new}_{m,t}}
\]

Observations of unknown age do not enter curve points and only appear in an "unknown age" note. When samples are insufficient the curve should be shown as ranges or discontinuous points; smoothing functions must not be used to fill in false precision.

### 8.3 90-day quick-sale floor

For the liquidation scenario of "must be realized within about 90 days", define:

\[
F_{b,t} = q_{b,t} \cdot \mathrm{NAV}_{b,t}
\]

The discount factor \( q \) is **to-be-calibrated** (it can be estimated from model liquidity, the recent spread between transactions and listings, or observed quick-sale transactions). Before calibration, a single floor point may be withheld and only the method described. An empirically unsupported round percentage must not be written in as the official floor.

---

## 9. Data integrity / audit trail

The daily pipeline has two non-overlapping stages:

1. **Ingest.** For each authorized source: save the raw response bytes (or the licensed API payload), final URL, HTTP status, fetch time (UTC), non-secret request headers, and the **SHA-256** of the raw bytes. Old snapshots are kept after website redesigns.  
2. **Parse / calculate.** The parser version is written into every normalized record. After a change to the calculation method or parsing rules, **all affected series must be recomputed** from historical snapshots, and the change log must publish old values, new values and the reason.

Any day's official NAV should be traceable to: (a) the list of normalized observations that entered the calculation and their weights; (b) the hashes of the corresponding raw snapshots; (c) the methodology version in force at the time. The index administrator does not modify archived raw snapshots. If a source later deletes a page, the archived snapshot still prevails, with a note that "the source page may have changed".

The target location for v0 code and archives is storage controlled by the index administrator (the survey files for this round are kept in the project's private storage). A production retention period of no less than **seven years** is suggested (aligned with common benchmark governance practice and record-keeping periods in some issuers' governance documents); the final period is **to be confirmed by the committee**.

---

## 10. Governance

ComputeNAV's governance aims to **align with** the requirements of the IOSCO Principles for Financial Benchmarks that are relevant to this project, in particular: clarity of benchmark design, data sufficiency and hierarchy, transparency, separation of conflicts of interest, change and cessation, accountability and complaints. This alignment is a **self-declaration**; it does not mean external certification has been completed or that the project is a regulated benchmark administrator.

**Proposed structure (people and charter to be established):**

- **Index Committee:** Responsible for methodology approval, parameter calibration, exception handling, source admission and exit, and quality-flag policy. Meetings should have written minutes.  
- **Operations and technology function:** Responsible for collection, parsing, calculation and publication; separate from the committee.  
- **Conflicts of interest:** The index administrator does not trade the underlying assets, make markets in ComputeNAV derivatives, underwrite residual value, or make loans priced off this index. Any future related data-licensing or distribution agreements must be publicly disclosed.  
- **Methodology changes:** Routine parameter recalibration should be distinguished from structural revisions. Structural revisions should be announced in advance (lead time **to-be-calibrated**) and, where possible, run in parallel first. Emergency fixes (obvious errors) may be corrected first and announced afterwards, but the audit trail must be kept.  
- **Expert judgement:** Used only for missing data, source outages, or classification problems that clearly cannot be handled automatically; each judgement records the reason, person and time, and where possible an estimate of its impact on the index.  
- **Complaints and corrections:** A public contact channel should be provided; verified calculation errors should be corrected in the historical values and announced.

Source admission criteria (**to be refined**): legally obtainable, archivable, fields sufficient for normalization, acceptable ongoing availability, and a licence that permits benchmark use. The Vast.ai public feed states that index use requires a separate licence — until that licence is obtained, the feed must not enter the official calculation and may only be used as a research comparison.

---

## 11. Known limitations

1. **The transaction tape is almost empty.** Public HTTP tests on 2026-11-16 did not capture a single sold H100/A100 transaction. Until this changes, any single-point NAV can only be indicative.  
2. **Listings are highly dispersed and on inconsistent bases.** H100 listings from different sources on the same day can differ several-fold; some are non-executable, some ex-VAT, some full systems.  
3. **Age and usage intensity are generally missing.** Without age, a reliable residual curve cannot be built.  
4. **Access restrictions.** eBay, major auction houses and some dealers return 403/405 to keyless HTTP; samples must not be "thickened" by captcha circumvention or non-compliant scraping.  
5. **Licence restrictions.** At least Vast.ai explicitly restricts use of its public pricing feed in indices/benchmarks; systematic collection from other sources must also be confirmed source by source.  
6. **No US new-card reference price today.** The cost approach's \( P^{new} \) does not yet have a qualified US channel anchor.  
7. **Rents are asks / list prices, not all executed contracts.** Using them in a DCF inherits model risk from utilization, operations and discount assumptions. This is also why this index does not treat the income approach as its primary definition.  
8. **Installed-base weights are not set.** The composite is not yet available.  
9. **Geography, tax, shipping and warranty** materially affect realizable value; v0 can only label them and cannot yet make full net-proceeds adjustments.  
10. **This index has not been recognized by any regulator**, and must not be described as a benchmark approved by the CFTC or SEC.

---

## 12. v0 roadmap (60–90 days)

The goal follows the same starting sequence as Silicon Data: **first get the machine collecting, cleaning and archiving prices every day and build a verifiable history, then talk about distribution and contracts.**

| Phase | Approx. timing | Content |
|---|---|---|
| Weeks 1–2 | Collector goes live | Daily snapshots of sources with confirmed licences (dealer catalogs, licensed rental APIs, periodic EDGAR searches). Complete the source register and hash archiving. Start **formal authorization** requests for eBay / auction data; no circumvention scraping. Contact Vast.ai and others for the required data licences. |
| Weeks 3–6 | Normalization and trial calculation | Launch parsers, trial rules for splitting systems (parameters still in trial), the three-approach side-by-side table, and quality flags. Output component values daily without rushing to treat a combined point as official. Add US new-card reference prices. Start tracking data-quality metrics such as the "missing-age rate". |
| Weeks 7–12 | Build history | Run continuously to a full 60 days, aiming for 90. During this period, back-filling uncollected dates to "look complete" is forbidden. The committee freezes candidate first-version weights and outlier rules and runs parallel trial calculations. If transaction samples are still very thin, keep the indicative status and say so publicly. |
| After 60–90 days | Assess moving to v1 | Promoting a subset to an official index is considered only when: the methodology has been approved by the committee; the audit trail is complete; key source licences are in place; quality-flag definitions are stable; and limitations remain publicly disclosed. |

---

## 13. Parameters to be calibrated (this document must not be used to fill in values)

- Relative weights of sold vs listed \( w^{sold}, w^{list} \)  
- Weights for combining the three approaches and rules for varying them with sample size  
- Rolling window length, outlier multiple \( k \), minimum sample size  
- Income approach: rent term structure, utilization path, operating costs, discount rate, remaining life, terminal value  
- Cost approach: official new-card price source, form of the depreciation function, condition factors  
- Method for valuing the host portion of systems  
- Source of installed-base weights  
- 90-day quick-sale discount \( q \)  
- Advance notice period for methodology changes, record retention period  
- Whether NVL is included in the H100 sub-index; official bucket list for the composite  

---

## 14. Appendix: location of survey archives

- Source register, the day's price samples, raw responses (including the bytes needed for hash verification): project private storage, **not published with the public repository** (some sources' terms do not allow redistribution).  
- Daily pipeline code: `pipeline/run_daily.py`; public aggregate export: `pipeline/export_public.py`.  
- This methodology: `docs/methodology.md`.  

This document contains no forecast of future index levels and does not constitute investment, credit or valuation advice.
