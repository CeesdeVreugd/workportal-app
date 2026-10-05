/* WorkPortal – algemene scripts */
(function () {
  "use strict";
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  window.WP = window.WP || {};
  WP.csrf = CSRF;

  WP.api = function (url, data, method) {
    return fetch(url, {
      method: method || "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
      body: data === undefined ? undefined : JSON.stringify(data)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (js) {
        if (!r.ok) { throw new Error(js.error || ("Fout " + r.status)); }
        return js;
      });
    });
  };

  WP.fmt = function (v, dec) {
    dec = dec === undefined ? 2 : dec;
    var n = Number(v || 0);
    return n.toLocaleString("nl-NL", { minimumFractionDigits: dec, maximumFractionDigits: dec });
  };
  WP.eur = function (v, dec) {
    var n = Number(v || 0);
    return (n < 0 ? "− " : "") + "€ " + WP.fmt(Math.abs(n), dec === undefined ? 2 : dec);
  };
  WP.num = function (s) {
    if (typeof s === "number") return s;
    s = String(s || "").trim().replace(/\s/g, "").replace("€", "");
    if (!s) return 0;
    if (s.indexOf(",") >= 0) s = s.replace(/\./g, "").replace(",", ".");
    var n = parseFloat(s);
    return isNaN(n) ? 0 : n;
  };

  // Klikbare tabelregels
  document.addEventListener("click", function (e) {
    var tr = e.target.closest("tr[data-href]");
    if (tr && !e.target.closest("a,button,input,select,label,td.chk")) { window.location = tr.getAttribute("data-href"); }
  });

  // Bevestigen
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    var btn = e.target.querySelector("button[type=submit],button:not([type])");
    if (btn && !e.target.hasAttribute("data-no-disable")) { setTimeout(function () { btn.disabled = true; }, 0); }
  });

  // Foto-invoer: direct versturen of voorbeeld tonen
  document.querySelectorAll("input[type=file][data-autosubmit]").forEach(function (inp) {
    inp.addEventListener("change", function () { if (inp.files.length) inp.form.submit(); });
  });
  document.querySelectorAll("input[type=file][data-preview]").forEach(function (inp) {
    var box = document.getElementById(inp.getAttribute("data-preview"));
    inp.addEventListener("change", function () {
      if (!box) return;
      box.innerHTML = "";
      Array.prototype.forEach.call(inp.files, function (f) {
        var d = document.createElement("div"); d.className = "photo";
        if (f.type.indexOf("image/") === 0) {
          var img = document.createElement("img"); img.src = URL.createObjectURL(f); d.appendChild(img);
        } else { d.textContent = f.name; d.style.padding = "8px"; d.style.fontSize = "12px"; }
        box.appendChild(d);
      });
    });
  });

  // Handtekeningveld
  document.querySelectorAll("canvas.sigpad").forEach(function (cv) {
    var target = document.getElementById(cv.getAttribute("data-target"));
    var ctx = cv.getContext("2d"), drawing = false, dirty = false;
    function size() {
      var r = cv.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
      cv.width = r.width * ratio; cv.height = r.height * ratio;
      ctx.scale(ratio, ratio); ctx.lineWidth = 2.4; ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.strokeStyle = "#0A0A96";
    }
    size();
    function pos(e) { var r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
    cv.addEventListener("pointerdown", function (e) { drawing = true; cv.setPointerCapture(e.pointerId); var p = pos(e); ctx.beginPath(); ctx.moveTo(p.x, p.y); });
    cv.addEventListener("pointermove", function (e) { if (!drawing) return; var p = pos(e); ctx.lineTo(p.x, p.y); ctx.stroke(); dirty = true; });
    function end() { if (!drawing) return; drawing = false; if (target && dirty) target.value = cv.toDataURL("image/png"); }
    cv.addEventListener("pointerup", end); cv.addEventListener("pointercancel", end); cv.addEventListener("pointerleave", end);
    var clr = document.querySelector('[data-clear="' + cv.id + '"]');
    if (clr) clr.addEventListener("click", function () { ctx.clearRect(0, 0, cv.width, cv.height); dirty = false; if (target) target.value = ""; });
  });

  // Aftellende timers
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    document.querySelectorAll("[data-countdown]").forEach(function (el) {
      var due = new Date(el.getAttribute("data-countdown")).getTime();
      var start = el.getAttribute("data-start") ? new Date(el.getAttribute("data-start")).getTime() : null;
      var diff = Math.round((due - Date.now()) / 1000);
      var box = el.closest(".timer");
      if (diff <= 0) {
        el.textContent = el.getAttribute("data-due-text") || "Nu controleren";
        if (box) box.classList.add("due");
        if (!el.dataset.alerted && el.hasAttribute("data-alert")) {
          el.dataset.alerted = "1";
          try { if (navigator.vibrate) navigator.vibrate([300, 150, 300]); } catch (x) {}
          if ("Notification" in window && Notification.permission === "granted") {
            try { new Notification("Druktest: tijd voor controle", { body: el.getAttribute("data-alert"), icon: "/static/img/wp-round-192.png" }); } catch (x) {}
          }
        }
      } else {
        var h = Math.floor(diff / 3600), m = Math.floor((diff % 3600) / 60), s = diff % 60;
        el.textContent = (h ? h + ":" : "") + pad(m) + ":" + pad(s);
        if (box) box.classList.remove("due");
      }
      var bar = box ? box.querySelector(".bar div") : null;
      if (bar && start) {
        var pct = Math.min(100, Math.max(0, (Date.now() - start) / (due - start) * 100));
        bar.style.width = pct + "%";
      }
    });
  }
  if (document.querySelector("[data-countdown]")) { tick(); setInterval(tick, 1000); }

  // Service worker (PWA + pushmeldingen)
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
  }

  function urlB64ToUint8Array(base64String) {
    var padding = "=".repeat((4 - base64String.length % 4) % 4);
    var base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = window.atob(base64), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; ++i) out[i] = raw.charCodeAt(i);
    return out;
  }

  WP.enablePush = function (statusEl) {
    function say(t) { if (statusEl) statusEl.textContent = t; }
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      say("Dit apparaat of deze browser ondersteunt geen pushmeldingen. Op iPhone: zet WorkPortal eerst op je beginscherm (Deel → Zet op beginscherm) en open hem daarvandaan.");
      return;
    }
    Notification.requestPermission().then(function (perm) {
      if (perm !== "granted") { say("Meldingen zijn geweigerd. Zet ze aan in de instellingen van je browser."); return; }
      return fetch("/api/push/key", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (js) {
        return navigator.serviceWorker.ready.then(function (reg) {
          return reg.pushManager.getSubscription().then(function (sub) {
            return sub || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToUint8Array(js.key) });
          });
        });
      }).then(function (sub) {
        return WP.api("/api/push/subscribe", sub.toJSON());
      }).then(function () { say("Meldingen staan aan op dit apparaat."); });
    }).catch(function (err) { say("Aanzetten mislukt: " + err.message); });
  };
  WP.testPush = function (statusEl) {
    WP.api("/api/push/test", {}).then(function (js) {
      if (statusEl) statusEl.textContent = js.sent ? "Testmelding verstuurd." : "Geen actief apparaat gevonden. Zet eerst meldingen aan.";
    }).catch(function (e) { if (statusEl) statusEl.textContent = e.message; });
  };
})();

