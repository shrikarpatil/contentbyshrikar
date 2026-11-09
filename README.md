# InfluencerAI

A lightweight, incremental pipeline for turning structured screenplays into vertical short-form videos. The existing topic, research, screenplay, and review agents remain separate from media generation.

## Current status

Running `main.py` with no arguments executes the integrated workflow: trending-topic discovery, web research, structured screenplay generation, structured quality review, and video generation only if approved. The primary audience is in India: topic discovery prioritizes India-relevant trends and sources, while global stories need a concrete, evidence-based connection to Indian viewers. Narration remains English by default, without assuming one Indian language or culture. Topic selection verifies current interest using dated sources (prefer the last 7 days; up to 14 days only with continuing momentum), tracks the last 50 topics to avoid repeats, and asks the agent to vary across appropriate genres. Agent calls retry transient network, throttling, and server failures; invalid screenplay and topic candidates receive bounded corrective retries. Screenplays use four or five richly composed photorealistic images with zoom/pan camera motion. Aim for under 30 seconds and about 48 narration words, but longer scripts/videos are allowed when needed for a clear, natural, complete story. The writer retries hard format issues (such as the number of scene images), but does not reject a screenplay just for crossing the preferred runtime or word count. Agent artifacts (topic, research, screenplay JSON, review, and video path) are saved under `output/content-runs/`; the persistent `output/topic-history.json` is initialized from existing run artifacts.

Topic discovery makes up to six attempts, retaining rejected candidates and their reasons in later search prompts and exploring different genre groups. If no fresh, sufficiently dated candidate is found, the workflow stops rather than publishing a repeated or unverified topic; additional searches may incur API charges.

The local image provider creates offline stylized artwork. For the requested photorealistic cinematic images, select OpenAI image generation (`--image-provider openai`); image API usage may incur charges. Windows SAPI is the free local narration option; for a more natural neural voice, enable OpenAI TTS with `TTS_PROVIDER=openai` and `TTS_VOICE=marin` (billable API usage). Screenplay-based caption timing works without transcription downloads; optional Faster-Whisper enables speech-derived timestamps.

## Setup

Use Python 3.14 as specified by `.python-version` and install the project with `uv sync`. Copy `.env.example` to `.env` and set `OPENAI_API_KEY` to run the four-agent workflow; agent, speech, and image API calls can incur charges. No API key is required for the local sample-video command when both image and TTS providers are local. OpenAI image generation and speech synthesis are separately configurable. Never commit `.env`.

FFmpeg is bundled via `imageio-ffmpeg`; an installed FFmpeg on PATH or `FFMPEG_PATH` can be used instead. Local voice synthesis uses Windows SAPI, so the default TTS provider requires Windows.

## Commands

Run the complete topic-to-video workflow. Video generation is gated on an explicit reviewer approval with a score of at least 7/10 and no critical failure:

```powershell
uv run python main.py
```

Run the bundled local sample without any agent/API calls, or render a manually approved screenplay JSON directly:

```powershell
uv run python main.py --sample
uv run python main.py --screenplay path/to/approved-screenplay.json
```

The final video is written under `output/<timestamp>-<title>/final.mp4` by default. Change the output path with `OUTPUT_DIRECTORY` in `.env` or `--output-dir`. To opt into paid OpenAI image generation, the default image model is `gpt-image-2.6-flare`; a retired `gpt-image-1` value in an older `.env` is automatically migrated. Image model API access may require organization verification:

```powershell
uv run python main.py --image-provider openai
uv run python main.py --image-provider openai --image-model gpt-image-2.6-flare
uv run python main.py --tts-provider openai --tts-voice marin
```

OpenAI's current speech guide recommends `marin` or `cedar`; `TTS_INSTRUCTIONS` can steer delivery. Speech output is AI-generated: a `voice-disclosure.txt` reminder is saved with each OpenAI-voice run, and viewers should be clearly informed when publishing. OpenAI's current docs also announce the `gpt-4o-mini-tts` Speech API model is scheduled for shutdown on **2027-01-06**, so plan a migration before then.

To validate or prepare a screenplay without generating a video, use:

```powershell
uv run influencerai validate input/screenplays/sample.json
uv run influencerai prepare input/screenplays/sample.json
```

Captions use a smaller font and sit above the separate on-screen scene text. The bundled audio catalog is stored entirely under [assets](assets/README.md): three mood-tagged Mixkit music tracks and four licensed transition/whoosh effects, with source/license notes alongside them. The pipeline picks music to match the screenplay mood, keeps positive soundtrack settings between 0.12 and 0.18 gain, and ducks it under narration; new screenplays default to 0.14. Set volume to zero to omit music. It also resolves exact effect stems. If the music folder is empty, it downloads the curated Mixkit fallback. Set `AUTO_DOWNLOAD_MUSIC=false` to disable that fallback; if download fails, the video continues without music. Set `TRANSCRIPTION_PROVIDER=whisper` and install `uv sync --extra transcription` to use local Faster-Whisper (model download on first run). With its default `screenplay` setting, caption timing is estimated from narration text, with no model download.

