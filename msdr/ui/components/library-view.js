import { LitElement, html } from "../vendor/lit.js";
import { mhz, bw, relativeOrDate } from "../format.js";
import { antennaDisplay } from "../antenna.js";
import * as api from "../api.js";
import "./led-dot.js";
import "./tag-chips.js";
import "./failure-note.js";

const PAGE_SIZE = 50;
const SEARCH_DEBOUNCE_MS = 250;
// Every ids-across-pages fetch (select-all-matching) uses the contract's max.
const IDS_PAGE_LIMIT = 200;
// Above this many rows, DELETE asks for confirmation first.
const DELETE_CONFIRM_AT = 10;

const STATUSES = ["", "unknown", "tentative", "identified"];
const STATUS_LABELS = { "": "ALL STATUSES", unknown: "unknown", tentative: "tentative", identified: "identified" };

// Same three bands as catalog-view.js's BANDS/bandRange -- kept as its own
// copy (catalog-view doesn't export either) rather than a shared import,
// matching how each view already owns its band chip/select.
const BANDS = [
  { value: "", label: "ALL BANDS" },
  { value: "lw_mw", label: "LW/MW <1.8 MHZ" },
  { value: "hf", label: "HF 1.8-30" },
  { value: "vhf", label: "VHF+" },
];

function bandRange(band) {
  if (band === "lw_mw") return { hi_hz: 1_800_000 };
  if (band === "hf") return { lo_hz: 1_800_000, hi_hz: 30_000_000 };
  if (band === "vhf") return { lo_hz: 30_000_000 };
  return {};
}

const SORTS = [
  { value: "name", label: "NAME" },
  { value: "frequency", label: "FREQUENCY" },
  { value: "last_heard", label: "LAST HEARD" },
  { value: "heard_count", label: "HEARD COUNT" },
  { value: "created", label: "CREATED" },
];

// Must match msdr/library/models.py SIGNAL_COLORS (see contract.md), same
// set drawer.js's signal color swatch row uses.
const SIGNAL_COLORS = ["red", "orange", "yellow", "green", "blue", "magenta", "cyan", "white"];

// Built-in list id/name (see msdr/api contract for v0.11.0 "Lists") --
// always present, can't be renamed or deleted.
const FAVORITES_ID = "favorites";

/** Full-width memory browser (v0.10.0 "Library", v0.11.0 "Lists"): server-
 * paged/filtered/sorted table over GET /api/library, multi-row selection
 * (this page, or every matching row via "select all N matching"), bulk
 * tag/status/color/list/delete with a one-step undo, named lists (built-in
 * Favorites + user lists) with drag-free ▲▼/◂▸ reordering, saved searches,
 * and a duplicate-detection/merge panel. Rendered by app.js in place of
 * `.grid` while open, same as any other section reaching the backend
 * directly. */
export class LibraryView extends LitElement {
  static properties = {
    tags: { type: Array },
    tagsById: { type: Object },
    failure: { type: Object },
    refreshAt: { type: Number }, // bumped by app.js after a ws "inbox" push or a global undo
    _query: { state: true },
    _status: { state: true },
    _tag: { state: true },
    _band: { state: true },
    _sort: { state: true },
    _order: { state: true },
    _page: { state: true },
    _items: { state: true },
    _total: { state: true },
    _selected: { state: true },
    _selectingAll: { state: true },
    _bulkTagId: { state: true },
    _bulkStatus: { state: true },
    _bulkListId: { state: true },
    _undo: { state: true },
    _lists: { state: true },
    _list: { state: true },
    _addingList: { state: true },
    _newListName: { state: true },
    _renaming: { state: true },
    _renameValue: { state: true },
    _searches: { state: true },
    _searchesOpen: { state: true },
    _duplicatesOpen: { state: true },
    _duplicateTolerance: { state: true },
    _duplicateGroups: { state: true },
    _duplicateKeep: { state: true },
    _duplicatesLoading: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.tags = [];
    this.tagsById = {};
    this.failure = null;
    this.refreshAt = null;
    this._query = "";
    this._status = "";
    this._tag = "";
    this._band = "";
    this._sort = "last_heard";
    this._order = "desc";
    this._page = 0;
    this._items = [];
    this._total = 0;
    this._selected = new Set();
    this._selectingAll = false;
    this._bulkTagId = "";
    this._bulkStatus = "";
    this._bulkListId = "";
    this._undo = null;
    this._lastUndoId = null;
    this._lists = [];
    this._list = "";
    this._addingList = false;
    this._newListName = "";
    this._renaming = false;
    this._renameValue = "";
    this._searches = [];
    this._searchesOpen = false;
    this._duplicatesOpen = false;
    this._duplicateTolerance = 100;
    this._duplicateGroups = [];
    this._duplicateKeep = new Map();
    this._duplicatesLoading = false;
    this._debounce = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._load();
    this._loadUndo();
    this._loadLists();
    this._loadSearches();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearTimeout(this._debounce);
  }