/* Afhankelijke keuzelijsten: <select data-filter-by="customer_id"> met <option data-c="..."> */
(function () {
  document.querySelectorAll("select[data-filter-by]").forEach(function (sel) {
    var src = document.querySelector('[name="' + sel.getAttribute("data-filter-by") + '"]');
    if (!src) return;
    function apply(initial) {
      var v = src.value;
      Array.prototype.forEach.call(sel.options, function (o) {
        var c = o.getAttribute("data-c");
        var show = !c || !v || c === v;
        o.hidden = !show; o.disabled = !show;
      });
      if (!initial && sel.selectedOptions.length && sel.selectedOptions[0].disabled) sel.value = "";
    }
    src.addEventListener("change", function () { apply(false); });
    apply(true);
  });
})();

/* Meldingvenster bij machine/relatie (meenemen / let op) */
(function () {
  var dlg = document.getElementById("alertdlg");
  if (!dlg || typeof dlg.showModal !== "function") return;
  var key = "wp-alert-" + dlg.getAttribute("data-key");
  var seen = false;
  try { seen = sessionStorage.getItem(key) === "1"; } catch (e) {}
  function open() { dlg.showModal(); }
  function close() { try { sessionStorage.setItem(key, "1"); } catch (e) {} dlg.close(); }
  if (dlg.getAttribute("data-auto") === "1" && !seen) open();
  document.querySelectorAll("[data-open-alert]").forEach(function (b) { b.addEventListener("click", open); });
  dlg.querySelectorAll("[data-close]").forEach(function (b) { b.addEventListener("click", close); });
  var go = dlg.querySelector("[data-go]"), boxes = dlg.querySelectorAll("input[name=item]");
  function check() { if (!go) return; var all = true; boxes.forEach(function (b) { if (!b.checked) all = false; }); go.disabled = !all; }
  boxes.forEach(function (b) { b.addEventListener("change", check); });
  check();
  dlg.querySelector("form").addEventListener("submit", function () { try { sessionStorage.setItem(key, "1"); } catch (e) {} });
})();

