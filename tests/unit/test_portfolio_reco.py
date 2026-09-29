"""
Unit tests for Portfolio Recommendation pipeline tools.

Covers the three critical behaviours:
  1. Portfolio Impact Analyzer — sector matching, exposure threshold, alternatives
  2. Product Universe Filter   — strips non-universe funds, adds ⚠️ flag when needed
  3. Template structure        — correct nodes, edges, agent configs
"""
import pytest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Shared fixtures — minimal catalogue and investor data
# ---------------------------------------------------------------------------

CATALOGUE = {
    "MF001": {
        "fund_id": "MF001", "fund_name": "ICICI Bluechip Fund",
        "sub_category": "Large Cap", "risk_grade": "Very High",
        "expense_ratio_pct": "0.89", "aum_cr": "69755",
        "primary_sectors": ["Financial Services", "IT"],
    },
    "MF002": {
        "fund_id": "MF002", "fund_name": "Mirae Large Cap Fund",
        "sub_category": "Large Cap", "risk_grade": "Very High",
        "expense_ratio_pct": "0.54", "aum_cr": "38200",
        "primary_sectors": ["Financial Services", "IT"],
    },
    "MF022": {
        "fund_id": "MF022", "fund_name": "HDFC Corporate Bond Fund",
        "sub_category": "Corporate Bond", "risk_grade": "Moderate",
        "expense_ratio_pct": "0.31", "aum_cr": "31611",
        "primary_sectors": ["AAA Corporate Bonds"],
    },
    "MF025": {
        "fund_id": "MF025", "fund_name": "SBI Magnum Gilt Fund",
        "sub_category": "Gilt", "risk_grade": "Moderate",
        "expense_ratio_pct": "0.43", "aum_cr": "9400",
        "primary_sectors": ["Government Securities"],
    },
    "MF027": {
        "fund_id": "MF027", "fund_name": "Aditya Birla Liquid Fund",
        "sub_category": "Liquid", "risk_grade": "Low to Moderate",
        "expense_ratio_pct": "0.21", "aum_cr": "36500",
        "primary_sectors": ["Treasury Bills"],
        },
}

# One moderate investor holding MF001 (IT+FinSvc) heavily, plus a bond fund
INVESTORS = {
    "INV001": {
        "investor_id": "INV001", "name": "Test Investor",
        "risk_profile": "Moderate", "age": 42,
        "life_stage": "Mid-career",
        "portfolio_value_inr": 1_000_000,
        "holdings": [
            {"fund_id": "MF001", "fund_name": "ICICI Bluechip Fund",
             "current_value": 700_000, "weight_pct": 70.0},
            {"fund_id": "MF022", "fund_name": "HDFC Corporate Bond Fund",
             "current_value": 300_000, "weight_pct": 30.0},
        ],
    },
}


# ---------------------------------------------------------------------------
# Portfolio Impact Analyzer
# ---------------------------------------------------------------------------

