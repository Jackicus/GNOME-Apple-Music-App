# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""widgets.util.MappedHandlers: a widget's handlers on a longer-lived object are connected only
while the widget is mapped, and hold the widget weakly."""

import gc
import unittest

from tests.gtk import pump, requires_gtk, wait_for

from gi.repository import GObject


class _Source(GObject.Object):
    """An object that outlives the widget, as the library does."""

    __gsignals__ = {'changed': (GObject.SignalFlags.RUN_FIRST, None, (int,))}


@requires_gtk
class MappedHandlersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gi.repository import Gtk

        cls.window = Gtk.Window(default_width=200, default_height=200)
        cls.box = Gtk.Box()
        cls.window.set_child(cls.box)
        cls.window.present()
        wait_for(cls.window.get_mapped, timeout=2)

    @classmethod
    def tearDownClass(cls):
        cls.window.destroy()
        pump()
        del cls.window, cls.box

    def make(self):
        from gi.repository import Gtk

        from applemusic.widgets.util import MappedHandlers

        class Probe(Gtk.Box):
            def __init__(self, source):
                super().__init__()
                self.calls = []
                self.handlers = MappedHandlers(self)
                self.handlers.add(source, 'changed', self.on_changed)

            def on_changed(self, source, value):
                self.calls.append(value)

        return Probe

    def test_connected_only_while_mapped(self):
        source = _Source()
        widget = self.make()(source)
        source.emit('changed', 1)  # not mapped yet
        self.box.append(widget)
        self.assertTrue(wait_for(widget.get_mapped))
        source.emit('changed', 2)
        widget.set_visible(False)  # unmapped
        pump()
        source.emit('changed', 3)
        widget.set_visible(True)
        self.assertTrue(wait_for(widget.get_mapped))
        source.emit('changed', 4)
        self.assertEqual(widget.calls, [2, 4])
        self.box.remove(widget)

    def test_added_while_mapped_connects_at_once_and_remove_disconnects(self):
        source, other = _Source(), _Source()
        widget = self.make()(source)
        self.box.append(widget)
        self.assertTrue(wait_for(widget.get_mapped))
        widget.handlers.add(other, 'changed', widget.on_changed)
        other.emit('changed', 5)
        widget.handlers.remove(other)
        other.emit('changed', 6)
        source.emit('changed', 7)
        self.assertEqual(widget.calls, [5, 7])
        self.box.remove(widget)

    def test_the_widget_is_freed(self):
        source = _Source()
        widget = self.make()(source)
        self.box.append(widget)
        self.assertTrue(wait_for(widget.get_mapped))
        finalized = []
        widget.weak_ref(lambda: finalized.append(True))
        self.box.remove(widget)
        del widget
        pump()
        gc.collect()
        self.assertEqual(finalized, [True])
        source.emit('changed', 8)  # nothing left connected to call


if __name__ == '__main__':
    unittest.main()
