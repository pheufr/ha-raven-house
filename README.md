# Home Assistant Raven House Tools

This repository provides the backend Home Assistant integration `raven_house_tools` (shown in Home Assistant as `Raven House Tools`).

It manages the Raven House feature domains:

- `RH Jobs`
- `RH Quiz`
- `RH Soundboard` services and runtime state

Dashboard/Lovelace cards now live in the companion repository `ha-raven-house-cards`.

## Installation

### HACS
1. Add this repository as a custom repository in HACS.
2. Set the repository type to `Integration`.
3. Install the repository.
4. Restart Home Assistant.
5. Add the `Raven House Tools` integration from Settings -> Devices & Services.
6. Add one entry for `RH Jobs` and one entry for `RH Quiz`.

To use dashboard cards, also install `ha-raven-house-cards` as a separate HACS custom repository of type `Dashboard`.

### Manual
1. Copy `custom_components/raven_house_tools` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Add the `Raven House Tools` integration from Settings -> Devices & Services.

## Companion Dashboard Cards

Install and manage frontend cards from `ha-raven-house-cards`.

Card resources are provided there as:

- `rh-jobs-card.js`
- `rh-quiz-card.js`
- `rh-quiz-master-card.js`
- `rh-quiz-round-card.js`
- `rh-soundboard-card.js`

## Raven House Jobs

The `RH Jobs` entry tracks recurring household jobs as individual devices.

### Device Model

Each job becomes one device with these entities:

- `binary_sensor.rh_jobs_{job_id}`: primary due / not-due state
- `switch.rh_jobs_{job_id}_manual_due`: trigger/dismiss state manually
- `text.rh_jobs_{job_id}_name`: rename the job
- `sensor.rh_jobs_{job_id}_last_triggered`
- `sensor.rh_jobs_{job_id}_last_completed`
- `sensor.rh_jobs_{job_id}_next_due`
- `sensor.rh_jobs_{job_id}_created`
- `sensor.rh_jobs_{job_id}_priority`

The primary binary sensor keeps scheduling metadata and attributes such as `image`, `priority`, `trigger_type`, `cron_expression`, and `days_interval`.

### Managing Jobs

Jobs can be managed from the job device page using control entities.

Jobs can also be created via the `raven_house_tools.add_job` service.

To edit or delete an existing job, open RH Jobs integration options and choose Manage Jobs (Edit/Delete).

Each job supports:

- `name`
- `trigger_type`: `schedule`, `frequency`, or `manual`
- `cron_expression`
- `days_interval`
- `image` (supports media picker/upload in flows and services)
- `icon`
- `colour`
- `priority`

### Services

Service domain: `raven_house_tools`

- `trigger_job`
- `complete_job`
- `dismiss_job`
- `rename_job`
- `update_job_image`
- `add_job`

Example:

```yaml
service: raven_house_tools.complete_job
data:
  entity_id: binary_sensor.rh_jobs_trash_day
```

Set/update job image without manually typing a URL:

```yaml
service: raven_house_tools.update_job_image
data:
  entity_id: binary_sensor.rh_jobs_trash_day
  image: /media/local/jobs/trash.png
```

Use the Actions/Services UI target picker for the job entity and image picker for `image`.

### Example Automations

Calendar-driven trigger for all RH Jobs (example calendar: `calendar.bins`):

```yaml
alias: RH Jobs - Trigger from bins calendar
mode: single
triggers:
  - trigger: calendar
    entity_id: calendar.bins
    event: start
    offset: "-24:00:00"
variables:
  due_window_hours: 24
  jobs_to_trigger: >
    {% set ns = namespace(ids=[]) %}
    {% set event_summary = trigger.calendar_event.summary | lower | trim %}
    {% for job in states.binary_sensor | selectattr('entity_id', 'search', '^binary_sensor\\.rh_jobs_') %}
      {% set job_name = job.name | lower | trim %}
      {% set job_id = job.entity_id | replace('binary_sensor.rh_jobs_', '') %}
      {% set last_completed = states('sensor.rh_jobs_' ~ job_id ~ '_last_completed') %}
      {% set last_completed_ts = as_timestamp(last_completed, default=0) %}
      {% set completed_in_window = last_completed_ts > 0 and (as_timestamp(now()) - last_completed_ts) <= (due_window_hours * 3600) %}
      {% if job_name == event_summary and not completed_in_window %}
        {% set ns.ids = ns.ids + [job_id] %}
      {% endif %}
    {% endfor %}
    {{ ns.ids }}
actions:
  - repeat:
      for_each: "{{ jobs_to_trigger }}"
      sequence:
        - action: button.press
          target:
            entity_id: "button.rh_jobs_{{ repeat.item }}_trigger"
```

Auto-dismiss/complete from a sensor state change:

```yaml
alias: RH Jobs - Auto complete from front door
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.front_door_opening
    to: "on"
actions:
  - action: button.press
    target:
      entity_id: button.rh_jobs_96566d0a_complete
```

## Raven House Quiz

The `RH Quiz` entry manages quiz participants as individual devices.

### Device Model

Each player becomes one device with these entities:

- `sensor.rh_quiz_{player_id}`: primary total score entity
- `sensor.rh_quiz_{player_id}_round`
- `sensor.rh_quiz_{player_id}_last_round`
- `sensor.rh_quiz_{player_id}_alias`
- `binary_sensor.rh_quiz_{player_id}_enabled`
- `switch.rh_quiz_{player_id}_enabled`: enable/disable from device view
- `button.rh_quiz_{player_id}_reset_score`: reset one participant score
- `text.rh_quiz_{player_id}_name`: rename participant
- `text.rh_quiz_{player_id}_alias`: update alias
- `text.rh_quiz_{player_id}_photo`: update image path

### Managing Players

Players can be managed from each participant device page using control entities.

Players can also be created via the `raven_house_tools.add_player` service.

Each player supports:

- `name`
- `alias`
- `photo` (supports media picker/upload in flows and services)
- `enabled`

### Services

Service domain: `raven_house_tools`

- `add_player`
- `remove_player`
- `enable_player`
- `disable_player`
- `rename_player`
- `update_player_alias`
- `update_player_photo`
- `reset_player_score`
- `add_points`
- `remove_points`
- `use_joker`
- `set_quiz_rounds`
- `start_new_round`
- `end_round`
- `start_round`
- `start_new_quiz`
- `reset_quiz`

Example:

```yaml
service: raven_house_tools.add_points
data:
  entity_id: sensor.rh_quiz_alice
  points: 5
```

## Raven House Soundboard

The soundboard backend provides services and session state used by the Raven House soundboard card.

### Services

Service domain: `raven_house_tools`

- `soundboard_set_target`
- `soundboard_set_mode` (optional advanced service for automations)
- `soundboard_connect`
- `soundboard_disconnect`
- `soundboard_play_clip`

Runtime status sensor:

- `sensor.rh_soundboard_session`

## Notes

- This repository is backend-only (`Integration` in HACS).
- Install `ha-raven-house-cards` separately as `Dashboard` in HACS for UI cards.
- Home Assistant 2026.3 or newer is recommended.

## License

MIT
