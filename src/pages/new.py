"""The New destination: Apple Music's editorial New page (engine.browse(): the featured
banners, Best New Songs, New Releases, playlists, stations, videos) as shelves."""

from gettext import gettext as _

from . import app
from .shelves import ShelvesPage


def create(destination):
    return ShelvesPage(destination.title, lambda refresh: app().engine.browse(refresh=refresh),
                       icon_name=destination.icon_name, hero=True,
                       empty_title=_('Nothing New'),
                       empty_description=_('Apple Music has nothing new to show right now'))