  /** app.js bumps `refreshAt` after a ws "inbox" push (a library change
   * elsewhere) and after the global Cmd/Ctrl-Z undo -- reload the current
   * page, list counts, and the undo label rather than resetting filters/
   * selection. */
  updated(changed) {
    if (changed.has("refreshAt") && changed.get("refreshAt") !== undefined) {
      this._load();
      this._loadUndo();
      this._loadLists();
    }
    // `?selected` on <option> stops driving a <select> once the operator has
    // changed it by hand (the option becomes "dirty"), so a programmatic
    // filter change -- applying a saved search, picking a list -- would
    // update the query but leave the old choice showing. Sync the value
    // property after every render instead.
    const sync = { status: this._status, tag: this._tag, band: this._band, sort: this._sort };
    for (const [label, value] of Object.entries(sync)) {
      const el = this.querySelector(`select[aria-label="${label}"]`);
      if (el && el.value !== (value ?? "")) el.value = value ?? "";
    }
  }

  _filtersActive() {
    return !!this._query || !!this._status || !!this._tag || !!this._band;
  }

  /** True while a list is selected and no sort has been explicitly chosen --
   * the server defaults to list order in that case, and rows get ▲▼/Alt+
   * ArrowUp/Down reorder controls (contract: "default sort = list order
   * when list set and no sort given"). */
  _isListOrderMode() {
    return !!this._list && !this._sort;
  }

  _sortOptions() {
    return this._list ? [{ value: "", label: "LIST ORDER" }, ...SORTS] : SORTS;
  }

  _params(extra = {}) {
    const range = bandRange(this._band);
    return {
      q: this._query || undefined,
      status: this._status || undefined,
      tag: this._tag || undefined,
      list: this._list || undefined,
      lo_hz: range.lo_hz,
      hi_hz: range.hi_hz,
      sort: this._sort || undefined,
      order: this._sort ? this._order : undefined,
      ...extra,
    };
  }

