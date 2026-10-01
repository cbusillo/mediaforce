import unittest
from typing import Any

from mediaforce.library.workflow_state import ItemWorkflowState
from mediaforce.web.runtime.episode_progress import season_episode_progress

SEASON = "tv/Show/Season 1"


def _item(number: int, state: str = "encode_candidate", *, blocker: str | None = None) -> ItemWorkflowState:
    return ItemWorkflowState(
        item_id=number,
        rel_path=f"{SEASON}/Episode {number:02d}.mkv",
        status="planned",
        state=state,  # type: ignore[arg-type]
        lane="encode",
        has_staged_output=False,
        blocker=blocker,
    )


def _run(status: str, **fields: Any) -> dict[str, Any]:
    return {"job_id": f"run-{status}", "status": status, "progress": {}, **fields}


def _stages(episodes: list[dict[str, Any]]) -> dict[str, str]:
    return {episode["rel_path"].rsplit("/", 1)[-1]: episode["stage"] for episode in episodes}


class SeasonEpisodeProgressTests(unittest.TestCase):
    def test_each_episode_reads_where_it_has_got(self) -> None:
        items = [
            _item(1, "complete"),
            _item(2),
            _item(3),
            _item(4),
            _item(5),
            _item(6, "ready_to_validate"),
            _item(7, "held", blocker="This season is still receiving episodes."),
            _item(8),
            _item(9),
            _item(10),
        ]
        runs = {
            items[1].rel_path: _run("running", progress={"progress_state": "encoding", "percent_complete": 41.6}),
            items[2].rel_path: _run("running", progress={"progress_state": "quality_search"}),
            items[3].rel_path: _run("queued", waiting_reason="Waiting for a host schedule window."),
            items[4].rel_path: _run(
                "needs_attention",
                progress={"failure_analysis": {"kind": "final_size_target_miss"}},
            ),
            items[8].rel_path: _run(
                "needs_attention",
                progress={"failure_analysis": {"owner_size_decision": {"answer": "keep_original"}}},
            ),
            items[9].rel_path: _run("running", progress={"progress_state": "staging_source"}),
        }

        episodes = season_episode_progress(items, runs, {1: 900_000_000})
        by_name = {episode["rel_path"].rsplit("/", 1)[-1]: episode for episode in episodes}

        self.assertEqual(
            _stages(episodes),
            {
                "Episode 01.mkv": "published",
                "Episode 02.mkv": "compressing",
                "Episode 03.mkv": "measuring",
                "Episode 04.mkv": "waiting",
                "Episode 05.mkv": "needs_you",
                "Episode 06.mkv": "checking",
                "Episode 07.mkv": "held",
                "Episode 08.mkv": "not_started",
                "Episode 09.mkv": "kept_original",
                "Episode 10.mkv": "getting_ready",
            },
        )
        self.assertEqual(by_name["Episode 01.mkv"]["bytes_saved"], 900_000_000)
        self.assertEqual(by_name["Episode 02.mkv"]["percent_complete"], 42)
        self.assertEqual(by_name["Episode 04.mkv"]["detail"], "waiting for a scheduled time")
        self.assertEqual(by_name["Episode 05.mkv"]["detail"], "didn't pass the final size check")
        self.assertEqual(by_name["Episode 07.mkv"]["detail"], "This season is still receiving episodes.")
        self.assertEqual(by_name["Episode 09.mkv"]["detail"], "kept as the original, your choice")
        # The owner's part comes first, then work under way, then what is done or staying.
        self.assertEqual(
            [episode["stage"] for episode in episodes],
            [
                "needs_you",
                "compressing",
                "measuring",
                "getting_ready",
                "checking",
                "waiting",
                "published",
                "kept_original",
                "held",
                "not_started",
            ],
        )

    def test_a_waiting_run_only_the_owner_can_end_needs_them(self) -> None:
        item = _item(1)
        run = _run(
            "queued",
            waiting_reason=(
                "Estimated runtime about 4h is longer than every configured host schedule window "
                "(longest about 3h on Smoke Worker). Widen a host window or use Bypass scheduler."
            ),
        )

        [episode] = season_episode_progress([item], {item.rel_path: run}, {})

        self.assertEqual(episode["stage"], "needs_you")
        self.assertEqual(episode["detail"], "longer than every work window")

    def test_a_size_question_comes_with_the_episode(self) -> None:
        item = _item(1)
        run = _run(
            "needs_attention",
            job_id="shard-1",
            progress={
                "failure_analysis": {
                    "kind": "quality_floor_size_conflict",
                    "retry_strategy": "needs_operator_approval",
                    "target_size_bytes": 191_800_000,
                    "proposed_target_size_bytes": 358_900_000,
                    "item_rel_path": item.rel_path,
                }
            },
        )

        [episode] = season_episode_progress([item], {item.rel_path: run}, {})

        self.assertEqual(episode["stage"], "needs_you")
        self.assertEqual(
            episode["size_question"],
            {
                "job_id": "shard-1",
                "rel_path": item.rel_path,
                "goal_bytes": 191_800_000,
                "smallest_quality_safe_bytes": 358_900_000,
            },
        )

    def test_a_held_episode_the_owner_queued_anyway_shows_its_run(self) -> None:
        item = _item(1, "held", blocker="This season is still receiving episodes.")
        run = _run("running", progress={"progress_state": "encoding", "percent_complete": 10})

        [episode] = season_episode_progress([item], {item.rel_path: run}, {})

        self.assertEqual(episode["stage"], "compressing")

    def test_a_finished_run_defers_to_the_files_own_state(self) -> None:
        staged = _item(1, "ready_to_validate")
        lost = _item(2, "blocked", blocker="Encoded item is missing its staged output.")
        runs = {staged.rel_path: _run("completed"), lost.rel_path: _run("completed")}

        episodes = season_episode_progress([staged, lost], runs, {})

        self.assertEqual(_stages(episodes), {"Episode 01.mkv": "checking", "Episode 02.mkv": "needs_you"})

    def test_a_checked_file_waiting_on_the_owner_needs_them(self) -> None:
        held, failed, remade = _item(1, "ready_to_validate"), _item(2, "ready_to_validate"), _item(3, "ready_to_validate")
        runs = {
            held.rel_path: _run("completed"),
            failed.rel_path: _run("completed"),
            remade.rel_path: _run("running", progress={"progress_state": "encoding", "percent_complete": 5}),
        }

        episodes = season_episode_progress(
            [held, failed, remade],
            runs,
            {},
            {held.item_id: "size_held", failed.item_id: "failed", remade.item_id: "size_held"},
        )
        by_name = {episode["rel_path"].rsplit("/", 1)[-1]: episode for episode in episodes}

        self.assertEqual(by_name["Episode 01.mkv"]["stage"], "needs_you")
        self.assertEqual(by_name["Episode 01.mkv"]["owner_action"], "keep_or_remake")
        self.assertEqual(by_name["Episode 02.mkv"]["stage"], "needs_you")
        self.assertEqual(by_name["Episode 02.mkv"]["detail"], "didn't pass its check")
        # Making it again is the newer work, so it shows instead of the old question.
        self.assertEqual(by_name["Episode 03.mkv"]["stage"], "compressing")

    def test_missing_files_are_left_out(self) -> None:
        self.assertEqual(season_episode_progress([_item(1, "missing")], {}, {}), [])


if __name__ == "__main__":
    unittest.main()
