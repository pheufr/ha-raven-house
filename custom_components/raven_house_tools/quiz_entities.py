"""Entity model for Raven House Quiz support within Raven House Tools."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any
import uuid

import voluptuous as vol
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .quiz_const import (
    ATTR_ACTIVE_ROUND_INDEX,
    ATTR_ACTIVE_ROUND_NAME,
    ATTR_CREATED,
    ATTR_LAST_ROUND_SCORE,
    ATTR_PLAYER_ALIAS,
    ATTR_PLAYER_ENABLED,
    ATTR_PLAYER_ID,
    ATTR_PLAYER_METRIC,
    ATTR_PLAYER_NAME,
    ATTR_PLAYER_PHOTO,
    ATTR_QUIZ_ROUNDS,
    ATTR_ROUND_POSITION_INDEX,
    ATTR_ROUND_SCORE,
    ATTR_TOTAL_ROUNDS,
    ATTR_TOTAL_SCORE,
    DOMAIN,
    PREFIX_QUIZ,
    QUIZ_SIGNAL_UPDATE,
    SERVICE_ADD_PLAYER,
    SERVICE_ADD_POINTS,
    SERVICE_DISABLE_PLAYER,
    SERVICE_END_ROUND,
    SERVICE_ENABLE_PLAYER,
    SERVICE_REMOVE_PLAYER,
    SERVICE_REMOVE_POINTS,
    SERVICE_RENAME_PLAYER,
    SERVICE_RESET_QUIZ,
    SERVICE_RESET_PLAYER_SCORE,
    SERVICE_SET_QUIZ_ROUNDS,
    SERVICE_START_ROUND,
    SERVICE_START_NEW_QUIZ,
    SERVICE_START_NEW_ROUND,
    STORAGE_VERSION,
    SERVICE_UPDATE_PLAYER_ALIAS,
    SERVICE_UPDATE_PLAYER_PHOTO,
    SERVICE_USE_JOKER,
)
from .features import entry_id_supports_quiz

_LOGGER = logging.getLogger(__name__)


MEDIA_SELECTOR_SCHEMA = vol.Schema(
    {
        vol.Required("media_content_id"): cv.string,
        vol.Optional("media_content_type"): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)


def _normalize_media_value(value: Any) -> str:
    """Normalize media selector output into a storable path/URL."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "entity_picture", "path"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate.strip()

        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            thumbnail = metadata.get("thumbnail")
            if isinstance(thumbnail, str):
                return thumbnail.strip()

        media_content_id = value.get("media_content_id")
        if isinstance(media_content_id, str):
            return media_content_id.strip()
    return ""


def _quiz_storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.players_{entry_id}"


def _entry_data(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})


