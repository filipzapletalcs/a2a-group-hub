# src/telegram/config.py
"""Configuration for Telegram bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# Default OpenClaw topic mapping (discovered 2026-03-28)
DEFAULT_CHANNEL_TOPIC_MAP = {
    "leaders": 3,
    "dev-team": 4,
    "testing-team": 5,
    "ops-team": 6,
    "pm-team": 7,
    "knowledge-team": 8,
}

# Agent name → TG_TOKEN env var name
AGENT_TOKEN_MAP = {
    "nexus": "TG_TOKEN_NEXUS",
    "apollo": "TG_TOKEN_APOLLO",
    "rex": "TG_TOKEN_REX",
    "pixel": "TG_TOKEN_PIXEL",
    "nova": "TG_TOKEN_NOVA",
    "swift": "TG_TOKEN_SWIFT",
    "hawk": "TG_TOKEN_HAWK",
    "sentinel": "TG_TOKEN_SENTINEL",
    "shield": "TG_TOKEN_SHIELD",
    "phantom": "TG_TOKEN_PHANTOM",
    "lens": "TG_TOKEN_LENS",
    "aria": "TG_TOKEN_ARIA",
    "forge": "TG_TOKEN_FORGE",
    "bolt": "TG_TOKEN_BOLT",
    "iris": "TG_TOKEN_IRIS",
    "kai": "TG_TOKEN_KAI",
    "mona": "TG_TOKEN_MONA",
    "atlas": "TG_TOKEN_ATLAS",
    "echo": "TG_TOKEN_ECHO",
    "sage": "TG_TOKEN_SAGE",
    "quill": "TG_TOKEN_QUILL",
    "scout": "TG_TOKEN_SCOUT",
    "archi": "TG_TOKEN_ARCHI",
    "vigil": "TG_TOKEN_VIGIL",
}


@dataclass
class TelegramConfig:
    """Telegram bridge configuration, typically loaded from environment."""

    enabled: bool = False
    bot_token: str = ""          # System bot — for polling + error log
    chat_id: int = 0
    channel_topic_map: dict[str, int] = field(default_factory=dict)
    agent_tokens: dict[str, str] = field(default_factory=dict)  # agent_id → bot token

    @classmethod
    def from_env(cls) -> TelegramConfig:
        """Load config from environment variables."""
        enabled = os.environ.get("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes")

        # Load topic map from env or use defaults
        topic_map = dict(DEFAULT_CHANNEL_TOPIC_MAP)
        for channel_id, default_topic in DEFAULT_CHANNEL_TOPIC_MAP.items():
            env_key = "TG_TOPIC_" + channel_id.upper().replace("-", "_")
            val = os.environ.get(env_key)
            if val:
                topic_map[channel_id] = int(val)

        # Load agent bot tokens from env
        agent_tokens: dict[str, str] = {}
        for agent_id, env_key in AGENT_TOKEN_MAP.items():
            token = os.environ.get(env_key, "")
            if token:
                agent_tokens[agent_id] = token

        return cls(
            enabled=enabled,
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=int(os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TG_SUPERGROUP_ID", "0"))),
            channel_topic_map=topic_map,
            agent_tokens=agent_tokens,
        )
