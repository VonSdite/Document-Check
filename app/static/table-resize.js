(() => {
  "use strict";

  const MIN_COLUMN_WIDTH = 56;
  const KEYBOARD_RESIZE_STEP = 12;

  function tableHeaderCells(table) {
    const headerRow = table.tHead?.rows?.[0];
    if (!headerRow) {
      return [];
    }
    const cells = Array.from(headerRow.cells);
    if (
      cells.length < 2
      || cells.some((cell) => cell.colSpan !== 1 || cell.rowSpan !== 1)
    ) {
      return [];
    }
    return cells;
  }

  function resizeColgroup(table, columnCount) {
    let colgroup = Array.from(table.children).find(
      (child) => child instanceof HTMLTableColElement && child.dataset.tableResizeColumns === "true",
    );
    if (!colgroup) {
      colgroup = document.createElement("colgroup");
      colgroup.dataset.tableResizeColumns = "true";
      const reference = table.caption?.nextSibling || table.firstChild;
      table.insertBefore(colgroup, reference);
    }
    while (colgroup.children.length < columnCount) {
      colgroup.appendChild(document.createElement("col"));
    }
    while (colgroup.children.length > columnCount) {
      colgroup.lastElementChild?.remove();
    }
    return colgroup;
  }

  function prepareResize(table, headerCells) {
    const widths = headerCells.map((cell) => cell.getBoundingClientRect().width);
    const totalWidth = widths.reduce((total, width) => total + width, 0);
    if (!totalWidth || widths.some((width) => width <= 0)) {
      return null;
    }

    const blockLayout = getComputedStyle(table).display === "block";
    const colgroup = resizeColgroup(table, widths.length);
    Array.from(colgroup.children).forEach((column, index) => {
      column.style.width = blockLayout
        ? `${widths[index]}px`
        : `${(widths[index] / totalWidth) * 100}%`;
    });
    if (blockLayout) {
      Array.from(table.rows).forEach((row) => {
        Array.from(row.cells).forEach((cell) => {
          cell.style.minWidth = "0px";
        });
      });
    } else {
      headerCells.forEach((cell) => {
        cell.style.width = "auto";
      });
      table.classList.add("table-columns-resized");
    }
    return {
      blockLayout,
      columns: Array.from(colgroup.children),
      totalWidth,
      widths,
    };
  }

  function applyResize(state, boundaryIndex, delta) {
    const startLeftWidth = state.widths[boundaryIndex];
    const startRightWidth = state.widths[boundaryIndex + 1];
    const pairWidth = startLeftWidth + startRightWidth;
    const minLeftWidth = Math.min(MIN_COLUMN_WIDTH, startLeftWidth);
    const minRightWidth = Math.min(MIN_COLUMN_WIDTH, startRightWidth);
    const leftWidth = Math.min(
      Math.max(startLeftWidth + delta, minLeftWidth),
      pairWidth - minRightWidth,
    );
    const rightWidth = pairWidth - leftWidth;

    state.columns[boundaryIndex].style.width = state.blockLayout
      ? `${leftWidth}px`
      : `${(leftWidth / state.totalWidth) * 100}%`;
    state.columns[boundaryIndex + 1].style.width = state.blockLayout
      ? `${rightWidth}px`
      : `${(rightWidth / state.totalWidth) * 100}%`;
    return { leftWidth, minLeftWidth, rightWidth };
  }

  function headerLabel(cell, index) {
    const copy = cell.cloneNode(true);
    copy.querySelectorAll(".table-column-resizer, .help-tip, [aria-hidden='true']").forEach((node) => node.remove());
    return copy.textContent.trim() || `第 ${index + 1} 列`;
  }

  function updateSeparatorValue(separator, state, boundaryIndex, resized) {
    const pairWidth = state.widths[boundaryIndex] + state.widths[boundaryIndex + 1];
    const minRightWidth = Math.min(MIN_COLUMN_WIDTH, state.widths[boundaryIndex + 1]);
    separator.setAttribute("aria-valuemin", String(Math.round(resized.minLeftWidth)));
    separator.setAttribute("aria-valuemax", String(Math.round(pairWidth - minRightWidth)));
    separator.setAttribute("aria-valuenow", String(Math.round(resized.leftWidth)));
  }

  function beginPointerResize(event, table, headerCells, separator, boundaryIndex) {
    if (event.button !== 0) {
      return;
    }
    const state = prepareResize(table, headerCells);
    if (!state) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    separator.classList.add("is-active");
    document.body.classList.add("table-column-resize-active");
    separator.setPointerCapture?.(event.pointerId);

    const move = (moveEvent) => {
      const resized = applyResize(state, boundaryIndex, moveEvent.clientX - startX);
      updateSeparatorValue(separator, state, boundaryIndex, resized);
    };
    const finish = () => {
      separator.classList.remove("is-active");
      document.body.classList.remove("table-column-resize-active");
      separator.removeEventListener("pointermove", move);
      separator.removeEventListener("pointerup", finish);
      separator.removeEventListener("pointercancel", finish);
      separator.removeEventListener("lostpointercapture", finish);
    };

    separator.addEventListener("pointermove", move);
    separator.addEventListener("pointerup", finish);
    separator.addEventListener("pointercancel", finish);
    separator.addEventListener("lostpointercapture", finish);
  }

  function resizeWithKeyboard(event, table, headerCells, separator, boundaryIndex) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
      return;
    }
    const state = prepareResize(table, headerCells);
    if (!state) {
      return;
    }
    event.preventDefault();
    const direction = event.key === "ArrowLeft" ? -1 : 1;
    const resized = applyResize(state, boundaryIndex, direction * KEYBOARD_RESIZE_STEP);
    updateSeparatorValue(separator, state, boundaryIndex, resized);
  }

  function enhanceTable(table) {
    if (!(table instanceof HTMLTableElement) || table.dataset.tableResizeReady === "true") {
      return;
    }
    const headerCells = tableHeaderCells(table);
    if (headerCells.length < 2) {
      return;
    }

    table.dataset.tableResizeReady = "true";
    headerCells.slice(0, -1).forEach((cell, boundaryIndex) => {
      const separator = document.createElement("span");
      separator.className = "table-column-resizer";
      separator.dataset.tableColumnResizer = String(boundaryIndex);
      separator.tabIndex = 0;
      separator.setAttribute("role", "separator");
      separator.setAttribute("aria-orientation", "vertical");
      separator.setAttribute(
        "aria-label",
        `调整${headerLabel(cell, boundaryIndex)}与${headerLabel(headerCells[boundaryIndex + 1], boundaryIndex + 1)}的列宽`,
      );
      separator.addEventListener("pointerdown", (event) => {
        beginPointerResize(event, table, headerCells, separator, boundaryIndex);
      });
      separator.addEventListener("keydown", (event) => {
        resizeWithKeyboard(event, table, headerCells, separator, boundaryIndex);
      });
      cell.classList.add("table-column-resizable");
      cell.appendChild(separator);
    });
  }

  function enhanceTables(root) {
    if (root instanceof HTMLTableElement) {
      enhanceTable(root);
    }
    if (root instanceof Document || root instanceof DocumentFragment || root instanceof Element) {
      root.querySelectorAll("table").forEach(enhanceTable);
    }
  }

  function initializeTableResize() {
    enhanceTables(document);
    const observer = new MutationObserver((records) => {
      records.forEach((record) => {
        record.addedNodes.forEach((node) => enhanceTables(node));
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeTableResize, { once: true });
  } else {
    initializeTableResize();
  }
})();
