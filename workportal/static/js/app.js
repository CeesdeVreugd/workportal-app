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
    if (tr && !e.target.closest("a,button,input,select,label")) { window.location = tr.getAttribute("data-href"); }
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
            try { new Notification("Druktest: tijd voor controle", { body: el.getAttribute("data-alert"), icon: "/static/img/icon-192.png" }); } catch (x) {}
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
