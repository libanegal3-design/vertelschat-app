/* Vertelschat: small progressive enhancements. Everything works without JavaScript (native audio controls,
   plain forms); this file only adds the calmer player, copy buttons and live status. */
(function () {
  "use strict";

  function hash(str) {
    var h = 2166136261;
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }

  function bars(seed, n) {
    // Decorative, stable per recording (not a real waveform): gentle, speech-like rhythm.
    var out = [], x = seed || 1;
    for (var i = 0; i < n; i++) {
      x ^= x << 13; x ^= x >>> 17; x ^= x << 5; x >>>= 0;
      var r = (x % 1000) / 1000;
      var envelope = 0.55 + 0.45 * Math.sin((i / n) * Math.PI);
      out.push(Math.max(0.16, Math.min(1, (0.25 + r * 0.75) * envelope)));
    }
    return out;
  }

  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    var m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  var current = null;

  function enhance(player) {
    var audio = player.querySelector("audio");
    var btn = player.querySelector("[data-play]");
    if (!audio || !btn) return;
    var track = player.querySelector("[data-track]");
    var timeEl = player.querySelector("[data-time]");
    var total = parseFloat(player.getAttribute("data-duration") || "0");
    var fill = null;
    if (track) {
      var n = parseInt(player.getAttribute("data-bars") || "44", 10);
      var hs = bars(hash(audio.currentSrc || audio.getAttribute("src") || ""), n);
      var wave = document.createElement("div"); wave.className = "wave";
      fill = document.createElement("div"); fill.className = "wave-fill";
      hs.forEach(function (h) {
        var a = document.createElement("i"); a.style.height = Math.round(h * 100) + "%"; wave.appendChild(a);
        var b = document.createElement("i"); b.style.height = Math.round(h * 100) + "%"; fill.appendChild(b);
      });
      track.appendChild(wave); track.appendChild(fill);
      track.setAttribute("role", "slider");
      track.setAttribute("aria-label", "Positie in de opname");
      track.setAttribute("tabindex", "0");
      track.addEventListener("click", function (e) {
        var rect = track.getBoundingClientRect();
        var ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
        var d = audio.duration || total;
        if (d) { audio.currentTime = ratio * d; if (audio.paused) play(); }
      });
      track.addEventListener("keydown", function (e) {
        if (e.key === "ArrowRight") { audio.currentTime = Math.min((audio.duration || total), audio.currentTime + 5); e.preventDefault(); }
        if (e.key === "ArrowLeft") { audio.currentTime = Math.max(0, audio.currentTime - 5); e.preventDefault(); }
      });
    }
    function play() {
      if (current && current !== audio) current.pause();
      current = audio;
      var p = audio.play();
      if (p && p.catch) p.catch(function () { player.classList.remove("is-playing"); });
    }
    btn.addEventListener("click", function () { if (audio.paused) play(); else audio.pause(); });
    audio.addEventListener("play", function () { player.classList.add("is-playing"); btn.setAttribute("aria-label", "Pauzeer"); });
    audio.addEventListener("pause", function () { player.classList.remove("is-playing"); btn.setAttribute("aria-label", "Luister"); });
    audio.addEventListener("timeupdate", function () {
      var d = audio.duration || total;
      if (fill && d) fill.style.clipPath = "inset(0 " + (100 - (audio.currentTime / d) * 100) + "% 0 0)";
      if (timeEl) timeEl.textContent = fmt(audio.currentTime > 0 ? audio.currentTime : d);
      if (track && d) track.setAttribute("aria-valuenow", Math.round((audio.currentTime / d) * 100));
    });
    audio.addEventListener("ended", function () {
      player.classList.remove("is-playing");
      if (fill) fill.style.clipPath = "inset(0 100% 0 0)";
      if (timeEl) timeEl.textContent = fmt(audio.duration || total);
      var group = player.getAttribute("data-group");
      if (group) {
        var all = Array.prototype.slice.call(document.querySelectorAll('[data-player][data-group="' + group + '"]'));
        var next = all[all.indexOf(player) + 1];
        if (next) { var nb = next.querySelector("[data-play]"); if (nb) nb.click(); }
      }
    });
    player.classList.add("is-enhanced");
  }

  function copy(btn) {
    var sel = btn.getAttribute("data-copy");
    var el = sel ? document.querySelector(sel) : null;
    var text = el ? (el.value || el.textContent) : btn.getAttribute("data-copy-text") || "";
    var done = function () {
      var old = btn.textContent; btn.textContent = "Gekopieerd";
      setTimeout(function () { btn.textContent = old; }, 1800);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text.trim()).then(done, function () {});
    } else if (el && el.select) { el.select(); document.execCommand("copy"); done(); }
  }

  function pollStatus(box) {
    var url = box.getAttribute("data-poll-status");
    var label = box.querySelector("[data-status-text]");
    var tick = function () {
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (s) {
        if (s.connected && s.consent === "given") {
          box.classList.add("is-done");
          if (label) label.textContent = box.getAttribute("data-done-text") || "Verbonden";
          var next = box.querySelector("[data-done-link]"); if (next) next.hidden = false;
          return;
        }
        if (s.connected && label) label.textContent = box.getAttribute("data-linked-text") || label.textContent;
        setTimeout(tick, 4000);
      }).catch(function () { setTimeout(tick, 8000); });
    };
    setTimeout(tick, 2500);
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-player]"), enhance);
    Array.prototype.forEach.call(document.querySelectorAll("[data-copy], [data-copy-text]"), function (b) {
      b.addEventListener("click", function (e) { e.preventDefault(); copy(b); });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-poll-status]"), pollStatus);
    Array.prototype.forEach.call(document.querySelectorAll("form[data-confirm]"), function (f) {
      f.addEventListener("submit", function (e) { if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault(); });
    });
    var refresh = document.querySelector("[data-refresh-while]");
    if (refresh) setTimeout(function () { window.location.reload(); }, 3500);
  });
})();

