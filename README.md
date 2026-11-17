# ComputeNAV · GPU NAV 指数（研究项目）

> **高中生研究项目 · High-school student research project**
> **指示性数据，尚无成交样本 · Indicative data only, no sold transactions yet**
> **不构成投资建议 · Not investment advice**

ComputeNAV 是一个每日更新的二手数据中心 GPU **指示性可变现净值（NAV，美元/卡）** 研究指数，配一个静态网页看板（GitHub Pages）。

## 覆盖范围

| 子指数 | 说明 |
|---|---|
| H100 SXM | 80GB SXM 模块 |
| H100 PCIe | 80GB PCIe 卡 |
| A100 80GB | SXM / PCIe 80GB |
| A100 40GB | 40GB |
| 综合 | 四只子指数等权平均（装机量权数待校准） |

每个型号的指示性 NAV = 当日可得的三种方法的等权平均：

- **市场法（market）**：经销商在售挂牌的不加权中位数（尚无成交样本，sold = 0）。
- **收益法（income）**：公开按需/价目租金（USD/GPU-小时）中位数的占位 DCF。
- **成本法（cost）**：新卡锚点 × 剩余价值系数（未知机龄取中点）。

所有收益法/成本法参数都是 **待校准的占位假设**，详见 [`docs/methodology.md`](docs/methodology.md)。缺失的项在数据中为 `null`，看板显示为 “—”，不会当作 0。

## 样本量规则（n < 3 不公开）

- 任一型号的某个方法（市场/收益/成本）当日样本数 **n < 3** 时，公开数据中该方法的数值为 `null`，并给出 `insufficient_sample.<方法>=true` 和 `n`；看板显示「样本不足 (n=X)」。由该方法直接得出的数值也一并隐藏：收益法 n<3 时隐藏租金中位数，成本法 n<3 时隐藏新卡锚点。
- **公式不变**：上游 `run_daily.py` 仍按方法论用全部分项计算指示性 NAV 和综合指数。
- **NAV / 综合的处理**：只有在“和其他已公开数值放在一起也无法反推出任何被隐藏分项”时才公开；否则也为 `null`，并标 `nav_hidden=true` 和原因。`export_public.py` 用精确的线性代数判断（隐藏分项是否能被已公开的 NAV/综合方程唯一解出），先判断各型号 NAV，再判断综合。典型情况：
  - 某型号只有一个分项被隐藏，其余分项已公开 → NAV 可反推该分项 → 隐藏 NAV；
  - NAV 只由一个 n<3 的分项构成 → NAV 就等于该分项 → 隐藏；
  - 综合 = 四个型号 NAV 的平均，若恰有一个型号 NAV 被隐藏 → 综合可反推它 → 隐藏综合。
  - 两个及以上被隐藏分项共同进入一个 NAV 时，公开的 NAV 只能反推出它们的组合，不能还原任何单个分项，因此照常公开。
- `history.json` 的每个点同样处理，并保存各方法的 n。`export_public.py` 在写文件前会对 latest 和全部历史点重新检查，发现 n<3 的数值或可反推的 NAV/综合就拒绝发布。

## 每日更新流程

`.github/workflows/daily.yml`，每天 **12:30 UTC**（= 太平洋时间 04:30 PST / 05:30 PDT）自动运行，也可在 Actions 页面手动触发（workflow_dispatch）：

1. `pipeline/run_daily.py` 抓取公开来源，单源失败只记日志不中断；原始快照、SQLite 库与观测明细全部写在 runner 的临时目录，**任务结束即丢弃**，不提交、不上传 artifact、不放 cache。
2. `pipeline/export_public.py` 把当天结果转成 **只含汇总值** 的 `docs/data/latest.json`，并把当天的点 upsert 进 `docs/data/history.json`（同一 PT 日期重复运行以最后一次为准）。
3. 工作流把这两个 JSON 提交回 `main`，然后把 `docs/` 部署到 GitHub Pages。

