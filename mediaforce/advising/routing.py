from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import int_value, object_dict, object_list


class AdvisorTask(StrEnum):
    SEED_POLICY = "seed_policy"
    NOTE_TUNING = "note_tuning"
    REVIEW_ARTIFACT_CRITIQUE = "review_artifact_critique"
    OPERATOR_NOTE_PARSE = "operator_note_parse"


DEFAULT_ADVISOR_MODELS: Mapping[AdvisorTask, tuple[str, ...]] = {
    AdvisorTask.SEED_POLICY: ("gpt-5.6-terra", "gpt-5.6-sol"),
    AdvisorTask.NOTE_TUNING: ("gpt-5.6-terra", "gpt-5.6-sol"),
    AdvisorTask.REVIEW_ARTIFACT_CRITIQUE: ("gpt-5.6-terra", "gpt-5.6-sol"),
    AdvisorTask.OPERATOR_NOTE_PARSE: ("gpt-5.6-luna", "gpt-5.6-terra"),
}


@dataclass(frozen=True, slots=True)
class AdvisorModelPricing:
    input_usd_per_million: float | None = None
    cached_input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None


@dataclass(frozen=True, slots=True)
class AdvisorRoute:
    task: AdvisorTask
    models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdvisorRouting:
    command: str
    routes: Mapping[AdvisorTask, AdvisorRoute]
    auth_profile: str | None = None
    telemetry_path: Path | None = None
    telemetry_max_records: int = 5000
    model_pricing: Mapping[str, AdvisorModelPricing] | None = None

    def route_for(self, task: AdvisorTask) -> AdvisorRoute:
        route = self.routes.get(task)
        if route is not None and route.models:
            return route
        return AdvisorRoute(task=task, models=DEFAULT_ADVISOR_MODELS[task])

    def pricing_for(self, model: str) -> AdvisorModelPricing | None:
        return (self.model_pricing or {}).get(model)


def default_advisor_routing(*, telemetry_path: Path | None = None) -> AdvisorRouting:
    return AdvisorRouting(
        command="codex-lab",
        routes={
            task: AdvisorRoute(task=task, models=models)
            for task, models in DEFAULT_ADVISOR_MODELS.items()
        },
        telemetry_path=telemetry_path,
    )


def advisor_routing_from_config(config: MediaforceConfig) -> AdvisorRouting:
    raw = object_dict(config.raw.get("advisor"))
    command = str(raw.get("command") or "codex-lab").strip() or "codex-lab"
    route_payloads = object_dict(raw.get("routes"))
    routes: dict[AdvisorTask, AdvisorRoute] = {}
    for task, default_models in DEFAULT_ADVISOR_MODELS.items():
        route_payload = object_dict(route_payloads.get(task.value))
        models = _model_list(route_payload.get("models")) or default_models
        routes[task] = AdvisorRoute(task=task, models=models)
    telemetry_max_records = max(100, int_value(raw.get("telemetry_max_records")) or 5000)
    return AdvisorRouting(
        command=command,
        routes=routes,
        auth_profile=str(raw.get("auth_profile") or "").strip() or None,
        telemetry_path=config.paths.web_state_dir / "advisor-routing.jsonl",
        telemetry_max_records=telemetry_max_records,
        model_pricing=_model_pricing(raw.get("model_pricing")),
    )


def advisor_routing_for_models(
        task_models: Mapping[AdvisorTask, tuple[str, ...]],
        *,
        command: str = "codex-lab",
        auth_profile: str | None = None,
        telemetry_path: Path | None = None,
        model_pricing: Mapping[str, AdvisorModelPricing] | None = None,
) -> AdvisorRouting:
    routes = {
        task: AdvisorRoute(task=task, models=task_models.get(task) or default_models)
        for task, default_models in DEFAULT_ADVISOR_MODELS.items()
    }
    return AdvisorRouting(
        command=command,
        routes=routes,
        auth_profile=auth_profile,
        telemetry_path=telemetry_path,
        model_pricing=model_pricing,
    )


def _model_list(value: Any) -> tuple[str, ...]:
    values = object_list(value) if isinstance(value, list) else [value] if isinstance(value, str) else []
    models: list[str] = []
    for item in values:
        model = str(item or "").strip()
        if model and model not in models:
            models.append(model)
    return tuple(models)


def _model_pricing(value: Any) -> dict[str, AdvisorModelPricing]:
    pricing: dict[str, AdvisorModelPricing] = {}
    for model, raw_payload in object_dict(value).items():
        payload = object_dict(raw_payload)
        pricing[str(model)] = AdvisorModelPricing(
            input_usd_per_million=_optional_nonnegative_float(payload.get("input_usd_per_million")),
            cached_input_usd_per_million=_optional_nonnegative_float(
                payload.get("cached_input_usd_per_million")
            ),
            output_usd_per_million=_optional_nonnegative_float(payload.get("output_usd_per_million")),
        )
    return pricing


def _optional_nonnegative_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number >= 0 else None
