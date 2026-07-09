"""Optional LangGraph supervisor hook."""
from __future__ import annotations


def langgraph_status() -> dict:
    try:
        import langgraph  # noqa: F401
    except Exception:
        return {"ok": False, "action": "langgraph_status", "target": "langgraph",
                "summary": "LangGraph is not installed in this Python environment",
                "proof": "langgraph_import_checked", "error": "langgraph unavailable"}
    return {"ok": True, "action": "langgraph_status", "target": "langgraph",
            "summary": "LangGraph is available", "proof": "langgraph_import_checked"}


def supervisor_plan(goal: str) -> dict:
    try:
        from langgraph.graph import END, StateGraph
    except Exception:
        return {"ok": False, "action": "supervisor_plan", "target": goal,
                "summary": "LangGraph is not installed in this Python environment",
                "proof": "langgraph_import_checked", "error": "langgraph unavailable"}

    def plan_node(state: dict) -> dict:
        task = state["goal"].strip()
        return {"goal": task, "steps": [
            "Confirm the request and safety constraints",
            "Choose the smallest tool that can do the work",
            "Run the tool through the supervisor ledger",
            "Verify proof before reporting completion",
        ]}

    graph = StateGraph(dict)
    graph.add_node("plan", plan_node)
    graph.set_entry_point("plan")
    graph.add_edge("plan", END)
    result = graph.compile().invoke({"goal": goal})
    return {"ok": True, "action": "supervisor_plan", "target": goal,
            "summary": "Created LangGraph supervisor plan",
            "proof": f"steps={len(result.get('steps', []))}", "plan": result.get("steps", [])}
