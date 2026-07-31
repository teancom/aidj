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
- `aidj.announce`, which sends a supplied message to the configured TTS entity and media player immediately.
- `aidj.announce_next`, which waits for the configured player to enter a different `playing` track before speaking once.
- `aidj.queue_add`, which adds one media item to the configured Music Assistant queue without replacing the existing queue.
- An internal queue-read adapter using Home Assistant's response-capable `music_assistant.get_queue` action.
- A provider-neutral briefing layer with a Home Assistant entity-state provider and per-provider failure isolation. It is an internal foundation; no AI credentials or briefing action are configured yet.

Milestone 1 intentionally supports one station per Home Assistant instance. The optional `config_entry_id` field is reserved for the future multi-station configuration and is not needed yet.

Example action:

```yaml
action: aidj.announce
data:
  message: "This is Escondido Smart Home Radio"
```

If `config_entry_id` is supplied, it selects a specific AI DJ station. It is optional while the integration supports one station.

### Announcement sequencing

Use `aidj.announce` when an immediate interruption is intentional. Use `aidj.announce_next` when the message should wait for a track boundary:

```yaml
action: aidj.announce_next
data:
  message: "Coming up next on Escondido Smart Home Radio"
```

`announce_next` requires the configured player to be playing an identifiable track when it is armed. It listens only for state changes on that player, ignores pauses/stops and same-track updates, and speaks once after a different track enters `playing`. A pending listener is cancelled when the integration entry unloads or reloads.

### Briefing providers

The internal `briefing.py` module normalizes source facts as `BriefingItem` values. Providers implement `async_collect()`, and `async_collect_briefing()` keeps successful providers' items when another optional provider fails. `EntityStateProvider` exposes explicitly selected Home Assistant entities, and `WeatherEntityProvider` turns a configured HA weather entity into a compact conditions/temperature/humidity/wind fact. The current installation has `weather.forecast_home`; no Feedreader entity is present yet, so news remains a later provider rather than a guessed or empty integration. `HaConversationBriefingGenerator` can ask a configured HA conversation agent (currently `conversation.openai_conversation`) for plain speech through `conversation.process`; it validates the response and stores no AI credentials in AI DJ. Generation is an internal seam until a user-facing briefing action and orchestration policy are added.

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
