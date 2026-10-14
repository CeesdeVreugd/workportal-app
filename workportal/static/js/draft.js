/* Concept bewaren voor formulieren met data-draft="sleutel" (bijv. een servicebezoek).
   - Tekstvelden worden tijdens het typen bewaard op dit apparaat (localStorage).
   - Foto's uit invoervelden met data-collect worden verzameld (meerdere keren foto maken telt op) en bewaard in IndexedDB.
   - Gaat er iets mis (app herstart, vergrendeld, verbinding weg), dan wordt het concept bij terugkomst teruggezet.
   - Het concept wordt pas weggegooid als het opslaan gelukt is. */
(function () {
  "use strict";
  var CLEAR = "wp_draft_clear", cleared = null;
  // Na een geslaagd opslaan (groene melding op de volgende pagina) het concept opruimen
  try {
    var pend = sessionStorage.getItem(CLEAR);
    if (pend) {
      if (document.querySelector(".flash.ok")) {
        cleared = pend;
        localStorage.removeItem("wp_draft:" + pend);
        if (window.indexedDB) { var r1 = indexedDB.open("wp_drafts", 1); r1.onsuccess = function () { try { r1.result.transaction("photos", "readwrite").objectStore("photos").delete(pend + ":img"); } catch (x) {} }; }
        if (window.indexedDB) { var r0 = indexedDB.open("wp_drafts", 1); r0.onupgradeneeded = function () { r0.result.createObjectStore("photos"); }; r0.onsuccess = function () { try { r0.result.transaction("photos", "readwrite").objectStore("photos").delete(pend); } catch (x) {} }; }
      }
      sessionStorage.removeItem(CLEAR);
    }
  } catch (e) {}
  // Ook zonder data-draft: elk invulformulier met tekst (niet in Beheer) krijgt automatisch een concept
  var uid = (document.body.getAttribute("data-uid") || "0");
  if (!/^\/beheer/.test(location.pathname)) {
    Array.prototype.forEach.call(document.querySelectorAll("form"), function (f, i) {
      if (f.hasAttribute("data-draft") || f.hasAttribute("data-no-draft") || (f.method || "").toLowerCase() !== "post") return;
      var texts = f.querySelectorAll("textarea, input[type=text], input:not([type])");
      if (!f.querySelector("textarea") && texts.length < 2) return;
      f.setAttribute("data-draft", "auto:" + uid + ":" + location.pathname + ":" + (f.getAttribute("action") || "") + ":" + i);
    });
  }
  document.querySelectorAll("form[data-draft]").forEach(initDraft);

  window.WPDraft = { idbPut: function (k, v) { return idbPutG(k, v); }, idbGet: function (k) { return idbGetG(k); }, idbDel: function (k) { return idbDelG(k); } };
  function idbG() { return new Promise(function (res, rej) { if (!window.indexedDB) return rej(); var r = indexedDB.open("wp_drafts", 1); r.onupgradeneeded = function () { r.result.createObjectStore("photos"); }; r.onsuccess = function () { res(r.result); }; r.onerror = function () { rej(r.error); }; }); }
  function idbPutG(key, val) { return idbG().then(function (db) { return new Promise(function (res) { var tx = db.transaction("photos", "readwrite"); tx.objectStore("photos").put(val, key); tx.oncomplete = res; tx.onerror = res; }); }).catch(function () {}); }
  function idbGetG(key) { return idbG().then(function (db) { return new Promise(function (res) { var q = db.transaction("photos").objectStore("photos").get(key); q.onsuccess = function () { res(q.result); }; q.onerror = function () { res(null); }; }); }).catch(function () { return null; }); }
  function idbDelG(key) { return idbG().then(function (db) { db.transaction("photos", "readwrite").objectStore("photos").delete(key); }).catch(function () {}); }
  window.WPDraft.cleared = function () { return cleared; };

  function initDraft(form) {
  var KEY = form.getAttribute("data-draft"), LS = "wp_draft:" + KEY;
  var collected = [];   // {name, type, blob}
  var canDT = (function () { try { return !!new DataTransfer(); } catch (e) { return false; } })();

  // ---------------------------------------------------------------- IndexedDB voor foto's
  function idb() {
    return new Promise(function (res, rej) {
      if (!window.indexedDB) return rej();
      var r = indexedDB.open("wp_drafts", 1);
      r.onupgradeneeded = function () { r.result.createObjectStore("photos"); };
      r.onsuccess = function () { res(r.result); };
      r.onerror = function () { rej(r.error); };
    });
  }
  function idbPut(key, val) { return idb().then(function (db) { return new Promise(function (res) { var tx = db.transaction("photos", "readwrite"); tx.objectStore("photos").put(val, key); tx.oncomplete = res; tx.onerror = res; }); }).catch(function () {}); }
  function idbGet(key) { return idb().then(function (db) { return new Promise(function (res) { var q = db.transaction("photos").objectStore("photos").get(key); q.onsuccess = function () { res(q.result); }; q.onerror = function () { res(null); }; }); }).catch(function () { return null; }); }
  function idbDel(key) { return idb().then(function (db) { db.transaction("photos", "readwrite").objectStore("photos").delete(key); }).catch(function () {}); }

  // ---------------------------------------------------------------- tekstvelden
  function fields() {
    return Array.prototype.filter.call(form.elements, function (el) {
      return el.name && el.name !== "csrf_token" && !el.disabled && ["file", "submit", "button", "hidden"].indexOf(el.type) < 0;
    });
  }
  function snapshot() {
    var d = {};
    fields().forEach(function (el) { d[el.name + "#" + fields().filter(function (x) { return x.name === el.name; }).indexOf(el)] = (el.type === "checkbox" || el.type === "radio") ? el.checked : el.value; });
    return d;
  }
  var initial = JSON.stringify(snapshot()), timer = null;
  var submitting = false;
  function save() {
    if (submitting) return;
    clearTimeout(timer);
    timer = setTimeout(function () {
      var snap = snapshot(), changed = JSON.stringify(snap) !== initial || collected.length;
      try {
        if (changed) localStorage.setItem(LS, JSON.stringify({ at: Date.now(), fields: snap, photos: collected.length }));
        else localStorage.removeItem(LS);
      } catch (e) {}
    }, 400);
  }
  form.addEventListener("input", save);
  form.addEventListener("change", function (e) { if (e.target.type !== "file") save(); });

  // ---------------------------------------------------------------- foto's verzamelen (verkleind, zodat opslaan snel gaat)
  var inputs = Array.prototype.slice.call(form.querySelectorAll("input[type=file][data-collect]"));
  var box = inputs.length ? document.getElementById(inputs[0].getAttribute("data-preview")) : null;
  if (box) box.setAttribute("data-gallery", "");
  var busy = 0;
  function loadImg(file) {
    if (window.createImageBitmap) return createImageBitmap(file, { imageOrientation: "from-image" }).catch(function () { return createImageBitmap(file); });
    return new Promise(function (res, rej) { var u = URL.createObjectURL(file), im = new Image(); im.onload = function () { res(im); }; im.onerror = rej; im.src = u; });
  }
  function toBlob(src, max, q) {
    var w = src.width, h = src.height, r = Math.min(1, max / Math.max(w, h));
    var c = document.createElement("canvas"); c.width = Math.round(w * r); c.height = Math.round(h * r);
    c.getContext("2d").drawImage(src, 0, 0, c.width, c.height);
    return new Promise(function (res) { c.toBlob(function (b) { c.width = c.height = 0; res(b); }, "image/jpeg", q); });
  }
  function shrink(file) {
    return loadImg(file).then(function (img) {
      return Promise.all([toBlob(img, 2000, 0.85), toBlob(img, 360, 0.7)]).then(function (r) { if (img.close) img.close(); return { blob: r[0], thumb: r[1] }; });
    }).catch(function () { return { blob: file, thumb: null }; });  // geen afbeelding die de browser kan lezen: origineel houden
  }
  function render() {
    if (!box) return;
    box.querySelectorAll("[data-collected], [data-from]").forEach(function (x) { if (x._url) URL.revokeObjectURL(x._url); if (x._full) URL.revokeObjectURL(x._full); x.remove(); });
    collected.forEach(function (f, i) {
      var d = document.createElement("div"); d.className = "photo"; d.setAttribute("data-collected", "1");
      var a = document.createElement("a"); a.href = "#"; d._full = URL.createObjectURL(f.blob);
      a.setAttribute("data-full", d._full); a.setAttribute("data-dl", d._full); a.setAttribute("data-name", "Nieuwe foto " + (i + 1) + " (nog niet opgeslagen)");
      var img = document.createElement("img"); d._url = URL.createObjectURL(f.thumb || f.blob); img.src = d._url; img.alt = f.name; a.appendChild(img); d.appendChild(a);
      var x = document.createElement("button"); x.type = "button"; x.className = "photo-x"; x.textContent = "×"; x.setAttribute("aria-label", "Foto weghalen");
      x.addEventListener("click", function () { collected.splice(i, 1); render(); persistPhotos(); save(); });
      d.appendChild(x); box.appendChild(d);
    });
    if (busy) { var w = document.createElement("div"); w.className = "photo photo-wait"; w.setAttribute("data-collected", "1"); w.textContent = "Foto verwerken…"; box.appendChild(w); }
  }
  function persistPhotos() { return idbPut(KEY, collected.map(function (f) { return { name: f.name, type: f.type, blob: f.blob, thumb: f.thumb }; })); }
  if (canDT) {
    inputs.forEach(function (inp) {
      inp.removeAttribute("data-preview");   // de verzamelaar toont de voorbeelden zelf
      inp.addEventListener("change", function (e) {
        e.stopImmediatePropagation();
        var files = Array.prototype.slice.call(inp.files); inp.value = "";
        busy += files.length; render();
        files.reduce(function (p, f, n) {   // één voor één, zodat de telefoon niet vastloopt op geheugen
          return p.then(function () {
            return shrink(f).then(function (r) {
              var base = f.name && f.name !== "image.jpg" ? f.name.replace(/\.[^.]+$/, "") : "foto-" + Date.now() + "-" + n;
              collected.push({ name: base + ".jpg", type: r.blob.type || "image/jpeg", blob: r.blob, thumb: r.thumb });
              busy--; render();
            });
          });
        }, Promise.resolve()).then(function () { persistPhotos(); save(); });
      }, true);
    });
  }

  // ---------------------------------------------------------------- opslaan met voortgang (de pagina blijft staan tot alles binnen is)
  var overlay = null;
  function progress(pct, text) {
    if (!overlay) {
      overlay = document.createElement("div"); overlay.className = "saveoverlay";
      overlay.innerHTML = '<div class="savebox"><b>Bezig met opslaan…</b><div class="savebar"><i></i></div><span class="savetxt"></span></div>';
      document.body.appendChild(overlay);
    }
    overlay.querySelector("i").style.width = pct + "%";
    overlay.querySelector(".savetxt").textContent = text || "";
  }
  function unlock() { submitting = false; form.removeAttribute("data-submitting"); setTimeout(function () { form.querySelectorAll("button").forEach(function (b) { b.disabled = false; }); }, 50); }
  form.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    try { sessionStorage.setItem(CLEAR, KEY); } catch (x) {}
    clearTimeout(timer); submitting = true; form.setAttribute("data-submitting", "1");
    if (!inputs.length) {   // gewoon versturen; werd het toch tegengehouden (bijv. een melding), dan verder bewaren
      setTimeout(function () { if (e.defaultPrevented) { submitting = false; form.removeAttribute("data-submitting"); try { sessionStorage.removeItem(CLEAR); } catch (x) {} } }, 0);
      return;
    }   // geen foto-verzamelaar: gewoon versturen
    if (!canDT || !window.FormData || !window.XMLHttpRequest) return;   // oude browser: gewoon versturen
    if (busy) { e.preventDefault(); unlock(); alert("Even wachten: de foto's worden nog verwerkt."); return; }
    e.preventDefault();
    var submitter = e.submitter;
    progress(1, collected.length ? "Foto's klaarmaken…" : "");
    // foto's eerst echt inlezen (iPhone: een bewaard of gekozen bestand kan anders leeg verstuurd worden)
    Promise.all(collected.map(function (f) {
      return (f.blob.arrayBuffer ? f.blob.arrayBuffer() : new Response(f.blob).arrayBuffer()).then(function (buf) {
        if (!buf || buf.byteLength < 500) throw new Error("leeg");
        return new File([buf], f.name, { type: f.type || "image/jpeg" });
      });
    })).then(function (files) { send(files, submitter); }).catch(function () {
      if (overlay) { overlay.remove(); overlay = null; } unlock();
      try { sessionStorage.removeItem(CLEAR); } catch (x) {}
      alert("Een of meer foto's konden niet worden gelezen. Haal die foto's weg (×) en voeg ze opnieuw toe. De tekst is bewaard.");
    });
  });

  function send(files, submitter) {
    var fd = new FormData(form);
    fd.delete("photos");
    files.forEach(function (f) { fd.append("photos", f); });
    if (submitter && submitter.name) fd.append(submitter.name, submitter.value);
    progress(2, files.length ? files.length + " foto" + (files.length === 1 ? "" : "'s") + " versturen" : "");
    var xhr = new XMLHttpRequest();
    xhr.open("POST", form.getAttribute("action") || location.href);
    xhr.upload.onprogress = function (ev) { if (ev.lengthComputable) progress(Math.round(ev.loaded / ev.total * 95), Math.round(ev.loaded / 1048576 * 10) / 10 + " van " + Math.round(ev.total / 1048576 * 10) / 10 + " MB"); };
    xhr.onload = function () {
      var url = xhr.responseURL || "";
      if (xhr.status >= 200 && xhr.status < 400 && url && !/\/(login|pin|vergrendel)/.test(url)) {
        progress(100, "Opgeslagen");
        var ok = /class="flash ok"/.test(xhr.responseText);
        try { sessionStorage.removeItem(CLEAR); } catch (x) {}
        if (ok) { try { localStorage.removeItem(LS); } catch (x) {} idbDel(KEY); }   // gelukt: concept weg
        setTimeout(function () {
          if (!ok) {   // formulier met een melding terug: die pagina tonen, concept blijft bewaard
            try { history.replaceState(null, "", url); } catch (x) {}
            document.open(); document.write(xhr.responseText); document.close();
          } else if (url.split("#")[0] === location.href.split("#")[0]) {
            try { history.replaceState(null, "", url); } catch (x) {}
            location.reload();
          } else location.replace(url);
        }, ok ? 150 : 0);
        return;
      }
      try { sessionStorage.removeItem(CLEAR); } catch (x) {}
      overlay.remove(); overlay = null; unlock();
      if (/\/(login|pin|vergrendel)/.test(url)) { alert("Je was vergrendeld. Je bezoek en foto's zijn bewaard: voer je pincode in en sla opnieuw op."); location.href = url; }
      else alert("Opslaan is niet gelukt (" + (xhr.status || "geen verbinding") + "). Je bezoek en foto's zijn bewaard; probeer het opnieuw.");
    };
    xhr.onerror = function () {
      try { sessionStorage.removeItem(CLEAR); } catch (x) {}
      if (overlay) { overlay.remove(); overlay = null; } unlock();
      alert("Geen verbinding. Je bezoek en foto's zijn bewaard op dit apparaat; probeer het opnieuw als je weer bereik hebt.");
    };
    xhr.send(fd);
  }

  // ---------------------------------------------------------------- concept terugzetten
  var raw = null;
  try { raw = JSON.parse(localStorage.getItem(LS) || "null"); } catch (e) {}
  if (raw && Date.now() - raw.at > 14 * 864e5) { try { localStorage.removeItem(LS); } catch (e) {} raw = null; idbDel(KEY); }
  function banner(at, nPhotos) {
    var b = document.createElement("div"); b.className = "flash info draftbar";
    var t = new Date(at), hh = ("0" + t.getHours()).slice(-2) + ":" + ("0" + t.getMinutes()).slice(-2);
    b.innerHTML = "<span><b>Concept teruggezet</b> van " + t.toLocaleDateString("nl-NL") + " " + hh +
      (nPhotos ? " (met " + nPhotos + " foto" + (nPhotos === 1 ? "" : "'s") + ")" : "") + ". Controleer en sla op.</span>";
    var x = document.createElement("button"); x.type = "button"; x.className = "btn sm ghost"; x.textContent = "Concept weggooien";
    x.addEventListener("click", function () {
      if (!confirm("Het teruggezette concept weggooien?")) return;
      try { localStorage.removeItem(LS); } catch (e) {} idbDel(KEY); idbDel(KEY + ":img"); setTimeout(function () { location.reload(); }, 150);
    });
    b.appendChild(x); form.parentNode.insertBefore(b, form);
    form.classList.remove("hidden");
    var det = form.closest("details"); if (det) det.open = true;
  }
  var restoredText = false;
  if (raw && raw.fields) {
    var counts = {};
    fields().forEach(function (el) {
      var i = counts[el.name] || 0; counts[el.name] = i + 1;
      var k = el.name + "#" + i;
      if (!(k in raw.fields)) return;
      var v = raw.fields[k];
      if (el.type === "checkbox" || el.type === "radio") { if (el.checked !== v) { el.checked = v; restoredText = true; } }
      else if (el.value !== v) { el.value = v; restoredText = true; el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("wp:set")); }
    });
  }
  (KEY === cleared ? Promise.resolve(null) : idbGet(KEY)).then(function (list) {
    if (list && list.length && canDT) { collected = list.map(function (f) { return { name: f.name, type: f.type, blob: f.blob, thumb: f.thumb || null }; }); render(); }
    if (restoredText || collected.length) banner(raw ? raw.at : Date.now(), collected.length);
    initial = restoredText ? "" : initial;
  });
  }
})();
