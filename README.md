# clipforge

## Quick start
```powershell
copy .env.example .env        # then put your Anthropic API key in .env
python -m clipforge serve     # open http://localhost:8000 — paste a YouTube link or upload a video
```
Or from the command line:
```powershell
python -m clipforge run "https://youtu.be/VIDEO_ID" -o out --length 50-60
```

## About

Long video → the best moments as scored, vertical, motion-captioned short clips.
A self-hosted take on the pipeline Kaleidly runs (see `EVIDENCE.md`):

```
transcribe ─▶ hook-finder ─▶ clip-cutter ─▶ captions ─▶ render
(whisper)     (Claude: Hook/   (sentence-     (word-timed    (ffmpeg: face-tracked 9:16,
 word times)   Flow/Value/      snapped,       ASS motion     punch-in zooms, hook card,
               Trend 0-100)     NMS-deduped)   captions)      progress bar, logo, music duck)
```

## Install
```bash
apt install ffmpeg            # or brew install ffmpeg
pip install -r requirements.txt   # + yt-dlp if you want YouTube/TikTok/IG/X links
export ANTHROPIC_API_KEY=sk-ant-...
```
Windows (PowerShell), from this folder (the one containing `requirements.txt`):
```powershell
winget install Gyan.FFmpeg                    # then open a new terminal so ffmpeg is on PATH
python -m pip install -r requirements.txt     # use `python -m pip` if pip.exe says "Access is denied"
```
Put your key in a `.env` file in this folder (one line: `ANTHROPIC_API_KEY=<your real key>`).
A key set in the terminal overrides `.env`; clear it with `Remove-Item Env:ANTHROPIC_API_KEY`.
(`npm install ffmpeg` / `pip install ffmpeg` install unrelated packages, not the ffmpeg binary.)

## Use
```bash
python -m clipforge run episode.mp4 -o out/ --length auto --max-clips 6   # or --length 50-60 (any MIN-MAX seconds)
python -m clipforge template > brand.json           # edit fonts/colours/animation/logo/music
python -m clipforge run episode.mp4 -o out/ --template brand.json
python -m clipforge rerender out/ clip_02 --hook "New headline" --trim 61.2 95.0
python -m clipforge serve --port 8000               # web dashboard at http://localhost:8000 + HTTP API
```
Output: `out/clip_XX.mp4`, `clip_XX.jpg` thumbnail, `clip_XX.ass` (editable captions) and `clips.json`
with score, grade breakdown, why-this-clip, hook + alt hooks, emphasis words, caption lines, social caption.

## How "finding the best part" works
1. **Transcript → sentences** with start/end, loudness rank (`e`) and speech rate (`r`).
2. **Claude** reads ~10-min chunks (90 s overlap) and returns candidates as sentence-index ranges
   with `hook / flow / value / trend` grades, a headline hook, 2 alt hooks and emphasis words
   (forced JSON via a tool schema). Model: `CLIPFORGE_MODEL` (default `claude-sonnet-4-5`).
3. **Post-processing**: snap to sentence boundaries, enforce length window
   (auto 15-60 s, short 12-30, medium 30-60, long 60-90), overall =
   0.35·hook + 0.20·flow + 0.30·value + 0.15·trend, temporal NMS, drop < 45.
   No API key → keyword/energy heuristic fallback (`scorer: "heuristic"`).

## Motion captions & graphics (brand template)
| Section | Options |
|---|---|
| layout | `9:16 / 1:1 / 16:9`; `fill` = smart vertical reframing: camera cuts are detected, shots with people get a crop that smoothly follows them (keeps two people together, locks off when still), shots without people (b-roll, cars, graphics) show the whole frame over a blurred copy; `fit` = whole frame over blur everywhere |
| captions | font, weight, s/m/l, position %, text/highlight/outline colours, `none/shadow/box`, words on screen, uppercase, animation `none / word / karaoke / pop`, LLM emphasis words |
| headline | hook as `bold` (default: big outlined text, numbers + key words highlighted, pops in) or `card / band / plain`; font, colours, top/bottom, size, seconds on screen |
| motion | progress bar, punch-in zoom on alternate sentences |
| logo / intro & outro / music | overlay PNG; prepend/append video or image; music bed sidechain-ducked under voice |

## Publishing (YouTube Shorts + Instagram Reels)
Claude writes the YouTube title / description / tags and the Instagram caption + hashtags from what is
said in each clip; every clip also gets a clean cover (the hook on the sharpest early frame, no captions).
Publish from the dashboard (per clip, or select several and schedule one per day) or from the CLI.

| | Setup |
|---|---|
| YouTube | Google Cloud project → enable **YouTube Data API v3** → OAuth client "Desktop app" → save as `credentials/client_secret.json`. On the OAuth consent screen add your Google account as a **test user**. Then click **Connect YouTube** in the dashboard (or `python -m clipforge connect youtube`). |
| Instagram | Professional (Creator/Business) account; Meta app with *Instagram API with Instagram login*; add `INSTAGRAM_ACCESS_TOKEN=…` to `.env`. Videos are uploaded straight from disk (resumable upload), no public hosting needed. Long-lived tokens are refreshed automatically. |

```powershell
python -m clipforge accounts                                   # what is connected
python -m clipforge publish out/ clip_01 --to youtube,instagram --privacy public
```
Limits: YouTube uploads cost 1,600 of the default 10,000 daily quota units (~6/day), and API projects that
have not passed Google's audit can only upload **private** videos. Instagram allows 100 API posts / 24 h.
`credentials/` and `.env` are git-ignored.

## API
`POST /jobs` (file or url) → poll `GET /jobs/{id}` (per-step status) → `GET /jobs/{id}/clips` →
`GET /jobs/{id}/files/clip_01.mp4` → `POST /jobs/{id}/clips/clip_01` `{hook, trim, lines, template}` to re-render.

## Tests
`python -m pytest -q tests` (no models or network needed).

GPU: transcription uses CUDA automatically when an NVIDIA GPU is present (`CLIPFORGE_CUDA=0` forces CPU);
rendering uses the NVENC hardware encoder when available (`CLIPFORGE_NVENC=0` forces libx264).

Env: `CLIPFORGE_WHISPER` (tiny/base/small/medium/large-v3, default small), `CLIPFORGE_CUDA`, `CLIPFORGE_NVENC`,
`CLIPFORGE_MODEL`, `CLIPFORGE_SCORER=heuristic`, `CLIPFORGE_DATA` (API job dir).
