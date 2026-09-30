// STEP/IGES -> GLB op de server (occt-import-js, OpenCascade WASM).
// Gebruik: node step2glb.js <invoer.step|.iges> <uitvoer.glb> [occt-map]
// Coördinaten blijven in mm (de WorkPortal-viewer meet in mm). Kleuren per vlak blijven behouden.
"use strict";
const fs = require("fs");
const path = require("path");

const [, , inFile, outFile, occtDirArg] = process.argv;
if (!inFile || !outFile) { console.error("gebruik: node step2glb.js in.step out.glb [occt-map]"); process.exit(2); }
const occtDir = occtDirArg || path.join(__dirname, "..", "workportal", "static", "3d", "occt");
const occtimportjs = require(path.join(occtDir, "occt-import-js.js"));

function log(msg) { process.stdout.write(msg + "\n"); }

(async () => {
  const t0 = Date.now();
  const occt = await occtimportjs({ locateFile: (f) => path.join(occtDir, f) });
  const buf = fs.readFileSync(inFile);
  const ext = path.extname(inFile).toLowerCase();
  const format = ext === ".igs" || ext === ".iges" ? "iges" : "step";
  log(`lezen ${format} ${(buf.length / 1048576).toFixed(1)} MB`);
  const res = occt[format === "iges" ? "ReadIgesFile" : "ReadStepFile"](new Uint8Array(buf),
    { linearUnit: "millimeter", linearDeflectionType: "bounding_box_ratio", linearDeflection: 0.001, angularDeflection: 0.5 });
  if (!res || !res.success) { console.error("STEP-lezer: bestand kon niet worden gelezen"); process.exit(3); }
  const nTri = res.meshes.reduce((a, m) => a + ((m.index && m.index.array.length) || 0), 0);
  if (!res.meshes.length || !nTri) { console.error("Het model bevat geen 3D-geometrie (geen vlakken)"); process.exit(4); }
  log(`gelezen in ${((Date.now() - t0) / 1000).toFixed(1)} s, ${res.meshes.length} meshes`);

  // ---- glTF opbouwen
  const bin = [];           // stukken binaire data
  let binLen = 0;
  const bufferViews = [], accessors = [], materials = [], meshes = [], nodes = [];
  const matIndex = new Map();
  function addView(typed, target) {
    const pad = (4 - (binLen % 4)) % 4;
    if (pad) { bin.push(Buffer.alloc(pad)); binLen += pad; }
    const b = Buffer.from(typed.buffer, typed.byteOffset, typed.byteLength);
    bin.push(b);
    bufferViews.push({ buffer: 0, byteOffset: binLen, byteLength: b.length, target });
    binLen += b.length;
    return bufferViews.length - 1;
  }
  function material(color) {
    const c = color ? color.map((v) => Math.round(v * 1000) / 1000) : null;
    const key = c ? c.join(",") : "standaard";
    if (matIndex.has(key)) return matIndex.get(key);
    const m = { name: key, pbrMetallicRoughness: { baseColorFactor: c ? [c[0], c[1], c[2], 1] : [0.75, 0.77, 0.82, 1], metallicFactor: 0.0, roughnessFactor: 0.6 }, doubleSided: true };
    materials.push(m); matIndex.set(key, materials.length - 1);
    return materials.length - 1;
  }
  const meshMap = res.meshes.map((m, mi) => {
    const pos = new Float32Array(m.attributes.position.array);
    if (!pos.length) return null;
    let min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < pos.length; i += 3) for (let k = 0; k < 3; k++) { const v = pos[i + k]; if (v < min[k]) min[k] = v; if (v > max[k]) max[k] = v; }
    accessors.push({ bufferView: addView(pos, 34962), componentType: 5126, count: pos.length / 3, type: "VEC3", min, max });
    const posAcc = accessors.length - 1;
    let nrmAcc;
    if (m.attributes.normal && m.attributes.normal.array.length === pos.length) {
      const n = new Float32Array(m.attributes.normal.array);
      accessors.push({ bufferView: addView(n, 34962), componentType: 5126, count: n.length / 3, type: "VEC3" });
      nrmAcc = accessors.length - 1;
    }
    const idx = m.index.array, vcount = pos.length / 3;
    // driehoeken groeperen per kleur (kleur per vlak uit de STEP)
    const groups = new Map();
    const tris = idx.length / 3;
    const faceColor = new Array(tris).fill(null);
    if (m.brep_faces) for (const f of m.brep_faces) if (f.color) for (let t = f.first; t <= f.last && t < tris; t++) faceColor[t] = f.color;
    for (let t = 0; t < tris; t++) {
      const col = faceColor[t] || m.color || null;
      const key = col ? col.join(",") : "-";
      let g = groups.get(key);
      if (!g) { g = { color: col, list: [] }; groups.set(key, g); }
      g.list.push(idx[t * 3], idx[t * 3 + 1], idx[t * 3 + 2]);
    }
    const primitives = [];
    for (const g of groups.values()) {
      const arr = vcount > 65535 ? new Uint32Array(g.list) : new Uint16Array(g.list);
      accessors.push({ bufferView: addView(arr, 34963), componentType: vcount > 65535 ? 5125 : 5123, count: arr.length, type: "SCALAR" });
      const attributes = { POSITION: posAcc };
      if (nrmAcc !== undefined) attributes.NORMAL = nrmAcc;
      primitives.push({ attributes, indices: accessors.length - 1, material: material(g.color), mode: 4 });
    }
    meshes.push({ name: m.name || "", primitives });
    return meshes.length - 1;
  });

  function addNode(n) {
    const node = { name: n.name || "" };
    const kids = [];
    for (const mi of n.meshes || []) {
      const gm = meshMap[mi];
      if (gm === null || gm === undefined) continue;
      const name = res.meshes[mi].name || "";
      if ((n.meshes.length === 1) && !(n.children || []).length) { node.mesh = gm; if (!node.name) node.name = name; }
      else { nodes.push({ name, mesh: gm }); kids.push(nodes.length - 1); }
    }
    nodes.push(node);
    const self = nodes.length - 1;
    for (const c of n.children || []) kids.push(addNode(c));
    if (kids.length) node.children = kids;
    return self;
  }
  const root = addNode(res.root);

  const gltf = {
    asset: { version: "2.0", generator: "WorkPortal step2glb (occt-import-js)" },
    extras: { unit: "mm" },
    scene: 0, scenes: [{ nodes: [root] }], nodes, meshes, materials, accessors, bufferViews,
    buffers: [{ byteLength: binLen }],
  };
  let json = Buffer.from(JSON.stringify(gltf), "utf8");
  const jpad = (4 - (json.length % 4)) % 4;
  if (jpad) json = Buffer.concat([json, Buffer.alloc(jpad, 0x20)]);
  const bpad = (4 - (binLen % 4)) % 4;
  if (bpad) { bin.push(Buffer.alloc(bpad)); binLen += bpad; }
  const header = Buffer.alloc(12), jh = Buffer.alloc(8), bh = Buffer.alloc(8);
  const total = 12 + 8 + json.length + 8 + binLen;
  header.writeUInt32LE(0x46546c67, 0); header.writeUInt32LE(2, 4); header.writeUInt32LE(total, 8);
  jh.writeUInt32LE(json.length, 0); jh.writeUInt32LE(0x4e4f534a, 4);
  bh.writeUInt32LE(binLen, 0); bh.writeUInt32LE(0x004e4942, 4);
  const tmp = outFile + ".tmp";
  const fd = fs.openSync(tmp, "w");
  for (const part of [header, jh, json, bh, ...bin]) fs.writeSync(fd, part);
  fs.closeSync(fd);
  fs.renameSync(tmp, outFile);
  log(`klaar in ${((Date.now() - t0) / 1000).toFixed(1)} s: ${(total / 1048576).toFixed(1)} MB glb`);
})().catch((e) => { console.error(String(e && e.message || e)); process.exit(1); });
