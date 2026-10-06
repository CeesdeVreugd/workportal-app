/* Rekentools: leidinginhoud, snelheid & debiet, pomp & drukverlies, CIP, verpompen.
   Alles rekent in de browser; opslaan stuurt invoer + uitkomsten naar de server. */
(function () {
  var root = document.getElementById("rt");
  if (!root) return;
  var N = WP.num, F = function (v, d) { return isFinite(v) ? WP.fmt(v, d === undefined ? 2 : d) : "–"; };
  var G = 9.81;

  // DIN 11850 reeks 2 (buitendiameter x wand) en inch-buis (OD), binnendiameter in mm
  var PIPES = [
    { id: "d13", label: "13 × 1,5 (DN10)", od: 13, w: 1.5, g: "DIN 11850 reeks 2" },
    { id: "d19", label: "19 × 1,5 (DN15)", od: 19, w: 1.5, g: "DIN 11850 reeks 2" },
    { id: "d23", label: "23 × 1,5 (DN20)", od: 23, w: 1.5, g: "DIN 11850 reeks 2" },
    { id: "d29", label: "29 × 1,5 (DN25)", od: 29, w: 1.5, g: "DIN 11850 reeks 2" },
    { id: "d35", label: "35 × 1,5 (DN32)", od: 35, w: 1.5, g: "DIN 11850 reeks 2" },
    { id: "d41", label: "41 × 1,5 (DN40)", od: 41, w: 1.5, g: "DIN 11850 reeks 2" },
    { id: "d53", label: "53 × 1,5 (DN50)", od: 53, w: 1.5, g: "DIN 11850 reeks 2" },
    { id: "d70", label: "70 × 2 (DN65)", od: 70, w: 2, g: "DIN 11850 reeks 2" },
    { id: "d85", label: "85 × 2 (DN80)", od: 85, w: 2, g: "DIN 11850 reeks 2" },
    { id: "d104", label: "104 × 2 (DN100)", od: 104, w: 2, g: "DIN 11850 reeks 2" },
    { id: "d129", label: "129 × 2 (DN125)", od: 129, w: 2, g: "DIN 11850 reeks 2" },
    { id: "d154", label: "154 × 2 (DN150)", od: 154, w: 2, g: "DIN 11850 reeks 2" },
    { id: "d204", label: "204 × 2 (DN200)", od: 204, w: 2, g: "DIN 11850 reeks 2" },
    { id: "i05", label: "½\" (12,7 × 1,65)", od: 12.7, w: 1.65, g: "Inch (OD)" },
    { id: "i075", label: "¾\" (19,05 × 1,65)", od: 19.05, w: 1.65, g: "Inch (OD)" },
    { id: "i1", label: "1\" (25,4 × 1,65)", od: 25.4, w: 1.65, g: "Inch (OD)" },
    { id: "i15", label: "1½\" (38,1 × 1,65)", od: 38.1, w: 1.65, g: "Inch (OD)" },
    { id: "i2", label: "2\" (50,8 × 1,65)", od: 50.8, w: 1.65, g: "Inch (OD)" },
    { id: "i25", label: "2½\" (63,5 × 1,65)", od: 63.5, w: 1.65, g: "Inch (OD)" },
    { id: "i3", label: "3\" (76,2 × 1,65)", od: 76.2, w: 1.65, g: "Inch (OD)" },
    { id: "i4", label: "4\" (101,6 × 2,11)", od: 101.6, w: 2.11, g: "Inch (OD)" },
    { id: "eigen", label: "Eigen binnendiameter…", od: 0, w: 0, g: "Anders" }
  ];
  var DIN = PIPES.filter(function (p) { return p.id.charAt(0) === "d"; });
  function pipeById(id) { for (var i = 0; i < PIPES.length; i++) if (PIPES[i].id === id) return PIPES[i]; return PIPES[6]; }
  function idOf(id, own) { var p = pipeById(id); return p.id === "eigen" ? N(own) : p.od - 2 * p.w; }
  function pipeLabel(id, own) { var p = pipeById(id); return p.id === "eigen" ? "ID " + F(N(own), 1) + " mm" : p.label; }

  // Media: dichtheid kg/m³, viscositeit mPa·s (indicatief, aan te passen)
  var MEDIA = [
    { id: "water20", label: "Water 20 °C", rho: 998, mu: 1.0 },
    { id: "water80", label: "Water 80 °C", rho: 972, mu: 0.35 },
    { id: "loog", label: "CIP-loog 1,5 % (75 °C)", rho: 1010, mu: 0.45 },
    { id: "zuur", label: "CIP-zuur 1 % (65 °C)", rho: 1005, mu: 0.5 },
    { id: "sap", label: "Sap (ca. 12 °Brix, 10 °C)", rho: 1048, mu: 2.0 },
    { id: "melk", label: "Melk (10 °C)", rho: 1032, mu: 2.0 },
    { id: "conc", label: "Concentraat (ca. 65 °Brix, 20 °C)", rho: 1320, mu: 500 },
    { id: "beslag", label: "Beslag (viscositeit invullen)", rho: 1100, mu: 5000 },
    { id: "eigen", label: "Eigen medium…", rho: 1000, mu: 1 }
  ];
  // Richtsnelheden m/s per toepassing [min, max]
  var APPS = [
    { id: "cip", label: "CIP / reinigen", min: 1.5, max: 2.5, note: "minimaal 1,5 m/s voor turbulente, reinigende stroming" },
    { id: "dun", label: "Water, sap, melk (persleiding)", min: 1.0, max: 2.5, note: "1–2,5 m/s" },
    { id: "conc", label: "Concentraat", min: 0.5, max: 1.5, note: "0,5–1,5 m/s" },
    { id: "visk", label: "Beslag / viskeus product", min: 0.2, max: 1.0, note: "0,2–1 m/s, rustig verpompen" },
    { id: "zuig", label: "Zuigleiding (dun product)", min: 0.5, max: 1.0, note: "maximaal ca. 1 m/s (cavitatie voorkomen)" }
  ];
  function app(id) { for (var i = 0; i < APPS.length; i++) if (APPS[i].id === id) return APPS[i]; return APPS[0]; }
  function medium(id) { for (var i = 0; i < MEDIA.length; i++) if (MEDIA[i].id === id) return MEDIA[i]; return MEDIA[0]; }

  function area(dmm) { var d = dmm / 1000; return Math.PI * d * d / 4; }                 // m²
  function vel(qm3h, dmm) { return dmm > 0 ? qm3h / 3600 / area(dmm) : NaN; }           // m/s
  function flow(v, dmm) { return v * area(dmm) * 3600; }                                // m³/h
  function reynolds(v, dmm, rho, mu) { return mu > 0 ? rho * v * (dmm / 1000) / (mu / 1000) : NaN; }
  function friction(re, dmm) {
    if (!(re > 0)) return NaN;
    if (re < 2300) return 64 / re;
    var eps = 0.002 / dmm; // ruwheid RVS ca. 0,002 mm
    var f = 0.25 / Math.pow(Math.log10(eps / 3.7 + 5.74 / Math.pow(re, 0.9)), 2);
    if (re < 4000) { var fl = 64 / 2300; return fl + (f - fl) * (re - 2300) / 1700; }
    return f;
  }
  function regime(re) { return !(re > 0) ? "–" : re < 2300 ? "laminair" : re < 4000 ? "overgangsgebied" : "turbulent"; }
  function judge(v, a) {
    if (!(v > 0)) return { cls: "", txt: "" };
    if (v < a.min) return { cls: "warn", txt: "te laag voor " + a.label + " (" + a.note + ")" };
    if (v > a.max) return { cls: "bad", txt: "te hoog voor " + a.label + " (" + a.note + ")" };
    return { cls: "ok", txt: "goed voor " + a.label + " (" + a.note + ")" };
  }

  // ---------- gedeelde keuzelijsten
  function fillPipes(sel) {
    var html = "", grp = "";
    PIPES.forEach(function (p) {
      if (p.g !== grp) { if (grp) html += "</optgroup>"; html += '<optgroup label="' + p.g + '">'; grp = p.g; }
      html += '<option value="' + p.id + '">' + p.label + "</option>";
    });
    sel.innerHTML = html + "</optgroup>";
  }
  root.querySelectorAll("select[data-pipes]").forEach(function (s) { fillPipes(s); s.value = s.getAttribute("data-default") || "d53"; });
  root.querySelectorAll("select[data-media]").forEach(function (s) {
    s.innerHTML = MEDIA.map(function (m) { return '<option value="' + m.id + '">' + m.label + "</option>"; }).join("");
  });
  root.querySelectorAll("select[data-apps]").forEach(function (s) {
    s.innerHTML = APPS.map(function (a) { return '<option value="' + a.id + '">' + a.label + "</option>"; }).join("");
    if (s.getAttribute("data-default")) s.value = s.getAttribute("data-default");
  });
  function bindMedium(prefix) {
    var sel = document.getElementById(prefix + "-med"), rho = document.getElementById(prefix + "-rho"), mu = document.getElementById(prefix + "-mu");
    if (!sel) return;
    sel.addEventListener("change", function () { var m = medium(sel.value); rho.value = F(m.rho, 0); mu.value = F(m.mu, m.mu < 10 ? 2 : 0); calc(); });
    if (!rho.value) { var m = medium(sel.value); rho.value = F(m.rho, 0); mu.value = F(m.mu, 2); }
  }
  function bindOwn(sel) {
    var own = document.getElementById(sel.id + "-own");
    if (!own) return;
    var upd = function () { own.closest("label").hidden = sel.value !== "eigen"; };
    sel.addEventListener("change", upd); upd();
  }
  root.querySelectorAll("select[data-pipes]").forEach(bindOwn);
  ["s", "p"].forEach(bindMedium);
  function val(id) { var e = document.getElementById(id); return e ? e.value : ""; }

  // ---------- 1. leidinginhoud
  var rows = document.getElementById("i-rows");
  function addRow(data) {
    data = data || {};
    var tr = document.createElement("tr");
    tr.innerHTML = '<td><select class="i-pipe"></select><input class="i-own" type="text" inputmode="decimal" placeholder="ID mm" hidden></td>' +
      '<td><input class="i-len" type="text" inputmode="decimal" placeholder="m"></td><td class="num i-vol">–</td>' +
      '<td><button type="button" class="iconbtn i-del" aria-label="Regel verwijderen">×</button></td>';
    rows.appendChild(tr);
    var s = tr.querySelector(".i-pipe"); fillPipes(s); s.value = data.pipe || "d53";
    tr.querySelector(".i-own").value = data.own || ""; tr.querySelector(".i-len").value = data.len || "";
    var own = tr.querySelector(".i-own");
    s.addEventListener("change", function () { own.hidden = s.value !== "eigen"; calc(); }); own.hidden = s.value !== "eigen";
    tr.querySelector(".i-del").addEventListener("click", function () { tr.remove(); calc(); });
  }
  document.getElementById("i-add").addEventListener("click", function () { addRow(); calc(); });

  var out = {};
  function setRes(tab, list, extra) { out[tab] = list; var box = document.getElementById(tab + "-res");
    box.innerHTML = list.map(function (r) { return '<div class="rt-res ' + (r[2] || "") + '"><span>' + r[0] + "</span><b>" + r[1] + "</b></div>"; }).join("") + (extra || ""); }

  function calcInhoud() {
    var tot = 0, n = 0;
    rows.querySelectorAll("tr").forEach(function (tr) {
      var id = idOf(tr.querySelector(".i-pipe").value, tr.querySelector(".i-own").value), L = N(tr.querySelector(".i-len").value);
      var v = area(id) * L * 1000; tr.querySelector(".i-vol").textContent = L > 0 && id > 0 ? F(v, 1) + " l" : "–";
      if (L > 0 && id > 0) { tot += v; n++; }
    });
    var extra = N(val("i-extra")), q = N(val("i-q"));
    var all = tot + extra;
    var res = [["Inhoud leidingen", F(tot, 1) + " liter"], ["Overige inhoud", F(extra, 1) + " liter"], ["Totale inhoud", F(all, 1) + " liter", "big"]];
    if (q > 0) res.push(["Vul-/rondgangtijd bij " + F(q, 1) + " m³/h", F(all / 1000 / q * 60, 1) + " min"]);
    setRes("i", res);
    window.RT_INHOUD = all;
  }

  // ---------- 2. snelheid & debiet
  function calcSnelheid() {
    var d = idOf(val("s-pipe"), val("s-pipe-own")), a = app(val("s-app")), rho = N(val("s-rho")), mu = N(val("s-mu"));
    var mode = (root.querySelector("input[name=s-mode]:checked") || {}).value || "q";
    var q, v;
    if (mode === "q") { q = N(val("s-q")); v = vel(q, d); } else { v = N(val("s-v")); q = flow(v, d); }
    var re = reynolds(v, d, rho, mu), j = judge(v, a);
    setRes("s", [["Binnendiameter", F(d, 1) + " mm"], ["Snelheid", F(v, 2) + " m/s", "big " + j.cls], ["Debiet", F(q, 2) + " m³/h  ·  " + F(q * 1000 / 60, 0) + " l/min"],
      ["Reynoldsgetal", F(re, 0) + " (" + regime(re) + ")"], ["Beoordeling", j.txt || "–", j.cls]]);
    // overzicht alle DIN-maten bij dit debiet
    var tb = DIN.map(function (p) {
      var di = p.od - 2 * p.w, vv = vel(q, di), jj = judge(vv, a);
      return "<tr class='" + (p.id === val("s-pipe") ? "sel" : "") + "'><td>" + p.label + "</td><td class='num'>" + F(di, 0) + "</td><td class='num'><span class='rt-dot " + jj.cls + "'></span>" + F(vv, 2) + "</td></tr>";
    }).join("");
    document.getElementById("s-table").innerHTML = q > 0 ? "<table class='table rt-table'><thead><tr><th>DIN 11850 reeks 2</th><th class='num'>ID mm</th><th class='num'>m/s bij " + F(q, 1) + " m³/h</th></tr></thead><tbody>" + tb + "</tbody></table>" : "";
  }

  // ---------- 3. pomp & drukverlies
  var K = { bocht: 0.4, tdoor: 0.4, taf: 1.3, vlinder: 0.3, zit: 2.5, terug: 2.0 };
  function calcPomp() {
    var d = idOf(val("p-pipe"), val("p-pipe-own")), q = N(val("p-q")), L = N(val("p-len")), rho = N(val("p-rho")) || 1000, mu = N(val("p-mu")) || 1;
    var v = vel(q, d), re = reynolds(v, d, rho, mu), f = friction(re, d), dyn = rho * v * v / 2;
    var ksum = 0; Object.keys(K).forEach(function (k) { ksum += K[k] * N(val("p-" + k)); });
    var dpPipe = f * (L / (d / 1000)) * dyn / 1e5, dpFit = ksum * dyn / 1e5, dpExtra = N(val("p-extra")), dz = N(val("p-dz"));
    var dpTot = dpPipe + dpFit + dpExtra, head = dpTot * 1e5 / (rho * G) + dz;
    var eta = (N(val("p-eta")) || 60) / 100, ph = rho * G * (q / 3600) * head / 1000, pa = ph / eta;
    var motors = [0.37, 0.55, 0.75, 1.1, 1.5, 2.2, 3, 4, 5.5, 7.5, 11, 15, 18.5, 22, 30, 37, 45];
    var mot = motors.filter(function (m) { return m >= pa * 1.15; })[0];
    var warn = re > 0 && re < 2300 && mu > 100 ? "<p class='hint'>Laminaire stroming van een stroperig product: de uitkomst is een indicatie. Bij beslag (niet-Newtons) kan de werkelijke weerstand afwijken; laat de pompleverancier meekijken.</p>" : "";
    setRes("p", [["Snelheid", F(v, 2) + " m/s"], ["Reynoldsgetal", F(re, 0) + " (" + regime(re) + ")"], ["Wrijvingsfactor", F(f, 4)],
      ["Drukverlies leiding", F(dpPipe, 3) + " bar"], ["Drukverlies bochten/kleppen", F(dpFit, 3) + " bar"], ["Extra (wisselaar, filter, sproeibol)", F(dpExtra, 2) + " bar"],
      ["Totaal drukverlies", F(dpTot, 2) + " bar", "big"], ["Benodigde opvoerhoogte", F(head, 1) + " m vloeistofkolom", "big"],
      ["Hydraulisch vermogen", F(ph, 2) + " kW"], ["Asvermogen (η " + F(eta * 100, 0) + " %)", F(pa, 2) + " kW"], ["Motor (incl. 15 % reserve)", mot ? mot.toString().replace(".", ",") + " kW" : "> 45 kW"]], warn);
  }

  // ---------- 4. CIP
  function calcCip() {
    var vol = N(val("c-vol")) + N(val("c-buffer")), d = idOf(val("c-pipe"), val("c-pipe-own")), vmin = N(val("c-vmin")) || 1.5;
    var qPipe = flow(vmin, d), tankD = N(val("c-tank")), qTank = tankD > 0 ? Math.PI * tankD * (N(val("c-lpm")) || 30) * 60 / 1000 : 0;
    var qAdv = Math.max(qPipe, qTank);
    var conc = N(val("c-conc")) / 100, stock = (val("c-stock") || "50|1.52").split("|"), sc = N(stock[0]) / 100, srho = N(stock[1]);
    if (val("c-stock") === "eigen") { sc = N(val("c-sconc")) / 100; srho = N(val("c-srho")) || 1; }
    var lStock = sc > 0 && srho > 0 ? vol * 1.0 * conc / (sc * srho) : NaN;
    var rinse = vol * (N(val("c-rinse")) || 3);
    var res = [["Inhoud CIP-circuit (incl. buffer)", F(vol, 0) + " liter"],
      ["Min. debiet voor " + F(vmin, 1) + " m/s in " + pipeLabel(val("c-pipe"), val("c-pipe-own")), F(qPipe, 1) + " m³/h"]];
    if (qTank > 0) res.push(["Debiet sproeibol tank Ø " + F(tankD, 2) + " m", F(qTank, 1) + " m³/h"]);
    res.push(["Advies CIP-pomp", F(qAdv, 1) + " m³/h (" + F(qAdv * 1000 / 60, 0) + " l/min)", "big"]);
    res.push(["Rondgangtijd", F(vol / 1000 / (qAdv || 1) * 60, 1) + " min"]);
    res.push(["Benodigde voorraadoplossing", F(lStock, 1) + " liter voor " + F(conc * 100, 2) + " %", "big"]);
    res.push(["Spoelwater per spoelstap", F(rinse, 0) + " liter (" + F(N(val("c-rinse")) || 3, 0) + "× circuitinhoud)"]);
    setRes("c", res, "<p class='hint'>Hoeveelheid chemie op basis van gewichtsprocenten, oplossing gerekend als 1 kg/l. Controleer altijd het productblad van de leverancier.</p>");
  }
  document.getElementById("c-take").addEventListener("click", function () { calcInhoud(); document.getElementById("c-vol").value = F(window.RT_INHOUD || 0, 0); calc(); });
  document.getElementById("c-stock").addEventListener("change", function () { document.getElementById("c-own-stock").hidden = this.value !== "eigen"; });

  // ---------- 5. verpompen
  function calcVerpompen() {
    var vol = N(val("v-vol")), mode = (root.querySelector("input[name=v-mode]:checked") || {}).value || "q", d = idOf(val("v-pipe"), val("v-pipe-own"));
    var q, t;
    if (mode === "q") { q = N(val("v-q")); t = q > 0 ? vol / 1000 / q * 60 : NaN; } else { t = N(val("v-t")); q = t > 0 ? vol / 1000 / (t / 60) : NaN; }
    var v = vel(q, d), a = app(val("v-app")), j = judge(v, a);
    setRes("v", [["Tijd", isFinite(t) ? (t >= 60 ? Math.floor(t / 60) + " u " + Math.round(t % 60) + " min" : F(t, 1) + " min") : "–", mode === "q" ? "big" : ""],
      ["Debiet", F(q, 2) + " m³/h  ·  " + F(q * 1000 / 60, 0) + " l/min", mode === "t" ? "big" : ""],
      ["Snelheid in " + pipeLabel(val("v-pipe"), val("v-pipe-own")), F(v, 2) + " m/s", j.cls], ["Beoordeling", j.txt || "–", j.cls]]);
  }

  // ---------- tabs, rekenen, invoer bewaren
  var tabs = root.querySelectorAll("[data-tab]"), current = root.getAttribute("data-start") || "i";
  function show(t) {
    current = t;
    tabs.forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-tab") === t); });
    root.querySelectorAll(".rt-pane").forEach(function (p) { p.hidden = p.id !== "pane-" + t; });
    document.getElementById("rt-kind").value = t;
    calc();
  }
  tabs.forEach(function (b) { b.addEventListener("click", function () { show(b.getAttribute("data-tab")); history.replaceState(null, "", "?tab=" + b.getAttribute("data-tab")); }); });
  function calc() {
    try { ({ i: calcInhoud, s: calcSnelheid, p: calcPomp, c: calcCip, v: calcVerpompen })[current](); } catch (e) { console.error(e); }
  }
  root.addEventListener("input", calc); root.addEventListener("change", calc);

  function inputs() {
    var o = {};
    document.getElementById("pane-" + current).querySelectorAll("input[id], select[id]").forEach(function (e) {
      if (e.type === "radio") return; o[e.id] = e.value;
    });
    document.getElementById("pane-" + current).querySelectorAll("input[type=radio]:checked").forEach(function (e) { o["radio:" + e.name] = e.value; });
    if (current === "i") o.rows = Array.prototype.map.call(rows.querySelectorAll("tr"), function (tr) {
      return { pipe: tr.querySelector(".i-pipe").value, own: tr.querySelector(".i-own").value, len: tr.querySelector(".i-len").value };
    });
    return o;
  }
  function restore(kind, o) {
    if (kind === "i") { rows.innerHTML = ""; (o.rows || []).forEach(addRow); }
    Object.keys(o).forEach(function (k) {
      if (k === "rows") return;
      if (k.indexOf("radio:") === 0) { var r = root.querySelector("input[name='" + k.slice(6) + "'][value='" + o[k] + "']"); if (r) r.checked = true; return; }
      var e = document.getElementById(k); if (e) { e.value = o[k]; e.dispatchEvent(new Event("change")); }
    });
  }
  // opslaan
  var form = document.getElementById("rt-save");
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    calc();
    var lines = current === "i" ? Array.prototype.map.call(rows.querySelectorAll("tr"), function (tr) {
      return [pipeLabel(tr.querySelector(".i-pipe").value, tr.querySelector(".i-own").value), F(N(tr.querySelector(".i-len").value), 2) + " m", tr.querySelector(".i-vol").textContent];
    }).filter(function (l) { return l[2] !== "–"; }) : [];
    var body = { id: form.elements.id.value || null, kind: current, title: form.elements.title.value, inputs: inputs(),
      results: (out[current] || []).map(function (r) { return [r[0], r[1]]; }), lines: lines,
      inputs_text: describeInputs(), link: form.elements.link.value };
    var st = document.getElementById("rt-status");
    st.textContent = "Opslaan…";
    WP.api(form.getAttribute("action"), body).then(function (js) {
      form.elements.id.value = js.id;
      st.innerHTML = 'Opgeslagen. <a href="' + js.pdf + '" data-pdf="' + (form.elements.title.value || "Berekening") + '">PDF bekijken</a>' + (js.link_url ? ' · <a href="' + js.link_url + '">naar ' + js.link_label + "</a>" : "");
    }).catch(function (err) { st.textContent = err.message; });
  });
  document.getElementById("rt-new").addEventListener("click", function () { form.elements.id.value = ""; document.getElementById("rt-status").textContent = "Wordt als nieuwe berekening opgeslagen."; });
  function describeInputs() {
    var list = [];
    document.getElementById("pane-" + current).querySelectorAll("label.f").forEach(function (l) {
      if (l.hidden || l.closest("[hidden]")) return;
      var e = l.querySelector("input:not([type=radio]):not([type=checkbox]), select"); if (!e || e.value === "") return;
      var name = (l.firstChild && l.firstChild.nodeType === 3 ? l.firstChild.nodeValue : l.textContent).trim();
      var v = e.tagName === "SELECT" ? e.options[e.selectedIndex].text : e.value;
      list.push([name, v]);
    });
    root.querySelectorAll("#pane-" + current + " input[type=radio]:checked").forEach(function (r) { list.push(["Keuze", r.closest("label").textContent.trim()]); });
    return list;
  }

  var saved = window.RT_SAVED;
  if (saved && saved.kind) { current = saved.kind; restore(saved.kind, saved.inputs || {}); form.elements.id.value = saved.id; form.elements.title.value = saved.title || ""; }
  if (!rows.children.length) { addRow({ pipe: "d53" }); addRow({ pipe: "d41" }); }
  show(current);
})();
