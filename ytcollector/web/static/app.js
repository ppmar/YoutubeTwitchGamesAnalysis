/* Collecteur Procom — the page draws itself from one JSON blob, no chart library. */
(function () {
  "use strict";

  var state = JSON.parse(document.getElementById("state").textContent);

  var NB = " "; // narrow no-break space, the French thousands separator
  var LEADERS = ["fortnite", "call-of-duty-warzone", "apex-legends", "pubg"];
  // Four validated hues for the leaders; the genre aggregate is the neutral,
  // drawn dashed so it never has to be told apart by colour alone.
  var SERIES_INK = ["--s1", "--s2", "--s3", "--s4", "--s-other"];
  var SERIES_DASH = [null, null, null, null, "5 4"];
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

  /* ---- chart registry --------------------------------------------------- */

  /* A chart measures its box to size itself, and a box inside a hidden tab
     measures zero. So drawing is deferred: every chart registers its draw
     function, and it runs when the box has a width — on load, on resize, and
     each time its tab is shown. */
  var charts = [];

  function register(box, draw) {
    var entry = {box: box, draw: draw, drawn: false};
    charts.push(entry);
    paint(entry);
    return entry;
  }

  function paint(entry) {
    if (!entry.box.clientWidth) return;
    entry.draw();
    entry.drawn = true;
  }

  function repaintAll() { charts.forEach(paint); }

  var resizePending;
  window.addEventListener("resize", function () {
    clearTimeout(resizePending);
    resizePending = setTimeout(repaintAll, 150);
  });

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
    $("[data-c-withc]").textContent = n(c.videosWithComments);
    $("[data-c-coverage]").textContent = c.eligibleForComments
      ? "sur " + n(c.eligibleForComments) + " éligibles ("
        + (100 * c.videosWithComments / c.eligibleForComments).toFixed(1).replace(".", ",")
        + " %) — c'est cette part qui décide de la fiabilité du score"
      : "aucune vidéo éligible pour l'instant";
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
    var byMonth = {}, index = {};
    LEADERS.forEach(function (id) { index[id] = {name: id, points: {}}; });

    state.series.forEach(function (s) {
      if (LEADERS.indexOf(s.game_id) < 0) return;
      index[s.game_id].name = s.name;
      s.points.forEach(function (pt) {
        index[s.game_id].points[pt[0]] = pt[1];
        byMonth[pt[0]] = true;
      });
    });

    // The aggregate comes from the server, which owns the definition of what
    // belongs to the genre (SPEC_FIXES F2).
    var rest = state.restOfGenre || {name: "Reste du genre", points: []};
    rest.points.forEach(function (pt) { byMonth[pt[0]] = true; });

    var months = Object.keys(byMonth).sort();
    var out = LEADERS.map(function (id) {
      var p = index[id].points;
      return {
        id: id,
        name: index[id].name,
        points: months.filter(function (m) { return p[m] !== undefined; })
                      .map(function (m) { return {month: m, value: p[m]}; }),
      };
    });
    out.push({
      id: "__other__",
      name: rest.name,
      points: rest.points.map(function (pt) { return {month: pt[0], value: pt[1]}; }),
    });
    return {series: out, months: months};
  }

  function renderTwitch() {
    var box = $("[data-chart-twitch]");
    var data = twitchSeries();
    if (!data.months.length) { box.replaceChildren(el("p", "note", "Pas de série Twitch chargée.")); return; }

    // Which series are switched off, by game id. The scale follows what is left,
    // which is the point of switching one off: Fortnite dwarfs the rest.
    var off = {};

    var legend = $("[data-legend]");
    legend.replaceChildren.apply(legend, data.series.map(function (s, i) {
      var colour = ink(SERIES_INK[i % SERIES_INK.length]);
      var sw = el("i");
      if (SERIES_DASH[i % SERIES_DASH.length]) {
        sw.style.background = "none";
        sw.style.borderTop = "2px dashed " + colour;
      } else {
        sw.style.background = colour;
      }
      var button = el("button");
      button.type = "button";
      button.setAttribute("aria-pressed", "true");
      button.title = "Masquer ou remontrer " + s.name;
      button.append(sw, el("span", null, s.name));
      button.addEventListener("click", function () {
        var left = data.series.filter(function (x) { return !off[x.id]; });
        if (!off[s.id] && left.length < 2) return;  // never leave an empty chart
        off[s.id] = !off[s.id];
        button.setAttribute("aria-pressed", off[s.id] ? "false" : "true");
        draw();
      });
      return button;
    }));

    var tip = el("div", "tip");
    box.replaceChildren(tip);

    function draw() {
      var old = box.querySelector("svg");
      if (old) old.remove();

      var shown = [];
      data.series.forEach(function (ser, si) {
        if (!off[ser.id]) shown.push({ser: ser, si: si});
      });

      var W = Math.max(280, box.clientWidth);
      var H = W < 560 ? 240 : 340;
      var max = 0;
      shown.forEach(function (item) {
        item.ser.points.forEach(function (p) { if (p.value > max) max = p.value; });
      });
      if (!max) max = 1;
      var step = Math.pow(10, Math.floor(Math.log10(max)));
      var top = Math.ceil(max / step) * step;

      var m = {t: 12, r: W < 560 ? 8 : 92, b: 26, l: 46};
      var iw = W - m.l - m.r, ih = H - m.t - m.b;
      var xOf = function (i) { return m.l + (data.months.length < 2 ? 0 : i * iw / (data.months.length - 1)); };
      var yOf = function (v) { return m.t + ih - (v / top) * ih; };

      var s = svg("svg", {viewBox: "0 0 " + W + " " + H, role: "img",
        "aria-label": "Heures regardées par mois sur Twitch, "
          + shown.length + " séries affichées"});

      var ax = svg("g", {class: "ax"});
      var ticks = 4;
      for (var t = 0; t <= ticks; t++) {
        var v = top * t / ticks, y = yOf(v);
        ax.append(svg("line", {x1: m.l, x2: W - m.r, y1: y, y2: y,
          class: t === 0 ? "base" : null}));
        var lab = svg("text", {x: m.l - 8, y: y + 4, "text-anchor": "end"});
        lab.textContent = v >= 1e6 ? Math.round(v / 1e6) + " M" : Math.round(v / 1e3) + " k";
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

      var labels = [];
      shown.forEach(function (item) {
        var ser = item.ser, si = item.si;
        var d = ser.points.map(function (p) {
          return xOf(data.months.indexOf(p.month)) + "," + yOf(p.value);
        }).join(" L");
        var path = svg("path", {d: "M" + d, fill: "none", "stroke-width": 2,
          "stroke-linejoin": "round", "stroke-linecap": "round",
          "stroke-dasharray": SERIES_DASH[si % SERIES_DASH.length] || undefined});
        path.style.stroke = ink(SERIES_INK[si % SERIES_INK.length]);
        s.append(path);

        if (W >= 560 && ser.points.length) {
          var last = ser.points[ser.points.length - 1];
          labels.push({
            x: xOf(data.months.indexOf(last.month)) + 7,
            y: yOf(last.value) + 4,
            name: ser.name.length > 13 ? ser.name.slice(0, 12) + "…" : ser.name,
          });
        }
      });

      // Four lines that end close together would print four labels on top of
      // each other; space them out from the top down.
      labels.sort(function (a, b) { return a.y - b.y; });
      var floor = m.t;
      labels.forEach(function (item) {
        var y = Math.max(item.y, floor + 11);
        floor = y;
        var tx = svg("text", {x: item.x, y: Math.min(y, m.t + ih), "font-size": 11});
        tx.style.fill = ink("--ink-soft");
        tx.textContent = item.name;
        s.append(tx);
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
        shown.forEach(function (item) {
          var ser = item.ser;
          var pt = null;
          for (var k = 0; k < ser.points.length; k++) {
            if (ser.points[k].month === month) { pt = ser.points[k]; break; }
          }
          var row = el("div", "tip-r");
          var sw = el("i");
          sw.style.background = ink(SERIES_INK[item.si % SERIES_INK.length]);
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

    register(box, draw);
  }

  /* ---- filtering a grid of cards ---------------------------------------- */

  /* The grids hold one card per game, which is more than fits on a screen once
     the collection runs. Each card carries a search key and the input hides
     what does not match. */
  function wireFilter(kind, box) {
    var input = document.querySelector('[data-filter="' + kind + '"]');
    var count = document.querySelector('[data-filter-count="' + kind + '"]');
    var controls = document.querySelector("[data-" + kind + "-controls]");
    if (controls) controls.hidden = false;
    if (!input) return;

    function apply() {
      var q = input.value.trim().toLowerCase();
      var shown = 0, total = 0;
      Array.prototype.forEach.call(box.children, function (card) {
        total++;
        var hit = !q || (card.dataset.key || "").indexOf(q) >= 0;
        card.hidden = !hit;
        if (hit) shown++;
      });
      if (count) {
        count.textContent = q ? shown + " sur " + total + " jeux" : total + " jeux";
      }
    }
    input.addEventListener("input", apply);
    apply();
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
      card.dataset.key = ((names[id] || id) + " " + id).toLowerCase();

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
      trace(tw, "--s2", true);
      trace(yt, "--s1", false);

      [[twPeak, "--s2"], [ytPeak, "--s1"]].forEach(function (pair) {
        if (!pair[0]) return;
        var dot = svg("circle", {cx: xOf(pair[0]), cy: yOf(100), r: 3.2});
        dot.style.fill = ink(pair[1]);
        s.append(dot);
      });

      card.append(s);

      var key = el("div", "cmp-key");
      [["--s1", "attention YouTube"], ["--s2", "heures Twitch"]].forEach(function (k) {
        var sp = el("span");
        var sw = el("i");
        sw.style.background = ink(k[0]);
        sp.append(sw, el("span", null, k[1]));
        key.append(sp);
      });
      card.append(key);
      return card;
    }));

    wireFilter("compare", box);
  }

  /* ---- franchises -------------------------------------------------------- */

  function renderFranchises() {
    var all = state.franchises || [];
    var usable = all.filter(function (f) { return f.twitch.length; });
    if (!usable.length) return;

    document.querySelector("[data-franchise-empty]").hidden = true;
    var box = $("[data-franchise]");
    box.hidden = false;

    box.replaceChildren.apply(box, usable.map(function (f) {
      var card = el("figure", "plot");
      card.style.margin = "0";

      var head = el("div", "cmp-head");
      head.append(el("span", "cmp-name", f.title));
      var ytPeak = f.youtube.length ? peakOf(f.youtube) : null;
      var twPeak = peakOf(f.twitch);
      if (ytPeak && twPeak) {
        var gap = monthsApart(ytPeak, twPeak);
        var chip = el("span", "cmp-gap");
        if (gap > 0) { chip.dataset.lead = "youtube"; chip.textContent = "YouTube en avance de " + gap + " mois"; }
        else if (gap < 0) { chip.dataset.lead = "twitch"; chip.textContent = "Twitch en avance de " + (-gap) + " mois"; }
        else { chip.textContent = "pics simultanés"; }
        head.append(chip);
      }
      card.append(head);

      var sub = el("p", "cmp-sub");
      sub.textContent = f.membersPresent.join(" + ")
        + (f.youtube.length
            ? " · pic Twitch " + monthLabel(twPeak)
              + (ytPeak ? " · pic YouTube " + monthLabel(ytPeak) : "")
            : " · pas encore de données YouTube");
      card.append(sub);

      card.append(twoSeriesChart(f.twitch, f.youtube, f.title));

      var key = el("div", "cmp-key");
      [["--s1", "attention YouTube"], ["--s2", "heures Twitch"]].forEach(function (k) {
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

  function peakOf(points) {
    var best = null;
    points.forEach(function (p) { if (!best || p[1] > best[1]) best = p; });
    return best ? best[0] : null;
  }

  function twoSeriesChart(twitch, youtube, label) {
    var monthSet = {};
    twitch.concat(youtube).forEach(function (p) { monthSet[p[0]] = true; });
    var months = Object.keys(monthSet).sort();

    var W = 420, H = 130, m = {t: 8, r: 6, b: 20, l: 6};
    var iw = W - m.l - m.r, ih = H - m.t - m.b;
    var xOf = function (mo) {
      var i = months.indexOf(mo);
      return m.l + (months.length < 2 ? 0 : i * iw / (months.length - 1));
    };
    var yOf = function (v) { return m.t + ih - (v / 100) * ih; };

    var s = svg("svg", {viewBox: "0 0 " + W + " " + H, role: "img",
      "aria-label": label + " : attention YouTube et audience Twitch, chacune sur 0 à 100"});

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

    [[twitch, "--s2", "3 3"], [youtube, "--s1", null]].forEach(function (spec) {
      if (spec[0].length < 2) return;
      var d = spec[0].map(function (p) { return xOf(p[0]) + "," + yOf(p[1]); }).join(" L");
      var path = svg("path", {d: "M" + d, fill: "none", "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
        "stroke-dasharray": spec[2] || undefined});
      path.style.stroke = ink(spec[1]);
      s.append(path);
    });
    return s;
  }

  /* ---- the genre's chronology ------------------------------------------- */

  function gameSpans() {
    var byId = {};
    state.games.forEach(function (g) { byId[g.id] = g; });
    return state.series.map(function (ser) {
      var months = ser.points.map(function (p) { return p[0]; }).sort();
      var peak = ser.points.reduce(function (best, p) {
        return !best || p[1] > best[1] ? p : best;
      }, null);
      return {
        id: ser.game_id,
        name: ser.name,
        months: months,
        first: months[0],
        last: months[months.length - 1],
        peak: peak ? peak[0] : null,
        hours: (byId[ser.game_id] || {}).hours || 0,
        enabled: (byId[ser.game_id] || {}).enabled,
      };
    }).filter(function (g) { return g.months.length; })
      .sort(function (a, b) { return a.first < b.first ? -1 : a.first > b.first ? 1 : 0; });
  }

  function allMonths() {
    var set = {};
    state.series.forEach(function (s) {
      s.points.forEach(function (p) { set[p[0]] = true; });
    });
    return Object.keys(set).sort();
  }

  function renderChrono() {
    var box = $("[data-chart-chrono]");
    var showModes = $("[data-chrono-modes]");
    var byVolume = $("[data-chrono-sort]");
    var all = gameSpans();
    var months = allMonths();
    if (!all.length || !months.length) {
      box.replaceChildren(el("p", "note", "Pas de série Twitch chargée."));
      return;
    }

    function selection() {
      var list = all.filter(function (g) {
        return g.enabled || (showModes && showModes.checked);
      });
      if (byVolume && byVolume.checked) {
        list = list.slice().sort(function (a, b) { return (b.hours || 0) - (a.hours || 0); });
      }
      return list;
    }

    function draw() {
      var games = selection();
      box.replaceChildren();
      if (!games.length) {
        box.replaceChildren(el("p", "note", "Aucun jeu sélectionné."));
        return;
      }
      var W = Math.max(300, box.clientWidth);
      var rowH = 15, labelW = W < 620 ? 0 : 160;
      var m = {t: 8, r: 10, b: 24, l: labelW};
      var H = m.t + games.length * rowH + m.b;
      var iw = W - m.l - m.r;
      var xOf = function (mo) {
        return m.l + months.indexOf(mo) * iw / Math.max(1, months.length - 1);
      };
      var cellW = Math.max(1.4, iw / months.length);

      var s = svg("svg", {viewBox: "0 0 " + W + " " + H, role: "img",
        "aria-label": "Présence de " + games.length
          + " jeux au top 200 Twitch, "
          + (byVolume && byVolume.checked ? "du plus regardé au moins regardé"
                                          : "du plus ancien arrivé au plus récent")});

      var ax = svg("g", {class: "ax"});
      months.forEach(function (mo) {
        if (mo.slice(5) !== "01" || parseInt(mo.slice(0, 4), 10) % 2) return;
        var x = xOf(mo);
        ax.append(svg("line", {x1: x, x2: x, y1: m.t, y2: m.t + games.length * rowH}));
        var tx = svg("text", {x: x, y: H - 8, "text-anchor": "middle"});
        tx.textContent = mo.slice(0, 4);
        ax.append(tx);
      });
      s.append(ax);

      games.forEach(function (g, i) {
        var y = m.t + i * rowH;
        var row = svg("g", {class: "chrono-row"});
        var tip = svg("title");
        tip.textContent = g.name + " — " + g.months.length + " mois, "
          + g.first + " à " + g.last + ", pic " + (g.peak || "?")
          + ", " + hours(g.hours || 0) + " h regardées";
        row.append(tip);
        row.append(svg("rect", {x: 0, y: y + 1, width: W, height: rowH - 2,
          class: "heat-row-bg"}));

        g.months.forEach(function (mo) {
          var bar = svg("rect", {x: xOf(mo) - cellW / 2, y: y + 2,
            width: cellW, height: rowH - 5, class: "chrono-bar"});
          bar.style.fill = g.enabled ? ink("--s1") : ink("--s-other");
          bar.style.opacity = g.enabled ? 0.55 : 0.3;
          row.append(bar);
        });

        if (g.peak) {
          var mark = svg("line", {x1: xOf(g.peak), x2: xOf(g.peak),
            y1: y + 1, y2: y + rowH - 2, "stroke-width": 1.6});
          mark.style.stroke = ink("--s2");
          row.append(mark);
        }

        if (labelW) {
          var label = svg("text", {x: labelW - 8, y: y + rowH / 2 + 3,
            "text-anchor": "end", "font-size": 10});
          label.style.fill = g.enabled ? ink("--ink-soft") : ink("--ink-faint");
          label.textContent = g.name.length > 24 ? g.name.slice(0, 23) + "…" : g.name;
          row.append(label);
        }
        s.append(row);
      });
      box.append(s);
    }

    [showModes, byVolume].forEach(function (input) {
      if (input) input.addEventListener("change", draw);
    });
    register(box, draw);
  }

  /* ---- rise against decline -------------------------------------------- */

  function renderShape() {
    var box = $("[data-chart-shape]");
    var games = gameSpans().filter(function (g) { return g.peak && g.months.length >= 3; });
    if (!games.length) { box.replaceChildren(el("p", "note", "Pas assez de données.")); return; }

    function monthsApartLocal(a, b) {
      return (parseInt(b.slice(0, 4), 10) * 12 + parseInt(b.slice(5, 7), 10)) -
             (parseInt(a.slice(0, 4), 10) * 12 + parseInt(a.slice(5, 7), 10));
    }
    var pts = games.map(function (g) {
      return {
        name: g.name, hours: g.hours, enabled: g.enabled,
        rise: monthsApartLocal(g.first, g.peak),
        fall: monthsApartLocal(g.peak, g.last),
      };
    });

    function draw() {
      box.replaceChildren();
      var W = Math.max(300, box.clientWidth);
      var H = W < 560 ? 320 : 400;
      var m = {t: 14, r: 14, b: 38, l: 44};
      var maxRise = Math.max.apply(null, pts.map(function (p) { return p.rise; })) + 2;
      var maxFall = Math.max.apply(null, pts.map(function (p) { return p.fall; })) + 2;
      var lim = Math.max(maxRise, maxFall);
      var xOf = function (v) { return m.l + v / lim * (W - m.l - m.r); };
      var yOf = function (v) { return H - m.b - v / lim * (H - m.t - m.b); };
      var maxHours = Math.max.apply(null, pts.map(function (p) { return p.hours; })) || 1;

      var s = svg("svg", {viewBox: "0 0 " + W + " " + H, role: "img",
        "aria-label": "Mois jusqu'au pic contre mois après le pic, un point par jeu"});

      var ax = svg("g", {class: "ax"});
      for (var t = 0; t <= 4; t++) {
        var v = Math.round(lim * t / 4);
        ax.append(svg("line", {x1: m.l, x2: W - m.r, y1: yOf(v), y2: yOf(v),
          class: t === 0 ? "base" : null}));
        var ly = svg("text", {x: m.l - 8, y: yOf(v) + 4, "text-anchor": "end"});
        ly.textContent = v;
        ax.append(ly);
        var lx = svg("text", {x: xOf(v), y: H - 20, "text-anchor": "middle"});
        lx.textContent = v;
        ax.append(lx);
      }
      var xt = svg("text", {x: (m.l + W - m.r) / 2, y: H - 4, "text-anchor": "middle"});
      xt.textContent = "mois jusqu'au pic";
      ax.append(xt);
      s.append(ax);

      // The diagonal: above it a game lasted longer than it took to rise.
      var diag = svg("line", {x1: xOf(0), y1: yOf(0), x2: xOf(lim), y2: yOf(lim),
        "stroke-dasharray": "4 4", "stroke-width": 1});
      diag.style.stroke = ink("--ink-faint");
      s.append(diag);

      pts.forEach(function (p) {
        var r = 3 + 9 * Math.sqrt(p.hours / maxHours);
        var g = svg("g");
        var tip = svg("title");
        tip.textContent = p.name + " — " + p.rise + " mois pour monter, "
          + p.fall + " mois après le pic";
        var c = svg("circle", {cx: xOf(p.rise), cy: yOf(p.fall), r: r.toFixed(1)});
        c.style.fill = p.enabled ? ink("--s3") : ink("--s-other");
        c.style.opacity = 0.45;
        c.style.stroke = p.enabled ? ink("--s3") : ink("--s-other");
        g.append(tip, c);
        if (r > 8) {
          var lab = svg("text", {x: xOf(p.rise), y: yOf(p.fall) - r - 4,
            "text-anchor": "middle", class: "dot-label"});
          lab.style.fill = ink("--ink-soft");
          lab.textContent = p.name.length > 18 ? p.name.slice(0, 17) + "…" : p.name;
          g.append(lab);
        }
        s.append(g);
      });

      var yt = svg("text", {x: 10, y: m.t + 4, "font-size": 11});
      yt.style.fill = ink("--ink-faint");
      yt.textContent = "mois après";
      s.append(yt);
      box.append(s);
    }

    register(box, draw);
  }

  /* ---- who publishes --------------------------------------------------- */

  function renderWho() {
    if (!state.attention.length) return;
    $("[data-who-empty]").hidden = true;
    var box = $("[data-who]");
    box.hidden = false;

    var names = {};
    state.games.forEach(function (g) { names[g.id] = g.name; });
    var byGame = {};
    state.attention.forEach(function (r) {
      if (r.hhi === null || r.hhi === undefined) return;
      (byGame[r.game_id] = byGame[r.game_id] || []).push(r);
    });

    var cards = Object.keys(byGame).sort().map(function (id) {
      var rows = byGame[id].slice().sort(function (a, b) {
        return (a.year - b.year) || (a.month - b.month);
      });
      var card = el("figure", "plot");
      card.style.margin = "0";
      card.dataset.key = ((names[id] || id) + " " + id).toLowerCase();
      var head = el("div", "who-head");
      head.append(el("span", "who-name", names[id] || id));
      var mean = rows.reduce(function (t, r) { return t + r.hhi; }, 0) / rows.length;
      head.append(el("span", "who-note",
        "Herfindahl moyen " + mean.toFixed(2).replace(".", ",")));
      card.append(head);

      var W = 420, H = 110, m = {t: 10, r: 8, b: 20, l: 30};
      var iw = W - m.l - m.r, ih = H - m.t - m.b;
      var xOf = function (i) { return m.l + (rows.length < 2 ? 0 : i * iw / (rows.length - 1)); };
      var yOf = function (v) { return m.t + ih - v * ih; };
      var s = svg("svg", {viewBox: "0 0 " + W + " " + H, role: "img",
        "aria-label": "Concentration des vues entre chaînes pour " + (names[id] || id)});

      var ax = svg("g", {class: "ax"});
      [0, 0.5, 1].forEach(function (v) {
        ax.append(svg("line", {x1: m.l, x2: W - m.r, y1: yOf(v), y2: yOf(v),
          class: v === 0 ? "base" : null}));
        var t = svg("text", {x: m.l - 6, y: yOf(v) + 4, "text-anchor": "end"});
        t.textContent = v === 0 ? "0" : v === 1 ? "1" : "0,5";
        ax.append(t);
      });
      rows.forEach(function (r, i) {
        if (r.month !== 1) return;
        var t = svg("text", {x: xOf(i), y: H - 6, "text-anchor": "middle"});
        t.textContent = r.year;
        ax.append(t);
      });
      s.append(ax);

      [["hhi", "--s1", null], ["topShare", "--s4", "4 3"], ["shorts", "--s3", "2 3"]]
        .forEach(function (spec) {
          var pts = rows.map(function (r, i) {
            var v = r[spec[0]];
            return v === null || v === undefined ? null : xOf(i) + "," + yOf(v);
          }).filter(Boolean);
          if (pts.length < 2) return;
          var path = svg("path", {d: "M" + pts.join(" L"), fill: "none",
            "stroke-width": 1.8, "stroke-dasharray": spec[2] || undefined,
            "stroke-linejoin": "round"});
          path.style.stroke = ink(spec[1]);
          s.append(path);
        });
      card.append(s);

      var key = el("div", "cmp-key");
      [["--s1", "Herfindahl des chaînes", null],
       ["--s4", "part de la 1re chaîne", "dashed"],
       ["--s3", "part de Shorts", "dotted"]].forEach(function (k) {
        var sp = el("span");
        var sw = el("i");
        if (k[2]) {
          sw.style.background = "none";
          sw.style.borderTop = "2px " + k[2] + " " + ink(k[0]);
        } else {
          sw.style.background = ink(k[0]);
        }
        sp.append(sw, el("span", null, k[1]));
        key.append(sp);
      });
      card.append(key);
      return card;
    });

    box.replaceChildren.apply(box, cards);
    wireFilter("who", box);
  }

  /* ---- roster ---------------------------------------------------------- */

  var TEXT_KEYS = ["name", "role", "first", "peak"];
  var rosterSort = {key: "hours", dir: -1};

  function renderRoster() {
    var body = $("[data-roster]");
    var input = document.querySelector('[data-filter="roster"]');
    var count = document.querySelector('[data-filter-count="roster"]');
    var heads = Array.prototype.slice.call(
      document.querySelectorAll(".roster thead th[data-sort]")
    );
    var maxHours = 0;
    state.games.forEach(function (g) { if ((g.hours || 0) > maxHours) maxHours = g.hours || 0; });

    function selection() {
      var q = ((input && input.value) || "").trim().toLowerCase();
      var list = state.games.filter(function (g) {
        return !q || (g.name || "").toLowerCase().indexOf(q) >= 0
                  || (g.id || "").indexOf(q) >= 0;
      });
      var key = rosterSort.key, dir = rosterSort.dir;
      return list.slice().sort(function (a, b) {
        if (TEXT_KEYS.indexOf(key) >= 0) {
          var x = (a[key] || ""), y = (b[key] || "");
          return x < y ? -dir : x > y ? dir : 0;
        }
        return ((a[key] || 0) - (b[key] || 0)) * dir;
      });
    }

    function row(g) {
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
    }

    function paintRows() {
      var list = selection();
      body.replaceChildren.apply(body, list.map(row));
      if (count) {
        count.textContent = list.length === state.games.length
          ? n(list.length) + " entrées"
          : n(list.length) + " sur " + n(state.games.length) + " entrées";
      }
    }

    heads.forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.dataset.sort;
        if (rosterSort.key === key) {
          rosterSort.dir = -rosterSort.dir;
        } else {
          rosterSort.key = key;
          rosterSort.dir = TEXT_KEYS.indexOf(key) >= 0 ? 1 : -1;
        }
        heads.forEach(function (other) { other.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", rosterSort.dir > 0 ? "ascending" : "descending");
        paintRows();
      });
    });
    if (input) input.addEventListener("input", paintRows);
    paintRows();
  }

  function renderFooter() {
    var d = new Date(state.generatedAt);
    $("[data-generated]").textContent = "le " + d.toLocaleDateString("fr-FR") +
      " à " + d.toLocaleTimeString("fr-FR", {hour: "2-digit", minute: "2-digit"});
  }

  /* ---- the game x tag matrix -------------------------------------------- */

  /* The matrix is derived from the client exports held under a confidentiality
     agreement, so the server only serves it on an opt-in local run. The tab is
     always there and explains itself when there is nothing to show. */
  var matrix = {payload: null, loading: false, asked: false, tag: null, game: null, order: null};

  var HEAT_STEPS = [0.14, 0.32, 0.52, 0.74, 1];

  function heatOpacity(value, scale) {
    if (!(value > 0) || !scale) return 0;
    var share = Math.min(1, value / scale);
    for (var i = 0; i < HEAT_STEPS.length; i++) {
      if (share <= (i + 1) / HEAT_STEPS.length) return HEAT_STEPS[i];
    }
    return 1;
  }

  function matrixParams() {
    var tag = ($("[data-matrix-tag]").value || "").trim();
    return "?games=" + encodeURIComponent($("[data-matrix-games]").value)
      + "&tags=" + encodeURIComponent($("[data-matrix-tags]").value)
      + (tag ? "&tag=" + encodeURIComponent(tag) : "");
  }

  function loadMatrix() {
    if (matrix.loading) return;
    matrix.loading = true;
    var note = $("[data-matrix-count]");
    if (note) note.textContent = "chargement…";
    var reason = $("[data-matrix-reason]");
    if (reason && !matrix.payload) reason.textContent = "Lecture de la matrice locale…";
    fetch("/api/genres/matrix" + matrixParams())
      .then(function (r) { return r.json(); })
      .then(function (payload) {
        matrix.loading = false;
        matrix.payload = payload;
        matrix.game = null;
        matrix.order = null;
        renderMatrix();
      })
      .catch(function (error) {
        matrix.loading = false;
        matrix.payload = {available: false, reason: "network", detail: String(error)};
        renderMatrix();
      });
  }

  var MATRIX_REASONS = {
    disabled: [
      "Cette vue reste locale : le serveur ne la sert pas.",
      "Les deux CSV sources sont des données client sous accord de confidentialité. "
        + "Relancez le serveur en local avec YTC_GENRES_LOCAL=1 pour l'activer ; "
        + "un service déployé ne doit jamais la servir.",
    ],
    missing: [
      "La matrice n'est pas encore construite.",
      "Lancez ytc genres build-matrix (jalon G1). Attendus dans "
        + "data/processed/genres/ : matrix.npz, rows.parquet et cols.parquet.",
    ],
    mismatch: [
      "La matrice et ses étiquettes ne s'accordent pas.",
      "Reconstruisez-la : rows.parquet et cols.parquet doivent décrire exactement "
        + "les lignes et les colonnes de matrix.npz.",
    ],
    unreadable: [
      "La matrice est illisible.",
      "Le fichier attendu est une matrice CSR écrite par scipy.sparse.save_npz.",
    ],
    network: [
      "Le serveur n'a pas répondu.",
      "Rechargez la page ; la vue ne lit rien d'autre que des fichiers locaux.",
    ],
  };

  function renderMatrix() {
    var payload = matrix.payload || {};
    var empty = $("[data-matrix-empty]");
    var body = $("[data-matrix]");

    if (!payload.available) {
      var said = MATRIX_REASONS[payload.reason] || MATRIX_REASONS.unreadable;
      $("[data-matrix-reason]").textContent = said[0];
      var howto = said[1];
      if (payload.detail) howto += " (" + payload.detail + ")";
      if (payload.missing && payload.missing.length) {
        howto += " Manquent : " + payload.missing.join(", ") + ".";
      }
      $("[data-matrix-howto]").textContent = howto;
      empty.hidden = false;
      body.hidden = true;
      return;
    }

    empty.hidden = true;
    body.hidden = false;
    renderMatrixStats(payload);

    var note = $("[data-matrix-count]");
    if (note) {
      note.textContent = payload.games.length + " jeux × " + payload.tags.length
        + " tags, " + n(payload.cells.length) + " cases non nulles";
    }
    renderHeatKey(payload);
    drawHeat();
    renderMatrixDetail();
  }

  function renderMatrixStats(payload) {
    var box = $("[data-matrix-stats]");
    var src = payload.source || {};
    var pairs = [
      ["Jeux retenus", n(src.games)],
      ["Tags en colonnes", n(src.tags)],
      ["Tags par jeu", String(src.tagsPerGame || 0).replace(".", ",")],
      ["Votes médians", n(src.votesMedian)],
    ];
    box.replaceChildren.apply(box, pairs.map(function (pair) {
      var cell = el("div");
      cell.append(el("dt", null, pair[0]), el("dd", null, pair[1]));
      return cell;
    }));
  }

  function renderHeatKey(payload) {
    var box = $("[data-matrix-key]");
    var ramp = el("div", "ramp");
    HEAT_STEPS.forEach(function (step) {
      var cell = el("i");
      cell.style.background = ink("--s1");
      cell.style.opacity = step;
      ramp.append(cell);
    });
    box.replaceChildren(
      el("span", null, "poids TF-IDF faible"),
      ramp,
      el("span", null, "fort (jusqu'à " + String(payload.scale).replace(".", ",") + ")"),
      el("span", "hint", "chaque ligne est de norme 1, les poids d'un jeu sont comparables entre eux")
    );
  }

  function matrixOrder(payload) {
    var mode = $("[data-matrix-sort]").value;
    var order = payload.games.map(function (_, i) { return i; });
    if (mode === "name") {
      order.sort(function (a, b) {
        var x = payload.games[a].name.toLowerCase(), y = payload.games[b].name.toLowerCase();
        return x < y ? -1 : x > y ? 1 : 0;
      });
    } else if (mode === "selected" && matrix.tag !== null) {
      var weight = {};
      payload.cells.forEach(function (c) {
        if (c[1] === matrix.tag) weight[c[0]] = c[2];
      });
      order.sort(function (a, b) { return (weight[b] || 0) - (weight[a] || 0); });
    }
    return order;
  }

  function drawHeat() {
    var payload = matrix.payload;
    var box = $("[data-matrix-heat]");
    if (!payload.games.length || !payload.tags.length) {
      box.replaceChildren(el("p", "note", "Aucun tag ne correspond à ce filtre."));
      return;
    }

    var order = matrixOrder(payload);
    var value = {};
    payload.cells.forEach(function (c) { value[c[0] + ":" + c[1]] = c[2]; });

    var cell = 15, labelW = 168, headH = 104;
    var W = labelW + payload.tags.length * cell + 12;
    var H = headH + order.length * cell + 8;

    var tip = el("div", "tip");
    var s = svg("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img",
      "aria-label": "Matrice de " + order.length + " jeux par " + payload.tags.length
        + " tags, poids TF-IDF"});

    // Tag headers, slanted so long names stay readable; clicking one selects it.
    payload.tags.forEach(function (tag, j) {
      var x = labelW + j * cell + cell / 2;
      var label = svg("text", {x: x, y: headH - 8, class: "heat-label",
        transform: "rotate(-45 " + x + " " + (headH - 8) + ")"});
      label.textContent = tag.tag.length > 22 ? tag.tag.slice(0, 21) + "…" : tag.tag;
      label.setAttribute("data-picked", matrix.tag === j ? "yes" : "no");
      var title = svg("title");
      title.textContent = tag.tag + " — famille " + tag.family + ", présent dans "
        + n(tag.df) + " jeux, idf " + String(tag.idf).replace(".", ",");
      label.append(title);
      label.addEventListener("click", function () {
        matrix.tag = matrix.tag === j ? null : j;
        matrix.game = null;
        drawHeat();
        renderMatrixDetail();
      });
      s.append(label);
    });

    order.forEach(function (gi, rowIndex) {
      var game = payload.games[gi];
      var y = headH + rowIndex * cell;
      var row = svg("g", {class: "heat-row"});
      row.append(svg("rect", {x: 0, y: y, width: W, height: cell, class: "heat-row-bg"}));

      var label = svg("text", {x: labelW - 10, y: y + cell / 2 + 3.5,
        "text-anchor": "end", class: "heat-label"});
      label.textContent = game.name.length > 26 ? game.name.slice(0, 25) + "…" : game.name;
      label.setAttribute("data-picked", matrix.game === gi ? "yes" : "no");
      var gtitle = svg("title");
      gtitle.textContent = game.name + " — appid " + game.appid + ", "
        + n(game.totalVotes) + " votes de tags"
        + (game.releaseDate ? ", sortie " + game.releaseDate : "");
      label.append(gtitle);
      label.addEventListener("click", function () {
        matrix.game = matrix.game === gi ? null : gi;
        matrix.tag = null;
        drawHeat();
        renderMatrixDetail();
      });
      row.append(label);

      payload.tags.forEach(function (tag, j) {
        var v = value[gi + ":" + j] || 0;
        var rect = svg("rect", {x: labelW + j * cell, y: y + 1,
          width: cell - 1, height: cell - 2, class: "heat-cell"});
        rect.style.fill = ink("--s1");
        rect.style.fillOpacity = heatOpacity(v, payload.scale);
        rect.addEventListener("pointerenter", function () {
          tip.replaceChildren(
            el("div", "tip-h", game.name),
            rowOf(tag.tag, v ? String(v).replace(".", ",") : "0"),
            rowOf("famille", tag.family),
            rowOf("tag dans", n(tag.df) + " jeux")
          );
          tip.style.opacity = 1;
          tip.style.left = Math.max(0, Math.min(labelW + j * cell + 18, W - 200)) + "px";
          tip.style.top = Math.max(0, y - 10) + "px";
        });
        row.append(rect);
      });
      s.append(row);
    });

    s.addEventListener("pointerleave", function () { tip.style.opacity = 0; });
    box.replaceChildren(tip, s);
  }

  function rowOf(label, value) {
    var row = el("div", "tip-r");
    row.append(el("span", null, label), el("b", null, value));
    return row;
  }

  /* The detail panel: one game's tag profile, or one tag's heaviest games.
     Both are read from the loaded corner of the matrix, not from the whole
     thing, so they describe what is on screen. */
  function renderMatrixDetail() {
    var payload = matrix.payload;
    var box = $("[data-matrix-detail]");
    if (!payload || (matrix.game === null && matrix.tag === null)) {
      box.hidden = true;
      box.replaceChildren();
      return;
    }

    var items = [];
    var heading, sub;
    if (matrix.game !== null) {
      var game = payload.games[matrix.game];
      heading = game.name;
      sub = "appid " + game.appid + " · " + n(game.totalVotes) + " votes de tags"
        + (game.releaseDate ? " · sortie " + game.releaseDate : "")
        + " — poids parmi les " + payload.tags.length + " tags affichés";
      payload.cells.forEach(function (c) {
        if (c[0] === matrix.game) {
          items.push({label: payload.tags[c[1]].tag, value: c[2]});
        }
      });
    } else {
      var tag = payload.tags[matrix.tag];
      heading = tag.tag;
      sub = "famille " + tag.family + " · présent dans " + n(tag.df) + " jeux · idf "
        + String(tag.idf).replace(".", ",")
        + " — poids parmi les " + payload.games.length + " jeux affichés";
      payload.cells.forEach(function (c) {
        if (c[1] === matrix.tag) {
          items.push({label: payload.games[c[0]].name, value: c[2]});
        }
      });
    }
    items.sort(function (a, b) { return b.value - a.value; });
    items = items.slice(0, 15);

    var top = items.length ? items[0].value : 1;
    var list = el("ol");
    items.forEach(function (item) {
      var li = el("li");
      var meter = el("span", "meter");
      var fill = el("i");
      fill.style.width = Math.max(1, Math.round(100 * item.value / top)) + "%";
      meter.append(fill);
      li.append(el("span", null, item.label), meter,
        el("b", null, String(item.value).replace(".", ",")));
      list.append(li);
    });

    var close = el("button", "close", "Fermer");
    close.type = "button";
    close.addEventListener("click", function () {
      matrix.game = null; matrix.tag = null;
      drawHeat();
      renderMatrixDetail();
    });

    box.replaceChildren(
      el("h3", null, heading),
      el("p", "detail-sub", sub),
      items.length ? list : el("p", "note", "Aucun poids non nul ici."),
      close
    );
    box.hidden = false;
  }

  function wireMatrixControls() {
    var tag = $("[data-matrix-tag]");
    if (!tag) return;  // the tab is not in the page
    var debounce;
    tag.addEventListener("input", function () {
      clearTimeout(debounce);
      debounce = setTimeout(function () { matrix.tag = null; loadMatrix(); }, 280);
    });
    ["[data-matrix-games]", "[data-matrix-tags]"].forEach(function (sel) {
      $(sel).addEventListener("change", function () { matrix.tag = null; loadMatrix(); });
    });
    $("[data-matrix-sort]").addEventListener("change", function () {
      if (matrix.payload && matrix.payload.available) { drawHeat(); }
    });
  }

  /* ---- tabs -------------------------------------------------------------- */

  function tabs() {
    var nav = $("[data-tabs]");
    if (!nav) return;
    var buttons = Array.prototype.slice.call(nav.querySelectorAll('[role="tab"]'));
    if (!buttons.length) return;
    var panels = buttons.map(function (b) {
      return document.getElementById(b.getAttribute("aria-controls"));
    });

    function show(id, focus) {
      buttons.forEach(function (button, i) {
        var on = button.id === id;
        button.setAttribute("aria-selected", on ? "true" : "false");
        button.tabIndex = on ? 0 : -1;
        if (panels[i]) panels[i].hidden = !on;
      });
      if (id === "t-matrice" && !matrix.asked) { matrix.asked = true; loadMatrix(); }
      // The panel was hidden until now, so its charts could not measure
      // themselves; they draw at the size they actually have.
      repaintAll();
      var button = document.getElementById(id);
      if (focus && button) button.focus();
      if (window.history && history.replaceState) {
        history.replaceState(null, "", "#" + id.slice(2));
      }
    }

    buttons.forEach(function (button, i) {
      button.addEventListener("click", function () { show(button.id, false); });
      button.addEventListener("keydown", function (ev) {
        var next = null;
        if (ev.key === "ArrowRight") next = buttons[(i + 1) % buttons.length];
        else if (ev.key === "ArrowLeft") next = buttons[(i - 1 + buttons.length) % buttons.length];
        else if (ev.key === "Home") next = buttons[0];
        else if (ev.key === "End") next = buttons[buttons.length - 1];
        if (!next) return;
        ev.preventDefault();
        show(next.id, true);
      });
    });

    nav.hidden = false;
    var wanted = "t-" + (location.hash || "").replace(/^#/, "");
    var start = buttons.filter(function (b) { return b.id === wanted; })[0] || buttons[0];
    show(start.id, false);

    window.addEventListener("hashchange", function () {
      var id = "t-" + location.hash.replace(/^#/, "");
      if (buttons.filter(function (b) { return b.id === id; }).length) show(id, false);
    });
  }

  renderStrip();
  renderCalendar();
  renderReadout();
  tickReset();
  renderTwitch();
  renderCompare();
  renderFranchises();
  renderChrono();
  renderShape();
  renderWho();
  renderRoster();
  renderFooter();
  wireMatrixControls();
  tabs();
})();