/* Projectmap (SharePoint) laden op de projectpagina */
(function () {
  var box = document.getElementById("spfolder");
  if (!box) return;
  fetch(box.getAttribute("data-src"), { credentials: "same-origin" })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
    .then(function (html) { box.innerHTML = html; })
    .catch(function () { box.innerHTML = '<div class="empty">SharePoint is op dit moment niet bereikbaar.</div>'; });
})();

/* Bulk omzetten projecten <-> orders */
(function () {
  var form = document.getElementById("bulkform"), bar = document.getElementById("bulkbar");
  if (!form || !bar) return;
  var boxes = function () { return form.querySelectorAll('input[name="ids"]'); };
  function update() {
    var n = form.querySelectorAll('input[name="ids"]:checked').length;
    document.getElementById("bulkn").textContent = n;
    bar.classList.toggle("hidden", n === 0);
  }
  form.addEventListener("change", function (e) {
    if (e.target.hasAttribute("data-bulk-all")) boxes().forEach(function (b) { b.checked = e.target.checked; });
    update();
  });
  form.addEventListener("click", function (e) {
    var td = e.target.closest("td.chk");
    if (td && e.target.tagName !== "INPUT") { var b = td.querySelector("input"); b.checked = !b.checked; update(); }
    if (e.target.closest("[data-bulk-clear]")) {
      boxes().forEach(function (b) { b.checked = false; });
      var all = form.querySelector("[data-bulk-all]"); if (all) all.checked = false;
      update();
    }
  });
})();

