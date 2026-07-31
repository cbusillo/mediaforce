def _assert_preregistration_import_tree_clean(
        repository_root: object = None,
) -> None:
    bootstrap_sys = __import__("sys")
    bootstrap_os = __import__("os")
    if repository_root is None:
        bootstrap_sys.dont_write_bytecode = True
        if not bootstrap_sys.flags.isolated or not bootstrap_sys.flags.no_site:
            environment = {
                key: value
                for key, value in bootstrap_os.environ.items()
                if not key.startswith("PYTHON")
            }
            bootstrap_os.execve(
                bootstrap_sys.executable,
                (
                    bootstrap_sys.executable,
                    "-I",
                    "-S",
                    bootstrap_os.path.realpath(__file__),
                    *bootstrap_sys.argv[1:],
                ),
                environment,
            )
        root = bootstrap_os.path.realpath(
            bootstrap_os.path.join(
                bootstrap_os.path.dirname(__file__),
                bootstrap_os.pardir,
            )
        )
        expected_script = bootstrap_os.path.join(
            root,
            "scripts",
            "verify_av1_cold_start_preregistration.py",
        )
        if bootstrap_os.path.realpath(__file__) != expected_script:
            raise RuntimeError(
                "AV1 preregistration runner must execute from its canonical repository path"
            )
    else:
        root = bootstrap_os.path.realpath(bootstrap_os.fspath(repository_root))

    def fail_closed(exc: BaseException) -> None:
        raise RuntimeError(
            "AV1 preregistration runner could not inspect repository imports"
        ) from exc

    for relative_root in ("mediaforce", "scripts"):
        import_root = bootstrap_os.path.join(root, relative_root)
        if not bootstrap_os.path.isdir(import_root):
            continue
        for _current_root, directory_names, filenames in bootstrap_os.walk(
                import_root,
                followlinks=False,
                onerror=fail_closed,
        ):
            if "__pycache__" in directory_names:
                raise RuntimeError(
                    "AV1 preregistration runner refuses repository bytecode caches"
                )
            if any(
                filename.endswith((".pyc", ".pyo"))
                for filename in filenames
            ):
                raise RuntimeError(
                    "AV1 preregistration runner refuses repository bytecode artifacts"
                )

    def git_output(*arguments: str) -> tuple[int, bytes]:
        bootstrap_time = __import__("time")
        read_descriptor, write_descriptor = bootstrap_os.pipe()
        process_id = bootstrap_os.fork()
        if process_id == 0:
            try:
                bootstrap_os.close(read_descriptor)
                bootstrap_os.dup2(write_descriptor, 1)
                null_descriptor = bootstrap_os.open(
                    bootstrap_os.devnull,
                    bootstrap_os.O_RDWR,
                )
                bootstrap_os.dup2(null_descriptor, 0)
                bootstrap_os.dup2(null_descriptor, 2)
                bootstrap_os.close(write_descriptor)
                if null_descriptor > 2:
                    bootstrap_os.close(null_descriptor)
                bootstrap_os.chdir(root)
                bootstrap_os.execve(
                    "/usr/bin/git",
                    (
                        "/usr/bin/git",
                        "-c",
                        "core.attributesFile=/dev/null",
                        "-c",
                        "core.fsmonitor=false",
                        "-c",
                        "core.hooksPath=/dev/null",
                        *arguments,
                    ),
                    {
                        "GIT_ATTR_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_NO_REPLACE_OBJECTS": "1",
                        "GIT_OPTIONAL_LOCKS": "0",
                        "HOME": root,
                        "LANG": "C",
                        "LC_ALL": "C",
                        "PATH": "/usr/bin:/bin",
                    },
                )
            except BaseException:
                bootstrap_os._exit(127)
        bootstrap_os.close(write_descriptor)
        output = bytearray()
        deadline = bootstrap_time.monotonic() + 15.0
        process_status = None
        try:
            bootstrap_os.set_blocking(read_descriptor, False)
            while True:
                try:
                    chunk = bootstrap_os.read(read_descriptor, 64 * 1024)
                except BlockingIOError:
                    chunk = None
                if chunk:
                    output.extend(chunk)
                    if len(output) > 1024 * 1024:
                        bootstrap_os.kill(process_id, 9)
                        _waited_process_id, process_status = (
                            bootstrap_os.waitpid(process_id, 0)
                        )
                        raise RuntimeError(
                            "AV1 preregistration repository inspection is oversized"
                        )
                waited_process_id, status = bootstrap_os.waitpid(
                    process_id,
                    bootstrap_os.WNOHANG,
                )
                if waited_process_id == process_id:
                    process_status = status
                    bootstrap_os.set_blocking(read_descriptor, True)
                    while True:
                        chunk = bootstrap_os.read(read_descriptor, 64 * 1024)
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > 1024 * 1024:
                            raise RuntimeError(
                                "AV1 preregistration repository inspection is oversized"
                            )
                    break
                if bootstrap_time.monotonic() >= deadline:
                    bootstrap_os.kill(process_id, 9)
                    _waited_process_id, process_status = bootstrap_os.waitpid(
                        process_id,
                        0,
                    )
                    raise RuntimeError(
                        "AV1 preregistration repository inspection timed out"
                    )
                bootstrap_time.sleep(0.01)
        except BaseException:
            if process_status is None:
                try:
                    bootstrap_os.kill(process_id, 9)
                except ProcessLookupError:
                    pass
                try:
                    bootstrap_os.waitpid(process_id, 0)
                except ChildProcessError:
                    pass
            raise
        finally:
            bootstrap_os.close(read_descriptor)
        if process_status is None:
            raise RuntimeError(
                "AV1 preregistration repository inspection did not finish"
            )
        return bootstrap_os.waitstatus_to_exitcode(process_status), bytes(output)

    pathspec = ("--", "mediaforce", "scripts")
    for ignored in (False, True):
        arguments = ["ls-files", "-z", "--others"]
        if ignored:
            arguments.append("--ignored")
        arguments.extend(("--exclude-standard", *pathspec))
        return_code, output = git_output(*arguments)
        if return_code != 0:
            raise RuntimeError(
                "AV1 preregistration runner could not inspect repository imports"
            )
        if output:
            raise RuntimeError(
                "AV1 preregistration runner refuses untracked or ignored import state"
            )
    for arguments in (
        (
            "diff",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            *pathspec,
        ),
        (
            "diff",
            "--cached",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            "HEAD",
            *pathspec,
        ),
    ):
        return_code, _output = git_output(*arguments)
        if return_code == 1:
            raise RuntimeError(
                "AV1 preregistration runner refuses modified import state"
            )
        if return_code != 0:
            raise RuntimeError(
                "AV1 preregistration runner could not inspect repository imports"
            )
    return_code, index_output = git_output(
        "ls-files",
        "-v",
        "-z",
        *pathspec,
    )
    if return_code != 0 or any(
        not record.startswith(b"H ")
        for record in index_output.split(b"\0")
        if record
    ):
        raise RuntimeError(
            "AV1 preregistration runner refuses exceptional repository index state"
        )

    if repository_root is None:
        version_directory = (
            f"python{bootstrap_sys.version_info.major}."
            f"{bootstrap_sys.version_info.minor}"
        )
        stdlib_setting = getattr(bootstrap_sys, "_stdlib_dir", None)
        if not isinstance(stdlib_setting, str) or not stdlib_setting:
            raise RuntimeError(
                "AV1 preregistration runner cannot identify the standard library"
            )
        stdlib_directory = bootstrap_os.path.realpath(stdlib_setting)
        trusted_paths = [
            bootstrap_os.path.join(
                bootstrap_sys.base_prefix,
                "lib",
                f"python{bootstrap_sys.version_info.major}"
                f"{bootstrap_sys.version_info.minor}.zip",
            ),
            stdlib_directory,
            bootstrap_os.path.join(stdlib_directory, "lib-dynload"),
        ]
        virtual_environment = bootstrap_os.environ.get("VIRTUAL_ENV")
        for prefix in (
            virtual_environment,
            bootstrap_sys.prefix,
            bootstrap_sys.base_prefix,
        ):
            if not prefix:
                continue
            trusted_paths.append(
                bootstrap_os.path.join(
                    bootstrap_os.path.realpath(prefix),
                    "lib",
                    version_directory,
                    "site-packages",
                )
            )
        normalized_paths = []
        for candidate in trusted_paths:
            normalized = bootstrap_os.path.realpath(candidate)
            if normalized not in normalized_paths and (
                bootstrap_os.path.exists(normalized)
                or normalized.endswith(".zip")
            ):
                normalized_paths.append(normalized)
        bootstrap_sys.path[:] = normalized_paths
        bootstrap_sys.path_importer_cache.clear()
        if any(
            name == "mediaforce" or name.startswith("mediaforce.")
            for name in bootstrap_sys.modules
        ):
            raise RuntimeError(
                "AV1 preregistration runner refuses preloaded mediaforce modules"
            )


if __name__ == "__main__":
    _assert_preregistration_import_tree_clean()
    import importlib.util as _bootstrap_importlib_util

    _bootstrap_package_path = (
        __import__("os").path.realpath(
            __import__("os").path.join(
                __import__("os").path.dirname(__file__),
                __import__("os").pardir,
                "mediaforce",
            )
        )
    )
    _bootstrap_package_spec = _bootstrap_importlib_util.spec_from_file_location(
        "mediaforce",
        __import__("os").path.join(_bootstrap_package_path, "__init__.py"),
        submodule_search_locations=[_bootstrap_package_path],
    )
    if (
        _bootstrap_package_spec is None
        or _bootstrap_package_spec.loader is None
    ):
        raise RuntimeError(
            "AV1 preregistration runner could not bind the canonical mediaforce package"
        )
    _bootstrap_package = _bootstrap_importlib_util.module_from_spec(
        _bootstrap_package_spec
    )
    __import__("sys").modules["mediaforce"] = _bootstrap_package
    _bootstrap_package_spec.loader.exec_module(_bootstrap_package)


import argparse
from contextlib import contextmanager
import ctypes
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, cast, Iterator, Sequence, TypeAlias
import uuid

