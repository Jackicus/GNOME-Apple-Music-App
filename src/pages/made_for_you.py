"""The Made for You destination: the personal mixes and stations Apple makes for the account
(engine.made_for_you(): the recommendations made only of those) as shelves, the mixes as
large cards."""

from gettext import gettext as _

from . import app
from .shelves import ShelvesPage


def create(destination):
    return ShelvesPage(destination.title,
                       lambda refresh: app().engine.made_for_you(refresh=refresh),
                       icon_name=destination.icon_name, hero=True,
                       empty_title=_('Nothing Made for You Yet'),
                       empty_description=_('Your mixes and stations appear here once Apple '
                                           'Music has made some'))
