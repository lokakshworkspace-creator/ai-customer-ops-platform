"""The agent's state machine: states, legal transitions, and what each state means for the
`workflow_runs.status` column. Pure data and functions: no I/O (golden rule 4: the state itself
lives in Postgres, `workflow_runs.state` / `.context`, and is checkpointed on every transition).

One tool loop per user message ("turn"):

    PROPOSE  --text only-------------------------------> RESPOND --> DONE
       |  ^                                                  |
       |  | results go back to the model                     +--> WAITING_FOR_HUMAN
       v  |                                                         (an approval is pending)
    EXECUTE  (every proposal goes through the tool executor, one at a time)

    PROPOSE / EXECUTE --limit reached--> ESCALATED
    PROPOSE / EXECUTE / RESPOND --error--> FAILED

There is no separate classification step: the model either answers in text or proposes tools.
Policy / FAQ questions are a stub until Phase 4 adds a search tool (the system prompt tells the
model it cannot answer them yet); nothing else in this machine changes when that tool arrives.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.db.models.enums import WorkflowStatus


class State(StrEnum):
    PROPOSE = "PROPOSE"  # asking the model what to do next
    EXECUTE = "EXECUTE"  # running the model's proposals through the executor
    RESPOND = "RESPOND"  # composing the reply
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"  # a refund approval is pending
    DONE = "DONE"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"  # a limit was hit; a person has to take over


TRANSITIONS: dict[State, frozenset[State]] = {
    State.PROPOSE: frozenset({State.EXECUTE, State.RESPOND, State.ESCALATED, State.FAILED}),
    State.EXECUTE: frozenset({State.PROPOSE, State.ESCALATED, State.FAILED}),
    State.RESPOND: frozenset({State.DONE, State.WAITING_FOR_HUMAN, State.FAILED}),
    State.WAITING_FOR_HUMAN: frozenset({State.DONE}),  # resume.py, once the approval is decided
    State.DONE: frozenset(),
    State.FAILED: frozenset(),
    State.ESCALATED: frozenset(),
}

STATUS_OF: dict[State, WorkflowStatus] = {
    State.PROPOSE: WorkflowStatus.running,
    State.EXECUTE: WorkflowStatus.running,
    State.RESPOND: WorkflowStatus.running,
    State.WAITING_FOR_HUMAN: WorkflowStatus.waiting_approval,
    State.DONE: WorkflowStatus.completed,
    State.FAILED: WorkflowStatus.failed,
    State.ESCALATED: WorkflowStatus.escalated,
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


class IllegalTransition(RuntimeError):
    pass


def check_transition(current: State, new: State) -> None:
    if new not in TRANSITIONS[current]:
        raise IllegalTransition(f"{current.value} -> {new.value} is not allowed")


@dataclass(frozen=True, slots=True)
class Caps:
    """Hard limits on tool calls. A model that loops, or is talked into looping, is stopped by
    these, not by anything it says."""

    per_turn: int
    per_conversation: int

    def exceeded(self, used_this_turn: int, used_in_conversation: int) -> bool:
        """True when ONE MORE tool call would go over a limit."""
        return used_this_turn >= self.per_turn or used_in_conversation >= self.per_conversation
