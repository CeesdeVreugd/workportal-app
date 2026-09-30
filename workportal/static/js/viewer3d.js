/* WorkPortal 3D-viewer (Online3DViewer-engine, MIT).
   Draaien/zoomen/pannen, standaardaanzichten, doorsnede, onderdelen tonen/verbergen,
   meten: punt-punt, vlak-vlak (hoek en afstand bij evenwijdige vlakken). */
(function () {
  "use strict";
  var cfg = window.WP3D || {};
  var wrap = document.getElementById("v3d-canvas");
  var overlay = document.getElementById("v3d-overlay");
  var statusEl = document.getElementById("v3d-status");
  var infoEl = document.getElementById("v3d-info");
  var treeEl = document.getElementById("v3d-tree");
  var measureList = document.getElementById("v3d-measures");
  if (!wrap || !window.OV) return;

  window.OV_OCCT_BASE = location.origin + cfg.occtBase;
  var V3 = null;              // THREE.Vector3-constructor (uit de camera van de viewer)
  var viewer = null, model = null;
  var hidden = new Set();     // verborgen mesh-instanties (key "nodeId:meshIndex")
  var selected = null;
  var mode = "draai";         // draai | punt | vlak
  var pending = null;         // eerste klik van een meting
  var measures = [];          // laatste metingen (max. 10); alleen de gekozen meting staat in beeld
  var shown = -1, measureNo = 0, MAX_MEASURES = 10;
  function addMeasure(m) {
    m.no = ++measureNo; measures.push(m);
    if (measures.length > MAX_MEASURES) measures.shift();
    shown = measures.length - 1;
  }
  var section = { on: false, axis: "z", flip: false, pos: 0, min: 0, max: 0 };

  function setStatus(text, kind) {
    if (!text) { statusEl.hidden = true; return; }
    statusEl.hidden = false;
    statusEl.className = "v3d-status " + (kind || "");
    statusEl.innerHTML = text;
  }
  function info(text) { infoEl.textContent = text || ""; infoEl.hidden = !text; }
  function fmt(n, d) { return n.toLocaleString("nl-NL", { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function keyOf(ud) { var id = ud && ud.originalMeshInstance && ud.originalMeshInstance.id; return id ? id.nodeId + ":" + id.meshIndex : ""; }

  // Engine-teksten in het Nederlands (de engine zelf blijft Engels voor de foutherkenning hieronder)
  var NL = [
    [/Failed to import model\./g, "Inlezen van het model mislukt."],
    [/Failed to load file for import\./g, "Ophalen van het bestand mislukt."],
    [/Failed to load occt-import-js\./g, "De STEP-lezer kon niet worden geladen of is vastgelopen."],
    [/No importable file found\./g, "Geen bruikbaar 3D-bestand gevonden."],
    [/No importable object found\./g, "Geen bruikbaar 3D-object gevonden."],
    [/The model contains no faces\./g, "Het model bevat geen vlakken."],
    [/The model contains no vertices\./g, "Het model bevat geen punten."],
    [/The model doesn't contain any (3D )?meshes\.[^)]*/g, "Het model bevat geen 3D-geometrie."],
    [/Unsupported extension: ([^.]*)\./g, "Bestandstype niet ondersteund: $1."],
    [/Unknown error\./g, "Onbekende fout."],
    [/Invalid [^.)]*\./g, "Ongeldig of beschadigd bestand."],
    [/Failed to parse [^.)]*\./g, "Bestand kon niet worden gelezen."],
    [/import mislukt/g, "het bestand kon niet worden gelezen"],
    [/(Uncaught )?RuntimeError: ?Aborted\([^)]*\)/g, "interne fout van de STEP-lezer"],
    [/(Uncaught )?RuntimeError: ?/g, "interne fout: "],
    [/out of memory|OOM/gi, "geheugen vol"]
  ];
  function nl(t) { t = String(t || ""); NL.forEach(function (r) { t = t.replace(r[0], r[1]); }); return t.replace(/[<>&]/g, "").slice(0, 200); }
  if (OV.SetLocalizedStrings && OV.SetLanguageCode) {
    OV.SetLocalizedStrings({ "Mesh {0}": { nl: "Deel {0}" }, "Unknown": { nl: "Onbekend" }, "Mesh": { nl: "Deel" }, "Material": { nl: "Materiaal" },
      "True": { nl: "Ja" }, "False": { nl: "Nee" }, "Type": { nl: "Soort" }, "Properties": { nl: "Eigenschappen" } });
    OV.SetLanguageCode("nl");
  }

  var ev = new OV.EmbeddedViewer(wrap, {
    backgroundColor: new OV.RGBAColor(244, 246, 250, 255),
    defaultColor: new OV.RGBColor(190, 196, 208),
    edgeSettings: new OV.EdgeSettings(false, new OV.RGBColor(20, 22, 58), 25),
    onModelLoaded: onLoaded
  });

  // voortgang/fouten van de engine opvangen
  var occtOk = false, watch = null, t0 = Date.now(), lastText = "", isStep = /\.(step|stp|iges|igs)$/i.test(cfg.url || cfg.title || "");
  function elapsed() { var s = Math.round((Date.now() - t0) / 1000); return Math.floor(s / 60) + ":" + ("0" + s % 60).slice(-2); }
  function fail(msg) {
    clearInterval(watch);
    setStatus("Het model kon niet worden geopend. " + msg + (cfg.url ? ' <a href="' + cfg.url + (cfg.url.indexOf("?") < 0 ? "?" : "&") + 'download=1">Bestand downloaden</a>' : ""), "error");
  }
  function poll() {
    if (window.WP3D_occtError) {
      var err = String(window.WP3D_occtError);
      fail(/memory|alloc|OOM|abort/i.test(err) ? "Het bestand is te groot voor de browser (geheugen vol). Exporteer een lichtere STEP (bijv. zonder bevestigingsmateriaal) of open het in eDrawings." :
        "De STEP-lezer gaf een fout: " + nl(err) + ".");
      return;
    }
    var pd = ev.progressDiv;
    if (pd && pd.innerHTML) {
      var t = pd.textContent || "";
      pd.style.display = "none";
      if (/error|failed|no importable|contain/i.test(t) && !/model\.\.\./i.test(t)) {
        var why = /no importable/i.test(t) ? "Dit bestandstype wordt niet ondersteund." :
          /load file for import/i.test(t) ? "Het bestand kon niet van de server of SharePoint worden opgehaald. Probeer het opnieuw; blijft het fout gaan, download het bestand dan en open het via 3D-modellen > Bestand van deze computer." :
          /no faces|no vertices|any meshes|no importable object/i.test(t) ? "Het bestand bevat geen vlakken (bijvoorbeeld alleen lijnen, punten of een lege samenstelling). Exporteer vanuit SolidWorks als STEP AP214 met de solids/bodies aan." :
          /occt/i.test(t) ? (occtOk ? "De STEP-lezer is vastgelopen tijdens het inlezen, meestal omdat het bestand te groot of te complex is voor de browser. Exporteer een lichtere STEP of open het in eDrawings." :
            "De STEP/IGES-lezer kon niet worden geladen op de server (zie README, 3D-modellen).") :
          isStep ? "De STEP-lezer kon dit bestand niet verwerken. Exporteer het opnieuw als STEP AP214 (solids) en probeer het nog eens." :
          "Controleer of het een geldig STEP-, IGES-, STL-, OBJ- of 3MF-bestand is.";
        fail(why + "<br><span class='small muted'>Technische melding: " + nl(t) + "</span>");
        return;
      }
      lastText = t;
    }
    if (!lastText) return;
    var txt = lastText.replace("Loading model...", "Model ophalen…").replace("Importing model...", "Model inlezen…").replace("Visualizing model...", "Model opbouwen…");
    var sec = (Date.now() - t0) / 1000, extra = "";
    if (/Importing/.test(lastText) && isStep) {
      extra = sec < 60 ? "<br><span class='small muted'>Grote STEP-bestanden kunnen even duren.</span>" :
        "<br><span class='small muted'>Nog bezig. Een grote samenstelling kan enkele minuten duren. Duurt het langer dan ca. 5 minuten, " +
        "dan is het bestand waarschijnlijk te zwaar voor de browser.</span>";
    }
    setStatus(txt + " <b>" + elapsed() + "</b>" + extra);
  }
  function startWatch() { clearInterval(watch); t0 = Date.now(); lastText = ""; window.WP3D_occtError = null; watch = setInterval(poll, 250); }
  startWatch();

  // STEP/IGES: eerst controleren of de lezer op de server staat
  function checkOcct() {
    return Promise.all(["occt-import-js-worker.js", "occt-import-js.wasm"].map(function (f) {
      return fetch(cfg.occtBase + f, { method: "HEAD", cache: "no-store" }).then(function (r) { return r.ok; }).catch(function () { return false; });
    })).then(function (oks) { return oks[0] && oks[1]; });
  }

  function load() {
    if (cfg.url) {
      setStatus("Model ophalen…" + (cfg.sizeMb ? " (" + fmt(cfg.sizeMb, 1) + " MB)" : ""));
      if (isStep) {
        checkOcct().then(function (ok) {
          occtOk = ok;
          if (!ok) { fail("De STEP/IGES-lezer ontbreekt op de server (map static/3d/occt, zie README, 3D-modellen). STL/OBJ/3MF werken wel."); return; }
          startWatch(); loadRemote();
        });
      } else {
        loadRemote();
      }
    } else {
      setStatus("Kies of sleep een 3D-bestand (STEP, IGES, STL, OBJ of 3MF) om het te bekijken. Het bestand wordt niet geüpload.", "hint");
    }
  }

  // ------------------------------------------------------------ grote bestanden op het apparaat bewaren
  // Boven de ingestelde grootte wordt het model één keer gedownload en in de opslag van de browser op dit
  // apparaat gezet (pc, telefoon, iPad). Daarna opent de viewer het vanaf het apparaat. Een nieuwe versie in
  // SharePoint heeft een andere sleutel en wordt dus opnieuw opgehaald; de oude versie wordt dan opgeruimd.
  var CACHE = "wp3d-modellen-v1";
  function cacheable() {
    return !!(cfg.cacheKey && window.caches && window.isSecureContext !== false && cfg.sizeMb >= (cfg.cacheMb || 0));
  }
  function cacheUrl(key) { return location.origin + "/__wp3d_cache/" + encodeURI(key); }
  function fileName() { return (cfg.title || "model").replace(/[\\/]/g, "_"); }
  function loadBlob(blob, fromDevice) {
    var file = new File([blob], fileName(), { type: "application/octet-stream" });
    setStatus(fromDevice ? "Model openen vanaf dit apparaat…" : "Model inlezen…");
    startWatch();
    ev.LoadModelFromFileList([file]);
    info(fromDevice ? "Geopend vanaf dit apparaat (niet opnieuw gedownload)." : "Model is op dit apparaat bewaard. De volgende keer opent het direct.");
    setTimeout(function () { if (/apparaat/.test(infoEl.textContent)) info(""); }, 7000);
  }
  function download(onProgress) {
    return fetch(cfg.url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      var total = +(r.headers.get("Content-Length") || 0) || (cfg.sizeMb * 1048576), got = 0;
      if (!r.body || !r.body.getReader) return r.blob();
      var reader = r.body.getReader(), parts = [];
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) return new Blob(parts);
          parts.push(res.value); got += res.value.length; onProgress(got, total);
          return pump();
        });
      }
      return pump();
    });
  }
  function loadRemote() {
    if (!cacheable()) { ev.LoadModelFromUrlList([cfg.url]); return; }
    var key = cacheUrl(cfg.cacheKey), base = cacheUrl(cfg.cacheKey.split("?v=")[0]), store;
    caches.open(CACHE).then(function (c) {
      store = c;
      return c.match(key);
    }).then(function (hit) {
      if (hit) return hit.blob().then(function (b) { loadBlob(b, true); });
      clearInterval(watch);
      setStatus("Groot bestand (" + fmt(cfg.sizeMb, 1) + " MB): eenmalig downloaden naar dit apparaat…");
      return download(function (got, total) {
        setStatus("Groot bestand eenmalig downloaden naar dit apparaat… <b>" + Math.min(99, Math.round(got / total * 100)) + "%</b>" +
          "<br><span class='small muted'>" + fmt(got / 1048576, 1) + " van " + fmt(total / 1048576, 1) + " MB. Daarna opent het de volgende keer direct.</span>");
      }).then(function (blob) {
        var saved = store.keys().then(function (keys) {  // oude versies van hetzelfde bestand opruimen
          return Promise.all(keys.filter(function (k) { return k.url !== key && k.url.split("?v=")[0] === base; }).map(function (k) { return store.delete(k); }));
        }).then(function () {
          return store.put(key, new Response(blob, { headers: { "Content-Type": "application/octet-stream", "X-WP-Name": encodeURIComponent(fileName()),
            "X-WP-Saved": new Date().toISOString(), "Content-Length": String(blob.size) } }));
        }).catch(function () { /* opslag vol of geweigerd: gewoon tonen */ });
        if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {});
        return saved.then(function () { loadBlob(blob, false); });
      });
    }).catch(function () {
      // opslag niet beschikbaar (privévenster, geen ruimte): gewoon rechtstreeks laden
      if (isStep) startWatch();
      ev.LoadModelFromUrlList([cfg.url]);
    });
  }

  // ------------------------------------------------------------ na laden
  function onLoaded() {
    clearInterval(watch);
    setStatus(null);
    viewer = ev.GetViewer();
    model = ev.GetModel();
    V3 = viewer.camera.position.constructor;
    viewer.SetUpVector(OV.Direction.Z, false);
    var origRender = viewer.Render.bind(viewer);
    viewer.Render = function () { origRender(); drawOverlay(); };
    viewer.SetMouseClickHandler(onClick);
    hidden.clear(); measures = []; shown = -1; pending = null; selected = null;
    buildTree();
    initSection();
    setView("iso");
    renderMeasures();
    document.body.classList.add("v3d-loaded");
  }

  // ------------------------------------------------------------ aanzichten
  var DIRS = { iso: [1, -1, 0.8], voor: [0, -1, 0], achter: [0, 1, 0], links: [-1, 0, 0], rechts: [1, 0, 0], boven: [0, 0, 1], onder: [0, 0, -1] };
  function visibleSphere() { return viewer.GetBoundingSphere(function (ud) { return !hidden.has(keyOf(ud)); }); }
  function setView(name) {
    if (!viewer) return;
    var d = DIRS[name] || DIRS.iso;
    var s = visibleSphere() || viewer.GetBoundingSphere(function () { return true; });
    if (!s) return;
    var cam = viewer.GetCamera().Clone();
    var len = Math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
    cam.center = new OV.Coord3D(s.center.x, s.center.y, s.center.z);
    cam.eye = new OV.Coord3D(s.center.x + d[0] / len * s.radius * 3, s.center.y + d[1] / len * s.radius * 3, s.center.z + d[2] / len * s.radius * 3);
    cam.up = (name === "boven" || name === "onder") ? new OV.Coord3D(0, 1, 0) : new OV.Coord3D(0, 0, 1);
    viewer.SetCamera(cam);
    viewer.FitSphereToWindow(s, false);
  }

  // ------------------------------------------------------------ onderdelen (boom)
  function nodeKeys(node, out) {
    node.GetMeshIndices().forEach(function (mi) { out.push(node.GetId() + ":" + mi); });
    node.GetChildNodes().forEach(function (c) { nodeKeys(c, out); });
    return out;
  }
  function nodeLabel(node) {
    var n = node.GetName();
    if (!n && node.GetMeshIndices().length === 1 && node.GetChildNodes().length === 0) n = model.GetMesh(node.GetMeshIndices()[0]).GetName();
    return n || "(naamloos)";
  }
  function buildTree() {
    treeEl.innerHTML = "";
    var root = model.GetRootNode();
    var ul = document.createElement("ul");
    var count = 0;
    function add(node, parentUl, depth) {
      var kids = node.GetChildNodes(), meshes = node.GetMeshIndices();
      if (!kids.length && !meshes.length) return;
      // tussenliggende knoop zonder naam en met één kind: overslaan
      if (!node.GetName() && kids.length === 1 && !meshes.length && depth > 0) { add(kids[0], parentUl, depth); return; }
      var li = document.createElement("li");
      var keys = nodeKeys(node, []);
      li.dataset.keys = keys.join(",");
      var row = document.createElement("div");
      row.className = "v3d-node";
      var hasKids = kids.length > 0 || meshes.length > 1;
      row.innerHTML = (hasKids ? '<button type="button" class="tw" aria-label="Open/dicht">▸</button>' : '<span class="tw"></span>') +
        '<label><input type="checkbox" checked> <span class="nm"></span></label>' +
        '<button type="button" class="solo" title="Alleen dit onderdeel tonen">◎</button>';
      row.querySelector(".nm").textContent = depth === 0 ? (cfg.title || nodeLabel(node)) : nodeLabel(node);
      li.appendChild(row);
      count++;
      if (hasKids) {
        var sub = document.createElement("ul");
        sub.hidden = depth > 0;
        if (depth === 0) row.querySelector(".tw").textContent = "▾";
        kids.forEach(function (k) { add(k, sub, depth + 1); });
        if (meshes.length > 1) meshes.forEach(function (mi) {
          var l2 = document.createElement("li"); l2.dataset.keys = node.GetId() + ":" + mi;
          l2.innerHTML = '<div class="v3d-node"><span class="tw"></span><label><input type="checkbox" checked> <span class="nm"></span></label><button type="button" class="solo" title="Alleen dit onderdeel tonen">◎</button></div>';
          l2.querySelector(".nm").textContent = model.GetMesh(mi).GetName() || "deel " + (mi + 1);
          sub.appendChild(l2); count++;
        });
        li.appendChild(sub);
      }
      parentUl.appendChild(li);
    }
    add(root, ul, 0);
    treeEl.appendChild(ul);
    document.getElementById("v3d-partcount").textContent = model.MeshInstanceCount ? model.MeshInstanceCount() : count;
  }
  treeEl.addEventListener("click", function (e) {
    var li = e.target.closest("li"); if (!li) return;
    if (e.target.classList.contains("tw") && e.target.tagName === "BUTTON") {
      var sub = li.querySelector(":scope > ul"); sub.hidden = !sub.hidden; e.target.textContent = sub.hidden ? "▸" : "▾"; return;
    }
    if (e.target.classList.contains("solo")) {
      var keep = new Set(li.dataset.keys.split(","));
      hidden.clear();
      viewer.EnumerateMeshesAndLinesUserData(function (ud) { var k = keyOf(ud); if (!keep.has(k)) hidden.add(k); });
      applyVisibility(); setView("iso"); return;
    }
    if (e.target.classList.contains("nm")) { select(li.dataset.keys.split(",")); }
  });
  treeEl.addEventListener("change", function (e) {
    if (e.target.type !== "checkbox") return;
    var li = e.target.closest("li");
    li.dataset.keys.split(",").forEach(function (k) { if (e.target.checked) hidden.delete(k); else hidden.add(k); });
    applyVisibility();
  });
  function applyVisibility() {
    viewer.SetMeshesVisibility(function (ud) { return !hidden.has(keyOf(ud)); });
    treeEl.querySelectorAll("li").forEach(function (li) {
      var ks = li.dataset.keys.split(","), n = ks.filter(function (k) { return hidden.has(k); }).length;
      var cb = li.querySelector(":scope > .v3d-node input");
      cb.checked = n < ks.length; cb.indeterminate = n > 0 && n < ks.length;
    });
    viewer.Render();
  }
  document.getElementById("v3d-showall").addEventListener("click", function () { hidden.clear(); if (viewer) { applyVisibility(); } });
  function select(keys) {
    selected = keys ? new Set(keys) : null;
    viewer.SetMeshesHighlight(new OV.RGBColor(0, 128, 255), function (ud) { return !!selected && selected.has(keyOf(ud)); });
    treeEl.querySelectorAll(".v3d-node.sel").forEach(function (n) { n.classList.remove("sel"); });
    if (keys && keys.length === 1) {
      var li = treeEl.querySelector('li[data-keys="' + keys[0] + '"]');
      if (li) {
        var p = li.parentElement;
        while (p && p !== treeEl) { if (p.tagName === "UL") p.hidden = false; p = p.parentElement; }
        li.querySelector(".v3d-node").classList.add("sel");
        li.scrollIntoView({ block: "nearest" });
        info(li.querySelector(".nm").textContent);
      }
    } else info("");
  }

  // ------------------------------------------------------------ klikken: selecteren of meten
  function hitAt(coords) {
    var h = viewer.GetMeshIntersectionUnderMouse(OV.IntersectionMode.MeshOnly, coords);
    if (!h || !h.face) return null;
    var n = h.face.normal.clone().transformDirection(h.object.matrixWorld).normalize();
    return { p: h.point.clone(), n: n, ud: h.object.userData, obj: h.object, tri: h.faceIndex };
  }
  function clipped(p) {
    if (!section.on) return false;
    var v = section.axis === "x" ? p.x : section.axis === "y" ? p.y : p.z;
    return section.flip ? v < section.pos : v > section.pos;
  }

  // ------------------------------------------------------------ topologie (vlakken en randen uit het beeldmodel)
  var COS_PLANAR = Math.cos(0.5 * Math.PI / 180), COS_SHARP = Math.cos(25 * Math.PI / 180);  // zelfde grens als 'Randen': alleen echte randen
  function topo(obj) {
    if (obj.userData.wpTopo !== undefined) return obj.userData.wpTopo;
    var geo = obj.geometry, pos = geo.attributes.position, idx = geo.index;
    var triCount = idx ? idx.count / 3 : pos.count / 3;
    if (triCount > 800000) { obj.userData.wpTopo = null; return null; }
    var m = obj.matrixWorld.elements, n = pos.count, P = new Float64Array(n * 3), i;
    var minx = Infinity, miny = Infinity, minz = Infinity, maxx = -Infinity, maxy = -Infinity, maxz = -Infinity;
    for (i = 0; i < n; i++) {
      var x = pos.getX(i), y = pos.getY(i), z = pos.getZ(i);
      var wx = m[0] * x + m[4] * y + m[8] * z + m[12], wy = m[1] * x + m[5] * y + m[9] * z + m[13], wz = m[2] * x + m[6] * y + m[10] * z + m[14];
      P[i * 3] = wx; P[i * 3 + 1] = wy; P[i * 3 + 2] = wz;
      if (wx < minx) minx = wx; if (wy < miny) miny = wy; if (wz < minz) minz = wz;
      if (wx > maxx) maxx = wx; if (wy > maxy) maxy = wy; if (wz > maxz) maxz = wz;
    }
    var diag = Math.hypot(maxx - minx, maxy - miny, maxz - minz) || 1, q = diag * 1e-6;
    var map = new Map(), vid = new Int32Array(n), W = [];
    for (i = 0; i < n; i++) {
      var k = Math.round(P[i * 3] / q) + "," + Math.round(P[i * 3 + 1] / q) + "," + Math.round(P[i * 3 + 2] / q);
      var id = map.get(k);
      if (id === undefined) { id = W.length / 3; map.set(k, id); W.push(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]); }
      vid[i] = id;
    }
    var T = new Int32Array(triCount * 3), N = new Float64Array(triCount * 3), E = new Map();
    for (var t = 0; t < triCount; t++) {
      for (var c = 0; c < 3; c++) T[t * 3 + c] = vid[idx ? idx.getX(t * 3 + c) : t * 3 + c];
      var a0 = T[t * 3] * 3, b0 = T[t * 3 + 1] * 3, c0 = T[t * 3 + 2] * 3;
      var ux = W[b0] - W[a0], uy = W[b0 + 1] - W[a0 + 1], uz = W[b0 + 2] - W[a0 + 2];
      var vx = W[c0] - W[a0], vy = W[c0 + 1] - W[a0 + 1], vz = W[c0 + 2] - W[a0 + 2];
      var nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx, l = Math.hypot(nx, ny, nz) || 1;
      N[t * 3] = nx / l; N[t * 3 + 1] = ny / l; N[t * 3 + 2] = nz / l;
      for (c = 0; c < 3; c++) {
        var va = T[t * 3 + c], vb = T[t * 3 + (c + 1) % 3], ek = va < vb ? va + "," + vb : vb + "," + va;
        var list = E.get(ek); if (list) list.push(t); else E.set(ek, [t]);
      }
    }
    obj.userData.wpTopo = { W: W, T: T, N: N, E: E, tol: diag * 1e-5 };
    return obj.userData.wpTopo;
  }
  function vtx(tp, i) { return new V3(tp.W[i * 3], tp.W[i * 3 + 1], tp.W[i * 3 + 2]); }
  function ndot(tp, a, b) { return tp.N[a * 3] * tp.N[b * 3] + tp.N[a * 3 + 1] * tp.N[b * 3 + 1] + tp.N[a * 3 + 2] * tp.N[b * 3 + 2]; }
  function triEdges(tp, t) {
    var out = [];
    for (var c = 0; c < 3; c++) { var a = tp.T[t * 3 + c], b = tp.T[t * 3 + (c + 1) % 3]; out.push(a < b ? [a, b] : [b, a]); }
    return out;
  }
  // vlak = aaneengesloten driehoeken met (vrijwel) dezelfde normaal
  function faceRegion(tp, t0) {
    var seen = new Set([t0]), stack = [t0], tris = [];
    while (stack.length && tris.length < 200000) {
      var t = stack.pop(); tris.push(t);
      triEdges(tp, t).forEach(function (e) {
        (tp.E.get(e[0] + "," + e[1]) || []).forEach(function (u) {
          if (!seen.has(u) && ndot(tp, u, t0) > COS_PLANAR) { seen.add(u); stack.push(u); }
        });
      });
    }
    // rand van het vlak: randen die maar bij één driehoek van het vlak horen
    var cnt = new Map();
    tris.forEach(function (t) { triEdges(tp, t).forEach(function (e) { var k = e[0] + "," + e[1]; cnt.set(k, (cnt.get(k) || 0) + 1); }); });
    var border = [];
    cnt.forEach(function (v, k) { if (v === 1) { var ab = k.split(","); border.push([+ab[0], +ab[1]]); } });
    var n = new V3(tp.N[t0 * 3], tp.N[t0 * 3 + 1], tp.N[t0 * 3 + 2]);
    return { tris: tris, set: seen, border: border, n: n };
  }
  function isSharp(tp, e) {
    var l = tp.E.get(e[0] + "," + e[1]) || [];
    return l.length !== 2 || ndot(tp, l[0], l[1]) < COS_SHARP;
  }
  function segDist(p, a, b) {
    var ab = b.clone().sub(a), t = ab.lengthSq() ? Math.max(0, Math.min(1, p.clone().sub(a).dot(ab) / ab.lengthSq())) : 0;
    return p.distanceTo(a.clone().add(ab.multiplyScalar(t)));
  }
  // rechte lijn = scherpe rand van het aangeklikte vlak, doorgetrokken over rechte stukken
  function vertEdges(tp) {
    if (tp.VE) return tp.VE;
    var VE = new Map();
    tp.E.forEach(function (tris, k) {
      var ab = k.split(","), e = [+ab[0], +ab[1]];
      [e[0], e[1]].forEach(function (v) { var l = VE.get(v); if (l) l.push(e); else VE.set(v, [e]); });
    });
    tp.VE = VE;
    return VE;
  }
  var ROUND_EDGE = "Dit is een ronde rand. Gebruik Diameter om die te meten.";
  // rechte lijn = echte (scherpe) rand bij de klik, doorgetrokken over rechte stukken.
  // Hulplijnen van het beeldmodel (tussen de facetten van een rond vlak) tellen niet mee.
  function pickLine(h) {
    var tp = topo(h.obj); if (!tp) return null;
    var reg = faceRegion(tp, h.tri);
    var cand = reg.border.filter(function (e) { return isSharp(tp, e); });
    triEdges(tp, h.tri).forEach(function (e) { if (isSharp(tp, e)) cand.push(e); });
    if (!cand.length) return { err: "Geen rechte rand bij deze klik. Klik op een vlak, dicht bij een echte rand." };
    var best = null, bd = Infinity;
    cand.forEach(function (e) { var d = segDist(h.p, vtx(tp, e[0]), vtx(tp, e[1])); if (d < bd) { bd = d; best = e; } });
    var a = vtx(tp, best[0]), b = vtx(tp, best[1]), dir = b.clone().sub(a).normalize();
    var VE = vertEdges(tp), used = new Set([best[0] + "," + best[1]]), ends = [best[0], best[1]];
    // doortrekken langs echte randen met dezelfde richting
    for (var s = 0; s < 2; s++) {
      var grew = true, guard = 0;
      while (grew && guard++ < 100000) {
        grew = false;
        var at = ends[s], list = VE.get(at) || [];
        for (var i = 0; i < list.length; i++) {
          var e = list[i], k = e[0] + "," + e[1];
          if (used.has(k) || !isSharp(tp, e)) continue;
          var other = e[0] === at ? e[1] : e[0];
          if (Math.abs(vtx(tp, other).sub(vtx(tp, at)).normalize().dot(dir)) > 0.99995) { ends[s] = other; used.add(k); grew = true; break; }
        }
      }
    }
    // loopt de rand aan een uiteinde met een kleine knik verder, dan is het een boog en geen rechte lijn
    var COS_BEND = Math.cos(30 * Math.PI / 180), COS_STRAIGHT = Math.cos(0.5 * Math.PI / 180);
    for (s = 0; s < 2; s++) {
      var at2 = ends[s], out = vtx(tp, at2).sub(vtx(tp, ends[1 - s])).normalize();
      var bend = (VE.get(at2) || []).some(function (e) {
        if (used.has(e[0] + "," + e[1]) || !isSharp(tp, e)) return false;
        var o = e[0] === at2 ? e[1] : e[0], d = vtx(tp, o).sub(vtx(tp, at2)).normalize().dot(out);
        return d > COS_BEND && d < COS_STRAIGHT;
      });
      if (bend) return { err: ROUND_EDGE };
    }
    return { a: vtx(tp, ends[0]), b: vtx(tp, ends[1]), dir: dir, segs: [[vtx(tp, ends[0]), vtx(tp, ends[1])]] };
  }
  function pickFace(h) {
    var tp = topo(h.obj);
    if (!tp) return { n: h.n, c: h.p, pts: [h.p], tris: [], segs: [], tp: null };
    var reg = faceRegion(tp, h.tri), c = new V3(0, 0, 0), vs = new Set();
    var soft = reg.border.filter(function (e) { return !isSharp(tp, e); }).length;
    if (reg.border.length && soft / reg.border.length > 0.3) return { err: "Dit is een rond vlak. Gebruik Diameter om het te meten." };
    reg.tris.forEach(function (t) { for (var k = 0; k < 3; k++) vs.add(tp.T[t * 3 + k]); });
    var pts = []; vs.forEach(function (v) { pts.push(vtx(tp, v)); c.add(pts[pts.length - 1]); });
    c.multiplyScalar(1 / pts.length);
    var segs = reg.border.filter(function (e) { return isSharp(tp, e); }).slice(0, 4000).map(function (e) { return [vtx(tp, e[0]), vtx(tp, e[1])]; });
    return { n: reg.n, c: c, pts: pts, tris: reg.tris, tp: tp, segs: segs, planar: reg.tris.length > 1 };
  }
  // ------------------------------------------------------------ diameter / radius
  function eigSym3(A) {  // Jacobi: eigenwaarden en -vectoren van een symmetrische 3x3
    var a = [A[0].slice(), A[1].slice(), A[2].slice()], v = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    for (var it = 0; it < 50; it++) {
      var p = 0, q = 1, mx = Math.abs(a[0][1]);
      if (Math.abs(a[0][2]) > mx) { p = 0; q = 2; mx = Math.abs(a[0][2]); }
      if (Math.abs(a[1][2]) > mx) { p = 1; q = 2; mx = Math.abs(a[1][2]); }
      if (mx < 1e-12) break;
      var th = (a[q][q] - a[p][p]) / (2 * a[p][q]), t = (th >= 0 ? 1 : -1) / (Math.abs(th) + Math.sqrt(th * th + 1));
      var c = 1 / Math.sqrt(t * t + 1), sn = t * c;
      for (var k = 0; k < 3; k++) {
        var akp = a[k][p], akq = a[k][q]; a[k][p] = c * akp - sn * akq; a[k][q] = sn * akp + c * akq;
      }
      for (k = 0; k < 3; k++) {
        var apk = a[p][k], aqk = a[q][k]; a[p][k] = c * apk - sn * aqk; a[q][k] = sn * apk + c * aqk;
      }
      for (k = 0; k < 3; k++) {
        var vkp = v[k][p], vkq = v[k][q]; v[k][p] = c * vkp - sn * vkq; v[k][q] = sn * vkp + c * vkq;
      }
    }
    return [0, 1, 2].map(function (i) { return { val: a[i][i], vec: new V3(v[0][i], v[1][i], v[2][i]) }; }).sort(function (x, y) { return x.val - y.val; });
  }
  function planeBasis(n) {
    var u = Math.abs(n.x) < 0.9 ? new V3(1, 0, 0) : new V3(0, 1, 0);
    u = u.sub(n.clone().multiplyScalar(u.dot(n))).normalize();
    return { u: u, v: n.clone().cross(u).normalize() };
  }
  // cirkel door punten (in het vlak loodrecht op 'axis'), kleinste kwadraten
  function fitCircle(pts, axis) {
    var b = planeBasis(axis), o = pts[0], n = pts.length, P = [], mu = 0, mv = 0;
    pts.forEach(function (p) { var d = p.clone().sub(o), x = d.dot(b.u), y = d.dot(b.v); P.push([x, y]); mu += x; mv += y; });
    mu /= n; mv /= n;
    var suu = 0, svv = 0, suv = 0, suuu = 0, svvv = 0, suvv = 0, svuu = 0;
    P.forEach(function (q) { var x = q[0] - mu, y = q[1] - mv; suu += x * x; svv += y * y; suv += x * y; suuu += x * x * x; svvv += y * y * y; suvv += x * y * y; svuu += y * x * x; });
    var det = suu * svv - suv * suv;
    if (Math.abs(det) < 1e-12) return null;
    var r1 = 0.5 * (suuu + suvv), r2 = 0.5 * (svvv + svuu);
    var uc = (r1 * svv - r2 * suv) / det, vc = (suu * r2 - suv * r1) / det;
    var r = Math.sqrt(uc * uc + vc * vc + (suu + svv) / n);
    var cx = uc + mu, cy = vc + mv, err = 0, angs = [];
    P.forEach(function (q) { var dx = q[0] - cx, dy = q[1] - cy; err += Math.pow(Math.hypot(dx, dy) - r, 2); angs.push(Math.atan2(dy, dx)); });
    angs.sort(function (x, y) { return x - y; });
    var gap = angs[0] + 2 * Math.PI - angs[angs.length - 1];
    for (var i = 1; i < angs.length; i++) gap = Math.max(gap, angs[i] - angs[i - 1]);
    var center = o.clone().add(b.u.clone().multiplyScalar(cx)).add(b.v.clone().multiplyScalar(cy));
    return { c: center, r: r, rms: Math.sqrt(err / n), cover: 360 - gap * 180 / Math.PI, u: b.u, v: b.v, axis: axis };
  }
  function circleSegs(f, k) {
    var out = [], prev = null;
    for (var i = 0; i <= (k || 72); i++) {
      var a = i / (k || 72) * 2 * Math.PI;
      var p = f.c.clone().add(f.u.clone().multiplyScalar(Math.cos(a) * f.r)).add(f.v.clone().multiplyScalar(Math.sin(a) * f.r));
      if (prev) out.push([prev, p]); prev = p;
    }
    return out;
  }
  // randlussen van een vlak, de lus die het dichtst bij de klik ligt
  function nearestLoop(tp, border, p) {
    var adj = new Map();
    border.forEach(function (e, i) { [e[0], e[1]].forEach(function (v) { var l = adj.get(v); if (l) l.push(i); else adj.set(v, [i]); }); });
    var comp = new Int32Array(border.length).fill(-1), loops = [];
    for (var i = 0; i < border.length; i++) {
      if (comp[i] >= 0) continue;
      var stack = [i], L = []; comp[i] = loops.length;
      while (stack.length) {
        var e = stack.pop(); L.push(e);
        [border[e][0], border[e][1]].forEach(function (v) { adj.get(v).forEach(function (f) { if (comp[f] < 0) { comp[f] = loops.length; stack.push(f); } }); });
      }
      loops.push(L);
    }
    var best = null, bd = Infinity;
    loops.forEach(function (L) {
      L.forEach(function (e) { var d = segDist(p, vtx(tp, border[e][0]), vtx(tp, border[e][1])); if (d < bd) { bd = d; best = L; } });
    });
    return best ? best.map(function (e) { return border[e]; }) : null;
  }
  function pickRound(h) {
    var tp = topo(h.obj); if (!tp) return null;
    var cand = [];
    // 1) rond vlak (cilinder/boring): aaneengesloten gladde driehoeken rond één as
    var seen = new Set([h.tri]), stack = [h.tri], tris = [], COS_SMOOTH = Math.cos(32 * Math.PI / 180);
    while (stack.length && tris.length < 60000) {
      var t = stack.pop(); tris.push(t);
      triEdges(tp, t).forEach(function (e) {
        (tp.E.get(e[0] + "," + e[1]) || []).forEach(function (u) { if (!seen.has(u) && ndot(tp, u, t) > COS_SMOOTH) { seen.add(u); stack.push(u); } });
      });
    }
    var M = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
    tris.forEach(function (t) {
      var n = [tp.N[t * 3], tp.N[t * 3 + 1], tp.N[t * 3 + 2]];
      for (var i = 0; i < 3; i++) for (var j = 0; j < 3; j++) M[i][j] += n[i] * n[j];
    });
    var eg = eigSym3(M);
    if (tris.length >= 4 && eg[1].val > 0.02 * tris.length) {
      var axis = eg[0].vec.normalize(), vs = new Set(), pts = [];
      tris.forEach(function (t) { for (var k = 0; k < 3; k++) vs.add(tp.T[t * 3 + k]); });
      vs.forEach(function (v) { pts.push(vtx(tp, v)); });
      var f = fitCircle(pts, axis);
      if (f && f.rms < Math.max(0.02 * f.r, 0.01)) {
        f.c.add(axis.clone().multiplyScalar(h.p.clone().sub(f.c).dot(axis)));  // op de hoogte van de klik
        f.src = "vlak"; cand.push(f);
      }
    }
    // 2) ronde rand van een vlak vlak (bijv. het kopvlak van een buis of een gat in een plaat)
    if (!cand.length) {
      var reg = faceRegion(tp, h.tri), loop = nearestLoop(tp, reg.border, h.p);
      if (loop && loop.length >= 6) {
        var lv = new Set(), lp = [];
        loop.forEach(function (e) { lv.add(e[0]); lv.add(e[1]); });
        lv.forEach(function (v) { lp.push(vtx(tp, v)); });
        var g = fitCircle(lp, reg.n);
        if (g && g.rms < Math.max(0.02 * g.r, 0.01)) { g.src = "rand"; cand.push(g); }
      }
    }
    return cand[0] || null;
  }

  // kleinste afstand punt-driehoek
  function ptTri(p, a, b, c) {
    var ab = b.clone().sub(a), ac = c.clone().sub(a), ap = p.clone().sub(a);
    var d1 = ab.dot(ap), d2 = ac.dot(ap); if (d1 <= 0 && d2 <= 0) return p.distanceTo(a);
    var bp = p.clone().sub(b), d3 = ab.dot(bp), d4 = ac.dot(bp); if (d3 >= 0 && d4 <= d3) return p.distanceTo(b);
    var vc = d1 * d4 - d3 * d2; if (vc <= 0 && d1 >= 0 && d3 <= 0) return p.distanceTo(a.clone().add(ab.multiplyScalar(d1 / (d1 - d3))));
    var cp = p.clone().sub(c), d5 = ab.dot(cp), d6 = ac.dot(cp); if (d6 >= 0 && d5 <= d6) return p.distanceTo(c);
    var vb = d5 * d2 - d1 * d6; if (vb <= 0 && d2 >= 0 && d6 <= 0) return p.distanceTo(a.clone().add(ac.multiplyScalar(d2 / (d2 - d6))));
    var va = d3 * d6 - d5 * d4;
    if (va <= 0 && d4 - d3 >= 0 && d5 - d6 >= 0) return p.distanceTo(b.clone().add(c.clone().sub(b).multiplyScalar((d4 - d3) / ((d4 - d3) + (d5 - d6)))));
    var den = 1 / (va + vb + vc);
    return p.distanceTo(a.clone().add(ab.multiplyScalar(vb * den)).add(ac.multiplyScalar(vc * den)));
  }
  function minFaceDist(A, B) {
    if (!A.tp || !B.tp) return A.c.distanceTo(B.c);
    var best = Infinity;
    function oneWay(P, Q) {
      var pts = P.pts, tris = Q.tris, step = Math.max(1, Math.ceil(pts.length * tris.length / 3e6));
      for (var i = 0; i < pts.length; i += step) for (var j = 0; j < tris.length; j++) {
        var t = tris[j], d = ptTri(pts[i], vtx(Q.tp, Q.tp.T[t * 3]), vtx(Q.tp, Q.tp.T[t * 3 + 1]), vtx(Q.tp, Q.tp.T[t * 3 + 2]));
        if (d < best) best = d;
      }
    }
    oneWay(A, B); oneWay(B, A);
    return best;
  }
  // kleinste afstand tussen twee lijnstukken
  function segSeg(p1, q1, p2, q2) {
    var d1 = q1.clone().sub(p1), d2 = q2.clone().sub(p2), r = p1.clone().sub(p2);
    var a = d1.dot(d1), e = d2.dot(d2), f = d2.dot(r), s, t;
    if (a <= 1e-12 && e <= 1e-12) return p1.distanceTo(p2);
    if (a <= 1e-12) { s = 0; t = Math.max(0, Math.min(1, f / e)); }
    else {
      var c = d1.dot(r);
      if (e <= 1e-12) { t = 0; s = Math.max(0, Math.min(1, -c / a)); }
      else {
        var b = d1.dot(d2), den = a * e - b * b;
        s = den !== 0 ? Math.max(0, Math.min(1, (b * f - c * e) / den)) : 0;
        t = (b * s + f) / e;
        if (t < 0) { t = 0; s = Math.max(0, Math.min(1, -c / a)); } else if (t > 1) { t = 1; s = Math.max(0, Math.min(1, (b - c) / a)); }
      }
    }
    return p1.clone().add(d1.multiplyScalar(s)).distanceTo(p2.clone().add(d2.multiplyScalar(t)));
  }

  // ------------------------------------------------------------ klikken: selecteren of meten
  var MODE_TXT = { punt: ["het eerste punt", "het tweede punt"], vlak: ["het eerste vlak", "het tweede vlak"], lijn: ["de eerste rand (klik vlak bij een rechte rand)", "de tweede rand"], rond: ["op een ronde rand of een rond vlak", ""] };
  function onClick(button, coords) {
    if (!viewer || button !== 1 && button !== 0) return;
    var h = hitAt(coords);
    if (mode === "draai") {
      if (!h) { select(null); return; }
      select([keyOf(h.ud)]);
      return;
    }
    if (!h) { info("Klik op het model."); return; }
    if (clipped(h.p)) { info("Dat punt ligt in het weggesneden deel. Draai het model of zet de doorsnede uit."); return; }
    if (mode === "rond") {
      var f = pickRound(h);
      if (!f) { info("Geen ronde rand of rond vlak herkend. Klik op het ronde vlak zelf of vlak naast de ronde rand."); return; }
      var full = f.cover > 200;
      var m1 = { type: "rond", segs: circleSegs(f), a: f.c.clone().sub(f.u.clone().multiplyScalar(f.r)), b: f.c.clone().add(f.u.clone().multiplyScalar(f.r)),
        dist: 2 * f.r, r: f.r };
      m1.label = full ? "Ø " + fmt(2 * f.r, 2) + " mm" : "R " + fmt(f.r, 2) + " mm";
      m1.extra = (full ? "Diameter (R " + fmt(f.r, 2) + " mm)" : "Radius van een boog van ca. " + Math.round(f.cover) + "° (Ø " + fmt(2 * f.r, 2) + " mm)") +
        (f.src === "rand" ? ", gemeten op de ronde rand" : ", gemeten op het ronde vlak") + ". Benadering uit het beeldmodel.";
      addMeasure(m1); info(m1.label); renderMeasures(); viewer.Render();
      return;
    }
    var sel;
    if (mode === "punt") sel = { p: h.p, segs: [] };
    else if (mode === "vlak") sel = pickFace(h);
    else sel = pickLine(h);
    if (!sel) { info("Hier kan niet worden gemeten. Klik op een ander deel van het model."); return; }
    if (sel.err) { info(sel.err); return; }
    if (!pending) { pending = sel; shown = -1; info("Klik " + MODE_TXT[mode][1] + "."); viewer.Render(); return; }
    var A = pending, B = sel, m = { type: mode, segs: (A.segs || []).concat(B.segs || []) };
    pending = null;
    if (mode === "punt") {
      m.a = A.p; m.b = B.p; m.dist = A.p.distanceTo(B.p);
      m.dx = Math.abs(B.p.x - A.p.x); m.dy = Math.abs(B.p.y - A.p.y); m.dz = Math.abs(B.p.z - A.p.z);
      m.label = fmt(m.dist, 2) + " mm";
    } else if (mode === "vlak") {
      m.a = A.c; m.b = B.c;
      var dot = Math.max(-1, Math.min(1, A.n.dot(B.n)));
      m.angle = Math.acos(dot) * 180 / Math.PI;
      m.parallel = m.angle < 0.5 || m.angle > 179.5;
      if (m.parallel) {
        m.dist = Math.abs(B.c.clone().sub(A.c).dot(A.n));
        m.label = fmt(m.dist, 2) + " mm";
        m.extra = "Evenwijdige vlakken, loodrechte afstand";
      } else {
        m.dist = minFaceDist(A, B);
        m.label = fmt(m.angle, 1) + "° · " + fmt(m.dist, 2) + " mm";
        m.extra = "Hoek tussen de vlakken en kleinste afstand" + (m.dist < 0.005 ? " (vlakken raken elkaar)" : "");
      }
    } else {
      m.a = A.a.clone().add(A.b).multiplyScalar(0.5); m.b = B.a.clone().add(B.b).multiplyScalar(0.5);
      var cosl = Math.abs(Math.max(-1, Math.min(1, A.dir.dot(B.dir))));
      m.angle = Math.acos(cosl) * 180 / Math.PI;
      m.parallel = m.angle < 0.2;
      if (m.parallel) {
        var r = B.a.clone().sub(A.a);
        m.dist = r.clone().sub(A.dir.clone().multiplyScalar(r.dot(A.dir))).length();
        m.label = fmt(m.dist, 2) + " mm";
        m.extra = "Evenwijdige randen, afstand tussen de lijnen (lengtes " + fmt(A.a.distanceTo(A.b), 1) + " / " + fmt(B.a.distanceTo(B.b), 1) + " mm)";
      } else {
        m.dist = segSeg(A.a, A.b, B.a, B.b);
        m.label = fmt(m.angle, 1) + "° · " + fmt(m.dist, 2) + " mm";
        m.extra = "Hoek tussen de randen en kleinste afstand";
      }
    }
    addMeasure(m);
    info(m.label);
    renderMeasures();
    viewer.Render();
  }

  // ------------------------------------------------------------ meetoverlay (SVG)
  var COLORS = { punt: "#0080FF", vlak: "#E5484D", lijn: "#1FA35B", rond: "#8E4EC6" };
  function toScreen(v) {
    var cam = viewer.camera, w = wrap.clientWidth, h = wrap.clientHeight;
    var p = v.clone().project(cam);
    return { x: (p.x + 1) / 2 * w, y: (1 - p.y) / 2 * h, ok: p.z < 1 && p.z > -1 };
  }
  function segsSvg(segs, col, width) {
    var d = "";
    for (var i = 0; i < segs.length && i < 4000; i++) {
      var a = toScreen(segs[i][0]), b = toScreen(segs[i][1]);
      if (a.ok && b.ok) d += "M" + a.x.toFixed(1) + " " + a.y.toFixed(1) + "L" + b.x.toFixed(1) + " " + b.y.toFixed(1);
    }
    return d ? '<path d="' + d + '" stroke="' + col + '" stroke-width="' + width + '" fill="none" stroke-linecap="round"/>' : "";
  }
  function drawOverlay() {
    if (!viewer || !V3) return;
    var w = wrap.clientWidth, h = wrap.clientHeight, s = "";
    overlay.setAttribute("viewBox", "0 0 " + w + " " + h);
    measures.forEach(function (m, i) {
      if (i !== shown) return;
      var col = COLORS[m.type];
      s += segsSvg(m.segs || [], col, m.type === "lijn" ? 4 : 2.5);
      var a = toScreen(m.a), b = toScreen(m.b);
      if (!a.ok || !b.ok) return;
      s += '<line x1="' + a.x + '" y1="' + a.y + '" x2="' + b.x + '" y2="' + b.y + '" stroke="' + col + '" stroke-width="2" stroke-dasharray="' + (m.type === "punt" ? "" : "6 4") + '"/>';
      s += '<circle cx="' + a.x + '" cy="' + a.y + '" r="5" fill="' + col + '" stroke="#fff" stroke-width="2"/><circle cx="' + b.x + '" cy="' + b.y + '" r="5" fill="' + col + '" stroke="#fff" stroke-width="2"/>';
      var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, label = m.label, tw = label.length * 7.2 + 14;
      s += '<g transform="translate(' + (mx - tw / 2) + ',' + (my - 26) + ')"><rect width="' + tw + '" height="22" rx="6" fill="#14163A" opacity=".88"/>' +
        '<text x="' + (tw / 2) + '" y="15" fill="#fff" font-size="12.5" font-family="Ubuntu, system-ui, sans-serif" text-anchor="middle">' + label + '</text></g>';
    });
    if (pending) {
      s += segsSvg(pending.segs || [], "#F2A516", 4);
      var q = toScreen(pending.p || pending.c || pending.a); if (q.ok) s += '<circle cx="' + q.x + '" cy="' + q.y + '" r="6" fill="#F2A516" stroke="#fff" stroke-width="2"/>';
    }
    overlay.innerHTML = s;
  }
  var TYPE_TXT = { punt: "Punt–punt", vlak: "Vlak–vlak", lijn: "Lijn–lijn", rond: "Diameter" };
  function renderMeasures() {
    if (!measures.length) { measureList.innerHTML = '<p class="muted small">Nog geen metingen. Kies <b>Punt–punt</b>, <b>Vlak–vlak</b> of <b>Lijn–lijn</b> en klik twee keer op het model, of kies <b>Diameter</b> en klik één keer op een ronde rand.</p>'; return; }
    var html = '<p class="small muted" style="margin:0 0 6px">Alleen de gekozen meting staat in beeld. Klik op een meting om die te tonen. De laatste ' + MAX_MEASURES + ' worden bewaard zolang dit scherm open is.</p>';
    for (var i = measures.length - 1; i >= 0; i--) {
      var m = measures[i], extra = m.type === "punt" ? "ΔX " + fmt(m.dx, 2) + " · ΔY " + fmt(m.dy, 2) + " · ΔZ " + fmt(m.dz, 2) + " mm" : m.extra;
      html += '<div class="v3d-m' + (i === shown ? ' on' : '') + '" data-i="' + i + '"><div class="v3d-m-t"><b>' + TYPE_TXT[m.type] + ': ' + m.label + '</b>' +
        '<button type="button" class="v3d-m-x" data-del="' + i + '" title="Meting verwijderen" aria-label="Meting verwijderen">×</button></div><div class="small muted">' + extra + '</div></div>';
    }
    measureList.innerHTML = html + '<div class="v3d-m-btns"><button type="button" class="btn sm" id="v3d-hidem">Niets tonen</button> <button type="button" class="btn sm" id="v3d-clearm">Alles wissen</button></div>';
    measureList.querySelectorAll(".v3d-m").forEach(function (el) {
      el.addEventListener("click", function (e) {
        var del = e.target.closest("[data-del]");
        if (del) { var k = +del.dataset.del; measures.splice(k, 1); if (shown === k) shown = -1; else if (shown > k) shown--; }
        else { var j = +el.dataset.i; shown = shown === j ? -1 : j; }
        pending = null; renderMeasures(); viewer.Render();
      });
    });
    document.getElementById("v3d-hidem").onclick = function () { shown = -1; pending = null; info(""); renderMeasures(); viewer.Render(); };
    document.getElementById("v3d-clearm").onclick = function () { measures = []; shown = -1; pending = null; renderMeasures(); info(""); viewer.Render(); };
  }

  // ------------------------------------------------------------ doorsnede
  var secRange = document.getElementById("v3d-sec-range");
  function initSection() {
    var bb = viewer.GetBoundingBox(function () { return true; });
    section.bb = bb;
    setAxis(section.axis);
  }
  function setAxis(ax) {
    section.axis = ax;
    if (!section.bb) return;
    section.min = section.bb.min[ax]; section.max = section.bb.max[ax];
    secRange.min = 0; secRange.max = 1000; secRange.value = 500;
    section.pos = (section.min + section.max) / 2;
    document.querySelectorAll("[data-axis]").forEach(function (b) { b.classList.toggle("on", b.dataset.axis === ax); });
    applySection();
  }
  function applySection() {
    var r = viewer.renderer;
    if (!section.on) { r.clippingPlanes = []; viewer.Render(); return; }
    var n = { x: 0, y: 0, z: 0 };
    n[section.axis] = section.flip ? 1 : -1;
    r.clippingPlanes = [{ normal: n, constant: section.flip ? -section.pos : section.pos }];
    document.getElementById("v3d-sec-val").textContent = section.axis.toUpperCase() + " = " + fmt(section.pos, 1) + " mm";
    viewer.Render();
  }
  secRange.addEventListener("input", function () {
    section.pos = section.min + (section.max - section.min) * (secRange.value / 1000);
    applySection();
  });
  document.getElementById("v3d-sec-on").addEventListener("change", function (e) {
    section.on = e.target.checked; document.getElementById("v3d-sec-ctl").hidden = !section.on; if (viewer) applySection();
  });
  document.querySelectorAll("[data-axis]").forEach(function (b) { b.addEventListener("click", function () { setAxis(b.dataset.axis); }); });
  document.getElementById("v3d-sec-flip").addEventListener("click", function () { section.flip = !section.flip; applySection(); });

  // ------------------------------------------------------------ werkbalk
  document.querySelectorAll("[data-view]").forEach(function (b) { b.addEventListener("click", function () { setView(b.dataset.view); }); });
  document.querySelectorAll("[data-mode]").forEach(function (b) {
    b.addEventListener("click", function () {
      mode = b.dataset.mode; pending = null;
      document.querySelectorAll("[data-mode]").forEach(function (x) { x.classList.toggle("on", x === b); });
      info(mode === "draai" ? "" : "Meten " + TYPE_TXT[mode].toLowerCase() + ": klik " + MODE_TXT[mode][0] + ".");
      if (mode !== "draai") openPanel("meten");
      if (viewer) viewer.Render();
    });
  });
  var edges = false, ortho = false;
  document.getElementById("v3d-edges").addEventListener("click", function (e) {
    edges = !edges; e.currentTarget.classList.toggle("on", edges);
    if (viewer) viewer.SetEdgeSettings(new OV.EdgeSettings(edges, new OV.RGBColor(20, 22, 58), 25));
  });
  document.getElementById("v3d-ortho").addEventListener("click", function (e) {
    ortho = !ortho; e.currentTarget.classList.toggle("on", ortho);
    if (viewer) { viewer.SetProjectionMode(ortho ? OV.ProjectionMode.Orthographic : OV.ProjectionMode.Perspective); viewer.Render(); }
  });
  document.getElementById("v3d-full").addEventListener("click", function () {
    var el = document.getElementById("v3d-app");
    if (document.fullscreenElement) document.exitFullscreen(); else if (el.requestFullscreen) el.requestFullscreen();
  });
  document.addEventListener("fullscreenchange", function () { setTimeout(function () { ev.Resize(); }, 60); });

  // panelen: op een groot scherm staat de onderdelenboom altijd rechts, doorsnede/metingen eronder.
  // Op telefoon/tablet staand is er één paneel tegelijk (onderin).
  var wideMq = window.matchMedia("(min-width: 900px)");
  var treeOn = wideMq.matches, active = null;
  function layoutPanels() {
    var wide = wideMq.matches, side = document.getElementById("v3d-side"), any = false;
    if (!wide && active && treeOn) treeOn = active === "onderdelen";
    document.querySelectorAll(".v3d-panel").forEach(function (p) {
      var name = p.id.replace("v3d-p-", "");
      var show = name === "onderdelen" ? (treeOn && (wide || !active || active === "onderdelen")) : name === active;
      p.hidden = !show; if (show) any = true;
    });
    document.querySelectorAll("[data-panel]").forEach(function (b) {
      b.classList.toggle("on", b.dataset.panel === "onderdelen" ? treeOn : b.dataset.panel === active);
    });
    side.classList.toggle("open", any);
    side.classList.toggle("both", any && treeOn && !!active && active !== "onderdelen" && wide);
    setTimeout(function () { ev.Resize(); if (viewer) viewer.Render(); }, 30);
  }
  function openPanel(name) {
    if (name === "onderdelen") { treeOn = true; if (!wideMq.matches) active = null; }
    else { active = name; if (!wideMq.matches) treeOn = false; }
    layoutPanels();
  }
  document.querySelectorAll("[data-panel]").forEach(function (b) {
    b.addEventListener("click", function () {
      var name = b.dataset.panel;
      if (b.classList.contains("on")) { if (name === "onderdelen") treeOn = false; else active = null; layoutPanels(); }
      else openPanel(name);
    });
  });
  document.getElementById("v3d-close").addEventListener("click", function () { treeOn = false; active = null; layoutPanels(); });
  (wideMq.addEventListener ? wideMq.addEventListener.bind(wideMq, "change") : wideMq.addListener.bind(wideMq))(layoutPanels);
  layoutPanels();

  // lokaal bestand openen (zonder upload)
  var picker = document.getElementById("v3d-file");
  function openFiles(files) {
    if (!files || !files.length) return;
    var f = files[0];
    if (f.size > cfg.maxMb * 1024 * 1024 * 3) { setStatus("Dit bestand is erg groot; openen kan lang duren.", "hint"); }
    cfg.title = f.name;
    document.getElementById("v3d-title").textContent = f.name;
    setStatus("Model inlezen…");
    isStep = /\.(step|stp|iges|igs)$/i.test(f.name);
    startWatch();
    if (!isStep) { ev.LoadModelFromFileList(files); return; }
    checkOcct().then(function (ok) {
      occtOk = ok;
      if (!ok) { fail("De STEP/IGES-lezer ontbreekt op de server (map static/3d/occt, zie README, 3D-modellen). STL/OBJ/3MF werken wel."); return; }
      ev.LoadModelFromFileList(files);
    });
  }
  if (picker) picker.addEventListener("change", function () { openFiles(picker.files); });
  wrap.addEventListener("dragover", function (e) { e.preventDefault(); });
  wrap.addEventListener("drop", function (e) { e.preventDefault(); openFiles(e.dataTransfer.files); });

  window.addEventListener("resize", function () { ev.Resize(); });
  window.WP3D_viewer = function () { return { viewer: viewer, model: model, measures: measures, shown: shown, hidden: hidden, section: section, toScreen: function (x, y, z) { return toScreen(new V3(x, y, z)); } }; };
  renderMeasures();
  load();
})();
