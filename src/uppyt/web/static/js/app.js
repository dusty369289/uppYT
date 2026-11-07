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

  // "Show more" asks for however many are showing now, plus a page. Some may have been marked seen since loading.
  document.body.addEventListener("htmx:configRequest", (event) => {
    if (event.target.id === "more-link") {
      event.detail.parameters.show = cards().length + Number(event.target.dataset.pageSize);
    }
  });

  const hideFallbacks = () => document.querySelectorAll("[data-hide-with-js]").forEach((element) => { element.hidden = true; });

  // Seen, Undo, Show more and Check now all swap away the control that had keyboard focus. Without this, the next Tab
  // starts again at the top of the page. Only when j/k isn't in use, since highlight() already keeps focus then.
  let refocus = null;
  const focusCard = (card) => { if (card) card.querySelector(".card__picture").focus({ preventScroll: true }); };
  const checkButton = () => document.querySelector('#results form[action="/refresh"] button');

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const elt = event.detail.elt;
    if (refocus && refocus.to === "check" && elt.matches(".tools")) return;  // the board a check asks for
    refocus = null;
    if (current >= 0 || !elt.contains(document.activeElement)) return;
    if (elt.matches(".card__seen")) refocus = { to: "seen", index: cards().indexOf(elt.closest(".card")) };
    else if (elt.closest(".toast")) refocus = { to: "undone", video: elt.getAttribute("hx-post").split("/")[2] };
    else if (elt.id === "more-link") refocus = { to: "more", index: cards().length };
    else if (checkButton() && elt === checkButton().form) refocus = { to: "check" };  // not on Settings
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    hideFallbacks();
    if (current >= 0 && cards().length) highlight(Math.min(current, cards().length - 1));
    // The check's own answer swaps nothing. The board it then asks for is what replaces the button.
    const asker = event.detail.requestConfig && event.detail.requestConfig.elt;
    if (refocus && refocus.to === "check" && asker && asker.matches(".tools") && event.target.id === "results") {
      refocus = null;
      const button = checkButton();
      if (button) button.focus({ preventScroll: true });
    }
  });

  document.body.addEventListener("htmx:afterRequest", (event) => {
    // The request's own element, since a card's form has already gone by the time this runs.
    const form = event.detail.requestConfig && event.detail.requestConfig.elt;
    if (!event.detail.successful || !form) return;
    const plan = refocus && refocus.to !== "check" ? refocus : null;
    if (plan) refocus = null;
    if (form.matches(".card__seen")) {
      const count = document.querySelector(".tools__count");
      if (count) count.textContent = `${Math.max(0, parseInt(count.textContent, 10) - 1)} unseen`;
      if (current >= 0) highlight(Math.min(current, cards().length - 1));
      else if (plan) {
        const all = cards();
        if (all.length) focusCard(all[Math.min(plan.index, all.length - 1)]);
        else if (checkButton()) checkButton().focus({ preventScroll: true });
      }
    } else if (form.closest(".toast")) {
      document.getElementById("toasts").replaceChildren();
      if (plan) focusCard(cards().find((card) => card.dataset.video === plan.video));
    } else if (plan && plan.to === "more") {
      const all = cards();
      focusCard(all[Math.min(plan.index, all.length - 1)]);
    }
  });

  // After checking for new videos, show them, by asking for the board again with the filters as they stand.
  document.body.addEventListener("uppyt-refreshed", () => {
    const filters = document.querySelector(".tools");
    if (filters) htmx.trigger(filters, "change");
  });

  // A toast fades after a while, unless someone is about to use it. Only the region's contents are swapped, so the
  // live region itself stays put and screen readers announce the change.
  let toastTimer;
  document.body.addEventListener("htmx:oobAfterSwap", (event) => {
    const region = event.detail.target;
    if (!region || region.id !== "toasts") return;
    clearTimeout(toastTimer);
    const fade = () => { if (!region.matches(":hover, :focus-within")) region.replaceChildren(); };
    toastTimer = setTimeout(fade, Number(region.dataset.fadeAfter));
  });

  // Choosing a filter applies it at once. The form still works without this, through its Apply button.
  hideFallbacks();
}());