from mediaforce.core.config import (
    DEFAULT_CONFIG_PATH,
    MediaforceConfig,
    load_config,
    migrate_config_state,
)
from mediaforce.core.db import open_readonly_db
from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.process_control import (
    ManagedProcessController,
    ProcessCancelledError,
    ProcessDeadlineEnforcementError,
    run_command,
)
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationError,
    AV1ColdStartValidationManifestV1,
    assert_preregistered_av1_cold_start_validation_manifest,
    build_av1_cold_start_validation_report,
    format_av1_cold_start_validation_report,
    load_av1_cold_start_validation_evidence_set,
    load_av1_cold_start_validation_manifest,
)
from mediaforce.tuning.av1_validation_v2 import (
    AV1ValidationManifestV2,
    AV1ValidationV2Error,
    assert_preregistered_av1_validation_manifest_v2,
    assert_preregistered_av1_validation_v2_eligibility,
    build_av1_validation_v2_derivation_authorization,
    load_av1_validation_manifest_v2,
    load_av1_validation_v2_eligibility,
)
from mediaforce.tuning.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY,
    AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST,
    AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA,
    AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA_VERSION,
    AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BLOB_BYTES,
    AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BUNDLE_BYTES,
    AV1_VALIDATION_DERIVATION_REVIEW_RESPONSE_SCHEMA_NAME,
    AV1_VALIDATION_DERIVATION_STRUCTURED_REVIEW_RUN_SCHEMA,
    AV1ValidationDerivationCandidateProposal,
    AV1ValidationDerivationError,
    AV1ValidationDerivationPlan,
    AV1ValidationDerivationSourceCommitment,
    AV1ValidationDerivationReviewClaim,
    AV1ValidationDerivationReviewDecision,
    AV1ValidationDerivationReviewEnvelope,
    assert_av1_validation_derivation_authorization_active,
    assert_av1_validation_derivation_observed_attempts_accepted,
    assert_av1_validation_derivation_repository_identity,
    AV1ValidationDerivationReviewLane,
    av1_validation_derivation_attempt_recovery_action,
    av1_validation_derivation_terminal_intent_published_after,
    av1_validation_derivation_review_analysis_sha256,
    av1_validation_derivation_review_developer_text,
    av1_validation_derivation_candidate_evaluation_public_summary,
    av1_validation_derivation_plan_public_summary,
    av1_validation_derivation_statistics_contract_sha256,
    assert_av1_validation_derivation_source_commitments,
    build_av1_validation_derivation_plan,
    build_av1_validation_derivation_source_commitments,
    build_av1_validation_derivation_review_claim,
    build_av1_validation_derivation_review_attestation,
    build_av1_validation_derivation_review_envelope,
    build_av1_validation_derivation_review_request,
    build_av1_validation_derivation_review_response_schema,
    evaluate_av1_validation_derivation_candidate,
    load_av1_validation_derivation_assignment_claims,
    load_av1_validation_derivation_attempt_publication_state,
    load_av1_validation_derivation_attempts,
    load_av1_validation_derivation_candidate_proposal,
    load_av1_validation_derivation_plan,
    load_av1_validation_derivation_review_claims,
    load_av1_validation_derivation_review_envelope,
    load_av1_validation_derivation_terminal_intents,
    load_av1_validation_derivation_terminal_records,
    load_av1_validation_derivation_verdict_claims,
    load_av1_validation_derivation_verdict_intent,
    retain_av1_validation_derivation_publication_directories,
    validate_av1_validation_derivation_review_response,
    validate_av1_validation_derivation_artifact_root_binding,
    write_av1_validation_derivation_candidate_proposal,
    write_av1_validation_derivation_plan,
    write_av1_validation_derivation_review_claim,
    write_av1_validation_derivation_review_envelope,
)
from mediaforce.web.runtime.av1_validation_derivation import (
    av1_validation_derivation_artifact_root,
    av1_validation_derivation_execution_environment_sha256,
    av1_validation_derivation_runtime_context_sha256,
    assert_av1_validation_derivation_source_snapshot_capacity,
    assert_av1_validation_derivation_execution_environment,
    finalize_av1_validation_derivation_candidate_lock,
    load_current_av1_validation_derivation_observations,
    record_av1_validation_derivation_visual_verdict,
    run_av1_validation_derivation_assignment,
)
from mediaforce.web.runtime import (
    av1_validation_derivation as av1_validation_derivation_runtime,
)
from mediaforce.web.runtime_lock import (
    MediaforceRuntimeBusyError,
    exclusive_mediaforce_runtime_lock,
    reserve_mediaforce_database_identity,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1ValidationPartitionError,
    AV1ValidationPrivatePartition,
    assert_private_artifact_path,
    av1_validation_partition_key_id,
    av1_validation_partition_public_summary,
    build_av1_validation_private_partition,
    ensure_av1_validation_partition_key,
    load_av1_validation_partition_key,
    load_av1_validation_private_partition,
    validate_av1_validation_partition_current_inputs,
    validate_av1_validation_private_partition,
    write_av1_validation_private_partition,
)
from mediaforce.tuning.av1_validation_partition_inventory import (
    AV1ValidationPartitionSourceSHA256Session,
    av1_validation_partition_source_sha256_resolver,
    load_av1_validation_partition_inventory,
)
ValidationManifest: TypeAlias = (
    AV1ColdStartValidationManifestV1 | AV1ValidationManifestV2
)
REPOSITORY_ROOT = Path(
    av1_validation_derivation_runtime.__file__
).resolve().parents[3]
_CANONICAL_PREREGISTRATION_RUNNER = (
    REPOSITORY_ROOT / "scripts" / "verify_av1_cold_start_preregistration.py"
)
_AGENT_REVIEW_MAX_SECONDS = 1800
_AGENT_REVIEW_SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_AGENT_REVIEW_GENERIC_IDENTITY = "mediaforce-review"
_REVIEW_GIT_COMMAND_PREFIX = (
    "/usr/bin/git",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "core.fsmonitor=false",
)
_REVIEW_GIT_DIFF_OPTIONS = (
    "--quiet",
    "--no-ext-diff",
    "--no-textconv",
    "--ignore-submodules=none",
)
_LLM_REVIEW_MAXIMUM_RESPONSE_BYTES = 64 * 1024
_MACH_O_MAGICS = frozenset({
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _assert_canonical_preregistration_runner() -> None:
    try:
        executing_path = Path(__file__).resolve(strict=True)
        canonical_path = _CANONICAL_PREREGISTRATION_RUNNER.resolve(strict=True)
        executing_info = executing_path.stat()
        canonical_info = canonical_path.stat()
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation preregistration runner identity is unavailable"
        ) from exc
    if (
        executing_path != canonical_path
        or not stat.S_ISREG(executing_info.st_mode)
        or (executing_info.st_dev, executing_info.st_ino)
        != (canonical_info.st_dev, canonical_info.st_ino)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation preregistration runner is not the canonical repository file"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an AV1 cold-start preregistration or aggregate its redacted evidence",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    validate = actions.add_parser(
        "validate", help="Validate one canonical preregistration manifest"
    )
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--json", action="store_true", dest="json_output")

    eligibility = actions.add_parser(
        "validate-eligibility",
        help="Validate the pinned machine-local v2 aggregate eligibility attestation",
    )
    eligibility.add_argument("attestation", type=Path)
    eligibility.add_argument("--json", action="store_true", dest="json_output")

    create_key = actions.add_parser(
        "create-partition-key",
        help="Create one owner-only machine-local key for the private v2 source partition",
    )
    create_key.add_argument("key", type=Path)
    create_key.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    create_key.add_argument("--json", action="store_true", dest="json_output")

    build_partition = actions.add_parser(
        "build-partition",
        help="Build the private v2 source partition without authorizing execution",
    )
    build_partition.add_argument("manifest", type=Path)
    build_partition.add_argument("eligibility", type=Path)
    build_partition.add_argument("--key", type=Path, required=True)
    build_partition.add_argument("--expected-token-key-id", required=True)
    build_partition.add_argument("--output", type=Path, required=True)
    build_partition.add_argument("--selected-at", required=True)
    build_partition.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    build_partition.add_argument("--json", action="store_true", dest="json_output")

    validate_partition = actions.add_parser(
        "validate-partition",
        help="Validate the exact private v2 source partition against current locked inputs",
    )
    validate_partition.add_argument("manifest", type=Path)
    validate_partition.add_argument("eligibility", type=Path)
    validate_partition.add_argument("partition", type=Path)
    validate_partition.add_argument("--key", type=Path, required=True)
    validate_partition.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    validate_partition.add_argument("--json", action="store_true", dest="json_output")

    create_derivation_plan = actions.add_parser(
        "create-derivation-plan",
        help="Create the immutable owner-only v2 derivation authorization and 24-slot worklist",
    )
    create_derivation_plan.add_argument("manifest", type=Path)
    create_derivation_plan.add_argument("eligibility", type=Path)
    create_derivation_plan.add_argument("partition", type=Path)
    create_derivation_plan.add_argument("--key", type=Path, required=True)
    create_derivation_plan.add_argument("--valid-until", required=True)
    create_derivation_plan.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    create_derivation_plan.add_argument("--json", action="store_true", dest="json_output")

    validate_derivation_plan = actions.add_parser(
        "validate-derivation-plan",
        help="Validate the private v2 derivation plan against current locked inputs",
    )
    validate_derivation_plan.add_argument("manifest", type=Path)
    validate_derivation_plan.add_argument("eligibility", type=Path)
    validate_derivation_plan.add_argument("partition", type=Path)
    validate_derivation_plan.add_argument("plan", type=Path)
    validate_derivation_plan.add_argument("--key", type=Path, required=True)
    validate_derivation_plan.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    validate_derivation_plan.add_argument("--json", action="store_true", dest="json_output")

    run_derivation = actions.add_parser(
        "run-derivation-assignment",
        help="Run one exact authorized derivation assignment through unchanged measured full search",
    )
    run_derivation.add_argument("manifest", type=Path)
    run_derivation.add_argument("partition", type=Path)
    run_derivation.add_argument("plan", type=Path)
    run_derivation.add_argument("assignment_id")
    run_derivation.add_argument("--key", type=Path, required=True)
    run_derivation.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_derivation.add_argument("--json", action="store_true", dest="json_output")

    derivation_status = actions.add_parser(
        "derivation-status",
        help="Report privacy-safe derivation attempt and terminal counts",
    )
    derivation_status.add_argument("plan", type=Path)
    derivation_status.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    derivation_status.add_argument("--json", action="store_true", dest="json_output")

    record_verdict = actions.add_parser(
        "record-derivation-verdict",
        help="Append one human visual verdict and freeze its terminal derivation record",
    )
    record_verdict.add_argument("manifest", type=Path)
    record_verdict.add_argument("partition", type=Path)
    record_verdict.add_argument("plan", type=Path)
    record_verdict.add_argument("assignment_id")
    record_verdict.add_argument("--key", type=Path, required=True)
    record_verdict.add_argument("--verdict", choices=("approved", "rejected"), required=True)
    record_verdict.add_argument("--concern-tag", action="append", default=[])
    record_verdict.add_argument("--evidence-id", action="append", default=[])
    record_verdict.add_argument("--moment-index", action="append", type=int, default=[])
    record_verdict.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    record_verdict.add_argument("--json", action="store_true", dest="json_output")

    build_proposal = actions.add_parser(
        "build-derivation-proposal",
        help="Build one deterministic unapproved candidate proposal or exact no-go",
    )
    build_proposal.add_argument("manifest", type=Path)
    build_proposal.add_argument("partition", type=Path)
    build_proposal.add_argument("plan", type=Path)
    build_proposal.add_argument("cell_plan_id")
    build_proposal.add_argument("--key", type=Path, required=True)
    build_proposal.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    build_proposal.add_argument("--json", action="store_true", dest="json_output")

    record_review = actions.add_parser(
        "record-derivation-review",
        help="Record one immutable proposal-bound independent review lane",
    )
    record_review.add_argument("plan", type=Path)
    record_review.add_argument("cell_plan_id")
    record_review.add_argument("--lane", choices=(
        "architecture",
        "statistical_model_contract",
        "privacy_security",
        "experimental_design",
        "adversarial",
    ), required=True)
    record_review.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    record_review.add_argument("--json", action="store_true", dest="json_output")

    finalize_lock = actions.add_parser(
        "finalize-derivation-lock",
        help="Finalize one owner-only candidate lock after all five clean review lanes",
    )
    finalize_lock.add_argument("manifest", type=Path)
    finalize_lock.add_argument("partition", type=Path)
    finalize_lock.add_argument("plan", type=Path)
    finalize_lock.add_argument("cell_plan_id")
    finalize_lock.add_argument("--key", type=Path, required=True)
    finalize_lock.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    finalize_lock.add_argument("--json", action="store_true", dest="json_output")

    report = actions.add_parser(
        "report", help="Build a deterministic aggregate acceptance report"
    )
    report.add_argument("manifest", type=Path)
    report.add_argument("evidence", type=Path)
    report.add_argument("--as-of", required=True)
    report.add_argument(
        "--runtime-state", choices=("paused", "available"), default="paused"
    )
    report.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _assert_canonical_preregistration_runner()
    args = build_parser().parse_args(argv)
    try:
        if args.action == "create-partition-key":
            assert_private_artifact_path(args.key, repository_root=REPOSITORY_ROOT)
            config = load_config(args.config)
            try:
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "av1-partition-key-create"},
                ):
                    migrate_config_state(config)
                    reserve_mediaforce_database_identity(config)
                    token_key_id, created = ensure_av1_validation_partition_key(
                        args.key
                    )
            except MediaforceRuntimeBusyError as exc:
                raise AV1ValidationPartitionError(
                    "AV1 partition key creation requires the Mediaforce runtime to be paused"
                ) from exc
            payload = {
                "created": created,
                "token_key_id": token_key_id,
                "runtime_execution_authorized": False,
                "derivation_execution_authorized": False,
                "holdout_execution_authorized": False,
            }
            _print_partition_payload(payload, json_output=args.json_output)
            return 0

        if args.action in {"build-partition", "validate-partition"}:
            return _run_partition_action(args)

        if args.action == "validate-eligibility":
            assert_private_artifact_path(
                args.attestation,
                repository_root=REPOSITORY_ROOT,
            )
            eligibility = load_av1_validation_v2_eligibility(args.attestation)
            assert_preregistered_av1_validation_v2_eligibility(eligibility)
            payload = {
                "eligibility_valid": True,
                "runtime_execution_authorized": False,
                "derivation_execution_authorized": False,
                "holdout_execution_authorized": False,
            }
            _print_partition_payload(payload, json_output=args.json_output)
            return 0

        if args.action in {
            "create-derivation-plan",
            "validate-derivation-plan",
            "run-derivation-assignment",
            "derivation-status",
            "record-derivation-verdict",
            "build-derivation-proposal",
            "record-derivation-review",
            "finalize-derivation-lock",
        }:
            return _run_derivation_action(args)

        manifest = _load_manifest(args.manifest)
        if isinstance(manifest, AV1ValidationManifestV2):
            assert_preregistered_av1_validation_manifest_v2(manifest)
        else:
            assert_preregistered_av1_cold_start_validation_manifest(manifest)
        if args.action == "validate":
            payload = _validation_payload(manifest)
            if args.json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"manifest={manifest.manifest_id} state={payload['state']} "
                    f"plans={len(manifest.cell_plans)} cases={len(manifest.cases)} "
                    "runtime_execution_authorized=false "
                    f"holdout_execution_authorized={str(payload['holdout_execution_authorized']).lower()}"
                )
            return 0

        if isinstance(manifest, AV1ValidationManifestV2):
            raise AV1ValidationV2Error(
                "AV1 v2 holdout reports remain blocked until a separate execution authorization exists"
            )
        evidence_set = load_av1_cold_start_validation_evidence_set(args.evidence)
        report = build_av1_cold_start_validation_report(
            manifest,
            evidence_set,
            as_of=args.as_of,
            runtime_state=args.runtime_state,
        )
        if args.json_output:
            print(json.dumps(report.to_payload(), indent=2, sort_keys=True))
        else:
            print(format_av1_cold_start_validation_report(report))
        return 0 if report.supports_publication_review else 2
    except OSError:
        print(
            "AV1 cold-start validation failed: private or repository input is unreadable",
            file=sys.stderr,
        )
        return 1
    except (
        AV1ColdStartValidationError,
        AV1ValidationDerivationError,
        AV1ValidationPartitionError,
        AV1ValidationV2Error,
        json.JSONDecodeError,
    ) as exc:
        print(f"AV1 cold-start validation failed: {exc}", file=sys.stderr)
        return 1
    except (TypeError, ValueError):
        print(
            "AV1 cold-start validation failed: private or repository input is invalid",
            file=sys.stderr,
        )
        return 1