Run the unit tests:

```powershell
uv run python -m unittest discover -s tests -v
```

## Instagram Reels publishing

Instagram publishing is opt-in. The integration uploads the generated MP4 directly through Meta's resumable Reels API; it does not require public video hosting. YouTube publishing remains separate and unchanged.

Newly rendered MP4s use AAC audio at 48 kHz with a 112 kbps target to leave headroom below Instagram's 128 kbps specification. Existing MP4s are not converted when uploaded; re-render or re-encode older files if they exceed Instagram's audio specifications.

The hashtag researcher creates separate YouTube and Instagram lists from current web results. Instagram tags are saved as `instagram-hashtags.txt` and take priority in the Reel caption; older runs fall back to `hashtags.txt`.

Before publishing:

1. Use an Instagram **Professional** account (Business or Creator) connected to a Facebook Page.
2. Create/configure a Meta developer app with the Instagram Graph API and Facebook Login. Enable the permissions required to read the professional account and publish media, including `instagram_basic`, `instagram_content_publish`, and `pages_read_engagement`. Permission access and review requirements depend on the app and who will use it; accounts outside the app's roles generally require Advanced Access and App Review.
3. Generate an access token with the required permissions and obtain the numeric Instagram Professional Account ID for the linked Page. Keep the token private and do not paste it into source control or chat.

For local use, add these values to `.env`:

```dotenv
INSTAGRAM_ACCESS_TOKEN=your-token
INSTAGRAM_USER_ID=your-numeric-account-id
INSTAGRAM_GRAPH_API_VERSION=v27.0
```

Publish the approved video from a normal workflow run with `--instagram-upload`. To publish an existing MP4 without generating new content, use `--instagram-upload-video`; its directory must also contain the matching `screenplay.json` (and optionally `instagram-hashtags.txt` or `hashtags.txt`):

```powershell
uv run python main.py --instagram-upload
uv run python main.py --instagram-upload-video output/path/to/final.mp4
```

To publish a generated video to both platforms, use `--upload --instagram-upload`. To publish an existing video to both, pass the same MP4 to `--upload-video` and `--instagram-upload-video`. When both targets are selected, the uploads run concurrently. Instagram results are saved beside the MP4 as `instagram-media-id.txt` and, when available, `instagram-url.txt`.

For GitHub Actions, add `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_USER_ID` as secrets in the `Dev` environment. A manual workflow run can publish by checking **Publish to Instagram Reels**. Scheduled runs do not publish to Instagram unless you explicitly add the Actions repository variable `INSTAGRAM_UPLOAD_ENABLED` with value `true`; optionally set `INSTAGRAM_GRAPH_API_VERSION` as a variable to override `v27.0`. Test with a manual run before enabling scheduled publishing.

If the token is rejected as malformed, replace `INSTAGRAM_ACCESS_TOKEN` with the access token alone on one line, without spaces, line breaks, quotes, or a `Bearer` prefix. Do not share the token in logs or chat.

For upload troubleshooting, GitHub Actions logs sanitized request and response diagnostics. To enable the same diagnostics locally, set `INSTAGRAM_UPLOAD_DIAGNOSTICS=1` in your shell before running the upload. The logs include the video size and SHA-256 fingerprint, operating system, Python version, configured proxy presence, request metadata, HTTP status, and selected response-header indicators; they do not include the access token, proxy URLs, or video contents. Matching fingerprints confirm the same video was used. The workflow does not retain the MP4 in its artifacts. Uploading an existing video with `--instagram-upload-video` publishes it if successful.

For photorealistic images, select the OpenAI image provider; the offline local provider remains a stylized illustration fallback. OpenAI image generation may incur charges depending on model and settings; set `IMAGE_COST_PER_IMAGE_USD` to record an estimate in the run manifest.

## Deploying on GitHub Actions

`.github/workflows/shorts.yml` schedules runs at 12:13 and 18:43 IST (`43 6 * * *` and `13 13 * * *` UTC) and can also be started manually from the Actions tab. GitHub may delay scheduled runs during Actions load; confirm the workflow is on the default branch and Actions are enabled. If a scheduled run is missing, use **Actions → Generate and upload Short → Run workflow**.

Add these secrets to the `Dev` environment (Settings -> Environments -> Dev -> Environment secrets),
or as repository secrets (Settings -> Secrets and variables -> Actions):

- `OPENAI_API_KEY`
- `YOUTUBE_TOKEN_JSON`: the full contents of `credentials/youtube_token.json` created by `uv run python main.py --youtube-auth`
- `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_USER_ID` (only needed when publishing Reels)

Keep the Google OAuth app in **Production** so the refresh token does not expire after 7 days. Topic history is kept between runs with the Actions cache; run artifacts (JSON/text only) are kept for 7 days.
