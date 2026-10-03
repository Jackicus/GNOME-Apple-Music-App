# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""A track row's artist and album as links: a click on the album opens the page Go to Album
would (window.item_actions.go_to, related.py); on the artist, what the library holds of them
when it has them, else Apple Music's page, as Go to Artist (item_actions.show_artist).

    link = TrackLink('artist')        # in a row's cell, made once per recycled row
    link.show(track)                  # bind: the track's artist (or album), a link when it has
                                      # somewhere to go (related.has_artist, has_album)
    track_links.attach(view)          # a click on a link in view opens its page

A TrackLink is a one-line label as wide as its text, so only the text is the link: underlined
under the pointer (style.css), with the pointer cursor. The click is the view's, not each
recycled row's (one gesture, as widgets/context_menu.py's): in the capture phase, claimed when
the press is on a link, so the row is neither selected nor activated nor dragged; the page
opens on the release, over the same link. Not from a touchscreen, where a tap selects the row
and a long press opens its menu as anywhere else in the list. The row's Track is found as its
context menu finds it (its `context_item`). The links take no focus: the keyboard's way to
the same pages is the context menu's Go to Album and Go to Artist.
"""

from gi.repository import Gdk, Gtk, Pango

from .. import related
from . import context_menu


class TrackLink(Gtk.Label):
    """A track's artist (`kind` 'artist') or album ('album') in a row: see the module."""

    __gtype_name__ = 'AppleMusicTrackLink'

    def __init__(self, kind, **kwargs):
        super().__init__(xalign=0, halign=Gtk.Align.START, valign=Gtk.Align.CENTER,
                         ellipsize=Pango.EllipsizeMode.END, single_line_mode=True, **kwargs)
        self.kind = kind
        self.active = False

    def show(self, track):
        """Show track's artist or album, a link when Go to would find somewhere to go."""
        if track is None:
            self.set_text('')
            self._set_active(False)
            return
        if self.kind == 'artist':
            self.set_text(track.artist or '')
            self._set_active(related.has_artist(track))
        else:
            self.set_text(track.album or '')
            self._set_active(bool(track.album) and related.has_album(track))

    def _set_active(self, active):
        if active == self.active:
            return
        self.active = active
        if active:
            self.add_css_class('track-link')
            self.set_cursor_from_name('pointer')
        else:
            self.remove_css_class('track-link')
            self.set_cursor(None)


def link_at(view, x, y):
    """The active TrackLink at (x, y) in view, or None."""
    widget = view.pick(x, y, Gtk.PickFlags.DEFAULT)
    if widget is not None and not isinstance(widget, TrackLink):
        widget = widget.get_ancestor(TrackLink)
    return widget if isinstance(widget, TrackLink) and widget.active else None


def open_link(view, link):
    """Open the page `link` (a TrackLink in view) leads to: its row's Track's artist or
    album, through the window's item actions. False when the row has no Track."""
    found = context_menu.find(view, link)
    actions = getattr(view.get_root(), 'item_actions', None)
    if found is None or actions is None:
        return False
    if link.kind == 'artist':
        actions.show_artist(found[1])
    else:
        actions.go_to(found[1], link.kind)
    return True


def attach(view):
    """Make the TrackLinks in view's rows open their pages when clicked. Once per view."""
    if getattr(view, '_track_links_attached', False):
        return
    view._track_links_attached = True
    pressed = []  # the link pressed, while the press lasts

    def on_pressed(gesture, n_press, x, y):
        device = gesture.get_current_event_device()
        if device is not None and device.get_source() == Gdk.InputSource.TOUCHSCREEN:
            pressed.clear()
            return
        link = link_at(gesture.get_widget(), x, y)
        pressed[:] = [link] if link is not None and n_press == 1 else []
        if link is not None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def on_released(gesture, _n_press, x, y):
        view = gesture.get_widget()
        link = pressed.pop() if pressed else None
        if link is not None and link_at(view, x, y) is link:
            open_link(view, link)

    def on_cancelled(_gesture, _sequence):
        pressed.clear()

    click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY,
                             propagation_phase=Gtk.PropagationPhase.CAPTURE)
    click.connect('pressed', on_pressed)
    click.connect('released', on_released)
    click.connect('cancel', on_cancelled)
    view.add_controller(click)
