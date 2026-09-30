"""STEP/IGES op de server omzetten naar GLB, zodat grote modellen snel openen (fase 2).

- Omzetten gebeurt met Node.js + occt-import-js (dezelfde OpenCascade-lezer als in de browser),
  script: scripts/step2glb.js. Eén omzetting tegelijk, in een achtergrondthread.
- Resultaat staat in DATA_DIR/3d-cache/<sleutel>.glb, de sleutel is een hash van bron + versie.
  Wordt de STEP in SharePoint gewijzigd, dan hoort daar een nieuwe sleutel bij en wordt opnieuw omgezet.
- De browser laadt daarna de GLB (seconden) in plaats van de STEP zelf in te lezen (minuten).
- Ontbreekt Node.js of mislukt het omzetten, dan leest de browser de STEP zoals voorheen.
"""
import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

STEP_EXT = (".step", ".stp", ".iges", ".igs")
TIMEOUT = int(os.environ.get("WP3D_CONVERT_TIMEOUT", "1800"))      # max. 30 minuten per model
NODE_HEAP_MB = int(os.environ.get("WP3D_NODE_HEAP_MB", "6144"))
KEEP_DAYS = 120

_q = queue.Queue()
_busy = set()
_lock = threading.Lock()
_worker = None
_cfg = {}


def is_step(name):
    return (name or "").lower().endswith(STEP_EXT)


def node_bin():
    return shutil.which("node") or shutil.which("nodejs")


def available(app):
    root = app.root_path
    return bool(node_bin()) and os.path.exists(os.path.join(root, "static", "3d", "occt", "occt-import-js.wasm")) \
        and os.path.exists(os.path.join(os.path.dirname(root), "scripts", "step2glb.js"))


def cache_dir(app):
    d = os.path.join(app.config["DATA_DIR"], "3d-cache")
    os.makedirs(d, exist_ok=True)
    return d


def make_key(source, version):
    return hashlib.sha1(f"{source}|{version}".encode("utf-8")).hexdigest()


def valid_key(key):
    return len(key or "") == 40 and all(c in "0123456789abcdef" for c in key)


def glb_path(app, key):
    return os.path.join(cache_dir(app), key + ".glb")


def _state_path(app, key):
    return os.path.join(cache_dir(app), key + ".json")


def _write_state(app, key, **st):
    old = status(app, key) or {}
    old.update(st)
    tmp = _state_path(app, key) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(old, fh)
    os.replace(tmp, _state_path(app, key))


def status(app, key):
    if not valid_key(key):
        return None
    if os.path.exists(glb_path(app, key)):
        st = {"state": "klaar", "size": os.path.getsize(glb_path(app, key))}
        try:
            with open(_state_path(app, key), encoding="utf-8") as fh:
                st = {**json.load(fh), **st}
        except (OSError, ValueError):
            pass
        return st
    try:
        with open(_state_path(app, key), encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None
    # een 'bezig' van een vorige herstart geldt niet meer
    if st.get("state") in ("wachtrij", "bezig") and key not in _busy:
        return None
    return st


def ensure(app, source, version, name, fetch):
    """Zorgt dat er een GLB komt. fetch(pad) schrijft de bron naar pad. Geeft (sleutel, status)."""
    key = make_key(source, version)
    st = status(app, key)
    if st and st.get("state") in ("klaar", "bezig", "wachtrij"):
        return key, st
    if st and st.get("state") == "fout" and time.time() - st.get("finished", 0) < 6 * 3600:
        return key, st   # niet elke keer opnieuw proberen
    with _lock:
        if key in _busy:
            return key, status(app, key)
        _busy.add(key)
    _write_state(app, key, state="wachtrij", name=name, source=source, queued=time.time(), error=None)
    _q.put((key, name, fetch))
    _start(app)
    return key, status(app, key)


def _start(app):
    global _worker
    _cfg["app"] = app
    if _worker and _worker.is_alive():
        return
    _worker = threading.Thread(target=_run, daemon=True, name="wp-3d-omzetten")
    _worker.start()


def _run():
    app = _cfg["app"]
    while True:
        key, name, fetch = _q.get()
        try:
            _convert(app, key, name, fetch)
        except Exception as exc:  # noqa: BLE001 - de fout hoort in de status, niet in de log van de worker
            _write_state(app, key, state="fout", error=str(exc)[:300], finished=time.time())
        finally:
            with _lock:
                _busy.discard(key)
            _q.task_done()
        _cleanup(app)


def _convert(app, key, name, fetch):
    _write_state(app, key, state="bezig", started=time.time(), step="ophalen")
    ext = os.path.splitext(name)[1].lower() or ".step"
    with tempfile.TemporaryDirectory(prefix="wp3d-") as tmp:
        src = os.path.join(tmp, "bron" + ext)
        fetch(src)
        _write_state(app, key, step="inlezen", src_size=os.path.getsize(src))
        out = os.path.join(tmp, "model.glb")
        script = os.path.join(os.path.dirname(app.root_path), "scripts", "step2glb.js")
        occt = os.path.join(app.root_path, "static", "3d", "occt")
        cmd = [node_bin(), f"--max-old-space-size={NODE_HEAP_MB}", script, src, out, occt]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Omzetten duurde langer dan {TIMEOUT // 60} minuten")
        if r.returncode != 0 or not os.path.exists(out):
            msg = (r.stderr or r.stdout or "").strip().splitlines()
            msg = msg[-1] if msg else f"afgebroken (code {r.returncode})"
            if r.returncode in (-9, 137) or "memory" in msg.lower():
                msg = "Te weinig geheugen op de server voor dit model"
            raise RuntimeError(msg)
        shutil.move(out, glb_path(app, key))
    _write_state(app, key, state="klaar", finished=time.time(), step=None, error=None)


def _cleanup(app):
    """GLB's die lang niet meer zijn gebruikt opruimen."""
    d, limit = cache_dir(app), time.time() - KEEP_DAYS * 86400
    for f in os.listdir(d):
        p = os.path.join(d, f)
        try:
            if os.path.getatime(p) < limit and os.path.getmtime(p) < limit:
                os.remove(p)
        except OSError:
            pass


def touch(app, key):
    try:
        os.utime(glb_path(app, key), None)
    except OSError:
        pass
