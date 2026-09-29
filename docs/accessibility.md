# Accessibility

The keyboard walkthrough every release should pass, and how to check it. The rules for new
widgets (names, roles, tooltips, focus) are in `.claude/rules/ui.md`; the keys themselves are in
`src/shortcuts.py`, which also feeds the Keyboard Shortcuts dialog (Ctrl+?).

## Keyboard walkthrough

`scripts/a11y_check.py` walks these steps on the demo library (add `--size 360x640` for the
narrow layout, `--names` for the accessible names). With a real keyboard, do the same by hand.
When a key or a page changes, change this list and the script together.

1. Ctrl+1 puts the focus on the selected sidebar row. Down and Up move: in the wide layout
   they select, which shows the page; in the narrow one they only move. Enter shows the row's
   page (and opens or closes a folder); a screen reader hears whether a folder is expanded.
2. Ctrl+2 puts the focus in the page: the first tile of a grid, the Songs table, the Search
   entry, an album's Play. Arrows move between tiles and rows, Tab leaves a grid, shelf or list
   after one item, Enter opens a tile or plays a row, Menu or Shift+F10 opens the focused item's
   context menu and Escape closes it.
3. On an album or playlist: Play, Tab to Shuffle and to More Options (the item's menu), Tab to
   the first track (Shift+Tab goes back to More Options), Down, and Enter plays from that
   track. Alt+Left (or Escape inside the page) goes back.
4. Ctrl+3 puts the focus on the player bar's play button (with nothing playing, on the bar,
   which Enter opens as Now Playing). Space presses the focused button, switch, check box or
   boxed-list row, as anywhere in GNOME; on a tile, a list item, the seek slider or nothing, it
   plays or pauses, but never in an entry, in a menu or in a dialog. Ctrl+Right and Ctrl+Left
   skip.
5. Ctrl+Shift+N opens Now Playing with the focus on its play button; Tab goes through the controls,
   the Lyrics and Up Next toggle (Left and Right switch) and into the list (arrows move, Enter
   seeks to a line or plays an entry), then the close button. Escape closes the sheet and the
   focus goes back to where it was.
6. Ctrl+F shows Search with the cursor in the entry; Tab reaches the Apple Music / Your Library
   toggle; Escape in the entry clears it.
7. F10 opens the main menu, from a page in the narrow layout too (the sidebar comes back with
   it). Ctrl+, opens Preferences: Tab through the rows, Alt and the underlined letter for its
   buttons, Escape closes it. Ctrl+? lists every shortcut.
8. With a dialog open over the window, Alt+Left, Ctrl+F, Ctrl+1 to 3, Ctrl+Shift+N and Space
   leave the page behind it alone.

## The tools

- `scripts/a11y_check.py` cannot send real key presses (none can be synthesised on the
  development desktop). Its `press()` runs the handlers a real event would reach: the
  capture-phase key and shortcut controllers from the window down to the focus, then the
  shortcut controllers from the focus up, and a popover's own key handler. Typing is not
  emulated; the entry's text is set.
- `--names` starts a private accessibility bus (a `dbus-daemon` with at-spi's configuration,
  from /usr/share/defaults/at-spi2 or /etc/at-spi2, on an abstract socket, and
  `at-spi2-registryd` from /usr/libexec, /usr/lib or /usr/lib/at-spi2-core), stops both when it
  exits, and lists each page's focusable controls that have no accessible name, as libatspi
  (and so Orca) reads them. GTK and libatspi both follow `AT_SPI_BUS_ADDRESS`.
- An `Atspi.EventListener` sees events only when registered before the app starts, and after
  `Atspi.init()`. `Gtk.Accessible.announce()` from a widget that no client has asked about is
  dropped; from the window it is always sent (`object:announcement`), which is why the app
  announces through the window.

## Known gaps

- libadwaita's own: `AdwSidebar` names none of its rows in sidebar mode and does not expose a
  folder's state (the window sets both itself); `AdwShortcutsDialog`'s rows are unnamed
  (dialogs/shortcuts.py names them); `AdwComboRow` exposes an unnamed inner list item; an open `Adw.BottomSheet`
  leaves the content behind it "showing".
- Not checked by machine: real key presses and input methods, Orca itself, the high-contrast
  switch live, and right-to-left layouts.
