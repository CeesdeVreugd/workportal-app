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
  var measures = [];
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

  var ev = new OV.EmbeddedViewer(wrap, {
    backgroundColor: new OV.RGBAColor(244, 246, 250, 255),
    defaultColor: new OV.RGBColor(190, 196, 208),
    edgeSettings: new OV.EdgeSettings(false, new OV.RGBColor(20, 22, 58), 1),
    onModelLoaded: onLoaded
  });

  // voortgang/fouten van de engine opvangen
  var watch = null;
  function poll() {
    var pd = ev.progressDiv;
    if (pd && pd.innerHTML) {
      var t = pd.textContent || "";
      pd.style.display = "none";
      if (/error|failed|unknown|no importable|not/i.test(t) && !/model\.\.\./i.test(t)) {
        clearInterval(watch);
        setStatus("Het model kon niet worden geopend. " + (/no importable/i.test(t) ? "Dit bestandstype wordt niet ondersteund." :
          (cfg.occt ? "Controleer of het een geldig STEP-, IGES-, STL-, OBJ- of 3MF-bestand is." :
            "STEP/IGES-ondersteuning is nog niet geïnstalleerd op de server (zie README, 3D-modellen).")), "error");
      } else {
        setStatus(t.replace("Loading model...", "Model ophalen…").replace("Importing model...", "Model inlezen… (grote STEP-bestanden kunnen even duren)")
          .replace("Visualizing model...", "Model opbouwen…"));
      }
    }
  }
  function startWatch() { clearInterval(watch); watch = setInterval(poll, 150); }
  startWatch();

  function load() {
    if (cfg.url) {
      setStatus("Model ophalen…" + (cfg.sizeMb ? " (" + fmt(cfg.sizeMb, 1) + " MB)" : ""));
      ev.LoadModelFromUrlList([cfg.url]);
    } else {
      setStatus("Kies of sleep een 3D-bestand (STEP, IGES, STL, OBJ of 3MF) om het te bekijken. Het bestand wordt niet geüpload.", "hint");
    }
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
    hidden.clear(); measures = []; pending = null; selected = null;
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
          l2.querySelector(".nm").textContent = model.GetMesh(mi).GetName() || "body " + (mi + 1);
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
    return { p: h.point.clone(), n: n, ud: h.object.userData };
  }
  function clipped(p) {
    if (!section.on) return false;
    var v = section.axis === "x" ? p.x : section.axis === "y" ? p.y : p.z;
    return section.flip ? v < section.pos : v > section.pos;
  }
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
    if (!pending) { pending = h; info(mode === "punt" ? "Klik het tweede punt." : "Klik het tweede vlak."); viewer.Render(); return; }
    var a = pending, b = h; pending = null;
    var m = { type: mode, a: a.p, b: b.p };
    if (mode === "punt") {
      m.dist = a.p.distanceTo(b.p);
      m.dx = Math.abs(b.p.x - a.p.x); m.dy = Math.abs(b.p.y - a.p.y); m.dz = Math.abs(b.p.z - a.p.z);
      m.label = fmt(m.dist, 2) + " mm";
    } else {
      var dot = Math.max(-1, Math.min(1, a.n.dot(b.n)));
      m.angle = Math.acos(dot) * 180 / Math.PI;
      m.parallel = m.angle < 0.5 || m.angle > 179.5;
      if (m.parallel) {
        var diff = b.p.clone().sub(a.p);
        m.dist = Math.abs(diff.dot(a.n));
        m.label = fmt(m.dist, 2) + " mm (evenwijdig)";
      } else {
        m.label = fmt(m.angle, 1) + "°";
        m.dist = a.p.distanceTo(b.p);
      }
    }
    measures.push(m);
    info(m.label);
    renderMeasures();
    viewer.Render();
  }

  // ------------------------------------------------------------ meetoverlay (SVG)
  function toScreen(v) {
    var cam = viewer.camera, w = wrap.clientWidth, h = wrap.clientHeight;
    var p = v.clone().project(cam);
    return { x: (p.x + 1) / 2 * w, y: (1 - p.y) / 2 * h, ok: p.z < 1 && p.z > -1 };
  }
  function drawOverlay() {
    if (!viewer || !V3) return;
    var w = wrap.clientWidth, h = wrap.clientHeight, s = "";
    overlay.setAttribute("viewBox", "0 0 " + w + " " + h);
    measures.forEach(function (m, i) {
      var a = toScreen(m.a), b = toScreen(m.b);
      if (!a.ok || !b.ok) return;
      var col = m.type === "punt" ? "#0080FF" : "#E5484D";
      s += '<line x1="' + a.x + '" y1="' + a.y + '" x2="' + b.x + '" y2="' + b.y + '" stroke="' + col + '" stroke-width="2" stroke-dasharray="' + (m.type === "vlak" ? "6 4" : "") + '"/>';
      s += '<circle cx="' + a.x + '" cy="' + a.y + '" r="5" fill="' + col + '" stroke="#fff" stroke-width="2"/><circle cx="' + b.x + '" cy="' + b.y + '" r="5" fill="' + col + '" stroke="#fff" stroke-width="2"/>';
      var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, label = (i + 1) + ": " + m.label, tw = label.length * 7.2 + 14;
      s += '<g transform="translate(' + (mx - tw / 2) + ',' + (my - 26) + ')"><rect width="' + tw + '" height="22" rx="6" fill="#14163A" opacity=".88"/>' +
        '<text x="' + (tw / 2) + '" y="15" fill="#fff" font-size="12.5" font-family="Ubuntu, system-ui, sans-serif" text-anchor="middle">' + label + '</text></g>';
    });
    if (pending) { var q = toScreen(pending.p); if (q.ok) s += '<circle cx="' + q.x + '" cy="' + q.y + '" r="6" fill="#F2A516" stroke="#fff" stroke-width="2"/>'; }
    overlay.innerHTML = s;
  }
  function renderMeasures() {
    if (!measures.length) { measureList.innerHTML = '<p class="muted small">Nog geen metingen. Kies <b>Punt–punt</b> of <b>Vlak–vlak</b> en klik twee keer op het model.</p>'; return; }
    measureList.innerHTML = measures.map(function (m, i) {
      var extra = m.type === "punt" ? "ΔX " + fmt(m.dx, 2) + " · ΔY " + fmt(m.dy, 2) + " · ΔZ " + fmt(m.dz, 2) + " mm"
        : (m.parallel ? "Vlakken evenwijdig, loodrechte afstand" : "Hoek tussen de vlakken (afstand klikpunten " + fmt(m.dist, 2) + " mm)");
      return '<div class="v3d-m"><b>' + (i + 1) + '. ' + (m.type === "punt" ? "Punt–punt" : "Vlak–vlak") + ': ' + m.label + '</b><div class="small muted">' + extra + '</div></div>';
    }).join("") + '<button type="button" class="btn sm" id="v3d-clearm">Metingen wissen</button>';
    document.getElementById("v3d-clearm").onclick = function () { measures = []; pending = null; renderMeasures(); info(""); viewer.Render(); };
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
      info(mode === "punt" ? "Meten punt–punt: klik het eerste punt." : mode === "vlak" ? "Meten vlak–vlak: klik het eerste vlak." : "");
      if (mode !== "draai") openPanel("meten");
      if (viewer) viewer.Render();
    });
  });
  var edges = false, ortho = false;
  document.getElementById("v3d-edges").addEventListener("click", function (e) {
    edges = !edges; e.currentTarget.classList.toggle("on", edges);
    if (viewer) viewer.SetEdgeSettings(new OV.EdgeSettings(edges, new OV.RGBColor(20, 22, 58), 1));
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

  // panelen (onderdelen / doorsnede / meten)
  function openPanel(name) {
    document.querySelectorAll(".v3d-panel").forEach(function (p) { p.hidden = p.id !== "v3d-p-" + name ? true : false; });
    document.querySelectorAll("[data-panel]").forEach(function (b) { b.classList.toggle("on", b.dataset.panel === name); });
    document.getElementById("v3d-side").classList.add("open");
    setTimeout(function () { ev.Resize(); }, 30);
  }
  document.querySelectorAll("[data-panel]").forEach(function (b) {
    b.addEventListener("click", function () {
      var side = document.getElementById("v3d-side");
      if (b.classList.contains("on") && side.classList.contains("open")) { side.classList.remove("open"); b.classList.remove("on"); setTimeout(function () { ev.Resize(); }, 30); }
      else openPanel(b.dataset.panel);
    });
  });
  document.getElementById("v3d-close").addEventListener("click", function () {
    document.getElementById("v3d-side").classList.remove("open");
    document.querySelectorAll("[data-panel]").forEach(function (b) { b.classList.remove("on"); });
    setTimeout(function () { ev.Resize(); }, 30);
  });

  // lokaal bestand openen (zonder upload)
  var picker = document.getElementById("v3d-file");
  function openFiles(files) {
    if (!files || !files.length) return;
    var f = files[0];
    if (f.size > cfg.maxMb * 1024 * 1024 * 3) { setStatus("Dit bestand is erg groot; openen kan lang duren.", "hint"); }
    cfg.title = f.name;
    document.getElementById("v3d-title").textContent = f.name;
    setStatus("Model inlezen…");
    startWatch();
    ev.LoadModelFromFileList(files);
  }
  if (picker) picker.addEventListener("change", function () { openFiles(picker.files); });
  wrap.addEventListener("dragover", function (e) { e.preventDefault(); });
  wrap.addEventListener("drop", function (e) { e.preventDefault(); openFiles(e.dataTransfer.files); });

  window.addEventListener("resize", function () { ev.Resize(); });
  window.WP3D_viewer = function () { return { viewer: viewer, model: model, measures: measures, hidden: hidden, section: section }; };
  renderMeasures();
  load();
})();
