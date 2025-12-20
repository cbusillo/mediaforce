import json
import os
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from pydantic import BaseModel, Field


RequestJsonFn = Callable[[str, str, Optional[Mapping[str, Any]]], dict[str, Any]]


class WorkerControlPayload(BaseModel):
    mode: str = "run"
    stop_now: bool = False


class WorkerClaimPayload(BaseModel):
    id: int
    path: str
    detected_tier: Optional[str] = None
    bitrate_kbps: Optional[int] = None
    duration_sec: Optional[float] = None
    library_id: Optional[str] = None


class WorkerClaimResponse(BaseModel):
    success: bool = True
    error: Optional[str] = None
    control: WorkerControlPayload = Field(default_factory=WorkerControlPayload)
    claimed: Optional[WorkerClaimPayload] = None
    show_name: Optional[str] = None
    override_tier: Optional[str] = None


class WorkerControlResponse(BaseModel):
    success: bool = True
    error: Optional[str] = None
    control: WorkerControlPayload = Field(default_factory=WorkerControlPayload)


class SuccessResponse(BaseModel):
    success: bool = True
    error: Optional[str] = None


class ProgressStartResponse(BaseModel):
    success: bool = True
    error: Optional[str] = None
    progress_id: int = 0


class EncodeReportResponse(BaseModel):
    success: bool = True
    error: Optional[str] = None
    encode_result_id: int = 0


class EvaluationThresholdsPayload(BaseModel):
    min: Optional[float] = None
    median: Optional[float] = None
    max: Optional[float] = None


class EvaluationStartResponse(BaseModel):
    success: bool = True
    error: Optional[str] = None
    evaluation_id: int = 0
    thresholds: EvaluationThresholdsPayload = Field(default_factory=EvaluationThresholdsPayload)


class WorkerClaimRequestPayload(BaseModel):
    machine: str
    available: bool = True
    sample_path: Optional[str] = None
    status_message: Optional[str] = None


class WorkerControlAckPayload(BaseModel):
    machine: str
    action: str


class WorkerReleasePayload(BaseModel):
    machine: str
    id: int
    success: bool
    error: Optional[str] = None


class WorkerProgressStartPayload(BaseModel):
    source_id: int
    source_path: str
    output_path: str
    machine: str
    tier: str
    duration_sec: float
    total_frames: Optional[int] = None


class WorkerProgressUpdatePayload(BaseModel):
    progress_id: int
    frame: int = 0
    fps: float = 0.0
    speed: float = 0.0
    size_bytes: int = 0
    time_encoded_sec: float = 0.0
    bitrate_kbps: Optional[float] = None
    duration_sec: Optional[float] = None
    phase: Optional[str] = None
    phase_detail: Optional[str] = None


class EvaluationSubmitSamplesPayload(BaseModel):
    samples: list[Mapping[str, Any]]
    target_height: Optional[int] = None
    target_height_reason: Optional[str] = None


class EvaluationSubmitSummaryPayload(BaseModel):
    weighted: Optional[float] = None
    median: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None


class EvaluationSubmitResponse(BaseModel):
    success: bool = True
    error: Optional[str] = None
    selected_profile: Optional[str] = None
    initial_profile: Optional[str] = None
    decision: Optional[str] = None
    status: Optional[str] = None
    note: Optional[str] = None
    summary: Optional[EvaluationSubmitSummaryPayload] = None


class WorkerMetricsPayload(BaseModel):
    ssim: Optional[float] = None
    psnr: Optional[float] = None
    vmaf: Optional[float] = None
    sample_duration_sec: Optional[float] = None
    sample_start_sec: Optional[float] = None


class WorkerOutlierPayload(BaseModel):
    is_outlier: bool
    reasons: list[str] = Field(default_factory=list)


