from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.memory.voice import DEFAULT_VOICE_PROFILE, VoiceProfile


@dataclass
class MemoryStore:
    """Zywa pamiec systemu: semantyczna (glos redakcji) + robocza (stan runu).

    Glos idzie do QualityJudge i agentow LLM; pamiec robocza trzyma artefakty
    kroku w obrebie jednego runu.
    """

    voice: VoiceProfile = field(default_factory=lambda: DEFAULT_VOICE_PROFILE)
    working: dict[str, Any] = field(default_factory=dict)

    def write(self, key: str, value: Any) -> None:
        self.working[key] = value

    def banned_phrases(self) -> list[str]:
        return self.voice.banned_phrases