def _run_partition_action(args: argparse.Namespace) -> int:
    manifest = load_av1_validation_manifest_v2(args.manifest)
    assert_preregistered_av1_validation_manifest_v2(manifest)
    assert_private_artifact_path(args.eligibility, repository_root=REPOSITORY_ROOT)
    assert_private_artifact_path(args.key, repository_root=REPOSITORY_ROOT)
    eligibility = load_av1_validation_v2_eligibility(args.eligibility)
    assert_preregistered_av1_validation_v2_eligibility(eligibility)
    token_key = load_av1_validation_partition_key(args.key)
    if args.action == "build-partition":
        assert_private_artifact_path(args.output, repository_root=REPOSITORY_ROOT)
        config = load_config(args.config)
        try:
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "av1-partition-build"},
            ):
                migrate_config_state(config)
                reserve_mediaforce_database_identity(config)
                with open_readonly_db(config.paths.db_path) as connection:
                    inventory = load_av1_validation_partition_inventory(
                        connection,
                        config=config,
                    )
                    with av1_validation_partition_source_sha256_resolver(
                        connection,
                        config=config,
                        verify_evidence=True,
                    ) as source_sha256_resolver:
                        partition = build_av1_validation_private_partition(
                            manifest=manifest,
                            eligibility_attestation_id=eligibility.attestation_id,
                            eligibility_payload_sha256=eligibility.payload_sha256,
                            sources=inventory.sources,
                            expectations=inventory.expectations,
                            token_key=token_key,
                            expected_token_key_id=args.expected_token_key_id,
                            selected_at=args.selected_at,
                        )
                        build_av1_validation_derivation_source_commitments(
                            partition=partition,
                            assignments=tuple(
                                assignment
                                for assignment in partition.assignments
                                if assignment.role == "derivation"
                            ),
                            resolver=source_sha256_resolver,
                        )
                        source_sha256_resolver.verify()
                        write_av1_validation_private_partition(
                            args.output,
                            partition,
                            before_publish=source_sha256_resolver.assert_quiet,
                        )
        except MediaforceRuntimeBusyError as exc:
            raise AV1ValidationPartitionError(
                "AV1 partition build requires the Mediaforce runtime to be paused"
            ) from exc
    else:
        assert_private_artifact_path(args.partition, repository_root=REPOSITORY_ROOT)
        partition = load_av1_validation_private_partition(args.partition)
        if av1_validation_partition_key_id(token_key) != partition.token_key_id:
            raise AV1ValidationPartitionError(
                "AV1 partition key does not match the frozen token-key ID"
            )
        validate_av1_validation_private_partition(
            partition,
            manifest=manifest,
            token_key=token_key,
        )
        config = load_config(args.config)
        with open_readonly_db(config.paths.db_path) as connection:
            inventory = load_av1_validation_partition_inventory(
                connection,
                config=config,
            )
            validate_av1_validation_partition_current_inputs(
                partition,
                manifest=manifest,
                sources=inventory.sources,
                expectations=inventory.expectations,
                token_key=token_key,
            )
    _print_partition_payload(
        av1_validation_partition_public_summary(partition),
        json_output=args.json_output,
    )
    return 0


def _run_derivation_action(
        args: argparse.Namespace,
) -> int:
    _assert_canonical_preregistration_runner()
    direct_write_action = args.action in {
        "create-derivation-plan",
        "build-derivation-proposal",
        "record-derivation-review",
    }
    if direct_write_action:
        config = load_config(args.config)
        try:
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={
                    "purpose": "av1-derivation-tooling",
                    "action": str(args.action),
                },
            ):
                migrate_config_state(config)
                reserve_mediaforce_database_identity(config)
                return _run_derivation_action_body(
                    args,
                    locked_config=config,
                )
        except MediaforceRuntimeBusyError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation tooling requires the Mediaforce runtime to be paused"
            ) from exc
    return _run_derivation_action_body(args, locked_config=None)


