from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# China Standard Time has no DST for the dates supported by this application.
CST = timezone(timedelta(hours=8), name="Asia/Shanghai")


@dataclass(frozen=True)
class Message:
    group_id: str
    member_id: str
    message_id: str
    timestamp: datetime
    text: str
    mentioned: bool = True
    is_group: bool = True
    display_name: str = "训练家"


@dataclass(frozen=True)
class CommandButton:
    label: str
    command: str


@dataclass(frozen=True)
class Reply:
    group_id: str
    mention_member_id: str
    text: str
    image_path: Path | None = None
    buttons: tuple[CommandButton, ...] = ()


@dataclass(frozen=True)
class Draw:
    pokemon_id: int
    day: str
    count: int
    is_new: bool
    repeated_today: bool
    unlocked_achievements: tuple[str, ...] = ()


@dataclass(frozen=True)
class Exploration:
    day: str
    items: tuple[Draw, ...]
    repeated_today: bool
    guaranteed: bool = False
    legacy: bool = False
    unlocked_achievements: tuple[str, ...] = ()
    upgrade_bonus: bool = False
