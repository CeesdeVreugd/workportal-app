"""Haalt occt-import-js (STEP/IGES-lezer, OpenCascade WASM) op en zet hem in workportal/static/3d/occt/.

Draait één keer tijdens 'docker build'. Daarna host WorkPortal de bestanden zelf (geen CDN in de browser).
Staan de bestanden er al (bijv. handmatig neergezet), dan wordt er niets gedownload.

Handmatig (als de server tijdens het bouwen geen internet heeft):
  npm pack occt-import-js@0.0.22   ->  uitpakken  ->  package/dist/occt-import-js.js, occt-import-js.wasm,
  occt-import-js-worker.js en package/LICENSE.md kopiëren naar workportal/static/3d/occt/
"""
import io
import os
import sys
import tarfile
import urllib.request

VERSION = "0.0.22"
URLS = [
    f"https://registry.npmjs.org/occt-import-js/-/occt-import-js-{VERSION}.tgz",
    f"https://registry.npmmirror.com/occt-import-js/-/occt-import-js-{VERSION}.tgz",
]
WANT = {
    "package/dist/occt-import-js.js": "occt-import-js.js",
    "package/dist/occt-import-js.wasm": "occt-import-js.wasm",
    "package/dist/occt-import-js-worker.js": "occt-import-js-worker.js",
}
DEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workportal", "static", "3d", "occt")


def main():
    os.makedirs(DEST, exist_ok=True)
    if all(os.path.exists(os.path.join(DEST, f)) for f in WANT.values()):
        print("[3D] occt-import-js staat er al.")
        return 0
    for url in URLS:
        try:
            print(f"[3D] occt-import-js {VERSION} ophalen: {url}", flush=True)
            data = urllib.request.urlopen(url, timeout=120).read()
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                names = tar.getnames()
                for src, dst in WANT.items():
                    with tar.extractfile(src) as fh, open(os.path.join(DEST, dst), "wb") as out:
                        out.write(fh.read())
                for lic in ("package/LICENSE.md", "package/LICENSE"):
                    if lic in names:
                        with tar.extractfile(lic) as fh, open(os.path.join(DEST, "LICENSE.md"), "wb") as out:
                            out.write(fh.read())
                        break
            print("[3D] klaar.")
            return 0
        except Exception as exc:  # netwerk of ander formaat: volgende bron proberen
            print(f"[3D] mislukt: {exc}", flush=True)
    print("[3D] WAARSCHUWING: occt-import-js niet opgehaald. STEP/IGES werkt pas als de bestanden in "
          "workportal/static/3d/occt/ staan (zie README, 3D-modellen). STL/OBJ/3MF werken wel.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