def _run_derivation_action_body(
        args: argparse.Namespace,
        *,
        locked_config: MediaforceConfig | None,
) -> int:
    direct_write_action = args.action in {
        "create-derivation-plan",
        "build-derivation-proposal",
        "record-derivation-review",
    }
    if direct_write_action and locked_config is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation tooling lost its locked runtime context"
        )
    if args.action in {"create-derivation-plan", "validate-derivation-plan"}:
        return _run_derivation_plan_action(
            args,
            config=locked_config or load_config(args.config),
        )

    if args.action == "run-derivation-assignment":
        for path in (args.partition, args.plan, args.key):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        manifest = load_av1_validation_manifest_v2(args.manifest)
        partition = load_av1_validation_private_partition(args.partition)
        plan, artifact_root = _load_recovery_capable_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        token_key = load_av1_validation_partition_key(args.key)
        process_controller = ManagedProcessController()

        attempt = run_av1_validation_derivation_assignment(
            config_path=args.config,
            manifest=manifest,
            partition=partition,
            token_key=token_key,
            plan=plan,
            assignment_id=args.assignment_id,
            attempts_directory=artifact_root / "attempts",
            terminal_records_directory=artifact_root / "terminal-records",
            repository_identity_resolver=_live_repository_identity,
            process_controller=process_controller,
        )
        _print_partition_payload(
            {
                "attempt_id": attempt.attempt_id,
                "attempt_sha256": attempt.payload_sha256,
                "status": attempt.status,
                "review_required": attempt.status == "review_pending",
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0 if attempt.status == "review_pending" else 2

    if args.action == "derivation-status":
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        attempts_directory = artifact_root / "attempts"
        records_directory = artifact_root / "terminal-records"
        attempts = (
            load_av1_validation_derivation_attempts(
                attempts_directory,
                review_pending_published_before=plan.authorization.valid_until,
                allow_unaccepted_review_pending=True,
            )
            if attempts_directory.exists()
            else ()
        )
        records = (
            load_av1_validation_derivation_terminal_records(
                records_directory,
                observed_published_before=plan.authorization.valid_until,
            )
            if records_directory.exists()
            else ()
        )
        assignment_claims = load_av1_validation_derivation_assignment_claims(
            attempts_directory,
            plan=plan,
        )
        terminal_intents_directory = artifact_root / "terminal-intents"
        terminal_intents = (
            load_av1_validation_derivation_terminal_intents(
                terminal_intents_directory,
                observed_published_before=plan.authorization.valid_until,
                allow_late_observed=True,
            )
            if terminal_intents_directory.exists()
            else ()
        )
        verdict_claims = load_av1_validation_derivation_verdict_claims(
            artifact_root / "verdict-claims",
            plan=plan,
            attempts=attempts,
        )
        attempts_by_assignment = {
            attempt.assignment_id: attempt
            for attempt in attempts
        }
        attempt_assignment_ids = set(attempts_by_assignment)
        terminal_assignment_ids = {
            record.assignment_id
            for record in records
        }
        unaccepted_attempt_count = sum(
            av1_validation_derivation_attempt_recovery_action(
                attempt,
                publication_state,
                terminal_intents=terminal_intents,
                terminal_records=records,
            )
            != "none"
            for attempt in attempts
            if attempt.status == "review_pending"
            for publication_state in (
                load_av1_validation_derivation_attempt_publication_state(
                    attempts_directory,
                    attempt,
                    published_before=plan.authorization.valid_until,
                ),
            )
        )
        unresolved_assignment_claim_count = sum(
            str(claim["assignment_id"]) not in attempt_assignment_ids
            for claim in assignment_claims
        )
        unresolved_terminal_intent_count = sum(
            intent.assignment_id not in terminal_assignment_ids
            for intent in terminal_intents
        )
        late_observed_terminal_intent_count = sum(
            av1_validation_derivation_terminal_intent_published_after(
                terminal_intents_directory,
                intent,
                published_before=plan.authorization.valid_until,
            )
            for intent in terminal_intents
        )
        unresolved_verdict_claim_count = 0
        unresolved_verdict_intent_count = 0
        for claim in verdict_claims:
            assignment_id = str(claim["assignment_id"])
            if assignment_id in terminal_assignment_ids:
                continue
            attempt = attempts_by_assignment[assignment_id]
            verdict_intent = load_av1_validation_derivation_verdict_intent(
                artifact_root / "verdict-intents",
                plan=plan,
                attempt=attempt,
            )
            if verdict_intent is None:
                unresolved_verdict_claim_count += 1
            else:
                unresolved_verdict_intent_count += 1
        recovery_required = any((
            unaccepted_attempt_count,
            unresolved_assignment_claim_count,
            unresolved_terminal_intent_count,
            late_observed_terminal_intent_count,
            unresolved_verdict_claim_count,
            unresolved_verdict_intent_count,
        ))
        attempt_counts = {
            status: sum(attempt.status == status for attempt in attempts)
            for status in ("review_pending", "failed", "excluded", "stopped")
        }
        record_counts = {
            status: sum(record.status == status for record in records)
            for status in ("observed", "failed", "excluded", "stopped")
        }
        _print_partition_payload(
            {
                "plan_id": plan.plan_id,
                "registered_assignment_count": len(plan.assignments),
                "attempt_count": len(attempts),
                "terminal_record_count": len(records),
                "assignment_claim_count": len(assignment_claims),
                "terminal_intent_count": len(terminal_intents),
                "verdict_claim_count": len(verdict_claims),
                "unaccepted_attempt_count": unaccepted_attempt_count,
                "unresolved_assignment_claim_count": (
                    unresolved_assignment_claim_count
                ),
                "unresolved_terminal_intent_count": (
                    unresolved_terminal_intent_count
                ),
                "late_observed_terminal_intent_count": (
                    late_observed_terminal_intent_count
                ),
                "unresolved_verdict_claim_count": (
                    unresolved_verdict_claim_count
                ),
                "unresolved_verdict_intent_count": (
                    unresolved_verdict_intent_count
                ),
                "recovery_required": recovery_required,
                **{f"attempt_{key}_count": value for key, value in attempt_counts.items()},
                **{f"terminal_{key}_count": value for key, value in record_counts.items()},
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 2 if recovery_required else 0

    if args.action == "record-derivation-verdict":
        for path in (args.partition, args.plan, args.key):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        manifest = load_av1_validation_manifest_v2(args.manifest)
        assert_preregistered_av1_validation_manifest_v2(manifest)
        partition = load_av1_validation_private_partition(args.partition)
        token_key = load_av1_validation_partition_key(args.key)
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        attempts = load_av1_validation_derivation_attempts(
            artifact_root / "attempts",
            review_pending_published_before=plan.authorization.valid_until,
        )
        attempt = next(
            (item for item in attempts if item.assignment_id == args.assignment_id),
            None,
        )
        if attempt is None:
            raise AV1ValidationDerivationError(
                "AV1 derivation assignment has no immutable attempt"
            )
        terminal = record_av1_validation_derivation_visual_verdict(
            config_path=args.config,
            manifest=manifest,
            plan=plan,
            partition=partition,
            token_key=token_key,
            attempt=attempt,
            terminal_records_directory=artifact_root / "terminal-records",
            verdict=args.verdict,
            concern_tags=args.concern_tag,
            evidence_ids=args.evidence_id,
            moment_indexes=args.moment_index,
            repository_identity_resolver=_live_repository_identity,
        )
        _print_partition_payload(
            {
                "terminal_record_sha256": terminal.payload_sha256,
                "status": terminal.status,
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0 if terminal.status == "observed" else 2

    if args.action == "build-derivation-proposal":
        return _run_derivation_proposal_action(
            args,
            config=cast(MediaforceConfig, locked_config),
            repository_identity_resolver=_live_repository_identity,
        )

    if args.action == "record-derivation-review":
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
            config=locked_config,
        )
        assert_av1_validation_derivation_execution_environment(plan)
        proposal = load_av1_validation_derivation_candidate_proposal(
            artifact_root,
            plan=plan,
            cell_plan_id=args.cell_plan_id,
        )
        with retain_av1_validation_derivation_publication_directories((
            (
                artifact_root / "review-claims" / proposal.proposal_id,
                "review_claims",
                proposal.proposal_id,
                proposal.payload_sha256,
            ),
            (
                artifact_root / "reviews" / proposal.proposal_id,
                "reviews",
                proposal.proposal_id,
                proposal.payload_sha256,
            ),
        )) as publication_guard:
            existing_review = _load_existing_derivation_review(
                artifact_root=artifact_root,
                plan=plan,
                proposal=proposal,
                lane=args.lane,
            )
            repository_commit, repository_tree = _repository_review_identity(
                process_controller=ManagedProcessController(),
            )
            assert_av1_validation_derivation_repository_identity(
                plan,
                repository_commit=repository_commit,
                repository_tree=repository_tree,
            )
            if existing_review is None:
                claim, review_evidence, decision = _run_code_llm_review(
                    artifact_root=artifact_root,
                    plan=plan,
                    proposal=proposal,
                    lane=args.lane,
                    before_publish=publication_guard,
                )
                review_evidence_sha256 = (
                    f"sha256:{hashlib.sha256(review_evidence).hexdigest()}"
                )
                review = build_av1_validation_derivation_review_attestation(
                    proposal=proposal,
                    claim=claim,
                    review_evidence_sha256=review_evidence_sha256,
                    decision=decision,
                    reviewed_at=_now_iso(),
                )
                envelope = build_av1_validation_derivation_review_envelope(
                    review=review,
                    evidence=review_evidence,
                )
            else:
                claim, envelope = existing_review
                review = envelope.review
            assert_av1_validation_derivation_execution_environment(plan)
            write_av1_validation_derivation_review_envelope(
                artifact_root,
                plan=plan,
                proposal=proposal,
                claim=claim,
                envelope=envelope,
                before_publish=publication_guard,
            )
            publication_guard()
        _print_partition_payload(
            {
                "proposal_id": proposal.proposal_id,
                "review_lane": review.lane,
                "decision": review.decision,
                "review_sha256": review.payload_sha256,
                "candidate_lock_created": False,
                "holdout_execution_authorized": False,
            },
            json_output=args.json_output,
        )
        return 0 if review.decision == "approved" else 2

    if args.action == "finalize-derivation-lock":
        for path in (
            args.plan,
            args.partition,
            args.key,
        ):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        manifest = load_av1_validation_manifest_v2(args.manifest)
        assert_preregistered_av1_validation_manifest_v2(manifest)
        partition = load_av1_validation_private_partition(args.partition)
        token_key = load_av1_validation_partition_key(args.key)
        validate_av1_validation_private_partition(
            partition,
            manifest=manifest,
            token_key=token_key,
        )
        lock_envelope = finalize_av1_validation_derivation_candidate_lock(
            config_path=args.config,
            manifest=manifest,
            partition=partition,
            token_key=token_key,
            plan_path=args.plan,
            cell_plan_id=args.cell_plan_id,
            repository_identity_resolver=_live_repository_identity,
            now_iso=_now_iso,
        )
        candidate_lock = lock_envelope.candidate_lock
        _print_partition_payload(
            {
                "candidate_lock_id": candidate_lock.candidate_lock_id,
                "candidate_lock_sha256": candidate_lock.payload_sha256,
                "candidate_lock_envelope_sha256": lock_envelope.payload_sha256,
                "candidate_lock_reviewed": True,
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0

    raise AV1ValidationDerivationError("AV1 derivation action is unsupported")


def _run_derivation_plan_action(
        args: argparse.Namespace,
        *,
        config: MediaforceConfig,
) -> int:
    with _load_current_derivation_inputs(
        args,
        config=config,
    ) as (
        manifest,
        partition,
        _token_key,
        source_sha256_session,
        source_commitments,
    ):
        runtime_context_sha256 = (
            av1_validation_derivation_runtime_context_sha256(config)
        )
        quality_metrics = {
            assignment.quality_metric
            for assignment in partition.assignments
            if assignment.role == "derivation"
        }
        if len(quality_metrics) != 1:
            raise AV1ValidationDerivationError(
                "AV1 derivation partition quality metric is not uniform"
            )
        execution_environment_sha256 = (
            av1_validation_derivation_execution_environment_sha256(
                quality_metric=next(iter(quality_metrics)),
            )
        )
        statistics_contract_sha256 = (
            av1_validation_derivation_statistics_contract_sha256(manifest)
        )
        (
            _review_runner_path,
            review_runner_canonical_path_sha256,
            review_runner_binary_sha256,
        ) = _review_runner_identity()
        repository_process_controller = ManagedProcessController()
        repository_commit, repository_tree = _repository_review_identity(
            process_controller=repository_process_controller,
        )
        if args.action == "create-derivation-plan":
            artifact_root = (
                config.paths.web_state_dir
                / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
                / partition.partition_id
            ).expanduser().resolve()
            assert_private_artifact_path(
                artifact_root,
                repository_root=REPOSITORY_ROOT,
            )
            existing_plan_path = artifact_root / "plan.json"
            if existing_plan_path.exists() or existing_plan_path.is_symlink():
                plan = load_av1_validation_derivation_plan(existing_plan_path)
                if plan.authorization.valid_until != args.valid_until:
                    raise AV1ValidationDerivationError(
                        "AV1 derivation plan retry changed the authorization window"
                    )
            else:
                root_binding_path = artifact_root / ".binding"
                if root_binding_path.exists() or root_binding_path.is_symlink():
                    raise AV1ValidationDerivationError(
                        "AV1 derivation root binding exists without its immutable plan"
                    )
                authorization = build_av1_validation_v2_derivation_authorization(
                    manifest=manifest,
                    selection_lock_sha256=partition.selection_lock_sha256,
                    derivation_partition_sha256=partition.derivation_partition_sha256,
                    authorized_at=_now_iso(),
                    valid_until=args.valid_until,
                )
                plan = build_av1_validation_derivation_plan(
                    manifest=manifest,
                    partition=partition,
                    authorization=authorization,
                    runtime_context_sha256=runtime_context_sha256,
                    execution_environment_sha256=execution_environment_sha256,
                    statistics_contract_sha256=statistics_contract_sha256,
                    review_runner_canonical_path_sha256=(
                        review_runner_canonical_path_sha256
                    ),
                    review_runner_binary_sha256=review_runner_binary_sha256,
                    repository_commit=repository_commit,
                    repository_tree=repository_tree,
                    source_commitments=source_commitments,
                )
                artifact_root = _derivation_artifact_root_for_plan(
                    config=config,
                    plan=plan,
                )
        else:
            plan, _artifact_root = _load_canonical_derivation_plan(
                plan_path=args.plan,
                config_path=args.config,
                config=config,
            )
        if (
            plan.execution_environment_sha256
            != execution_environment_sha256
            or plan.statistics_contract_sha256
            != statistics_contract_sha256
            or plan.review_runner_canonical_path_sha256
            != review_runner_canonical_path_sha256
            or plan.review_runner_binary_sha256
            != review_runner_binary_sha256
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation plan execution contract drifted"
            )
        assert_av1_validation_derivation_repository_identity(
            plan,
            repository_commit=repository_commit,
            repository_tree=repository_tree,
        )
        rebuilt = build_av1_validation_derivation_plan(
            manifest=manifest,
            partition=partition,
            authorization=plan.authorization,
            runtime_context_sha256=runtime_context_sha256,
            execution_environment_sha256=execution_environment_sha256,
            statistics_contract_sha256=statistics_contract_sha256,
            review_runner_canonical_path_sha256=(
                review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=review_runner_binary_sha256,
            repository_commit=repository_commit,
            repository_tree=repository_tree,
            source_commitments=source_commitments,
        )
        if rebuilt != plan:
            raise AV1ValidationDerivationError(
                "AV1 derivation plan does not match current locked inputs"
            )
        if args.action == "create-derivation-plan":
            def _assert_plan_repository_identity() -> None:
                current_identity = _repository_review_identity(
                    process_controller=repository_process_controller,
                )
                if current_identity != (repository_commit, repository_tree):
                    raise AV1ValidationDerivationError(
                        "AV1 derivation plan repository identity changed before publication"
                    )

            def _before_plan_publish() -> None:
                assert_av1_validation_derivation_authorization_active(
                    plan,
                    at=_now_iso(),
                )
                _assert_plan_repository_identity()
                source_sha256_session.assert_quiet()
                assert_av1_validation_derivation_source_snapshot_capacity(
                    artifact_root,
                    plan=plan,
                )
                source_sha256_session.assert_quiet()
                _assert_plan_repository_identity()
                assert_av1_validation_derivation_authorization_active(
                    plan,
                    at=_now_iso(),
                )

            def _after_plan_publish() -> None:
                source_sha256_session.assert_quiet()
                _assert_plan_repository_identity()

            write_av1_validation_derivation_plan(
                artifact_root,
                plan,
                before_publish=_before_plan_publish,
                after_publish=_after_plan_publish,
                before_bind=_before_plan_publish,
            )
        _print_partition_payload(
            av1_validation_derivation_plan_public_summary(plan),
            json_output=args.json_output,
        )
        return 0


def _run_derivation_proposal_action(
        args: argparse.Namespace,
        *,
        config: MediaforceConfig,
        repository_identity_resolver: Callable[[], tuple[str, str]],
) -> int:
    for path in (
        args.partition,
        args.key,
        args.plan,
    ):
        assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
    manifest = load_av1_validation_manifest_v2(args.manifest)
    assert_preregistered_av1_validation_manifest_v2(manifest)
    plan, artifact_root = _load_canonical_derivation_plan(
        plan_path=args.plan,
        config_path=args.config,
        config=config,
    )

    def assert_live_repository_identity() -> None:
        repository_commit, repository_tree = repository_identity_resolver()
        assert_av1_validation_derivation_repository_identity(
            plan,
            repository_commit=repository_commit,
            repository_tree=repository_tree,
        )

    assert_live_repository_identity()
    with _load_derivation_partition_for_evaluation(
        manifest=manifest,
        partition_path=args.partition,
        key_path=args.key,
        config_path=args.config,
        config=config,
        plan=plan,
    ) as (partition, source_sha256_session):
        assert_av1_validation_derivation_execution_environment(plan)
        attempts = load_av1_validation_derivation_attempts(
            artifact_root / "attempts",
            review_pending_published_before=plan.authorization.valid_until,
            require_durable=True,
        )
        records = load_av1_validation_derivation_terminal_records(
            artifact_root / "terminal-records",
            observed_published_before=plan.authorization.valid_until,
        )
        assert_av1_validation_derivation_observed_attempts_accepted(
            artifact_root,
            plan,
            attempts,
            records,
        )
        current_observations = load_current_av1_validation_derivation_observations(
            config_path=args.config,
            config=config,
            records=records,
        )
        proposal_path = (
            artifact_root / "proposals" / f"{args.cell_plan_id}.json"
        )
        proposal_exists = proposal_path.exists() or proposal_path.is_symlink()
        if proposal_exists:
            persisted_proposal = load_av1_validation_derivation_candidate_proposal(
                artifact_root,
                plan=plan,
                cell_plan_id=args.cell_plan_id,
            )
            proposed_at = persisted_proposal.proposed_at
        else:
            proposed_at = _now_iso()
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=manifest,
            plan=plan,
            partition=partition,
            cell_plan_id=args.cell_plan_id,
            attempts=attempts,
            records=records,
            current_observations=current_observations,
            proposed_at=proposed_at,
        )
        summary = av1_validation_derivation_candidate_evaluation_public_summary(
            evaluation
        )
        _print_partition_payload(summary, json_output=args.json_output)
        if evaluation.proposal is None:
            return 2
        assert_av1_validation_derivation_execution_environment(plan)
        def _before_proposal_publish() -> None:
            source_sha256_session.assert_quiet()
            assert_av1_validation_derivation_authorization_active(
                plan,
                at=_now_iso(),
            )
            assert_live_repository_identity()

        write_av1_validation_derivation_candidate_proposal(
            artifact_root,
            plan=plan,
            proposal=evaluation.proposal,
            before_publish=(None if proposal_exists else _before_proposal_publish),
        )
        assert_live_repository_identity()
        return 0


@contextmanager
def _load_current_derivation_inputs(
        args: argparse.Namespace,
        *,
        config: MediaforceConfig | None = None,
) -> Iterator[tuple[
    AV1ValidationManifestV2,
    AV1ValidationPrivatePartition,
    bytes,
    AV1ValidationPartitionSourceSHA256Session,
    tuple[AV1ValidationDerivationSourceCommitment, ...],
]]:
    for path in (args.eligibility, args.partition, args.key):
        assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
    manifest = load_av1_validation_manifest_v2(args.manifest)
    assert_preregistered_av1_validation_manifest_v2(manifest)
    eligibility = load_av1_validation_v2_eligibility(args.eligibility)
    assert_preregistered_av1_validation_v2_eligibility(eligibility)
    partition = load_av1_validation_private_partition(args.partition)
    token_key = load_av1_validation_partition_key(args.key)
    validate_av1_validation_private_partition(
        partition,
        manifest=manifest,
        token_key=token_key,
    )
    current_config = config or load_config(args.config)
    with open_readonly_db(current_config.paths.db_path) as connection:
        inventory = load_av1_validation_partition_inventory(
            connection,
            config=current_config,
        )
        with av1_validation_partition_source_sha256_resolver(
            connection,
            config=current_config,
        ) as source_sha256_resolver:
            validate_av1_validation_partition_current_inputs(
                partition,
                manifest=manifest,
                sources=inventory.sources,
                expectations=inventory.expectations,
                token_key=token_key,
            )
            source_commitments = (
                build_av1_validation_derivation_source_commitments(
                    partition=partition,
                    assignments=tuple(
                        assignment
                        for assignment in partition.assignments
                        if assignment.role == "derivation"
                    ),
                    resolver=source_sha256_resolver,
                )
            )
            source_sha256_resolver.verify()
            yield (
                manifest,
                partition,
                token_key,
                source_sha256_resolver,
                source_commitments,
            )


@contextmanager
def _load_derivation_partition_for_evaluation(
        *,
        manifest: AV1ValidationManifestV2,
        partition_path: Path,
        key_path: Path,
        config_path: Path,
        config: MediaforceConfig | None = None,
        plan: AV1ValidationDerivationPlan,
) -> Iterator[tuple[
    AV1ValidationPrivatePartition,
    AV1ValidationPartitionSourceSHA256Session,
]]:
    partition = load_av1_validation_private_partition(partition_path)
    token_key = load_av1_validation_partition_key(key_path)
    validate_av1_validation_private_partition(
        partition,
        manifest=manifest,
        token_key=token_key,
    )
    current_config = config or load_config(config_path)
    with open_readonly_db(current_config.paths.db_path) as connection:
        inventory = load_av1_validation_partition_inventory(
            connection,
            config=current_config,
        )
        with av1_validation_partition_source_sha256_resolver(
            connection,
            config=current_config,
        ) as source_sha256_resolver:
            validate_av1_validation_partition_current_inputs(
                partition,
                manifest=manifest,
                sources=inventory.sources,
                expectations=inventory.expectations,
                token_key=token_key,
            )
            assert_av1_validation_derivation_source_commitments(
                plan,
                resolver=source_sha256_resolver,
            )
            source_sha256_resolver.verify()
            yield partition, source_sha256_resolver


def _derivation_artifact_root_for_plan(
        *,
        config: MediaforceConfig,
        plan: AV1ValidationDerivationPlan,
) -> Path:
    root = av1_validation_derivation_artifact_root(config, plan)
    assert_private_artifact_path(root, repository_root=REPOSITORY_ROOT)
    return root


def _load_canonical_derivation_plan(
        *,
        plan_path: Path,
        config_path: Path,
        config: MediaforceConfig | None = None,
) -> tuple[AV1ValidationDerivationPlan, Path]:
    plan, bound_artifact_root = _load_bound_derivation_plan(plan_path)
    artifact_root = _derivation_artifact_root_for_plan(
        config=config or load_config(config_path),
        plan=plan,
    )
    if bound_artifact_root != artifact_root.expanduser().resolve():
        raise AV1ValidationDerivationError(
            "AV1 derivation plan must use the partition-global canonical private state root"
        )
    return plan, artifact_root


def _load_recovery_capable_derivation_plan(
        *,
        plan_path: Path,
        config_path: Path,
) -> tuple[AV1ValidationDerivationPlan, Path]:
    plan, artifact_root = _load_bound_derivation_plan(plan_path)
    config = load_config(config_path)
    expected_state_root = (
        config.paths.web_state_dir
        / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
        / plan.partition_id
    ).expanduser().resolve()
    if artifact_root != expected_state_root:
        raise AV1ValidationDerivationError(
            "AV1 derivation recovery requires the canonical runtime state root"
        )
    return plan, artifact_root


def _load_bound_derivation_plan(
        plan_path: Path,
) -> tuple[AV1ValidationDerivationPlan, Path]:
    assert_private_artifact_path(plan_path, repository_root=REPOSITORY_ROOT)
    plan = load_av1_validation_derivation_plan(plan_path)
    artifact_root = plan_path.expanduser().resolve().parent
    if plan_path.expanduser().resolve() != artifact_root / "plan.json":
        raise AV1ValidationDerivationError(
            "AV1 derivation plan must use the frozen artifact-root plan path"
        )
    validate_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )
    return plan, artifact_root


def _review_runner_identity() -> tuple[Path, str, str]:
    code_binary = shutil.which("code")
    if code_binary is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation review requires the Every Code executable"
        )
    try:
        resolved_binary = Path(code_binary).expanduser().resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable is unavailable"
        ) from exc
    if resolved_binary != _trusted_code_ancestor_path():
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable is not the active trusted runner"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved_binary, flags)
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable is unavailable"
        ) from exc
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = resolved_binary.lstat()
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
            or not os.access(resolved_binary, os.X_OK)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation Every Code executable identity is invalid"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            binary_bytes = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _assert_native_review_runner(binary_bytes)
    canonical_path_sha256 = (
        f"sha256:{hashlib.sha256(str(resolved_binary).encode('utf-8')).hexdigest()}"
    )
    binary_sha256 = f"sha256:{hashlib.sha256(binary_bytes).hexdigest()}"
    return resolved_binary, canonical_path_sha256, binary_sha256


def _authorized_review_runner_identity(
        plan: AV1ValidationDerivationPlan,
) -> tuple[Path, str, str]:
    identity = _review_runner_identity()
    if (
        identity[1] != plan.review_runner_canonical_path_sha256
        or identity[2] != plan.review_runner_binary_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable drifted from the plan"
        )
    return identity


def _is_raw_git_object_id(value: str) -> bool:
    return (
        len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _review_git_command(*arguments: str) -> list[str]:
    return [*_REVIEW_GIT_COMMAND_PREFIX, *arguments]


def _review_git_diff_command(command: str, *arguments: str) -> list[str]:
    return _review_git_command(
        command,
        *_REVIEW_GIT_DIFF_OPTIONS,
        *arguments,
    )


def _repository_review_identity(
        *,
        process_controller: ManagedProcessController,
        repository_root: Path | None = None,
) -> tuple[str, str]:
    root = (REPOSITORY_ROOT if repository_root is None else repository_root).resolve()

    def resolve_identity() -> tuple[str, str]:
        identity = run_command(
            _review_git_command("rev-parse", "HEAD", "HEAD^{tree}"),
            process_controller=process_controller,
            cwd=root,
            env=_review_git_environment(repository_root=root),
            timeout=15,
            check=False,
        )
        object_ids = tuple(identity.stdout.splitlines())
        if (
            identity.returncode != 0
            or len(object_ids) != 2
            or any(not _is_raw_git_object_id(object_id) for object_id in object_ids)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository identity is unavailable"
            )
        return object_ids[0], object_ids[1]

    def assert_clean_state(object_ids: tuple[str, str]) -> None:
        index_state = run_command(
            _review_git_command("ls-files", "-v", "-z"),
            process_controller=process_controller,
            cwd=root,
            env=_review_git_environment(repository_root=root),
            timeout=15,
            check=False,
        )
        if index_state.returncode != 0:
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository index state is unavailable"
            )
        if any(
            not record.startswith("H ")
            for record in index_state.stdout.split("\0")
            if record
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository has unsafe index state"
            )
        for tracked_state_command in (
            _review_git_diff_command(
                "diff-index",
                "--cached",
                object_ids[0],
                "--",
            ),
            _review_git_diff_command("diff-files", "--"),
        ):
            tracked_state = run_command(
                tracked_state_command,
                process_controller=process_controller,
                cwd=root,
                env=_review_git_environment(repository_root=root),
                timeout=15,
                check=False,
            )
            if tracked_state.returncode == 1:
                raise AV1ValidationDerivationError(
                    "AV1 derivation review repository has uncommitted tracked changes"
                )
            if tracked_state.returncode != 0:
                raise AV1ValidationDerivationError(
                    "AV1 derivation review repository state is unavailable"
                )
        repository_state = run_command(
            _review_git_command(
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ),
            process_controller=process_controller,
            cwd=root,
            env=_review_git_environment(repository_root=root),
            timeout=15,
            check=False,
        )
        if repository_state.returncode != 0:
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository state is unavailable"
            )
        if repository_state.stdout:
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository is not clean"
            )
        ignored_implementation_state = run_command(
            _review_git_command(
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--",
                "mediaforce",
                "scripts/verify_av1_cold_start_preregistration.py",
            ),
            process_controller=process_controller,
            cwd=root,
            env=_review_git_environment(repository_root=root),
            timeout=15,
            check=False,
        )
        if ignored_implementation_state.returncode != 0:
            raise AV1ValidationDerivationError(
                "AV1 derivation ignored implementation state is unavailable"
            )
        if ignored_implementation_state.stdout:
            raise AV1ValidationDerivationError(
                "AV1 derivation repository has ignored implementation artifacts"
            )

    object_ids = resolve_identity()
    assert_clean_state(object_ids)
    verified_object_ids = resolve_identity()
    if verified_object_ids != object_ids:
        raise AV1ValidationDerivationError(
            "AV1 derivation review repository identity changed during verification"
        )
    assert_clean_state(verified_object_ids)
    return verified_object_ids


def _live_repository_identity() -> tuple[str, str]:
    return _repository_review_identity(
        process_controller=ManagedProcessController(),
    )


def _review_git_environment(*, repository_root: Path) -> dict[str, str]:
    return {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_WORK_TREE": str(repository_root.resolve()),
        "LANG": "C",
        "LC_ALL": "C",
        "PAGER": "cat",
        "PATH": _AGENT_REVIEW_SAFE_PATH,
    }


def _review_runner_environment(*, working_directory: Path) -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": pwd.getpwuid(os.getuid()).pw_dir,
        "LANG": "C",
        "LC_ALL": "C",
        "LOGNAME": _AGENT_REVIEW_GENERIC_IDENTITY,
        "PATH": _AGENT_REVIEW_SAFE_PATH,
        "PWD": str(working_directory),
        "SHELL": "/bin/zsh",
        "TMPDIR": "/tmp",
        "USER": _AGENT_REVIEW_GENERIC_IDENTITY,
        "ZDOTDIR": "/var/empty",
    }


