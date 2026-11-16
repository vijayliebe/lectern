# Lectern

A small HTTP service that produces **transcripts for local video files**. It's built for [EduNest](https://github.com/vijayliebe/edunest), a self-hosted course app, but works with any app that can make an HTTP request.

For each video it tries, in order:

1. **A subtitle file next to the video** (`Video.en.vtt`, `Video.srt`, …).
2. **YouTube captions**, found by the YouTube id in yt-dlp's `.info.json`, in the file name (`Title [id].mp4`), given by the caller, or by a **name search** that only accepts a result whose length matches within 2 seconds.
   - It picks the creator's captions in the video's language, then YouTube's own speech recognition in that language (`<lang>-orig`), then creator's English captions. It never asks for machine-translated tracks, which YouTube rate-limits.
   - It is gentle: one request at a time, a random 10–30 s pause between requests, and a pause of 1 hour (doubling up to 24 h) whenever YouTube answers "too many requests" or asks for a bot check. Each video is fetched once and stored.
3. **Speech-to-text on the CPU** with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), in a separate background queue at low priority.

Transcripts are stored as timed segments with the detected language, and served as JSON or WebVTT.

Coming next: English translation of non-English transcripts, summaries, detailed notes, and chat, using a cloud LLM.

## API

Every endpoint except `/v1/health` needs `Authorization: Bearer <LECTERN_API_KEY>`. Don't publish Lectern's port: let your app call it over the Docker network and do its own login and access checks first.

| Method | Path | |
|---|---|---|
| `POST` | `/v1/videos` | Register a video: `{"path", "title"?, "duration_s"?, "youtube_id"?, "priority"?: 0/1}`. Idempotent by path; a changed file starts over. Returns its status. |
| `POST` | `/v1/videos/batch` | The same for a list (up to 5000). |
| `GET` | `/v1/videos/{id}` | Status: `queued`, `fetching`, `whisper` (waiting for speech-to-text), `transcribing` (with `progress`), `done`, `failed`. |
| `GET` | `/v1/videos/{id}/transcript` | Segments as JSON; `?format=vtt` for WebVTT. `409` until it's done. |
| `POST` | `/v1/videos/{id}/retry` | Start over; `?whisper=true` goes straight to speech-to-text. |
| `GET` | `/v1/stats` | Queue counts, sources, YouTube pause state. |
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
| `PUID` / `PGID` | `1000` | Owner of the files in `/data`. |

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
