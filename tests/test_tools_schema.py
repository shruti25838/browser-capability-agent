"""Regression test for FINISH_TOOL's schema requiring `outputs`.

`outputs` was previously optional in FINISH_TOOL's input_schema, so a
discovery run could self-report nothing there even after actually extracting
a value -- silently weakening ArtifactWriter's VALUE_EXTRACTED generalization
net, which (before the output_value backstop in agent/discovery_agent.py /
agent/artifact_writer.py) keyed off that declaration alone.
"""

from __future__ import annotations

from agent.tools import FINISH_TOOL


def test_finish_tool_requires_outputs_declaration():
    assert "outputs" in FINISH_TOOL["input_schema"]["required"]
