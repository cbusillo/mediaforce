import json
from typing import Any, Callable

from mediaforce.core.type_defs import object_dict, object_list


AV1_EVIDENCE_PRECEDENCE = (
    "For this operator, source-resolution 1080p SVT-AV1 at a normalized total size of 200-300 MiB per "
    "40 minutes is an established, visually approved product baseline across conventional and dark or "
    "stylized TV material. Do not describe that baseline as low-bitrate, aggressive, high-risk, unrealistic, "
    "or suitable only for clean or forgiving material based on bitrate or source-size percentage alone. "
    "Evidence precedence is strict: follow the current explicit operator instruction first; then operator-approved "
    "visual results and saved accepted policies; then measured sample and validation results; and use generic "
    "codec heuristics only when stronger evidence is absent. Generic intuition may suggest what to inspect in a "
    "sample, but it may never veto a safe sample or override accepted visual evidence. A non-positive video budget, "
    "a measured encode failure, stale evidence, or an explicit visual rejection remains a concrete stop or redirect "
    "signal; current rejection outranks older approval. "
)


def _summarize_multimodal_review_pack(review_pack: dict[str, Any]) -> dict[str, Any]:
    summarized_pack = {key: value for key, value in review_pack.items() if key != "images"}
    if "images" in review_pack:
        summarized_pack["image_count"] = len(object_list(review_pack.get("images")))
    return summarized_pack


def _audio_tradeoff_key_copy(hint: dict[str, Any] | None) -> str:
    policy_key = str(object_dict(hint).get("policy_key") or "").strip()
    return policy_key or "the hinted audio bitrate"


def build_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "You are advising a personal media re-encoding calibration workflow. "
        "Bias slightly toward smaller files, but do not recommend obvious quality damage. "
        f"{AV1_EVIDENCE_PRECEDENCE}"
        "Return short markdown with these sections exactly: "
        "Recommendation, Why, Setting changes, Audio/Subtitles notes. "
        "Use plain language for a human reviewing TV encodes. Here is the calibration context:\n\n"
        f"{serialized}"
    )


def build_seed_prompt(
        payload: dict[str, Any],
        *,
        policy_key_paths: Callable[[dict[str, Any]], list[str]],
        policy_shape_example: Callable[[dict[str, Any]], dict[str, Any]],
) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    base_policy = object_dict(payload.get("base_policy"))
    hinted_audio_key = _audio_tradeoff_key_copy(object_dict(payload.get("audio_tradeoff_hint")))
    valid_keys = policy_key_paths(base_policy)
    policy_shape = policy_shape_example(base_policy)
    return (
        "You are helping seed a first-pass media encode calibration draft. "
        "This is a cold-start guess for one folder, not a measured calibration result. "
        "Your job is to draft the best first-pass attempt at the operator's request using the available policy keys, not to negotiate the operator back to the base policy. "
        "When runtime_toolbelt.decision_defaults or the video policy includes decision_model=size_first_review, treat the configured target_size_mb per target_runtime_minutes as the product's default goal and treat VMAF/XPSNR as guardrails for review rather than the primary objective. "
        f"{AV1_EVIDENCE_PRECEDENCE}"
        "Treat the operator note as an instruction to satisfy when it is clear and specific. "
        "Use the base policy as a starting surface, not as something that overrules a direct operator request. "
        "If the operator asks for materially smaller files, you are allowed to make material policy changes that pursue that goal, including changing quality targets, encoded-percent caps, CRF bounds, grain, preset, or sample methodology when those knobs exist in the allowed policy keys. "
        "Bias toward accomplishing the operator's ask while still describing the risk honestly. "
        f"When audio_tradeoff_hint.recommended_seed_action is 'hold', strongly prefer leaving {hinted_audio_key} at the base policy value in this first-pass seed. A tight size budget alone is not sufficient to override a hold, especially when audio_tradeoff_hint.review_confidence is low because these audio changes may be harder for the operator to judge casually. If you still lower that hinted audio bitrate despite a hold, explain why the savings are materially necessary and worth the review risk. "
        "When the note is a direct request or explicit experiment, default to honoring it. Use honored_with_risk only when supplied measurements show a concrete failed or impossible constraint, never because a bitrate merely looks small. "
        "Use softened only when the operator note is exploratory, ambiguous, self-contradictory, or cannot be expressed with the available policy keys. "
        "When operator_repeat_signal shows the same explicit experiment was asked for again after earlier softening, treat that as deliberate confirmation and keep the requested draft unless supplied measurements show it cannot run safely or the request cannot be expressed with the available policy keys. "
        "When supplied measurements show a concrete caveat while a safe sample remains queueable, draft the closest faithful experiment and use honored_with_risk. Reject or redirect a non-positive video budget, repeated measured failure, stale evidence, or explicit visual rejection. "
        "When latest_failed_sample_job is present, treat it as the most recent failed or stopped sample attempt; use its error, policy, and result fields to avoid repeating the same failure. "
        "If the operator asks for a smaller encode, do not silently move to a higher quality target or otherwise preserve the old behavior behind reassuring wording. "
        "When the available policy keys include video.black_bar_handling, consider setting it to smart for letterboxed, matted, widescreen, or black-bar-heavy sources when the operator note, folder class, or sample metadata makes that likely; prefer smart detection over manual crop unless the operator supplied an exact crop. "
        "Treat video.max_height as an explicit downsample/cap-height request knob. Do not infer 1080p or 720p scaling from a size budget alone; use max_height only when the operator request or requested_experiment clearly asks for a scale target. When the operator asks to preserve source resolution, avoid downscaling, or keep max_height unset/0, preserve video.max_height as 0 rather than replacing it with the source height. "
        "Do not anchor on legacy H.264 or HEVC bitrate intuition when the policy is using AV1. "
        "Teach media-class taste by choosing representative sample moments, not by inventing a compression floor before measurement. "
        "If you soften or reject a request, say so explicitly and only do it for the narrow reasons above. "
        "High-80s VMAF can be a strong visually approved AV1 result; report the measured score without turning it into a quality verdict. "
        "Use metric_support and the current base policy to decide whether VMAF or XPSNR guidance is more relevant. "
        "Return valid JSON only with this exact shape: "
        f'{{"request_response":"short conversational reply","request_disposition":"honored|honored_with_risk|softened|rejected|unclear","summary":"short sentence","diagnosis":"short diagnosis","confidence":"high|medium|low","evidence_checked":["..."],"suggested_follow_up":"optional short suggestion","feasibility_note":"optional short feasibility note","policy":{json.dumps(policy_shape, sort_keys=True)}}}. '
        "Do not use markdown fences. Use the current base policy as the draft surface. "
        "For any key that should stay unchanged, send null for that key. "
        f"Only use these policy keys: {', '.join(valid_keys) if valid_keys else 'none'}. "
        "Do not invent any other keys. Keep the reply direct and conversational, and keep the summary short and practical. Here is the folder context:\n\n"
        f"{serialized}"
    )