/* pricing V2: copy steppers, live order sum, copy-to-clipboard */
(function () {
  function eur(c) { var w = Math.floor(c / 100), r = c % 100; return "\u20ac" + w + (r ? "," + (r < 10 ? "0" : "") + r : ""); }
  document.querySelectorAll(".stepper").forEach(function (st) {
    var input = st.querySelector("input");
    st.querySelectorAll("[data-step]").forEach(function (b) {
      b.addEventListener("click", function () {
        var v = Math.max(1, Math.min(20, (parseInt(input.value, 10) || 1) + parseInt(b.getAttribute("data-step"), 10)));
        input.value = v; input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    });
  });
  var form = document.querySelector("[data-order-form]");
  if (form) {
    var credits = parseInt(form.dataset.credits, 10) || 0, first = +form.dataset.first, next = +form.dataset.next,
        sur = +form.dataset.surcharge, sum = form.querySelector("[data-order-sum]"), btn = form.querySelector("[data-order-submit]");
    var update = function () {
      var n = Math.max(1, Math.min(20, parseInt(form.quantity.value, 10) || 1));
      var fromCredit = Math.min(credits, n), paid = n - fromCredit, total = fromCredit * sur, parts = [];
      if (fromCredit) parts.push(fromCredit + " uit je boektegoed" + (sur ? " (+ " + eur(sur) + " toeslag per stuk)" : ""));
      for (var i = 0; i < paid; i++) total += (fromCredit === 0 && i === 0) ? first : next;
      if (paid) parts.push(paid + " extra");
      sum.textContent = parts.join(" + ") + (total ? ": " + eur(total) + " te betalen." : ", gratis verzonden.");
      btn.textContent = total ? "Verder naar betalen (" + eur(total) + ")" : "Bestellen";
    };
    form.quantity.addEventListener("input", update); update();
  }
  var fam = document.querySelector("[data-family-form]");
  if (fam) {
    var fsum = fam.querySelector("[data-family-sum]");
    var toCents = function (t) { var m = t.replace("\u20ac", "").split(","); return parseInt(m[0], 10) * 100 + (m[1] ? parseInt(m[1], 10) : 0); };
    var f1 = toCents(fam.dataset.first), fn = toCents(fam.dataset.next);
    var fbtn = fam.querySelector("[data-family-submit]");
    var fu = function () { var n = Math.max(1, Math.min(20, parseInt(fam.aantal.value, 10) || 1));
      if (fbtn) fbtn.textContent = n === 1 ? "Bestel een exemplaar" : "Bestel " + n + " exemplaren";
      fsum.textContent = eur(f1 + (n - 1) * fn) + (n > 1 ? " voor " + n + " exemplaren in \u00e9\u00e9n pakket" : "") + ", gratis verzonden in Nederland en Belgi\u00eb."; };
    fam.aantal.addEventListener("input", fu); fu();
  }
})();
