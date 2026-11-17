/* ComputeNAV dashboard: renders docs/data/latest.json + history.json.
 * No numbers are hard-coded here; everything comes from the JSON.
 * null / undefined / NaN are always shown as "—", never as 0. */
(function () {
  "use strict";
  var DASH = "\u2014";
  var TZ = "America/Los_Angeles";
  var MODEL_KEYS = ["H100_SXM", "H100_PCIe", "A100_80GB", "A100_40GB"];
  var COLORS = { COMPOSITE: "#1d2330", H100_SXM: "#2f5bd3", H100_PCIe: "#0f9d8a", A100_80GB: "#d97706", A100_40GB: "#b42318" };

  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function usd(v, d) {
    if (!isNum(v)) return DASH;
    d = d == null ? 2 : d;
    return "$" + v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function int(v) { return isNum(v) ? String(v) : DASH; }
  function pct(v) { return isNum(v) ? (Math.round(v * 1000) / 10) + "%" : DASH; }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtPT(s) {
    if (!s) return DASH;
    var d = new Date(s);
    if (isNaN(d.getTime())) return esc(s);
    var p = new Intl.DateTimeFormat("en-CA", {
      timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZoneName: "short"
    }).formatToParts(d).reduce(function (a, x) { a[x.type] = x.value; return a; }, {});
    var hh = p.hour === "24" ? "00" : p.hour;
    return p.year + "-" + p.month + "-" + p.day + " " + hh + ":" + p.minute + ":" + p.second + " " + (p.timeZoneName || "PT") + " (America/Los_Angeles)";
  }
  function $(id) { return document.getElementById(id); }

  function getJSON(url) {
    return fetch(url + "?v=" + Date.now(), { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(url + " HTTP " + r.status);
      return r.json();
    });
  }

  function renderProps(L) {
    var h = L.source_health || [];
    var ok = h.filter(function (s) { return s.ok; }).length;
    var skipped = h.filter(function (s) { return s.skipped; }).length;
    var failed = h.length - ok - skipped;
    var fx = L.fx || {};
    var items = [
      ["最近更新（PT） / Last updated (PT)", fmtPT(L.computed_at_pt)],
      ["指数日（PT） / As-of date (PT)", esc(L.asof_date_pt || DASH)],
      ["运行编号 / Run ID", esc(L.run_id || DASH)],
      ["今日成交样本 / Sold transactions today", int(L.sold_count_today)],
      ["汇率 / FX", "1 EUR = " + usd(fx.usd_per_eur, 4) + "; 1 GBP = " + usd(fx.usd_per_gbp, 4) + "<br><span class='en'>as of " + fmtPT(fx.asof_utc) + "</span>"],
      ["源健康 / Sources", ok + " 成功 ok / " + failed + " 失败 failed / " + skipped + " 跳过 skipped"],
      ["当日观测 / Observations (not published)", int(L.observation_count) + (L.official_observation_counts ?
        "<br><span class='en'>官方口径 official: 市场 market " + int(L.official_observation_counts.market) + " · 收益 income " + int(L.official_observation_counts.income) + " · 成本 cost " + int(L.official_observation_counts.cost_new) + "</span>" : "")]
    ];
    $("props").innerHTML = items.map(function (it) {
      return "<div><dt>" + it[0] + "</dt><dd>" + it[1] + "</dd></div>";
    }).join("");
  }

  function renderMetrics(L) {
    var c = L.composite || {};
    var cHidden = !!c.nav_hidden;
    var html = "<div class='metric primary" + (cHidden ? " warn" : "") + "'><div class='v'>" + (cHidden ? "已隐藏 <small>hidden</small>" : usd(c.nav_indicative_usd)) +
      "</div><div class='l'>" + esc(c.label || "综合") +
      "</div><div class='d'>" + (cHidden && c.nav_hidden_reason ? esc(c.nav_hidden_reason.zh) + "<br><span class='en'>" + esc(c.nav_hidden_reason.en) + "</span><br>" : "") +
      "四只子指数等权平均（" + int(c.n_models) + " 个型号有值），装机量权数待校准<br><span class='en'>" + esc(c.label_en || "") + "</span></div></div>";
    MODEL_KEYS.forEach(function (k) {
      var m = (L.models || {})[k];
      if (!m) { html += "<div class='metric warn'><div class='v'>" + DASH + "</div><div class='l'>" + k + "</div><div class='d'>当日无数据 / no data</div></div>"; return; }
      var ins = m.insufficient_sample || {};
      var legNames = [], small = [];
      [["market", "市场 market"], ["income", "收益 income"], ["cost", "成本 cost"]].forEach(function (lg) {
        var n = m[lg[0] + "_n"];
        if (isNum(n) && n > 0) legNames.push(lg[1]);
        if (ins[lg[0]]) small.push(lg[1] + " n=" + n);
      });
      var partial = legNames.length < 3;
      var detail = partial ? ("仅 " + (legNames.join(" + ") || DASH) + "，不可直接横向比较 · only " + legNames.length + " of 3 methods")
                           : "市场/收益/成本三法等权 · 3 methods equal-weight";
      if (small.length) detail += "<br>样本不足未公开 / not published (n&lt;3): " + esc(small.join(", "));
      var hidden = !!m.nav_hidden;
      var val = hidden ? "已隐藏 <small>hidden</small>" : usd(m.nav_indicative_usd);
      if (hidden && m.nav_hidden_reason) detail = esc(m.nav_hidden_reason.zh) + "<br><span class='en'>" + esc(m.nav_hidden_reason.en) + "</span><br>" + detail;
      html += "<div class='metric" + (partial || hidden ? " warn" : "") + "'><div class='v'>" + val + "</div><div class='l'>" +
        esc(m.label) + "</div><div class='d'>" + detail + "</div></div>";
    });
    $("metrics").innerHTML = html;
  }

  function qualityText(q) {
    if (!q) return DASH;
    var map = {
      "sold_n=0": "成交 n=0",
      "no_official_listings": "无官方挂牌",
      "no_official_rentals": "无官方租金",
      "no_new_card_anchor": "无新卡锚点",
      "income_assumptions=to-be-calibrated": "收益法参数待校准",
      "cost_age_fraction=to-be-calibrated": "成本法残值系数待校准"
    };
    return q.split(";").map(function (t) { return map[t] || t; }).join("；");
  }

  function renderBreakdown(L) {
    var head = ["型号<br>Model", "指示性 NAV<br>Indicative", "市场法<br>Market", "n", "收益法<br>Income", "租金中位数<br>Rent median /GPU-hr", "n", "成本法<br>Cost", "新卡锚点<br>New-card anchor", "n", "质量标记<br>Quality flags"];
    var num = [false, true, true, true, true, true, true, true, true, true, false];
    var rows = MODEL_KEYS.map(function (k) {
      var m = (L.models || {})[k] || {};
      var ins = m.insufficient_sample || {};
      function small(leg) { return "<span class='skip'>样本不足 (n=" + int(m[leg + "_n"]) + ")</span>"; }
      function leg(name, v, d) { return ins[name] ? small(name) : usd(v, d); }
      return [esc(m.label || k), m.nav_hidden ? "<span class='skip'>已隐藏 hidden</span>" : usd(m.nav_indicative_usd),
        leg("market", m.market_usd), int(m.market_n), leg("income", m.income_usd),
        leg("income", m.rental_median_usd_per_hour, 4), int(m.income_n),
        leg("cost", m.cost_usd), leg("cost", m.new_card_usd), int(m.cost_n), esc(qualityText(m.quality_flag))];
    });
    $("breakdown").innerHTML = "<thead><tr>" + head.map(function (h, i) { return "<th" + (num[i] ? " class='num'" : "") + ">" + h + "</th>"; }).join("") +
      "</tr></thead><tbody>" + rows.map(function (r) {
        return "<tr>" + r.map(function (c, i) { return "<td" + (num[i] ? " class='num'" : "") + ">" + c + "</td>"; }).join("") + "</tr>";
      }).join("") + "</tbody>";
  }

  function renderSmallSampleNote(L) {
    var r = L.small_sample_rule;
    if (!r) return;
    $("small-sample").innerHTML = "样本量规则 / small-sample rule (n&lt;" + int(r.min_n) + "): " + esc(r.zh) + " <span class='en'>" + esc(r.en) + "</span>";
  }

  function renderChart(H) {
    var dates = (H && H.dates) || [];
    var keys = ["COMPOSITE"].concat(MODEL_KEYS);
    var series = keys.map(function (k) {
      var s = (H.series || {})[k] || { points: [] };
      var byDate = {};
      (s.points || []).forEach(function (p) { byDate[p.asof_date_pt] = p.nav_indicative_usd; });
      return { key: k, label: s.label || k, values: dates.map(function (d) { return isNum(byDate[d]) ? byDate[d] : null; }) };
    });
    var all = [];
    series.forEach(function (s) { s.values.forEach(function (v) { if (isNum(v)) all.push(v); }); });
    if (!dates.length || !all.length) { $("chart").innerHTML = "<p class='note'>暂无历史数据 / no history yet</p>"; return; }
    var W = 1100, Hh = 340, pl = 70, pr = 20, pt = 16, pb = 40;
    var max = Math.ceil(Math.max.apply(null, all) / 5000) * 5000 || 5000;
    var min = 0;
    function x(i) { return dates.length === 1 ? pl + (W - pl - pr) / 2 : pl + i * (W - pl - pr) / (dates.length - 1); }
    function y(v) { return pt + (Hh - pt - pb) * (1 - (v - min) / (max - min)); }
    var svg = "<svg viewBox='0 0 " + W + " " + Hh + "' role='img' aria-label='NAV history chart'>";
    for (var g = 0; g <= 5; g++) {
      var gv = min + (max - min) * g / 5;
      svg += "<line x1='" + pl + "' x2='" + (W - pr) + "' y1='" + y(gv) + "' y2='" + y(gv) + "' stroke='#e3e6ec'/>" +
        "<text x='" + (pl - 8) + "' y='" + (y(gv) + 4) + "' font-size='12' text-anchor='end' fill='#5d6676'>$" + Math.round(gv).toLocaleString("en-US") + "</text>";
    }
    var step = Math.max(1, Math.ceil(dates.length / 10));
    dates.forEach(function (d, i) {
      if (i % step === 0 || i === dates.length - 1)
        svg += "<text x='" + x(i) + "' y='" + (Hh - 14) + "' font-size='12' text-anchor='middle' fill='#5d6676'>" + esc(d) + "</text>";
    });
    series.forEach(function (s) {
      var col = COLORS[s.key] || "#888", seg = [], paths = [];
      s.values.forEach(function (v, i) {
        if (isNum(v)) seg.push(x(i) + "," + y(v)); else if (seg.length) { paths.push(seg); seg = []; }
      });
      if (seg.length) paths.push(seg);
      paths.forEach(function (p) {
        if (p.length > 1) svg += "<polyline fill='none' stroke='" + col + "' stroke-width='" + (s.key === "COMPOSITE" ? 3 : 2) + "' points='" + p.join(" ") + "'/>";
      });
      s.values.forEach(function (v, i) {
        if (isNum(v)) svg += "<circle cx='" + x(i) + "' cy='" + y(v) + "' r='4' fill='" + col + "'><title>" + esc(s.label) + " " + esc(dates[i]) + ": " + usd(v) + "</title></circle>";
      });
    });
    svg += "</svg>";
    $("chart").innerHTML = svg;
    $("legend").innerHTML = series.map(function (s) {
      var last = null;
      for (var i = s.values.length - 1; i >= 0; i--) if (isNum(s.values[i])) { last = s.values[i]; break; }
      return "<span style='--c:" + (COLORS[s.key] || "#888") + "'>" + esc(s.label) + "（最新 " + usd(last) + "）</span>";
    }).join("") + "<span class='note' style='--c:transparent'>" + dates.length + " 个交易日 / days · 缺值或已隐藏的点不插补 / missing or hidden points are not interpolated</span>";
  }

  function renderAssumptions(L) {
    var a = L.assumptions || {}, inc = a.income || {}, cost = a.cost || {}, mkt = a.market || {}, comp = a.composite || {};
    $("assumptions").innerHTML =
      "<p><strong>" + esc(a.disclaimer_zh || "以下参数均为占位假设，未经校准。") + "</strong> <span class='en'>All parameters below are placeholders (status: " + esc(a.status || DASH) + ").</span></p><ul>" +
      "<li>收益法 DCF / Income: 剩余寿命 remaining life " + int(inc.remaining_life_years) + " 年 yrs · 利用率 utilization " + pct(inc.utilization) +
      " · 运营成本 opex " + pct(inc.opex_ratio) + " of gross rent · 年折现率 discount " + pct(inc.discount_rate_annual) +
      " · 期末残值 terminal " + (isNum(inc.terminal_value) ? String(inc.terminal_value) : DASH) + " · " + int(inc.hours_per_year) + " h/yr</li>" +
      "<li>成本法 / Cost: 新卡锚点 × 剩余价值系数 " + (isNum(cost.unknown_age_remaining_fraction) ? cost.unknown_age_remaining_fraction : DASH) +
      "（情景锚点寿命 " + int(cost.book_life_years_anchor) + " 年）。" + esc(cost.new_price_source || "") + "</li>" +
      "<li>市场法 / Market: " + esc(mkt.listing_rule || DASH) + "</li>" +
      "<li>综合 / Composite: " + esc(comp.rule || DASH) + "</li></ul>";
  }

  function renderHealth(L) {
    var h = L.source_health || [];
    var ok = h.filter(function (s) { return s.ok; }).length;
    var sk = h.filter(function (s) { return s.skipped; }).length;
    $("health-caption").innerHTML = "本次运行 / this run: " + ok + " 成功 ok, " + (h.length - ok - sk) + " 失败 failed, " + sk +
      " 跳过（许可未定）skipped (licence pending). 单源失败不影响其余源；未绕过任何访问限制。 <span class='en'>No access block was bypassed.</span>";
    var roleMap = { licence_restricted: "许可受限 licence-restricted", cross_check_only: "仅交叉核对 cross-check only (weight 0)", collected: "采集 collected" };
    $("health").innerHTML = "<thead><tr><th>源 Source</th><th>状态 Status</th><th class='num'>HTTP</th><th class='num'>观测 Obs</th><th>角色 Role</th><th>说明 Note</th></tr></thead><tbody>" +
      h.map(function (s) {
        var st = s.skipped ? "<span class='skip'>跳过 skipped</span>" : s.ok ? "<span class='ok'>成功 ok</span>" : "<span class='bad'>失败 failed</span>";
        return "<tr><td>" + esc(s.source_id) + "</td><td>" + st + "</td><td class='num'>" + int(s.http_status) + "</td><td class='num'>" + int(s.observation_count) +
          "</td><td>" + esc(roleMap[s.role] || s.role || DASH) + (s.observation_only ? "（仅观察，不进入官方计算 / observation only）" : "") + "</td><td>" + esc(s.error || DASH) + "</td></tr>";
      }).join("") + "</tbody>";
  }

  function renderLicensing(L) {
    var lic = L.licensing || {};
    var ex = (lic.excluded_from_index || []).map(function (e) { return "<li><code>" + esc(e.source_id) + "</code>: " + esc(e.reason) + "</li>"; }).join("");
    var cc = (lic.cross_check_only_not_published || []).map(function (e) { return "<li><code>" + esc(e.source_id) + "</code>: " + esc(e.reason) + "</li>"; }).join("");
    $("licensing").innerHTML = "<p>" + esc(lic.note_zh || "") + "</p><p class='en'>" + esc(lic.note_en || "") + "</p>" +
      (ex ? "<p><strong>不采集、不进入计算 / Excluded until licensed:</strong></p><ul>" + ex + "</ul>" : "") +
      (cc ? "<p><strong>仅交叉核对、原始值不公开 / Cross-check only, raw values not published:</strong></p><ul>" + cc + "</ul>" : "");
  }

  Promise.all([getJSON("data/latest.json"), getJSON("data/history.json")]).then(function (r) {
    var L = r[0], H = r[1];
    renderProps(L); renderMetrics(L); renderBreakdown(L); renderSmallSampleNote(L); renderChart(H);
    renderAssumptions(L); renderHealth(L); renderLicensing(L);
    document.body.setAttribute("data-rendered", "1");
  }).catch(function (e) {
    var el = $("error"); el.hidden = false;
    el.textContent = "数据加载失败 / Failed to load data: " + e.message;
  });
})();
