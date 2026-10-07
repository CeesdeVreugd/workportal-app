/* Afdrukken via Printix: <button data-print="url" data-print-pad="pad" data-print-name="naam" data-print-opts='{"copies":1,...}'>
   Opent een klein venster (aantal, dubbelzijdig, kleur) en stuurt het bestand naar de printer. */
(function () {
  "use strict";
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || window.WP_CSRF || "";
  var dlg = null;
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
  function close() { if (dlg) { dlg.remove(); dlg = null; } }
  function open(btn) {
    close();
    var o = {};
    try { o = JSON.parse(btn.getAttribute("data-print-opts") || "{}") || {}; } catch (x) {}
    var name = btn.getAttribute("data-print-name") || "bestand";
    dlg = document.createElement("div");
    dlg.className = "pdlg";
    dlg.innerHTML = '<div class="pdlg-box" role="dialog" aria-modal="true" aria-label="Afdrukken">' +
      '<div class="pdlg-head"><b>Afdrukken</b><button type="button" class="pdlg-x" aria-label="Sluiten">✕</button></div>' +
      '<div class="pdlg-file">' + esc(name) + (o.printer ? '<span>naar ' + esc(o.printer) + '</span>' : '') + '</div>' +
      '<label class="pdlg-row"><span>Aantal</span><span class="pdlg-num"><button type="button" data-d="-1" aria-label="Minder">−</button>' +
      '<input type="number" min="1" max="50" value="' + (parseInt(o.copies, 10) || 1) + '" inputmode="numeric"><button type="button" data-d="1" aria-label="Meer">+</button></span></label>' +
      '<label class="pdlg-row"><span>Dubbelzijdig</span><select>' +
      '<option value="NONE">Nee</option><option value="LONG_EDGE">Ja, lange zijde</option><option value="SHORT_EDGE">Ja, korte zijde</option></select></label>' +
      '<label class="pdlg-row pdlg-check"><span>Kleur</span><input type="checkbox"' + (o.color ? " checked" : "") + '></label>' +
      '<div class="pdlg-msg" hidden></div>' +
      '<div class="pdlg-btns"><button type="button" class="btn ghost pdlg-cancel">Annuleren</button><button type="button" class="btn primary pdlg-go">Afdrukken</button></div></div>';
    document.body.appendChild(dlg);
    var num = dlg.querySelector("input[type=number]"), sel = dlg.querySelector("select"), col = dlg.querySelector("input[type=checkbox]");
    var msg = dlg.querySelector(".pdlg-msg"), go = dlg.querySelector(".pdlg-go");
    sel.value = o.duplex || "NONE";
    dlg.querySelectorAll("[data-d]").forEach(function (b) {
      b.addEventListener("click", function (e) { e.preventDefault(); num.value = Math.max(1, Math.min(50, (parseInt(num.value, 10) || 1) + parseInt(b.getAttribute("data-d"), 10))); });
    });
    dlg.querySelector(".pdlg-x").addEventListener("click", close);
    dlg.querySelector(".pdlg-cancel").addEventListener("click", close);
    dlg.addEventListener("click", function (e) { if (e.target === dlg) close(); });
    go.addEventListener("click", function () {
      var fd = new FormData();
      fd.append("pad", btn.getAttribute("data-print-pad") || ""); fd.append("copies", num.value);
      fd.append("duplex", sel.value); fd.append("color", col.checked ? "1" : "0"); fd.append("csrf_token", CSRF);
      go.disabled = true; go.textContent = "Versturen…"; msg.hidden = true;
      fetch(btn.getAttribute("data-print"), { method: "POST", body: fd, credentials: "same-origin", headers: { "X-CSRF-Token": CSRF } })
        .then(function (r) { return r.json().then(function (js) { return { ok: r.ok, js: js }; }); })
        .then(function (res) {
          if (res.ok && res.js.ok) {
            msg.className = "pdlg-msg ok"; msg.textContent = "Naar de printer gestuurd" + (res.js.copies > 1 ? " (" + res.js.copies + "×)" : "") + ".";
            msg.hidden = false; go.textContent = "Klaar"; setTimeout(close, 1600);
          } else { throw new Error(res.js.error || "Afdrukken is mislukt."); }
        })
        .catch(function (err) {
          msg.className = "pdlg-msg err"; msg.textContent = err.message || "Geen verbinding."; msg.hidden = false;
          go.disabled = false; go.textContent = "Opnieuw";
        });
    });
    go.focus();
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-print]");
    if (!b) return;
    e.preventDefault(); e.stopPropagation(); open(b);
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
})();
