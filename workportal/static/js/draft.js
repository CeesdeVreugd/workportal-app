/* Concept bewaren voor formulieren met data-draft="sleutel" (bijv. een servicebezoek).
   - Tekstvelden worden tijdens het typen bewaard op dit apparaat (localStorage).
   - Foto's uit invoervelden met data-collect worden verzameld (meerdere keren foto maken telt op) en bewaard in IndexedDB.
   - Gaat er iets mis (app herstart, vergrendeld, verbinding weg), dan wordt het concept bij terugkomst teruggezet.
   - Het concept wordt pas weggegooid als het opslaan gelukt is. */
(function () {
  "use strict";
  var form = document.querySelector("form[data-draft]");
  var CLEAR = "wp_draft_clear";
  // Na een geslaagd opslaan (groene melding op de volgende pagina) het concept opruimen
  try {
    var pend = sessionStorage.getItem(CLEAR);
    if (pend && (!form || form.getAttribute("data-draft") !== pend)) {
      if (document.querySelector(".flash.ok")) { localStorage.removeItem("wp_draft:" + pend); idbDel(pend); }
      sessionStorage.removeItem(CLEAR);
    }
  } catch (e) {}
  if (!form) return;

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
  function save() {
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

  // ---------------------------------------------------------------- foto's verzamelen
  var inputs = Array.prototype.slice.call(form.querySelectorAll("input[type=file][data-collect]"));
  var box = inputs.length ? document.getElementById(inputs[0].getAttribute("data-preview")) : null;
  function render() {
    if (!box) return;
    box.querySelectorAll("[data-collected], [data-from]").forEach(function (x) { x.remove(); });
    collected.forEach(function (f, i) {
      var d = document.createElement("div"); d.className = "photo"; d.setAttribute("data-collected", "1");
      var img = document.createElement("img"); img.src = URL.createObjectURL(f.blob); img.alt = f.name; d.appendChild(img);
      var x = document.createElement("button"); x.type = "button"; x.className = "photo-x"; x.textContent = "×"; x.setAttribute("aria-label", "Foto weghalen");
      x.addEventListener("click", function () { collected.splice(i, 1); render(); persistPhotos(); save(); });
      d.appendChild(x); box.appendChild(d);
    });
  }
  function persistPhotos() { return idbPut(KEY, collected.map(function (f) { return { name: f.name, type: f.type, blob: f.blob }; })); }
  if (canDT) {
    inputs.forEach(function (inp) {
      inp.removeAttribute("data-preview");   // de verzamelaar toont de voorbeelden zelf
      inp.addEventListener("change", function (e) {
        e.stopImmediatePropagation();
        Array.prototype.forEach.call(inp.files, function (f, n) {
          var name = f.name && f.name !== "image.jpg" ? f.name : "foto-" + Date.now() + "-" + n + ".jpg";
          collected.push({ name: name, type: f.type || "image/jpeg", blob: f });
        });
        inp.value = "";
        render(); persistPhotos(); save();
      }, true);
    });
  }

  // ---------------------------------------------------------------- opslaan
  form.addEventListener("submit", function () {
    if (canDT && collected.length && inputs.length) {
      var dt = new DataTransfer();
      collected.forEach(function (f) { dt.items.add(new File([f.blob], f.name, { type: f.type })); });
      inputs[0].files = dt.files;
      inputs.slice(1).forEach(function (i) { i.value = ""; });
    }
    try { sessionStorage.setItem(CLEAR, KEY); } catch (e) {}
  });

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
      try { localStorage.removeItem(LS); } catch (e) {} idbDel(KEY); location.reload();
    });
    b.appendChild(x); form.parentNode.insertBefore(b, form);
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
      else if (el.value !== v) { el.value = v; restoredText = true; el.dispatchEvent(new Event("input", { bubbles: true })); }
    });
  }
  idbGet(KEY).then(function (list) {
    if (list && list.length && canDT) { collected = list.map(function (f) { return { name: f.name, type: f.type, blob: f.blob }; }); render(); }
    if (restoredText || collected.length) banner(raw ? raw.at : Date.now(), collected.length);
    initial = restoredText ? "" : initial;
  });
})();
