/* Gallery behaviour: the tag filter and the copy button. Plain JS.
   Page data is only ever read as attributes and written with textContent.
   Without this script every page is complete: all cards are visible. */
(function () {
  "use strict";

  function all(selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector));
  }

  function initFilter() {
    var placementButtons = all("#placement button");
    var tagButtons = all("#filters button");
    var count = document.getElementById("count");
    var cards = all(".card[data-tags]").map(function (el) {
      var tags = [];
      try {
        tags = JSON.parse(el.getAttribute("data-tags")) || [];
      } catch (e) {
        tags = [];
      }
      return {
        el: el,
        tags: tags,
        placement: el.getAttribute("data-placement") || "standalone"
      };
    });

    function render() {
      var placement = "";
      placementButtons.forEach(function (button) {
        if (button.getAttribute("aria-pressed") === "true") {
          placement = button.getAttribute("data-placement") || "";
        }
      });
      var tag = "";
      tagButtons.forEach(function (button) {
        if (button.getAttribute("aria-pressed") === "true") {
          tag = button.getAttribute("data-tag") || "";
        }
      });
      var shown = 0;
      cards.forEach(function (card) {
        var show =
          (placement === "" || card.placement === placement) &&
          (tag === "" || card.tags.indexOf(tag) !== -1);
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

    function wire(buttons, attr) {
      buttons.forEach(function (button) {
        button.hidden = false;
        button.addEventListener("click", function () {
          var value = button.getAttribute(attr) || "";
          buttons.forEach(function (other) {
            other.setAttribute(
              "aria-pressed",
              (other.getAttribute(attr) || "") === value ? "true" : "false"
            );
          });
          render();
        });
      });
    }

    wire(placementButtons, "data-placement");
    wire(tagButtons, "data-tag");
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
