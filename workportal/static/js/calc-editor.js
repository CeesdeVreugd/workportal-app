/* WorkPortal – calculatie-editor */
(function () {
  "use strict";
  var CFG = window.CALC_CFG;
  var data = JSON.parse(document.getElementById("calcdata").textContent);
  var RO = CFG.readonly;
  var HOURS = ["uur", "uren", "u"];
  var active = 0; // index van tabblad; -1 = totalen
  var dirty = false, saving = false, timer = null;
  var tabs = document.getElementById("tabs"), ed = document.getElementById("editor");
  var CAT_TITLES = {}; CFG.categories.forEach(function (c) { CAT_TITLES[c[0]] = c[1]; });

  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "html") e.innerHTML = attrs[k];
      else if (k.indexOf("on") === 0) e.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) e.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
    });
    (kids || []).forEach(function (k) { if (k) e.appendChild(typeof k === "string" ? document.createTextNode(k) : k); });
    return e;
  }
  var N = WP.num, eur = function (v) { return WP.eur(v, 2); };
  function fmtIn(v) {
    if (v === null || v === undefined || v === "") return "";
    return Number(v).toLocaleString("nl-NL", { maximumFractionDigits: 4, useGrouping: false });
  }

  function kdTotal(part) { return (part.koopdelen || []).reduce(function (s, k) { return s + N(k.p) * N(k.q); }, 0); }
  function lineCalc(part, l) {
    var price = l.kd ? kdTotal(part) : N(l.p), f = (l.f === "" || l.f === null || l.f === undefined) ? 1 : N(l.f);
    return { base: N(l.q) * price, incl: N(l.q) * price * f, price: price };
  }
  function secCalc(part, s) {
    var t = 0, h = 0, b = 0;
    s.lines.forEach(function (l) { var c = lineCalc(part, l); t += c.incl; b += c.base; if (HOURS.indexOf(String(l.u || "").toLowerCase()) >= 0) h += N(l.q); });
    return { total: t, base: b, hours: h };
  }
  function partCalc(part) { var t = 0, h = 0; part.sections.forEach(function (s) { var c = secCalc(part, s); t += c.total; h += c.hours; }); return { total: t, hours: h }; }
  function allCalc() { var t = 0, h = 0; data.parts.forEach(function (p) { var c = partCalc(p); t += c.total; h += c.hours; }); return { total: t, hours: h }; }

  function markDirty() {
    if (RO) return;
    dirty = true;
    document.getElementById("saveState").textContent = "Niet opgeslagen wijzigingen";
    clearTimeout(timer); timer = setTimeout(save, 2500);
  }

  function refreshTotals() {
    ed.querySelectorAll("[data-line]").forEach(function (tr) {
      var pi = +tr.getAttribute("data-p"), si = +tr.getAttribute("data-s"), li = +tr.getAttribute("data-line");
      var part = data.parts[pi], l = part.sections[si].lines[li], c = lineCalc(part, l);
      tr.querySelector(".o-base").textContent = eur(c.base);
      tr.querySelector(".o-incl").textContent = eur(c.incl);
      if (l.kd) { var pp = tr.querySelector(".kdprice"); if (pp) pp.textContent = eur(c.price); }
    });
    ed.querySelectorAll("[data-sectot]").forEach(function (e) {
      var pi = +e.getAttribute("data-p"), si = +e.getAttribute("data-s"), c = secCalc(data.parts[pi], data.parts[pi].sections[si]);
      e.textContent = eur(c.total) + (c.hours ? "  ·  " + WP.fmt(c.hours, 2) + " uur" : "");
    });
    ed.querySelectorAll("[data-kdtot]").forEach(function (e) { e.textContent = eur(kdTotal(data.parts[+e.getAttribute("data-p")])); });
    ed.querySelectorAll("[data-kdline]").forEach(function (e) {
      var k = data.parts[+e.getAttribute("data-p")].koopdelen[+e.getAttribute("data-kdline")];
      e.textContent = eur(N(k.p) * N(k.q));
    });
    tabs.querySelectorAll("[data-ptot]").forEach(function (e) { e.textContent = WP.eur(partCalc(data.parts[+e.getAttribute("data-ptot")]).total, 0); });
    var a = allCalc(), q = Math.max(1, parseInt(document.getElementById("f_quantity").value || "1", 10));
    document.getElementById("sumTotal").textContent = eur(a.total);
    document.getElementById("sumHours").textContent = WP.fmt(a.hours, 2);
    document.getElementById("perPiece").classList.toggle("hidden", q < 2);
    document.getElementById("sumPiece").textContent = eur(a.total / q);
    var agreed = N(document.getElementById("f_agreed").value);
    var ab = document.getElementById("agreedBox"); ab.classList.toggle("hidden", !agreed);
    if (agreed) {
      var d = agreed - a.total, s = document.getElementById("sumAgreed");
      s.textContent = (d >= 0 ? "+ " : "") + eur(d) + " (" + WP.fmt(a.total ? d / a.total * 100 : 0, 1) + "%)";
      s.style.color = d >= 0 ? "var(--ok)" : "var(--bad)";
    }
    if (active === -1) renderTotals();
  }

  function input(val, cls, onchange, attrs) {
    var a = Object.assign({ type: "text", value: val === null || val === undefined ? "" : val, class: cls || "" }, attrs || {});
    if (RO) a.readonly = true;
    var i = el("input", a);
    i.addEventListener("input", function () { onchange(i.value); refreshTotals(); markDirty(); });
    return i;
  }

  function renderTabs() {
    tabs.innerHTML = "";
    data.parts.forEach(function (p, i) {
      tabs.appendChild(el("button", { type: "button", class: i === active ? "active" : "", onclick: function () { active = i; render(); } },
        [p.name || ("Onderdeel " + (i + 1)), el("span", { class: "muted small", "data-ptot": i, style: "margin-left:8px;font-weight:400" })]));
    });
    tabs.appendChild(el("button", { type: "button", class: active === -1 ? "active" : "", onclick: function () { active = -1; render(); } }, ["Totalen"]));
    if (!RO) tabs.appendChild(el("button", { type: "button", onclick: addPart, title: "Onderdeel toevoegen" }, ["+ Onderdeel"]));
  }

  function addPart() {
    var base = data.parts[data.parts.length - 1];
    var maxPos = 0; data.parts.forEach(function (p) { p.sections.forEach(function (s) { maxPos = Math.max(maxPos, N(s.erp_pos)); }); });
    var np = { name: "Tabblad " + String(data.parts.length + 1).padStart(2, "0"), koopdelen: [], sections: [] };
    (base ? base.sections : []).forEach(function (s) {
      maxPos += 100;
      np.sections.push({ category: s.category, title: s.title, erp_pos: maxPos, lines: s.lines.map(function (l) { return { d: l.d, u: l.u, q: l.kd ? 1 : 0, p: l.kd ? 0 : l.p, f: l.f, kd: l.kd }; }) });
    });
    data.parts.push(np); active = data.parts.length - 1; render(); markDirty();
  }

  function unitSelect(l) {
    var s = el("select", { class: "u" });
    if (RO) s.disabled = true;
    var opts = CFG.units.slice(); if (l.u && opts.indexOf(l.u) < 0) opts.push(l.u);
    opts.forEach(function (u) { var o = el("option", { value: u, text: u }); if (u === l.u) o.selected = true; s.appendChild(o); });
    s.addEventListener("change", function () { l.u = s.value; refreshTotals(); markDirty(); });
    return s;
  }

  function renderPart(pi) {
    var part = data.parts[pi];
    var wrap = el("div", { class: "calc-part" });
    if (!RO) {
      wrap.appendChild(el("div", { class: "actions" }, [
        el("label", { class: "f", style: "flex:1;max-width:360px" }, ["Naam onderdeel", input(part.name, "", function (v) { part.name = v; var b = tabs.children[pi]; if (b) b.firstChild.textContent = v; })]),
        data.parts.length > 1 ? el("button", { type: "button", class: "btn danger sm", style: "align-self:flex-end", onclick: function () {
          if (!confirm("Onderdeel '" + part.name + "' verwijderen?")) return; data.parts.splice(pi, 1); active = 0; render(); markDirty(); } }, ["Onderdeel verwijderen"]) : null
      ]));
    }
    part.sections.forEach(function (s, si) {
      var sec = el("section", { class: "calc-sec" });
      var head = el("div", { class: "calc-sec-head" }, [
        el("span", { class: "small muted" }, ["Pos."]),
        input(s.erp_pos, "pos", function (v) { s.erp_pos = parseInt(v, 10) || null; }, { inputmode: "numeric", "aria-label": "ERP-positie" }),
        el("div", { class: "title" }, [RO ? s.title : input(s.title, "", function (v) { s.title = v; }, { style: "font-weight:700;color:var(--basic)" })]),
        el("div", { class: "tot", "data-sectot": 1, "data-p": pi, "data-s": si })
      ]);
      if (!RO) head.appendChild(el("button", { type: "button", class: "iconbtn", title: "Sectie verwijderen", "aria-label": "Sectie verwijderen", html: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
        onclick: function () { if (!confirm("Sectie '" + s.title + "' verwijderen?")) return; part.sections.splice(si, 1); render(); markDirty(); } }));
      sec.appendChild(head);
      var tbl = el("table", { class: "calc-table" });
      tbl.appendChild(el("thead", null, [el("tr", null, [
        el("th", { text: "Omschrijving" }), el("th", { text: "Aantal" }), el("th", { text: "Eenheid" }), el("th", { text: "Prijs p/st" }),
        el("th", { text: "Marge+risico" }), el("th", { text: "Totaal", style: "text-align:right" }), el("th", { text: "Incl. marge", style: "text-align:right" }), el("th")])]));
      var tb = el("tbody");
      s.lines.forEach(function (l, li) {
        var tr = el("tr", { "data-line": li, "data-p": pi, "data-s": si, class: l.kd ? "kd" : "" });
        tr.appendChild(el("td", null, [input(l.d, "", function (v) { l.d = v; })]));
        tr.appendChild(el("td", null, [input(fmtIn(l.q), "n", function (v) { l.q = N(v); }, { inputmode: "decimal" })]));
        tr.appendChild(el("td", null, [unitSelect(l)]));
        tr.appendChild(el("td", null, [l.kd ? el("span", { class: "kdprice small", title: "Totaal koopdelen specificatie" }) : input(fmtIn(l.p), "n", function (v) { l.p = N(v); }, { inputmode: "decimal" })]));
        tr.appendChild(el("td", null, [input(fmtIn(l.f), "n", function (v) { l.f = v === "" ? 1 : N(v); }, { inputmode: "decimal", title: "Factor, bijv. 1,25 = 25% marge" })]));
        tr.appendChild(el("td", { class: "out o-base" }));
        tr.appendChild(el("td", { class: "out o-incl", style: "font-weight:500" }));
        tr.appendChild(el("td", null, [RO ? null : el("button", { type: "button", class: "iconbtn", "aria-label": "Regel verwijderen", title: "Regel verwijderen", text: "×",
          onclick: function () { s.lines.splice(li, 1); render(); markDirty(); } })]));
        tb.appendChild(tr);
      });
      tbl.appendChild(tb);
      sec.appendChild(el("div", { class: "tablewrap" }, [tbl]));
      if (!RO) {
        var dflt = s.lines.length ? s.lines[s.lines.length - 1] : { u: "uur", p: 0, f: 1 };
        sec.appendChild(el("div", { class: "calc-foot" }, [
          el("button", { type: "button", class: "btn sm", onclick: function () { s.lines.push({ d: "", u: dflt.kd ? "post" : dflt.u, q: 0, p: dflt.kd ? 0 : dflt.p, f: dflt.f, kd: false }); render(); var ins = ed.querySelectorAll('tr[data-p="' + pi + '"][data-s="' + si + '"] input'); if (ins.length) ins[ins.length - 4].focus(); } }, ["+ Regel"])
        ]));
      }
      wrap.appendChild(sec);
    });
    if (!RO) {
      var sel = el("select", { style: "width:auto" }, CFG.categories.map(function (c) { return el("option", { value: c[0], text: c[1] }); }).concat([el("option", { value: "overig", text: "Overig" })]));
      wrap.appendChild(el("div", { class: "actions" }, [sel, el("button", { type: "button", class: "btn sm", onclick: function () {
        var maxPos = 0; data.parts.forEach(function (p) { p.sections.forEach(function (s) { maxPos = Math.max(maxPos, N(s.erp_pos)); }); });
        part.sections.push({ category: sel.value, title: CAT_TITLES[sel.value] || "Overig", erp_pos: maxPos + 100, lines: [{ d: "", u: "uur", q: 0, p: 0, f: 1, kd: false }] });
        render(); markDirty(); } }, ["+ Sectie toevoegen"])]));
    }

    // koopdelen
    var kd = el("section", { class: "card" });
    kd.appendChild(el("div", { class: "card-head" }, [el("h2", { text: "Koopdelen specificatie – " + (part.name || "") }), el("b", { "data-kdtot": 1, "data-p": pi })]));
    var kt = el("table", { class: "calc-table" });
    kt.appendChild(el("thead", null, [el("tr", null, [el("th", { text: "Benaming" }), el("th", { text: "Stuksprijs" }), el("th", { text: "Aantal" }), el("th", { text: "Totaal", style: "text-align:right" }), el("th")])]));
    var ktb = el("tbody");
    part.koopdelen.forEach(function (k, ki) {
      ktb.appendChild(el("tr", null, [
        el("td", null, [input(k.d, "", function (v) { k.d = v; })]),
        el("td", null, [input(fmtIn(k.p), "n", function (v) { k.p = N(v); }, { inputmode: "decimal" })]),
        el("td", null, [input(fmtIn(k.q), "n", function (v) { k.q = N(v); }, { inputmode: "decimal" })]),
        el("td", { class: "out", "data-kdline": ki, "data-p": pi }),
        el("td", null, [RO ? null : el("button", { type: "button", class: "iconbtn", text: "×", "aria-label": "Verwijderen", onclick: function () { part.koopdelen.splice(ki, 1); render(); markDirty(); } })])
      ]));
    });
    kt.appendChild(ktb);
    kd.appendChild(el("div", { class: "tablewrap" }, [kt]));
    if (!RO) kd.appendChild(el("div", { style: "margin-top:10px" }, [el("button", { type: "button", class: "btn sm", onclick: function () { part.koopdelen.push({ d: "", p: 0, q: 1 }); render(); } }, ["+ Koopdeel"])]));
    kd.appendChild(el("p", { class: "hint", text: "Het totaal komt automatisch op de regel 'Koopdelen' bij Alle inkopen, inclusief de marge-factor van die regel." }));
    wrap.appendChild(kd);
    return wrap;
  }

  function renderTotals() {
    var wrap = el("section", { class: "card flush tablewrap" });
    var tbl = el("table", { class: "table" });
    var cats = CFG.categories.map(function (c) { return c[0]; });
    tbl.appendChild(el("thead", null, [el("tr", null, [el("th", { text: "Onderdeel" }), el("th", { class: "num", text: "Totaal" })].concat(
      CFG.categories.map(function (c) { return el("th", { class: "num", text: c[1] }); })).concat([el("th", { class: "num", text: "Uren" })]))]));
    var tb = el("tbody"), colTot = cats.map(function () { return 0; }), all = 0, hrs = 0;
    data.parts.forEach(function (p) {
      var pc = partCalc(p); all += pc.total; hrs += pc.hours;
      var cells = [el("td", null, [el("b", { text: p.name })]), el("td", { class: "num", html: "<b>" + eur(pc.total) + "</b>" })];
      cats.forEach(function (c, ci) {
        var v = 0; p.sections.forEach(function (s) { if (s.category === c) v += secCalc(p, s).total; });
        colTot[ci] += v; cells.push(el("td", { class: "num", text: v ? eur(v) : "–" }));
      });
      cells.push(el("td", { class: "num", text: WP.fmt(pc.hours, 2) }));
      tb.appendChild(el("tr", null, cells));
    });
    tbl.appendChild(tb);
    tbl.appendChild(el("tfoot", null, [el("tr", null, [el("td", { text: "Totaalprijs" }), el("td", { class: "num", text: eur(all) })].concat(
      colTot.map(function (v) { return el("td", { class: "num", text: v ? eur(v) : "–" }); })).concat([el("td", { class: "num", text: WP.fmt(hrs, 2) })]))]));
    wrap.appendChild(tbl);
    var pos = el("section", { class: "card" }, [el("h2", { text: "Per ERP-positie (voor de na-calculatie)" })]);
    var rows = {};
    data.parts.forEach(function (p) { p.sections.forEach(function (s) { if (!s.erp_pos) return; var c = secCalc(p, s); var r = rows[s.erp_pos] || (rows[s.erp_pos] = { t: s.title, total: 0, h: 0, part: p.name }); r.total += c.total; r.h += c.hours; }); });
    var pt = el("table", { class: "table" }); pt.appendChild(el("thead", null, [el("tr", null, [el("th", { text: "Pos." }), el("th", { text: "Item" }), el("th", { class: "num", text: "Uren" }), el("th", { class: "num", text: "Incl. marge" })])]));
    var ptb = el("tbody");
    Object.keys(rows).sort(function (a, b) { return a - b; }).forEach(function (k) { var r = rows[k]; ptb.appendChild(el("tr", null, [el("td", { text: k }), el("td", { text: r.t + " · " + r.part }), el("td", { class: "num", text: WP.fmt(r.h, 2) }), el("td", { class: "num", text: eur(r.total) })])); });
    pt.appendChild(ptb); pos.appendChild(el("div", { class: "tablewrap" }, [pt]));
    pos.appendChild(el("p", { class: "hint", text: "Maak in het ERP de items met dezelfde positienummers aan, dan koppelt de na-calculatie automatisch per item." }));
    ed.innerHTML = ""; ed.appendChild(el("div", { class: "grid" }, [wrap, pos]));
  }

  function render() {
    renderTabs();
    if (active === -1) { renderTotals(); }
    else { ed.innerHTML = ""; ed.appendChild(renderPart(active)); }
    refreshTotals();
  }

  function collect() {
    return {
      title: document.getElementById("f_title").value,
      status: document.getElementById("f_status").value,
      customer_id: document.getElementById("f_customer").value || null,
      customer_text: document.getElementById("f_customer_text").value,
      project_id: document.getElementById("f_project").value || null,
      quantity: document.getElementById("f_quantity").value,
      agreed_price: N(document.getElementById("f_agreed").value) || null,
      notes: document.getElementById("f_notes").value,
      parts: data.parts
    };
  }

  function save() {
    if (RO || saving) return;
    clearTimeout(timer);
    saving = true;
    var st = document.getElementById("saveState"); st.textContent = "Opslaan…";
    WP.api(CFG.saveUrl, collect()).then(function () {
      dirty = false; saving = false;
      var d = new Date(); st.textContent = "Opgeslagen " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
      document.getElementById("hdrTitle").textContent = document.getElementById("f_title").value;
    }).catch(function (e) { saving = false; st.textContent = "Opslaan mislukt: " + e.message; });
  }

  ["f_title", "f_status", "f_customer", "f_customer_text", "f_project", "f_quantity", "f_agreed", "f_notes"].forEach(function (id) {
    var e = document.getElementById(id); if (!e) return;
    if (RO) { e.disabled = true; return; }
    e.addEventListener("input", function () { refreshTotals(); markDirty(); });
    e.addEventListener("change", function () { refreshTotals(); markDirty(); });
  });
  var sb = document.getElementById("saveBtn"); if (sb) sb.addEventListener("click", save);
  window.addEventListener("beforeunload", function (e) { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("keydown", function (e) { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save(); } });
  render();
})();