/* Blokken die hun inhoud los laden (bijv. 3D-modellen uit SharePoint) */
(function () {
  document.querySelectorAll("[data-partial]").forEach(function (box) {
    fetch(box.getAttribute("data-partial"), { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) { box.innerHTML = html; })
      .catch(function () { box.innerHTML = '<div class="empty">Kon de lijst niet laden.</div>'; });
  });
  /* 3D-upload: te groot bestand meteen melden */
  document.querySelectorAll("form.m3dform").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      var max = (parseInt(f.getAttribute("data-maxmb"), 10) || 60) * 1024 * 1024, msg = f.querySelector(".m3dmsg");
      var files = f.querySelector('input[type=file]').files, total = 0, big = [];
      for (var i = 0; i < files.length; i++) { total += files[i].size; if (files[i].size > max) big.push(files[i].name); }
      if (big.length || total > max) {
        e.preventDefault();
        msg.hidden = false; msg.style.color = "var(--bad)";
        msg.textContent = big.length ? big.join(", ") + " is groter dan 60 MB. Exporteer de STEP opnieuw met minder detail, of zet hem direct in de map 1 Tekeningen in SharePoint."
          : "Samen groter dan 60 MB. Upload de bestanden één voor één.";
        return;
      }
      var btn = f.querySelector("button"); btn.disabled = true; btn.textContent = "Bezig met uploaden…";
    });
  });

  // Bestandskiezer altijd in het Nederlands (de standaardknop volgt de taal van de browser)
  document.querySelectorAll('input[type="file"]').forEach(function (inp) {
    if (inp.hidden || inp.closest(".addphoto")) return;
    var wrapEl = document.createElement("span"); wrapEl.className = "filepick";
    var b = document.createElement("span"); b.className = "btn sm"; b.textContent = inp.multiple ? "Bestanden kiezen" : "Bestand kiezen";
    var n = document.createElement("span"); n.className = "fp-name muted small"; n.textContent = "Geen bestand gekozen";
    inp.parentNode.insertBefore(wrapEl, inp); wrapEl.appendChild(inp); wrapEl.appendChild(b); wrapEl.appendChild(n);
    inp.classList.add("fp-native");
    inp.addEventListener("invalid", function () { inp.setCustomValidity(inp.multiple ? "Kies één of meer bestanden." : "Kies een bestand."); });
    inp.addEventListener("change", function () {
      inp.setCustomValidity("");
      var c = inp.files ? inp.files.length : 0;
      n.textContent = c === 0 ? "Geen bestand gekozen" : c === 1 ? inp.files[0].name : c + " bestanden gekozen";
    });
    if (inp.closest("form")) inp.closest("form").addEventListener("reset", function () { setTimeout(function () { n.textContent = "Geen bestand gekozen"; }); });
  });

  // PDF's openen in de WorkPortal-viewer met terug-knop (links met data-pdf)
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[data-pdf]");
    if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return;
    var href = a.getAttribute("href") || "";
    if (href.charAt(0) !== "/" || /[?&]download=1/.test(href)) return;
    e.preventDefault();
    var title = a.getAttribute("data-pdf") || a.textContent.trim() || "PDF";
    location.href = "/pdf?src=" + encodeURIComponent(href) + "&terug=" + encodeURIComponent(location.pathname + location.search + location.hash) +
      "&titel=" + encodeURIComponent(title);
  });
})();

/* Zoekbare keuzelijst: <select data-combo [data-filter-by="customer_id"] [data-add="machine"]>
   - typen om te zoeken; alleen opties van de gekozen klant (data-c) als data-filter-by gezet is
   - "+ nieuw toevoegen" opent een klein venster en maakt het item direct aan (data-add) */