def _run_review_git(
        arguments: list[str],
        *,
        repository_root: Path,
        process_controller: ManagedProcessController,
        binary: bool = False,
) -> str | bytes:
    try:
        completed = run_command(
            _review_git_command(*arguments),
            process_controller=process_controller,
            cwd=repository_root,
            env=_review_git_environment(repository_root=repository_root),
            text=not binary,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git data is unavailable"
        ) from exc
    if completed.returncode != 0:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git data is unavailable"
        )
    output = completed.stdout
    if binary:
        if not isinstance(output, bytes):
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git blob data is invalid"
            )
        return output
    if not isinstance(output, str):
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git text data is invalid"
        )
    return output


def _review_git_commit_tree(
        commit: str,
        *,
        repository_root: Path,
        process_controller: ManagedProcessController,
) -> str:
    payload = _run_review_git(
        ["cat-file", "commit", commit],
        repository_root=repository_root,
        process_controller=process_controller,
        binary=True,
    )
    if not isinstance(payload, bytes):
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git commit data is invalid"
        )
    headers, separator, _message = payload.partition(b"\n\n")
    tree_entries = [
        header.removeprefix(b"tree ")
        for header in headers.splitlines()
        if header.startswith(b"tree ")
    ]
    if not separator or len(tree_entries) != 1:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git commit data is invalid"
        )
    try:
        tree = tree_entries[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git commit data is invalid"
        ) from exc
    if not _is_raw_git_object_id(tree):
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git commit data is invalid"
        )
    return tree


