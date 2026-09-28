---
paths:
  - "src/window.py"
  - "src/window.blp"
  - "src/sidebar.py"
  - "src/sections.py"
  - "tests/test_sidebar.py"
  - "tests/test_sections.py"
---

# AdwSidebar (libadwaita 1.9): behaviour the window depends on

The Playlists section is bound to `sidebar.py`'s entries over `library.playlist_tree()`; the
window keeps its selection, folder expansion, context menu and drop target.

- Items are GObjects, not widgets: no children, no indentation (a folder's contents follow it;
  its arrow suffix says whether they show). `selected` is an index across all sections.
- A click selects (`notify::selected-item`) and then emits `activated`; a click on the selected
  item only emits `activated`; `set_selected()` only notifies. So selection shows pages and
  `activated` opens and closes folders.
- Splicing out the selected bound item sets `selected` to INVALID, and `bind_model` makes new
  items for whatever is spliced in: reselect by key afterwards.
- With nothing selected, its list selects the row with the focus when the window is shown or
  becomes active. Never leave it empty on purpose; the window routes its own selections through
  `_set_selected`, which shows nothing.
- It scrolls to its selection only when its list maps; a later `set_selected()` does not
  scroll. It has no scroll-to of its own, but its rows sit in ScrolledWindow > Viewport >
  ListBox, and `Gtk.Viewport.scroll_to(row, None)` on that viewport, after the map, works.
- Context menus: it emits `setup-menu(item)`, then shows one `Gtk.PopoverMenu` built from the
  section's `menu-model`. An empty model still shows an empty popover (the window pops it down
  from an idle). `setup-menu(None)` can arrive after the next item's `setup-menu`: never clear
  the menu on None.
- Drops: `setup_drop_target(Gdk.DragAction.COPY, [TrackRef])` takes Python GObject classes;
  `drop-enter` returns the action (0 refuses). An item's `drag-motion-activate` defaults to TRUE,
  which switches pages under a drag: every item here sets it FALSE.
- In sidebar mode it names none of its rows for assistive technology and says nothing of a
  folder's state; the window sets both on the rows itself.
