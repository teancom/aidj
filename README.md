# AI DJ for Home Assistant

AI DJ is a Home Assistant custom integration for queue-aware, AI-generated DJ presentation around Music Assistant playback.

The project is currently in an early milestone. The first release provides a config-entry setup flow and a one-shot `aidj.announce` action. Playback orchestration, Music Assistant queue integration, weather, feed-backed news, and AI briefings will be added incrementally.

## Install for development

1. Add this repository to HACS as a **Custom repository** with type **Integration**.
2. Download AI DJ from HACS.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and select **AI DJ**.
5. Choose a media player and TTS entity.

The repository is intended to be installed through HACS. Do not copy individual Python files into `custom_components` manually.

## Milestone 1

Milestone 1 provides:

- UI-based config flow for one AI DJ station.
- Options flow for changing the station name, player, and TTS entity.
- `aidj.start`, which resumes playback on the configured media player.
- `aidj.stop`, which stops playback on the configured media player.
- `aidj.announce`, which sends a supplied message to the configured TTS entity and media player.
- `aidj.queue_add`, which adds one media item to the configured Music Assistant queue without replacing the existing queue.
- An internal queue-read adapter using Home Assistant's response-capable `music_assistant.get_queue` action.

Milestone 1 intentionally supports one station per Home Assistant instance. The optional `config_entry_id` field is reserved for the future multi-station configuration and is not needed yet.

Example action:

```yaml
action: aidj.announce
data:
  message: "This is Escondido Smart Home Radio"
```

If `config_entry_id` is supplied, it selects a specific AI DJ station. It is optional while the integration supports one station.

### Queue control

AI DJ targets the configured Home Assistant `media_player` entity for Music Assistant actions. It does not store a Music Assistant URL, token, or internal player ID. The safe queue action is:

```yaml
action: aidj.queue_add
data:
  media_id: spotify://track/example
```

This delegates to `music_assistant.play_media` with `enqueue: add`, so the existing queue is preserved. Before adding, AI DJ reads `music_assistant.get_queue` and skips media already in the current or next item. Successfully added IDs are also remembered for the lifetime of the station runtime, preventing repeated calls from appending duplicates.

## Design goals

- Use Home Assistant config entries and selectors instead of YAML configuration.
- Use Home Assistant media-player and TTS abstractions.
- Use Home Assistant's Music Assistant integration for queue operations, avoiding a second MA connection or stored MA credentials.
- Treat news as a provider interface. The first planned provider is Home Assistant Feedreader; direct RSS parsing is not part of the initial design.
- Treat AI as optional infrastructure for generated DJ content, but skip an interruption when an AI briefing cannot be generated or validated.

## Development

The integration lives under `custom_components/aidj/`, as required by HACS. The repository keeps dependency-free checks separate from Home Assistant runtime tests:

```bash
# Dependency-free repository checks
python3 -m compileall -q custom_components tests
python3 -m unittest discover -s tests -v

# Home Assistant config-flow and service tests
.venv/bin/python -m pytest -q
```

The test environment used for the current milestone is Home Assistant 2026.7.4 with `pytest<9` and `pytest-homeassistant-custom-component`. The `.venv/` directory is local development state and is intentionally gitignored.

This repository is not yet an official Home Assistant core integration and is distributed as a HACS custom repository while it is developed.
