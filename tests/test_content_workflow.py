"""No-network tests for the multi-agent approval gate and video handoff."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from influencerai.config.settings import Settings
from influencerai.pipeline.content_workflow import (
    AgentBundle,
    _copy_social_metadata,
    _invoke_with_retries,
    _publish_to_targets,
    _is_repeated_topic,
    _load_topic_history,
    MAX_TOPIC_SELECTION_ATTEMPTS,
    extract_hashtags,
    _save_topic_history,
    _topic_genre,
    _topic_title,
    _validate_trend_evidence,
    run_content_workflow,
)
from influencerai.schemas.review import ScreenplayReview
from influencerai.schemas.screenplay import Screenplay


def screenplay_payload(scene_count: int = 5, *, words_per_scene: int = 5) -> dict:
    return {
        "video_title": "Reviewed Pipeline Story",
        "target_duration": 26,
        "aspect_ratio": "9:16",
        "scenes": [
            {
                "scene_id": scene_id,
                "duration": 26 / scene_count,
                "voiceover": " ".join([f"scene{scene_id}"] * words_per_scene),
                "image_prompt": f"A specific cinematic scene {scene_id}.",
                "motion": "subtle_zoom",
            }
            for scene_id in range(1, scene_count + 1)
        ],
    }


def approved_review(decision: str = "approve", required_changes: list[str] | None = None) -> dict:
    return {
        "decision": decision,
        "overall_score": 8.1 if decision == "approve" else 6.4,
        "critical_failure": False,
        "score_breakdown": {
            "hook": 8,
            "storytelling": 8,
            "retention": 8,
            "visual_potential": 8,
            "narration": 8,
            "payoff": 8,
            "originality": 8,
        },
        "strongest_elements": ["Clear story progression."],
        "biggest_problems": [],
        "retention_risks": [],
        "factual_issues": [],
        "required_changes": required_changes if required_changes is not None else (
            [] if decision == "approve" else ["Strengthen the opening hook."]
        ),
        "final_recommendation": "Ready for generation." if decision == "approve" else "Revise before generation.",
    }


def current_topic_opportunity(title: str = "A verified current story opportunity", genre: str = "science and space") -> str:
    today = datetime.now().astimezone().date().isoformat()
    return (
        f"TOPIC: {title}\n\n"
        f"GENRE: {genre}\n\n"
        "TREND EVIDENCE:\n"
        f"- Current independent coverage dated {today} shows renewed interest.\n\n"
        "SOURCES:\n- Independent coverage: https://example.com/current-story\n"
    )


class ContentWorkflowTests(unittest.TestCase):
    def test_social_hashtag_files_are_copied_beside_rendered_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            screenplay_directory = root / "content-run"
            video_directory = root / "video-run"
            screenplay_directory.mkdir()
            video_directory.mkdir()
            (screenplay_directory / "hashtags.txt").write_text("#YouTubeTopic", encoding="utf-8")
            (screenplay_directory / "instagram-hashtags.txt").write_text("#InstagramTopic", encoding="utf-8")
            (video_directory / "hashtags.txt").write_text("#KeepExisting", encoding="utf-8")

            _copy_social_metadata(
                screenplay_directory / "screenplay.json",
                video_directory / "final.mp4",
            )

            self.assertEqual((video_directory / "hashtags.txt").read_text(encoding="utf-8"), "#KeepExisting")
            self.assertEqual(
                (video_directory / "instagram-hashtags.txt").read_text(encoding="utf-8"),
                "#InstagramTopic",
            )

    def test_extract_hashtags_reads_platform_specific_sections(self) -> None:
        output = (
            "YOUTUBE HASHTAGS: #SpaceShorts #India\n"
            "INSTAGRAM HASHTAGS: #Astrophysics #SpacePhotography #IndiaScience #Cosmos #Astronomy"
        )

        self.assertEqual(extract_hashtags(output, section="YOUTUBE HASHTAGS"), ["#SpaceShorts", "#India"])
        self.assertEqual(
            extract_hashtags(output, limit=5, section="INSTAGRAM HASHTAGS"),
            ["#Astrophysics", "#SpacePhotography", "#IndiaScience", "#Cosmos", "#Astronomy"],
        )

    def test_dual_platform_uploads_run_concurrently(self) -> None:
        upload_barrier = threading.Barrier(2)
        completed: list[str] = []

        def youtube_upload(_video_path: Path, _screenplay_path: Path) -> None:
            upload_barrier.wait(timeout=2)
            completed.append("youtube")

        def instagram_upload(_video_path: Path, _screenplay_path: Path) -> None:
            upload_barrier.wait(timeout=2)
            completed.append("instagram")

        _publish_to_targets(
            Path("final.mp4"),
            Path("screenplay.json"),
            youtube_upload=youtube_upload,
            instagram_upload=instagram_upload,
        )

        self.assertCountEqual(completed, ["youtube", "instagram"])

    def run_workflow(self, decision: str):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
        bundle = AgentBundle(object(), object(), object(), object())
        responses = [
            current_topic_opportunity(),
            "RESEARCH: Verified source-backed facts",
            Screenplay.model_validate(screenplay_payload()),
            ScreenplayReview.model_validate(approved_review(decision)),
        ]

        async def fake_agent_runner(agent, prompt):
            return responses.pop(0)

        video_generator = Mock()
        video_generator.return_value = root / "output" / "final.mp4"
        result = asyncio.run(
            run_content_workflow(
                settings=settings,
                agents=bundle,
                agent_runner=fake_agent_runner,
                video_generator=video_generator,
            )
        )
        return result, video_generator

    def test_explicit_approval_generates_video_from_saved_screenplay(self) -> None:
        result, video_generator = self.run_workflow("approve")
        video_generator.assert_called_once()
        screenplay_path = video_generator.call_args.args[0]
        self.assertEqual(screenplay_path, result.screenplay_path)
        self.assertTrue(screenplay_path.is_file())
        self.assertEqual(json.loads(screenplay_path.read_text(encoding="utf-8"))["video_title"], "Reviewed Pipeline Story")
        self.assertTrue((result.run_directory / "review.json").is_file())
        self.assertEqual(result.video_path, video_generator.return_value)

    def test_india_audience_context_is_applied_to_every_agent_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            responses = [
                current_topic_opportunity(),
                "RESEARCH: Verified source-backed facts",
                Screenplay.model_validate(screenplay_payload()),
                ScreenplayReview.model_validate(approved_review()),
            ]
            prompts = []

            async def fake_agent_runner(agent, prompt):
                prompts.append(prompt)
                return responses.pop(0)

            asyncio.run(
                run_content_workflow(
                    settings=settings,
                    agents=bundle,
                    agent_runner=fake_agent_runner,
                    video_generator=Mock(return_value=root / "final.mp4"),
                )
            )

        self.assertEqual(len(prompts), 4)
        for prompt in prompts:
            self.assertIn("primary audience", prompt)
            self.assertIn("India", prompt)
        self.assertIn("English by default", prompts[2])
        self.assertIn("Compare at least three distinct potential stories", prompts[0])
        self.assertIn("return the single best opportunity", prompts[0].lower())
        self.assertIn("three distinct research-grounded opening lines", prompts[2])
        self.assertIn("resolve the opening's promise", prompts[2])
        self.assertIn("first spoken sentence and first visual", prompts[3])
        self.assertIn("recommend revision", prompts[3])

    def test_agent_runner_retries_transient_failures_only(self) -> None:
        class TemporaryAgentFailure(Exception):
            pass

        calls = 0

        async def unstable_agent(agent, prompt):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TemporaryAgentFailure("temporary network issue")
            return "recovered"

        result = asyncio.run(
            _invoke_with_retries(
                unstable_agent,
                object(),
                "prompt",
                retryable_errors=(TemporaryAgentFailure,),
                retry_delay_seconds=0,
            )
        )
        self.assertEqual(result, "recovered")
        self.assertEqual(calls, 2)

        calls = 0

        async def permanent_failure(agent, prompt):
            nonlocal calls
            calls += 1
            raise ValueError("invalid agent output")

        with self.assertRaisesRegex(ValueError, "invalid agent output"):
            asyncio.run(
                _invoke_with_retries(
                    permanent_failure,
                    object(),
                    "prompt",
                    retryable_errors=(TemporaryAgentFailure,),
                    retry_delay_seconds=0,
                )
            )
        self.assertEqual(calls, 1)

    def test_topic_history_bootstraps_from_existing_run_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "output"
            run = output_root / "content-runs" / "old-run"
            run.mkdir(parents=True)
            (run / "topic-generator-output.md").write_text(
                "```json\nTOPIC:\nA surprising wildlife migration\n",
                encoding="utf-8",
            )
            history = _load_topic_history(output_root)
        self.assertEqual(history, [{"topic": "A surprising wildlife migration", "genre": "nature and wildlife"}])

    def test_persisted_history_is_not_reordered_by_older_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "output"
            old_run = output_root / "content-runs" / "old-run"
            old_run.mkdir(parents=True)
            (old_run / "topic-generator-output.md").write_text("TOPIC:\nAn old topic\n", encoding="utf-8")
            current_history = [{"topic": "The latest selected topic", "genre": "science and space"}]
            _save_topic_history(output_root, current_history)

            self.assertEqual(_load_topic_history(output_root), current_history)

    def test_topic_title_parser_handles_multiline_heading_and_genre_inference(self) -> None:
        topic = "TOPIC:\nNASA's Swift Space Telescope's Uncontrolled Reentry\n"
        self.assertEqual(_topic_title(topic), "NASA's Swift Space Telescope's Uncontrolled Reentry")
        self.assertEqual(_topic_genre(topic), "science and space")

    def test_topic_repeat_detection_catches_reworded_same_event(self) -> None:
        previous = "The Impact of the 2026 Midterm Elections on AI Regulation and Data Center Development"
        candidate = "The Impact of the Democrats' Midterm Victory on the AI Industry"
        self.assertTrue(_is_repeated_topic(candidate, [previous]))

    def test_repeated_topic_is_reselected_and_history_passed_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            repeated = current_topic_opportunity("A surprising wildlife migration", "nature and wildlife")
            fresh = current_topic_opportunity("A newly documented deep sea species", "science and space")
            responses = [
                repeated,
                "RESEARCH: Verified source-backed facts",
                Screenplay.model_validate(screenplay_payload()),
                ScreenplayReview.model_validate(approved_review()),
            ]
            prompts = []
            topic_calls = 0

            async def fake_agent_runner(agent, prompt):
                nonlocal topic_calls
                prompts.append(prompt)
                if agent is bundle.topic_generator:
                    topic_calls += 1
                    return repeated if topic_calls == 1 else fresh
                return responses.pop(0)

            # Seed persistent history as if the first topic had been produced by an earlier run.
            history_root = settings.output_directory
            (history_root / "content-runs" / "old-run").mkdir(parents=True)
            (history_root / "content-runs" / "old-run" / "topic-generator-output.md").write_text(
                repeated,
                encoding="utf-8",
            )

            result = asyncio.run(
                run_content_workflow(
                    settings=settings,
                    agents=bundle,
                    agent_runner=fake_agent_runner,
                    video_generator=Mock(return_value=root / "final.mp4"),
                )
            )

            topic_prompts = [prompt for prompt in prompts if "Find one currently trending" in prompt or "previous topic candidate" in prompt]
            self.assertEqual(len(topic_prompts), 2)
            self.assertIn("A surprising wildlife migration", topic_prompts[0])
            self.assertIn("different appropriate genre", topic_prompts[1])
            saved_topic = (result.run_directory / "topic-generator-output.md").read_text(encoding="utf-8")
            self.assertIn("A newly documented deep sea species", saved_topic)

    def test_topic_retries_keep_date_history_and_all_rejected_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            repeated = current_topic_opportunity("India's indigenous GPU development plan", "technology and AI")
            stale = (
                "TOPIC: A past archaeology discovery\nGENRE: history and archaeology\n"
                "TREND EVIDENCE:\n- Report dated 2001-01-01.\n"
                "SOURCES:\n- https://example.com/old-discovery\n"
            )
            fresh = current_topic_opportunity("A recent wildlife conservation discovery", "nature and wildlife")
            candidates = iter([repeated, stale, repeated, fresh])
            prompts: list[str] = []
            responses = iter([
                "RESEARCH: Verified source-backed facts",
                Screenplay.model_validate(screenplay_payload()),
                ScreenplayReview.model_validate(approved_review()),
            ])

            async def fake_agent_runner(agent, prompt):
                if agent is bundle.topic_generator:
                    prompts.append(prompt)
                    return next(candidates)
                return next(responses)

            history = [{"topic": f"Historic topic {i}", "genre": "science and space"} for i in range(25)]
            history[0]["topic"] = "India's indigenous GPU development plan"
            _save_topic_history(settings.output_directory, history)
            result = asyncio.run(run_content_workflow(
                settings=settings,
                agents=bundle,
                agent_runner=fake_agent_runner,
                video_generator=Mock(return_value=root / "final.mp4"),
            ))

            self.assertEqual(len(prompts), 4)
            self.assertIn("India's indigenous GPU development plan", prompts[0])
            self.assertIn("Today is " + datetime.now().astimezone().date().isoformat(), prompts[3])
            self.assertIn("India's indigenous GPU development plan", prompts[3])
            self.assertIn("A past archaeology discovery", prompts[3])
            self.assertIn("stale or future-dated", prompts[3])
            self.assertIn("Compare distinct supported stories", prompts[3])
            self.assertIn("return only the strongest eligible opportunity", prompts[3])
            saved_topic = (result.run_directory / "topic-generator-output.md").read_text(encoding="utf-8")
            self.assertIn("A recent wildlife conservation discovery", saved_topic)

    def test_topic_retry_exhaustion_does_not_render_or_save_topic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            repeated = current_topic_opportunity("India's indigenous GPU development plan", "technology and AI")
            _save_topic_history(settings.output_directory, [
                {"topic": "India's indigenous GPU development plan", "genre": "technology and AI"}
            ])
            prompts: list[str] = []
            generator = Mock()

            async def fake_agent_runner(agent, prompt):
                self.assertIs(agent, bundle.topic_generator)
                prompts.append(prompt)
                return repeated

            with self.assertRaisesRegex(ValueError, "Could not select a fresh, trend-verified topic"):
                asyncio.run(run_content_workflow(
                    settings=settings,
                    agents=bundle,
                    agent_runner=fake_agent_runner,
                    video_generator=generator,
                ))

            self.assertEqual(len(prompts), MAX_TOPIC_SELECTION_ATTEMPTS)
            generator.assert_not_called()
            self.assertEqual(len(_load_topic_history(settings.output_directory)), 1)

    def test_topic_search_uses_today_and_preserves_all_rejected_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            _save_topic_history(settings.output_directory, [
                {"topic": "Previously covered wildlife", "genre": "nature and wildlife"},
            ])
            old_date = (datetime.now().astimezone().date() - timedelta(days=30)).isoformat()
            candidates = iter([
                current_topic_opportunity("Previously covered wildlife"),
                f"TOPIC: Old moon discovery\nTREND EVIDENCE:\n- {old_date} old report\nSOURCES:\n- https://example.com/old\n",
                current_topic_opportunity("A new marine discovery"),
            ])
            responses = iter([
                "RESEARCH: Verified source-backed facts", screenplay_payload(), approved_review(),
            ])
            topic_prompts = []
            research_prompts = []

            async def runner(agent, prompt):
                if agent is bundle.topic_generator:
                    topic_prompts.append(prompt)
                    return next(candidates)
                if agent is bundle.topic_researcher:
                    research_prompts.append(prompt)
                return next(responses)

            asyncio.run(run_content_workflow(
                settings=settings, agents=bundle, agent_runner=runner,
                video_generator=Mock(return_value=root / "final.mp4"),
            ))
            today = datetime.now().astimezone().date().isoformat()
            self.assertEqual(len(topic_prompts), 3)
            for prompt in topic_prompts:
                self.assertIn(f"Today is {today}", prompt)
                self.assertIn(f"as of {today}", prompt)
            self.assertIn("Previously covered wildlife", topic_prompts[2])
            self.assertIn("Old moon discovery", topic_prompts[2])
            self.assertIn("stale or future-dated", topic_prompts[2])
            self.assertIn(f"as of {today}", research_prompts[0])

    def test_invalid_screenplay_output_is_retried_with_validation_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            responses = [
                current_topic_opportunity(),
                "RESEARCH: Verified source-backed facts",
                "not valid screenplay json",
                Screenplay.model_validate(screenplay_payload()),
                ScreenplayReview.model_validate(approved_review()),
            ]
            prompts = []

            async def fake_agent_runner(agent, prompt):
                prompts.append(prompt)
                return responses.pop(0)

            video_generator = Mock(return_value=root / "output" / "final.mp4")
            result = asyncio.run(
                run_content_workflow(
                    settings=settings,
                    agents=bundle,
                    agent_runner=fake_agent_runner,
                    video_generator=video_generator,
                )
            )

            video_generator.assert_called_once()
            self.assertIn("validation issues", prompts[3])
            self.assertTrue((result.run_directory / "screenplay-attempt-01-invalid.txt").is_file())
            self.assertTrue(result.screenplay_path.is_file())

    def test_invalid_reviewer_output_is_retried_with_schema_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            valid_review = ScreenplayReview.model_validate(approved_review())
            responses = [
                current_topic_opportunity(),
                "RESEARCH: Verified source-backed facts",
                Screenplay.model_validate(screenplay_payload()),
                "not valid review json",
                valid_review,
            ]
            prompts = []

            async def fake_agent_runner(agent, prompt):
                prompts.append(prompt)
                return responses.pop(0)

            video_generator = Mock(return_value=root / "output" / "final.mp4")
            result = asyncio.run(
                run_content_workflow(
                    settings=settings,
                    agents=bundle,
                    agent_runner=fake_agent_runner,
                    video_generator=video_generator,
                )
            )

            video_generator.assert_called_once()
            self.assertIn("Correct the validation issues", prompts[4])
            self.assertTrue((result.run_directory / "review-attempt-01-invalid.txt").is_file())
            self.assertEqual(result.review.decision, "approve")

    def test_approval_with_advisory_required_changes_still_generates_video(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
        bundle = AgentBundle(object(), object(), object(), object())
        responses = [
            current_topic_opportunity(),
            "RESEARCH: Verified source-backed facts",
            Screenplay.model_validate(screenplay_payload()),
            ScreenplayReview.model_validate(
                approved_review(required_changes=["Confirm stock metrics before publication."])
            ),
        ]

        async def fake_agent_runner(agent, prompt):
            return responses.pop(0)

        video_generator = Mock(return_value=root / "output" / "final.mp4")
        result = asyncio.run(
            run_content_workflow(
                settings=settings,
                agents=bundle,
                agent_runner=fake_agent_runner,
                video_generator=video_generator,
            )
        )

        video_generator.assert_called_once()
        self.assertEqual(result.video_path, video_generator.return_value)

    def test_reviewer_revision_is_rewritten_and_reviewed_before_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            revised = screenplay_payload()
            revised["video_title"] = "Revised Story"
            responses = iter([
                current_topic_opportunity(), "RESEARCH: Verified source-backed facts",
                screenplay_payload(), approved_review("revise"), revised, approved_review(),
            ])
            prompts = []

            async def runner(agent, prompt):
                prompts.append((agent, prompt))
                return next(responses)

            generator = Mock(return_value=root / "final.mp4")
            result = asyncio.run(run_content_workflow(settings=settings, agents=bundle, agent_runner=runner, video_generator=generator))
            generator.assert_called_once()
            self.assertEqual(result.review.decision, "approve")
            self.assertEqual(json.loads(result.screenplay_path.read_text(encoding="utf-8"))["video_title"], "Revised Story")
            self.assertIn("Strengthen the opening hook", prompts[4][1])
            self.assertIn("Verified source-backed facts", prompts[5][1])
            self.assertTrue((result.run_directory / "editorial-review-01.json").is_file())
            self.assertTrue((result.run_directory / "editorial-screenplay-01.json").is_file())

    def test_reviewer_revision_exhaustion_never_renders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            responses = iter([
                current_topic_opportunity(), "RESEARCH: Verified source-backed facts",
                screenplay_payload(), approved_review("revise"),
                *[item for _ in range(5) for item in (screenplay_payload(), approved_review("revise"))],
            ])
            calls = []

            async def runner(agent, prompt):
                calls.append(agent)
                return next(responses)

            generator = Mock()
            result = asyncio.run(run_content_workflow(settings=settings, agents=bundle, agent_runner=runner, video_generator=generator))
            generator.assert_not_called()
            self.assertIsNone(result.video_path)
            self.assertEqual(result.review.decision, "revise")
            self.assertEqual(calls.count(bundle.screenplay_writer), 6)
            self.assertEqual(calls.count(bundle.reviewer), 6)
            self.assertTrue((result.run_directory / "editorial-review-05.json").is_file())
            self.assertTrue((result.run_directory / "editorial-screenplay-05.json").is_file())

    def test_fifth_editorial_revision_can_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            responses = iter([
                current_topic_opportunity(), "RESEARCH: Verified source-backed facts",
                screenplay_payload(), approved_review("revise"),
                *[item for _ in range(4) for item in (screenplay_payload(), approved_review("revise"))],
                screenplay_payload(), approved_review(),
            ])

            async def runner(agent, prompt):
                return next(responses)

            generator = Mock(return_value=root / "final.mp4")
            result = asyncio.run(run_content_workflow(settings=settings, agents=bundle, agent_runner=runner, video_generator=generator))
            generator.assert_called_once()
            self.assertEqual(result.review.decision, "approve")
            self.assertTrue((result.run_directory / "editorial-screenplay-05.json").is_file())

    def test_invalid_revision_is_retried_without_reviewing_or_rendering_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            responses = iter([
                current_topic_opportunity(), "RESEARCH: Verified source-backed facts",
                screenplay_payload(), approved_review("revise"), screenplay_payload(scene_count=6),
                screenplay_payload(), approved_review(),
            ])
            prompts = []

            async def runner(agent, prompt):
                prompts.append(prompt)
                return next(responses)

            generator = Mock(return_value=root / "final.mp4")
            result = asyncio.run(run_content_workflow(settings=settings, agents=bundle, agent_runner=runner, video_generator=generator))
            generator.assert_called_once()
            self.assertIn("got 6", prompts[5])
            self.assertTrue((result.run_directory / "editorial-screenplay-01-invalid.json").is_file())

    def test_reviewer_reject_is_not_rewritten(self) -> None:
        result, video_generator = self.run_workflow("reject")
        video_generator.assert_not_called()
        self.assertIsNone(result.video_path)
        self.assertEqual(result.review.decision, "reject")

    def test_revision_with_factual_blocker_never_renders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            blocked = approved_review()
            blocked["factual_issues"] = ["Unsupported claim"]
            responses = iter([
                current_topic_opportunity(), "RESEARCH: Verified source-backed facts",
                screenplay_payload(), approved_review("revise"), screenplay_payload(), blocked,
            ])

            async def runner(agent, prompt):
                return next(responses)

            generator = Mock()
            result = asyncio.run(run_content_workflow(settings=settings, agents=bundle, agent_runner=runner, video_generator=generator))
            generator.assert_not_called()
            self.assertEqual(result.review.factual_issues, ["Unsupported claim"])
            self.assertIsNone(result.video_path)

    def test_wrong_scene_count_is_revised_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            responses = [
                current_topic_opportunity(),
                "RESEARCH: Verified source-backed facts",
                Screenplay.model_validate(screenplay_payload(scene_count=6)),
                Screenplay.model_validate(screenplay_payload(scene_count=5)),
                ScreenplayReview.model_validate(approved_review()),
            ]
            prompts = []

            async def fake_agent_runner(agent, prompt):
                prompts.append(prompt)
                return responses.pop(0)

            video_generator = Mock(return_value=root / "output" / "final.mp4")
            result = asyncio.run(
                run_content_workflow(
                    settings=settings,
                    agents=bundle,
                    agent_runner=fake_agent_runner,
                    video_generator=video_generator,
                )
            )
            video_generator.assert_called_once()
            revised_screenplay = Screenplay.model_validate_json(result.screenplay_path.read_text(encoding="utf-8"))
            self.assertEqual(len(revised_screenplay.scenes), 5)
            self.assertIn("4-5 scenes/images", prompts[3])
            self.assertTrue((result.run_directory / "screenplay-attempt-01.json").is_file())
            self.assertTrue((result.run_directory / "screenplay-attempt-02.json").is_file())

    def test_narration_above_preference_is_allowed_if_reviewer_approves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            too_long = screenplay_payload(scene_count=5, words_per_scene=10)
            responses = [
                current_topic_opportunity(),
                "RESEARCH: Verified source-backed facts",
                Screenplay.model_validate(too_long),
                ScreenplayReview.model_validate(approved_review()),
            ]
            prompts = []

            async def fake_agent_runner(agent, prompt):
                prompts.append(prompt)
                return responses.pop(0)

            video_generator = Mock(return_value=root / "output" / "final.mp4")
            result = asyncio.run(
                run_content_workflow(
                    settings=settings,
                    agents=bundle,
                    agent_runner=fake_agent_runner,
                    video_generator=video_generator,
                )
            )
            video_generator.assert_called_once()
            self.assertEqual(len(prompts), 4)
            revised_screenplay = Screenplay.model_validate_json(result.screenplay_path.read_text(encoding="utf-8"))
            self.assertGreater(sum(len(scene.voiceover.split()) for scene in revised_screenplay.scenes), 48)

    def test_invalid_scene_count_after_retries_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(Settings.from_env(project_root=root), output_directory=root / "output")
            bundle = AgentBundle(object(), object(), object(), object())
            invalid_scene_count = Screenplay.model_validate(screenplay_payload(scene_count=6))
            responses = [
                current_topic_opportunity(),
                "RESEARCH: Verified source-backed facts",
                invalid_scene_count,
                invalid_scene_count,
                invalid_scene_count,
            ]

            async def fake_agent_runner(agent, prompt):
                return responses.pop(0)

            video_generator = Mock()
            with self.assertRaisesRegex(ValueError, "after 2 revision attempts.*Must have 4-5 scenes/images"):
                asyncio.run(
                    run_content_workflow(
                        settings=settings,
                        agents=bundle,
                        agent_runner=fake_agent_runner,
                        video_generator=video_generator,
                    )
                )
            video_generator.assert_not_called()
            video_generator.assert_not_called()

    def test_trend_validator_requires_recent_dated_urls(self) -> None:
        today = datetime.now().astimezone().date()
        _validate_trend_evidence(
            "TREND EVIDENCE:\n- " + today.isoformat() + " — current report\n"
            "SOURCES:\n- https://example.com/new\n",
            today=today,
        )
        stale_date = (today - timedelta(days=30)).isoformat()
        with self.assertRaisesRegex(ValueError, "stale or future-dated"):
            _validate_trend_evidence(
                f"TREND EVIDENCE:\n- Old report dated {stale_date}.\nSOURCES:\n- https://example.com/old\n",
                today=today,
            )
        with self.assertRaisesRegex(ValueError, "dated source URLs"):
            _validate_trend_evidence("TOPIC: Evergreen idea with no trend proof", today=today)

    def test_trend_validator_accepts_dated_bullets_with_urls_in_sources_section(self) -> None:
        today = datetime.now().astimezone().date()
        recent_date = today.isoformat()
        opportunity = (
            "TREND EVIDENCE:\n"
            f"- News report published {recent_date} describes a current trend.\n\n"
            "SOURCES:\n"
            "- https://example.com/news-report\n"
        )
        _validate_trend_evidence(opportunity, today=today)

    def test_trend_validator_accepts_english_date_and_url_date(self) -> None:
        today = datetime.now().astimezone().date()
        published = today.strftime("%B ") + str(today.day) + today.strftime(", %Y")
        opportunity = (
            "TREND EVIDENCE:\n"
            f"- Report published on {published} documents a current surge.\n"
            f"- The dated primary source is https://example.com/{today:%Y/%m/%d}/story\n\n"
            "SOURCES:\n- https://example.com/source\n"
        )
        _validate_trend_evidence(opportunity, today=today)

    def test_trend_validator_rejects_vague_date_with_actionable_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit publication/event date"):
            _validate_trend_evidence(
                "TREND EVIDENCE:\n- The topic resurfaced in late 2026.\n"
                "SOURCES:\n- https://example.com/story\n",
                today=datetime.now().astimezone().date(),
            )

    def test_two_recent_trend_signals_allow_separate_source_urls(self) -> None:
        today = datetime.now().astimezone().date()
        first_date = (today - timedelta(days=9)).isoformat()
        second_date = (today - timedelta(days=12)).isoformat()
        opportunity = (
            "TREND EVIDENCE:\n"
            f"- First continuing signal dated {first_date}.\n"
            f"- Second independent signal dated {second_date}.\n\n"
            "SOURCES:\n- https://example.com/one\n- https://example.com/two\n"
        )
        _validate_trend_evidence(opportunity, today=today)

    def test_approval_property_requires_threshold_and_no_factual_blockers(self) -> None:
        review = ScreenplayReview.model_validate(approved_review())
        self.assertTrue(review.approves_video_generation)
        self.assertTrue(review.model_copy(update={"overall_score": 6.0}).approves_video_generation)
        self.assertTrue(review.model_copy(update={"overall_score": 6.8}).approves_video_generation)
        low_score = review.model_copy(update={"overall_score": 5.99})
        critical = review.model_copy(update={"critical_failure": True})
        unresolved_fact = review.model_copy(update={"factual_issues": ["Claim needs verification."]})
        required_revision = review.model_copy(update={"required_changes": ["Clarify the claim."]})
        self.assertFalse(low_score.approves_video_generation)
        self.assertFalse(review.model_copy(update={"decision": "revise", "overall_score": 6.8}).approves_video_generation)
        self.assertFalse(critical.approves_video_generation)
        self.assertFalse(unresolved_fact.approves_video_generation)
        self.assertTrue(required_revision.approves_video_generation)


if __name__ == "__main__":
    unittest.main()