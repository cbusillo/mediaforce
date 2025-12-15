from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Optional


RequestJsonFn = Callable[[str, str, Optional[dict[str, Any]]], dict[str, Any]]


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
        payload: Optional[dict[str, Any]],
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
    ) -> WorkerClaimResult:
        payload: dict[str, Any] = {"machine": machine, "available": bool(available)}
        if sample_path:
            payload["sample_path"] = str(sample_path)
        data = self._request_json("POST", "/api/worker/claim", payload)
        if not data.get("success"):
            raise WorkerApiError(data.get("error") or "claim failed")

        control = data.get("control") or {}
        mode = str(control.get("mode") or "run")
        stop_now = bool(control.get("stop_now") or False)

        claimed = data.get("claimed")
        if not claimed:
            return WorkerClaimResult(claim=None, control_mode=mode, stop_now=stop_now)

        claim = WorkerClaim(
            id=int(claimed["id"]),
            path=str(claimed["path"]),
            detected_tier=claimed.get("detected_tier"),
            bitrate_kbps=claimed.get("bitrate_kbps"),
            duration_sec=claimed.get("duration_sec"),
            library_id=claimed.get("library_id"),
            show_name=data.get("show_name"),
            override_tier=data.get("override_tier"),
        )

        return WorkerClaimResult(claim=claim, control_mode=mode, stop_now=stop_now)

    def control(self, *, machine: str) -> WorkerClaimResult:
        machine_q = urllib.parse.quote(str(machine), safe="")
        data = self._request_json("GET", f"/api/worker/control?machine={machine_q}", None)
        if not data.get("success"):
            raise WorkerApiError(data.get("error") or "control failed")
        control = data.get("control") or {}
        mode = str(control.get("mode") or "run")
        stop_now = bool(control.get("stop_now") or False)
        return WorkerClaimResult(claim=None, control_mode=mode, stop_now=stop_now)

    def ack(self, *, machine: str, action: str = "stop_now") -> None:
        data = self._request_json(
            "POST",
            "/api/worker/control/ack",
            {"machine": machine, "action": action},
        )
        if not data.get("success"):
            raise WorkerApiError(data.get("error") or "ack failed")

    def release(self, *, machine: str, source_id: int, success: bool, error: Optional[str] = None) -> None:
        data = self._request_json(
            "POST",
            "/api/worker/release",
            {"machine": machine, "id": source_id, "success": bool(success), "error": error},
        )
        if not data.get("success"):
            raise WorkerApiError(data.get("error") or "release failed")

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
        payload: dict[str, Any] = {
            "source_id": source_id,
            "source_path": source_path,
            "output_path": output_path,
            "machine": machine,
            "tier": tier,
            "duration_sec": duration_sec,
        }
        if total_frames is not None:
            payload["total_frames"] = total_frames
        data = self._request_json("POST", "/api/worker/progress/start", payload)
        if not data.get("success"):
            raise WorkerApiError(data.get("error") or "progress start failed")
        return int(data["progress_id"])

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
        payload: dict[str, Any] = {
            "progress_id": progress_id,
            "frame": frame,
            "fps": fps,
            "speed": speed,
            "size_bytes": size_bytes,
            "time_encoded_sec": time_encoded_sec,
        }
        if bitrate_kbps is not None:
            payload["bitrate_kbps"] = bitrate_kbps
        if duration_sec is not None:
            payload["duration_sec"] = duration_sec
        if phase is not None:
            payload["phase"] = phase
        if phase_detail is not None:
            payload["phase_detail"] = phase_detail

        data = self._request_json("POST", "/api/worker/progress/update", payload)
        if not data.get("success"):
            raise WorkerApiError(data.get("error") or "progress update failed")

    def report_encode_result(self, *, payload: dict[str, Any]) -> int:
        data = self._request_json("POST", "/api/worker/report", payload)
        if not data.get("success"):
            raise WorkerApiError(data.get("error") or "report failed")
        return int(data.get("encode_result_id") or 0)

    def evaluation_start(
        self,
        *,
        media_id: int,
        initial_profile: str,
        sample_length: float,
    ) -> tuple[int, dict[str, Any]]:
        data = self._request_json(
            "POST",
            "/api/evaluations/start",
            {
                "media_id": int(media_id),
                "initial_profile": str(initial_profile),
                "sample_length": float(sample_length),
            },
        )
        if not data.get("success"):
            raise WorkerApiError(data.get("error") or "evaluation start failed")
        return int(data.get("evaluation_id") or 0), dict(data.get("thresholds") or {})

    def evaluation_submit_samples(
        self,
        *,
        evaluation_id: int,
        samples: list[dict[str, Any]],
        target_height: Optional[int] = None,
        target_height_reason: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"samples": samples}
        if target_height is not None:
            payload["target_height"] = int(target_height)
        if target_height_reason is not None:
            payload["target_height_reason"] = str(target_height_reason)

        data = self._request_json("POST", f"/api/evaluations/{int(evaluation_id)}/samples", payload)
        if not data.get("success"):
            raise WorkerApiError(data.get("error") or "evaluation submit samples failed")
        return data
