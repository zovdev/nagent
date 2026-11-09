"""Event types and payloads for the append-only history log.

Every state change in a chat is an event. The effective (model-visible)
history is a replay of content events with tombstone/edit applied.
"""

from __future__ import annotations

import json
import time
import uuid

# Content events (become part of the model-visible history)
USER_MESSAGE = "user_message"
ASSISTANT_THINK = "assistant_think"   # a completed think block (text)
ASSISTANT_TEXT = "assistant_text"      # a completed text block
TOOL_CALL = "tool_call"                # model requested a tool
TOOL_RESULT = "tool_result"            # tool execution result
TURN_META = "turn_meta"                # turn boundary: model used, usage stats
AGENT_NOTICE = "agent_notice"          # server -> model note (injected as a user
                                       # role message; e.g. tool-failure hint)
COMPACT = "compact"                    # compaction summary; ALSO a mutation:
                                       # data.forgotten_event_ids get status
                                       # 'forgotten' (model does not see them,
                                       # the user still does — OpenHands pattern)
PLAN_APPROVED = "plan_approved"        # user-approved plan + checklist artifact
                                       # (model-visible; the exploration that
                                       # produced it is masked away)

# Mutation events (change how content events replay)
EDIT = "edit"                          # replaces payload of target event
DELETE = "delete"                      # tombstones target event
MASK = "mask"                          # hides targets from MODEL context only
                                       # (§15 timeout-retry / plan-mode cleanup)

# Lifecycle
INTERRUPT = "interrupt"                # generation stopped by user
ERROR = "error"                        # provider/tool error (user-visible)
FALLBACK = "fallback"                  # cascade switched model mid-turn (user-visible)
PERMISSION_REQUEST = "permission_request"  # tool needs user approval (ask mode)
PERMISSION_RESULT = "permission_result"    # the decision the user made
CHECKPOINT = "checkpoint"              # shadow-git checkpoint for a finished turn
SUBAGENT_START = "subagent_start"      # Task tool spawned a zero-context subagent
SUBAGENT_PROGRESS = "subagent_progress"    # per-iteration progress (user-visible)
SUBAGENT_END = "subagent_end"          # subagent finished (result preview)
PLAN_SUBMITTED = "plan_submitted"      # plan mode produced a plan, awaiting user
PLAN_REJECTED = "plan_rejected"        # user declined the plan
SUMMARIZE = "summarize"                # two-step tool-output summarization (§11):
                                       # intermediate requests — user-visible,
                                       # NEVER part of the model context
DETECTOR = "detector"                  # repeat / tool-failure warning (§5)

# Phase 4 (§13 web layer)
BG_START = "bg_start"                  # background task started (terminal tab)
BG_END = "bg_end"                      # background task finished / killed
NOTIFICATION = "notification"          # user toast + optional sound (§10)

CONTENT_TYPES = {USER_MESSAGE, ASSISTANT_THINK, ASSISTANT_TEXT, TOOL_CALL,
                 TOOL_RESULT, AGENT_NOTICE, COMPACT, PLAN_APPROVED}
MUTATION_TYPES = {EDIT, DELETE, MASK}


def new_id() -> str:
    return uuid.uuid4().hex


def make_event(chat_id: str, seq: int, type_: str, data: dict | None = None,
               target: str | None = None) -> dict:
    return {
        "id": new_id(),
        "chat_id": chat_id,
        "seq": seq,
        "ts": time.time(),
        "type": type_,
        "data": data or {},
        "target": target,          # for EDIT/DELETE: id of the event affected
        "status": "active",        # active | deleted | replaced
    }


def dumps(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


def model_role(event: dict) -> str | None:
    """OpenAI role this effective event maps to (None = not sent to model)."""
    t = event["type"]
    if t in (USER_MESSAGE, AGENT_NOTICE, COMPACT, PLAN_APPROVED):
        return "user"
    if t in (ASSISTANT_THINK, ASSISTANT_TEXT, TOOL_CALL):
        return "assistant"
    if t == TOOL_RESULT:
        return "tool"
    return None


def timeout_error_payload(tool: str, seconds: float, cap: float) -> dict:
    """The §15 timeout protocol message the model sees instead of a result."""
    return {
        "error": "timeout",
        "timeout": True,
        "message": (
            f"Вызов {tool} не успел завершиться за {seconds:g} сек. "
            f"Если задача долгая — вызови ТОТ ЖЕ тулкалинг с теми же аргументами, "
            f"добавив параметр timeout=N (секунды, максимум {cap:g}). "
            f"Неудачный вызов будет затёрт из контекста автоматически."
        ),
    }