class WorkerEncodeReportPayload(BaseModel):
    source_id: int
    source_path: str
    tier: str
    crf: int
    preset: int
    film_grain: int
    denoise: Optional[str] = None
    output_path: str
    output_size_bytes: int
    output_bitrate_kbps: Optional[int] = None
    source_size_bytes: int
    machine: str
    started_at: str
    success: bool
    error_message: Optional[str] = None
    metrics: Optional[WorkerMetricsPayload] = None
    outlier: Optional[WorkerOutlierPayload] = None
    profile_eval_id: Optional[int] = None
    progress_id: Optional[int] = None


@dataclass
class WorkerClaim:
    id: int
    path: str
    detected_tier: Optional[str] = None
    bitrate_kbps: Optional[int] = None
    duration_sec: Optional[float] = None
    library_id: Optional[str] = None
    show_name: Optional[str] = None
    override_tier: Optional[str] = None


@dataclass
class WorkerClaimResult:
    claim: Optional[WorkerClaim]
    control_mode: str = "run"
    stop_now: bool = False


class WorkerApiError(RuntimeError):
    pass


class WorkerApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_sec: float = 10.0,
        token: Optional[str] = None,
        request_json: Optional[RequestJsonFn] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self.token = token if token is not None else os.getenv("MEDIAFORCE_API_TOKEN")
        self._request_json = request_json or self._request_json_urllib

    def _request_json_urllib(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data_bytes = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")
            except Exception:
                body = ""
            raise WorkerApiError(f"HTTP {e.code} for {url}: {body}") from e
        except urllib.error.URLError as e:
            raise WorkerApiError(f"Request failed for {url}: {e}") from e
        except Exception as e:
            raise WorkerApiError(f"Request failed for {url}: {e}") from e

    def claim(
        self,
        *,
        machine: str,
        available: bool = True,
        sample_path: Optional[str] = None,
        status_message: Optional[str] = None,
    ) -> WorkerClaimResult:
        payload = WorkerClaimRequestPayload(
            machine=machine,
            available=bool(available),
            sample_path=str(sample_path) if sample_path else None,
            status_message=str(status_message) if status_message else None,
        )
        response = WorkerClaimResponse.model_validate(
            self._request_json("POST", "/api/worker/claim", payload.model_dump(exclude_none=True))
        )
        if not response.success:
            raise WorkerApiError(response.error or "claim failed")

        mode = str(response.control.mode or "run")
        stop_now = bool(response.control.stop_now)
        if response.claimed is None:
            return WorkerClaimResult(claim=None, control_mode=mode, stop_now=stop_now)

        claim = WorkerClaim(
            id=int(response.claimed.id),
            path=str(response.claimed.path),
            detected_tier=response.claimed.detected_tier,
            bitrate_kbps=response.claimed.bitrate_kbps,
            duration_sec=response.claimed.duration_sec,
            library_id=response.claimed.library_id,
            show_name=response.show_name,
            override_tier=response.override_tier,
        )

        return WorkerClaimResult(claim=claim, control_mode=mode, stop_now=stop_now)

    def control(self, *, machine: str) -> WorkerClaimResult:
        machine_q = urllib.parse.quote(str(machine), safe="")
        response = WorkerControlResponse.model_validate(
            self._request_json("GET", f"/api/worker/control?machine={machine_q}", None)
        )
        if not response.success:
            raise WorkerApiError(response.error or "control failed")
        mode = str(response.control.mode or "run")
        stop_now = bool(response.control.stop_now)
        return WorkerClaimResult(claim=None, control_mode=mode, stop_now=stop_now)

    def ack(self, *, machine: str, action: str = "stop_now") -> None:
        payload = WorkerControlAckPayload(machine=machine, action=action)
        response = SuccessResponse.model_validate(
            self._request_json(
                "POST",
                "/api/worker/control/ack",
                payload.model_dump(exclude_none=True),
            )
        )
        if not response.success:
            raise WorkerApiError(response.error or "ack failed")

    def release(self, *, machine: str, source_id: int, success: bool, error: Optional[str] = None) -> None:
        payload = WorkerReleasePayload(
            machine=machine,
            id=source_id,
            success=bool(success),
            error=error,
        )
        response = SuccessResponse.model_validate(
            self._request_json(
                "POST",
                "/api/worker/release",
                payload.model_dump(exclude_none=True),
            )
        )
        if not response.success:
            raise WorkerApiError(response.error or "release failed")

    def progress_start(
        self,
        *,
        source_id: int,
        source_path: str,
        output_path: str,
        machine: str,
        tier: str,
        duration_sec: float,
        total_frames: Optional[int] = None,
    ) -> int:
        payload = WorkerProgressStartPayload(
            source_id=source_id,
            source_path=source_path,
            output_path=output_path,
            machine=machine,
            tier=tier,
            duration_sec=duration_sec,
            total_frames=total_frames,
        )
        response = ProgressStartResponse.model_validate(
            self._request_json(
                "POST",
                "/api/worker/progress/start",
                payload.model_dump(exclude_none=True),
            )
        )
        if not response.success:
            raise WorkerApiError(response.error or "progress start failed")
        return int(response.progress_id)

    def progress_update(
        self,
        *,
        progress_id: int,
        frame: int = 0,
        fps: float = 0.0,
        speed: float = 0.0,
        bitrate_kbps: Optional[float] = None,
        size_bytes: int = 0,
        time_encoded_sec: float = 0.0,
        duration_sec: Optional[float] = None,
        phase: Optional[str] = None,
        phase_detail: Optional[str] = None,
    ) -> None:
        payload = WorkerProgressUpdatePayload(
            progress_id=progress_id,
            frame=frame,
            fps=fps,
            speed=speed,
            size_bytes=size_bytes,
            time_encoded_sec=time_encoded_sec,
            bitrate_kbps=bitrate_kbps,
            duration_sec=duration_sec,
            phase=phase,
            phase_detail=phase_detail,
        )

        response = SuccessResponse.model_validate(
            self._request_json(
                "POST",
                "/api/worker/progress/update",
                payload.model_dump(exclude_none=True),
            )
        )
        if not response.success:
            raise WorkerApiError(response.error or "progress update failed")

    def report_encode_result(self, *, payload: WorkerEncodeReportPayload) -> int:
        response = EncodeReportResponse.model_validate(
            self._request_json(
                "POST",
                "/api/worker/report",
                payload.model_dump(exclude_none=True),
            )
        )
        if not response.success:
            raise WorkerApiError(response.error or "report failed")
        return int(response.encode_result_id)

    def evaluation_start(
        self,
        *,
        media_id: int,
        initial_profile: str,
        sample_length: float,
    ) -> tuple[int, EvaluationThresholdsPayload]:
        response = EvaluationStartResponse.model_validate(
            self._request_json(
            "POST",
            "/api/evaluations/start",
            {
                "media_id": int(media_id),
                "initial_profile": str(initial_profile),
                "sample_length": float(sample_length),
            },
            )
        )
        if not response.success:
            raise WorkerApiError(response.error or "evaluation start failed")
        return int(response.evaluation_id), response.thresholds

    def evaluation_submit_samples(
        self,
        *,
        evaluation_id: int,
        samples: Sequence[Mapping[str, Any]],
        target_height: Optional[int] = None,
        target_height_reason: Optional[str] = None,
    ) -> EvaluationSubmitResponse:
        payload = EvaluationSubmitSamplesPayload(
            samples=samples,
            target_height=int(target_height) if target_height is not None else None,
            target_height_reason=str(target_height_reason) if target_height_reason is not None else None,
        )

        data = self._request_json(
            "POST",
            f"/api/evaluations/{int(evaluation_id)}/samples",
            payload.model_dump(exclude_none=True),
        )
        response = EvaluationSubmitResponse.model_validate(data)
        if not response.success:
            raise WorkerApiError(response.error or "evaluation submit samples failed")
        return response
