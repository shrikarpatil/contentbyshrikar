"""Structured quality review that gates downstream video generation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScoreBreakdown(BaseModel):
    """Reviewer scores for the screenplay's quality dimensions."""

    model_config = ConfigDict(extra="forbid")

    hook: float = Field(ge=0, le=10)
    storytelling: float = Field(ge=0, le=10)
    retention: float = Field(ge=0, le=10)
    visual_potential: float = Field(ge=0, le=10)
    narration: float = Field(ge=0, le=10)
    payoff: float = Field(ge=0, le=10)
    originality: float = Field(ge=0, le=10)


class ScreenplayReview(BaseModel):
    """Machine-readable review; only a clean approval may start rendering."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approve", "revise", "reject"]
    overall_score: float = Field(ge=0, le=10)
    critical_failure: bool = False
    score_breakdown: ScoreBreakdown
    strongest_elements: list[str] = Field(default_factory=list)
    biggest_problems: list[str] = Field(default_factory=list)
    retention_risks: list[str] = Field(default_factory=list)
    factual_issues: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    final_recommendation: str = Field(min_length=1)

    @property
    def approves_video_generation(self) -> bool:
        """Return true for an explicit, threshold-meeting approval without factual blockers."""
        return (
            self.decision == "approve"
            and self.overall_score >= 7
            and not self.critical_failure
            and not self.factual_issues
        )