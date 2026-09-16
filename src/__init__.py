"""猫娘聊天。"""

from .state import GameState, SaveStore, estimate_tokens, tone_profile
from .scoring import Proposal, Rules, ScoreResult, load_rules, resolve
from .engine import Engine, TurnReport
from .memory import (
    CompressDecision,
    apply_compression,
    decide as decide_compression,
    detect_drift,
    merge_bible,
    render_bible,
    search_blocks,
    should_inject_style,
    style_anchor_text,
)

__all__ = [
    "GameState",
    "SaveStore",
    "estimate_tokens",
    "tone_profile",
    "Proposal",
    "Rules",
    "ScoreResult",
    "load_rules",
    "resolve",
    "Engine",
    "TurnReport",
    "CompressDecision",
    "apply_compression",
    "decide_compression",
    "detect_drift",
    "merge_bible",
    "render_bible",
    "search_blocks",
    "should_inject_style",
    "style_anchor_text",
]
