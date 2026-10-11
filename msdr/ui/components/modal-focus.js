/** Small shared helpers for the app's open-boolean modal components. */
export function focusableIn(container) {
  return [...container.querySelectorAll(
    'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',
  )].filter(
    (el) =>
      !el.hidden &&
      el.getAttribute("aria-hidden") !== "true" &&
      (typeof el.getClientRects !== "function" || el.getClientRects().length > 0),
  );
}

function isTopmostModal(dialog) {
  const dialogs = dialog.ownerDocument.querySelectorAll('[role="dialog"][aria-modal="true"], dialog[open]');
  return dialogs[dialogs.length - 1] === dialog;
}

export function trapModalTab(event, dialog) {
  if (event.key !== "Tab" || !isTopmostModal(dialog)) return;
  const items = focusableIn(dialog);
  if (!items.length) {
    event.preventDefault();
    dialog.focus();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const active = dialog.ownerDocument.activeElement;
  if (event.shiftKey && (active === first || !dialog.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

export function containModalFocus(event, dialog, fallback) {
  if (isTopmostModal(dialog) && !dialog.contains(event.target)) {
    (fallback ?? focusableIn(dialog)[0] ?? dialog).focus();
  }
}

export function restoreModalFocus(element) {
  if (element?.isConnected && typeof element.focus === "function") element.focus();
}
