# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Context menus and track drags for the items of a view: a Gtk.GridView, Gtk.ListView or
Gtk.ColumnView of tiles or rows, a Gtk.FlowBox or Gtk.ListBox, or a single widget.

    context_menu.attach(view)              # menus on right-click, long-press, Menu, Shift+F10
    context_menu.attach(view, drag=True)   # and its tracks dragged as a TrackRef
    context_menu.popup(widget, obj, x, y)  # the menu for obj, at (x, y) in widget

A widget offering a menu has a `context_item` attribute: the Item or Track it shows, or None
(an unbound row), and `context_queued` true when that is an entry of the player's queue (the
player bar's item, Up Next's rows: a menu without Play, actions.build_menu). The controllers
sit on the view, not on each recycled tile: the right click and the long press in the capture
phase, so they come before the rows' own click gestures (which would select or activate), the
shortcuts for the focused row. What was pressed is found by picking the widget under the
pointer and going up to the one with a context_item, or, from a row or tile of the view (a
list item, a column view's row, a FlowBoxChild), down into it: a click on a Songs row's album
column finds its title cell.

The menu is the window's (window.item_actions.menu_for(obj): see actions.py), shown in a
Gtk.PopoverMenu parented to that widget, without an arrow, pointing at the pointer, or beside
the widget from the keyboard; it is unparented once closed (after an idle: its action is
activated after it closes, and looks the window's actions up through its parent). The row or
tile whose menu is open is marked `.has-open-popup` meanwhile, as GTK's own menus mark
theirs, so it stays highlighted while the pointer is in the menu.
"""

from gi.repository import Gdk, GLib, Graphene, Gtk

from .. import shortcuts
from ..library import TrackRef

# The containers whose children are rows or tiles.
LIST_TYPES = (Gtk.ListBase, Gtk.FlowBox, Gtk.ListBox)

# The keys that open the focused item's menu, as a Gtk.ShortcutTrigger string: the
# shortcuts table's alternatives, which the Keyboard Shortcuts dialog lists.
MENU_KEYS = '|'.join(shortcuts.CONTEXT_MENU.split())


def context_object(widget):
    return getattr(widget, 'context_item', None)


def _descendant(widget):
    """The first visible widget under `widget` (itself included) with a context_item, and
    that item: (widget, obj) or None."""
    obj = context_object(widget)
    if obj is not None:
        return widget, obj
    child = widget.get_first_child()
    while child is not None:
        if child.get_visible() and not isinstance(child, Gtk.Popover):
            found = _descendant(child)
            if found is not None:
                return found
        child = child.get_next_sibling()
    return None


def find(view, widget):
    """(widget, obj) for what `widget` (picked in `view`, or focused there) belongs to: it or
    its nearest ancestor with a context_item, or what the row or tile holding it holds. None
    when it is none of the view's items (the view's background, a page's hero)."""
    while widget is not None and widget is not view:
        obj = context_object(widget)
        if obj is not None:
            return widget, obj
        parent = widget.get_parent()
        if isinstance(parent, LIST_TYPES):  # a row or tile of a list: what it holds
            return _descendant(widget)
        widget = parent
    if widget is view:
        obj = context_object(view)
        if obj is not None:
            return view, obj
    return None


def owner(widget):
    """The row or tile of a list that holds widget (widget itself when it is one): the
    child of a LIST_TYPES container; None when widget is in no list."""
    while widget is not None:
        if isinstance(widget.get_parent(), LIST_TYPES):
            return widget
        widget = widget.get_parent()
    return None


def popup(widget, obj, x=None, y=None, queued=None):
    """Show the menu for obj in a popover parented to widget, pointing at (x, y) in its
    coordinates, or at the whole widget; `queued` as widget's `context_queued` says unless
    given. The popover, or None when obj has no menu."""
    actions = getattr(widget.get_root(), 'item_actions', None)
    if queued is None:
        queued = bool(getattr(widget, 'context_queued', False))
    menu = actions.menu_for(obj, queued=queued) if actions is not None else None
    if menu is None:
        return None
    popover = Gtk.PopoverMenu.new_from_model(menu)
    popover.set_has_arrow(False)
    popover.set_parent(widget)
    if x is not None and y is not None:
        rectangle = Gdk.Rectangle()
        rectangle.x, rectangle.y, rectangle.width, rectangle.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rectangle)
        popover.set_halign(Gtk.Align.START)  # the menu opens from the pointer
    marked = owner(widget)
    if marked is not None:
        marked.add_css_class('has-open-popup')
    popover.connect('closed', _on_closed, marked)
    popover.popup()
    return popover


def _on_closed(popover, marked):
    if marked is not None:
        marked.remove_css_class('has-open-popup')
    GLib.idle_add(_unparent, popover)


def _unparent(popover):
    if popover.get_parent() is not None:
        popover.unparent()
    return GLib.SOURCE_REMOVE


def _popup_at(view, x, y):
    """The menu for what is at (x, y) in view: True when there was one."""
    found = find(view, view.pick(x, y, Gtk.PickFlags.DEFAULT))
    if found is None:
        return False
    widget, obj = found
    point = Graphene.Point()
    point.x, point.y = x, y
    ok, local = view.compute_point(widget, point)
    if not ok:
        return False
    return popup(widget, obj, local.x, local.y) is not None


def _on_pressed(gesture, _n_press, x, y):
    if _popup_at(gesture.get_widget(), x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)


def _on_long_pressed(gesture, x, y):
    if _popup_at(gesture.get_widget(), x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)


def popup_focused(view):
    """The menu for the row or tile of view with the keyboard focus: True when there was
    one (Menu, Shift+F10)."""
    focus = view.get_root().get_focus() if view.get_root() is not None else None
    if focus is None or not (focus is view or focus.is_ancestor(view)):
        return False
    found = find(view, focus)
    if found is None and (focus is not view or not isinstance(view, LIST_TYPES)):
        found = _descendant(focus)  # the focused row's item, or a single widget's (the bar)
    if found is None:
        return False
    widget, obj = found
    # A button inside the widget with the menu (the player bar's): the menu hangs from it,
    # and the focus goes back to it when the menu closes.
    anchor = focus if focus is not widget and focus.is_ancestor(widget) else widget
    return popup(anchor, obj, queued=bool(getattr(widget, 'context_queued', False))) is not None


def _on_menu_key(view, _arguments):
    return popup_focused(view)


def _on_prepare(source, x, y):
    """A drag from a track row: its TrackRef, with the row as the drag icon. Not from a
    touchscreen, where a drag scrolls the list (a long press opens the menu instead)."""
    device = source.get_current_event_device()
    if device is not None and device.get_source() == Gdk.InputSource.TOUCHSCREEN:
        return None
    view = source.get_widget()
    found = find(view, view.pick(x, y, Gtk.PickFlags.DEFAULT))
    if found is None:
        return None
    widget, obj = found
    ref = TrackRef.for_object(obj)
    if ref is None:
        return None
    point = Graphene.Point()
    point.x, point.y = x, y
    ok, local = view.compute_point(widget, point)
    hot_x, hot_y = (int(local.x), int(local.y)) if ok else (0, 0)
    source.set_icon(Gtk.WidgetPaintable.new(widget), hot_x, hot_y)
    return Gdk.ContentProvider.new_for_value(ref)


def attach(view, drag=False):
    """Give view's items context menus (and, with drag, make its tracks draggable onto the
    sidebar's playlists). Once per view: attaching again does nothing."""
    if getattr(view, '_context_menu_attached', False):
        return
    view._context_menu_attached = True

    click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY,
                             propagation_phase=Gtk.PropagationPhase.CAPTURE)
    click.connect('pressed', _on_pressed)
    view.add_controller(click)

    press = Gtk.GestureLongPress(touch_only=True, propagation_phase=Gtk.PropagationPhase.CAPTURE)
    press.connect('pressed', _on_long_pressed)
    view.add_controller(press)

    keys = Gtk.ShortcutController()
    keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(MENU_KEYS),
                                       Gtk.CallbackAction.new(_on_menu_key)))
    view.add_controller(keys)

    if drag:
        source = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        source.connect('prepare', _on_prepare)
        view.add_controller(source)
