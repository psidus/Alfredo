"""
Marquee area selector for Loop select mode.

LTR (left→right): only fully enclosed blocks.
RTL (right→left): blocks that intersect the rectangle (even partially).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

import streamlit.components.v1 as components

_COMPONENT = components.declare_component(
    "alfredo_marquee_select",
    path=os.path.dirname(__file__),
)


def select_nodes_in_rect(
    nodes: Sequence[Dict[str, Any]],
    *,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> List[str]:
    """
    Apply Figma-like selection rules.

    - Left→right (x2 >= x1): full containment only
    - Right→left (x2 < x1): partial intersection
    """
    left = min(x1, x2)
    right = max(x1, x2)
    top = min(y1, y2)
    bottom = max(y1, y2)
    full = x2 >= x1
    out: List[str] = []
    for n in nodes:
        nid = n.get("id")
        if not nid:
            continue
        nx = float(n.get("x") or 0)
        ny = float(n.get("y") or 0)
        nw = float(n.get("w") or n.get("width") or 0)
        nh = float(n.get("h") or n.get("height") or 0)
        if nw <= 0 or nh <= 0:
            continue
        n_right = nx + nw
        n_bottom = ny + nh
        if full:
            if nx >= left and n_right <= right and ny >= top and n_bottom <= bottom:
                out.append(str(nid))
        else:
            intersects = not (n_right < left or nx > right or n_bottom < top or ny > bottom)
            if intersects:
                out.append(str(nid))
    return out


def render_marquee_select(
    nodes: Sequence[Dict[str, Any]],
    *,
    key: str = "loop_marquee",
    height: int = 360,
) -> Optional[Dict[str, Any]]:
    """
    Render interactive marquee board.

    `nodes` items: {id, label, x, y, w, h}
    Returns last selection payload:
      {ids: [...], x1,y1,x2,y2, direction: 'ltr'|'rtl'} or None
    """
    payload = _COMPONENT(
        nodes=list(nodes),
        key=key,
        default=None,
        height=height,
    )
    if not payload:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    # Prefer ids computed in JS; re-validate in Python for consistency
    ids = select_nodes_in_rect(
        nodes,
        x1=float(payload.get("x1") or 0),
        y1=float(payload.get("y1") or 0),
        x2=float(payload.get("x2") or 0),
        y2=float(payload.get("y2") or 0),
    )
    payload = dict(payload)
    payload["ids"] = ids
    payload["direction"] = "ltr" if float(payload.get("x2") or 0) >= float(payload.get("x1") or 0) else "rtl"
    return payload