def build_tune_prompt(
        payload: dict[str, Any],
        *,
        policy_key_paths: Callable[[dict[str, Any]], list[str]],
        policy_shape_example: Callable[[dict[str, Any]], dict[str, Any]],
) -> str:
    prompt_payload = dict(payload)
    review_pack = object_dict(prompt_payload.get("multimodal_review_pack"))
    if review_pack:
        prompt_payload["multimodal_review_pack"] = _summarize_multimodal_review_pack(review_pack)
    serialized = json.dumps(prompt_payload, indent=2, sort_keys=True)
    current_policy = object_dict(payload.get("policy"))
    hinted_audio_key = _audio_tradeoff_key_copy(
        object_dict(object_dict(payload.get("runtime_toolbelt")).get("audio_tradeoff_hint"))
    )
    valid_keys = policy_key_paths(current_policy)
    policy_shape = policy_shape_example(current_policy)
    return (
        "You are GPT-5.4 acting as a fast media encode tuning worker. "
        "Your job is to use the operator's note plus the current calibration context to draft the next sampled calibration run for operator review before anything queues. "
        "When runtime_toolbelt.decision_defaults says decision_model=size_first_review, center the next run around the configured target_size_mb per target_runtime_minutes and the operator's visual review, using VMAF/XPSNR as guardrails instead of overriding the requested size-first intent. "
        f"{AV1_EVIDENCE_PRECEDENCE}"
        "The runtime has already gathered the only allowed quick toolbelt evidence for this turn and will run a separate self-check after your proposal. "
        "Do not assume any other probing, frame grabs, or external tools are available. "
        "When review_media_context is present, treat it as part of the active review conversation: the operator may be asking about the sampled source-versus-draft clips, the compare clip, or the current audio tradeoff before approving a folder encode. "
        "When multimodal_review_pack is present, the attached images appear in the same order as multimodal_review_pack.artifacts. Use them as real review evidence, but describe them carefully and never pretend they show more than they actually show. "
        "You cannot literally watch or hear the clips from this prompt, so never pretend that you did. Instead, use the supplied review context, measured calibration result, policy, audio summary, and the operator's own observations to reason collaboratively about likely video and audio quality tradeoffs. "
        "When review_artifact_critique is present, treat it as a first-pass reading over artifacts extracted from the actual review clips, and use it to focus the next draft on the moments most likely to fail visual review. "
        "If the operator is clearly discussing what they saw or heard, engage that review directly in request_response and suggested_follow_up instead of replying like a generic size-only tuner. "
        "Do not do slow exhaustive analysis or long encodes. If you are not confident, say so clearly instead of making up certainty. "
        "Treat the operator note as an instruction to satisfy when it is clear and specific, not as a decorative hint. "
        "Your job is to draft the best next run that actually pursues the operator's request using the available policy keys. "
        "You are allowed to make material policy changes when the note calls for them, including quality targets, encoded-percent caps, CRF bounds, grain, preset, or sample methodology, so long as you stay within the allowed keys and explain the tradeoff honestly. "
        f"When runtime_toolbelt.audio_tradeoff_hint says {hinted_audio_key} is low leverage, prefer video or methodology changes before lowering that hinted audio bitrate. A size budget target alone is not sufficient reason to step down low-leverage hinted audio, especially when runtime_toolbelt.audio_tradeoff_hint.review_confidence is low because those audio changes may be harder for the operator to judge casually. If you still lower it, explain why the savings are materially necessary and worth that review risk, unless the operator's note explicitly references audio quality or audio bitrate. "
        "Treat an operator size budget such as 'around 300MB' as a planning target for the first measured comparison, not as an automatic hard ceiling. Do not turn a size budget alone into max_encoded_percent, a lower quality target, lower audio bitrate, or lower resolution before measurement. Once runtime_toolbelt.size_target_analysis shows a confirmed follow-up size target landed above the target band, the next draft must materially move toward that requested size; if the runtime applies a measured max_encoded_percent ceiling, explain it plainly as the learned next-sample constraint instead of fighting or hiding it. If the measured sample is under the target band, prefer spending saved bitrate on quality instead of making it smaller. "
        "Use softened only when the note is exploratory, ambiguous, self-contradictory, or cannot be expressed with the available policy keys. "
        "If you intentionally soften or redirect an explicit request, say that plainly in request_response, diagnosis, and suggested_follow_up instead of hiding the tradeoff. "
        "If the operator asks for a smaller encode, do not silently move to a higher quality target or preserve the old behavior behind reassuring language. "
        "When the available policy keys include video.black_bar_handling, consider setting it to smart for letterboxed, matted, widescreen, or black-bar-heavy sources when the operator note, review evidence, or sample metadata makes that likely; prefer smart detection over manual crop unless the operator supplied an exact crop. "
        "Treat video.max_height as an explicit downsample/cap-height request knob. Do not infer 1080p or 720p scaling from a size budget alone; use max_height only when operator_note_parse.scale_height, requested_experiment, or the operator note clearly asks for a scale target. When the operator asks to preserve source resolution, avoid downscaling, or keep max_height unset/0, preserve video.max_height as 0 rather than replacing it with the source height. "
        "Do not anchor on legacy H.264 or HEVC bitrate intuition when the policy is using AV1. "
        "Treat high-80s VMAF as a measured score for review, not as evidence that an AV1 draft is inherently deficient. "
        "When requested_experiment or retrieved_memory shows the operator repeated the same explicit request, treat that as deliberate confirmation and keep the requested draft unless supplied measurements show it cannot run safely or the request cannot be expressed with the available policy keys. "
        "When supplied measurements show a concrete caveat while a safe sample remains queueable, draft the closest faithful experiment and use honored_with_risk. Reject or redirect a non-positive video budget, repeated measured failure, stale evidence, or explicit visual rejection. "
        "When latest_failed_sample_job is present, treat it as the most recent failed or stopped sample attempt; use its error, policy, and result fields to diagnose what to change instead of repeating that run. "
        "Return JSON only with no markdown fences or extra commentary. "
        "Return valid JSON only with this exact shape: "
        f'{{"request_response":"short conversational reply","request_disposition":"honored|honored_with_risk|softened|rejected|unclear","summary":"short sentence","diagnosis":"short diagnosis","confidence":"high|medium|low","evidence_checked":["..."],"suggested_follow_up":"optional short suggestion","feasibility_note":"optional short feasibility note","policy":{json.dumps(policy_shape, sort_keys=True)}}}. '
        "Use the current policy as the full draft surface for this run. For any key that should stay unchanged, send null for that key. "
        "It is acceptable to change methodology knobs like preset, sample cadence, or CRF bounds when the note clearly justifies it, but be explicit in the diagnosis when you do that. "
        f"Only use these policy keys: {', '.join(valid_keys) if valid_keys else 'none'}. "
        "Do not invent other keys. Keep request_response conversational and direct. Here is the tuning context:\n\n"
        f"{serialized}"
    )