(function () {
  var ADD = {
    klant: { title: "Nieuwe klant", label: "klant", fields: [["name", "Naam *", "text", true], ["city", "Plaats", "text"], ["email", "E-mail", "email"], ["phone", "Telefoon", "tel"]] },
    locatie: { title: "Nieuwe locatie", label: "locatie", fields: [["name", "Naam *", "text", true], ["address", "Adres", "text"], ["postcode", "Postcode", "text"], ["city", "Plaats", "text"]] },
    machine: { title: "Nieuwe machine / installatie", label: "machine", fields: [["name", "Naam *", "text", true], ["serial", "Serienummer", "text"], ["year", "Bouwjaar", "text"]] },
    contact: { title: "Nieuwe contactpersoon", label: "contactpersoon", fields: [["name", "Naam *", "text", true], ["function", "Functie", "text"], ["phone", "Telefoon", "tel"], ["email", "E-mail", "email"]] }
  };
  function norm(s) { return (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, ""); }
  function esc(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

  function openAdd(kind, text, sel, parentVal, done) {
    var cfg = ADD[kind]; if (!cfg) return;
    var dlg = document.createElement("dialog");
    dlg.className = "combo-dlg";
    var html = '<form method="dialog" class="form"><h2>' + esc(cfg.title) + '</h2><div class="fgrid">';
    cfg.fields.forEach(function (f) {
      html += '<label class="f' + (f[0] === "name" ? " full" : "") + '">' + esc(f[1]) + '<input type="' + f[2] + '" name="' + f[0] + '"' + (f[3] ? " required" : "") + '></label>';
    });
    html += '</div>' + (kind === "locatie" ? '<div data-addr-search></div>' : '') + '<p class="hint err" hidden></p><div class="actions"><button class="btn primary" value="ok">Toevoegen</button><button class="btn ghost" value="cancel" formnovalidate>Annuleren</button></div></form>';
    dlg.innerHTML = html;
    document.body.appendChild(dlg);
    var form = dlg.querySelector("form");
    form.elements.name.value = text || "";
    var ab = dlg.querySelector("[data-addr-search]"); if (ab && WP.addrSearch) WP.addrSearch(ab);
    form.addEventListener("submit", function (ev) {
      if (ev.submitter && ev.submitter.value === "cancel") return;
      ev.preventDefault();
      var data = { customer_id: parentVal || null };
      var loc = document.querySelector('[name="location_id"]');
      if (kind === "machine" && loc && loc.value) data.location_id = loc.value;
      cfg.fields.forEach(function (f) { data[f[0]] = form.elements[f[0]].value; });
      var btn = form.querySelector('button[value="ok"]'); btn.disabled = true;
      WP.api("/klanten/api/snel/" + kind, data).then(function (js) {
        dlg.close(); dlg.remove(); done(js);
      }).catch(function (e) {
        btn.disabled = false; var p = form.querySelector(".err"); p.hidden = false; p.textContent = e.message;
      });
    });
    dlg.addEventListener("close", function () { setTimeout(function () { if (dlg.parentNode) dlg.remove(); }, 0); });
    dlg.showModal();
    setTimeout(function () { form.elements.name.focus(); }, 30);
  }

  function enhance(sel) {
    if (sel.dataset.comboReady) return; sel.dataset.comboReady = "1";
    var parentName = sel.getAttribute("data-filter-by");
    var src = parentName ? document.querySelector('[name="' + parentName + '"]') : null;
    var kind = sel.getAttribute("data-add");
    var wrap = document.createElement("div"); wrap.className = "combo";
    var inp = document.createElement("input"); inp.type = "text"; inp.autocomplete = "off"; inp.className = "combo-input";
    inp.setAttribute("role", "combobox"); inp.setAttribute("aria-expanded", "false");
    var clear = document.createElement("button"); clear.type = "button"; clear.className = "combo-clear"; clear.setAttribute("aria-label", "Leegmaken"); clear.innerHTML = "&times;";
    var list = document.createElement("ul"); list.className = "combo-list"; list.setAttribute("role", "listbox"); list.hidden = true;
    sel.parentNode.insertBefore(wrap, sel); wrap.appendChild(sel); wrap.appendChild(inp); wrap.appendChild(clear); wrap.appendChild(list);
    sel.classList.add("combo-native"); sel.tabIndex = -1;
    var active = -1, items = [];

    function parentVal() { return src ? src.value : ""; }
    function selectable() {
      var pv = parentVal();
      return Array.prototype.filter.call(sel.options, function (o) {
        if (!o.value) return false;
        var c = o.getAttribute("data-c");
        if (!src) return true;
        if (!pv) return !sel.hasAttribute("data-need");
        return !c || c === pv;
      });
    }
    function syncText() {
      var o = sel.selectedOptions[0];
      inp.value = o && o.value ? o.textContent.trim() : "";
      clear.hidden = !inp.value;
      var need = src && sel.hasAttribute("data-need") && !parentVal();
      inp.disabled = !!need || sel.disabled;
      inp.placeholder = need ? "Kies eerst een klant" : (sel.getAttribute("data-placeholder") || "Typ om te zoeken…");
    }
    function render(q) {
      var words = norm(q).split(/\s+/).filter(Boolean);
      items = selectable().filter(function (o) {
        var t = norm(o.textContent + " " + (o.getAttribute("data-s") || ""));
        return words.every(function (w) { return t.indexOf(w) >= 0; });
      }).slice(0, 60);
      var html = items.map(function (o, i) {
        return '<li role="option" data-i="' + i + '" class="' + (o.selected ? "sel" : "") + '">' + esc(o.textContent.trim()) + "</li>";
      }).join("");
      if (!items.length) html = '<li class="combo-empty">Niets gevonden</li>';
      if (kind && ADD[kind]) html += '<li class="combo-add" data-add="1">+ ' + (q ? "“" + esc(q) + "” toevoegen als nieuwe " : "Nieuwe ") + esc(ADD[kind].label) + (q ? "" : " toevoegen") + "</li>";
      list.innerHTML = html; active = -1; list.hidden = false; inp.setAttribute("aria-expanded", "true");
    }
    function close() { list.hidden = true; inp.setAttribute("aria-expanded", "false"); syncText(); }
    function choose(o) {
      sel.value = o ? o.value : "";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      close();
    }
    function add(q) {
      list.hidden = true;
      openAdd(kind, q, sel, parentVal(), function (js) {
        var o = document.createElement("option");
        o.value = js.id; o.textContent = js.label;
        if (src && js.customer_id) o.setAttribute("data-c", js.customer_id);
        sel.appendChild(o);
        choose(o);
      });
    }
    inp.addEventListener("focus", function () { inp.select(); render(""); });
    inp.addEventListener("input", function () { render(inp.value); });
    inp.addEventListener("keydown", function (e) {
      var lis = list.querySelectorAll("li[data-i], li.combo-add");
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault(); if (list.hidden) render(inp.value);
        active = Math.max(0, Math.min(lis.length - 1, active + (e.key === "ArrowDown" ? 1 : -1)));
        lis.forEach(function (li, i) { li.classList.toggle("act", i === active); });
        if (lis[active]) lis[active].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        if (!list.hidden) { e.preventDefault(); var li = lis[active] || (items.length && inp.value.trim() ? lis[0] : null); if (li) li.dispatchEvent(new Event("mousedown", { bubbles: true })); }
      } else if (e.key === "Escape") { close(); }
    });
    inp.addEventListener("blur", function () { setTimeout(close, 150); });
    list.addEventListener("mousedown", function (e) {
      var li = e.target.closest("li"); if (!li) return; e.preventDefault();
      if (li.getAttribute("data-add")) { add(inp.value.trim() && !items.some(function (o) { return norm(o.textContent.trim()) === norm(inp.value.trim()); }) ? inp.value.trim() : ""); return; }
      if (li.hasAttribute("data-i")) choose(items[+li.getAttribute("data-i")]);
    });
    clear.addEventListener("click", function () { choose(null); inp.focus(); });
    sel.addEventListener("change", syncText);
    if (src) src.addEventListener("change", function () {
      var o = sel.selectedOptions[0], c = o && o.getAttribute("data-c");
      if (o && o.value && c && c !== src.value) { sel.value = ""; sel.dispatchEvent(new Event("change", { bubbles: true })); }
      syncText();
    });
    syncText();
  }
  document.querySelectorAll("select[data-combo]").forEach(enhance);
})();

