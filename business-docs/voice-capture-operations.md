# Voice Capture Operations

## Runtime behavior

FieldBase checks `GET /api/voice/transcription` when an owner opens the New Job modal.

| Server configuration | Browser path |
|---|---|
| `OPENAI_API_KEY` set and MediaRecorder available | Record locally, upload once, transcribe on the server |
| No `OPENAI_API_KEY` | Existing Web Speech API behavior |
| Neither capture method available | Editable typed transcript |

The audio route and capability check require an authenticated owner session. API keys stay server-side and are never returned by the capability endpoint.

## Configuration

Set secrets in the deployment environment, not in source control.

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | To enable server transcription | Provider credential |
| `OPENAI_BASE_URL` | No | Existing OpenAI-compatible provider base URL; defaults to `https://api.openai.com/v1` |
| `OPENAI_TRANSCRIPTION_MODEL` | No | Provider model; defaults to `gpt-4o-mini-transcribe` |

`ANTHROPIC_API_KEY` continues to power transcript-to-job extraction. It does not enable audio transcription.

## Limits and handling

- The browser stops a recording after two minutes.
- The server rejects payloads over 15 MB and unsupported media types.
- Accepted upload families are FLAC, M4A/MP4, MP3/MPEG, OGG, WAV, and WebM.
- Audio is held in request memory for the provider call. FieldBase does not write recordings to disk or object storage.
- Provider calls use a 5-second connection timeout and a 90-second response timeout.
- Provider error bodies are not relayed to the browser or written to application logs.

## Deployment smoke check

1. Deploy without `OPENAI_API_KEY`; confirm the mic still uses browser speech or permits typing.
2. Add `OPENAI_API_KEY` through the deployment secret manager and restart the app.
3. Sign in as an owner and confirm `GET /api/voice/transcription` returns `{"configured":true}` without any credential data.
4. Record a short note on iOS Safari and Android Chrome; stop it and confirm the transcript appears before extraction.
5. Deny microphone permission and confirm the modal remains usable through typed input.
6. Remove or invalidate the provider configuration and confirm the UI reports the failure without losing typed-input access.

Do not use a production customer recording for the smoke check. Use a synthetic note with no personal or job-site data.
