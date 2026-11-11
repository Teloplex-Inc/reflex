#!/usr/bin/env python3
"""LangChain/LangSmith adapter — Reflex as a pre-router for chains and as a judge.
Use 1 (routing): wrap your chain entry:
    from reflex.adapters.langchain import route
    tier = route(query)   # -> "cheap" | "frontier"
Use 2 (judge): score agent traces against rubric questions for cheap evals at scale."""
from reflex.adapters.hermes import gate

def route(query: str) -> str:
    v = gate(query)
    if v["action"] == "skip_to_handler": return "cheap"   # direct handler
    return "frontier"                                      # everything else