/* Adres zoeken (PDOK Locatieserver, Nederlandse adressen): <div data-addr-search></div> in een formulier
   met velden address, postcode en city. Typ postcode + huisnummer of straat + plaats en klik Zoek adres. */
(function () {
  var BASE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/";
  function esc(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
  WP.addrSearch = function (box) {
    if (!box || box.dataset.ready) return; box.dataset.ready = "1";
    var form = box.closest("form");
    box.classList.add("addr-search");
    box.innerHTML = '<label class="f">Adres zoeken<span class="addr-row"><input type="search" placeholder="Postcode + huisnummer, of straat en plaats" autocomplete="off">' +
      '<button type="button" class="btn sm">Zoek adres</button></span></label><ul class="combo-list" hidden></ul><p class="hint" hidden></p>';
    var inp = box.querySelector("input"), btn = box.querySelector("button"), list = box.querySelector("ul"), hint = box.querySelector(".hint");
    var f = function (n) { return form && form.querySelector('[name="' + n + '"]'); };
    inp.value = [f("address") && f("address").value, f("postcode") && f("postcode").value, f("city") && f("city").value].filter(Boolean).join(" ");
    function say(t) { hint.hidden = !t; hint.textContent = t || ""; }
    function search() {
      var q = inp.value.trim(); if (!q) { say("Typ eerst een postcode en huisnummer, of een straat en plaats."); return; }
      say("Zoeken…"); list.hidden = true;
      fetch(BASE + "suggest?rows=8&fq=type:adres&q=" + encodeURIComponent(q)).then(function (r) { return r.json(); }).then(function (js) {
        var docs = (js.response && js.response.docs) || [];
        if (!docs.length) { say("Geen adres gevonden. Probeer postcode + huisnummer."); return; }
        say("");
        list.innerHTML = docs.map(function (d) { return '<li data-id="' + esc(d.id) + '">' + esc(d.weergavenaam) + "</li>"; }).join("");
        list.hidden = false;
      }).catch(function () { say("Adres zoeken lukt nu niet (geen verbinding met de adressendienst)."); });
    }
    btn.addEventListener("click", search);
    inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); search(); } });
    list.addEventListener("mousedown", function (e) {
      var li = e.target.closest("li[data-id]"); if (!li) return; e.preventDefault();
      fetch(BASE + "lookup?fl=straatnaam,huisnummer,huisletter,huisnummertoevoeging,postcode,woonplaatsnaam&id=" + encodeURIComponent(li.getAttribute("data-id")))
        .then(function (r) { return r.json(); }).then(function (js) {
          var d = (js.response && js.response.docs && js.response.docs[0]) || {};
          var nr = [d.huisnummer, d.huisletter].filter(Boolean).join("") + (d.huisnummertoevoeging ? "-" + d.huisnummertoevoeging : "");
          if (f("address")) f("address").value = [d.straatnaam, nr].filter(Boolean).join(" ");
          if (f("postcode")) f("postcode").value = d.postcode || "";
          if (f("city")) f("city").value = d.woonplaatsnaam || "";
          inp.value = li.textContent; list.hidden = true; say("Adres ingevuld.");
        }).catch(function () { say("Adres ophalen mislukt."); });
    });
    document.addEventListener("mousedown", function (e) { if (!box.contains(e.target)) list.hidden = true; });
  };
  document.querySelectorAll("[data-addr-search]").forEach(WP.addrSearch);
})();