def build_review_artifact_critique_prompt(payload: dict[str, Any]) -> str:
    prompt_payload = dict(payload)
    review_pack = object_dict(prompt_payload.get("multimodal_review_pack"))
    if review_pack:
        prompt_payload["multimodal_review_pack"] = _summarize_multimodal_review_pack(review_pack)
    serialized = json.dumps(prompt_payload, indent=2, sort_keys=True)
    return (
        "You are reviewing source-versus-draft media artifacts extracted from actual sampled review clips. "
        "Use the attached images as visual evidence from the real clips, but never claim to watch motion or hear audio beyond what those artifacts can support. "
        "Do not infer visible damage from bitrate, source-size percentage, or metric target when the supplied artifacts do not show it. "
        "Focus on what looks preserved, what looks fragile, and which moments deserve extra caution before another draft is approved. "
        "Return JSON only with no markdown fences or extra commentary. "
        "Return valid JSON only with this exact shape: "
        '{"summary":"short practical read","confidence":"high|medium|low","weakest_moments":["..."],"preserved_strengths":["..."],"artifacts_to_recheck":["..."],"recommendation":"short recommendation","evidence_checked":["..."]}. '
        "Keep the answer concise and grounded in the supplied artifacts and metadata. Here is the review context:\n\n"
        f"{serialized}"
    )


