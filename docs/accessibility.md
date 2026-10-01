# Accessibility

The keyboard walkthrough every release should pass, and how to check it. The rules for new
widgets (names, roles, tooltips, focus) are in `.claude/rules/ui.md`; the keys themselves are in
`src/shortcuts.py`, which also feeds the Keyboard Shortcuts dialog (Ctrl+?).

## Keyboard walkthrough

`scripts/headless.sh scripts/a11y_check.py` walks these steps on the demo library (add
`--size 360x640` for the narrow layout, `--names` for the accessible names), step 8 with the
window maximized (a dialog is inside the window only when that is maximized or tiled; else it
is a window of its own, which the window's keys never reach). The keys are real: the headless
mutter injects them through its RemoteDesktop interface (`scripts/remote_keys.py`), so the
window is active and what needs that is walked too — Tab from row to row in Preferences and,
in the narrow layout, Down from one sidebar section to the next. Of the mnemonics in
Preferences it presses Clear's and checks that every labelled button has one, since the demo
leaves the other buttons insensitive or hidden. When a key or a page changes, change this
list and the script together.

1. Ctrl+1 puts the focus on the selected sidebar row. Down and Up move through every
   destination: in the wide layout they select, which shows the page; in the narrow one they
   only move (each section is a boxed list of its own there, and libadwaita passes the focus
   on from one to the next). Enter shows the row's page (and, in the wide layout, opens or
   closes a folder); Right opens a focused folder and Left closes it, without changing the
   page, and Left on a playlist inside a folder moves to the folder. A screen reader hears
   whether a folder is expanded and how deep an item is nested.
2. Ctrl+2 puts the focus in the page: the first tile of a grid, the Songs table, the Search
   entry, an album's Play; from the page's header bar (Back, Sort By, a filter) too. Arrows
   move between tiles and rows, Tab leaves a grid, shelf or list after one item, Enter opens a
   tile or plays a row, Menu or Shift+F10 opens the focused item's context menu and Escape
   closes it.
3. On an album or playlist: Play, Tab to Shuffle and to More Options (the item's menu), Tab to
   the first track (Shift+Tab goes back to More Options), Down, and Enter plays from that
   track. Alt+Left (or Escape inside the page) goes back. On an artist: Play, More Options,
   the latest release, then (past the Top Songs arrows and See All) the top songs, where the
   arrows move from song to song and Enter plays the top songs from the one focused.
4. Ctrl+3 puts the focus on the player bar's play button (with nothing playing, on the bar,
   which Enter opens as Now Playing). Space presses the focused button, switch, check box or
   boxed-list row, as anywhere in GNOME; on a tile, a list item, the seek slider or nothing, it
   plays or pauses, but never in an entry, in a menu or in a dialog. Ctrl+Right and Ctrl+Left
   skip.
5. Ctrl+Shift+N opens Now Playing with the focus on its play button; Tab goes through the controls,
   the Lyrics and Up Next toggle (Left and Right move between the two, Space or Enter
   switches) and into the list (arrows move, Enter seeks to a line or plays an entry), then
   the close button. Escape closes the sheet and the focus goes back to where it was.
6. Ctrl+F shows Search with the cursor in the entry, over any page opened from its results;
   Tab reaches the Apple Music / Your Library toggle; Escape in the entry clears it. On the
   Songs page, Ctrl+F puts the cursor in the page's own filter instead.
7. F10 opens the main menu, from a page in the narrow layout too (the sidebar comes back with
   it). Ctrl+, opens Preferences: Tab through the rows, Alt and the underlined letter for its
   buttons, Escape closes it. Ctrl+? lists every shortcut.
8. With a dialog open over the window, Alt+Left, Ctrl+F, Ctrl+1 to 3, Ctrl+Shift+N and Space
   leave the page behind it alone.

## The tools

- `scripts/a11y_check.py` sends real key presses. `scripts/remote_keys.py` starts a session on
  the headless mutter's `org.gnome.Mutter.RemoteDesktop` and `NotifyKeyboardKeycode` injects
  each one as a keyboard would; only keyboard events are asked for, so no screen-cast stream is
  needed. That call takes evdev keycodes, which the module works out from the accelerator
  through the keymap the compositor gave the client (`Gtk.accelerator_parse`, then
  `Gdk.Display.map_keyval`, a Wayland or XKB keycode being the evdev one plus 8); a keyval on a
  shifted level of its key brings Shift with it. Typing is real too. The compositor points the
  keyboard at a window only once it has an event to deliver, so the script presses Shift on its
  own first: without that the window is never active and the first key is lost.
- `--emulate-keys`, and the fallback where that interface is not on the session bus (on a
  desktop session it is behind the remote-desktop portal, which asks the user first),
  dispatches each press through GTK's own handlers instead. `press()` runs the handlers a real
  event would reach: the capture-phase key and shortcut controllers from the window down to the
  focus (a mnemonic, Alt and a label's underlined letter, runs there on its label, through the
  shortcut manager above it), then the shortcut controllers from the focus up, and a popover's
  own key handler. Typing is not emulated (the entry's text is set), the window is never
  active, and the steps that need an active window are left out.
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

- libadwaita's own, worked around here: `AdwSidebar` names none of its rows in sidebar mode
  and does not expose a folder's state or level (`sidebar_view.py` sets them);
  `AdwShortcutsDialog`'s rows are unnamed (dialogs/shortcuts.py names them); `AdwComboRow`'s
  default factories leave its list items unnamed, in the popup and in the row's own display
  of the value (dialogs/preferences.py gives the row factories that name them); an open
  `Adw.BottomSheet` leaves the content behind it "showing".
- Not checked by machine: input methods, Orca itself, the high-contrast switch live, and
  right-to-left layouts. Nor is CI: its container has no mutter, so the keyboard walkthrough is
  a tool to run here, not a job there.
