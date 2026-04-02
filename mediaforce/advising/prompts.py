import json
from typing import Any, Callable

from mediaforce.core.type_defs import object_dict, object_list


def build_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "You are advising a personal media re-encoding calibration workflow. "
        "Bias slightly toward smaller files, but do not recommend obvious quality damage. "
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
    valid_keys = policy_key_paths(base_policy)
    policy_shape = policy_shape_example(base_policy)
    return (
        "You are helping seed a first-pass media encode calibration draft. "
        "This is a cold-start guess for one folder, not a measured calibration result. "
        "Treat the operator note as the start of a conversation, not a decorative hint. "
        "Bias slightly toward smaller files, but avoid obvious quality damage. "
        "Teach media-class taste, not a single-title compression floor. "
        "Use the base policy as the safe default and make only the smallest helpful overrides. "
        "Clean, forgiving material can lean one notch smaller than default, but dark, grainy, noisy, fast-motion, or otherwise uncertain material should stay close to the base policy until measured calibration proves it can move further. "
        "Do not chase dramatic savings, do not generalize aggressively from one easy sample item, and prefer leaving a key out when the evidence is weak. "
        "If the operator asks for a smaller encode, do not silently move to a higher quality target. "
        "If the request looks unrealistic, you may still draft the operator-confirmed experiment, but say that plainly and mark it as honored_with_risk instead of fighting the request. "
        "If you soften or reject a request, say so explicitly. "
        "If the class signals clearly look like clean 1080p catalog TV, a mild lean can look like VMAF around 94-95, XPSNR around 39-40, max encoded percent in the low-to-mid 70s, grain around 4-6, and 5.1 Opus around 224k, but treat those as gentle reference points rather than mandatory targets. "
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
        summarized_pack = {key: value for key, value in review_pack.items() if key != "images"}
        if "images" in review_pack:
            summarized_pack["image_count"] = len(object_list(review_pack.get("images")))
        prompt_payload["multimodal_review_pack"] = summarized_pack
    serialized = json.dumps(prompt_payload, indent=2, sort_keys=True)
    current_policy = object_dict(payload.get("policy"))
    valid_keys = policy_key_paths(current_policy)
    policy_shape = policy_shape_example(current_policy)
    return (
        "You are GPT-5.4 acting as a fast media encode tuning worker. "
        "Your job is to use the operator's note plus the current calibration context to draft the next sampled calibration run for operator review before anything queues. "
        "The runtime has already gathered the only allowed quick toolbelt evidence for this turn and will run a separate self-check after your proposal. "
        "Do not assume any other probing, frame grabs, or external tools are available. "
        "When review_media_context is present, treat it as part of the active review conversation: the operator may be asking about the sampled source-versus-draft clips, the compare clip, or the current audio tradeoff before approving a folder encode. "
        "When multimodal_review_pack is present, the attached images appear in the same order as multimodal_review_pack.artifacts. Use them as real review evidence, but describe them carefully and never pretend they show more than they actually show. "
        "You cannot literally watch or hear the clips from this prompt, so never pretend that you did. Instead, use the supplied review context, measured calibration result, policy, audio summary, and the operator's own observations to reason collaboratively about likely video and audio quality tradeoffs. "
        "If the operator is clearly discussing what they saw or heard, engage that review directly in request_response and suggested_follow_up instead of replying like a generic size-only tuner. "
        "Do not do slow exhaustive analysis or long encodes. If you are not confident, say so clearly instead of making up certainty. "
        "Treat the operator note as the start of a conversation, not a decorative hint. "
        "Use the operator note directly. If you intentionally soften or redirect an explicit request, say that plainly in request_response, diagnosis, and suggested_follow_up instead of hiding the tradeoff. "
        "If the operator asks for a smaller encode, do not silently move to a higher quality target. "
        "If the request looks unrealistic, you may still draft the operator-confirmed experiment, but mark it honored_with_risk and explain the risk plainly instead of fighting the request. "
        "Return JSON only with no markdown fences or extra commentary. "
        "Return valid JSON only with this exact shape: "
        f'{{"request_response":"short conversational reply","request_disposition":"honored|honored_with_risk|softened|rejected|unclear","summary":"short sentence","diagnosis":"short diagnosis","confidence":"high|medium|low","evidence_checked":["..."],"suggested_follow_up":"optional short suggestion","feasibility_note":"optional short feasibility note","policy":{json.dumps(policy_shape, sort_keys=True)}}}. '
        "Use the current policy as the full draft surface for this run. For any key that should stay unchanged, send null for that key. "
        "It is acceptable to change methodology knobs like preset, sample cadence, or CRF bounds when the note clearly justifies it, but be explicit in the diagnosis when you do that. "
        f"Only use these policy keys: {', '.join(valid_keys) if valid_keys else 'none'}. "
        "Do not invent other keys. Keep request_response conversational and direct. Here is the tuning context:\n\n"
        f"{serialized}"
    )


def build_run_verdict_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "You are GPT-5.4 summarizing a completed media encode calibration run for a human operator. "
        "The run is already measured by deterministic tools; do not invent metrics. "
        "If the operator requested an explicit experiment, judge the outcome against that request instead of generic conservative defaults. "
        "Keep the answer short, practical, and honest about risk. "
        "Return valid JSON only with this exact shape: "
        '{"summary":"short verdict","outcome":"strong_match|acceptable_experiment|needs_review|poor_fit","confidence":"high|medium|low","next_step":"optional short suggestion","evidence_checked":["..."]}. '
        "Here is the completed calibration context:\n\n"
        f"{serialized}"
    )