def _build_av1_validation_derivation_review_bundle(
        *,
        claim: AV1ValidationDerivationReviewClaim,
        process_controller: ManagedProcessController,
        repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    resolved_tree = _review_git_commit_tree(
        claim.repository_commit,
        repository_root=repository_root,
        process_controller=process_controller,
    )
    if resolved_tree != claim.repository_tree:
        raise AV1ValidationDerivationError(
            "AV1 derivation review bundle repository identity drifted"
        )
    allowed_paths = AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST.get(claim.lane)
    if allowed_paths is None:
        raise AV1ValidationDerivationError("AV1 derivation review bundle lane is invalid")
    files: list[dict[str, object]] = []
    total_size = 0
    for path in allowed_paths:
        listing = _run_review_git(
            [
                "ls-tree",
                "-z",
                "--full-tree",
                resolved_tree,
                "--",
                path,
            ],
            repository_root=repository_root,
            process_controller=process_controller,
            binary=True,
        )
        records = [record for record in listing.split(b"\0") if record]
        if len(records) != 1 or b"\t" not in records[0]:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle allowlisted path is unavailable"
            )
        metadata, encoded_path = records[0].split(b"\t", 1)
        try:
            mode, object_type, blob_id_bytes = metadata.split(b" ", 2)
            listed_path = encoded_path.decode("utf-8")
            blob_id = blob_id_bytes.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle tree entry is invalid"
            ) from exc
        if (
            listed_path != path
            or mode not in {b"100644", b"100755"}
            or object_type != b"blob"
            or not _is_raw_git_object_id(blob_id)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle tree entry is not an allowlisted blob"
            )
        raw_size = _run_review_git(
            ["cat-file", "-s", blob_id],
            repository_root=repository_root,
            process_controller=process_controller,
        )
        try:
            blob_size = int(raw_size.strip())
        except ValueError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle blob size is invalid"
            ) from exc
        if (
            blob_size < 0
            or blob_size > AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BLOB_BYTES
            or total_size + blob_size
            > AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BUNDLE_BYTES
        ):
            raise AV1ValidationDerivationError("AV1 derivation review bundle is oversized")
        blob = _run_review_git(
            ["cat-file", "blob", blob_id],
            repository_root=repository_root,
            process_controller=process_controller,
            binary=True,
        )
        if len(blob) != blob_size:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle blob size drifted"
            )
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle blob is not UTF-8"
            ) from exc
        files.append({
            "path": path,
            "blob_id": blob_id,
            "blob_size_bytes": blob_size,
            "blob_sha256": f"sha256:{hashlib.sha256(blob).hexdigest()}",
            "text": text,
        })
        total_size += blob_size
    semantic_payload = {
        "schema": AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA_VERSION,
        "lane": claim.lane,
        "repository_commit": claim.repository_commit,
        "repository_tree": claim.repository_tree,
        "files": files,
    }
    return {
        **semantic_payload,
        "payload_sha256": (
            f"sha256:{hashlib.sha256(canonical_json_bytes(semantic_payload)).hexdigest()}"
        ),
    }


