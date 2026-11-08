"""Topic-to-approved-screenplay workflow and video-generation entry point."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from pydantic import ValidationError

from influencerai.config.settings import PROJECT_ROOT, Settings
from influencerai.pipeline.limits import (
    MAX_PRODUCTION_SCENES,
    MIN_PRODUCTION_SCENES,
    PREFERRED_NARRATION_WORDS,
    PREFERRED_VIDEO_DURATION_SECONDS,
)
from influencerai.pipeline.orchestrator import run_pipeline
from influencerai.schemas.review import ScreenplayReview
from influencerai.schemas.screenplay import Screenplay

LOGGER = logging.getLogger("influencerai.content_workflow")
PROMPTS_DIR = PROJECT_ROOT / "src" / "prompts"


@dataclass(frozen=True, slots=True)
class AgentBundle:
    """The four agents participating in one content run."""

    topic_generator: Any
    topic_researcher: Any
    screenplay_writer: Any
    reviewer: Any
    hashtag_researcher: Any = None


@dataclass(frozen=True, slots=True)
class ContentWorkflowResult:
    """Persisted agent artifacts and the approval/video result."""

    run_directory: Path
    screenplay_path: Path
    review: ScreenplayReview
    video_path: Path | None


AgentRunner = Callable[[Any, str], Awaitable[Any]]
VideoGenerator = Callable[..., Path]
MAX_SCREENPLAY_REVISIONS = 2
MAX_REVIEW_REVISIONS = 2
MAX_TOPIC_SELECTION_ATTEMPTS = 3
MAX_AGENT_RETRIES = 2
MAX_TOPIC_HISTORY = 50
TRANSIENT_AGENT_ERRORS = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
MONTH_NAMES = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
APPROPRIATE_GENRES = (
    "science and space",
    "technology and AI",
    "nature and wildlife",
    "history and archaeology",
    "culture and arts",
    "sports",
    "food and culinary discoveries",
    "travel and geography",
    "business and economics",
    "politics and civics",
    "health and medicine",
    "environment and climate",
    "architecture and design",
    "entertainment and gaming",
    "education and learning",
    "engineering and inventions",
    "human-interest and everyday ingenuity",
)
AUDIENCE_CONTEXT = (
    "The primary audience is a broad audience in India. Prefer India-relevant trending stories and local/regional sources; "
    "global stories are welcome when their relevance to Indian viewers is concrete and evidence-based. Do not force a local angle, "
    "assume a single Indian language or culture, or use stereotypes. Keep narration in English by default."
)
GENRE_KEYWORDS = {
    "politics and civics": ("election", "congress", "government", "policy", "parliament", "court", "civic"),
    "science and space": ("space", "nasa", "telescope", "planet", "scientist", "species", "ocean", "deep sea"),
    "technology and AI": ("technology", "tech", "ai ", "artificial intelligence", "robot", "software", "chip"),
    "nature and wildlife": ("wildlife", "animal", "bird", "migration", "forest", "whale", "ecosystem"),
    "history and archaeology": ("history", "ancient", "archaeology", "archaeological", "artifact", "ruins"),
    "culture and arts": ("culture", "artist", "artwork", "museum", "music", "film", "language"),
    "sports": ("sport", "football", "soccer", "olympic", "athlete", "tournament", "championship"),
    "food and culinary discoveries": ("food", "recipe", "restaurant", "chef", "culinary", "cuisine"),
    "travel and geography": ("travel", "island", "country", "city", "geography", "landmark"),
    "business and economics": ("business", "economy", "market", "stock", "company", "trade", "economics"),
    "health and medicine": ("health", "medical", "medicine", "hospital", "vaccine", "clinical"),
    "environment and climate": ("climate", "environment", "renewable", "pollution", "conservation"),
    "architecture and design": ("architecture", "architect", "design", "building", "construction"),
    "entertainment and gaming": ("entertainment", "gaming", "game", "video game", "streaming", "celebrity"),
    "education and learning": ("education", "school", "university", "student", "learning", "research"),
    "engineering and inventions": ("engineering", "invention", "inventor", "prototype", "breakthrough"),
}


def _validate_trend_evidence(topic: str, *, today: date | None = None) -> None:
    """Require recent, dated source URLs before spending on research/rendering."""
    current_date = today or datetime.now().astimezone().date()
    section = re.search(
        r"(?ims)^\s*TREND EVIDENCE\s*:\s*(.*?)(?=^\s*(?:SOURCES|CONFIDENCE)\s*:)",
        topic,
    )
    if section is None:
        raise ValueError("Topic Generator must include a TREND EVIDENCE section with dated source URLs.")

    trend_dates = _extract_trend_dates(section.group(1))

    sources_section = re.search(r"(?ims)^\s*SOURCES\s*:\s*(.*?)(?=^\s*CONFIDENCE\s*:|\Z)", topic)
    source_urls = set(re.findall(r"https?://[^\s)\]>]+", sources_section.group(1) if sources_section else ""))
    recent_signals = [
        signal_date
        for signal_date in trend_dates
        if signal_date <= current_date and (current_date - signal_date).days <= 7
    ]
    continuing_signals = [
        signal_date
        for signal_date in trend_dates
        if signal_date <= current_date and (current_date - signal_date).days <= 14
    ]
    has_recent_evidence = bool(recent_signals and source_urls)
    has_continuing_evidence = len(continuing_signals) >= 2 and len(source_urls) >= 2
    if not has_recent_evidence and not has_continuing_evidence:
        if not trend_dates:
            raise ValueError(
                "TREND EVIDENCE must include an explicit publication/event date (for example 2026-11-05 or November 5, 2026); "
                "vague dates such as 'late 2026' are not verifiable."
            )
        if not source_urls:
            raise ValueError("TREND EVIDENCE is dated, but no supporting URLs were found in the SOURCES section.")
        raise ValueError(
            "Trend evidence is stale or future-dated. Include a signal from the last 7 days with a supporting URL, "
            "or at least two signals and two URLs from the last 14 days."
        )


def _extract_trend_dates(section: str) -> list[date]:
    """Parse explicit ISO, English month-name, or URL-path dates from trend evidence."""
    date_patterns = (
        r"\b(20\d{2}-\d{2}-\d{2})\b",
        rf"\b({MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b",
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_NAMES})[,]?\s+(20\d{{2}})\b",
        r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)",
    )
    parsed_dates: list[date] = []
    for line in section.splitlines():
        for pattern_index, pattern in enumerate(date_patterns):
            for match in re.finditer(pattern, line, flags=re.IGNORECASE):
                try:
                    if pattern_index == 0:
                        parsed = date.fromisoformat(match.group(1))
                    elif pattern_index == 1:
                        parsed = datetime.strptime(
                            f"{match.group(1)} {match.group(2)} {match.group(3)}",
                            "%B %d %Y",
                        ).date()
                    elif pattern_index == 2:
                        parsed = datetime.strptime(
                            f"{match.group(2)} {match.group(1)} {match.group(3)}",
                            "%B %d %Y",
                        ).date()
                    else:
                        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                except ValueError:
                    continue
                parsed_dates.append(parsed)
    return parsed_dates


def _read_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48] or "content-run"


def _create_content_run_directory(output_root: Path) -> Path:
    root = output_root / "content-runs"
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    run_directory = root / f"{timestamp}-topic-to-video"
    run_directory.mkdir()
    return run_directory


def _topic_title(topic: str) -> str:
    lines = topic.replace("```json", "").replace("```", "").splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"(?i)^\s*TOPIC\s*:\s*(.*?)\s*$", line)
        if match is None:
            continue
        inline_title = match.group(1).strip()
        if inline_title:
            return inline_title
        for following_line in lines[index + 1 :]:
            candidate = following_line.strip()
            if candidate and not re.match(r"^[A-Z][A-Z _-]*:\s*$", candidate):
                return candidate
    return next((line.strip() for line in topic.splitlines() if line.strip()), "")


def _normalize_topic(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _topic_genre(topic: str) -> str:
    match = re.search(r"(?im)^\s*GENRE\s*:\s*(.+?)\s*$", topic)
    if match:
        return match.group(1).strip().lower()
    searchable = _topic_title(topic).lower()
    return next(
        (genre for genre, keywords in GENRE_KEYWORDS.items() if any(keyword in searchable for keyword in keywords)),
        "unspecified",
    )


def _is_repeated_topic(candidate: str, previous_topics: list[str]) -> bool:
    normalized = _normalize_topic(candidate)
    if not normalized:
        return False
    stop_words = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with", "impact", "rise"}
    candidate_words = set(normalized.split()) - stop_words
    for previous in previous_topics:
        prior = _normalize_topic(previous)
        if not prior:
            continue
        similarity = difflib.SequenceMatcher(None, normalized, prior).ratio()
        prior_words = set(prior.split()) - stop_words
        shared_words = candidate_words.intersection(prior_words)
        shared_ratio = len(shared_words) / max(1, min(len(candidate_words), len(prior_words)))
        candidate_concepts = _topic_concepts(candidate)
        prior_concepts = _topic_concepts(previous)
        shared_concepts = candidate_concepts.intersection(prior_concepts)
        same_election_ai_story = {"midterm-election", "artificial-intelligence"}.issubset(shared_concepts)
        if (
            similarity >= 0.76
            or (len(shared_words) >= 3 and shared_ratio >= 0.65)
            or same_election_ai_story
        ):
            return True
    return False


def _topic_concepts(value: str) -> set[str]:
    words = set(_normalize_topic(value).split())
    concepts: set[str] = set()
    if words.intersection({"midterm", "midterms"}) and words.intersection(
        {"election", "elections", "democrat", "democrats", "victory"}
    ):
        concepts.add("midterm-election")
    if "ai" in words or {"artificial", "intelligence"}.issubset(words):
        concepts.add("artificial-intelligence")
    return concepts


def _load_topic_history(output_root: Path) -> list[dict[str, str]]:
    """Load persisted titles, bootstrapping from older run artifacts once."""
    history_path = output_root / "topic-history.json"
    bootstrap_from_runs = not history_path.is_file()
    entries: list[dict[str, str]] = []
    if history_path.is_file():
        payload = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"Topic history must be a JSON list: {history_path}")
        entries = [
            {"topic": str(item["topic"]), "genre": str(item.get("genre", "unspecified"))}
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("topic"), str) and item["topic"].strip()
        ]

    known_topics = {_normalize_topic(item["topic"]) for item in entries}
    run_root = output_root / "content-runs"
    if bootstrap_from_runs and run_root.is_dir():
        for artifact in sorted(run_root.glob("*/topic-generator-output.md")):
            raw_topic = artifact.read_text(encoding="utf-8")
            title = _topic_title(raw_topic)
            normalized = _normalize_topic(title)
            if normalized and normalized not in known_topics:
                repeated_index = next(
                    (
                        index
                        for index, entry in enumerate(entries)
                        if _is_repeated_topic(title, [entry["topic"]])
                    ),
                    None,
                )
                if repeated_index is not None:
                    entries.pop(repeated_index)
                entries.append({"topic": title, "genre": _topic_genre(raw_topic)})
                known_topics = {_normalize_topic(item["topic"]) for item in entries}
    return entries[-MAX_TOPIC_HISTORY:]


def _save_topic_history(output_root: Path, entries: list[dict[str, str]]) -> None:
    history_path = output_root / "topic-history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = history_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(entries[-MAX_TOPIC_HISTORY:], indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(history_path)


async def _invoke_with_retries(
    invoke: AgentRunner,
    agent: Any,
    prompt: str,
    *,
    retryable_errors: tuple[type[Exception], ...] = TRANSIENT_AGENT_ERRORS,
    retry_delay_seconds: float = 1.0,
) -> Any:
    """Retry only transient transport, throttling, and server failures."""
    for attempt in range(MAX_AGENT_RETRIES + 1):
        try:
            return await invoke(agent, prompt)
        except retryable_errors as exc:
            if attempt >= MAX_AGENT_RETRIES:
                raise
            delay = retry_delay_seconds * (2**attempt)
            LOGGER.warning(
                "Transient agent failure (%s); retrying attempt %d/%d in %.1fs.",
                type(exc).__name__,
                attempt + 2,
                MAX_AGENT_RETRIES + 1,
                delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Agent retry loop exited unexpectedly.")


def create_agent_bundle(settings: Settings) -> AgentBundle:
    """Create OpenAI Agents SDK agents with typed screenplay/review outputs."""
    if not settings.openai_api_key and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY in .env to run the topic-to-video agent workflow.")
    try:
        from agents import Agent, WebSearchTool
    except ImportError as exc:
        raise RuntimeError("The OpenAI Agents SDK is not installed; run `uv sync` first.") from exc

    writer_instructions = (
        _read_prompt("screenplay-writer.txt")
        + "\n\n"
        + "PRODUCTION JSON CONTRACT: Return exactly the screenplay object required by the structured output schema. "
        + "Do not return Markdown, a screenplay template, or extra fields. Create 4 or 5 scenes/images. "
        + "Prefer about 26 seconds and around 48 narration words, but longer scripts are allowed when needed for a clear, complete story. "
        + "Set background_music.volume between 0.12 and 0.18 so the bed remains audible but below narration. "
        + "Each scene must have concise natural voiceover, a rich photorealistic documentary image_prompt, "
        + "one supported motion value, optional short on_screen_text, an optional sound_effect asset stem, and a supported transition. "
        + "Use aspect_ratio 9:16. Make each single generated image a detailed, realistic, camera-movable documentary frame. "
        + "The narration and image prompts must remain grounded in the supplied research."
    )
    reviewer_instructions = (
        _read_prompt("reviewer.txt")
        + "\n\n"
        + "STRUCTURED REVIEW CONTRACT: Return the schema fields exactly. Use decision values approve, revise, or reject in lowercase. "
        + "Approve only when the weighted score is at least 7.0 and there is no critical factual or logic failure. "
        + "Set critical_failure=true for any critical failure listed in the review instructions. "
        + "A revise/reject decision must never be treated as approved for video generation. "
        + "When decision is approve, required_changes must be empty; put non-blocking suggestions in biggest_problems instead. "
        + "Assess factual claims against the supplied research and note missing or unsupported claims. "
        + "Require 4-5 scenes, but treat the sub-30-second target and roughly 48 narration words as preferences, not hard gates. "
        + "Do not reject solely for a modest runtime or word-count overrun when the story remains engaging and complete."
    )
    return AgentBundle(
        topic_generator=Agent(
            name="Topic Generator",
            instructions=_read_prompt("topic-generation.txt"),
            tools=[WebSearchTool()],
            model=settings.openai_model,
        ),
        topic_researcher=Agent(
            name="Topic Researcher",
            instructions=_read_prompt("topic-researcher.txt"),
            tools=[WebSearchTool()],
            model=settings.openai_model,
        ),
        screenplay_writer=Agent(
            name="Screenplay Writer",
            instructions=writer_instructions,
            output_type=Screenplay,
            model=settings.openai_model,
        ),
        reviewer=Agent(
            name="Reviewer",
            instructions=reviewer_instructions,
            output_type=ScreenplayReview,
            model=settings.openai_model,
        ),
        hashtag_researcher=Agent(
            name="Hashtag Researcher",
            instructions=(
                "You find currently trending YouTube Shorts hashtags. Use web search to see which hashtags are being used "
                "right now for the given topic, especially by Indian creators and audiences. Return ONLY 5 to 7 hashtags "
                "on one line separated by spaces, each starting with #. Include at least one India-relevant tag. "
                "Exclude #Shorts, #fyp and #viral. Never invent obscure or unrelated tags."
            ),
            tools=[WebSearchTool()],
            model=settings.openai_model,
        ),
    )


async def _run_agent(agent: Any, prompt: str) -> Any:
    """Run one agent without exposing request credentials in logs."""
    from agents import Runner

    result = await Runner.run(agent, prompt)
    return result.final_output


def _validated_screenplay(value: Any) -> Screenplay:
    if isinstance(value, Screenplay):
        return value
    if isinstance(value, str):
        return Screenplay.model_validate_json(value)
    return Screenplay.model_validate(value)


def _validated_review(value: Any) -> ScreenplayReview:
    if isinstance(value, ScreenplayReview):
        return value
    if isinstance(value, str):
        return ScreenplayReview.model_validate_json(value)
    return ScreenplayReview.model_validate(value)


async def run_content_workflow(
    *,
    settings: Settings | None = None,
    output_directory: Path | None = None,
    agents: AgentBundle | None = None,
    agent_runner: AgentRunner | None = None,
    video_generator: VideoGenerator | None = None,
) -> ContentWorkflowResult:
    """Discover, research, write, review, then render only an approved screenplay."""
    settings = settings or Settings.from_env()
    if output_directory is not None:
        from dataclasses import replace

        settings = replace(settings, output_directory=output_directory)
    agent_bundle = agents or create_agent_bundle(settings)
    invoke = agent_runner or _run_agent
    generate_video = video_generator or run_pipeline
    run_directory = _create_content_run_directory(settings.output_directory)

    LOGGER.info("Starting topic-to-video workflow with model %s.", settings.openai_model)
    current_date = datetime.now().astimezone().date().isoformat()
    topic_history = _load_topic_history(settings.output_directory)
    previous_topics = [entry["topic"] for entry in topic_history]
    previous_genres = [entry["genre"] for entry in topic_history[-8:] if entry["genre"] != "unspecified"]
    genre_guidance = ", ".join(APPROPRIATE_GENRES)
    recent_topics = "\n".join(f"- {title}" for title in previous_topics[-20:]) or "- None recorded yet"
    recent_genres = ", ".join(previous_genres[-6:]) or "No genre history yet"
    topic_prompt = (
        f"Today is {current_date}. Find one currently trending, high-potential faceless short-form video opportunity. "
        f"{AUDIENCE_CONTEXT} "
        "Prioritize evidence from the last 7 days (14 days only if momentum is demonstrably continuing). "
        "Verify dates and trend signals with web search; do not present evergreen stories as trending. "
        f"Explore a broad range of appropriate genres, such as {genre_guidance}; choose a well-supported trend, "
        "prefer a different genre from recent runs when another comparably strong trend exists, and do not force a genre without evidence. "
        "Keep topics suitable for a general audience: avoid graphic violence, sexual content, dangerous instructions, and unverified claims. "
        "Return the single best opportunity in the required format, including a one-line GENRE field.\n\n"
        f"RECENTLY USED TOPICS — do not repeat or lightly rephrase:\n{recent_topics}\n\n"
        f"RECENT GENRES: {recent_genres}"
    )
    topic: str | None = None
    topic_failures: list[str] = []
    for attempt in range(MAX_TOPIC_SELECTION_ATTEMPTS):
        topic_value = await _invoke_with_retries(invoke, agent_bundle.topic_generator, topic_prompt)
        if not isinstance(topic_value, str) or not topic_value.strip():
            issue = "Topic Generator returned empty or non-text output."
        else:
            topic = topic_value.strip()
            _write_text(run_directory / f"topic-generator-attempt-{attempt + 1:02d}.md", topic)
            try:
                _validate_trend_evidence(topic)
                title = _topic_title(topic)
                if _is_repeated_topic(title, previous_topics):
                    issue = f"Topic repeats or closely resembles a previous topic: {title}"
                else:
                    topic_failures = []
                    break
            except ValueError as exc:
                issue = str(exc)
        topic_failures.append(issue)
        if attempt + 1 < MAX_TOPIC_SELECTION_ATTEMPTS:
            topic_prompt = (
                "Your previous topic candidate could not be used. Find a different, currently trending opportunity, "
                "preferably in a different appropriate genre. Do not invent trend evidence. "
                f"{AUDIENCE_CONTEXT} "
                "In TREND EVIDENCE, include an explicit publication/event date in ISO YYYY-MM-DD or Month D, YYYY format "
                "on each supporting signal line, and put the corresponding source URLs in SOURCES. Avoid vague dates. "
                f"Return the required format with GENRE and dated evidence.\n\nREASON TO RESELECT:\n{issue}\n\n"
                f"RECENTLY USED TOPICS:\n{recent_topics}\n\nRECENT GENRES: {recent_genres}"
            )
    if topic is None or topic_failures:
        raise ValueError(
            f"Could not select a fresh, trend-verified topic after {MAX_TOPIC_SELECTION_ATTEMPTS} attempts: "
            + "; ".join(topic_failures)
        )
    _write_text(run_directory / "topic-generator-output.md", topic)
    topic_history.append({"topic": _topic_title(topic), "genre": _topic_genre(topic)})
    _save_topic_history(settings.output_directory, topic_history)

    research = await _invoke_with_retries(
        invoke,
        agent_bundle.topic_researcher,
        f"Today is {current_date}. Research and verify both the facts and whether this opportunity is still trending now. "
        f"{AUDIENCE_CONTEXT} "
        "Check publication dates/current activity, preserve source URLs, and clearly label uncertainty. Do not write a screenplay.\n\n"
        f"CONTENT OPPORTUNITY:\n{topic}",
    )
    if not isinstance(research, str) or not research.strip():
        raise ValueError("Topic Researcher returned empty or non-text output.")
    _write_text(run_directory / "topic-researcher-output.md", research)

    hashtags: list[str] = []
    if agent_bundle.hashtag_researcher is not None:
        try:
            tag_output = await _invoke_with_retries(
                invoke,
                agent_bundle.hashtag_researcher,
                f"Today is {current_date}. Find the currently trending YouTube Shorts hashtags for this topic. "
                f"{AUDIENCE_CONTEXT}\n\nTOPIC:\n{_topic_title(topic)}\n\nGENRE: {_topic_genre(topic)}",
            )
            hashtags = extract_hashtags(f"HASHTAGS:\n{tag_output}") if isinstance(tag_output, str) else []
        except Exception as exc:
            LOGGER.warning("Hashtag research failed (%s); using topic-agent hashtags.", type(exc).__name__)
    hashtags = hashtags or extract_hashtags(topic)
    if hashtags:
        _write_text(run_directory / "hashtags.txt", " ".join(hashtags))

    writer_prompt = (
        "Write a production-ready, factually grounded, short-form vertical screenplay with 4 or 5 richly described photorealistic images. "
        f"{AUDIENCE_CONTEXT} "
        f"Aim for a runtime under 30 seconds (about {PREFERRED_VIDEO_DURATION_SECONDS} seconds) and around "
        f"{PREFERRED_NARRATION_WORDS} spoken words, but do not sacrifice clarity, natural narration, or story completeness to meet those targets. "
        "Longer videos are acceptable when the material needs them. Return only the structured screenplay.\n\n"
        f"CONTENT OPPORTUNITY:\n{topic}\n\nRESEARCH PACKAGE:\n{research}"
    )
    screenplay: Screenplay | None = None
    last_issues: list[str] = []
    for attempt in range(MAX_SCREENPLAY_REVISIONS + 1):
        screenplay_value = await _invoke_with_retries(invoke, agent_bundle.screenplay_writer, writer_prompt)
        try:
            screenplay = _validated_screenplay(screenplay_value)
        except ValidationError as exc:
            screenplay = None
            last_issues = [f"Invalid structured screenplay: {exc}"]
            raw_output = screenplay_value if isinstance(screenplay_value, str) else repr(screenplay_value)
            _write_text(run_directory / f"screenplay-attempt-{attempt + 1:02d}-invalid.txt", raw_output)
            if attempt >= MAX_SCREENPLAY_REVISIONS:
                break
            writer_prompt = (
                "Your prior output did not match the required structured screenplay schema. Correct the validation issues "
                "and return only a valid screenplay object. Preserve verified facts; do not add claims.\n\n"
                f"VALIDATION ISSUES:\n{exc}\n\nPREVIOUS OUTPUT:\n{raw_output}\n\n"
                f"CONTENT OPPORTUNITY:\n{topic}\n\nRESEARCH PACKAGE:\n{research}"
            )
            continue

        attempt_path = run_directory / f"screenplay-attempt-{attempt + 1:02d}.json"
        attempt_path.write_text(screenplay.model_dump_json(indent=2) + "\n", encoding="utf-8")
        scene_duration = sum(scene.duration for scene in screenplay.scenes)
        narration_words = sum(len(scene.voiceover.split()) for scene in screenplay.scenes)
        issues: list[str] = []
        if not MIN_PRODUCTION_SCENES <= len(screenplay.scenes) <= MAX_PRODUCTION_SCENES:
            issues.append(
                f"Must have {MIN_PRODUCTION_SCENES}-{MAX_PRODUCTION_SCENES} scenes/images; got {len(screenplay.scenes)}."
            )

        last_issues = issues
        if not issues:
            if scene_duration > PREFERRED_VIDEO_DURATION_SECONDS:
                LOGGER.info(
                    "Screenplay plans %.1fs (preferred target %.1fs); proceeding because longer videos are allowed.",
                    scene_duration,
                    PREFERRED_VIDEO_DURATION_SECONDS,
                )
            if narration_words > PREFERRED_NARRATION_WORDS:
                LOGGER.info(
                    "Screenplay has %d narration words (preferred around %d); prioritizing natural delivery and completeness.",
                    narration_words,
                    PREFERRED_NARRATION_WORDS,
                )
            break
        if attempt >= MAX_SCREENPLAY_REVISIONS:
            break
        LOGGER.warning(
            "Screenplay draft %d/%d misses production limits (%s); requesting a concise rewrite.",
            attempt + 1,
            MAX_SCREENPLAY_REVISIONS + 1,
            "; ".join(issues),
        )
        writer_prompt = (
            "Revise the screenplay JSON below to correct every production-limit issue. Preserve its factual claims and story, "
            "but shorten or restructure narration as needed. Do not add facts. Return only the corrected structured screenplay.\n\n"
            "PRODUCTION ISSUES:\n- "
            + "\n- ".join(issues)
            + f"\n\nCURRENT SCREENPLAY JSON:\n{screenplay.model_dump_json(indent=2)}\n\n"
            f"CONTENT OPPORTUNITY:\n{topic}\n\nRESEARCH PACKAGE:\n{research}"
        )

    if screenplay is None or last_issues:
        raise ValueError(
            f"Screenplay still violates production limits after {MAX_SCREENPLAY_REVISIONS} revision attempts: "
            + "; ".join(last_issues)
        )
    screenplay_path = run_directory / "screenplay.json"
    screenplay_path.write_text(screenplay.model_dump_json(indent=2) + "\n", encoding="utf-8")

    review_prompt = (
        "Review this screenplay strictly against the opportunity and research. Return only the structured review.\n\n"
        f"{AUDIENCE_CONTEXT}\n\n"
        f"CONTENT OPPORTUNITY:\n{topic}\n\nRESEARCH PACKAGE:\n{research}\n\n"
        f"SCREENPLAY JSON:\n{screenplay.model_dump_json(indent=2)}"
    )
    review: ScreenplayReview | None = None
    last_review_error: ValidationError | None = None
    for attempt in range(MAX_REVIEW_REVISIONS + 1):
        review_value = await _invoke_with_retries(invoke, agent_bundle.reviewer, review_prompt)
        try:
            review = _validated_review(review_value)
            break
        except ValidationError as exc:
            last_review_error = exc
            raw_output = review_value if isinstance(review_value, str) else repr(review_value)
            _write_text(run_directory / f"review-attempt-{attempt + 1:02d}-invalid.txt", raw_output)
            if attempt >= MAX_REVIEW_REVISIONS:
                break
            review_prompt = (
                "Your prior output did not match the structured review schema. Correct the validation issues and "
                "return only a valid review object. Do not change the screenplay or invent new evidence.\n\n"
                f"VALIDATION ISSUES:\n{exc}\n\nPREVIOUS OUTPUT:\n{raw_output}\n\n"
                f"SCREENPLAY JSON:\n{screenplay.model_dump_json(indent=2)}"
            )
    if review is None:
        raise ValueError(
            f"Reviewer returned an invalid structured review after {MAX_REVIEW_REVISIONS} revision attempts: "
            f"{last_review_error}"
        )
    (run_directory / "review.json").write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")

    if not review.approves_video_generation:
        LOGGER.warning(
            "Reviewer decision is %s (%.1f/10, critical_failure=%s); skipping video generation.",
            review.decision,
            review.overall_score,
            review.critical_failure,
        )
        return ContentWorkflowResult(run_directory, screenplay_path, review, None)

    if review.required_changes:
        LOGGER.warning(
            "Reviewer approved the screenplay but listed advisory required_changes; proceeding because there are no factual blockers: %s",
            "; ".join(review.required_changes),
        )
    LOGGER.info("Screenplay approved (%.1f/10); starting video generation.", review.overall_score)
    video_path = generate_video(
        screenplay_path,
        output_directory=settings.output_directory,
        settings=settings,
    )
    _write_text(run_directory / "video-output.txt", str(video_path.resolve()))
    return ContentWorkflowResult(run_directory, screenplay_path, review, video_path)


def extract_hashtags(topic: str, limit: int = 6) -> list[str]:
    """Pull the HASHTAGS section from the topic output."""
    match = re.search(r"(?ims)^\s*HASHTAGS\s*:\s*(.*?)(?=^\s*[A-Z ]+:\s*$|\Z)", topic)
    if not match:
        return []
    seen: list[str] = []
    for tag in re.findall(r"#\w+", match.group(1)):
        if tag.lower() not in {"#shorts"} and tag.lower() not in {t.lower() for t in seen}:
            seen.append(tag)
    return seen[:limit]


def main() -> int:
    """Run the integrated agents by default, or render an explicitly supplied screenplay/sample."""
    parser = argparse.ArgumentParser(description="Create a reviewed faceless short-form video.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--screenplay", type=Path, help="Render a supplied production-ready screenplay JSON directly.")
    source.add_argument("--sample", action="store_true", help="Render the bundled sample screenplay without calling agents.")
    parser.add_argument("--output-dir", type=Path, help="Override OUTPUT_DIRECTORY.")
    parser.add_argument("--upload", action="store_true", help="Upload the approved video to YouTube.")
    parser.add_argument(
        "--youtube-privacy",
        choices=("private", "unlisted", "public"),
        default=None,
        help="Privacy for uploads (default: YOUTUBE_PRIVACY or public).",
    )
    parser.add_argument("--youtube-auth", action="store_true", help="Run the one-time YouTube login and exit.")
    parser.add_argument("--upload-video", type=Path, help="Upload an existing final.mp4 (uses screenplay.json beside it).")
    parser.add_argument(
        "--image-provider",
        choices=("local", "openai"),
        help="Override IMAGE_PROVIDER; OpenAI image requests are billed.",
    )
    parser.add_argument(
        "--image-model",
        help="Override OPENAI_IMAGE_MODEL (current default: gpt-image-2.6-flare).",
    )
    parser.add_argument(
        "--tts-provider",
        choices=("local", "openai"),
        help="Override TTS_PROVIDER; OpenAI speech requests are billed.",
    )
    parser.add_argument(
        "--tts-voice",
        help="Override TTS_VOICE (e.g. marin, cedar, coral, or a Windows SAPI voice name).",
    )
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if args.output_dir is not None:
            from dataclasses import replace

            settings = replace(settings, output_directory=args.output_dir)
        if args.image_provider or args.image_model:
            from dataclasses import replace

            settings = replace(
                settings,
                image_provider=args.image_provider or settings.image_provider,
                openai_image_model=args.image_model or settings.openai_image_model,
            )
        if args.tts_provider or args.tts_voice:
            from dataclasses import replace

            settings = replace(
                settings,
                tts_provider=args.tts_provider or settings.tts_provider,
                tts_voice=args.tts_voice or settings.tts_voice,
            )

        import os

        from influencerai.services import youtube_service as yt

        secret = yt.find_client_secret(PROJECT_ROOT)
        yt_settings = yt.YouTubeSettings(
            client_secret_path=secret or PROJECT_ROOT / "client_secret.json",
            token_path=PROJECT_ROOT / "credentials" / "youtube_token.json",
            privacy_status=args.youtube_privacy or os.getenv("YOUTUBE_PRIVACY", "public").lower(),
        )

        def upload(video: Path, screenplay: Path) -> None:
            tags_path = screenplay.parent / "hashtags.txt"
            hashtags = tags_path.read_text(encoding="utf-8").split() if tags_path.exists() else []
            video_id = yt.upload_video(video, yt.build_metadata(screenplay, yt_settings, hashtags=hashtags), yt_settings)
            url = f"https://youtu.be/{video_id}"
            _write_text(video.parent / "youtube-url.txt", url)
            print(f"Uploaded to YouTube ({yt_settings.privacy_status}): {url}")

        if args.youtube_auth:
            yt.get_credentials(yt_settings)
            print(f"YouTube login saved to {yt_settings.token_path}")
            return 0
        if args.upload_video:
            upload(args.upload_video, args.upload_video.parent / "screenplay.json")
            return 0

        if args.sample or args.screenplay:
            screenplay_path = args.screenplay or PROJECT_ROOT / "input" / "screenplays" / "sample.json"
            video_path = run_pipeline(screenplay_path, settings=settings)
            print(f"Video created: {video_path}")
            if args.upload:
                upload(video_path, screenplay_path)
            return 0

        result = asyncio.run(run_content_workflow(settings=settings))
        if result.video_path is None:
            print(
                f"No video generated: reviewer decision {result.review.decision.upper()} "
                f"({result.review.overall_score:.1f}/10). Review: {result.run_directory / 'review.json'}"
            )
            return 0
        print(f"Approved video created: {result.video_path}")
        print(f"Agent artifacts: {result.run_directory}")
        if args.upload:
            upload(result.video_path, result.screenplay_path)
        return 0
    except Exception as exc:
        LOGGER.exception("Content workflow failed (%s).", type(exc).__name__)
        print(f"Content workflow failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())