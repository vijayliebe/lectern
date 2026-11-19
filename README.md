# Lectern

A small HTTP service that produces **transcripts for local video files**. It's built for [EduNest](https://github.com/vijayliebe/edunest), a self-hosted course app, but works with any app that can make an HTTP request.

For each video it tries, in order:

1. **A subtitle file next to the video** (`Video.en.vtt`, `Video.srt`, …).
2. **YouTube captions**, found by the YouTube id in yt-dlp's `.info.json`, in the file name (`Title [id].mp4`), given by the caller, or by a **name search** that only accepts a result whose length matches within 2 seconds.
   - It picks the creator's captions in the video's language, then YouTube's own speech recognition in that language (`<lang>-orig`), then creator's English captions. It never asks for machine-translated tracks, which YouTube rate-limits.
   - It is gentle: one request at a time, a random 10–30 s pause between requests, and a pause of 1 hour (doubling up to 24 h) whenever YouTube answers "too many requests" or asks for a bot check. Each video is fetched once and stored.
3. **Speech-to-text on the CPU** with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), in a separate background queue at low priority.

Transcripts are stored as timed segments with the detected language, and served as JSON or WebVTT.

With an LLM API key (OpenRouter by default, or Anthropic directly) it also writes, from the transcript and always in English, whatever the transcript's language:
- a **summary**: a few sentences, the key points and an outline, each pointing to a `[mm:ss]` moment;
- **study notes**: a Mermaid mind map of the topics, then the video part by part (`## [mm:ss] Title`) with bullet points, tables, Mermaid diagrams (flowcharts, sequence diagrams, ...), formulas, examples, "Remember" and "Common mistake" boxes, and review questions. The prompt includes rules that keep the Mermaid syntax valid; an app that shows the notes should render ```mermaid blocks;
- an **English translation** of a non-English transcript, line by line with the original timing, so it works as subtitles.

These are made only when asked for, once per video, and kept until the transcript changes.

It can also **chat about a video**: the whole transcript goes into the prompt (cached, so follow-up questions are cheap), answers stream back and point to `[mm:ss]` moments. Answers are in English unless the learner writes in another language. Lectern keeps no chat history: the caller sends the conversation each time.

### Other languages (for example Hindi)

- **YouTube:** for a Hindi video Lectern takes the creator's Hindi captions, or else YouTube's own Hindi speech recognition (`hi-orig`), never a machine-translated track. If the creator uploaded English captions, those are used.
- **Speech-to-text:** Whisper detects the language by itself. For Hindi, `LECTERN_WHISPER_MODEL=medium` or `large-v3-turbo` is much more accurate than `small`, at the cost of speed.
- **Summary and study notes** are written in English from the original transcript; **translation** gives English subtitles with the original timing.

## API

Every endpoint except `/v1/health` needs `Authorization: Bearer <LECTERN_API_KEY>`. Don't publish Lectern's port: let your app call it over the Docker network and do its own login and access checks first.

| Method | Path | |
|---|---|---|
| `POST` | `/v1/videos` | Register a video: `{"path", "title"?, "duration_s"?, "youtube_id"?, "priority"?: 0/1}`. Idempotent by path; a changed file starts over. Returns its status. |
| `POST` | `/v1/videos/batch` | The same for a list (up to 5000). |
| `GET` | `/v1/videos/{id}` | Status: `queued`, `fetching`, `whisper` (waiting for speech-to-text), `transcribing` (with `progress`), `done`, `failed`. |
| `GET` | `/v1/videos/{id}/transcript` | Segments as JSON; `?format=vtt` for WebVTT. `409` until it's done. |
| `POST` | `/v1/videos/{id}/now` | Someone wants this one now: it goes ahead of everything else in its queue (a failed one is tried again). `GET /v1/videos/{id}` shows `ahead`, the number of videos before it. |
| `POST` | `/v1/videos/{id}/retry` | Start over; `?whisper=true` goes straight to speech-to-text. |
| `GET` | `/v1/videos/{id}/transcript?lang=en` | The English translation, once made (`409` before). |
| `POST` | `/v1/videos/{id}/summary` | Ask for the summary (`notes`, `translation` work the same way). Made in the background; `?again=true` makes it again. |
| `GET` | `/v1/videos/{id}/summary` | Its status (`none`, `queued`, `working`, `done`, `failed`) and, when done, the Markdown, the model and the tokens used. |
| `POST` | `/v1/videos/{id}/chat` | `{"messages": [{"role": "user", "content": "..."}, ...], "at": 125}` (the conversation so far, ending with the question; `at` = where the viewer is, optional). Streams server-sent events: `{"delta": "..."}` pieces, then `{"done": true, "model", "input_tokens", "output_tokens"}` or `{"error": "..."}`. |
| `GET` | `/v1/stats` | Queue counts, sources, YouTube pause state, and summary jobs with total tokens used. |
| `GET` | `/v1/health` | No key needed. |

Paths must be inside `LECTERN_MEDIA_ROOTS`; anything else is refused, including `..` tricks.

## Configuration

| Variable | Default | |
|---|---|---|
| `LECTERN_API_KEY` | — (required) | At least 16 random characters, e.g. `openssl rand -hex 24`. |
| `LECTERN_MEDIA_ROOTS` | `/media` | Comma-separated folders it may read. |
| `LECTERN_DATA_DIR` | `/data` | Database and the speech model. |
| `LECTERN_YOUTUBE` | `1` | Use YouTube captions. |
| `LECTERN_YT_NAME_SEARCH` | `1` | Search YouTube by title when no id is known. |
| `LECTERN_YT_MIN_GAP` / `LECTERN_YT_MAX_GAP` | `10` / `30` | Random pause between YouTube requests, in seconds. |
| `LECTERN_YT_BACKOFF` | `3600` | First pause after YouTube pushes back; doubles each time, max 24 h. |
| `LECTERN_YT_COOKIES` | — | Path to a `cookies.txt`, only if YouTube insists on a sign-in. |
| `LECTERN_WHISPER` | `1` | Use speech-to-text when nothing else has a transcript. |
| `LECTERN_WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3-turbo`, … Downloaded on first use. |
| `LECTERN_WHISPER_THREADS` | `4` | CPU threads. |
| `LECTERN_WHISPER_COMPUTE` | `int8` | |
| `LECTERN_WHISPER_BEAM` | `1` | `1` is about twice as fast on a CPU; `5` is slightly more accurate. |
| `OPENROUTER_API_KEY` | — | Turns on summaries, study notes and translation through [OpenRouter](https://openrouter.ai) (the default provider). Without a key they are off. |
| `ANTHROPIC_API_KEY` | — | Use the Anthropic API directly instead (used when no OpenRouter key is set). |
| `LECTERN_LLM_PROVIDER` | automatic | `openrouter` or `anthropic`, to choose when both keys are set. |
| `LECTERN_LLM` | `1` | `0` turns them off even with a key. |
| `LECTERN_LLM_MODEL` | `anthropic/claude-opus-5.5` on OpenRouter, `claude-opus-5-5` on Anthropic | Model for summaries, study notes and translations: any model id the provider offers (on OpenRouter, see openrouter.ai/models). |
| `LECTERN_LLM_CHAT_MODEL` | same as `LECTERN_LLM_MODEL` | Model for chat answers, if you want a different (e.g. faster, cheaper) one. |
| `LECTERN_LLM_EFFORT` | `medium` | How hard the model thinks for summaries and notes: `low`, `medium`, `high`. |
| `LECTERN_LLM_TRANSLATE_EFFORT` | `low` | The same, for translations. |
| `LECTERN_LLM_CHAT_EFFORT` | `low` | The same, for chat answers (low = quick). |
| `PUID` / `PGID` | `1000` | Owner of the files in `/data`. |

### Cost and privacy of summaries

- The transcript (not the video) is sent to the LLM provider, with the video's title. With OpenRouter, it passes them on to the company that runs the chosen model. Nothing is sent until someone asks for a summary, notes or a translation.
- With Claude Opus 5.5 ($4 per million input tokens, $20 per million output tokens), a 1-hour lecture's transcript is roughly 15,000 tokens: a summary costs about 10 cents, study notes about 15 to 25 cents, and a translation about 30 to 40 cents (its output is as long as the transcript). Each is made once per video. The summary and the notes share the transcript through prompt caching, which makes the second one cheaper.
- Chat: each question sends the transcript and the conversation. With Claude Opus 5.5 and a 1-hour lecture, the first question costs about 8 cents (it puts the transcript in the prompt cache) and follow-ups within 5 minutes about 1 to 2 cents each; after a longer pause the next question pays for the cache again.
- `/v1/stats` shows the tokens used so far for summaries, notes and translations.
- With the Anthropic API directly, a declined request is retried on a fallback model Anthropic picks (server-side fallback). With OpenRouter, failures are retried with back-off. Either way, if it still fails the job shows the reason.
- Prompts are the same for every provider. Cheaper models (for example `anthropic/claude-sonnet-5.5`, half the price) work too; quality of the notes is best with Opus.

## Run it

With Docker Compose, next to the app that uses it. Don't publish its port:

```yaml
  lectern:
    image: lectern:latest            # build it with `make save` -> lectern-latest.tar
    environment:
      - LECTERN_API_KEY=<openssl rand -hex 24>
      - PUID=1000
      - PGID=1000
    volumes:
      - /path/on/host/lectern:/data
      - /path/to/videos:/media:ro    # the same path your app uses for the files
    cpus: 4
    restart: unless-stopped
```

Then, from your app:

```bash
curl -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
     -d '{"path": "/media/talk.mp4"}' http://lectern:8000/v1/videos
curl -H "Authorization: Bearer $KEY" "http://lectern:8000/v1/videos/1/transcript?format=vtt"
```

## Development

```bash
make test     # unit tests
make dev      # runs on :8000 against ./media and prints a random API key
make save     # x86_64 image as lectern-latest.tar
```

## License

[MIT](LICENSE)