**历史连续性**：`docs/data/history.json` 本身就是长期公开记录，保存在仓库里，每次运行只追加/更新当天的点，因此不需要保留 `nav.db`。2026-11-16 的首个点由 `pipeline/seed_from_db.py` 用上游的 `run_daily.py --recompute-from-run` 在内部数据库副本上重算得到（仅观察源不进入官方计算）。

**管道脚本与上游一致**：`pipeline/run_daily.py` 是内部上游脚本的逐字副本，只加了两个默认无效（no-op）的环境变量钩子：`COMPUTENAV_DATA_DIR`（私有输出目录，默认与上游相同）和 `COMPUTENAV_SKIP_SOURCES`（跳过指定源的抓取，默认不跳过）。差异见 `pipeline/upstream-hooks.patch`，可直接并回上游，此后两边可以完全相同。

本地运行：

```bash
COMPUTENAV_DATA_DIR=/tmp/computenav-private \
COMPUTENAV_SKIP_SOURCES=vast_public_feed,vast_bundles,ebay_sold,ebay_active \
python3 pipeline/run_daily.py
python3 pipeline/export_public.py --private-latest /tmp/computenav-private/output/latest.json
pip install -r requirements.txt && python3 pipeline/render_methodology.py
python3 -m http.server -d docs 8000   # 打开 http://localhost:8000
# 注意：不设 COMPUTENAV_DATA_DIR 时脚本与上游一样写入 /workspace/computenav
```

## 数据许可（Data licensing）

- **Vast.ai**：其公共定价数据条款写明“用于任何指数、基准或衍生产品须取得数据许可（data@vast.ai）”。按方法论第 10 节，取得许可前 Vast.ai 只作观察、**不进入官方计算**；每日 CI 中干脆不抓取（`COMPUTENAV_SKIP_SOURCES`）。
- **eBay**：没有经授权的 API 访问，网页抓取会被拦截（403），本项目不绕过任何访问限制，CI 中不请求 eBay。
- **Clustermelt、Mulligan（转述 eBay 挂牌）**：上游列为仅观察源，不进入官方计算；**AnchorGPU、ServerPartsDeals** 的相关挂牌也不进入官方中位数。以上原始数值均不公开。
- 公开仓库与网页 **只包含按型号汇总** 的 NAV、三法结果、样本数和中位数；**不包含** 任何单条挂牌/租金观测、分来源原始价格、原始快照或数据库（见 `.gitignore`）。
- 其他公开价目页来源（RunPod、Lambda、CoreWeave 等）在系统化采集前仍需逐家确认条款。

## 免责声明

本项目由高中生出于学习研究目的制作。所有数据均为指示性估计，可能不完整或有误，不构成正式指数，不构成任何投资、信贷或估值建议，不对准确性作任何保证。

## 版权（Copyright）

© 2026 ComputeNAV 项目作者。保留所有权利（All rights reserved）。本仓库不附带任何开源许可证，未授予任何复制、修改或再分发的许可；公开可见不代表获得使用许可。

---

### English summary

ComputeNAV is a high-school student research project that publishes a daily **indicative** NAV (USD per card) for used H100 SXM, H100 PCIe, A100 80GB and A100 40GB GPUs, equal-weighting market (dealer listings), income (placeholder DCF of public rental rates) and cost (new-card anchor × placeholder remaining-value factor) approaches. A GitHub Actions workflow runs daily at 12:30 UTC (04:30 PST / 05:30 PDT), publishes only aggregated JSON to `docs/data/`, and deploys the static dashboard to GitHub Pages. Raw observations, snapshots and the database are never committed or published. Vast.ai, eBay, Clustermelt and Mulligan are excluded from the official calculation (methodology §10); CI does not fetch Vast.ai or eBay. Method values with n < 3 (and any NAV/composite that would reveal them) are not published. **Indicative data only. Not investment advice.** © 2026 ComputeNAV authors. All rights reserved; no licence is granted.
