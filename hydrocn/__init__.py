# -*- coding: utf-8 -*-
"""HydroCN QGIS plugin entry point. Licensed GPL-2.0-or-later."""


def classFactory(iface):
    from .hydrocn_plugin import HydroCNPlugin
    return HydroCNPlugin(iface)
