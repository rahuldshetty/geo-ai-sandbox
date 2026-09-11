import json
import tempfile
import unittest
from pathlib import Path

from geoai.server.notebook import (
    cell_to_nb,
    nb_to_cell,
    new_cell,
    read_nb,
    write_nb,
)


class NotebookSerializationTests(unittest.TestCase):
    def test_prompt_serializes_as_markdown_and_round_trips_as_prompt(self):
        cell = new_cell("prompt", "Map the Nepal flood extent.")
        cell["execution_count"] = 2
        cell["status"] = "done"

        nb_cell = cell_to_nb(cell)

        self.assertEqual(nb_cell["cell_type"], "markdown")
        self.assertEqual(nb_cell["metadata"]["geoai"]["kind"], "prompt")
        restored = nb_to_cell(nb_cell)
        self.assertEqual(restored["kind"], "prompt")
        self.assertEqual(restored["source"], cell["source"])
        self.assertEqual(restored["execution_count"], 2)
        self.assertEqual(restored["status"], "done")

    def test_legacy_code_prompt_still_loads(self):
        legacy = {
            "cell_type": "code",
            "id": "legacy-prompt",
            "metadata": {"geoai": {"kind": "prompt"}},
            "execution_count": 1,
            "outputs": [],
            "source": ["Legacy request"],
        }

        restored = nb_to_cell(legacy)

        self.assertEqual(restored["kind"], "prompt")
        self.assertEqual(restored["execution_count"], 1)

    def test_tool_cell_preserves_provenance_and_output(self):
        cell = new_cell(
            "tool",
            "raster_info(**{'path': 'data/flood.tif'})",
            metadata={
                "geoai": {
                    "kind": "tool",
                    "tool_name": "raster_info",
                    "tool_call_id": "call-1",
                    "generated": True,
                }
            },
        )
        cell["execution_count"] = 1
        cell["status"] = "done"
        cell["outputs"] = [
            {
                "output_type": "stream",
                "name": "stdout",
                "text": '{"count": 3}',
                "ename": None,
                "evalue": None,
                "traceback": None,
            }
        ]

        restored = nb_to_cell(cell_to_nb(cell))

        self.assertEqual(restored["kind"], "tool")
        self.assertEqual(restored["metadata"]["geoai"]["tool_call_id"], "call-1")
        self.assertEqual(restored["outputs"][0]["text"], '{"count": 3}')

    def test_deferred_tool_preserves_waiting_status(self):
        cell = new_cell(
            "tool",
            "request_user_input(**{})",
            metadata={"geoai": {"kind": "tool", "tool_call_id": "call-1"}},
        )
        cell["status"] = "waiting_for_input"

        restored = nb_to_cell(cell_to_nb(cell))

        self.assertEqual(restored["kind"], "tool")
        self.assertEqual(restored["status"], "waiting_for_input")

    def test_notebook_keeps_prompt_tool_and_response_order(self):
        prompt = new_cell("prompt", "Compare before and after imagery.")
        tool = new_cell(
            "tool",
            "describe_map(**{})",
            metadata={"geoai": {"kind": "tool", "generated": True}},
        )
        response = new_cell(
            "markdown",
            "The comparison is ready.",
            metadata={"geoai": {"kind": "response", "generated": True}},
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notebook.ipynb"
            write_nb(path, [prompt, tool, response])
            raw = json.loads(path.read_text(encoding="utf-8"))
            restored = read_nb(path)

        self.assertEqual(
            [cell["cell_type"] for cell in raw["cells"]],
            ["markdown", "code", "markdown"],
        )
        self.assertEqual(
            [cell["kind"] for cell in restored],
            ["prompt", "tool", "markdown"],
        )

    def test_pending_interaction_survives_notebook_round_trip(self):
        prompt = new_cell("prompt", "Load the best flood imagery.")
        interaction = {
            "id": "interaction-1",
            "tool_call_id": "call-1",
            "title": "Choose imagery",
            "prompt": "Several scenes are available.",
            "fields": [],
        }
        prompt["status"] = "waiting_for_input"
        prompt["interaction"] = interaction
        prompt["metadata"]["geoai"] = {"interaction": interaction}

        restored = nb_to_cell(cell_to_nb(prompt))

        self.assertEqual(restored["status"], "waiting_for_input")
        self.assertEqual(restored["interaction"]["id"], "interaction-1")

    def test_submitted_interaction_history_survives_prompt_round_trip(self):
        prompt = new_cell("prompt", "Load the best flood imagery.")
        submitted = {
            "id": "interaction-1",
            "tool_call_id": "call-1",
            "title": "Choose imagery",
            "prompt": "Several scenes are available.",
            "fields": [],
            "answers": {"scene": "post"},
            "submitted": True,
        }
        prompt["status"] = "done"
        prompt["interaction_history"] = [submitted]

        restored = nb_to_cell(cell_to_nb(prompt))

        self.assertEqual(restored["status"], "done")
        self.assertEqual(restored["interaction_history"], [submitted])

    def test_persistent_interaction_cell_keeps_form_and_answers(self):
        interaction = {
            "id": "interaction-1",
            "tool_call_id": "call-1",
            "title": "Choose imagery",
            "prompt": "Select a scene.",
            "fields": [],
        }
        cell = new_cell(
            "interaction",
            "### Input provided\n- **Scene:** Post-event",
            metadata={"geoai": {"parent_cell_id": "prompt-1"}},
        )
        cell["status"] = "done"
        cell["interaction"] = interaction
        cell["answers"] = {"scene": "post"}

        restored = nb_to_cell(cell_to_nb(cell))

        self.assertEqual(restored["kind"], "interaction")
        self.assertEqual(restored["status"], "done")
        self.assertEqual(restored["interaction"]["id"], "interaction-1")
        self.assertEqual(restored["answers"], {"scene": "post"})


if __name__ == "__main__":
    unittest.main()
