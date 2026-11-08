"""Command-line entry point for screenplay validation and run preparation."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from influencerai.config.settings import Settings
from influencerai.schemas.screenplay import load_screenplay
from influencerai.utils.file_manager import create_run_directory
from influencerai.utils.logging import configure_run_logger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="influencerai", description="Prepare a short-form video generation run.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("validate", "Validate a screenplay JSON file."),
        ("prepare", "Validate a screenplay and create its working directory."),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("screenplay", type=Path, help="Path to screenplay JSON.")
        if command == "prepare":
            command_parser.add_argument("--output-dir", type=Path, help="Override OUTPUT_DIRECTORY.")
    return parser


def main() -> int:
    """Run the command-line application and return a process exit code."""
    args = _build_parser().parse_args()
    try:
        screenplay = load_screenplay(args.screenplay)
        if args.command == "validate":
            print(f"Valid screenplay: {screenplay.video_title} ({len(screenplay.scenes)} scenes)")
            return 0

        settings = Settings.from_env()
        output_root = args.output_dir or settings.output_directory
        run_directory = create_run_directory(screenplay, output_root)
        logger = configure_run_logger(run_directory)
        logger.info("Prepared run for %d scenes; media generation is not yet configured.", len(screenplay.scenes))
        print(f"Run directory: {run_directory}")
        return 0
    except (OSError, ValueError, ValidationError) as exc:
        logging.getLogger("influencerai").error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())