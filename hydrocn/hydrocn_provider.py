# -*- coding: utf-8 -*-
"""HydroCN Processing provider. Licensed GPL-2.0-or-later."""

import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .hydrocn_algorithm import (
    CalculateCurveNumberAlgorithm,
    ValidateServicesAlgorithm,
)


class HydroCNProvider(QgsProcessingProvider):

    def loadAlgorithms(self):
        self.addAlgorithm(CalculateCurveNumberAlgorithm())
        self.addAlgorithm(ValidateServicesAlgorithm())

    def id(self):
        return "hydrocn"

    def name(self):
        return "HydroCN"

    def longName(self):
        return "HydroCN — SCS Curve Number tools"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
