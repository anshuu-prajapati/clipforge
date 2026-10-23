# How Kaleidly works — evidence

Source: public homepage https://www.kaleidly.com/ and its 22 Next.js JS bundles
(`/_next/static/immutable/chunks/*.js`), fetched 23 Oct 2026. The app ships its full UI
string table (i18n messages) in those bundles, so the strings below come straight from the product.
Backend models/vendors are **not** exposed (server-side); only PostHog, Sentry and Cloudflare are visible.

## 1. Pipeline (run panel step names)
```
"steps":{"transcribe":{"name":"transcribe","status":"transcribing audio","detail":"48 min"},
 "voice":{"name":"voice-match","status":"learning your voice","detail":"2 posts"},
 "hooks":{"name":"hook-finder","status":"scoring moments","detail":"31 candidates"},
 "cut":{"name":"clip-cutter","status":"cutting vertical clips","detail":"7 clips"},
 "captions":{"name":"captions","status":"captions timed to every word","detail":"7 clips"},
 "render":{"name":"render","status":"rendering clean 1080p","detail":"3 of 7"},
 "thread":{"name":"x-thread",…},"linkedin":{"name":"linkedin-post",…},"newsletter":{…},"summary":{…}}
```
→ Transcript-first: many candidate moments are generated, scored, and the top ones cut.

## 2. "Best part" = transcript-driven scoring with 4 grades
```
"why":"Why this clip","grades":"Score breakdown",
"grade":{"hook":"Hook","flow":"Flow","value":"Value","trend":"Trend"},
"from":"From {start} in the episode"
"score":{"label":"Score {score} of 100"}
"sort":{"label":"Sort clips","score":"Highest score","duration":"Shortest first","order":"Episode order"}
```
It is speech-only — no visual analysis of the moment:
```
"no_speech":{"hint":"Everything here is built from what someone says — the words drive the captions
 and the cuts. Music, dance and b-roll on their own have nothing to work from."}
"no_moments":"Nothing in the recording stood out as a clip."
"works":{"horizontal":"16:9","length":"{minLength}+","quality":"{minHeight}p+","talking":"Someone talking"}
```
Clip length options: `Auto / Short (Under 30s) / Medium (30–60s) / Long (Over 60s)`, "Up to {count} clips".

## 3. Reframing
```
"layout":{"aspect":"Aspect ratio","aspects":{"9:16":"Vertical","1:1":"Square","16:9":"Wide"},
 "fit":"Framing","fill":"Fill","fillSoon":"Soon",
 "fillSoonToast":"Fill is coming soon. Clips use Fit for now.",
 "blur":"Blurred background","blurFillHint":"Used with Fit"}
```
→ Today: whole 16:9 frame letterboxed over a blurred copy. No face-tracked crop yet.

## 4. Motion captions + hook headline ("Brand templates")
```
"lede":"How every clip Kaleidly cuts looks: captions, headline, logo, intro and music."
captions: font, weight 400–900, size S/M/L, "{value}% from the top", text colour, highlight colour,
 outline, background none/shadow/box, "Words on screen", Uppercase,
 "animations":{"none":"None","word":"One word","karaoke":"Karaoke","pop":"Pop"}
"headline":{"show":"Show headline","textPlaceholder":"Each clip’s hook","positions":{"top","bottom"},
 "styles":{"card":"Card","band":"Band","plain":"Plain"},"duration":"Time on screen"}
"introOutro":"Intro & outro","music":"Music", logo overlay, "Upload font"
```
Clip editor tabs `Captions / Style / Trim`, "Other hooks", "Split line at cursor", "Join with line above",
and: `"burnedIn":"Captions, hook and logo are part of this video. Your edits show once you save."`
→ Captions are burned in server-side; every edit triggers a re-render ("Saved. Rendering the new version").

## What clipforge copies vs. adds
| Kaleidly | clipforge |
|---|---|
| transcribe → hook-finder → clip-cutter → captions → render | same steps & names |
| Score /100 with Hook/Flow/Value/Trend + "Why this clip" | Claude grades all four + why + hook + alt hooks |
| Fit + blurred background | ✔, plus **Fill with face tracking** (their "coming soon") |
| Caption animations none/word/karaoke/pop, highlight, box/shadow | ✔ (libass), plus LLM-picked emphasis words |
| Hook headline card/band/plain | ✔ with slide/fade motion |
| Logo, intro/outro, music | ✔, music ducked under speech |
| — | punch-in zooms, progress bar, loudness normalisation |
| voice-match posts (X/LinkedIn/newsletter) | not built (out of scope) |
