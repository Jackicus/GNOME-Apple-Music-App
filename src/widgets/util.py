"""connect_weak(): a signal connection that does not keep the handler's widget alive (and
weak_method(), the same for any other callback a child holds); MappedHandlers: a widget's
handlers on objects that outlive it (the library, the engine), connected only while it is
mapped.

A widget that connects one of its children's signals (or an owned object's: a list factory,
an adjustment, a sort model, an event controller) to one of its own bound methods makes a
reference cycle that runs through C: the widget holds the child, the child holds the
signal's closure, the closure holds the bound method, and the bound method holds the
widget's Python wrapper, which holds the widget. Python's collector cannot see the C part,
so the widget is never freed, nor anything it holds (a page's rows and tiles). Blueprint's
`=> $handler()` callbacks make the same cycle (PyGObject connects them to the template's
bound methods).

connect_weak() breaks it: the closure holds the method's function and a GObject weak
reference to the widget (GObject.Object.weak_ref()), so the widget is freed when nothing
else holds it, and the handler disconnects itself the first time it is emitted after that (a
long-lived emitter, such as a library Item, never piles up dead handlers).

Not a Python weakref: with PyGObject 3.56 a GObject's Python wrapper lives only while Python
holds it. The GObject keeps the wrapper's attributes, and a new wrapper is made the next time
Python gets the object (from get_visible_page(), a signal's arguments...). A weakref.ref or
weakref.WeakMethod of a widget therefore dies as soon as Python lets go of it, however long
the widget lives, and says nothing about whether it was freed; a GObject weak reference
follows the object itself.

A widget that can be dropped (a pushed page, a Shelf, a dialog) connects every signal of an
object it holds this way. Handlers on the widget itself (`self.connect('map', ...)`) and
GObject vfuncs (`do_map`) need nothing: PyGObject sees those.
"""

import weakref

from gi.repository import GObject


def _weak_target(method):
    """(a callable answering the method's object or None, the method's function)."""
    target, function = method.__self__, method.__func__
    if isinstance(target, GObject.Object):
        return target.weak_ref(), function
    return weakref.ref(target), function


def connect_weak(obj, signal, method, *extra):
    """obj.connect(signal, method, *extra), holding the object of `method` (a bound method)
    weakly.

    The handler returns the method's result (a signal that wants one, such as
    GtkOverlay::get-child-position, gets it) and, once the method's object has been freed,
    disconnects itself and returns None. Returns the handler id, as connect() does. `extra`
    is held strongly: never pass the method's object there.
    """
    target, function = _weak_target(method)
    handler_id = None

    def handler(emitter, *args):
        instance = target()
        if instance is None:
            if handler_id is not None and emitter.handler_is_connected(handler_id):
                emitter.disconnect(handler_id)
            return None
        return function(instance, emitter, *args, *extra)

    handler_id = obj.connect(signal, handler)
    return handler_id


def weak_method(method):
    """A function that calls `method` (a bound method) with its object held weakly, as
    connect_weak() does, for a callback that is not a signal handler but is held by a child
    all the same (the create function of Gtk.FlowBox.bind_model). It returns None once the
    method's object has been freed."""
    target, function = _weak_target(method)

    def call(*args):
        instance = target()
        return None if instance is None else function(instance, *args)

    return call


class MappedHandlers:
    """The handlers a widget holds on objects that outlive it (the library, the engine),
    connected while the widget is mapped: declared once, in __init__,

        self._handlers = MappedHandlers(self)
        self._handlers.add(library, 'notify::state', self._update)

    each is connected when the widget maps (and at once if it is mapped), weakly, as
    connect_weak() does, and disconnected when it unmaps, so a hidden page does no work for a
    library that changes and a dropped one leaves nothing connected. The widget's do_map runs
    before the handlers are connected: it catches up with what changed while it was hidden.
    remove(obj) forgets an object's handlers (an item the page no longer shows).
    """

    def __init__(self, widget):
        self._widget = widget.weak_ref()
        self._entries = []  # [obj, signal, the callback's target (weak), function, handler id]
        # The closures hold this object, which holds the widget weakly: no cycle.
        widget.connect('map', self._on_map)
        widget.connect('unmap', self._on_unmap)

    def add(self, obj, signal, callback):
        """Call callback (a bound method of the widget, or of an object it holds) with the
        signal's arguments whenever obj emits signal while the widget is mapped."""
        target, function = _weak_target(callback)
        entry = [obj, signal, target, function, None]
        self._entries.append(entry)
        widget = self._widget()
        if widget is not None and widget.get_mapped():
            self._connect(entry)

    def remove(self, obj):
        """Disconnect and forget every handler on obj."""
        for entry in [entry for entry in self._entries if entry[0] is obj]:
            self._disconnect(entry)
            self._entries.remove(entry)

    def _connect(self, entry):
        if entry[4] is not None:
            return
        obj, signal, target, function, _handler = entry

        def handler(emitter, *args):
            instance = target()
            return None if instance is None else function(instance, emitter, *args)

        entry[4] = obj.connect(signal, handler)

    @staticmethod
    def _disconnect(entry):
        if entry[4] is not None:
            entry[0].disconnect(entry[4])
            entry[4] = None

    def _on_map(self, _widget):
        for entry in self._entries:
            self._connect(entry)

    def _on_unmap(self, _widget):
        for entry in self._entries:
            self._disconnect(entry)
