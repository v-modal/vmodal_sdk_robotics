"""Durable, lightweight LeRobot artifact uploader."""

from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

from .contracts import Artifact, ArtifactInput, DatasetRevision, Receipt, RevisionInput, TransportCapabilities, TransportUnknown
from .runner import Runner, RunnerConfig
from .spool import Spool, SpoolConfig
from .markers import Marker, mark, marker_load, marker_rotate, marker_attach

__version__ = "0.2.0"

__all__ = [
    "Marker",
    "mark",
    "marker_load",
    "marker_rotate",
    "marker_attach",
    "Artifact",
    "ArtifactInput",
    "DatasetRevision",
    "Receipt",
    "RevisionInput",
    "Runner",
    "RunnerConfig",
    "Spool",
    "SpoolConfig",
    "TransportCapabilities",
    "TransportUnknown",
    "__version__",
]
