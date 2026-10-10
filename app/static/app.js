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

  const pinStyle = { radius: 9, weight: 2, color: "#ffffff", fillColor: "#1f5c4a", fillOpacity: 1 };

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

    chatForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      say("user", text);
      history.push({ role: "user", content: text });
      const pending = say("assistant pending", "Searching…");
      const button = chatForm.querySelector("button");
      button.disabled = true;
      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: history, filters: filterForm ? formValues() : {} }),
        });
        const data = await res.json();
        pending.remove();
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
    });

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
    document.getElementById("new-listing").addEventListener("submit", () => {
      document.getElementById("generate-btn").disabled = true;
      document.getElementById("generate-wait").hidden = false;
    });
  }

  // Editing a draft selects it.
  document.querySelectorAll(".draft").forEach((label) => {
    label.addEventListener("input", () => { label.querySelector("input[type=radio]").checked = true; });
  });
})();