def build_run_verdict_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "You are GPT-5.4 summarizing a completed media encode calibration run for a human operator. "
        "The run is already measured by deterministic tools; do not invent metrics. "
        f"{AV1_EVIDENCE_PRECEDENCE}"
        "If the operator requested an explicit experiment, judge the outcome against that request instead of generic conservative defaults. "
        "When size_target_analysis is present, use its status and target band directly: inside_target_band means the size request is satisfied, under_target means the run is smaller than requested without implying quality damage, and over_target means another measured sample may be needed if the operator still wants that size. "
        "Keep the answer short, practical, and honest about risk. "
        "Return valid JSON only with this exact shape: "
        '{"summary":"short verdict","outcome":"strong_match|acceptable_experiment|needs_review|poor_fit","confidence":"high|medium|low","next_step":"optional short suggestion","evidence_checked":["..."]}. '
        "Here is the completed calibration context:\n\n"
        f"{serialized}"
    )


def build_operator_note_parse_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "You are classifying a media encode operator note before policy compilation. "
        "Decide whether the note is a direct request to execute now, an exploratory question, approval feedback, or something else. "
        "Extract only explicit tuning requests that are actually present in the note. "
        "Mark operator_confirmed true only when the note is a clear instruction the system should act on now. "
        "Mark measured_size_followup true only when the note is explicitly a follow-up to a previous measured sample missing or exceeding a size target, such as revising smaller after a larger-than-target sample. "
        "Set evidence_authority to operator_observed when the operator reports a positive observation about what they actually saw or heard in a real sample. Set it to approved_visual_result only when the operator clearly says they reviewed and accepted or approved the real result. Set it to rejected_visual_result when the operator explicitly rejects the real result or reports visible or audible damage. Otherwise set it to none. "
        "Directive questions still count as direct requests when they clearly ask for action, such as 'Target 300MB per episode?' or 'Can you target 300MB per episode?'. "
        "Exploratory wording stays unconfirmed when the operator is asking whether a change would help or is realistic, such as 'Can we try to target 85 VMAF instead? Will that help?' or 'I want to understand if 300MB per episode is realistic.' "
        "Extract explicit downsample or output-height cap requests into scale_height, such as 'downsample to 1080p', 'cap at 720p', or 'make the 4K files 1080p'. This is only for requested scaling; do not infer a scale target from source resolution, a size budget, or general smaller-file language. If the operator asks to preserve source resolution, avoid downscaling, or keep max_height unset/0, set scale_height to 0 to represent source resolution/no height cap. "
        "Extract explicit black-bar handling requests into black_bar_handling as smart when the operator asks for smart or automatic black-bar detection/cropping. Extract an exact manual crop into crop only when the note includes a concrete W:H:X:Y crop like 1920:800:0:140. Do not invent a crop. "
        "If any scale, black-bar, or crop target appears with a size budget or metric target, use combined_experiment. If only scale, black-bar, or crop targets are requested, use scale_target. If both a size budget and a metric target are explicitly requested, use combined_experiment. "
        "Return JSON only with no markdown fences or extra commentary. "
        "Return valid JSON only with this exact shape: "
        '{"summary":"short summary","intent_type":"direct_request|exploratory_question|approval_feedback|other|unclear","request_type":"none|metric_target|size_budget|scale_target|combined_experiment","operator_confirmed":true,"measured_size_followup":false,"evidence_authority":"none|operator_observed|approved_visual_result|rejected_visual_result","metric":"vmaf|xpsnr|null","metric_target":85,"size_budget_value":300,"size_budget_unit":"mb|gb|kb|tb|null","scale_height":1080,"black_bar_handling":"smart|null","crop":"1920:800:0:140|null","reasoning_note":"short explanation"}. '
        "Do not invent requests that are not explicitly in the note. Here is the note context:\n\n"
        f"{serialized}"
    )