def _write_review_request(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise AV1ValidationDerivationError(
                "AV1 derivation review request file could not be written"
            )
        remaining = remaining[written:]


@contextmanager
def _owner_only_review_request_file(request: bytes) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="mediaforce-av1-review-request-", dir="/tmp"))
    path = directory / "request.json"
    descriptor = -1
    directory_identity: tuple[int, int] | None = None
    file_identity: tuple[int, int] | None = None
    try:
        os.chmod(directory, 0o700)
        directory_info = directory.lstat()
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or stat.S_IMODE(directory_info.st_mode) != 0o700
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review request directory is not owner-only"
            )
        directory_identity = (directory_info.st_dev, directory_info.st_ino)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        file_info = os.fstat(descriptor)
        file_identity = (file_info.st_dev, file_info.st_ino)
        _write_review_request(descriptor, request)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        file_info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_info.st_mode)
            or file_info.st_uid != os.getuid()
            or stat.S_IMODE(file_info.st_mode) != 0o400
            or file_info.st_nlink != 1
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review request file is not owner-only"
            )
        os.close(descriptor)
        descriptor = -1
        yield path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_identity is None:
            return
        try:
            directory_info = directory.lstat()
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review request cleanup identity is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or stat.S_IMODE(directory_info.st_mode) != 0o700
            or (directory_info.st_dev, directory_info.st_ino) != directory_identity
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review request cleanup identity changed"
            )
        try:
            if file_identity is not None:
                file_info = path.lstat()
                if (
                    not stat.S_ISREG(file_info.st_mode)
                    or stat.S_ISLNK(file_info.st_mode)
                    or (file_info.st_dev, file_info.st_ino) != file_identity
                    or file_info.st_uid != os.getuid()
                    or stat.S_IMODE(file_info.st_mode) != 0o400
                    or file_info.st_nlink != 1
                ):
                    raise AV1ValidationDerivationError(
                        "AV1 derivation review request cleanup identity changed"
                    )
                path.unlink()
            elif path.exists() or path.is_symlink():
                raise AV1ValidationDerivationError(
                    "AV1 derivation review request cleanup identity is unavailable"
                )
            directory.rmdir()
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review request cleanup failed"
            ) from exc


def _structured_review_response(
        stdout: str,
        *,
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
) -> tuple[dict[str, object], AV1ValidationDerivationReviewDecision]:
    response_text = stdout.strip()
    if not response_text:
        raise AV1ValidationDerivationError(
            "AV1 derivation structured review did not return one JSON response"
        )
    try:
        response = json.loads(
            response_text,
            object_pairs_hook=_review_response_json_object,
        )
        response_payload = dict(response)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation structured review response is invalid"
        ) from exc
    decision = validate_av1_validation_derivation_review_response(
        response_payload,
        proposal=proposal,
        claim=claim,
    )
    return response_payload, decision


def _review_response_json_object(
        pairs: list[tuple[str, object]],
) -> dict[str, object]:
    response: dict[str, object] = {}
    for key, value in pairs:
        if key in response:
            raise AV1ValidationDerivationError(
                "AV1 derivation structured review response has duplicate JSON keys"
            )
        response[key] = value
    return response


def _assert_native_review_runner(binary_bytes: bytes) -> None:
    if binary_bytes[:4] not in _MACH_O_MAGICS:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable must be a native Mach-O binary"
        )


def _trusted_code_ancestor_path() -> Path:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation active Every Code runner identity is unavailable"
        ) from exc
    process_id = os.getppid()
    for _ in range(16):
        buffer = ctypes.create_string_buffer(4096)
        if proc_pidpath(process_id, buffer, len(buffer)) > 0:
            candidate = Path(buffer.value.decode("utf-8")).resolve()
            if candidate.name == "code":
                return candidate
        parent = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", str(process_id)],
            text=True,
            capture_output=True,
            check=False,
            env={"PATH": _AGENT_REVIEW_SAFE_PATH},
        )
        try:
            process_id = int(parent.stdout.strip())
        except ValueError:
            break
        if process_id <= 1:
            break
    raise AV1ValidationDerivationError(
        "AV1 derivation must run from the active trusted Every Code session"
    )


def _load_existing_derivation_review(
        *,
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: AV1ValidationDerivationReviewLane,
) -> tuple[
    AV1ValidationDerivationReviewClaim,
    AV1ValidationDerivationReviewEnvelope,
] | None:
    claim_path = (
        artifact_root
        / "review-claims"
        / proposal.proposal_id
        / f"{lane}.json"
    )
    envelope_path = (
        artifact_root
        / "reviews"
        / proposal.proposal_id
        / f"{lane}.json"
    )
    claim_exists = claim_path.exists() or claim_path.is_symlink()
    envelope_exists = envelope_path.exists() or envelope_path.is_symlink()
    if not claim_exists:
        if envelope_exists:
            raise AV1ValidationDerivationError(
                "AV1 derivation review envelope has no immutable lane claim"
            )
        return None
    claims = load_av1_validation_derivation_review_claims(
        artifact_root,
        plan=plan,
        proposal=proposal,
    )
    claim = next((item for item in claims if item.lane == lane), None)
    if claim is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation review claim directory lost its requested lane"
        )
    if not envelope_exists:
        raise AV1ValidationDerivationError(
            "AV1 derivation interrupted review claim is terminal and cannot be resumed"
        )
    envelope = load_av1_validation_derivation_review_envelope(
        artifact_root,
        plan=plan,
        proposal=proposal,
        claim=claim,
    )
    return claim, envelope


