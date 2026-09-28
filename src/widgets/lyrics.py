"""AppleMusicLyricsView: the Now Playing sheet's Lyrics tab, following the Player.

A Gtk.Stack of four pages: synced lyrics as a Gtk.ListView over the Lyrics object's store
of LyricLine (one wrapping label a row; the line current at the Player's position carries
the `current` CSS class, the others `dim-label`; a click seeks to the line's start),
unsynced lyrics as one wrapping label in a scrolled window, a spinner while the engine
reads them, and a compact "No Lyrics" status page otherwise (none, or no engine).

The list follows playback only while the sheet is open (set_active), and only when the
current line changes: scroll_to brings the row on screen, and once the list's geometry has
held still for a frame (a tick callback: the sheet's opening lays the list out over a few
frames) the row glides to the middle of the view. Not while the user scrolled the list in
the last USER_SCROLL_PAUSE seconds. Nothing here runs on a timer: the Player's position
events (four a second) drive it.
"""

import logging
import time
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk, Pango

from .transport import run_command

log = logging.getLogger(__name__)

USER_SCROLL_PAUSE = 4.0    # seconds after the user scrolls before the list follows again
CENTRE_DURATION = 300      # ms, the glide bringing the current line to the middle
SETTLE_FRAMES = 40         # frames to wait for the row's geometry to hold still, at most