  async _load() {
    // Query/filter/paging can overlap loads; only the latest request may write.
    const seq = (this._reqSeq = (this._reqSeq || 0) + 1);
    try {
      const res = await api.library(this._params({ offset: this._page * PAGE_SIZE, limit: PAGE_SIZE }));
      if (seq !== this._reqSeq) return;
      this._items = res.items ?? [];
      this._total = res.total ?? 0;
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
    } catch (err) {
      if (seq !== this._reqSeq) return;
      this._items = [];
      this._total = 0;
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  /** Degrades to no undo affordance rather than breaking the view -- this is
   * a convenience button, not core to browsing/editing the library. */
  async _loadUndo() {
    try {
      const res = await api.libraryUndoPeek();
      this._undo = res.undo ?? null;
      if (this._lastUndoId === null) this._lastUndoId = this._undo?.id ?? "";
      else if (this._undo?.id && this._undo.id !== this._lastUndoId) {
        this._notifyUndo(this._undo);
      }
    } catch {
      this._undo = null;
    }
  }

  _notifyUndo(undo, at = Date.now()) {
    if (!undo?.id) return;
    this._lastUndoId = undo.id;
    this.dispatchEvent(new CustomEvent("library-undoable", {
      detail: { undo, at }, bubbles: true, composed: true,
    }));
  }

  async _syncUndo(at = Date.now()) {
    try {
      const res = await api.libraryUndoPeek();
      this._undo = res.undo ?? null;
      if (this._undo?.id !== this._lastUndoId) this._notifyUndo(this._undo, at);
    } catch {
      // Undo availability is secondary to the edit itself.
    }
  }

  async _loadLists() {
    try {
      this._lists = await api.listLists();
    } catch {
      this._lists = [];
    }
  }

  async _loadSearches() {
    try {
      this._searches = await api.listSavedSearches();
    } catch {
      this._searches = [];
    }
  }

  _resetAndLoad() {
    this._page = 0;
    this._selected = new Set();
    this._load();
  }

  _onQueryInput(value) {
    this._query = value;
    clearTimeout(this._debounce);
    this._debounce = setTimeout(() => this._resetAndLoad(), SEARCH_DEBOUNCE_MS);
  }

  _onStatusChange(value) {
    this._status = value;
    this._resetAndLoad();
  }

  _onTagChange(value) {
    this._tag = value;
    this._resetAndLoad();
  }

  _onBandChange(value) {
    this._band = value;
    this._resetAndLoad();
  }

  _onSortChange(value) {
    this._sort = value;
    this._load();
  }

  _toggleOrder() {
    this._order = this._order === "asc" ? "desc" : "asc";
    this._load();
  }

  _pageCount() {
    return Math.max(1, Math.ceil(this._total / PAGE_SIZE));
  }

  _goPage(delta) {
    const next = this._page + delta;
    if (next < 0 || next >= this._pageCount()) return;
    this._page = next;
    this._load();
  }

  // --- lists -----------------------------------------------------------------

  _selectList(id) {
    this._list = id;
    this._sort = id ? "" : "last_heard";
    this._order = "desc";
    this._resetAndLoad();
  }

  _startAddList() {
    this._addingList = true;
    this._newListName = "";
  }

  _cancelAddList() {
    this._addingList = false;
    this._newListName = "";
  }

  async _onNewListKeydown(e) {
    if (e.key === "Escape") {
      this._cancelAddList();
      return;
    }
    if (e.key !== "Enter") return;
    const name = this._newListName.trim();
    if (!name) return;
    try {
      const created = await api.createList(name);
      this._addingList = false;
      this._newListName = "";
      await this._loadLists();
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
      this._selectList(created.id);
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _startRename(l) {
    this._renaming = true;
    this._renameValue = l.name;
  }

  async _onRenameKeydown(e, l) {
    if (e.key === "Escape") {
      this._renaming = false;
      return;
    }
    if (e.key !== "Enter") return;
    const name = this._renameValue.trim();
    if (!name) return;
    try {
      await api.patchList(l.id, { name });
      this._renaming = false;
      await this._loadLists();
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  async _deleteList(l) {
    if (!window.confirm(`Delete list "${l.name}"?`)) return;
    try {
      await api.deleteList(l.id);
      await this._loadLists();
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
      if (this._list === l.id) this._selectList("");
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  /** ◂▸ on the selected list chip -- swaps `position` with the neighboring
   * user list (built-ins are excluded from reordering). */
  async _moveList(l, delta) {
    const users = this._lists.filter((x) => !x.builtin).sort((a, b) => a.position - b.position);
    const idx = users.findIndex((x) => x.id === l.id);
    const j = idx + delta;
    if (idx < 0 || j < 0 || j >= users.length) return;
    try {
      const a = users[idx];
      const b = users[j];
      await api.patchList(a.id, { position: b.position });
      await api.patchList(b.id, { position: a.position });
      await this._loadLists();
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _isFavorite(s) {
    return (s.list_ids ?? []).includes(FAVORITES_ID);
  }

  async _toggleFavorite(s) {
    const undoAt = Date.now();
    const fav = this._isFavorite(s);
    try {
      if (fav) await api.removeListItems(FAVORITES_ID, [s.id]);
      else await api.addListItems(FAVORITES_ID, [s.id]);
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
      await this._syncUndo(undoAt);
      await this._load();
      await this._loadLists();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  /** ▲▼ row buttons / Alt+ArrowUp/Down -- moves a row within the *full* list
   * order (not just the current page), so it re-reads the list's full order
   * from the server before swapping and PUTting the permutation back. */
  async _moveRow(id, delta) {
    if (!this._list) return;
    try {
      const full = await api.listListItems(this._list);
      const ids = full.map((s) => s.id);
      const i = ids.indexOf(id);
      const j = i + delta;
      if (i < 0 || j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      await api.orderListItems(this._list, ids);
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
      await this._load();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _onRowKeydown(e, s, orderMode) {
    if (!orderMode || !e.altKey) return;
    if (e.key === "ArrowUp") {
      e.preventDefault();
      this._moveRow(s.id, -1);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      this._moveRow(s.id, 1);
    }
  }

  // --- saved searches ----------------------------------------------------------

  _toggleSearchesMenu() {
    this._searchesOpen = !this._searchesOpen;
  }

  _bandForRange(lo, hi) {
    for (const b of BANDS) {
      if (!b.value) continue;
      const r = bandRange(b.value);
      if ((r.lo_hz ?? undefined) === lo && (r.hi_hz ?? undefined) === hi) return b.value;
    }
    return "";
  }

  _applySearch(s) {
    const p = s.params || {};
    this._query = p.q || "";
    this._status = p.status || "";
    this._tag = p.tag || "";
    this._list = p.list || "";
    this._band = this._bandForRange(p.lo_hz, p.hi_hz);
    this._sort = p.sort || "";
    this._order = p.order || "desc";
    this._searchesOpen = false;
    this._resetAndLoad();
  }

  async _saveCurrentSearch() {
    const name = window.prompt("Save search as:");
    if (!name || !name.trim()) return;
    const { offset: _offset, limit: _limit, ...rest } = this._params();
    const params = Object.fromEntries(Object.entries(rest).filter(([, v]) => v !== undefined && v !== ""));
    try {
      await api.createSavedSearch(name.trim(), params);
      this._searchesOpen = false;
      await this._loadSearches();
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  async _deleteSearch(s) {
    if (!window.confirm(`Delete saved search "${s.name}"?`)) return;
    try {
      await api.deleteSavedSearch(s.id);
      await this._loadSearches();
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  // --- duplicates ----------------------------------------------------------------

  async _openDuplicates() {
    this._duplicatesOpen = true;
    await this._loadDuplicates();
  }

  _closeDuplicates() {
    this._duplicatesOpen = false;
  }

  _groupKey(group) {
    return group.map((s) => s.id).join(",");
  }

  /** Default "keep" pick: highest heard_count, tie -> oldest created_at. */
  _defaultKeep(group) {
    return [...group].sort((a, b) => {
      if ((b.heard_count ?? 0) !== (a.heard_count ?? 0)) return (b.heard_count ?? 0) - (a.heard_count ?? 0);
      return (a.created_at ?? "").localeCompare(b.created_at ?? "");
    })[0];
  }

  async _loadDuplicates() {
    this._duplicatesLoading = true;
    try {
      const res = await api.libraryDuplicates(this._duplicateTolerance);
      this._duplicateGroups = res.groups ?? [];
      const keep = new Map();
      for (const group of this._duplicateGroups) keep.set(this._groupKey(group), this._defaultKeep(group).id);
      this._duplicateKeep = keep;
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
    } catch (err) {
      this._duplicateGroups = [];
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._duplicatesLoading = false;
    }
  }

  _onToleranceChange(value) {
    const n = Number(value);
    this._duplicateTolerance = Number.isFinite(n) && n >= 0 ? n : this._duplicateTolerance;
    this._loadDuplicates();
  }

  _setKeep(key, id) {
    const next = new Map(this._duplicateKeep);
    next.set(key, id);
    this._duplicateKeep = next;
  }

  async _merge(group) {
    const key = this._groupKey(group);
    const keepId = this._duplicateKeep.get(key);
    const mergeIds = group.map((s) => s.id).filter((id) => id !== keepId);
    if (!keepId || !mergeIds.length) return;
    const undoAt = Date.now();
    try {
      await api.mergeLibrary(keepId, mergeIds);
      await this._syncUndo(undoAt);
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "merged duplicate memories" } }));
      await this._loadDuplicates();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  // --- selection -----------------------------------------------------------

  _toggleRow(id, checked) {
    const next = new Set(this._selected);
    if (checked) next.add(id);
    else next.delete(id);
    this._selected = next;
  }

  _headerChecked() {
    return this._items.length > 0 && this._items.every((it) => this._selected.has(it.id));
  }

  _toggleHeader(checked) {
    const next = new Set(this._selected);
    for (const it of this._items) {
      if (checked) next.add(it.id);
      else next.delete(it.id);
    }
    this._selected = next;
  }

  /** "select all N matching" -- loops GET /api/library at the contract's max
   * limit (200) across every page of the current filter, not just what's
   * rendered, so a bulk op can apply to the whole result set. */
  async _selectAllMatching() {
    this._selectingAll = true;
    try {
      const ids = new Set();
      let offset = 0;
      for (;;) {
        const res = await api.library(this._params({ offset, limit: IDS_PAGE_LIMIT }));
        for (const it of res.items ?? []) ids.add(it.id);
        offset += IDS_PAGE_LIMIT;
        if (!res.items?.length || offset >= (res.total ?? 0)) break;
      }
      this._selected = ids;
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._selectingAll = false;
    }
  }

  // --- row / bulk actions ----------------------------------------------------

  _select(item) {
    this.dispatchEvent(new CustomEvent("select-signal", { detail: { kind: "signal", item } }));
  }

  _tune(ev, item) {
    ev.stopPropagation();
    this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id: item.id } }));
  }

  /** U3: opens the signal-detail panel -- a separate ⓘ button next to the
   * existing favorite/tune/reorder buttons so row click (select-signal, the
   * drawer) keeps working. */
  _openSignal(ev, s) {
    ev.stopPropagation();
    this.dispatchEvent(
      new CustomEvent("signal-open", {
        detail: { frequency_hz: s.frequency_hz, signal_id: s.id },
        bubbles: true,
        composed: true,
      })
    );
  }

  async _bulk(op, value) {
    const ids = Array.from(this._selected);
    if (!ids.length) return;
    const undoAt = Date.now();
    try {
      const res = await api.libraryBulk(ids, op, value);
      this._undo = res.undo ?? null;
      this._notifyUndo(this._undo, undoAt);
      this._selected = new Set();
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
      this.dispatchEvent(
        new CustomEvent("action", {
          detail: { message: `${op} applied to ${res.affected} signal${res.affected === 1 ? "" : "s"}` },
        })
      );
      await this._load();
      await this._loadLists();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _bulkTagAdd() {
    if (this._bulkTagId) this._bulk("tag_add", this._bulkTagId);
  }

  _bulkTagRemove() {
    if (this._bulkTagId) this._bulk("tag_remove", this._bulkTagId);
  }

  _bulkSetStatus(value) {
    this._bulkStatus = value;
    if (value) this._bulk("set_status", value);
  }

  _bulkSetColor(color) {
    this._bulk("set_color", color);
  }

  _bulkListAdd() {
    if (this._bulkListId) this._bulk("list_add", this._bulkListId);
  }

  _bulkListRemove() {
    if (this._bulkListId) this._bulk("list_remove", this._bulkListId);
  }

  _bulkDelete() {
    const n = this._selected.size;
    if (n > DELETE_CONFIRM_AT && !window.confirm(`Delete ${n} memories?`)) return;
    this._bulk("delete", undefined);
  }

  async _onUndoClick() {
    const undoId = this._undo?.id;
    try {
      const res = await api.libraryUndo();
      this.dispatchEvent(new CustomEvent("library-undo-consumed", {
        detail: { id: undoId }, bubbles: true, composed: true,
      }));
      this._undo = res.undo ?? null;
      this._lastUndoId = this._undo?.id ?? "";
      this.dispatchEvent(new CustomEvent("dismiss-failure"));
      this.dispatchEvent(new CustomEvent("action", { detail: { message: `undone: ${res.undone}` } }));
      await this._load();
      await this._loadLists();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _capture() {
    this.dispatchEvent(new CustomEvent("capture"));
  }

  // --- render ----------------------------------------------------------------

  render() {
    return html`
      <div class="panel">
        <h2>Library</h2>
        ${this.failure
          ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
          : ""}
        ${this._duplicatesOpen ? this._duplicatesPanel() : this._libraryBody()}
      </div>
    `;
  }

  _libraryBody() {
    const filtersActive = this._filtersActive();
    return html`
      ${this._listStrip()}
      ${this._toolbar()}
      ${this._selected.size > 0 || this._undo ? this._bulkBar() : ""}
      ${this._list && !this._sort && filtersActive
        ? html`<p class="dim" style="font-size:0.85em;margin:0 0 8px">
            order only meaningful on the full list — clear filters to reorder
          </p>`
        : ""}
      ${!this._items.length
        ? html`<p class="empty">
            ${filtersActive || this._total > 0 ? "no memories match" : "library is empty — press S to save the current frequency"}
            ${!filtersActive && this._total === 0
              ? html`<button style="margin-left:8px" @click=${() => this._capture()}>＋ SAVE TO LIBRARY</button>`
              : ""}
          </p>`
        : html`
            <div class="lib-table-wrap">
              <table class="lib-table mono">
                <thead>
                  <tr>
                    <th style="width:24px">
                      <input
                        type="checkbox"
                        aria-label="select this page"
                        .checked=${this._headerChecked()}
                        @change=${(e) => this._toggleHeader(e.target.checked)}
                      />
                    </th>
                    <th style="width:16px"></th>
                    <th>Name</th>
                    <th>Frequency</th>
                    <th>Mode/BW</th>
                    <th>Status</th>
                    <th>Tags</th>
                    <th>Heard</th>
                    <th>Last heard</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  ${this._items.map((s, i) => this._row(s, i))}
                </tbody>
              </table>
            </div>
            <div style="display:flex;align-items:center;gap:8px;margin-top:8px;font-size:0.85em">
              <button
                style="font-size:0.85em"
                ?disabled=${this._page === 0}
                @click=${() => this._goPage(-1)}
              >
                ‹ prev
              </button>
              <span class="dim mono">
                ${this._total === 0 ? "0–0 of 0" : `${this._page * PAGE_SIZE + 1}–${Math.min((this._page + 1) * PAGE_SIZE, this._total)} of ${this._total}`}
              </span>
              <button
                style="font-size:0.85em"
                ?disabled=${(this._page + 1) * PAGE_SIZE >= this._total}
                @click=${() => this._goPage(1)}
              >
                next ›
              </button>
              ${this._total > this._items.length
                ? html`<button
                    style="font-size:0.85em"
                    ?disabled=${this._selectingAll}
                    @click=${() => this._selectAllMatching()}
                  >
                    select all ${this._total} matching
                  </button>`
                : ""}
            </div>
          `}
    `;
  }

  _listStrip() {
    const selected = this._lists.find((l) => l.id === this._list);
    return html`
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
        ${this._listChip("", "ALL")}
        ${this._lists.map((l) => this._listChip(l.id, l.builtin ? `★ ${l.name.toUpperCase()}` : `${l.name} (${l.count})`, l))}
        ${this._addingList
          ? html`<input
              aria-label="new list name"
              style="width:140px"
              .value=${this._newListName}
              @input=${(e) => (this._newListName = e.target.value)}
              @keydown=${(e) => this._onNewListKeydown(e)}
              @blur=${() => this._cancelAddList()}
            />`
          : html`<button class="tag-chip mono" @click=${() => this._startAddList()}>+ LIST</button>`}
        ${selected && !selected.builtin ? this._listControls(selected) : ""}
      </div>
    `;
  }

  _listChip(id, label, l) {
    const active = this._list === id;
    return html`
      <button
        class="tag-chip mono ${active ? "selected" : ""}"
        aria-pressed=${active}
        style="border-color:${active ? "var(--accent)" : "transparent"};${active ? "color:var(--accent)" : ""}"
        @click=${() => this._selectList(id)}
      >
        ${label}
      </button>
    `;
  }

  _listControls(l) {
    return html`
      ${this._renaming
        ? html`<input
            aria-label="rename list"
            style="width:140px"
            .value=${this._renameValue}
            @input=${(e) => (this._renameValue = e.target.value)}
            @keydown=${(e) => this._onRenameKeydown(e, l)}
            @blur=${() => (this._renaming = false)}
          />`
        : html`<button style="font-size:0.85em" @click=${() => this._startRename(l)}>RENAME</button>`}
      <button style="font-size:0.85em;color:var(--danger)" @click=${() => this._deleteList(l)}>DELETE</button>
      <button title="move earlier" aria-label="move ${l.name} earlier" style="font-size:0.85em" @click=${() => this._moveList(l, -1)}>◂</button>
      <button title="move later" aria-label="move ${l.name} later" style="font-size:0.85em" @click=${() => this._moveList(l, 1)}>▸</button>
    `;
  }

  _toolbar() {
    return html`
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
        <input
          name="library-query"
          style="flex:1;min-width:160px"
          placeholder="search memories…"
          .value=${this._query}
          @input=${(e) => this._onQueryInput(e.target.value)}
        />
        <select aria-label="status" @change=${(e) => this._onStatusChange(e.target.value)}>
          ${STATUSES.map(
            (s) => html`<option value=${s} ?selected=${s === this._status}>${STATUS_LABELS[s]}</option>`
          )}
        </select>
        <select aria-label="tag" @change=${(e) => this._onTagChange(e.target.value)}>
          <option value="" ?selected=${!this._tag}>ALL TAGS</option>
          ${this.tags.map((t) => html`<option value=${t.id} ?selected=${t.id === this._tag}>${t.name}</option>`)}
        </select>
        <select aria-label="band" @change=${(e) => this._onBandChange(e.target.value)}>
          ${BANDS.map((b) => html`<option value=${b.value} ?selected=${b.value === this._band}>${b.label}</option>`)}
        </select>
        <select aria-label="sort" @change=${(e) => this._onSortChange(e.target.value)}>
          ${this._sortOptions().map((s) => html`<option value=${s.value} ?selected=${s.value === this._sort}>${s.label}</option>`)}
        </select>
        <button
          title="toggle sort order"
          style="font-size:0.85em"
          @click=${() => this._toggleOrder()}
        >
          ${this._order === "asc" ? "▲ ASC" : "▼ DESC"}
        </button>
        <span style="position:relative">
          <button
            id="searches-menu-button"
            aria-haspopup="true"
            aria-expanded=${this._searchesOpen}
            style="font-size:0.85em"
            @click=${() => this._toggleSearchesMenu()}
          >
            SEARCHES ▾
          </button>
          ${this._searchesMenu()}
        </span>
        <button style="font-size:0.85em" @click=${() => this._openDuplicates()}>DUPLICATES</button>
      </div>
    `;
  }

  _searchesMenu() {
    if (!this._searchesOpen) return "";
    return html`
      <div
        class="panel"
        style="position:absolute;top:100%;left:0;margin-top:4px;padding:6px;display:flex;flex-direction:column;gap:2px;z-index:2;width:max-content;min-width:200px"
        @keydown=${(ev) => {
          if (ev.key !== "Escape") return;
          ev.preventDefault();
          ev.stopPropagation();
          this._searchesOpen = false;
          this.querySelector("#searches-menu-button")?.focus();
        }}
      >
        ${this._searches.length
          ? this._searches.map(
              (s) => html`
                <div style="display:flex;align-items:center;gap:6px">
                  <button style="flex:1;text-align:left;font-size:0.85em" @click=${() => this._applySearch(s)}>${s.name}</button>
                  <button
                    title="delete"
                    aria-label="delete saved search ${s.name}"
                    style="font-size:0.85em;color:var(--danger)"
                    @click=${() => this._deleteSearch(s)}
                  >
                    &times;
                  </button>
                </div>
              `
            )
          : html`<span class="dim" style="font-size:0.85em;padding:4px">no saved searches</span>`}
        <button style="font-size:0.85em;margin-top:4px" @click=${() => this._saveCurrentSearch()}>SAVE CURRENT…</button>
      </div>
    `;
  }

  _duplicatesPanel() {
    return html`
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <button style="font-size:0.85em" @click=${() => this._closeDuplicates()}>‹ BACK</button>
        <label class="dim" style="font-size:0.85em" for="dup-tolerance">tolerance (Hz)</label>
        <input
          id="dup-tolerance"
          type="number"
          min="0"
          style="width:90px"
          .value=${String(this._duplicateTolerance)}
          @change=${(e) => this._onToleranceChange(e.target.value)}
        />
      </div>
      ${this._duplicatesLoading
        ? html`<p class="empty">loading…</p>`
        : !this._duplicateGroups.length
          ? html`<p class="empty">no duplicates found</p>`
          : this._duplicateGroups.map((g) => this._duplicateGroup(g))}
    `;
  }

  _duplicateGroup(group) {
    const key = this._groupKey(group);
    const keepId = this._duplicateKeep.get(key);
    return html`
      <div class="dup-group panel" style="margin-bottom:8px;padding:8px">
        ${group.map(
          (s) => html`
            <div style="display:flex;align-items:center;gap:8px;padding:2px 0">
              <input
                type="radio"
                name="keep-${key}"
                aria-label="keep ${s.name}"
                .checked=${s.id === keepId}
                @change=${() => this._setKeep(key, s.id)}
              />
              <span class="mono">${mhz(s.frequency_hz)}</span>
              <span>${s.name}</span>
              <span class="dim">${s.demodulator ?? "--"}</span>
              <span class="dim mono">heard ${s.heard_count ?? 0}</span>
              <span class="dim mono">${relativeOrDate(s.created_at)}</span>
            </div>
          `
        )}
        <button style="font-size:0.85em;margin-top:6px" @click=${() => this._merge(group)}>MERGE</button>
      </div>
    `;
  }

  _bulkBar() {
    const n = this._selected.size;
    // Shown while an undo exists even with nothing selected: a bulk op clears
    // the selection, and UNDO must stay reachable right after it.
    return html`
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;padding:8px;border:1px solid var(--line);margin-bottom:8px">
        ${n > 0 ? this._selectionControls(n) : ""}
        <span class="spacer"></span>
        ${this._undo
          ? html`<button style="font-size:0.85em" @click=${() => this._onUndoClick()}>UNDO "${this._undo.label}"</button>`
          : ""}
        ${n > 0
          ? html`<button style="font-size:0.85em;color:var(--danger)" @click=${() => this._bulkDelete()}>DELETE</button>`
          : ""}
      </div>
    `;
  }

  _selectionControls(n) {
    return html`
        <span class="mono">${n} selected</span>
        <select aria-label="bulk tag" @change=${(e) => (this._bulkTagId = e.target.value)}>
          <option value="" ?selected=${!this._bulkTagId}>tag…</option>
          ${this.tags.map((t) => html`<option value=${t.id} ?selected=${t.id === this._bulkTagId}>${t.name}</option>`)}
        </select>
        <button style="font-size:0.85em" ?disabled=${!this._bulkTagId} @click=${() => this._bulkTagAdd()}>+ tag</button>
        <button style="font-size:0.85em" ?disabled=${!this._bulkTagId} @click=${() => this._bulkTagRemove()}>- tag</button>
        <select aria-label="bulk status" @change=${(e) => this._bulkSetStatus(e.target.value)}>
          <option value="" ?selected=${!this._bulkStatus}>status…</option>
          ${STATUSES.filter((s) => s).map((s) => html`<option value=${s}>${s}</option>`)}
        </select>
        <select aria-label="bulk list" @change=${(e) => (this._bulkListId = e.target.value)}>
          <option value="" ?selected=${!this._bulkListId}>list…</option>
          ${this._lists.map(
            (l) => html`<option value=${l.id} ?selected=${l.id === this._bulkListId}>${l.builtin ? "★ " + l.name : l.name}</option>`
          )}
        </select>
        <button style="font-size:0.85em" ?disabled=${!this._bulkListId} @click=${() => this._bulkListAdd()}>+ list</button>
        <button style="font-size:0.85em" ?disabled=${!this._bulkListId} @click=${() => this._bulkListRemove()}>- list</button>
        <span class="dim" style="font-size:0.8em">color</span>
        <div class="swatch-row">
          ${SIGNAL_COLORS.map(
            (c) => html`<button
              class="swatch"
              style="--sw:var(--led-${c})"
              title=${c}
              aria-label="color ${c}"
              @click=${() => this._bulkSetColor(c)}
            ></button>`
          )}
          <button class="swatch none" title="none" aria-label="clear color" @click=${() => this._bulkSetColor(null)}>&times;</button>
        </div>
    `;
  }

  _row(s, index) {
    const fav = this._isFavorite(s);
    const orderMode = this._isListOrderMode() && !this._filtersActive();
    const atTop = this._page === 0 && index === 0;
    const atBottom = (this._page + 1) * PAGE_SIZE >= this._total && index === this._items.length - 1;
    return html`
      <tr @click=${() => this._select(s)} @keydown=${(e) => this._onRowKeydown(e, s, orderMode)}>
        <td @click=${(ev) => ev.stopPropagation()}>
          <input
            type="checkbox"
            aria-label="select ${s.name}"
            .checked=${this._selected.has(s.id)}
            @change=${(e) => this._toggleRow(s.id, e.target.checked)}
          />
        </td>
        <td><led-dot .color=${s.color ?? null}></led-dot></td>
        <td>${s.name}</td>
        <td class="mono">${mhz(s.frequency_hz)}</td>
        <td class="dim">${s.demodulator ?? "--"}${s.filter_bandwidth_hz ? ` ${bw(s.filter_bandwidth_hz)}` : ""}${s.antenna ? ` ANT ${antennaDisplay(s.antenna)}` : ""}</td>
        <td>${s.status}</td>
        <td><tag-chips .tagIds=${s.tag_ids ?? []} .tagsById=${this.tagsById}></tag-chips></td>
        <td class="mono">${s.heard_count ?? 0}</td>
        <td class="dim mono">${relativeOrDate(s.last_heard_at)}</td>
        <td @click=${(ev) => ev.stopPropagation()} style="white-space:nowrap">
          <button
            title=${fav ? "Remove from Favorites" : "Add to Favorites"}
            aria-label=${fav ? `remove ${s.name} from favorites` : `add ${s.name} to favorites`}
            aria-pressed=${fav}
            style="font-size:0.85em;padding:2px 6px;color:${fav ? "var(--accent)" : "var(--ink)"}"
            @click=${() => this._toggleFavorite(s)}
          >
            ${fav ? "★" : "☆"}
          </button>
          ${orderMode
            ? html`
                <button
                  title="move up"
                  aria-label="move ${s.name} up"
                  style="font-size:0.8em;padding:2px 6px"
                  ?disabled=${atTop}
                  @click=${() => this._moveRow(s.id, -1)}
                >
                  ▲
                </button>
                <button
                  title="move down"
                  aria-label="move ${s.name} down"
                  style="font-size:0.8em;padding:2px 6px"
                  ?disabled=${atBottom}
                  @click=${() => this._moveRow(s.id, 1)}
                >
                  ▼
                </button>
              `
            : ""}
          <button
            class="tune"
            title="Tune"
            aria-label="tune ${s.name}"
            style="font-size:0.8em;padding:2px 8px"
            @click=${(ev) => this._tune(ev, s)}
          >
            ▸
          </button>
          <button
            title="Signal detail"
            aria-label="signal detail for ${s.name}"
            style="font-size:0.8em;padding:2px 8px"
            @click=${(ev) => this._openSignal(ev, s)}
          >
            ⓘ
          </button>
        </td>
      </tr>
    `;
  }
}

customElements.define("library-view", LibraryView);
