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
        # Keep strong Python references: on QGIS 3.22-3.24 the registered
        # instances' Python halves can be garbage-collected after a plugin
        # unload/reload cycle, leaving zombie algorithms whose id() returns
        # ":" and which processing.run can no longer find.
        self._algorithms = [
            CalculateCurveNumberAlgorithm(),
            ValidateServicesAlgorithm(),
        ]
        for alg in self._algorithms:
            self.addAlgorithm(alg)

    def id(self):
        return "hydrocn"

    def name(self):
        return "HydroCN"

    def longName(self):
        return "HydroCN — SCS Curve Number tools"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