class TestPortfolioImpactAnalyzer:

    def _state(self, affected_sectors: str, sentiment: str = "negative") -> dict:
        event = "Test market event"
        return {
            "messages": [],
            "current_output": (
                f"EVENT_SUMMARY: {event}\n"
                f"ALL_AFFECTED_SECTORS: {affected_sectors}\n"
                f"SENTIMENT: {'NEGATIVE' if sentiment == 'negative' else 'POSITIVE'}"
            ),
        }

    @pytest.mark.asyncio
    async def test_impacted_investor_appears_in_output(self):
        """Investor with >5% weighted exposure to affected sectors should be flagged."""
        from app.tools.portfolio_impact_analyzer import run
        with patch("app.tools.portfolio_impact_analyzer._load_catalogue", return_value=CATALOGUE), \
             patch("app.tools.portfolio_impact_analyzer._load_investors", return_value=INVESTORS):
            result = await run(self._state("IT, Financial Services"))

        assert "INV001" in result["portfolio_impact"]
        assert "Test Investor" in result["portfolio_impact"]

    @pytest.mark.asyncio
    async def test_investor_below_threshold_excluded(self):
        """Investor with <5% weighted exposure should not appear in the impact report."""
        from app.tools.portfolio_impact_analyzer import run
        # Sectors that don't match any of INV001's fund sectors
        with patch("app.tools.portfolio_impact_analyzer._load_catalogue", return_value=CATALOGUE), \
             patch("app.tools.portfolio_impact_analyzer._load_investors", return_value=INVESTORS):
            result = await run(self._state("Infrastructure, Metals"))

        assert "INV001" not in result["portfolio_impact"]
        assert "No investors are materially affected" in result["portfolio_impact"]

    @pytest.mark.asyncio
    async def test_alternatives_not_already_held(self):
        """Recommended alternatives must not include funds already held by the investor."""
        from app.tools.portfolio_impact_analyzer import run
        with patch("app.tools.portfolio_impact_analyzer._load_catalogue", return_value=CATALOGUE), \
             patch("app.tools.portfolio_impact_analyzer._load_investors", return_value=INVESTORS):
            result = await run(self._state("IT, Financial Services"))

        impact = result["portfolio_impact"]
        # MF001 and MF022 are already held — should not appear as alternatives
        lines = [l for l in impact.splitlines() if l.strip().startswith("+")]
        for line in lines:
            assert "MF001" not in line
            assert "MF022" not in line

    @pytest.mark.asyncio
    async def test_missing_sectors_returns_gracefully(self):
        """If agent output has no ALL_AFFECTED_SECTORS, tool returns without crashing."""
        from app.tools.portfolio_impact_analyzer import run
        state = {"messages": [], "current_output": "No structured output here."}
        with patch("app.tools.portfolio_impact_analyzer._load_catalogue", return_value=CATALOGUE), \
             patch("app.tools.portfolio_impact_analyzer._load_investors", return_value=INVESTORS):
            result = await run(state)

        assert "portfolio_impact" in result
        assert "Could not parse" in result["portfolio_impact"]

    @pytest.mark.asyncio
    async def test_output_written_to_state_and_messages(self):
        """run() must write portfolio_impact to state and append to messages."""
        from app.tools.portfolio_impact_analyzer import run
        with patch("app.tools.portfolio_impact_analyzer._load_catalogue", return_value=CATALOGUE), \
             patch("app.tools.portfolio_impact_analyzer._load_investors", return_value=INVESTORS):
            result = await run(self._state("IT"))

        assert "portfolio_impact" in result
        assert len(result["messages"]) == 1
        assert result["messages"][0] == result["portfolio_impact"]


# ---------------------------------------------------------------------------
# Product Universe Filter
# ---------------------------------------------------------------------------

