from __future__ import annotations

import inspect
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .topic_shift_detector import TopicShiftDetector


TitleModel = Callable[[dict[str, Any]], str | Awaitable[str]]


@dataclass(frozen=True, slots=True)
class TitleGenerationRequest:
    session_id: str
    understood_intent: str
    current_title: str
    reason: str
    output_contract: dict[str, Any] = field(
        default_factory=lambda: {
            "language": "zh-TW",
            "max_characters": 24,
            "style": "specific_noun_phrase",
            "forbid": ["新對話", "Stock AI Agent 對話", "問題", "聊天"],
        }
    )

    def to_payload(self) -> dict[str, Any]:
        return {
            "task": "generate_session_title",
            "session_id": self.session_id,
            "understood_intent": self.understood_intent,
            "current_title": self.current_title,
            "reason": self.reason,
            "output_contract": dict(self.output_contract),
        }


@dataclass(frozen=True, slots=True)
class TitleRevision:
    revision_id: str
    session_id: str
    title: str
    previous_title: str | None
    reason: str
    source: str
    created_at: str = field(default_factory=lambda: _now())


class SessionTitleHistory:
    """Append-only title history contract; persistence is supplied by the Host."""

    def __init__(self, session_id: str, *, temporary_title: str = "新對話") -> None:
        self.session_id = session_id
        self._revisions: list[TitleRevision] = [
            TitleRevision(
                revision_id=f"TITLE-{uuid4().hex}",
                session_id=session_id,
                title=_clean_title(temporary_title),
                previous_title=None,
                reason="temporary",
                source="host",
            )
        ]

    @property
    def current_title(self) -> str:
        return self._revisions[-1].title

    @property
    def revisions(self) -> tuple[TitleRevision, ...]:
        return tuple(self._revisions)

    def append(self, title: str, *, reason: str, source: str) -> TitleRevision:
        cleaned = _clean_title(title)
        if cleaned == self.current_title:
            return self._revisions[-1]
        revision = TitleRevision(
            revision_id=f"TITLE-{uuid4().hex}",
            session_id=self.session_id,
            title=cleaned,
            previous_title=self.current_title,
            reason=reason,
            source=source,
        )
        self._revisions.append(revision)
        return revision


class SessionTitleGenerator:
    """Generate semantic titles after intent understanding and revise on topic shift."""

    def __init__(
        self,
        model: TitleModel | None = None,
        *,
        topic_shift_detector: TopicShiftDetector | None = None,
    ) -> None:
        self.model = model
        self.topic_shift_detector = topic_shift_detector or TopicShiftDetector()

    async def generate(
        self,
        history: SessionTitleHistory,
        *,
        understood_intent: str,
        previous_intent: str = "",
    ) -> TitleRevision:
        shift = self.topic_shift_detector.detect(previous_intent, understood_intent)
        reason = "topic_shift" if shift.shifted else "first_intent_understood"
        request = TitleGenerationRequest(
            session_id=history.session_id,
            understood_intent=understood_intent,
            current_title=history.current_title,
            reason=reason,
        )
        title = await self._invoke(request)
        return history.append(
            title,
            reason=reason,
            source="model" if self.model else "deterministic_fallback",
        )

    async def _invoke(self, request: TitleGenerationRequest) -> str:
        if self.model is None:
            return fallback_title(request.understood_intent)
        result = self.model(request.to_payload())
        title = await result if inspect.isawaitable(result) else result
        return _clean_title(str(title))


# Compatibility names used by the session domain package. Title records stay
# immutable and stores append revisions rather than overwriting title history.
SessionTitle = TitleRevision


class InMemoryTitleHistoryStore:
    def __init__(self) -> None:
        self._items: dict[str, list[SessionTitle]] = {}

    def save(self, title: SessionTitle) -> None:
        items = self._items.setdefault(title.session_id, [])
        if any(item.revision_id == title.revision_id for item in items):
            raise ValueError(f"Title revision already exists: {title.revision_id}")
        items.append(title)

    def list_for_session(self, session_id: str) -> list[SessionTitle]:
        return list(self._items.get(session_id, ()))

    def current(self, session_id: str) -> SessionTitle | None:
        items = self._items.get(session_id, ())
        return items[-1] if items else None


class SQLiteTitleHistoryStore:
    """Persistence adapter for Host-created title tables; it performs no migration."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()

    def save(self, title: SessionTitle) -> None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "select coalesce(max(revision), 0) from agent_session_title_history where session_id=?",
                (title.session_id,),
            ).fetchone()
            revision = int(row[0]) + 1
            payload = json.dumps(
                {
                    "source": title.source,
                    "previous_title": title.previous_title,
                    "revision_id": title.revision_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            conn.execute(
                """
                insert into agent_session_title_history(
                    title_history_id, session_id, revision, title, reason, created_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title.revision_id,
                    title.session_id,
                    revision,
                    title.title,
                    title.reason,
                    title.created_at,
                    payload,
                ),
            )

    def list_for_session(self, session_id: str) -> list[SessionTitle]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_session_title_history
                 where session_id=? order by revision
                """,
                (session_id,),
            ).fetchall()
        result: list[SessionTitle] = []
        previous: str | None = None
        for row in rows:
            metadata = json.loads(row["payload_json"] or "{}")
            result.append(
                SessionTitle(
                    revision_id=row["title_history_id"],
                    session_id=row["session_id"],
                    title=row["title"],
                    previous_title=metadata.get("previous_title", previous),
                    reason=row["reason"],
                    source=str(metadata.get("source") or "host"),
                    created_at=row["created_at"],
                )
            )
            previous = row["title"]
        return result

    def current(self, session_id: str) -> SessionTitle | None:
        items = self.list_for_session(session_id)
        return items[-1] if items else None


class TitleGenerator(SessionTitleGenerator):
    """Canonical session-domain name retained alongside the explicit class name."""


def fallback_title(understood_intent: str) -> str:
    """Specific non-LLM fallback used only when no title model is configured."""

    text = re.sub(r"\s+", "", understood_intent.strip())
    text = re.sub(r"^(請|幫我|我想要|想要|可以幫我)", "", text)
    text = re.sub(r"[，。！？?!.、:：；;]+", " ", text).strip()
    candidate = text.split(" ", 1)[0][:24]
    return candidate or "待理解的新對話"


def _clean_title(title: str) -> str:
    cleaned = re.sub(r"[\r\n\t]+", " ", title).strip().strip('"\'`')
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        raise ValueError("Session title cannot be empty")
    return cleaned[:24]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