class LyricsView(Gtk.Stack):
    """The Lyrics tab. See the module."""

    __gtype_name__ = 'AppleMusicLyricsView'

    def __init__(self):
        super().__init__(vhomogeneous=True, hhomogeneous=True,
                         transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.add_css_class('lyrics-view')
        self._player = None
        self._app = None
        self._active = False
        self._lyrics = None
        self._current = -1              # the index of the line current, or -1
        self._rows = {}                 # bound position -> its label
        self._centre_pending = None     # a line to centre once its row holds still
        self._tick = None               # the tick callback watching for that
        self._tick_geometry = None      # what the last frame measured
        self._tick_frames = 0
        self._page_seen = None          # the page size the last scroll_to or centring saw
        self._user_scrolled_at = 0.0
        self._animation = None

        self.empty_page = Adw.StatusPage(icon_name='music-note-symbolic', title=_('No Lyrics'),
                                         description=_('Apple Music has no lyrics for this'))
        self.empty_page.add_css_class('compact')
        self.add_named(self.empty_page, 'empty')

        spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                              width_request=32, height_request=32)
        self.add_named(spinner, 'loading')

        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self._on_setup)
        factory.connect('bind', self._on_bind)
        factory.connect('unbind', self._on_unbind)
        # Tab leaves the list after the line with the focus; the arrows move between lines,
        # Enter seeks to one.
        self.list_view = Gtk.ListView(factory=factory, single_click_activate=True,
                                      model=Gtk.NoSelection(),
                                      tab_behavior=Gtk.ListTabBehavior.ITEM)
        self.list_view.add_css_class('lyrics-list')
        self.list_view.connect('activate', self._on_activate)
        self.scrolled = Gtk.ScrolledWindow(child=self.list_view,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL,
                                           propagation_phase=Gtk.PropagationPhase.CAPTURE)
        scroll.connect('scroll', self._on_user_scroll)
        self.scrolled.add_controller(scroll)
        # The sheet is laid out at its closed size until a frame or two after it opens (and
        # the window may be resized): a line brought into view in the old page is brought
        # into view again when the page changes.
        self.scrolled.get_vadjustment().connect('changed', self._on_adjustment_changed)
        self.add_named(self.scrolled, 'synced')

        self.text_label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0,
                                    valign=Gtk.Align.START, selectable=True,
                                    margin_start=12, margin_end=12, margin_top=6,
                                    margin_bottom=24)
        self.text_label.add_css_class('lyrics-text')
        self.add_named(Gtk.ScrolledWindow(child=self.text_label,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER), 'text')

    def set_player(self, player, app):
        self._player = player
        self._app = app
        player.connect('notify::lyrics', lambda *_: self._on_lyrics())
        player.connect('notify::lyrics-loading', lambda *_: self._update_page())
        player.connect('notify::position', lambda *_: self._follow())
        self._on_lyrics()

    def set_active(self, active):
        """Follow playback (scroll to the current line) only while shown."""
        self._active = active
        if active:
            self._user_scrolled_at = 0.0
            self._follow(force=True)

    # -- the lyrics --------------------------------------------------------------------

    def _on_lyrics(self):
        lyrics = self._player.lyrics
        if lyrics is not self._lyrics:
            self._lyrics = lyrics
            self._current = -1
            self._stop_centring()
            self.list_view.set_model(
                Gtk.NoSelection(model=lyrics.lines if lyrics is not None and lyrics.synced
                                else None))
            self.text_label.set_label(lyrics.text if lyrics is not None else '')
            self.scrolled.get_vadjustment().set_value(0)
        self._update_page()
        self._follow(force=True)

    def _update_page(self):
        lyrics = self._lyrics
        if lyrics is not None and lyrics.synced:
            self.set_visible_child_name('synced')
        elif lyrics is not None:
            self.set_visible_child_name('text')
        elif self._player is not None and self._player.lyrics_loading:
            self.set_visible_child_name('loading')
        else:
            self.set_visible_child_name('empty')

    # -- the current line --------------------------------------------------------------

    def _follow(self, force=False):
        """Mark the line current at the Player's position and bring it into view when it
        changed (or `force`: the lyrics or the sheet's state changed)."""
        lyrics = self._lyrics
        if lyrics is None or not lyrics.synced or self._player is None:
            return
        index = lyrics.index_at(self._player.position)
        if index == self._current and not force:
            return
        previous, self._current = self._current, index
        self._style(previous)
        self._style(index)
        if self._active:
            self._bring_into_view(index)

    def _style(self, position):
        label = self._rows.get(position)
        if label is not None:
            _set_current(label, position == self._current)

    def _bring_into_view(self, index):
        """scroll_to the line's row (a no-op when it is on screen), then centre it once
        its geometry holds still: the sheet's opening, and the list's own layout, take a
        few frames, during which a row's bounds are what an earlier layout left."""
        if index < 0 or time.monotonic() - self._user_scrolled_at < USER_SCROLL_PAUSE:
            return
        self._page_seen = round(self.scrolled.get_vadjustment().get_page_size())
        self.list_view.scroll_to(index, Gtk.ListScrollFlags.NONE, None)
        self._centre_pending = index
        self._tick_geometry = None
        self._tick_frames = 0
        if self._tick is None:
            self._tick = self.list_view.add_tick_callback(self._on_tick)

    def _on_tick(self, _widget, _clock):
        index = self._centre_pending
        if index is None or index != self._current:
            return self._stop_centring()
        label = self._rows.get(index)
        geometry = self._geometry(label) if label is not None and label.get_mapped() else None
        self._tick_frames += 1
        if geometry is not None and geometry == self._tick_geometry:
            log.debug('lyrics: line %d held still after %d frames: centring', index,
                      self._tick_frames)
            self._centre(label, geometry)
            return self._stop_centring()
        self._tick_geometry = geometry
        if self._tick_frames > SETTLE_FRAMES:
            adjustment = self.scrolled.get_vadjustment()
            log.debug('lyrics: line %d never held still; giving up (rows %s, value %.0f, '
                      'page %.0f, upper %.0f, list %d high)', index, sorted(self._rows),
                      adjustment.get_value(), adjustment.get_page_size(),
                      adjustment.get_upper(), self.list_view.get_height())
            return self._stop_centring()
        return GLib.SOURCE_CONTINUE

    def _on_adjustment_changed(self, adjustment):
        page = round(adjustment.get_page_size())
        if self._active and self._current >= 0 and page > 0 and page != self._page_seen:
            log.debug('lyrics: the page is %d now (was %s): line %d brought into view again',
                      page, self._page_seen, self._current)
            self._page_seen = page
            # Not from inside the list's allocation, where this signal comes from: a
            # scroll_to asked for there is lost.
            GLib.idle_add(self._bring_current_into_view)

    def _bring_current_into_view(self):
        if self._active and self._current >= 0:
            self._bring_into_view(self._current)
        return GLib.SOURCE_REMOVE

    def _stop_centring(self):
        self._centre_pending = None
        if self._tick is not None:
            self.list_view.remove_tick_callback(self._tick)
            self._tick = None
        return GLib.SOURCE_REMOVE

    def _geometry(self, label):
        """(row y, row height, page, upper) for the row holding `label`, in the list's
        coordinates, or None while it has no place yet."""
        row = label.get_parent() or label
        ok, bounds = row.compute_bounds(self.list_view)
        adjustment = self.scrolled.get_vadjustment()
        page = adjustment.get_page_size()
        if not ok or bounds.get_height() <= 0 or page <= 0:
            return None
        return (round(bounds.get_y()), round(bounds.get_height()), round(page),
                round(adjustment.get_upper()))

    def _centre(self, label, geometry):
        """Glide the scrolled window so the row holding `label` sits in the middle."""
        y, height, page, upper = geometry
        self._page_seen = page
        adjustment = self.scrolled.get_vadjustment()
        target = adjustment.get_value() + y + height / 2 - page / 2
        target = max(adjustment.get_lower(), min(target, upper - page))
        log.debug('lyrics: centre row at y %d h %d: value %.0f -> %.0f (page %d, upper %d; '
                  'list %d, scrolled %d, stack %d high)', y, height, adjustment.get_value(),
                  target, page, upper, self.list_view.get_height(), self.scrolled.get_height(),
                  self.get_height())
        self._stop_animation()
        if abs(target - adjustment.get_value()) < 1:
            return
        animation = Adw.TimedAnimation(
            widget=self.scrolled, value_from=adjustment.get_value(), value_to=target,
            duration=CENTRE_DURATION, easing=Adw.Easing.EASE_OUT_CUBIC,
            target=Adw.PropertyAnimationTarget.new(adjustment, 'value'))
        self._animation = animation
        animation.play()

    def _stop_animation(self):
        if self._animation is not None:
            self._animation.skip()
            self._animation = None

    def _on_user_scroll(self, _controller, _dx, _dy):
        self._user_scrolled_at = time.monotonic()
        self._stop_animation()
        self._stop_centring()
        return False

    # -- the list -----------------------------------------------------------------------

    def _on_setup(self, _factory, list_item):
        label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0,
                          justify=Gtk.Justification.LEFT)
        label.add_css_class('lyric-line')
        list_item.set_child(label)

    def _on_bind(self, _factory, list_item):
        position = list_item.get_position()
        label = list_item.get_child()
        text = list_item.get_item().text
        label.set_label(text)
        list_item.set_accessible_label(text)
        self._rows[position] = label
        _set_current(label, position == self._current)

    def _on_unbind(self, _factory, list_item):
        position = list_item.get_position()
        if self._rows.get(position) is list_item.get_child():
            del self._rows[position]

    def _on_activate(self, _list_view, position):
        """A click on a line seeks to where it starts."""
        if self._lyrics is None or self._player is None or self._player.track is None:
            return
        self._user_scrolled_at = 0.0
        run_command(self._app, self._player.seek(self._lyrics.start_of(position)))


def _set_current(label, current):
    if current:
        label.add_css_class('current')
        label.remove_css_class('dim-label')
    else:
        label.remove_css_class('current')
        label.add_css_class('dim-label')
