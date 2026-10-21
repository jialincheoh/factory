// Progressive enhancements. Without JavaScript, the filter bar still works as
// a plain GET form; the chat, map, photo viewer and phone reveal need it.
(function () {
  "use strict";

  const hasMap = typeof window.L !== "undefined";
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text; // never parsed as HTML
    return e;
  };

  function baseMap(node, lat, lng, zoom) {
    const map = L.map(node, { scrollWheelZoom: false }).setView([lat, lng], zoom);
    L.tileLayer(node.dataset.tiles + "/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    return map;
  }

  const pinStyle = { radius: 9, weight: 2, color: "#ffffff", fillColor: "#1f5fae", fillOpacity: 1 };

  // ---------------------------------------------------------------- results map
  const mapNode = document.getElementById("results-map");
  let resultsMap = null;
  let markerLayer = null;

  function showMarkers(markers) {
    if (!resultsMap) return;
    markerLayer.clearLayers();
    const bounds = [];
    for (const m of markers) {
      const popup = el("div", "map-popup");
      const link = el("a", "", m.title);
      link.href = m.url;
      popup.append(link, el("div", "map-popup-price", m.price));
      L.circleMarker([m.lat, m.lng], pinStyle).bindPopup(popup).addTo(markerLayer);
      bounds.push([m.lat, m.lng]);
    }
    if (bounds.length === 1) resultsMap.setView(bounds[0], 12);
    else if (bounds.length > 1) resultsMap.fitBounds(bounds, { padding: [30, 30], maxZoom: 12 });
    else resultsMap.setView([4.2, 102.0], 6);
  }

  if (mapNode && hasMap) {
    resultsMap = baseMap(mapNode, 4.2, 102.0, 6);
    markerLayer = L.layerGroup().addTo(resultsMap);
    showMarkers(JSON.parse(document.getElementById("markers-data").textContent));
    document.querySelectorAll(".view-toggle .toggle").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".view-toggle .toggle").forEach((b) => b.classList.toggle("active", b === btn));
        mapNode.hidden = btn.dataset.view === "list";
        if (!mapNode.hidden) resultsMap.invalidateSize();
      });
    });
  }

  // --------------------------------------------------- filter bar + results
  const filterForm = document.getElementById("filter-form");
  const results = document.getElementById("results");
  const heading = document.getElementById("results-heading");

  function formValues() {
    const data = {};
    for (const [k, v] of new FormData(filterForm)) if (v !== "") data[k] = v;
    return data;
  }

  function setFormValues(values) {
    for (const field of filterForm.elements) {
      if (!field.name || !(field.name in values)) continue;
      const v = values[field.name];
      if (field.type === "checkbox") field.checked = !!v;
      else field.value = v === null ? "" : String(v);
    }
  }

  function syncUrl() {
    const qs = new URLSearchParams(formValues()).toString();
    history.replaceState(null, "", qs ? "/?" + qs : "/");
  }

  function showResults(data) {
    results.innerHTML = data.html; // rendered server-side by autoescaping Jinja
    heading.textContent = data.count + " matching listing" + (data.count === 1 ? "" : "s");
    showMarkers(data.markers || []);
  }

  async function runFilters() {
    const res = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formValues()),
    });
    if (res.ok) showResults(await res.json());
    syncUrl();
  }

  if (filterForm) {
    filterForm.addEventListener("submit", (e) => { e.preventDefault(); runFilters(); });
    filterForm.addEventListener("change", (e) => { if (e.target.tagName !== "INPUT" || e.target.type === "checkbox") runFilters(); });
    document.getElementById("filter-clear").addEventListener("click", (e) => {
      e.preventDefault();
      filterForm.reset();
      setFormValues({ deal: "", type: "", area: "", state: "", max_price: "", min_acres: "", min_power: "",
                      min_ceiling: "", tenure: "", container: false });
      runFilters();
    });
  }

  // ---------------------------------------------------------------- chat search
  const chatForm = document.getElementById("chat-form");
  if (chatForm) {
    const log = document.getElementById("chat-log");
    const input = document.getElementById("chat-input");
    const reset = document.getElementById("chat-reset");
    let history = [];

    const say = (role, text) => {
      const p = el("p", "msg " + role, text);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
      return p;
    };

    // One path for typed, spoken and photo searches. `request` is either a JSON body or a FormData
    // for /api/chat/media; `shown` is what the buyer sees as their own message.
    const send = async (request, shown) => {
      const bubble = say("user", shown.text);
      if (shown.image) { const img = document.createElement("img"); img.src = shown.image; img.alt = "Your photo"; bubble.appendChild(img); }
      history.push({ role: "user", content: shown.sent });
      const pending = say("assistant pending", "Searching…");
      const button = chatForm.querySelector("button[type=submit]");
      button.disabled = true;
      try {
        const res = request instanceof FormData
          ? await fetch("/api/chat/media", { method: "POST", headers: { "X-Requested-With": "fetch" }, body: request })
          : await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
        const data = await res.json();
        pending.remove();
        if (data.heard !== undefined) {               // voice: show what was understood, and keep it in the history
          if (data.heard) { bubble.textContent = "🎤 " + data.heard; history[history.length - 1].content = data.heard; }
          else { history.pop(); say("assistant", data.reply); return; }
        }
        say("assistant", data.reply || "Something went wrong.");
        if (res.ok) history.push({ role: "assistant", content: data.reply });
        else history.pop();
        if (data.html !== undefined) {
          showResults(data);
          if (filterForm && data.form) { setFormValues(data.form); syncUrl(); }
        }
        reset.hidden = false;
      } catch (err) {
        pending.remove();
        history.pop();
        say("assistant", "Could not reach the server. Please try again.");
      } finally {
        button.disabled = false;
        input.focus();
      }
    };
    const currentFilters = () => (filterForm ? formValues() : {});

    chatForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) { input.focus(); return; }
      input.value = "";
      send({ messages: history.concat([{ role: "user", content: text }]), filters: currentFilters() },
           { text, sent: text });
    });

    // ---- photo search: the photo is shrunk in the browser, so uploads from a phone camera stay small.
    const photoInput = document.getElementById("chat-photo");
    if (photoInput) {
      photoInput.addEventListener("change", async () => {
        const file = photoInput.files[0];
        photoInput.value = "";
        if (!file) return;
        const caption = input.value.trim();
        input.value = "";
        let blob = file, preview;
        try {
          const bitmap = await createImageBitmap(file);
          const scale = Math.min(1, 1280 / Math.max(bitmap.width, bitmap.height));
          const canvas = document.createElement("canvas");
          canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
          canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
          blob = await new Promise((ok) => canvas.toBlob(ok, "image/jpeg", 0.85));
          preview = canvas.toDataURL("image/jpeg", 0.6);
        } catch (err) { /* browser cannot decode it: send the original and let the server decide */ }
        const sent = ("[photo] " + caption).trim();
        const fd = new FormData();
        fd.append("messages", JSON.stringify(history.concat([{ role: "user", content: caption }])));
        fd.append("filters", JSON.stringify(currentFilters()));
        fd.append("photo", blob, "photo.jpg");
        send(fd, { text: caption || "Photo", sent, image: preview });
      });
    }

    // ---- voice search: record 16 kHz mono PCM and wrap it as WAV, which every browser can produce
    // without a codec and the model accepts as is.
    const mic = document.getElementById("chat-mic");
    const recordingHint = document.getElementById("chat-recording");
    if (mic && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.AudioContext) {
      mic.hidden = false;
      let rec = null;
      const MAX_SECONDS = 30;

      const wavBlob = (chunks, sampleRate) => {
        const length = chunks.reduce((n, c) => n + c.length, 0);
        const buf = new ArrayBuffer(44 + length * 2), v = new DataView(buf);
        const str = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
        str(0, "RIFF"); v.setUint32(4, 36 + length * 2, true); str(8, "WAVE"); str(12, "fmt ");
        v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
        v.setUint32(24, sampleRate, true); v.setUint32(28, sampleRate * 2, true); v.setUint16(32, 2, true);
        v.setUint16(34, 16, true); str(36, "data"); v.setUint32(40, length * 2, true);
        let o = 44;
        for (const c of chunks) for (let i = 0; i < c.length; i++, o += 2) {
          const x = Math.max(-1, Math.min(1, c[i])); v.setInt16(o, x < 0 ? x * 0x8000 : x * 0x7fff, true);
        }
        return new Blob([buf], { type: "audio/wav" });
      };

      const stop = () => {
        if (!rec) return;
        const { stream, ctx, chunks, timer } = rec;
        rec = null;
        clearTimeout(timer);
        stream.getTracks().forEach((t) => t.stop());
        const rate = ctx.sampleRate;
        ctx.close();
        mic.classList.remove("recording"); recordingHint.hidden = true;
        const seconds = chunks.reduce((n, c) => n + c.length, 0) / rate;
        if (seconds < 0.5) { say("assistant", "That was too short. Hold on a moment after tapping the microphone, then speak."); return; }
        const fd = new FormData();
        fd.append("messages", JSON.stringify(history.concat([{ role: "user", content: "[voice message]" }])));
        fd.append("filters", JSON.stringify(currentFilters()));
        fd.append("audio", wavBlob(chunks, rate), "voice.wav");
        send(fd, { text: "🎤 Voice message (" + Math.round(seconds) + " s)", sent: "[voice message]" });
      };

      mic.addEventListener("click", async () => {
        if (rec) { stop(); return; }
        let stream;
        try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
        catch (err) { say("assistant", "Microphone access was refused. You can type your search instead."); return; }
        const ctx = new AudioContext({ sampleRate: 16000 });
        const source = ctx.createMediaStreamSource(stream);
        const proc = ctx.createScriptProcessor(4096, 1, 1);
        const chunks = [];
        proc.onaudioprocess = (e) => { if (rec) chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
        source.connect(proc); proc.connect(ctx.destination);
        rec = { stream, ctx, chunks, timer: setTimeout(stop, MAX_SECONDS * 1000) };
        mic.classList.add("recording"); recordingHint.hidden = false;
      });
    }

    reset.addEventListener("click", () => { window.location.href = "/"; });
  }

  // ---------------------------------------------------------------- photo viewer
  const viewer = document.getElementById("viewer");
  if (viewer) {
    const imgs = [...viewer.querySelectorAll(".viewer-img")];
    const thumbs = [...viewer.querySelectorAll(".thumb")];
    const pos = document.getElementById("viewer-pos");
    const box = document.getElementById("lightbox");
    const boxImg = document.getElementById("lightbox-img");
    let current = 0;

    const show = (i) => {
      current = (i + imgs.length) % imgs.length;
      imgs.forEach((img, j) => img.classList.toggle("active", j === current));
      thumbs.forEach((t, j) => t.classList.toggle("active", j === current));
      pos.textContent = current + 1;
      boxImg.src = imgs[current].src;
      boxImg.alt = imgs[current].alt;
      if (thumbs[current]) thumbs[current].scrollIntoView({ block: "nearest", inline: "nearest" });
    };

    viewer.querySelectorAll(".prev").forEach((b) => b.addEventListener("click", () => show(current - 1)));
    viewer.querySelectorAll(".next").forEach((b) => b.addEventListener("click", () => show(current + 1)));
    thumbs.forEach((t) => t.addEventListener("click", () => show(Number(t.dataset.index))));

    const open = () => { show(current); box.showModal(); };
    viewer.querySelector(".viewer-expand").addEventListener("click", open);
    imgs.forEach((img) => img.addEventListener("click", open));
    box.querySelector(".lb-close").addEventListener("click", () => box.close());
    box.querySelectorAll(".lb-nav.prev").forEach((b) => b.addEventListener("click", () => show(current - 1)));
    box.querySelectorAll(".lb-nav.next").forEach((b) => b.addEventListener("click", () => show(current + 1)));
    box.addEventListener("click", (e) => { if (e.target === box) box.close(); }); // backdrop

    document.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft") show(current - 1);
      else if (e.key === "ArrowRight") show(current + 1);
    });

    // Swipe on touch screens.
    let startX = null;
    const stage = viewer.querySelector(".viewer-stage");
    [stage, box].forEach((node) => {
      node.addEventListener("touchstart", (e) => { startX = e.touches[0].clientX; }, { passive: true });
      node.addEventListener("touchend", (e) => {
        if (startX === null) return;
        const dx = e.changedTouches[0].clientX - startX;
        if (Math.abs(dx) > 40) show(current + (dx < 0 ? 1 : -1));
        startX = null;
      });
    });
    show(0);
  }

  // ---------------------------------------------------------------- listing map
  const listingMap = document.getElementById("listing-map");
  if (listingMap && hasMap) {
    const lat = Number(listingMap.dataset.lat), lng = Number(listingMap.dataset.lng);
    const map = baseMap(listingMap, lat, lng, 14);
    L.circleMarker([lat, lng], pinStyle).addTo(map);
  }

  // ---------------------------------------------------------------- pin picker (agent review)
  const pinMap = document.getElementById("pin-map");
  if (pinMap && hasMap) {
    const latInput = document.getElementById("pin-lat"), lngInput = document.getElementById("pin-lng");
    const map = baseMap(pinMap, Number(pinMap.dataset.lat), Number(pinMap.dataset.lng), Number(pinMap.dataset.zoom));
    let marker = null;
    const place = (latlng) => {
      if (!marker) {
        marker = L.marker(latlng, { draggable: true, keyboard: true, title: "Site location" }).addTo(map);
        marker.on("dragend", () => place(marker.getLatLng()));
      }
      marker.setLatLng(latlng);
      latInput.value = latlng.lat.toFixed(6);
      lngInput.value = latlng.lng.toFixed(6);
    };
    if (pinMap.dataset.set === "1") place(L.latLng(Number(pinMap.dataset.lat), Number(pinMap.dataset.lng)));
    map.on("click", (e) => place(e.latlng));
  }

  // ---------------------------------------------------------------- phone reveal
  const reveal = document.getElementById("reveal");
  if (reveal) {
    reveal.addEventListener("click", async () => {
      reveal.disabled = true;
      const res = await fetch("/api/listings/" + reveal.dataset.listing + "/phone", {
        method: "POST", headers: { "X-Requested-With": "fetch" },
      });
      const out = document.getElementById("phone");
      if (res.ok) {
        const d = await res.json();
        out.textContent = d.name + ": " + d.phone;
        reveal.hidden = true;
      } else {
        out.textContent = "Could not load the number. Please try again.";
        reveal.disabled = false;
      }
      out.hidden = false;
    });
  }

  // ---------------------------------------------------------------- new listing
  const photoInput = document.getElementById("photo-input");
  if (photoInput) {
    photoInput.addEventListener("change", () => {
      const box = document.getElementById("photo-preview");
      box.replaceChildren();
      for (const file of photoInput.files) {
        const img = el("img");
        img.src = URL.createObjectURL(file);
        img.alt = "";
        box.appendChild(img);
      }
    });
    // Submit with fetch and show each step of the draft graph as it finishes (the server streams
    // one JSON line per step). Without JavaScript the form posts normally and redirects when done.
    const form = document.getElementById("new-listing");
    form.addEventListener("submit", async (e) => {
      if (!window.fetch || !window.TextDecoder || !window.ReadableStream) return;
      e.preventDefault();
      const button = document.getElementById("generate-btn");
      const wait = document.getElementById("generate-wait");
      const steps = document.getElementById("generate-steps");
      button.disabled = true; wait.hidden = false; steps.replaceChildren(); steps.hidden = false;
      const addStep = (text, cls) => {
        const prev = steps.lastElementChild; if (prev) prev.classList.add("done");
        const li = el("li", cls || "", text); steps.appendChild(li); return li;
      };
      const fail = (text) => { addStep(text, "error"); button.disabled = false; wait.hidden = true; };
      let res;
      try {
        res = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { Accept: "application/x-ndjson" } });
      } catch (err) { fail("Could not reach the server. Please try again."); return; }
      if (!(res.headers.get("content-type") || "").includes("application/x-ndjson")) {
        // A validation error comes back as the normal page: show it.
        const html = await res.text(); document.open(); document.write(html); document.close(); return;
      }
      const reader = res.body.getReader(), decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        let nl;
        while ((nl = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, nl).trim(); buffer = buffer.slice(nl + 1);
          if (!line) continue;
          const msg = JSON.parse(line);
          if (msg.step) addStep(msg.step);
          if (msg.error) { fail(msg.error); return; }
          if (msg.done) { addStep("Done. Opening your drafts…", "done"); window.location.href = msg.redirect; return; }
        }
        if (done) break;
      }
      fail("The connection closed before the drafts were ready. Please try again.");
    });
  }

  // Revising a draft can take a few seconds: say so, and stop double submits.
  document.querySelectorAll(".revise-form").forEach((f) => {
    f.addEventListener("submit", () => {
      document.querySelectorAll(`button[form="${f.id}"]`).forEach((b) => { b.disabled = true; b.textContent = "Revising…"; });
    });
  });

  // Editing a draft selects it.
  document.querySelectorAll(".draft").forEach((label) => {
    label.addEventListener("input", () => { label.querySelector("input[type=radio]").checked = true; });
  });
})();
