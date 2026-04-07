from __future__ import annotations

from app.graph.nodes import AgentNodes
from app.graph.router import route_from_status
from app.graph.state import AgentState


def build_graph(nodes: AgentNodes):
    try:
        from langgraph.graph import END, START, StateGraph
    except Exception as exc:
        raise RuntimeError("langgraph is required. Install dependencies first.") from exc

    graph = StateGraph(AgentState)
    graph.add_node("dispatch", lambda state: state)
    graph.add_node("intake", nodes.intake)
    graph.add_node("preflight", nodes.preflight)
    graph.add_node("plan", nodes.plan)
    graph.add_node("act", nodes.act)
    graph.add_node("human_confirm", nodes.human_confirm)
    graph.add_node("verify", nodes.verify)
    graph.add_node("diagnose", nodes.diagnose)
    graph.add_node("reflect", nodes.reflect)
    graph.add_node("finish", nodes.finish)

    graph.add_edge(START, "dispatch")

    graph.add_conditional_edges(
        "dispatch",
        route_from_status,
        {
            "intake": "intake",
            "preflight": "preflight",
            "plan": "plan",
            "act": "act",
            "human_confirm": "human_confirm",
            "verify": "verify",
            "diagnose": "diagnose",
            "reflect": "reflect",
            "finish": "finish",
        },
    )

    graph.add_edge("intake", "dispatch")
    graph.add_edge("preflight", "dispatch")
    graph.add_edge("plan", "dispatch")
    graph.add_edge("act", "dispatch")
    graph.add_edge("human_confirm", "dispatch")
    graph.add_edge("verify", "dispatch")
    graph.add_edge("diagnose", "dispatch")
    graph.add_edge("reflect", "dispatch")
    graph.add_edge("finish", END)

    return graph.compile()