/* Slepen en neerzetten in een SharePoint-map (projectmap/ordermap) */
(function () {
  var zone = document.getElementById("dropzone");
  if (!zone) return;
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  var url = zone.getAttribute("data-url"), pad = zone.getAttribute("data-pad") || "", listUrl = zone.getAttribute("data-list");
  var queue = document.getElementById("dropqueue"), where = document.getElementById("dropwhere"), depth = 0;
  function esc(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
  function target(el) { var r = el && el.closest && el.closest("[data-droppath]"); return r; }
  function hasFiles(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0; }
  function clearRows() { zone.querySelectorAll(".droptarget").forEach(function (r) { r.classList.remove("droptarget"); }); }
  zone.addEventListener("dragenter", function (e) { if (!hasFiles(e)) return; e.preventDefault(); depth++; zone.classList.add("over"); });
  zone.addEventListener("dragleave", function (e) { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) { zone.classList.remove("over"); clearRows(); } });
  zone.addEventListener("dragover", function (e) {
    if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy";
    var r = target(e.target); clearRows();
    if (r) { r.classList.add("droptarget"); where.textContent = "in map " + r.getAttribute("data-dropname"); }
    else where.textContent = "in deze map";
  });
  zone.addEventListener("drop", function (e) {
    if (!hasFiles(e)) return; e.preventDefault(); depth = 0; zone.classList.remove("over");
    var r = target(e.target); clearRows();
    send(Array.prototype.slice.call(e.dataTransfer.files), r ? r.getAttribute("data-droppath") : pad, r ? r.getAttribute("data-dropname") : null);
  });
  var inp = document.getElementById("dropinput");
  if (inp) inp.addEventListener("change", function () { send(Array.prototype.slice.call(inp.files), pad, null); inp.value = ""; });
  // voorkom dat de browser een losgelaten bestand buiten het vak opent
  window.addEventListener("dragover", function (e) { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", function (e) { if (hasFiles(e) && !zone.contains(e.target)) e.preventDefault(); });

  function send(files, dest, destName) {
    files = files.filter(function (f) { return f.size > 0 || f.type; });
    if (!files.length) { queue.hidden = false; queue.innerHTML = '<div class="dq-done">Mappen kun je niet slepen, alleen bestanden.</div>'; return; }
    queue.hidden = false;
    var i = 0, ok = 0, failed = 0;
    function next() {
      if (i >= files.length) {
        var msg = ok + " bestand" + (ok === 1 ? "" : "en") + " toegevoegd" + (destName ? " aan " + destName : "") + (failed ? ", " + failed + " mislukt" : "") + ".";
        queue.insertAdjacentHTML("beforeend", '<div class="dq-done">' + esc(msg) + "</div>");
        refresh(); setTimeout(function () { if (!failed) { queue.hidden = true; queue.innerHTML = ""; } }, 4000);
        return;
      }
      var f = files[i++];
      var row = document.createElement("div"); row.className = "dq-row";
      row.innerHTML = '<span class="dq-name">' + esc(f.name) + '</span><span class="dq-bar"><i></i></span><span class="dq-st">0%</span>';
      queue.appendChild(row);
      var fd = new FormData(); fd.append("files", f, f.name); fd.append("pad", dest || ""); fd.append("csrf_token", CSRF);
      var xhr = new XMLHttpRequest(); xhr.open("POST", url); xhr.setRequestHeader("X-CSRF-Token", CSRF);
      xhr.upload.onprogress = function (ev) { if (ev.lengthComputable) { var p = Math.round(ev.loaded / ev.total * 95); row.querySelector("i").style.width = p + "%"; row.querySelector(".dq-st").textContent = p + "%"; } };
      xhr.onload = function () {
        var js = {}; try { js = JSON.parse(xhr.responseText); } catch (e) {}
        if (xhr.status === 200 && js.ok) { ok++; row.classList.add("ok"); row.querySelector("i").style.width = "100%"; row.querySelector(".dq-st").textContent = js.saved && js.saved[0] !== f.name ? "opgeslagen als " + js.saved[0] : "klaar"; }
        else { failed++; row.classList.add("err"); row.querySelector(".dq-st").textContent = xhr.status === 413 ? "te groot" : (js.error || "mislukt"); }
        next();
      };
      xhr.onerror = function () { failed++; row.classList.add("err"); row.querySelector(".dq-st").textContent = "geen verbinding"; next(); };
      xhr.send(fd);
    }
    next();
  }
  function refresh() {
    fetch(listUrl, { credentials: "same-origin" }).then(function (r) { return r.text(); }).then(function (h) {
      document.getElementById("droplist").innerHTML = h;
    }).catch(function () {});
  }
})();