def _normalize_players(players_data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    players: dict[str, dict[str, Any]] = {}
    raw_players = (players_data or {}).get("players", [])
    for player in raw_players:
        player_id = player.get("id")
        if not player_id:
            continue
        players[player_id] = {
            "id": player_id,
            "name": player.get("name", ""),
            "alias": player.get("alias", ""),
            "photo": player.get("photo", ""),
            "total_score": int(player.get("total_score", 0)),
            "current_round_score": int(player.get("current_round_score", 0)),
            "last_round_score": int(player.get("last_round_score", 0)),
            "enabled": bool(player.get("enabled", False)),
            "created": player.get("created") or utcnow().isoformat(),
        }
    return players


def _normalize_rounds(quiz_data: dict[str, Any] | None) -> list[str]:
    rounds: list[str] = []
    raw_rounds = (quiz_data or {}).get("rounds", [])
    if not isinstance(raw_rounds, list):
        return rounds
    for item in raw_rounds:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name:
            rounds.append(name)
    return rounds


def _normalize_active_round_index(quiz_data: dict[str, Any] | None, rounds: list[str]) -> int | None:
    raw_index = (quiz_data or {}).get("active_round_index")
    if raw_index is None or raw_index == "":
        return None
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        return None
    if 0 <= index < len(rounds):
        return index
    return None


def _normalize_round_position_index(quiz_data: dict[str, Any] | None, rounds: list[str]) -> int | None:
    raw_index = (quiz_data or {}).get("round_position_index")
    if raw_index is None or raw_index == "":
        return _normalize_active_round_index(quiz_data, rounds)
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        return _normalize_active_round_index(quiz_data, rounds)
    if 0 <= index < len(rounds):
        return index
    return _normalize_active_round_index(quiz_data, rounds)


def _active_round_name(rounds: list[str], active_round_index: int | None) -> str | None:
    if active_round_index is None:
        return None
    if 0 <= active_round_index < len(rounds):
        return rounds[active_round_index]
    return None


async def _ensure_runtime(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    data = _entry_data(hass, entry_id)
    if "quiz_store" not in data:
        data["quiz_store"] = Store(hass, STORAGE_VERSION, _quiz_storage_key(entry_id))
    if "quiz_players" not in data:
        quiz_data = await data["quiz_store"].async_load() or {"players": []}
        data["quiz_players"] = _normalize_players(quiz_data)
        rounds = _normalize_rounds(quiz_data)
        data["quiz_rounds"] = rounds
        data["quiz_active_round_index"] = _normalize_active_round_index(quiz_data, rounds)
        data["quiz_round_position_index"] = _normalize_round_position_index(quiz_data, rounds)
    data.setdefault("quiz_rounds", [])
    data.setdefault("quiz_active_round_index", None)
    data.setdefault("quiz_round_position_index", data.get("quiz_active_round_index"))
    data.setdefault("quiz_sensor_entities", {})
    data.setdefault("quiz_binary_entities", {})
    data.setdefault("quiz_switch_entities", {})
    data.setdefault("quiz_text_entities", {})
    data.setdefault("quiz_button_entities", {})
    data.setdefault("quiz_round_entity", None)
    return data


async def _save_players(hass: HomeAssistant, entry_id: str) -> None:
    data = await _ensure_runtime(hass, entry_id)
    await data["quiz_store"].async_save(
        {
            "players": list(data["quiz_players"].values()),
            "rounds": list(data.get("quiz_rounds", [])),
            "active_round_index": data.get("quiz_active_round_index"),
            "round_position_index": data.get("quiz_round_position_index"),
        }
    )


async def async_setup_sensors(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up player sensors."""
    data = await _ensure_runtime(hass, config_entry.entry_id)
    data["quiz_sensor_add_entities"] = async_add_entities

    entities: list[SensorEntity] = []
    for player_id in sorted(data["quiz_players"]):
        entities.extend(_build_player_sensor_entities(hass, config_entry.entry_id, player_id))
    entities.append(QuizRoundsSensor(hass, config_entry.entry_id))

    if entities:
        async_add_entities(entities)
        _store_sensor_entities(data, entities)
        for entity in entities:
            if isinstance(entity, QuizRoundsSensor):
                data["quiz_round_entity"] = entity


async def async_setup_binary_sensors(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up player binary sensors."""
    data = await _ensure_runtime(hass, config_entry.entry_id)
    data["quiz_binary_add_entities"] = async_add_entities

    entities = [
        QuizPlayerEnabledBinarySensor(hass, config_entry.entry_id, player_id)
        for player_id in sorted(data["quiz_players"])
    ]
    if entities:
        async_add_entities(entities)
        for entity in entities:
            data["quiz_binary_entities"][entity.player_id] = entity


async def async_setup_switches(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up player switches."""
    data = await _ensure_runtime(hass, config_entry.entry_id)
    data["quiz_switch_add_entities"] = async_add_entities

    entities = [
        QuizPlayerEnabledSwitch(hass, config_entry.entry_id, player_id)
        for player_id in sorted(data["quiz_players"])
    ]
    if entities:
        async_add_entities(entities)
        for entity in entities:
            data["quiz_switch_entities"][entity.player_id] = entity


async def async_setup_texts(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up player text controls."""
    data = await _ensure_runtime(hass, config_entry.entry_id)
    data["quiz_text_add_entities"] = async_add_entities

    entities: list[TextEntity] = []
    for player_id in sorted(data["quiz_players"]):
        entities.extend(_build_player_text_entities(hass, config_entry.entry_id, player_id))
    if entities:
        async_add_entities(entities)
        _store_text_entities(data, entities)


async def async_setup_buttons(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up player button controls."""
    data = await _ensure_runtime(hass, config_entry.entry_id)
    data["quiz_button_add_entities"] = async_add_entities

    entities = [
        QuizPlayerResetScoreButton(hass, config_entry.entry_id, player_id)
        for player_id in sorted(data["quiz_players"])
    ]
    if entities:
        async_add_entities(entities)
        for entity in entities:
            data["quiz_button_entities"][entity.player_id] = entity


def _build_player_sensor_entities(
    hass: HomeAssistant,
    entry_id: str,
    player_id: str,
) -> list[SensorEntity]:
    return [
        QuizTotalScoreSensor(hass, entry_id, player_id),
        QuizRoundScoreSensor(hass, entry_id, player_id),
        QuizLastRoundScoreSensor(hass, entry_id, player_id),
        QuizAliasSensor(hass, entry_id, player_id),
    ]


def _store_sensor_entities(data: dict[str, Any], entities: list[SensorEntity]) -> None:
    by_player = data.setdefault("quiz_sensor_entities", {})
    for entity in entities:
        if isinstance(entity, QuizRoundsSensor):
            continue
        by_player.setdefault(entity.player_id, []).append(entity)


def _build_player_text_entities(
    hass: HomeAssistant,
    entry_id: str,
    player_id: str,
) -> list[TextEntity]:
    return [
        QuizPlayerNameText(hass, entry_id, player_id),
        QuizPlayerAliasText(hass, entry_id, player_id),
        QuizPlayerPhotoText(hass, entry_id, player_id),
    ]


def _store_text_entities(data: dict[str, Any], entities: list[TextEntity]) -> None:
    by_player = data.setdefault("quiz_text_entities", {})
    for entity in entities:
        by_player.setdefault(entity.player_id, []).append(entity)


def _entity_player_id(entity_id: str) -> str | None:
    sensor_prefix = f"sensor.{PREFIX_QUIZ}_"
    binary_prefix = f"binary_sensor.{PREFIX_QUIZ}_"

    if entity_id.startswith(sensor_prefix):
        player_part = entity_id[len(sensor_prefix) :]
    elif entity_id.startswith(binary_prefix):
        player_part = entity_id[len(binary_prefix) :]
    else:
        return None

    for suffix in ("_round", "_last_round", "_alias", "_enabled"):
        if player_part.endswith(suffix):
            return player_part[: -len(suffix)]
    return player_part


def _find_player_by_target(
    hass: HomeAssistant, entity_id: str | list[str] | None, player_id: str | None
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    entity_ids: list[str] = []
    if isinstance(entity_id, str):
        entity_ids = [entity_id]
    elif isinstance(entity_id, list):
        entity_ids = [item for item in entity_id if isinstance(item, str)]

    for entry_id, data in hass.data.get(DOMAIN, {}).items():
        if not entry_id_supports_quiz(hass, entry_id):
            continue
        players = data.get("quiz_players", {})
        if player_id and player_id in players:
            return entry_id, data, players[player_id]
        for item_entity_id in entity_ids:
            parsed_player_id = _entity_player_id(item_entity_id)
            if parsed_player_id and parsed_player_id in players:
                return entry_id, data, players[parsed_player_id]
    return None


async def async_setup_quiz_services(hass: HomeAssistant) -> None:
    """Register Raven House Quiz services."""

    async def _broadcast_rounds(entry_id: str) -> None:
        async_dispatcher_send(hass, f"{QUIZ_SIGNAL_UPDATE}_{entry_id}_rounds")

    async def _broadcast(entry_id: str, player_id: str | None = None) -> None:
        if player_id is not None:
            async_dispatcher_send(hass, f"{QUIZ_SIGNAL_UPDATE}_{entry_id}_{player_id}")
            return
        data = await _ensure_runtime(hass, entry_id)
        for existing_player_id in data.get("quiz_players", {}):
            async_dispatcher_send(hass, f"{QUIZ_SIGNAL_UPDATE}_{entry_id}_{existing_player_id}")
        await _broadcast_rounds(entry_id)

    def _normalize_round_inputs(raw_rounds: Any) -> list[str]:
        if not isinstance(raw_rounds, list):
            return []
        rounds: list[str] = []
        for item in raw_rounds:
            if not isinstance(item, str):
                continue
            name = item.strip()
            if name:
                rounds.append(name)
        return rounds

    def _coerce_round_index(raw_index: Any, round_count: int) -> int | None:
        if raw_index is None or raw_index == "":
            return None
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return None
        return index if 0 <= index < round_count else None

    def _next_round_index(current_index: int | None, round_count: int) -> int | None:
        if round_count <= 0:
            return None
        if current_index is None:
            return 0
        next_index = current_index + 1
        return next_index if next_index < round_count else None

    def _end_round_internal(data: dict[str, Any]) -> bool:
        active_round_index = _coerce_round_index(
            data.get("quiz_active_round_index"),
            len(data.get("quiz_rounds", [])),
        )
        if active_round_index is None:
            return False
        for player in data["quiz_players"].values():
            if not player.get("enabled"):
                continue
            round_score = int(player.get(ATTR_ROUND_SCORE, 0))
            player[ATTR_LAST_ROUND_SCORE] = round_score
            player[ATTR_TOTAL_SCORE] = int(player.get(ATTR_TOTAL_SCORE, 0)) + round_score
            player[ATTR_ROUND_SCORE] = 0
        data["quiz_round_position_index"] = active_round_index
        data["quiz_active_round_index"] = None
        return True

    def _start_round_internal(data: dict[str, Any]) -> bool:
        rounds = data.get("quiz_rounds", [])
        if not rounds:
            data["quiz_active_round_index"] = None
            data["quiz_round_position_index"] = None
            return False
        if _coerce_round_index(data.get("quiz_active_round_index"), len(rounds)) is not None:
            return False
        next_round_index = _next_round_index(data.get("quiz_round_position_index"), len(rounds))
        if next_round_index is None:
            return False
        data["quiz_active_round_index"] = next_round_index
        data["quiz_round_position_index"] = next_round_index
        return True

    async def _add_player(call: ServiceCall) -> None:
        player = {
            "id": str(uuid.uuid4())[:8],
            "name": call.data["name"],
            "alias": call.data["alias"],
            "photo": _normalize_media_value(call.data.get("photo", "")),
            "total_score": 0,
            "current_round_score": 0,
            "last_round_score": 0,
            "enabled": True,
            "created": utcnow().isoformat(),
        }

        for entry_id in hass.data.get(DOMAIN, {}):
            if not entry_id_supports_quiz(hass, entry_id):
                continue
            data = await _ensure_runtime(hass, entry_id)
            data["quiz_players"][player["id"]] = player
            await _save_players(hass, entry_id)

            sensor_add = data.get("quiz_sensor_add_entities")
            if sensor_add:
                sensor_entities = _build_player_sensor_entities(hass, entry_id, player["id"])
                sensor_add(sensor_entities)
                _store_sensor_entities(data, sensor_entities)

            binary_add = data.get("quiz_binary_add_entities")
            if binary_add:
                binary_entity = QuizPlayerEnabledBinarySensor(hass, entry_id, player["id"])
                binary_add([binary_entity])
                data["quiz_binary_entities"][player["id"]] = binary_entity

            switch_add = data.get("quiz_switch_add_entities")
            if switch_add:
                switch_entity = QuizPlayerEnabledSwitch(hass, entry_id, player["id"])
                switch_add([switch_entity])
                data["quiz_switch_entities"][player["id"]] = switch_entity

            text_add = data.get("quiz_text_add_entities")
            if text_add:
                text_entities = _build_player_text_entities(hass, entry_id, player["id"])
                text_add(text_entities)
                _store_text_entities(data, text_entities)

            button_add = data.get("quiz_button_add_entities")
            if button_add:
                button_entity = QuizPlayerResetScoreButton(hass, entry_id, player["id"])
                button_add([button_entity])
                data["quiz_button_entities"][player["id"]] = button_entity

            await _broadcast(entry_id, player["id"])
            return

    async def _remove_player(call: ServiceCall) -> None:
        result = _find_player_by_target(hass, call.data.get("entity_id"), call.data.get("player_id"))
        if result is None:
            return
        entry_id, data, player = result
        player_id = player["id"]
        data["quiz_players"].pop(player_id, None)

        for entity in data.get("quiz_sensor_entities", {}).pop(player_id, []):
            await entity.async_remove()
        binary_entity = data.get("quiz_binary_entities", {}).pop(player_id, None)
        if binary_entity is not None:
            await binary_entity.async_remove()
        switch_entity = data.get("quiz_switch_entities", {}).pop(player_id, None)
        if switch_entity is not None:
            await switch_entity.async_remove()
        for entity in data.get("quiz_text_entities", {}).pop(player_id, []):
            await entity.async_remove()
        button_entity = data.get("quiz_button_entities", {}).pop(player_id, None)
        if button_entity is not None:
            await button_entity.async_remove()

        await _save_players(hass, entry_id)

    async def _set_enabled(call: ServiceCall, enabled: bool) -> None:
        result = _find_player_by_target(hass, call.data.get("entity_id"), call.data.get("player_id"))
        if result is None:
            return
        entry_id, _, player = result
        player["enabled"] = enabled
        await _save_players(hass, entry_id)
        await _broadcast(entry_id, player["id"])

    async def _enable_player(call: ServiceCall) -> None:
        await _set_enabled(call, True)

    async def _disable_player(call: ServiceCall) -> None:
        await _set_enabled(call, False)

    async def _rename_player(call: ServiceCall) -> None:
        result = _find_player_by_target(hass, call.data.get("entity_id"), call.data.get("player_id"))
        if result is None:
            return
        entry_id, _, player = result
        name = str(call.data.get("name", "")).strip()
        if not name:
            return
        player["name"] = name
        await _save_players(hass, entry_id)
        await _broadcast(entry_id, player["id"])

    async def _update_alias(call: ServiceCall) -> None:
        result = _find_player_by_target(hass, call.data.get("entity_id"), call.data.get("player_id"))
        if result is None:
            return
        entry_id, _, player = result
        alias = str(call.data.get("alias", "")).strip()
        if not alias:
            return
        player["alias"] = alias
        await _save_players(hass, entry_id)
        await _broadcast(entry_id, player["id"])

    async def _update_photo(call: ServiceCall) -> None:
        result = _find_player_by_target(hass, call.data.get("entity_id"), call.data.get("player_id"))
        if result is None:
            return
        entry_id, _, player = result
        player["photo"] = _normalize_media_value(call.data.get("photo", ""))
        await _save_players(hass, entry_id)
        await _broadcast(entry_id, player["id"])

    async def _reset_player_score(call: ServiceCall) -> None:
        result = _find_player_by_target(hass, call.data.get("entity_id"), call.data.get("player_id"))
        if result is None:
            return
        entry_id, _, player = result
        player[ATTR_TOTAL_SCORE] = 0
        player[ATTR_ROUND_SCORE] = 0
        player[ATTR_LAST_ROUND_SCORE] = 0
        await _save_players(hass, entry_id)
        await _broadcast(entry_id, player["id"])

    async def _change_points(call: ServiceCall, multiplier: int) -> None:
        result = _find_player_by_target(hass, call.data.get("entity_id"), call.data.get("player_id"))
        if result is None:
            return
        entry_id, _, player = result
        points = int(call.data["points"]) * multiplier
        player[ATTR_ROUND_SCORE] = int(player.get(ATTR_ROUND_SCORE, 0)) + points
        await _save_players(hass, entry_id)
        await _broadcast(entry_id, player["id"])

    async def _add_points(call: ServiceCall) -> None:
        await _change_points(call, 1)

    async def _remove_points(call: ServiceCall) -> None:
        await _change_points(call, -1)

    async def _use_joker(call: ServiceCall) -> None:
        result = _find_player_by_target(hass, call.data.get("entity_id"), call.data.get("player_id"))
        if result is None:
            return
        entry_id, _, player = result
        player[ATTR_ROUND_SCORE] = int(player.get(ATTR_ROUND_SCORE, 0)) * 2
        await _save_players(hass, entry_id)
        await _broadcast(entry_id, player["id"])

    async def _set_quiz_rounds(call: ServiceCall) -> None:
        rounds = _normalize_round_inputs(call.data.get("rounds"))
        raw_active_index = call.data.get("active_round_index")
        raw_position_index = call.data.get("round_position_index")
        for entry_id in hass.data.get(DOMAIN, {}):
            if not entry_id_supports_quiz(hass, entry_id):
                continue
            data = await _ensure_runtime(hass, entry_id)
            active_round_index = _coerce_round_index(raw_active_index, len(rounds))
            round_position_index = _coerce_round_index(raw_position_index, len(rounds))
            if round_position_index is None:
                if active_round_index is not None:
                    round_position_index = active_round_index
                else:
                    round_position_index = _coerce_round_index(
                        data.get("quiz_round_position_index"),
                        len(rounds),
                    )
            data["quiz_rounds"] = rounds
            data["quiz_active_round_index"] = active_round_index
            data["quiz_round_position_index"] = round_position_index
            await _save_players(hass, entry_id)
            await _broadcast_rounds(entry_id)
            return

    async def _end_round(call: ServiceCall) -> None:
        del call
        for entry_id in hass.data.get(DOMAIN, {}):
            if not entry_id_supports_quiz(hass, entry_id):
                continue
            data = await _ensure_runtime(hass, entry_id)
            if not _end_round_internal(data):
                continue
            await _save_players(hass, entry_id)
            await _broadcast(entry_id)

    async def _start_round(call: ServiceCall) -> None:
        del call
        for entry_id in hass.data.get(DOMAIN, {}):
            if not entry_id_supports_quiz(hass, entry_id):
                continue
            data = await _ensure_runtime(hass, entry_id)
            if not _start_round_internal(data):
                continue
            await _save_players(hass, entry_id)
            await _broadcast(entry_id)

    async def _start_new_round(call: ServiceCall) -> None:
        del call
        for entry_id in hass.data.get(DOMAIN, {}):
            if not entry_id_supports_quiz(hass, entry_id):
                continue
            data = await _ensure_runtime(hass, entry_id)
            changed = _end_round_internal(data)
            changed = _start_round_internal(data) or changed
            if not changed:
                continue
            await _save_players(hass, entry_id)
            await _broadcast(entry_id)

    async def _start_new_quiz(call: ServiceCall) -> None:
        del call
        for entry_id in hass.data.get(DOMAIN, {}):
            if not entry_id_supports_quiz(hass, entry_id):
                continue
            data = await _ensure_runtime(hass, entry_id)
            for player in data["quiz_players"].values():
                player[ATTR_TOTAL_SCORE] = 0
                player[ATTR_ROUND_SCORE] = 0
                player[ATTR_LAST_ROUND_SCORE] = 0
            data["quiz_active_round_index"] = 0 if data.get("quiz_rounds") else None
            data["quiz_round_position_index"] = 0 if data.get("quiz_rounds") else None
            await _save_players(hass, entry_id)
            await _broadcast(entry_id)

    async def _reset_quiz(call: ServiceCall) -> None:
        del call
        for entry_id in hass.data.get(DOMAIN, {}):
            if not entry_id_supports_quiz(hass, entry_id):
                continue
            data = await _ensure_runtime(hass, entry_id)
            for player in data["quiz_players"].values():
                player[ATTR_TOTAL_SCORE] = 0
                player[ATTR_ROUND_SCORE] = 0
                player[ATTR_LAST_ROUND_SCORE] = 0
                player[ATTR_PLAYER_ENABLED] = False
            data["quiz_active_round_index"] = None
            data["quiz_round_position_index"] = None
            await _save_players(hass, entry_id)
            await _broadcast(entry_id)

    target_schema = vol.Schema(
        {
            vol.Optional("entity_id"): vol.Any(cv.entity_id, [cv.entity_id]),
            vol.Optional("player_id"): cv.string,
        }
    )
    points_schema = vol.Schema(
        {
            vol.Optional("entity_id"): vol.Any(cv.entity_id, [cv.entity_id]),
            vol.Optional("player_id"): cv.string,
            vol.Required("points"): vol.Coerce(int),
        }
    )
    set_rounds_schema = vol.Schema(
        {
            vol.Required("rounds"): [cv.string],
            vol.Optional("active_round_index"): vol.Any(None, vol.Coerce(int)),
            vol.Optional("round_position_index"): vol.Any(None, vol.Coerce(int)),
        }
    )
    rename_schema = vol.Schema(
        {
            vol.Optional("entity_id"): vol.Any(cv.entity_id, [cv.entity_id]),
            vol.Optional("player_id"): cv.string,
            vol.Required("name"): cv.string,
        }
    )
    alias_schema = vol.Schema(
        {
            vol.Optional("entity_id"): vol.Any(cv.entity_id, [cv.entity_id]),
            vol.Optional("player_id"): cv.string,
            vol.Required("alias"): cv.string,
        }
    )
    photo_schema = vol.Schema(
        {
            vol.Optional("entity_id"): vol.Any(cv.entity_id, [cv.entity_id]),
            vol.Optional("player_id"): cv.string,
            vol.Required("photo"): vol.Any(
                cv.string,
                MEDIA_SELECTOR_SCHEMA,
            ),
        }
    )

    if not hass.services.has_service(DOMAIN, SERVICE_ADD_PLAYER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_PLAYER,
            _add_player,
            schema=vol.Schema(
                {
                    vol.Required("name"): cv.string,
                    vol.Required("alias"): cv.string,
                    vol.Optional("photo", default=""): vol.Any(
                        cv.string,
                        MEDIA_SELECTOR_SCHEMA,
                    ),
                }
            ),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REMOVE_PLAYER):
        hass.services.async_register(DOMAIN, SERVICE_REMOVE_PLAYER, _remove_player, schema=target_schema)

    if not hass.services.has_service(DOMAIN, SERVICE_ENABLE_PLAYER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_ENABLE_PLAYER,
            _enable_player,
            schema=target_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_DISABLE_PLAYER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DISABLE_PLAYER,
            _disable_player,
            schema=target_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RENAME_PLAYER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RENAME_PLAYER,
            _rename_player,
            schema=rename_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_PLAYER_ALIAS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_PLAYER_ALIAS,
            _update_alias,
            schema=alias_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_PLAYER_PHOTO):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_PLAYER_PHOTO,
            _update_photo,
            schema=photo_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RESET_PLAYER_SCORE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESET_PLAYER_SCORE,
            _reset_player_score,
            schema=target_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_ADD_POINTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_POINTS,
            _add_points,
            schema=points_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REMOVE_POINTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_POINTS,
            _remove_points,
            schema=points_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_USE_JOKER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_USE_JOKER,
            _use_joker,
            schema=target_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_QUIZ_ROUNDS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_QUIZ_ROUNDS,
            _set_quiz_rounds,
            schema=set_rounds_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_END_ROUND):
        hass.services.async_register(DOMAIN, SERVICE_END_ROUND, _end_round)

    if not hass.services.has_service(DOMAIN, SERVICE_START_ROUND):
        hass.services.async_register(DOMAIN, SERVICE_START_ROUND, _start_round)

    if not hass.services.has_service(DOMAIN, SERVICE_START_NEW_ROUND):
        hass.services.async_register(DOMAIN, SERVICE_START_NEW_ROUND, _start_new_round)

    if not hass.services.has_service(DOMAIN, SERVICE_START_NEW_QUIZ):
        hass.services.async_register(DOMAIN, SERVICE_START_NEW_QUIZ, _start_new_quiz)

    if not hass.services.has_service(DOMAIN, SERVICE_RESET_QUIZ):
        hass.services.async_register(DOMAIN, SERVICE_RESET_QUIZ, _reset_quiz)


async def async_sync_players_from_storage(hass: HomeAssistant, entry_id: str) -> None:
    """Synchronize runtime entities with stored players."""
    data = await _ensure_runtime(hass, entry_id)
    quiz_data = await data["quiz_store"].async_load() or {"players": []}
    new_players = _normalize_players(quiz_data)
    data["quiz_rounds"] = _normalize_rounds(quiz_data)
    data["quiz_active_round_index"] = _normalize_active_round_index(quiz_data, data["quiz_rounds"])
    data["quiz_round_position_index"] = _normalize_round_position_index(quiz_data, data["quiz_rounds"])

    existing_ids = set(data["quiz_players"])
    new_ids = set(new_players)

    for removed_player_id in existing_ids - new_ids:
        for entity in data.get("quiz_sensor_entities", {}).pop(removed_player_id, []):
            await entity.async_remove()
        binary_entity = data.get("quiz_binary_entities", {}).pop(removed_player_id, None)
        if binary_entity is not None:
            await binary_entity.async_remove()
        switch_entity = data.get("quiz_switch_entities", {}).pop(removed_player_id, None)
        if switch_entity is not None:
            await switch_entity.async_remove()
        for entity in data.get("quiz_text_entities", {}).pop(removed_player_id, []):
            await entity.async_remove()
        button_entity = data.get("quiz_button_entities", {}).pop(removed_player_id, None)
        if button_entity is not None:
            await button_entity.async_remove()

    for player_id in existing_ids & new_ids:
        data["quiz_players"][player_id].update(new_players[player_id])
        async_dispatcher_send(hass, f"{QUIZ_SIGNAL_UPDATE}_{entry_id}_{player_id}")

    data["quiz_players"].update({player_id: new_players[player_id] for player_id in new_ids - existing_ids})

    added_player_ids = sorted(new_ids - existing_ids)
    sensor_add = data.get("quiz_sensor_add_entities")
    if sensor_add and added_player_ids:
        sensor_entities: list[SensorEntity] = []
        for player_id in added_player_ids:
            sensor_entities.extend(_build_player_sensor_entities(hass, entry_id, player_id))
        sensor_add(sensor_entities)
        _store_sensor_entities(data, sensor_entities)

    binary_add = data.get("quiz_binary_add_entities")
    if binary_add and added_player_ids:
        binary_entities = [
            QuizPlayerEnabledBinarySensor(hass, entry_id, player_id)
            for player_id in added_player_ids
        ]
        binary_add(binary_entities)
        for entity in binary_entities:
            data["quiz_binary_entities"][entity.player_id] = entity

    switch_add = data.get("quiz_switch_add_entities")
    if switch_add and added_player_ids:
        switch_entities = [
            QuizPlayerEnabledSwitch(hass, entry_id, player_id)
            for player_id in added_player_ids
        ]
        switch_add(switch_entities)
        for entity in switch_entities:
            data["quiz_switch_entities"][entity.player_id] = entity

    text_add = data.get("quiz_text_add_entities")
    if text_add and added_player_ids:
        text_entities: list[TextEntity] = []
        for player_id in added_player_ids:
            text_entities.extend(_build_player_text_entities(hass, entry_id, player_id))
        text_add(text_entities)
        _store_text_entities(data, text_entities)

    button_add = data.get("quiz_button_add_entities")
    if button_add and added_player_ids:
        button_entities = [
            QuizPlayerResetScoreButton(hass, entry_id, player_id)
            for player_id in added_player_ids
        ]
        button_add(button_entities)
        for entity in button_entities:
            data["quiz_button_entities"][entity.player_id] = entity

    async_dispatcher_send(hass, f"{QUIZ_SIGNAL_UPDATE}_{entry_id}_rounds")


class QuizEntityBase:
    """Shared helpers for quiz entities."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.player_id = player_id

    @property
    def _player(self) -> dict[str, Any] | None:
        return _entry_data(self.hass, self.entry_id).get("quiz_players", {}).get(self.player_id)

    @property
    def _device_info(self) -> DeviceInfo:
        player = self._player or {}
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry_id}_{self.player_id}")},
            name=player.get("name") or f"RH Quiz {self.player_id}",
            manufacturer="Raven House",
            model="Raven House Quiz Player",
        )

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info

    def _common_attributes(self) -> dict[str, Any]:
        player = self._player or {}
        return {
            ATTR_PLAYER_ID: self.player_id,
            ATTR_PLAYER_NAME: player.get("name", ""),
            ATTR_PLAYER_ALIAS: player.get("alias", ""),
            ATTR_PLAYER_PHOTO: player.get("photo", ""),
            ATTR_PLAYER_ENABLED: bool(player.get("enabled", False)),
            ATTR_TOTAL_SCORE: int(player.get("total_score", 0)),
            ATTR_ROUND_SCORE: int(player.get("current_round_score", 0)),
            ATTR_LAST_ROUND_SCORE: int(player.get("last_round_score", 0)),
            ATTR_CREATED: player.get("created"),
        }

    async def _subscribe_updates(self) -> Callable[[], None]:
        @callback
        def _handle_update() -> None:
            self.async_write_ha_state()

        return async_dispatcher_connect(
            self.hass,
            f"{QUIZ_SIGNAL_UPDATE}_{self.entry_id}_{self.player_id}",
            _handle_update,
        )


class QuizSensorBase(QuizEntityBase, SensorEntity):
    """Base class for player sensors."""

    @property
    def available(self) -> bool:
        return self._player is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(await self._subscribe_updates())


class QuizRoundsSensor(SensorEntity):
    """Quiz rounds status entity."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_rounds"
        self.entity_id = "sensor.rh_quiz_rounds"
        self._attr_name = "Quiz Rounds"

    @property
    def available(self) -> bool:
        return self.entry_id in self.hass.data.get(DOMAIN, {})

    async def async_added_to_hass(self) -> None:
        @callback
        def _handle_update() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{QUIZ_SIGNAL_UPDATE}_{self.entry_id}_rounds",
                _handle_update,
            )
        )

    @property
    def native_value(self) -> str | None:
        data = _entry_data(self.hass, self.entry_id)
        return _active_round_name(
            data.get("quiz_rounds", []),
            data.get("quiz_active_round_index"),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = _entry_data(self.hass, self.entry_id)
        rounds = list(data.get("quiz_rounds", []))
        active_round_index = data.get("quiz_active_round_index")
        round_position_index = data.get("quiz_round_position_index")
        return {
            ATTR_PLAYER_METRIC: "rounds",
            ATTR_QUIZ_ROUNDS: rounds,
            ATTR_ACTIVE_ROUND_INDEX: active_round_index,
            ATTR_ACTIVE_ROUND_NAME: _active_round_name(rounds, active_round_index),
            ATTR_ROUND_POSITION_INDEX: round_position_index,
            ATTR_TOTAL_ROUNDS: len(rounds),
        }


class QuizPlayerEnabledBinarySensor(QuizEntityBase, BinarySensorEntity):
    """Binary sensor exposing whether a player is enabled."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_enabled"
        self.entity_id = f"binary_sensor.{PREFIX_QUIZ}_{player_id}_enabled"
        self._attr_name = "Enabled"

    @property
    def available(self) -> bool:
        return self._player is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(await self._subscribe_updates())

    @property
    def is_on(self) -> bool:
        player = self._player or {}
        return bool(player.get("enabled", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._common_attributes()
        attrs[ATTR_PLAYER_METRIC] = "enabled"
        return attrs


class QuizTotalScoreSensor(QuizSensorBase):
    """Primary total score entity for a player device."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_total"
        self.entity_id = f"sensor.{PREFIX_QUIZ}_{player_id}"

    @property
    def name(self) -> str:
        player = self._player or {}
        return player.get("name") or f"RH Quiz {self.player_id}"

    @property
    def native_value(self) -> int | None:
        player = self._player
        if not player:
            return None
        return int(player.get("total_score", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._common_attributes()
        attrs[ATTR_PLAYER_METRIC] = "total_score"
        return attrs


class QuizRoundScoreSensor(QuizSensorBase):
    """Round score entity for a player device."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_round"
        self.entity_id = f"sensor.{PREFIX_QUIZ}_{player_id}_round"
        self._attr_name = "Round Score"

    @property
    def native_value(self) -> int | None:
        player = self._player
        if not player:
            return None
        return int(player.get("current_round_score", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._common_attributes()
        attrs[ATTR_PLAYER_METRIC] = "round_score"
        return attrs


class QuizLastRoundScoreSensor(QuizSensorBase):
    """Last round score entity for a player device."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_last_round"
        self.entity_id = f"sensor.{PREFIX_QUIZ}_{player_id}_last_round"
        self._attr_name = "Last Round Score"

    @property
    def native_value(self) -> int | None:
        player = self._player
        if not player:
            return None
        return int(player.get("last_round_score", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._common_attributes()
        attrs[ATTR_PLAYER_METRIC] = "last_round_score"
        return attrs


class QuizAliasSensor(QuizSensorBase):
    """Alias entity for a player device."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_alias"
        self.entity_id = f"sensor.{PREFIX_QUIZ}_{player_id}_alias"
        self._attr_name = "Alias"

    @property
    def native_value(self) -> str | None:
        player = self._player
        if not player:
            return None
        return player.get("alias") or player.get("name") or self.player_id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._common_attributes()
        attrs[ATTR_PLAYER_METRIC] = "alias"
        return attrs


class QuizPlayerEnabledSwitch(QuizEntityBase, SwitchEntity):
    """Switch to enable or disable a participant."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_enabled_switch"
        self.entity_id = f"switch.{PREFIX_QUIZ}_{player_id}_enabled"
        self._attr_name = "Enabled"

    @property
    def available(self) -> bool:
        return self._player is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(await self._subscribe_updates())

    @property
    def is_on(self) -> bool:
        player = self._player or {}
        return bool(player.get("enabled", False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        del kwargs
        player = self._player
        if not player:
            return
        player["enabled"] = True
        await _save_players(self.hass, self.entry_id)
        async_dispatcher_send(self.hass, f"{QUIZ_SIGNAL_UPDATE}_{self.entry_id}_{self.player_id}")

    async def async_turn_off(self, **kwargs: Any) -> None:
        del kwargs
        player = self._player
        if not player:
            return
        player["enabled"] = False
        await _save_players(self.hass, self.entry_id)
        async_dispatcher_send(self.hass, f"{QUIZ_SIGNAL_UPDATE}_{self.entry_id}_{self.player_id}")


class QuizPlayerNameText(QuizEntityBase, TextEntity):
    """Text entity for participant display name."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_name"
        self.entity_id = f"text.{PREFIX_QUIZ}_{player_id}_name"
        self._attr_name = "Name"
        self._attr_native_min = 1
        self._attr_native_max = 120

    @property
    def available(self) -> bool:
        return self._player is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(await self._subscribe_updates())

    @property
    def native_value(self) -> str:
        player = self._player or {}
        return player.get("name", "")

    async def async_set_value(self, value: str) -> None:
        name = value.strip()
        if not name:
            return
        player = self._player
        if not player:
            return
        player["name"] = name
        await _save_players(self.hass, self.entry_id)
        async_dispatcher_send(self.hass, f"{QUIZ_SIGNAL_UPDATE}_{self.entry_id}_{self.player_id}")


class QuizPlayerAliasText(QuizEntityBase, TextEntity):
    """Text entity for participant alias."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_alias_text"
        self.entity_id = f"text.{PREFIX_QUIZ}_{player_id}_alias"
        self._attr_name = "Alias"
        self._attr_native_min = 1
        self._attr_native_max = 120

    @property
    def available(self) -> bool:
        return self._player is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(await self._subscribe_updates())

    @property
    def native_value(self) -> str:
        player = self._player or {}
        return player.get("alias", "")

    async def async_set_value(self, value: str) -> None:
        alias = value.strip()
        if not alias:
            return
        player = self._player
        if not player:
            return
        player["alias"] = alias
        await _save_players(self.hass, self.entry_id)
        async_dispatcher_send(self.hass, f"{QUIZ_SIGNAL_UPDATE}_{self.entry_id}_{self.player_id}")


class QuizPlayerPhotoText(QuizEntityBase, TextEntity):
    """Text entity for participant image path."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_photo"
        self.entity_id = f"text.{PREFIX_QUIZ}_{player_id}_photo"
        self._attr_name = "Photo"
        self._attr_native_min = 0
        self._attr_native_max = 512

    @property
    def available(self) -> bool:
        return self._player is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(await self._subscribe_updates())

    @property
    def native_value(self) -> str:
        player = self._player or {}
        return player.get("photo", "")

    async def async_set_value(self, value: str) -> None:
        player = self._player
        if not player:
            return
        player["photo"] = value.strip()
        await _save_players(self.hass, self.entry_id)
        async_dispatcher_send(self.hass, f"{QUIZ_SIGNAL_UPDATE}_{self.entry_id}_{self.player_id}")


class QuizPlayerResetScoreButton(QuizEntityBase, ButtonEntity):
    """Button to reset one participant's scores."""

    def __init__(self, hass: HomeAssistant, entry_id: str, player_id: str) -> None:
        super().__init__(hass, entry_id, player_id)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{player_id}_reset_score"
        self.entity_id = f"button.{PREFIX_QUIZ}_{player_id}_reset_score"
        self._attr_name = "Reset Score"

    @property
    def available(self) -> bool:
        return self._player is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(await self._subscribe_updates())

    async def async_press(self) -> None:
        player = self._player
        if not player:
            return
        player[ATTR_TOTAL_SCORE] = 0
        player[ATTR_ROUND_SCORE] = 0
        player[ATTR_LAST_ROUND_SCORE] = 0
        await _save_players(self.hass, self.entry_id)
        async_dispatcher_send(self.hass, f"{QUIZ_SIGNAL_UPDATE}_{self.entry_id}_{self.player_id}")
