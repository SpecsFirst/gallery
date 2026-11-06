/* Gallery behaviour: filters, sort, URL state and copy buttons. Plain JS.
   Page data is only ever read as attributes and written with textContent. */
(function () {
  "use strict";

  function all(root, selector) {
    return Array.prototype.slice.call(root.querySelectorAll(selector));
  }

  function compareText(a, b) {
    return a < b ? -1 : a > b ? 1 : 0;
  }

  function initGallery(list) {
    var filters = document.getElementById("filters");
    var sortSelect = document.getElementById("sort");
    var count = document.getElementById("count");
    var empty = document.getElementById("empty");
    var clearButtons = [document.getElementById("clear"), document.getElementById("empty-clear")];
    var kindInputs = all(filters, 'input[name="kind"]');
    var tagInputs = all(filters, 'input[name="tag"]');

    var items = all(list, ".card").map(function (el) {
      var tags = [];
      try {
        tags = JSON.parse(el.getAttribute("data-tags")) || [];
      } catch (e) {
        tags = [];
      }
      var updated = el.getAttribute("data-updated");
      return {
        el: el,
        id: el.getAttribute("data-id") || "",
        name: el.getAttribute("data-name") || "",
        kind: el.getAttribute("data-kind") || "",
        tags: tags,
        votes: Number(el.getAttribute("data-votes")) || 0,
        updated: updated === "" || updated === null ? null : Number(updated)
      };
    });

    function readUrl() {
      var params = new URLSearchParams(window.location.search);
      var kind = params.get("kind") || "";
      var tags = params.getAll("tag");
      kindInputs.forEach(function (input) {
        input.checked = input.value === kind;
      });
      if (!kindInputs.some(function (input) { return input.checked; })) {
        kindInputs[0].checked = true;
      }
      tagInputs.forEach(function (input) {
        input.checked = tags.indexOf(input.value) !== -1;
      });
      sortSelect.value = params.get("sort") === "updated" ? "updated" : "votes";
    }

    function readControls() {
      var kind = "";
      kindInputs.forEach(function (input) {
        if (input.checked) { kind = input.value; }
      });
      return {
        kind: kind,
        tags: tagInputs.filter(function (input) { return input.checked; })
          .map(function (input) { return input.value; }),
        sort: sortSelect.value === "updated" ? "updated" : "votes"
      };
    }

    function writeUrl(state) {
      var params = new URLSearchParams();
      if (state.kind) { params.set("kind", state.kind); }
      state.tags.forEach(function (tag) { params.append("tag", tag); });
      if (state.sort !== "votes") { params.set("sort", state.sort); }
      var query = params.toString();
      window.history.replaceState(
        null, "",
        window.location.pathname + (query ? "?" + query : "") + window.location.hash
      );
    }

    function comparator(sort) {
      return function (a, b) {
        if (sort === "updated") {
          if (a.updated !== b.updated) {
            if (a.updated === null) { return 1; }
            if (b.updated === null) { return -1; }
            return b.updated - a.updated;
          }
        } else if (a.votes !== b.votes) {
          return b.votes - a.votes;
        }
        return compareText(a.name, b.name) || compareText(a.id, b.id);
      };
    }

    function matches(item, state) {
      return (!state.kind || item.kind === state.kind) &&
        state.tags.every(function (tag) { return item.tags.indexOf(tag) !== -1; });
    }

    function update() {
      var state = readControls();
      var shown = 0;
      items.slice().sort(comparator(state.sort)).forEach(function (item) {
        list.appendChild(item.el);
        var visible = matches(item, state);
        item.el.hidden = !visible;
        if (visible) { shown += 1; }
      });
      count.textContent = "Showing " + shown + " of " + items.length +
        (items.length === 1 ? " template" : " templates");
      count.hidden = false;
      empty.hidden = shown !== 0;
      clearButtons[0].hidden = !state.kind && state.tags.length === 0;
      writeUrl(state);
    }

    function clear() {
      kindInputs[0].checked = true;
      tagInputs.forEach(function (input) { input.checked = false; });
      update();
    }

    filters.addEventListener("change", update);
    clearButtons.forEach(function (button) {
      if (button) { button.addEventListener("click", clear); }
    });

    readUrl();
    filters.hidden = false;
    // On a narrow screen the templates come first, unless a shared link set filters.
    var query = new URLSearchParams(window.location.search);
    if (window.matchMedia("(max-width: 40rem)").matches && !query.has("kind") && !query.has("tag")) {
      document.getElementById("filter-panel").open = false;
    }
    update();
  }

  function initCopyButtons() {
    if (!navigator.clipboard) { return; }
    all(document, ".command").forEach(function (block) {
      var code = block.querySelector("code");
      if (!code) { return; }
      var button = document.createElement("button");
      button.type = "button";
      button.className = "copy";
      button.textContent = "Copy command";
      button.setAttribute("aria-live", "polite");
      button.addEventListener("click", function () {
        navigator.clipboard.writeText(code.textContent).then(function () {
          button.textContent = "Copied";
        }, function () {
          button.textContent = "Copy failed";
        });
        window.setTimeout(function () { button.textContent = "Copy command"; }, 2000);
      });
      block.appendChild(button);
    });
  }

  var list = document.getElementById("cards");
  if (list && document.getElementById("filters")) { initGallery(list); }
  initCopyButtons();
})();