def _assert_code_llm_request_contract(
        code_binary: Path,
        *,
        process_controller: ManagedProcessController,
) -> None:
    working_directory = Path("/tmp").resolve()
    try:
        completed = run_command(
            [str(code_binary), "llm", "request", "--help"],
            process_controller=process_controller,
            cwd=working_directory,
            env=_review_runner_environment(
                working_directory=working_directory,
            ),
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code LLM request contract is unavailable"
        ) from exc
    required_options = (
        "--developer",
        "--message-file",
        "--format-name",
        "--format-strict",
        "--schema-json",
    )
    if (
        completed.returncode != 0
        or any(option not in completed.stdout for option in required_options)
        or "--request-file" in completed.stdout
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code LLM request contract is incompatible"
        )


def _assert_no_tool_llm_review_command(command: Sequence[str]) -> None:
    required_options = {
        "--developer",
        "--format-name",
        "--format-strict",
        "--message-file",
        "--schema-json",
    }
    disallowed_arguments = {
        "exec",
        "--auto",
        "--json",
        "--message",
        "--request-file",
        "--sandbox",
        "--schema",
        "-s",
    }
    if (
        len(command) < 3
        or tuple(command[1:3]) != ("llm", "request")
        or not required_options.issubset(command)
        or any(argument in disallowed_arguments for argument in command[1:])
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review must use Every Code LLM request with no tools"
        )


def _run_code_llm_review(
        *,
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: AV1ValidationDerivationReviewLane,
        before_publish: Callable[[], None] | None = None,
) -> tuple[
    AV1ValidationDerivationReviewClaim,
    bytes,
    AV1ValidationDerivationReviewDecision,
]:
    try:
        deadline = datetime.fromisoformat(
            plan.authorization.valid_until.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review authorization deadline is invalid"
        ) from exc
    if deadline.tzinfo is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation review authorization deadline must include a UTC offset"
        )
    process_controller = ManagedProcessController()
    try:
        with process_controller.absolute_deadline(deadline.astimezone(UTC)):
            return _run_code_llm_review_before_deadline(
                artifact_root=artifact_root,
                plan=plan,
                proposal=proposal,
                lane=lane,
                process_controller=process_controller,
                before_publish=before_publish,
            )
    except (ProcessCancelledError, ProcessDeadlineEnforcementError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code review exceeded its authorization deadline"
        ) from exc


def _run_code_llm_review_before_deadline(
        *,
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: AV1ValidationDerivationReviewLane,
        process_controller: ManagedProcessController,
        before_publish: Callable[[], None] | None = None,
) -> tuple[
    AV1ValidationDerivationReviewClaim,
    bytes,
    AV1ValidationDerivationReviewDecision,
]:
    assert_av1_validation_derivation_execution_environment(
        plan,
        process_controller=process_controller,
    )
    before_identity = _authorized_review_runner_identity(plan)
    _assert_code_llm_request_contract(
        before_identity[0],
        process_controller=process_controller,
    )
    before_repository_identity = _repository_review_identity(
        process_controller=process_controller,
    )
    review_run_id = str(uuid.uuid4())
    claim = build_av1_validation_derivation_review_claim(
        plan=plan,
        proposal=proposal,
        repository_commit=before_repository_identity[0],
        repository_tree=before_repository_identity[1],
        lane=lane,
        review_run_id=review_run_id,
        review_runner_canonical_path_sha256=before_identity[1],
        review_runner_binary_sha256=before_identity[2],
        claimed_at=_now_iso(),
    )
    write_av1_validation_derivation_review_claim(
        artifact_root,
        plan=plan,
        proposal=proposal,
        claim=claim,
        before_publish=before_publish,
    )
    safe_bundle = _build_av1_validation_derivation_review_bundle(
        claim=claim,
        process_controller=process_controller,
    )
    request = build_av1_validation_derivation_review_request(
        proposal=proposal,
        claim=claim,
        safe_bundle=safe_bundle,
    )
    developer_text = av1_validation_derivation_review_developer_text(claim.lane)
    response_schema = build_av1_validation_derivation_review_response_schema(
        proposal=proposal,
        claim=claim,
    )
    request_bytes = canonical_json_bytes(request)
    response_schema_text = canonical_json_bytes(response_schema).decode("utf-8")
    try:
        with _owner_only_review_request_file(request_bytes) as request_path:
            command = [
                str(before_identity[0]),
                "llm",
                "request",
                "--developer",
                developer_text,
                "--message-file",
                str(request_path),
                "--format-type",
                "json_schema",
                "--format-name",
                AV1_VALIDATION_DERIVATION_REVIEW_RESPONSE_SCHEMA_NAME,
                "--format-strict",
                "--schema-json",
                response_schema_text,
            ]
            _assert_no_tool_llm_review_command(command)
            try:
                completed = run_command(
                    command,
                    process_controller=process_controller,
                    cwd=request_path.parent,
                    env=_review_runner_environment(
                        working_directory=request_path.parent,
                    ),
                    timeout=_AGENT_REVIEW_MAX_SECONDS + 30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AV1ValidationDerivationError(
                    "AV1 derivation Every Code structured review did not complete"
                ) from exc
    finally:
        integrity_errors: list[BaseException] = []
        try:
            after_repository_identity = _repository_review_identity(
                process_controller=process_controller,
            )
            if after_repository_identity != before_repository_identity:
                raise AV1ValidationDerivationError(
                    "AV1 derivation review repository changed during review"
                )
        except BaseException as exc:
            integrity_errors.append(exc)
        try:
            after_identity = _authorized_review_runner_identity(plan)
            if after_identity != before_identity:
                raise AV1ValidationDerivationError(
                    "AV1 derivation Every Code executable changed during review"
                )
        except BaseException as exc:
            integrity_errors.append(exc)
        if integrity_errors:
            primary_error = integrity_errors[0]
            for error in integrity_errors[1:]:
                primary_error.add_note(
                    "AV1 review integrity check also failed: "
                    f"{type(error).__name__}: {error}"
                )
            raise primary_error
    if completed.returncode != 0:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code structured review failed"
        )
    if len(completed.stdout.encode("utf-8")) > _LLM_REVIEW_MAXIMUM_RESPONSE_BYTES:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code structured review response is oversized"
        )
    model_response, decision = _structured_review_response(
        completed.stdout,
        proposal=proposal,
        claim=claim,
    )
    evidence = canonical_json_bytes({
        "schema": AV1_VALIDATION_DERIVATION_STRUCTURED_REVIEW_RUN_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA_VERSION,
        "review_run_id": review_run_id,
        "reviewer_token": f"agent:{review_run_id}",
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "review_claim_id": claim.claim_id,
        "review_claim_payload_sha256": claim.payload_sha256,
        "lane": claim.lane,
        "decision": decision,
        "repository_commit": claim.repository_commit,
        "repository_tree": claim.repository_tree,
        "review_runner_canonical_path_sha256": before_identity[1],
        "review_runner_binary_sha256": before_identity[2],
        "proposal": proposal.to_payload(),
        "review_claim": claim.to_payload(),
        "safe_bundle": safe_bundle,
        "request": request,
        "request_sha256": f"sha256:{hashlib.sha256(request_bytes).hexdigest()}",
        "developer_text": developer_text,
        "developer_text_sha256": (
            f"sha256:{hashlib.sha256(developer_text.encode('utf-8')).hexdigest()}"
        ),
        "response_schema": response_schema,
        "response_schema_sha256": (
            f"sha256:{hashlib.sha256(canonical_json_bytes(response_schema)).hexdigest()}"
        ),
        "returncode": completed.returncode,
        "stderr_sha256": (
            f"sha256:{hashlib.sha256(completed.stderr.encode('utf-8')).hexdigest()}"
        ),
        "model_response": model_response,
        "model_response_sha256": (
            f"sha256:{hashlib.sha256(canonical_json_bytes(model_response)).hexdigest()}"
        ),
        "analysis_sha256": av1_validation_derivation_review_analysis_sha256(
            model_response
        ),
    })
    return claim, evidence, decision


def _print_partition_payload(payload: dict[str, object], *, json_output: bool) -> None:
    assert_av1_cold_start_public_payload_safe(payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    fields = [
        f"{key}={str(value).lower() if isinstance(value, bool) else value}"
        for key, value in payload.items()
    ]
    print(" ".join(fields))


def _load_manifest(path: Path) -> ValidationManifest:
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise AV1ColdStartValidationError(
            "AV1 validation manifest must be a JSON object"
        )
    schema_version = payload.get("schema_version")
    if schema_version == 1:
        return load_av1_cold_start_validation_manifest(path)
    if schema_version == 2:
        return load_av1_validation_manifest_v2(path)
    raise AV1ColdStartValidationError(
        "AV1 validation manifest schema version is unsupported"
    )


def _validation_payload(manifest: ValidationManifest) -> dict[str, object]:
    if isinstance(manifest, AV1ValidationManifestV2):
        return {
            "manifest_id": manifest.manifest_id,
            "schema_version": 2,
            "state": "preregistered_derivation_only",
            "authority": "derivation_only",
            "cell_plan_count": len(manifest.cell_plans),
            "candidate_cell_count": sum(
                plan.mode == "publication_candidate" for plan in manifest.cell_plans
            ),
            "fallback_cell_count": sum(
                plan.mode == "fallback_conformance" for plan in manifest.cell_plans
            ),
            "excluded_cell_count": len(manifest.excluded_cells),
            "registered_case_count": len(manifest.cases),
            "runtime_execution_authorized": False,
            "holdout_execution_authorized": False,
        }
    return {
        "manifest_id": manifest.manifest_id,
        "schema_version": 1,
        "state": manifest.state,
        "cell_plan_count": len(manifest.cell_plans),
        "registered_case_count": len(manifest.cases),
        "runtime_execution_authorized": False,
        "holdout_execution_authorized": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
