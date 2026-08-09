from __future__ import annotations

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mediaforce.tuning import av1_validation_v4r3_dogfood_preparation as module
from mediaforce.tuning import (
    av1_validation_v4r3_execution_preflight_operation as preflight_module,
)
from mediaforce.tuning import av1_validation_v4r3_freeze_operation as freeze_module
from mediaforce.tuning import (
    av1_validation_v4r3_qualification_request_operation as request_module,
)
from mediaforce.tuning.av1_validation_v4r3_dogfood_preparation import (
    AV1V4R3DogfoodPreparationError,
    prepare_av1_v4r3_dogfood,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_config import (
    deserialize_av1_v4r3_effective_config_snapshot,
)
from scripts import prepare_av1_v4r3_dogfood as script_module
from tests.test_av1_validation_v4r3_execution_preflight_operation import _bindings
from tests.test_av1_validation_v4r3_preparation_custody import _clock, _rights


class AV1V4R3DogfoodPreparationTests(unittest.TestCase):
    def test_resumes_prepared_cohort_and_stops_before_execution(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _bindings(Path(raw))
            config = deserialize_av1_v4r3_effective_config_snapshot(
                (preparation.registry / "effective-config.json").read_bytes()
            )
            kwargs = {
                "repository_root": preparation.repository_root,
                "preparation_registry": preparation.registry,
                "ordinal_registry": ordinal.registry,
                "rights_attestation": _rights(),
                "owner_principal": "owner:test",
                "confirmed_owner_principal": "owner:test",
                "preparation_grant_valid_until": "2026-08-08T23:00:00Z",
                "ordinal_1_valid_until": "2026-08-08T07:50:00Z",
                "source_paths": config["source_paths"],
                "dedicated_instance_paths": config["dedicated_instance_paths"],
                "quality_temp_paths": config["quality_temp_paths"],
                "tool_paths": {
                    "ab_av1": Path("/usr/local/bin/ab-av1"),
                    "ffmpeg": Path("/usr/local/bin/ffmpeg"),
                    "ffprobe": Path("/usr/local/bin/ffprobe"),
                },
                "clock": _clock(4),
            }
            with (
                patch.object(
                    module, "_measure_clean_repository", return_value=repository
                ),
                patch.object(
                    freeze_module, "_measure_clean_repository", return_value=repository
                ),
                patch.object(
                    request_module, "_measure_clean_repository", return_value=repository
                ),
                patch.object(
                    preflight_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
            ):
                result = prepare_av1_v4r3_dogfood(**kwargs)
                resumed = prepare_av1_v4r3_dogfood(**kwargs)
            self.assertEqual(result.ordinal_grant["ordinal"], 1)
            self.assertFalse(resumed.created["ordinal_1_grant"])
            self.assertTrue((ordinal.registry / "plan.json").exists())
            self.assertTrue((ordinal.registry / "preflight.json").exists())
            self.assertTrue((ordinal.registry / "ordinal_01.grant.json").exists())
            self.assertFalse(any(ordinal.registry.glob("*.execution-grant.json")))
            self.assertFalse(any(ordinal.registry.glob("*.claim.json")))
            self.assertFalse((ordinal.registry / "ordinal_01.started.json").exists())

    def test_owner_mismatch_precedes_mutation(self) -> None:
        with self.assertRaises(AV1V4R3DogfoodPreparationError):
            prepare_av1_v4r3_dogfood(
                repository_root=Path("/private/repository"),
                preparation_registry=Path("/private/preparation"),
                ordinal_registry=Path("/private/ordinal"),
                rights_attestation={},
                owner_principal="owner:test",
                confirmed_owner_principal="owner:wrong",
                preparation_grant_valid_until="2026-08-09T12:00:00Z",
                ordinal_1_valid_until="2026-08-09T10:00:00Z",
                source_paths={},
                dedicated_instance_paths={},
                quality_temp_paths={},
                tool_paths={},
            )

    def test_json_cli_suppresses_private_paths(self) -> None:
        fake = SimpleNamespace(
            created={},
            grant={"grant_id": "av1vprepgrant4r3_" + "a" * 32},
            claim={"claim_id": "av1v4r3prepclaim_" + "b" * 32},
            custody={"custody_id": "av1v4r3keycustody_" + "c" * 32},
            bundle={
                "bundle_id": "av1v4r3prepbundle_" + "d" * 32,
                "effective_config_hmac_id": "av1v4r3confighmac_" + "e" * 32,
            },
            measurement={"measurement_id": "av1v4r3prepmeas_" + "f" * 32},
            freeze={"freeze_id": "av1vfreeze4r3_" + "1" * 32},
            request={"request_id": "av1v4r3req_" + "2" * 32},
            plan={"plan_id": "av1vordplan4r3_" + "3" * 32, "plan_closes_at": "x"},
            preflight={"preflight_id": "av1v4r3preflight_" + "4" * 32},
            ordinal_grant={
                "grant_id": "av1vordgrant4r3_" + "5" * 32,
                "valid_until": "y",
            },
        )
        payload = {
            "repository_root": "/private/repository",
            "preparation_registry": "/private/preparation",
            "ordinal_registry": "/private/ordinal",
            "rights_attestation": {},
            "owner_principal": "owner:test",
            "confirm_owner_principal": "owner:test",
            "preparation_grant_valid_until": "2026-08-09T12:00:00Z",
            "ordinal_1_valid_until": "2026-08-09T10:00:00Z",
            "source_paths": {},
            "dedicated_instance_paths": {},
            "quality_temp_paths": {},
            "tool_paths": {},
        }
        with (
            patch.object(script_module, "prepare_av1_v4r3_dogfood", return_value=fake),
            patch.object(
                script_module.sys,
                "stdin",
                SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())),
            ),
            patch("builtins.print") as output,
        ):
            status = script_module.main()
        self.assertEqual(status, 0)
        self.assertNotIn("/private", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
