/* Gallery behaviour: the tag filter and the copy button. Plain JS.
   Page data is only ever read as attributes and written with textContent.
   Without this script every page is complete: all cards are visible. */
(function () {
  "use strict";

  function all(selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector));
  }

  function initFilter() {
    var buttons = all("#filters button");
    var count = document.getElementById("count");
    var cards = all(".card[data-tags]").map(function (el) {
      var tags = [];
      try {
        tags = JSON.parse(el.getAttribute("data-tags")) || [];
      } catch (e) {
        tags = [];
      }
      return { el: el, tags: tags };
    });

    function select(tag) {
      var shown = 0;
      buttons.forEach(function (button) {
        var on = (button.getAttribute("data-tag") || "") === tag;
        button.setAttribute("aria-pressed", on ? "true" : "false");
      });
      cards.forEach(function (card) {
        var show = tag === "" || card.tags.indexOf(tag) !== -1;
        card.el.hidden = !show;
        if (show) {
          shown += 1;
        }
      });
      if (count) {
        count.textContent =
          shown + (shown === 1 ? " template" : " templates") + ", sorted by stars";
      }
    }

    buttons.forEach(function (button) {
      button.hidden = false;
      button.addEventListener("click", function () {
        select(button.getAttribute("data-tag") || "");
      });
    });
  }

  function initCopy(button) {
    var line = button.parentNode;
    var code = line ? line.querySelector("code") : null;
    var timer = null;
    if (!code || !navigator.clipboard || !navigator.clipboard.writeText) {
      return;
    }
    button.hidden = false;
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(code.textContent).then(function () {
        button.classList.add("done");
        button.setAttribute("aria-label", "Copied");
        window.clearTimeout(timer);
        timer = window.setTimeout(function () {
          button.classList.remove("done");
          button.setAttribute("aria-label", "Copy command");
        }, 1500);
      }, function () {});
    });
  }

  var group = document.getElementById("filters");
  if (group) {
    initFilter();
  }
  all(".copy").forEach(initCopy);
})();