class TestProductUniverseFilter:

    def _impact_with_valid_fund(self) -> str:
        return (
            "Portfolio Impact Analysis\n"
            "============================================================\n"
            "\n"
            "▶ Test Investor (INV001)  |  Moderate  |  Age 42\n"
            "  Affected exposure : 30.0%\n"
            "  Recommended alternatives (product universe only) :\n"
            "    + [MF001] ICICI Bluechip Fund  [Large Cap]  |  Expense: 0.89%\n"
            "    + [MF002] Mirae Large Cap Fund  [Large Cap]  |  Expense: 0.54%\n"
            "\n"
        )

    def _impact_with_invalid_fund(self) -> str:
        return (
            "Portfolio Impact Analysis\n"
            "============================================================\n"
            "\n"
            "▶ Test Investor (INV001)  |  Moderate  |  Age 42\n"
            "  Affected exposure : 30.0%\n"
            "  Recommended alternatives (product universe only) :\n"
            "    + [MF001] ICICI Bluechip Fund  [Large Cap]  |  Expense: 0.89%\n"
            "    + [MFXXX] Hallucinated Fund  [Unknown]  |  Expense: 0.99%\n"
            "\n"
        )

    def _impact_with_no_universe_marker(self) -> str:
        return (
            "Portfolio Impact Analysis\n"
            "============================================================\n"
            "\n"
            "▶ Test Investor (INV001)  |  Moderate  |  Age 42\n"
            "  Affected exposure : 30.0%\n"
            "  Recommended alternatives : NONE_IN_UNIVERSE\n"
            "\n"
        )

    @pytest.mark.asyncio
    async def test_valid_funds_pass_through(self):
        """Funds in the catalogue should not be stripped."""
        from app.tools.product_universe_filter import run
        valid_ids = {"MF001", "MF002", "MF022", "MF025"}
        with patch("app.tools.product_universe_filter._load_valid_fund_ids", return_value=valid_ids):
            result = await run({"messages": [], "portfolio_impact": self._impact_with_valid_fund()})

        assert "MF001" in result["validated_impact"]
        assert "MF002" in result["validated_impact"]
        assert "all recommendations validated ✓" in result["validated_impact"]

    @pytest.mark.asyncio
    async def test_non_universe_fund_is_stripped(self):
        """A fund ID not in the catalogue must be removed from the output."""
        from app.tools.product_universe_filter import run
        valid_ids = {"MF001", "MF002"}  # MFXXX is not valid
        with patch("app.tools.product_universe_filter._load_valid_fund_ids", return_value=valid_ids):
            result = await run({"messages": [], "portfolio_impact": self._impact_with_invalid_fund()})

        assert "MFXXX" not in result["validated_impact"]
        assert "MF001" in result["validated_impact"]

    @pytest.mark.asyncio
    async def test_no_valid_alternatives_adds_warning_flag(self):
        """When NONE_IN_UNIVERSE marker is present, ⚠️ flag must appear in output."""
        from app.tools.product_universe_filter import run
        valid_ids = {"MF001", "MF002"}
        with patch("app.tools.product_universe_filter._load_valid_fund_ids", return_value=valid_ids):
            result = await run({"messages": [], "portfolio_impact": self._impact_with_no_universe_marker()})

        assert "⚠️" in result["validated_impact"]
        assert "RM" in result["validated_impact"]

    @pytest.mark.asyncio
    async def test_empty_portfolio_impact_handled(self):
        """Missing portfolio_impact in state should not crash the filter."""
        from app.tools.product_universe_filter import run
        result = await run({"messages": []})
        assert "validated_impact" in result
        assert "skipping" in result["validated_impact"].lower()

    @pytest.mark.asyncio
    async def test_output_appended_to_messages(self):
        """Filter must append its output to messages and set current_output."""
        from app.tools.product_universe_filter import run
        valid_ids = {"MF001", "MF002"}
        with patch("app.tools.product_universe_filter._load_valid_fund_ids", return_value=valid_ids):
            result = await run({"messages": ["prior"], "portfolio_impact": self._impact_with_valid_fund()})

        assert len(result["messages"]) == 2
        assert result["current_output"] == result["validated_impact"]


# ---------------------------------------------------------------------------
# Template structure
# ---------------------------------------------------------------------------

class TestPortfolioRecoTemplate:

    def setup_method(self):
        from app.templates.portfolio_reco import PORTFOLIO_RECO_TEMPLATE
        self.tmpl = PORTFOLIO_RECO_TEMPLATE

    def test_template_id(self):
        assert self.tmpl["id"] == "portfolio-recommendation"

    def test_trigger_type_is_message(self):
        assert self.tmpl["trigger_type"] == "message"

    def test_has_two_agent_configs(self):
        """Market Signal Watcher + RM Recommendation Writer."""
        configs = self.tmpl["agent_configs"]
        assert len(configs) == 2
        node_ids = {c["node_id"] for c in configs}
        assert "market-signal-agent" in node_ids
        assert "rm-recommendation-agent" in node_ids

    def test_agent_tools_are_registered(self):
        """Tools declared in agent_configs must exist in TOOL_REGISTRY."""
        from app.tools import TOOL_REGISTRY
        for cfg in self.tmpl["agent_configs"]:
            for tool_name in cfg["tools"]:
                assert tool_name in TOOL_REGISTRY, f"{tool_name} not in TOOL_REGISTRY"

    def test_pipeline_tool_nodes_registered(self):
        """tool-type nodes must reference tools registered in PIPELINE_TOOLS."""
        from app.tools import PIPELINE_TOOLS
        nodes = self.tmpl["graph_definition"]["nodes"]
        tool_nodes = [n for n in nodes if n["type"] == "tool"]
        assert len(tool_nodes) == 2
        for n in tool_nodes:
            assert n["tool_name"] in PIPELINE_TOOLS, f"{n['tool_name']} not in PIPELINE_TOOLS"

    def test_graph_edges_form_linear_chain(self):
        """Edges must connect all 6 nodes in a single chain without branches."""
        edges = self.tmpl["graph_definition"]["edges"]
        nodes = self.tmpl["graph_definition"]["nodes"]
        assert len(edges) == len(nodes) - 1

    def test_trigger_type(self):
        """Template is manually triggered."""
        assert self.tmpl["trigger_type"] == "message"
