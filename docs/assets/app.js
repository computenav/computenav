/* ComputeNAV dashboard: renders docs/data/latest.json + history.json.
 * No numbers are hard-coded here; everything comes from the JSON.
 * null / undefined / NaN are always shown as "—", never as 0. */
(function () {
  "use strict";
  var DASH = "\u2014";
  var TZ = "America/Los_Angeles";
  var MODEL_KEYS = ["H100_SXM", "H100_PCIe", "A100_80GB", "A100_40GB"];
  var LEG_NAMES = { market: "market", income: "income", cost: "cost" };
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
    return p.year + "-" + p.month + "-" + p.day + " " + hh + ":" + p.minute + ":" + p.second + " " + (p.timeZoneName || "PT") + " (Silicon Valley time)";
  }
  function $(id) { return document.getElementById(id); }
  function hiddenHTML() { return "Hidden"; }
  function smallHTML(n) { return "<span class='skip'>Insufficient sample (n=" + int(n) + ")</span>"; }

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
    var oc = L.official_observation_counts;
    var items = [
      ["Last updated (PT)", fmtPT(L.computed_at_pt)],
      ["As-of date (PT)", esc(L.asof_date_pt || DASH)],
      ["Run ID", esc(L.run_id || DASH)],
      ["Sold transactions today", int(L.sold_count_today)],
      ["Currency", "All values in USD<br><span class='en'>non-USD sources converted at the daily FX rate, as of " + fmtPT(fx.asof_utc) + "</span>"],
      ["Sources", ok + " ok / " + failed + " failed / " + skipped + " skipped"],
      ["Observations (not published)", int(L.observation_count) + (oc ?
        "<br><span class='en'>used officially: market " + int(oc.market) + " · income " + int(oc.income) + " · cost " + int(oc.cost_new) + "</span>" : "")]
    ];
    $("props").innerHTML = items.map(function (it) {
      return "<div><dt>" + it[0] + "</dt><dd>" + it[1] + "</dd></div>";
    }).join("");
  }

  function renderMetrics(L) {
    var c = L.composite || {};
    var cHidden = !!c.nav_hidden;
    var html = "<div class='metric primary" + (cHidden ? " warn" : "") + "'><div class='v'>" + (cHidden ? hiddenHTML() : usd(c.nav_indicative_usd)) +
      "</div><div class='l'>" + esc(c.label || "Composite") + "</div><div class='d'>" +
      (cHidden && c.nav_hidden_reason ? esc(c.nav_hidden_reason) + "<br>" : "") +
      "Equal-weight mean of the sub-indices (" + int(c.n_models) + " models with a value); installed-base weights to be calibrated</div></div>";
    MODEL_KEYS.forEach(function (k) {
      var m = (L.models || {})[k];
      if (!m) { html += "<div class='metric warn'><div class='v'>" + DASH + "</div><div class='l'>" + k + "</div><div class='d'>No data</div></div>"; return; }
      var ins = m.insufficient_sample || {};
      var legNames = [], small = [];
      ["market", "income", "cost"].forEach(function (lg) {
        var n = m[lg + "_n"];
        if (isNum(n) && n > 0) legNames.push(LEG_NAMES[lg]);
        if (ins[lg]) small.push(LEG_NAMES[lg] + " n=" + n);
      });
      var partial = legNames.length < 3;
      var detail = partial ? ("Only " + (legNames.join(" + ") || DASH) + " (" + legNames.length + " of 3 methods); not directly comparable")
                           : "Market / income / cost, equal-weight";
      if (small.length) detail += "<br>Not published (n&lt;3): " + esc(small.join(", "));
      var hidden = !!m.nav_hidden;
      if (hidden && m.nav_hidden_reason) detail = esc(m.nav_hidden_reason) + "<br>" + detail;
      html += "<div class='metric" + (partial || hidden ? " warn" : "") + "'><div class='v'>" + (hidden ? hiddenHTML() : usd(m.nav_indicative_usd)) +
        "</div><div class='l'>" + esc(m.label) + "</div><div class='d'>" + detail + "</div></div>";
    });
    $("metrics").innerHTML = html;
  }

  function qualityText(q) {
    if (!q) return DASH;
    var map = {
      "sold_n=0": "no sold transactions",
      "no_official_listings": "no official listings",
      "no_official_rentals": "no official rents",
      "no_new_card_anchor": "no new-card anchor",
      "income_assumptions=to-be-calibrated": "income parameters to be calibrated",
      "cost_age_fraction=to-be-calibrated": "cost remaining-value factor to be calibrated"
    };
    return q.split(";").map(function (t) { return map[t] || t; }).join("; ");
  }

  function renderBreakdown(L) {
    var head = ["Model", "Indicative NAV", "Market", "n", "Income", "Rent median /GPU-hr", "n", "Cost", "New-card anchor", "n", "Quality flags"];
    var num = [false, true, true, true, true, true, true, true, true, true, false];
    var rows = MODEL_KEYS.map(function (k) {
      var m = (L.models || {})[k] || {};
      var ins = m.insufficient_sample || {};
      function leg(name, v, d) { return ins[name] ? smallHTML(m[name + "_n"]) : usd(v, d); }
      return [esc(m.label || k), m.nav_hidden ? "<span class='skip'>Hidden</span>" : usd(m.nav_indicative_usd),
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
    $("small-sample").innerHTML = "Small-sample rule (n&lt;" + int(r.min_n) + "): " + esc(r.text);
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
    if (!dates.length || !all.length) { $("chart").innerHTML = "<p class='note'>No history yet</p>"; return; }
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
      return "<span style='--c:" + (COLORS[s.key] || "#888") + "'>" + esc(s.label) + " (latest " + usd(last) + ")</span>";
    }).join("") + "<span class='note' style='--c:transparent'>" + dates.length + " day(s) · missing or hidden points are not interpolated</span>";
  }

  function renderAssumptions(L) {
    var a = L.assumptions || {}, inc = a.income || {}, cost = a.cost || {}, mkt = a.market || {}, comp = a.composite || {};
    $("assumptions").innerHTML =
      "<p><strong>" + esc(a.disclaimer || "All parameters below are uncalibrated placeholders.") + "</strong> <span class='en'>(status: " + esc(a.status || DASH) + ")</span></p><ul>" +
      "<li>Income approach (DCF): remaining life " + int(inc.remaining_life_years) + " yrs · utilization " + pct(inc.utilization) +
      " · opex " + pct(inc.opex_ratio) + " of gross rent · discount rate " + pct(inc.discount_rate_annual) +
      " · terminal value " + (isNum(inc.terminal_value) ? String(inc.terminal_value) : DASH) + " · " + int(inc.hours_per_year) + " h/yr</li>" +
      "<li>Cost approach: new-card anchor × remaining-value factor " + (isNum(cost.unknown_age_remaining_fraction) ? cost.unknown_age_remaining_fraction : DASH) +
      " (scenario anchor life " + int(cost.book_life_years_anchor) + " yrs). " + esc(cost.new_price_source || "") + "</li>" +
      "<li>Market approach: " + esc(mkt.listing_rule || DASH) + "</li>" +
      "<li>Composite: " + esc(comp.rule || DASH) + "</li></ul>";
  }

  function renderHealth(L) {
    var h = L.source_health || [];
    var ok = h.filter(function (s) { return s.ok; }).length;
    var sk = h.filter(function (s) { return s.skipped; }).length;
    $("health-caption").innerHTML = "This run: " + ok + " ok, " + (h.length - ok - sk) + " failed, " + sk +
      " skipped (licence pending). A failing source does not stop the others. No access block was bypassed.";
    var roleMap = { licence_restricted: "licence-restricted", cross_check_only: "cross-check only (weight 0)", collected: "collected" };
    $("health").innerHTML = "<thead><tr><th>Source</th><th>Status</th><th class='num'>HTTP</th><th class='num'>Obs</th><th>Role</th><th>Note</th></tr></thead><tbody>" +
      h.map(function (s) {
        var st = s.skipped ? "<span class='skip'>skipped</span>" : s.ok ? "<span class='ok'>ok</span>" : "<span class='bad'>failed</span>";
        return "<tr><td>" + esc(s.source_id) + "</td><td>" + st + "</td><td class='num'>" + int(s.http_status) + "</td><td class='num'>" + int(s.observation_count) +
          "</td><td>" + esc(roleMap[s.role] || s.role || DASH) + (s.observation_only ? " (observation only; not used in the official calculation)" : "") +
          "</td><td>" + esc(s.error || DASH) + "</td></tr>";
      }).join("") + "</tbody>";
  }

  function renderLicensing(L) {
    var lic = L.licensing || {};
    var ex = (lic.excluded_from_index || []).map(function (e) { return "<li><code>" + esc(e.source_id) + "</code>: " + esc(e.reason) + "</li>"; }).join("");
    var cc = (lic.cross_check_only_not_published || []).map(function (e) { return "<li><code>" + esc(e.source_id) + "</code>: " + esc(e.reason) + "</li>"; }).join("");
    $("licensing").innerHTML = "<p>" + esc(lic.note || "") + "</p>" +
      (ex ? "<p><strong>Excluded until licensed:</strong></p><ul>" + ex + "</ul>" : "") +
      (cc ? "<p><strong>Cross-check only; raw values not published:</strong></p><ul>" + cc + "</ul>" : "");
  }

  Promise.all([getJSON("data/latest.json"), getJSON("data/history.json")]).then(function (r) {
    var L = r[0], H = r[1];
    renderProps(L); renderMetrics(L); renderBreakdown(L); renderSmallSampleNote(L); renderChart(H);
    renderAssumptions(L); renderHealth(L); renderLicensing(L);
    document.body.setAttribute("data-rendered", "1");
  }).catch(function (e) {
    var el = $("error"); el.hidden = false;
    el.textContent = "Failed to load data: " + e.message;
  });
})();
