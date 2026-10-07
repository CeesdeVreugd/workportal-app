/* Aantekening: afbeelding (screenshot, 3D, foto, geplakt) met pijlen, tekst en vormen. Alles in de browser;
   bij opslaan gaat één platte JPEG mee in het formulier. */
(function () {
  "use strict";
  var form = document.getElementById("annform");
  if (!form) return;
  var edit = document.getElementById("ann-edit"), cv = document.getElementById("ann-canvas"), ctx = cv.getContext("2d");
  var empty = document.getElementById("ann-empty"), tools = document.getElementById("ann-tools");
  var cropbar = document.getElementById("ann-cropbar"), countEl = document.getElementById("ann-count");
  var stage = document.getElementById("ann-stage"), removeBtn = document.getElementById("ann-remove");
  var MAX = 2400;
  var base = null, marks = [], cur = null, tool = "arrow", color = "#E53935", size = 1, changed = false, removed = false;
  var crop = null; // {src: canvas, r: {x,y,w,h} | null, start}

  function $(id) { return document.getElementById(id); }
  function lw() { return Math.max(2, Math.max(cv.width, cv.height) / 170) * size; }

  // ------------------------------------------------------------ afbeelding laden
  function toCanvas(src, max) {
    var w = src.naturalWidth || src.videoWidth || src.width, h = src.naturalHeight || src.videoHeight || src.height;
    var r = Math.min(1, (max || MAX) / Math.max(w, h));
    var c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(w * r)); c.height = Math.max(1, Math.round(h * r));
    var x = c.getContext("2d"); x.fillStyle = "#fff"; x.fillRect(0, 0, c.width, c.height);
    x.drawImage(src, 0, 0, c.width, c.height);
    return c;
  }
  function showEditor(on) {
    empty.hidden = on; cv.hidden = !on; tools.hidden = !on || !!crop; cropbar.hidden = !crop; removeBtn.hidden = !on;
  }
  function setBase(src, isNew) {
    base = toCanvas(src); cv.width = base.width; cv.height = base.height;
    marks = []; cur = null; crop = null;
    if (isNew) changed = true;
    removed = false;
    showEditor(true); redraw();
  }
  function startCrop(src) {
    crop = { src: toCanvas(src, 4000), r: null };
    cv.width = crop.src.width; cv.height = crop.src.height;
    $("ann-crop-ok").disabled = true;
    showEditor(true); redraw();
    stage.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  function loadUrl(url, isNew, thenCrop) {
    var im = new Image();
    im.onload = function () { if (thenCrop) startCrop(im); else setBase(im, isNew); };
    im.onerror = function () { alert("Deze afbeelding kon niet worden geopend."); };
    im.src = url;
  }
  function loadBlob(blob, thenCrop) {
    if (!blob || !/^image\//.test(blob.type || "image/")) return;
    var url = URL.createObjectURL(blob);
    loadUrl(url, true, thenCrop);
  }

  var seed = edit.getAttribute("data-seed");
  if (seed) loadUrl(seed, seed.indexOf("data:") === 0, seed.indexOf("data:") === 0);

  // ------------------------------------------------------------ tekenen
  function drawMark(m, c) {
    c.save();
    c.strokeStyle = m.color; c.fillStyle = m.color; c.lineWidth = m.w; c.lineCap = "round"; c.lineJoin = "round";
    if (m.t === "arrow") {
      var dx = m.x2 - m.x1, dy = m.y2 - m.y1, len = Math.hypot(dx, dy), a = Math.atan2(dy, dx), hl = Math.min(len * 0.6, m.w * 4.5);
      var bx = m.x2 - Math.cos(a) * hl * 0.8, by = m.y2 - Math.sin(a) * hl * 0.8;
      outline(c, m, function () { c.beginPath(); c.moveTo(m.x1, m.y1); c.lineTo(bx, by); c.stroke(); });
      c.beginPath(); c.moveTo(m.x2, m.y2);
      c.lineTo(m.x2 - Math.cos(a - 0.45) * hl, m.y2 - Math.sin(a - 0.45) * hl);
      c.lineTo(m.x2 - Math.cos(a + 0.45) * hl, m.y2 - Math.sin(a + 0.45) * hl);
      c.closePath();
      c.save(); c.strokeStyle = haloColor(m.color); c.lineWidth = Math.max(2, m.w * 0.5); c.stroke(); c.restore();
      c.fill();
    } else if (m.t === "line") {
      outline(c, m, function () { c.beginPath(); c.moveTo(m.x1, m.y1); c.lineTo(m.x2, m.y2); c.stroke(); });
    } else if (m.t === "rect") {
      outline(c, m, function () { c.strokeRect(Math.min(m.x1, m.x2), Math.min(m.y1, m.y2), Math.abs(m.x2 - m.x1), Math.abs(m.y2 - m.y1)); });
    } else if (m.t === "ellipse") {
      outline(c, m, function () {
        c.beginPath();
        c.ellipse((m.x1 + m.x2) / 2, (m.y1 + m.y2) / 2, Math.max(1, Math.abs(m.x2 - m.x1) / 2), Math.max(1, Math.abs(m.y2 - m.y1) / 2), 0, 0, Math.PI * 2);
        c.stroke();
      });
    } else if (m.t === "pen") {
      outline(c, m, function () {
        c.beginPath(); c.moveTo(m.pts[0][0], m.pts[0][1]);
        for (var i = 1; i < m.pts.length; i++) c.lineTo(m.pts[i][0], m.pts[i][1]);
        c.stroke();
      });
    } else if (m.t === "text") {
      var fs = m.w * 5.5, lines = m.text.split("\n");
      c.font = "bold " + fs + "px Ubuntu, Arial, sans-serif"; c.textBaseline = "top";
      c.lineWidth = fs * 0.22; c.strokeStyle = haloColor(m.color);
      lines.forEach(function (ln, i) { c.strokeText(ln, m.x, m.y + i * fs * 1.2); c.fillText(ln, m.x, m.y + i * fs * 1.2); });
    } else if (m.t === "num") {
      var r = m.w * 3.6;
      c.beginPath(); c.arc(m.x, m.y, r, 0, Math.PI * 2); c.fill();
      c.lineWidth = Math.max(2, m.w * 0.5); c.strokeStyle = haloColor(m.color); c.stroke();
      c.fillStyle = m.color === "#FFFFFF" || m.color === "#FFC400" ? "#14163A" : "#FFFFFF";
      c.font = "bold " + (r * 1.15) + "px Ubuntu, Arial, sans-serif"; c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText(String(m.n), m.x, m.y + r * 0.05);
    }
    c.restore();
  }
  function haloColor(col) { return col === "#FFFFFF" || col === "#FFC400" ? "rgba(20,22,58,.75)" : "rgba(255,255,255,.9)"; }
  // lichte rand om lijnen, zodat ze ook op een drukke achtergrond goed te zien zijn
  function outline(c, m, path) {
    c.save(); c.strokeStyle = haloColor(m.color); c.lineWidth = m.w + Math.max(2, m.w * 0.6); path(); c.restore();
    path();
  }
  function redraw() {
    if (crop) {
      ctx.drawImage(crop.src, 0, 0);
      if (crop.r) {
        var r = norm(crop.r);
        ctx.save(); ctx.fillStyle = "rgba(10,10,40,.55)";
        ctx.beginPath(); ctx.rect(0, 0, cv.width, cv.height); ctx.rect(r.x, r.y, r.w, r.h); ctx.fill("evenodd");
        ctx.strokeStyle = "#0080FF"; ctx.lineWidth = Math.max(2, cv.width / 600); ctx.setLineDash([12, 8]); ctx.strokeRect(r.x, r.y, r.w, r.h);
        ctx.restore();
      } else {
        ctx.save(); ctx.fillStyle = "rgba(10,10,40,.25)"; ctx.fillRect(0, 0, cv.width, cv.height); ctx.restore();
      }
      return;
    }
    if (!base) return;
    ctx.drawImage(base, 0, 0);
    marks.forEach(function (m) { drawMark(m, ctx); });
    if (cur) drawMark(cur, ctx);
  }
  function norm(r) { return { x: Math.min(r.x1, r.x2), y: Math.min(r.y1, r.y2), w: Math.abs(r.x2 - r.x1), h: Math.abs(r.y2 - r.y1) }; }
  function nextNum() { var n = 0; marks.forEach(function (m) { if (m.t === "num") n = Math.max(n, m.n); }); return n + 1; }

  // ------------------------------------------------------------ aanwijzen
  function pos(e) {
    var b = cv.getBoundingClientRect();
    return [(e.clientX - b.left) * cv.width / b.width, (e.clientY - b.top) * cv.height / b.height];
  }
  cv.addEventListener("pointerdown", function (e) {
    if (e.button !== undefined && e.button !== 0) return;
    var p = pos(e);
    e.preventDefault();
    try { cv.setPointerCapture(e.pointerId); } catch (x) {}
    if (crop) { crop.r = { x1: p[0], y1: p[1], x2: p[0], y2: p[1] }; crop.drag = true; redraw(); return; }
    if (!base) return;
    if (tool === "text") return; // bij loslaten
    if (tool === "num") { marks.push({ t: "num", x: p[0], y: p[1], n: nextNum(), color: color, w: lw() }); changed = true; redraw(); return; }
    cur = { t: tool, x1: p[0], y1: p[1], x2: p[0], y2: p[1], color: color, w: lw() };
    if (tool === "pen") cur.pts = [p];
  });
  cv.addEventListener("pointermove", function (e) {
    var p = pos(e);
    if (crop && crop.drag) { crop.r.x2 = p[0]; crop.r.y2 = p[1]; redraw(); return; }
    if (!cur) return;
    cur.x2 = p[0]; cur.y2 = p[1];
    if (cur.pts) cur.pts.push(p);
    redraw();
  });
  cv.addEventListener("pointerup", function (e) {
    var p = pos(e);
    if (crop && crop.drag) {
      crop.drag = false;
      var r = norm(crop.r);
      if (r.w < 8 || r.h < 8) crop.r = null;
      $("ann-crop-ok").disabled = !crop.r; redraw(); return;
    }
    if (!base) return;
    if (tool === "text") {
      var t = window.prompt("Tekst bij de afbeelding:");
      if (t && t.trim()) { marks.push({ t: "text", x: p[0], y: p[1], text: t.trim(), color: color, w: lw() }); changed = true; }
      redraw(); return;
    }
    if (!cur) return;
    var big = cur.t === "pen" ? cur.pts.length > 2 : Math.hypot(cur.x2 - cur.x1, cur.y2 - cur.y1) > lw() * 2;
    if (big) { marks.push(cur); changed = true; }
    cur = null; redraw();
  });
  cv.addEventListener("pointercancel", function () { cur = null; if (crop) crop.drag = false; redraw(); });

  // ------------------------------------------------------------ werkbalk
  tools.querySelectorAll("[data-tool]").forEach(function (b) {
    b.addEventListener("click", function () {
      tool = b.getAttribute("data-tool");
      tools.querySelectorAll("[data-tool]").forEach(function (x) { x.classList.toggle("on", x === b); });
    });
  });
  tools.querySelectorAll("[data-color]").forEach(function (b) {
    b.addEventListener("click", function () {
      color = b.getAttribute("data-color");
      tools.querySelectorAll("[data-color]").forEach(function (x) { x.classList.toggle("on", x === b); });
    });
  });
  tools.querySelectorAll("[data-size]").forEach(function (b) {
    b.addEventListener("click", function () {
      size = parseFloat(b.getAttribute("data-size"));
      tools.querySelectorAll("[data-size]").forEach(function (x) { x.classList.toggle("on", x === b); });
    });
  });
  $("ann-undo").addEventListener("click", function () { if (marks.length) { marks.pop(); changed = true; redraw(); } });
  $("ann-clear").addEventListener("click", function () { if (marks.length && confirm("Alle pijlen, tekst en vormen weghalen?")) { marks = []; changed = true; redraw(); } });
  $("ann-crop").addEventListener("click", function () {
    // eerst alles plat maken, dan bijsnijden
    var flat = document.createElement("canvas"); flat.width = cv.width; flat.height = cv.height;
    var c = flat.getContext("2d"); c.drawImage(base, 0, 0); marks.forEach(function (m) { drawMark(m, c); });
    startCrop(flat);
  });
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "z" && !/INPUT|TEXTAREA|SELECT/.test((e.target || {}).tagName)) { e.preventDefault(); $("ann-undo").click(); }
  });

  $("ann-crop-ok").addEventListener("click", function () {
    if (!crop || !crop.r) return;
    var r = norm(crop.r), c = document.createElement("canvas");
    c.width = Math.round(r.w); c.height = Math.round(r.h);
    c.getContext("2d").drawImage(crop.src, r.x, r.y, r.w, r.h, 0, 0, c.width, c.height);
    crop = null; setBase(c, true);
  });
  $("ann-crop-all").addEventListener("click", function () { if (!crop) return; var s = crop.src; crop = null; setBase(s, true); });
  $("ann-crop-cancel").addEventListener("click", function () {
    var hadBase = !!base; crop = null;
    if (hadBase) { cv.width = base.width; cv.height = base.height; showEditor(true); redraw(); } else showEditor(false);
  });

  // ------------------------------------------------------------ bronnen
  function fromInput(inp) { inp.addEventListener("change", function () { if (inp.files && inp.files[0]) loadBlob(inp.files[0], false); inp.value = ""; }); }
  fromInput($("ann-cam")); fromInput($("ann-file"));
  $("ann-blank").addEventListener("click", function () {
    var c = document.createElement("canvas"); c.width = 1600; c.height = 1000;
    var x = c.getContext("2d"); x.fillStyle = "#fff"; x.fillRect(0, 0, c.width, c.height);
    setBase(c, true);
  });
  removeBtn.addEventListener("click", function () {
    if (!confirm("Afbeelding weghalen?")) return;
    base = null; marks = []; crop = null; removed = true; changed = true; showEditor(false);
  });
  document.addEventListener("paste", function (e) {
    var items = (e.clipboardData && e.clipboardData.items) || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.indexOf("image/") === 0) {
        if (base && !confirm("De huidige afbeelding vervangen door de geplakte?")) return;
        e.preventDefault(); loadBlob(items[i].getAsFile(), false); return;
      }
    }
  });
  stage.addEventListener("dragover", function (e) { e.preventDefault(); stage.classList.add("over"); });
  stage.addEventListener("dragleave", function () { stage.classList.remove("over"); });
  stage.addEventListener("drop", function (e) {
    e.preventDefault(); stage.classList.remove("over");
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) loadBlob(f, false);
  });

  // Screenshot van een ander programma (Chrome/Edge op de computer): venster of scherm kiezen, dan uitsnijden
  var screenBtn = $("ann-screen");
  var mobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && /Macintosh/.test(navigator.userAgent));
  if (navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia && !mobile) {
    screenBtn.hidden = false;
    document.querySelectorAll(".ann-screen-hint").forEach(function (x) { x.hidden = false; });
    screenBtn.addEventListener("click", takeScreenshot);
  }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function countdown(n) {
    countEl.hidden = false;
    var p = Promise.resolve();
    for (var i = n; i > 0; i--) (function (k) { p = p.then(function () { countEl.textContent = k; return wait(1000); }); })(i);
    return p.then(function () { countEl.hidden = true; });
  }
  function takeScreenshot() {
    var opts = { video: { displaySurface: "window", frameRate: 5 }, audio: false, selfBrowserSurface: "exclude", surfaceSwitching: "exclude", monitorTypeSurfaces: "include" };
    var ctl = null;
    if (window.CaptureController) { try { ctl = new CaptureController(); opts.controller = ctl; } catch (x) { ctl = null; } }
    var stream, surf;
    navigator.mediaDevices.getDisplayMedia(opts).then(function (s) {
      stream = s;
      var track = s.getVideoTracks()[0];
      surf = (track.getSettings && track.getSettings().displaySurface) || "";
      // bij een venster blijven we hier: het venster wordt op de achtergrond vastgelegd
      if (ctl && ctl.setFocusBehavior && surf !== "monitor") { try { ctl.setFocusBehavior("no-focus-change"); } catch (x) {} }
      var v = document.createElement("video"); v.muted = true; v.playsInline = true; v.srcObject = s;
      return v.play().then(function () { return v; });
    }).then(function (v) {
      // heel scherm: even tijd om naar het programma te gaan
      return (surf === "monitor" ? countdown(3) : wait(250)).then(function () { return v; });
    }).then(function (v) {
      var c = document.createElement("canvas"); c.width = v.videoWidth; c.height = v.videoHeight;
      c.getContext("2d").drawImage(v, 0, 0);
      stream.getTracks().forEach(function (t) { t.stop(); });
      try { window.focus(); } catch (x) {}
      if (base && !confirm("De huidige afbeelding vervangen door de screenshot?")) return;
      startCrop(c);
    }).catch(function (err) {
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
      countEl.hidden = true;
      if (err && err.name !== "NotAllowedError" && err.name !== "AbortError") alert("Screenshot maken lukte niet: " + (err.message || err.name));
    });
  }

  // ------------------------------------------------------------ status en opslaan
  var st = $("ann-status"), done = $("ann-done");
  st.addEventListener("change", function () { done.hidden = st.value !== "afgerond"; done.querySelector("textarea").required = st.value === "afgerond"; });
  done.querySelector("textarea").required = st.value === "afgerond";
  form.addEventListener("submit", function (e) {
    if (crop) { e.preventDefault(); alert("Kies eerst ‘Uitsnijden’ of ‘Hele beeld’."); return; }
    if (removed && !base) { $("ann-changed").value = "1"; $("ann-image").value = ""; return; }
    if (base && changed) {
      cur = null; redraw();
      $("ann-image").value = cv.toDataURL("image/jpeg", 0.88);
      $("ann-changed").value = "1";
    }
  });
})();
