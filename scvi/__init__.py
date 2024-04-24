"""scvi-tools."""

# Set default logging handler to avoid logging with logging.lastResort logger.
import logging
import warnings
from importlib.metadata import version

# this import needs to come after prior imports to prevent circular import
from . import data, external, model, utils
from ._constants import REGISTRY_KEYS
from ._settings import settings

package_name = "scvi-tools"
__version__ = version(package_name)

settings.verbosity = logging.INFO

# Jax sets the root logger, this prevents double output.
scvi_logger = logging.getLogger("scvi")
scvi_logger.propagate = False


__all__ = [
    "settings",
    "REGISTRY_KEYS",
    "data",
    "model",
    "external",
    "utils",
    "criticism",
]
