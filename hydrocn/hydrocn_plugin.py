# -*- coding: utf-8 -*-
"""HydroCN plugin class — registers the Processing provider.

Licensed GPL-2.0-or-later.
"""

from qgis.core import QgsApplication

from .hydrocn_provider import HydroCNProvider


class HydroCNPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initProcessing(self):
        self.provider = HydroCNProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
