from collections.abc import Callable, Collection
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

def _request_flag(body: object, key: str) -> bool:
    return isinstance(body, dict) and body.get(key) is True


def _request_item_ids(body: dict[str, Any]) -> list[int]:
    """The ``library_item_ids`` a per-file decision names: a non-empty list of positive ids."""
    item_ids = body.get("library_item_ids")
    if (not isinstance(item_ids, list) or not item_ids
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in item_ids)):
        raise HTTPException(status_code=400, detail="Name the files with library_item_ids.")
    return item_ids


async def request_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def register_folder_routes(
        app: FastAPI,
        *,
        save_series_lifecycle_action: Callable[[str, str], dict[str, Any]],
        save_audio_preset_action: Callable[[str, str], dict[str, Any]],
        dismiss_audio_preset_suggestion_action: Callable[[str], dict[str, Any]],
        queue_folder_encode_action: Callable[[str, str, bool, bool, str], dict[str, Any]],
        validate_folder_outputs_action: Callable[[str, str], dict[str, Any]],
        promote_folder_outputs_action: Callable[[str, str], dict[str, Any]],
        accept_ambiguous_motion_action: Callable[[str], dict[str, Any]] | None = None,
        keep_motion_held_originals_action: Callable[[str, list[int]], dict[str, Any]] | None = None,
        undo_motion_keep_action: Callable[[str, list[int]], dict[str, Any]] | None = None,
        put_original_back_action: Callable[[str, int], dict[str, Any]] | None = None,
        decide_size_held_action: Callable[[str, int | Collection[int], bool], dict[str, Any]] | None = None,
) -> None:
    @app.post("/api/folders/{prefix:path}/series-lifecycle")
    async def api_folder_series_lifecycle(prefix: str, request: Request) -> JSONResponse:
        body = await request_body(request)
        result = save_series_lifecycle_action(
            prefix.strip("/"),
            str(body.get("mode", "")),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/audio-preset")
    async def api_folder_audio_preset(prefix: str, request: Request) -> JSONResponse:
        body = await request_body(request)
        result = await run_in_threadpool(
            save_audio_preset_action,
            prefix.strip("/"),
            str(body.get("preset", "")),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/audio-preset-suggestion/dismiss")
    async def api_folder_audio_preset_suggestion_dismiss(prefix: str) -> JSONResponse:
        result = await run_in_threadpool(dismiss_audio_preset_suggestion_action, prefix.strip("/"))
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/queue-encode")
    async def api_folder_queue_encode(prefix: str, request: Request) -> JSONResponse:
        body = await request_body(request)
        result = await run_in_threadpool(
            queue_folder_encode_action,
            prefix.strip("/"),
            str(body.get("notes", "")),
            _request_flag(body, "bypass_schedule"),
            _request_flag(body, "override_policy_holds"),
            str(body.get("scope_membership_token", "")),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)
    if accept_ambiguous_motion_action is not None:
        accept_ambiguous_motion = accept_ambiguous_motion_action

        @app.post("/api/folders/{prefix:path}/accept-ambiguous-motion")
        async def api_folder_accept_ambiguous_motion(prefix: str) -> JSONResponse:
            result = await run_in_threadpool(accept_ambiguous_motion, prefix.strip("/"))
            return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    if keep_motion_held_originals_action is not None:
        keep_originals = keep_motion_held_originals_action

        @app.post("/api/folders/{prefix:path}/keep-motion-original")
        async def api_folder_keep_motion_original(prefix: str, request: Request) -> JSONResponse:
            item_ids = _request_item_ids(await request_body(request))
            result = await run_in_threadpool(keep_originals, prefix.strip("/"), item_ids)
            return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    if undo_motion_keep_action is not None:
        undo_keep = undo_motion_keep_action

        @app.post("/api/folders/{prefix:path}/undo-keep-motion-original")
        async def api_folder_undo_keep_motion_original(prefix: str, request: Request) -> JSONResponse:
            item_ids = _request_item_ids(await request_body(request))
            result = await run_in_threadpool(undo_keep, prefix.strip("/"), item_ids)
            return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    if put_original_back_action is not None:
        put_back = put_original_back_action

        @app.post("/api/folders/{prefix:path}/put-original-back")
        async def api_folder_put_original_back(prefix: str, request: Request) -> JSONResponse:
            item_ids = _request_item_ids(await request_body(request))
            if len(item_ids) != 1:
                raise HTTPException(status_code=400, detail="Name one file with library_item_ids.")
            result = await run_in_threadpool(put_back, prefix.strip("/"), item_ids[0])
            return JSONResponse(result, status_code=200 if result.get("ok") else 409)
    @app.post("/api/folders/{prefix:path}/validate-outputs")
    async def api_folder_validate_outputs(prefix: str, request: Request) -> JSONResponse:
        body = await request_body(request)
        result = await run_in_threadpool(
            validate_folder_outputs_action,
            prefix.strip("/"),
            str(body.get("scope_membership_token", "")),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/promote-outputs")
    async def api_folder_promote_outputs(prefix: str, request: Request) -> JSONResponse:
        body = await request_body(request)
        result = await run_in_threadpool(
            promote_folder_outputs_action,
            prefix.strip("/"),
            str(body.get("scope_membership_token", "")),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    if decide_size_held_action is not None:
        decide_size_held = decide_size_held_action

        @app.post("/api/folders/{prefix:path}/size-held-decision")
        async def api_folder_size_held_decision(prefix: str, request: Request) -> JSONResponse:
            body = await request_body(request)
            item_id = body.get("library_item_id")
            item_ids = body.get("library_item_ids")
            keep = body.get("keep")
            if item_ids is not None:
                if (item_id is not None or keep is not False or not isinstance(item_ids, list) or not item_ids
                        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in item_ids)):
                    raise HTTPException(status_code=400, detail="Name the files to make again with keep: false.")
                result = await run_in_threadpool(decide_size_held, prefix.strip("/"), item_ids, False)
                return JSONResponse(result, status_code=200 if result.get("ok") else 409)
            if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0 or not isinstance(keep, bool):
                raise HTTPException(status_code=400, detail="Name one file and whether to keep it.")
            result = await run_in_threadpool(decide_size_held, prefix.strip("/"), item_id, keep)
            return JSONResponse(result, status_code=200 if result.get("ok") else 409)
