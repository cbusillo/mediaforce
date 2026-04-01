"""Mediaforce package."""

import sys
from types import ModuleType

from .core import binaries, config, db, models, process_control, type_defs, utils
from .encoding import encode_queue, ffmpeg, quality
from .library import folder_profiles, planner, probe, run_manifests, scanner
from .tuning import calibration_jobs, tuning_memory


def _register_legacy_submodule(module: ModuleType) -> None:
    module_name = getattr(module, "__name__", "")
    short_name = module_name.rsplit(".", 1)[-1]
    sys.modules[f"{__name__}.{short_name}"] = module


for _module in (
    binaries,
    calibration_jobs,
    config,
    db,
    encode_queue,
    ffmpeg,
    folder_profiles,
    models,
    planner,
    probe,
    process_control,
    quality,
    run_manifests,
    scanner,
    tuning_memory,
    type_defs,
    utils,
):
    _register_legacy_submodule(_module)

