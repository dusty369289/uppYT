// The little the server can't do: keyboard shortcuts, and paging from however many cards are still showing.
(function () {
  "use strict";

  const cards = () => [...document.querySelectorAll(".card")];
  let current = -1;

  function highlight(index) {
    const all = cards();
    if (!all.length) { current = -1; return; }
    current = Math.max(0, Math.min(index, all.length - 1));
    all.forEach((card, i) => {
      if (i === current) card.setAttribute("data-current", "true");
      else card.removeAttribute("data-current");
    });
    all[current].scrollIntoView({ block: "nearest" });
    all[current].querySelector(".card__picture").focus({ preventScroll: true });
  }

  function isTyping(target) {
    return target.closest("input, select, textarea, [contenteditable]");
  }

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey || isTyping(event.target)) return;
    const all = cards();
    if (event.key === "j") { highlight(current + 1); }
    else if (event.key === "k") { highlight(current - 1); }
    else if (event.key === "s" && all[current]) { all[current].querySelector(".card__seen button").click(); }
    else if (event.key === "o" && all[current]) { all[current].querySelector(".card__picture").click(); }
    else if (event.key === "u") {
      const undo = document.querySelector(".toast form button");
      if (undo) undo.click(); else return;
    } else { return; }
    event.preventDefault();
  });

  // Marking cards seen removes them, so "load more" asks from how many are left, not from a fixed offset.
  document.body.addEventListener("htmx:configRequest", (event) => {
    if (event.target.id === "more-link") event.detail.parameters.offset = cards().length;
  });

  const hideFallbacks = () => document.querySelectorAll("[data-hide-with-js]").forEach((element) => { element.hidden = true; });

  document.body.addEventListener("htmx:afterSwap", () => {
    hideFallbacks();
    if (current >= 0 && cards().length) highlight(Math.min(current, cards().length - 1));
  });

  document.body.addEventListener("htmx:afterRequest", (event) => {
    const form = event.detail.elt;
    if (!event.detail.successful || !form.matches) return;
    if (form.matches(".card__seen")) {
      const count = document.querySelector(".tools__count");
      if (count) count.textContent = `${Math.max(0, parseInt(count.textContent, 10) - 1)} unseen`;
      if (current >= 0) highlight(Math.min(current, cards().length - 1));
    } else if (form.closest(".toast")) {
      document.getElementById("toasts").replaceChildren();
    }
  });

  // A toast fades after a while, unless someone is about to use it.
  let toastTimer;
  document.body.addEventListener("htmx:oobAfterSwap", (event) => {
    if (event.target.id !== "toasts") return;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { if (!event.target.matches(":hover, :focus-within")) event.target.replaceChildren(); }, 8000);
  });

  // Choosing a filter applies it at once. The form still works without this, through its Apply button.
  hideFallbacks();
}());
