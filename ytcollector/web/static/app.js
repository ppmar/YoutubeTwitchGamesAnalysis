/* Collecteur Procom — the page draws itself from one JSON blob, no chart library. */
(function () {
  "use strict";

  var state = JSON.parse(document.getElementById("state").textContent);

  var NB = " "; // narrow no-break space, the French thousands separator
  var LEADERS = ["fortnite", "call-of-duty-warzone", "apex-legends", "pubg"];
  var SERIES_INK = ["--signal", "--amber", "--ink", "--ink-soft", "--signal-dim"];
  var ROLES = {leader: "leader", precursor: "précurseur", follower: "suiveur", mode: "mode"};
  var MONTHS = ["janv.", "févr.", "mars", "avril", "mai", "juin",
                "juil.", "août", "sept.", "oct.", "nov.", "déc."];

  function n(v) { return (v || 0).toLocaleString("fr-FR").replace(/ /g, NB); }
  function hours(v) {
    if (v >= 1e9) return (v / 1e9).toFixed(2).replace(".", ",") + " Md";
    if (v >= 1e6) return Math.round(v / 1e6) + " M";
    if (v >= 1e3) return Math.round(v / 1e3) + " k";
    return n(v);
  }
  function monthLabel(ym) {
    return MONTHS[parseInt(ym.slice(5, 7), 10) - 1] + " " + ym.slice(0, 4);
  }
  function ink(name) { return "var(" + name + ")"; }
  function $(sel) { return document.querySelector(sel); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function svg(tag, attrs) {
    var e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) if (attrs[k] !== undefined) e.setAttribute(k, attrs[k]);
    return e;
  }

  /* ---- status strip ---------------------------------------------------- */

  function renderStrip() {
    var q = state.quota, p = state.progress;
    var node = $("[data-state-label]");
    node.replaceChildren();
    var working = p.done > 0;
    node.append(
      document.createTextNode(working ? "collecte en cours · " : "en veille · "),
      el("b", null, n(q.search_remaining)),
      document.createTextNode(" appels restants sur " + n(q.searchPerDay) + " aujourd'hui")
    );
  }

  /* ---- the collection calendar ----------------------------------------- */

  function renderCalendar() {
    var p = state.progress;
    var perDay = p.perDay || 95;
    var days = Math.max(1, p.daysTotal || Math.ceil(p.totalSlices / perDay));
    var box = $("[data-calendar]");
    box.replaceChildren();

    var spent = p.done;
    for (var i = 0; i < days; i++) {
      var inDay = Math.max(0, Math.min(perDay, spent - i * perDay));
      var ratio = inDay / perDay;
      var cell = el("div", "cal-day");
      cell.dataset.full = ratio >= 1 ? "yes" : "no";
      cell.dataset.today = (ratio > 0 && ratio < 1) ? "yes" : "no";
      cell.title = "Jour " + (i + 1) + " · " + n(Math.round(inDay)) + " / " + n(perDay) + " appels";
      var fill = el("div", "cal-fill");
      fill.style.height = (ratio * 100).toFixed(1) + "%";
      cell.append(fill, el("span", "cal-n", String(i + 1)));
      box.append(cell);
    }
    box.setAttribute("aria-label",
      days + " journées de quota, " + n(p.done) + " tranches collectées sur " + n(p.totalSlices));
  }

  function renderReadout() {
    var p = state.progress, q = state.quota, c = state.counts;
    $("[data-per-day]").textContent = n(q.searchPerDay);
    $("[data-total-slices]").textContent = n(p.totalSlices) + " tranches";

    var done = $("[data-done]");
    done.replaceChildren(document.createTextNode(n(p.done)),
      el("small", null, " / " + n(p.totalSlices)));

    $("[data-days]").textContent = n(p.daysRemaining);
    $("[data-search-left]").textContent = n(q.search_remaining);

    $("[data-c-games]").textContent = n(c.games);
    $("[data-c-games-note]").textContent = "sur " + n(c.gamesTotal) + " entrées de l'étude";
    $("[data-c-videos]").textContent = n(c.videos);
    $("[data-c-matched]").textContent = c.videos
      ? n(c.matched) + " citent le jeu dans leur titre, tags ou description"
      : "aucune recherche lancée";
    $("[data-c-channels]").textContent = n(c.channels);
    $("[data-c-comments]").textContent = n(c.comments);
    $("[data-roster-on]").textContent = n(c.games);
  }

  function tickReset() {
    var node = $("[data-reset]");
    var target = new Date(state.quota.resetsAt).getTime();
    function paint() {
      var left = Math.max(0, target - Date.now());
      var h = Math.floor(left / 3.6e6), m = Math.floor((left % 3.6e6) / 6e4);
      node.replaceChildren(document.createTextNode(h + " h " + String(m).padStart(2, "0")),
        el("small", null, " heure du Pacifique"));
    }
    paint();
    setInterval(paint, 30000);
  }

  /* ---- Twitch chart ---------------------------------------------------- */

  function twitchSeries() {
    var byMonth = {}, out = [], index = {};
    LEADERS.forEach(function (id) { index[id] = {name: id, points: {}}; });
    index.__other__ = {name: "Reste du genre", points: {}};

    state.series.forEach(function (s) {
      var key = LEADERS.indexOf(s.game_id) >= 0 ? s.game_id : "__other__";
      if (key !== "__other__") index[key].name = s.name;
      s.points.forEach(function (pt) {
        index[key].points[pt[0]] = (index[key].points[pt[0]] || 0) + pt[1];
        byMonth[pt[0]] = true;
      });
    });

    var months = Object.keys(byMonth).sort();
    Object.keys(index).forEach(function (key) {
      var p = index[key].points;
      out.push({
        id: key,
        name: index[key].name,
        points: months.filter(function (m) { return p[m] !== undefined; })
                      .map(function (m) { return {month: m, value: p[m]}; }),
      });
    });
    return {series: out, months: months};
  }

  function renderTwitch() {
    var box = $("[data-chart-twitch]");
    var data = twitchSeries();
    if (!data.months.length) { box.replaceChildren(el("p", "note", "Pas de série Twitch chargée.")); return; }

    var legend = $("[data-legend]");
    legend.replaceChildren.apply(legend, data.series.map(function (s, i) {
      var sp = el("span");
      var sw = el("i");
      sw.style.background = ink(SERIES_INK[i % SERIES_INK.length]);
      sp.append(sw, el("span", null, s.name));
      return sp;
    }));

    var tip = el("div", "tip");
    box.replaceChildren(tip);

    function draw() {
      var old = box.querySelector("svg");
      if (old) old.remove();

      var W = Math.max(280, box.clientWidth);
      var H = W < 560 ? 240 : 340;
      var max = 0;
      data.series.forEach(function (s) {
        s.points.forEach(function (p) { if (p.value > max) max = p.value; });
      });
      var step = Math.pow(10, Math.floor(Math.log10(max)));
      var top = Math.ceil(max / step) * step;

      var m = {t: 12, r: W < 560 ? 8 : 92, b: 26, l: 46};
      var iw = W - m.l - m.r, ih = H - m.t - m.b;
      var xOf = function (i) { return m.l + (data.months.length < 2 ? 0 : i * iw / (data.months.length - 1)); };
      var yOf = function (v) { return m.t + ih - (v / top) * ih; };

      var s = svg("svg", {viewBox: "0 0 " + W + " " + H, role: "img",
        "aria-label": "Heures regardées par mois sur Twitch, quatre leaders et reste du genre"});

      var ax = svg("g", {class: "ax"});
      var ticks = 4;
      for (var t = 0; t <= ticks; t++) {
        var v = top * t / ticks, y = yOf(v);
        ax.append(svg("line", {x1: m.l, x2: W - m.r, y1: y, y2: y,
          class: t === 0 ? "base" : null}));
        var lab = svg("text", {x: m.l - 8, y: y + 4, "text-anchor": "end"});
        lab.textContent = Math.round(v / 1e6) + " M";
        ax.append(lab);
      }
      var every = data.months.length > 60 ? 12 : 6;
      data.months.forEach(function (mo, i) {
        if (mo.slice(5) !== "01" || i % every !== 0) return;
        var tx = svg("text", {x: xOf(i), y: H - 8, "text-anchor": "middle"});
        tx.textContent = mo.slice(0, 4);
        ax.append(tx);
      });
      s.append(ax);

      data.series.forEach(function (ser, si) {
        var d = ser.points.map(function (p) {
          return xOf(data.months.indexOf(p.month)) + "," + yOf(p.value);
        }).join(" L");
        var path = svg("path", {d: "M" + d, fill: "none", "stroke-width": 2,
          "stroke-linejoin": "round", "stroke-linecap": "round"});
        path.style.stroke = ink(SERIES_INK[si % SERIES_INK.length]);
        s.append(path);

        if (W >= 560 && ser.points.length) {
          var last = ser.points[ser.points.length - 1];
          var tx = svg("text", {x: xOf(data.months.indexOf(last.month)) + 7,
            y: yOf(last.value) + 4, "font-size": 11});
          tx.style.fill = ink("--ink-soft");
          tx.textContent = ser.name.length > 13 ? ser.name.slice(0, 12) + "…" : ser.name;
          s.append(tx);
        }
      });

      var rule = svg("line", {y1: m.t, y2: m.t + ih, "stroke-width": 1, opacity: 0});
      rule.style.stroke = ink("--ink-faint");
      s.append(rule);

      var hit = svg("rect", {x: m.l, y: m.t, width: Math.max(0, iw), height: Math.max(0, ih),
        fill: "transparent"});
      hit.addEventListener("pointerleave", function () {
        tip.style.opacity = 0; rule.setAttribute("opacity", 0);
      });
      hit.addEventListener("pointermove", function (ev) {
        var r = s.getBoundingClientRect();
        var px = (ev.clientX - r.left) * (W / r.width);
        var i = Math.round((px - m.l) / (iw / Math.max(1, data.months.length - 1)));
        i = Math.max(0, Math.min(data.months.length - 1, i));
        var month = data.months[i], mx = xOf(i);
        rule.setAttribute("x1", mx); rule.setAttribute("x2", mx); rule.setAttribute("opacity", 1);

        var rows = [el("div", "tip-h", monthLabel(month))];
        data.series.forEach(function (ser, si) {
          var pt = null;
          for (var k = 0; k < ser.points.length; k++) {
            if (ser.points[k].month === month) { pt = ser.points[k]; break; }
          }
          var row = el("div", "tip-r");
          var sw = el("i");
          sw.style.background = ink(SERIES_INK[si % SERIES_INK.length]);
          row.append(sw, el("span", null, ser.name),
            el("b", null, pt ? hours(pt.value) + " h" : "hors top 200"));
          rows.push(row);
        });
        tip.replaceChildren.apply(tip, rows);
        tip.style.opacity = 1;
        var w = tip.offsetWidth;
        tip.style.left = Math.min(Math.max(0, mx * (box.clientWidth / W) - w / 2),
                                  box.clientWidth - w) + "px";
        tip.style.top = "0px";
      });
      s.append(hit);
      box.append(s);
    }

    draw();
    var pending;
    window.addEventListener("resize", function () {
      clearTimeout(pending);
      pending = setTimeout(draw, 140);
    });
  }

  /* ---- YouTube against Twitch, on one indexed scale --------------------- */

  function indexTo100(points) {
    var max = 0, i;
    for (i = 0; i < points.length; i++) if (points[i].value > max) max = points[i].value;
    if (!max) return points.map(function (p) { return {month: p.month, value: 0}; });
    return points.map(function (p) { return {month: p.month, value: 100 * p.value / max}; });
  }

  function peakMonth(points) {
    var best = null, i;
    for (i = 0; i < points.length; i++) {
      if (!best || points[i].value > best.value) best = points[i];
    }
    return best ? best.month : null;
  }

  function monthsApart(a, b) {
    return (parseInt(b.slice(0, 4), 10) * 12 + parseInt(b.slice(5, 7), 10)) -
           (parseInt(a.slice(0, 4), 10) * 12 + parseInt(a.slice(5, 7), 10));
  }

  function renderCompare() {
    if (!state.attention.length) return;
    document.querySelector("[data-compare-empty]").hidden = true;
    var box = $("[data-compare]");
    box.hidden = false;

    var twitchBy = {};
    state.series.forEach(function (s) {
      twitchBy[s.game_id] = s.points.map(function (pt) { return {month: pt[0], value: pt[1]}; });
    });
    var names = {};
    state.games.forEach(function (g) { names[g.id] = g.name; });

    var ytBy = {};
    state.attention.forEach(function (r) {
      var month = r.year + "-" + String(r.month).padStart(2, "0");
      (ytBy[r.game_id] = ytBy[r.game_id] || []).push({month: month, value: r.index || 0});
    });

    var ids = Object.keys(ytBy).sort(function (a, b) {
      return ytBy[b].length - ytBy[a].length;
    });

    box.replaceChildren.apply(box, ids.map(function (id) {
      var yt = ytBy[id].slice().sort(function (a, b) { return a.month < b.month ? -1 : 1; });
      var tw = indexTo100(twitchBy[id] || []);
      var card = el("figure", "plot");
      card.style.margin = "0";

      var head = el("div", "cmp-head");
      head.append(el("span", "cmp-name", names[id] || id));

      var ytPeak = peakMonth(yt), twPeak = peakMonth(tw);
      if (ytPeak && twPeak) {
        var gap = monthsApart(ytPeak, twPeak);
        var chip = el("span", "cmp-gap");
        if (gap > 0) {
          chip.dataset.lead = "youtube";
          chip.textContent = "YouTube en avance de " + gap + " mois";
        } else if (gap < 0) {
          chip.dataset.lead = "twitch";
          chip.textContent = "Twitch en avance de " + (-gap) + " mois";
        } else {
          chip.textContent = "pics simultanés";
        }
        head.append(chip);
      }
      card.append(head);

      var sub = el("p", "cmp-sub");
      sub.textContent = ytPeak && twPeak
        ? "pic YouTube " + monthLabel(ytPeak) + " · pic Twitch " + monthLabel(twPeak)
        : "pics indéterminés";
      card.append(sub);

      // One month axis for both signals, so the offset between them is readable.
      var monthSet = {};
      yt.concat(tw).forEach(function (p) { monthSet[p.month] = true; });
      var months = Object.keys(monthSet).sort();

      var W = 420, H = 130, m = {t: 8, r: 6, b: 20, l: 6};
      var iw = W - m.l - m.r, ih = H - m.t - m.b;
      var xOf = function (mo) {
        var i = months.indexOf(mo);
        return m.l + (months.length < 2 ? 0 : i * iw / (months.length - 1));
      };
      var yOf = function (v) { return m.t + ih - (v / 100) * ih; };

      var s = svg("svg", {viewBox: "0 0 " + W + " " + H, role: "img",
        "aria-label": (names[id] || id) +
          " : attention YouTube et audience Twitch, chacune ramenée sur 0 à 100"});

      var ax = svg("g", {class: "ax"});
      ax.append(svg("line", {x1: m.l, x2: W - m.r, y1: m.t + ih, y2: m.t + ih, class: "base"}));
      months.forEach(function (mo, i) {
        if (mo.slice(5) !== "01") return;
        if (months.length > 40 && i % 24 !== 0) return;
        var tx = svg("text", {x: xOf(mo), y: H - 6, "text-anchor": "middle"});
        tx.textContent = mo.slice(0, 4);
        ax.append(tx);
      });
      s.append(ax);

      function trace(points, colour, dashed) {
        if (!points.length) return;
        var d = points.map(function (p) { return xOf(p.month) + "," + yOf(p.value); }).join(" L");
        var path = svg("path", {d: "M" + d, fill: "none", "stroke-width": 2,
          "stroke-linejoin": "round", "stroke-linecap": "round",
          "stroke-dasharray": dashed ? "3 3" : undefined});
        path.style.stroke = ink(colour);
        s.append(path);
      }
      trace(tw, "--amber", true);
      trace(yt, "--signal", false);

      [[twPeak, "--amber"], [ytPeak, "--signal"]].forEach(function (pair) {
        if (!pair[0]) return;
        var dot = svg("circle", {cx: xOf(pair[0]), cy: yOf(100), r: 3.2});
        dot.style.fill = ink(pair[1]);
        s.append(dot);
      });

      card.append(s);

      var key = el("div", "cmp-key");
      [["--signal", "attention YouTube"], ["--amber", "heures Twitch"]].forEach(function (k) {
        var sp = el("span");
        var sw = el("i");
        sw.style.background = ink(k[0]);
        sp.append(sw, el("span", null, k[1]));
        key.append(sp);
      });
      card.append(key);
      return card;
    }));
  }

  /* ---- roster ---------------------------------------------------------- */

  function renderRoster() {
    var body = $("[data-roster]");
    var maxHours = 0;
    state.games.forEach(function (g) { if ((g.hours || 0) > maxHours) maxHours = g.hours || 0; });

    body.replaceChildren.apply(body, state.games.map(function (g) {
      var tr = el("tr");
      tr.dataset.off = g.enabled ? "no" : "yes";

      var name = el("td");
      name.append(el("span", "name", g.name));
      if (!g.enabled) name.append(el("span", "off", "mode seul"));
      tr.append(name);

      tr.append(el("td", null, ROLES[g.role] || g.role));
      tr.append(el("td", "num", n(g.months)));
      tr.append(el("td", null, g.first && g.last ? g.first + " → " + g.last : "—"));
      tr.append(el("td", null, g.peak || "—"));
      tr.append(el("td", "num", g.rank ? "#" + g.rank : "—"));

      var hoursCell = el("td", "num");
      hoursCell.append(document.createTextNode(hours(g.hours || 0) + " h"));
      var bar = el("span", "bar");
      bar.style.width = Math.max(1, Math.round(44 * (g.hours || 0) / (maxHours || 1))) + "px";
      hoursCell.append(bar);
      tr.append(hoursCell);

      tr.append(el("td", "num", n(g.slices)));
      return tr;
    }));
  }

  function renderFooter() {
    var d = new Date(state.generatedAt);
    $("[data-generated]").textContent = "le " + d.toLocaleDateString("fr-FR") +
      " à " + d.toLocaleTimeString("fr-FR", {hour: "2-digit", minute: "2-digit"});
  }

  renderStrip();
  renderCalendar();
  renderReadout();
  tickReset();
  renderTwitch();
  renderCompare();
  renderRoster();
  renderFooter();
})();
