// Small progressive enhancements; every page still works as plain HTML forms
// except the chat search and the phone reveal, which need JavaScript.
(function () {
  "use strict";

  // ---- buyer chat search ----
  const form = document.getElementById("chat-form");
  if (form) {
    const log = document.getElementById("chat-log");
    const input = document.getElementById("chat-input");
    const results = document.getElementById("results");
    const heading = document.getElementById("results-heading");
    const filtersEl = document.getElementById("chat-filters");
    const reset = document.getElementById("chat-reset");
    let history = [];

    const say = (role, text) => {
      const p = document.createElement("p");
      p.className = "msg " + role;
      p.textContent = text; // textContent: never interpreted as HTML
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
      return p;
    };

    const showFilters = (f) => {
      filtersEl.replaceChildren();
      const labels = {
        property_type: (v) => v, min_bedrooms: (v) => v + "+ bed", max_bedrooms: (v) => "≤" + v + " bed",
        min_bathrooms: (v) => v + "+ bath", min_price: (v) => "from " + Number(v).toLocaleString(),
        max_price: (v) => "up to " + Number(v).toLocaleString(), location: (v) => v, keywords: (v) => "“" + v + "”",
      };
      for (const [k, v] of Object.entries(f || {})) {
        const vals = k === "features" ? v : [labels[k] ? labels[k](v) : v];
        for (const val of vals) {
          const s = document.createElement("span");
          s.textContent = val;
          filtersEl.appendChild(s);
        }
      }
    };

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      say("user", text);
      history.push({ role: "user", content: text });
      const pending = say("assistant pending", "Searching…");
      form.querySelector("button").disabled = true;
      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: history }),
        });
        const data = await res.json();
        pending.remove();
        say("assistant", data.reply || "Something went wrong.");
        if (res.ok) history.push({ role: "assistant", content: data.reply });
        else history.pop();
        if (data.html !== undefined) {
          results.innerHTML = data.html; // rendered server-side by autoescaping Jinja
          heading.textContent = data.count + " matching listing" + (data.count === 1 ? "" : "s");
          showFilters(data.filters);
        }
        reset.hidden = false;
      } catch (err) {
        pending.remove();
        history.pop();
        say("assistant", "Could not reach the server. Please try again.");
      } finally {
        form.querySelector("button").disabled = false;
        input.focus();
      }
    });

    reset.addEventListener("click", () => window.location.reload());
  }

  // ---- phone reveal (logged as a phone_reveal event) ----
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

  // ---- new listing: photo previews and a waiting message ----
  const photoInput = document.getElementById("photo-input");
  if (photoInput) {
    photoInput.addEventListener("change", () => {
      const box = document.getElementById("photo-preview");
      box.replaceChildren();
      for (const file of photoInput.files) {
        const img = document.createElement("img");
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

  // ---- review: editing a draft selects it ----
  document.querySelectorAll(".draft").forEach((label) => {
    label.addEventListener("input", () => { label.querySelector("input[type=radio]").checked = true; });
  });
})();
