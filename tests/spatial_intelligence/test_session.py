"""Session integration: opening a workspace, running cells, and recording them.

These drive the real :class:`AppState` with its run worker against a scripted
:class:`FunctionModel`, so they cover the whole chain a prompt takes: the tool
registry, the runtime binding, streaming steps, progress jobs, trace files,
notebook provenance, and interaction resume.
"""

import asyncio
import json
import os
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ToolCallPart

from spatial_intelligence.session.app_state import AppState
from spatial_intelligence.session.notebook_session import ordered_notebook_cells

from .support import (
    Fails,
    FakeResponse,
    TurnScript,
    answers_with,
    scripted,
    stream_model,
    wait_for,
)

TERMINAL_STATUSES = frozenset({"done", "error", "stopped"})


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._previous_home = os.environ.get("GEOAI_HOME")
        os.environ["GEOAI_HOME"] = self._tmp.name

    def tearDown(self):
        if self._previous_home is None:
            os.environ.pop("GEOAI_HOME", None)
        else:
            os.environ["GEOAI_HOME"] = self._previous_home
        self._tmp.cleanup()

    def start(self, model) -> AppState:
        state = AppState(
            settings={
                "model": model,
                "theme": "light",
                "dangerous_mode": False,
                "max_retries": 2,
                "record_agent_steps": True,
            }
        )
        self.addCleanup(self._stop, state)
        state.open_workspace("session-test")
        self.workspace_root = Path(self._tmp.name) / "workspaces" / "session-test"
        return state

    @staticmethod
    def _stop(state: AppState) -> None:
        state.runs.wait_idle(10)

    def wait_for_status(self, state: AppState, cell_id: str, statuses) -> dict:
        cell = state.notebook.find(cell_id)
        ok = wait_for(lambda: cell["status"] in statuses)
        self.assertTrue(ok, f"cell stayed {cell['status']!r}")
        return cell

    def drainevents(self, subscriber: queue.Queue, name: str) -> list[dict]:
        collected: list[dict] = []
        while True:
            try:
                item = subscriber.get_nowait()
            except queue.Empty:
                return collected
            if item["event"] == name:
                collected.append(item["data"])


class PromptRunTests(SessionTestCase):
    def test_a_prompt_run_writes_files_records_provenance_and_streams_events(self):
        model = scripted(
            ToolCallPart("search_tools", {"queries": ["write a file"]}),
            ToolCallPart("write_file", {"path": "results/note.txt", "content": "hello"}),
            final="Wrote the note.",
        )
        state = self.start(model)
        subscriber = state.subscribe()
        cell_id = state.add_cell("prompt", "write a note")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertEqual(cell["status"], "done")
        self.assertEqual(cell["outputs"][0]["text"], "Wrote the note.")
        self.assertEqual(
            (self.workspace_root / "results" / "note.txt").read_text(encoding="utf-8"),
            "hello",
        )
        step_types = [step["type"] for step in cell["trace"]]
        self.assertIn("tool_call", step_types)
        self.assertIn("tool_result", step_types)
        self.assertIn("usage", step_types)
        # Three model requests: discover the deferred tool, call it, answer.
        self.assertEqual(cell["usage"]["requests"], 3)
        # The file write notified the UI through the runtime's events hook.
        self.assertTrue(self.drainevents(subscriber, "files"))

        saved = json.loads(
            (self.workspace_root / "notebook.ipynb").read_text(encoding="utf-8")
        )
        kinds = [
            ((nb_cell.get("metadata") or {}).get("geoai") or {}).get("kind")
            or nb_cell["cell_type"]
            for nb_cell in saved["cells"]
        ]
        self.assertEqual(kinds, ["prompt", "tool", "tool", "response"])

    def test_trace_steps_stream_while_the_cell_is_still_running(self):
        """Live steps must reach the browser mid-run, not only at the end.

        Regression: the front end drops a streamed step when it has no trace
        container yet, so this pins the other half of the contract — that the
        server publishes steps as they happen rather than once the run ends.
        """
        from pydantic_ai.models.function import DeltaToolCall

        async def stream(messages, info):
            from pydantic_ai.messages import ToolReturnPart

            answered = any(
                isinstance(part, ToolReturnPart)
                for message in messages
                for part in getattr(message, "parts", [])
            )
            if answered:
                # Hold the run open after the tool result, so the assertions
                # below run while the cell is still working.
                await asyncio.sleep(1.0)
                yield "finished"
            else:
                yield {0: DeltaToolCall(name="list_files", json_args="{}")}

        state = self.start(stream_model(stream))
        subscriber = state.subscribe()
        cell_id = state.add_cell("prompt", "list the files")["cells"][-1]["id"]

        streamed: list[dict] = []

        def collect_step() -> bool:
            streamed.extend(self.drainevents(subscriber, "trace"))
            return bool(streamed)

        state.run_cell(cell_id)
        self.assertTrue(
            wait_for(collect_step), "no trace step was published before the run ended"
        )
        self.assertEqual(state.notebook.find(cell_id)["status"], "running")
        self.assertIn("tool_call", [item["step"]["type"] for item in streamed])
        self.assertTrue(all(item["id"] == cell_id for item in streamed))

        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)
        self.assertEqual(cell["status"], "done")

    def test_a_prompt_run_persists_its_trace_file(self):
        # list_files is a core tool, so it needs no discovery step.
        model = scripted(ToolCallPart("list_files", {}), final="Listed the files.")
        state = self.start(model)
        cell_id = state.add_cell("prompt", "what is here?")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        trace = (self.workspace_root / "traces" / f"{cell_id}.jsonl").read_text(
            encoding="utf-8"
        )
        records = [json.loads(line) for line in trace.splitlines()]
        self.assertEqual(records[0]["prompt"], "what is here?")
        self.assertEqual(records[-1]["status"], "done")
        self.assertEqual(cell["run_id"], records[0]["run_id"])

    def test_the_data_listing_is_prepended_to_the_prompt(self):
        prompts: list[str] = []

        async def stream(messages, info):
            prompts.append(
                "\n".join(
                    part.content
                    for message in messages
                    for part in getattr(message, "parts", [])
                    if getattr(part, "part_kind", None) == "user-prompt"
                )
            )
            yield "ok"

        state = self.start(stream_model(stream))
        workspace = self.workspace_root
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "data" / "scene.tif").write_bytes(b"x")
        cell_id = state.add_cell("prompt", "describe the scene")["cells"][-1]["id"]

        state.run_cell(cell_id)
        self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertIn("data/scene.tif", prompts[0])
        self.assertIn("describe the scene", prompts[0])


class ProgressJobTests(SessionTestCase):
    def test_a_download_reports_progress_jobs_through_the_session(self):
        model = scripted(
            ToolCallPart("search_tools", {"queries": ["download a url"]}),
            ToolCallPart(
                "download",
                {"url": "https://example.com/scene.tif", "filename": "scene.tif"},
            ),
            final="Downloaded it.",
        )
        state = self.start(model)
        subscriber = state.subscribe()
        cell_id = state.add_cell("prompt", "fetch the scene")["cells"][-1]["id"]

        with patch(
            "spatial_intelligence.workspace.files.urllib.request.urlopen",
            return_value=FakeResponse(b"asset"),
        ):
            state.run_cell(cell_id)
            cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertEqual(cell["status"], "done")
        self.assertEqual(
            (self.workspace_root / "data" / "scene.tif").read_bytes(), b"asset"
        )

        jobs = self.drainevents(subscriber, "job")
        final = [job for job in jobs if job["status"] == "done"]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["kind"], "download")
        self.assertEqual(final[0]["artifact"], "data/scene.tif")
        self.assertEqual(final[0]["parent_id"], cell_id)
        self.assertEqual(final[0]["total"], 5)
        # The snapshot endpoint carries the job list the UI restores from.
        snapshot = state.snapshot()
        self.assertEqual(snapshot["jobs"][0]["job_id"], final[0]["job_id"])

    def test_a_rerun_clears_the_previous_progress_jobs(self):
        model = scripted(
            ToolCallPart("search_tools", {"queries": ["download a url"]}),
            ToolCallPart(
                "download",
                {"url": "https://example.com/one.tif", "filename": "one.tif"},
            ),
            final="Done.",
        )
        state = self.start(model)
        cell_id = state.add_cell("prompt", "get it")["cells"][-1]["id"]

        with patch(
            "spatial_intelligence.workspace.files.urllib.request.urlopen",
            return_value=FakeResponse(b"asset"),
        ):
            state.run_cell(cell_id)
            self.wait_for_status(state, cell_id, TERMINAL_STATUSES)
            first = state.snapshot()["jobs"]
            state.run_cell(cell_id)
            self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        second = state.snapshot()["jobs"]
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["job_id"], second[0]["job_id"])


class PythonCellTests(SessionTestCase):
    def test_a_python_cell_runs_and_records_its_full_output(self):
        state = self.start(answers_with("unused"))
        cell_id = state.add_cell("python", "print('a' * 3)\n6 * 7")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertEqual(cell["status"], "done")
        self.assertEqual(cell["execution_count"], 1)
        self.assertIn("aaa", cell["outputs"][0]["text"])
        self.assertIn("42", cell["outputs"][0]["text"])

    def test_a_python_cell_surfaces_a_crash_as_an_error_output(self):
        state = self.start(answers_with("unused"))
        cell_id = state.add_cell("python", "raise ValueError('nope')")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertEqual(cell["status"], "done")  # a traceback is still output
        self.assertIn("ValueError", cell["outputs"][0]["text"])

    def test_markdown_cells_are_not_runnable(self):
        state = self.start(answers_with("unused"))
        cell_id = state.add_cell("markdown", "# notes")["cells"][-1]["id"]

        with self.assertRaises(ValueError):
            state.run_cell(cell_id)


class InteractionTests(SessionTestCase):
    def interaction_call(self) -> ToolCallPart:
        return ToolCallPart(
            "request_user_input",
            {
                "title": "Pick a scene",
                "prompt": "Which acquisition?",
                "fields": [
                    {
                        "id": "scene",
                        "label": "Scene",
                        "type": "radio",
                        "options": [
                            {"value": "before", "label": "Before"},
                            {"value": "after", "label": "After", "recommended": True},
                        ],
                    }
                ],
            },
        )

    def test_a_prompt_pauses_for_input_and_resumes_with_the_answers(self):
        state = self.start(scripted(self.interaction_call(), final="Chosen."))
        cell_id = state.add_cell("prompt", "compare imagery")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, {"waiting_for_input"})

        interaction = cell["interaction"]
        self.assertEqual(interaction["title"], "Pick a scene")
        self.assertEqual(cell["status"], "waiting_for_input")
        self.assertEqual(cell["outputs"], [])

        state.respond_interaction(cell_id, interaction["id"], {"scene": "after"})
        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertEqual(cell["status"], "done")
        self.assertEqual(cell["outputs"][0]["text"], "Chosen.")
        self.assertIsNone(cell["interaction"])
        history = cell["interaction_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["answers"], {"scene": "after"})
        self.assertTrue(history[0]["submitted"])

        saved = json.loads(
            (self.workspace_root / "notebook.ipynb").read_text(encoding="utf-8")
        )
        sources = [
            "".join(nb_cell.get("source") or []) for nb_cell in saved["cells"]
        ]
        self.assertTrue(any("Input provided" in source for source in sources))
        self.assertTrue(any("**Scene:** After" in source for source in sources))

    def test_answers_are_validated_against_the_pending_form(self):
        state = self.start(scripted(self.interaction_call(), final="Chosen."))
        cell_id = state.add_cell("prompt", "compare")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, {"waiting_for_input"})
        interaction_id = cell["interaction"]["id"]

        with self.assertRaises(ValueError):
            state.respond_interaction(cell_id, interaction_id, {"scene": "sideways"})
        with self.assertRaises(ValueError):
            state.respond_interaction(cell_id, interaction_id, {"unknown": "x"})
        with self.assertRaises(ValueError):
            state.respond_interaction(cell_id, "stale-id", {"scene": "after"})

        state.respond_interaction(cell_id, interaction_id, {"scene": "after"})
        self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

    def test_cancelling_an_interaction_stops_the_cell(self):
        state = self.start(scripted(self.interaction_call(), final="Chosen."))
        cell_id = state.add_cell("prompt", "compare")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, {"waiting_for_input"})

        state.cancel_interaction(cell_id, cell["interaction"]["id"])

        self.assertEqual(cell["status"], "stopped")
        self.assertIsNone(cell["interaction"])
        self.assertIn("Stopped while waiting", cell["outputs"][0]["text"])


class TransientFailureTests(SessionTestCase):
    """A provider hiccup must not lose work, and must not be replayed blindly.

    The run loop restarts an attempt only when no tool that mutated the
    workspace or the map has completed; these tests pin both sides of that
    decision, plus the plan rollback that makes a replay safe.
    """

    def test_a_failure_after_a_read_only_tool_is_retried(self):
        script = TurnScript(
            ToolCallPart("list_files", {}),
            Fails(ModelHTTPError(503, "test-model")),
            final="Recovered after the failure.",
        )
        state = self.start(script.model)
        cell_id = state.add_cell("prompt", "list the files")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertEqual(cell["status"], "done")
        self.assertEqual(cell["outputs"][0]["text"], "Recovered after the failure.")
        self.assertEqual(script.requests, 3)  # tool call, failure, retry
        notes = [step.get("content", "") for step in cell["trace"] if step["type"] == "text"]
        self.assertTrue(any("Retrying" in note for note in notes), notes)

    def test_a_failure_after_a_successful_write_is_not_replayed(self):
        script = TurnScript(
            ToolCallPart("search_tools", {"queries": ["write a file"]}),
            ToolCallPart("write_file", {"path": "results/kept.txt", "content": "kept"}),
            Fails(ModelHTTPError(503, "test-model")),
            final="this attempt must never run",
        )
        state = self.start(script.model)
        cell_id = state.add_cell("prompt", "write a note")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertEqual(cell["status"], "error")
        self.assertIn("automatic replay was skipped", cell["outputs"][0]["evalue"])
        self.assertEqual(script.requests, 3)  # no replay, so no fourth request
        self.assertEqual(
            (self.workspace_root / "results" / "kept.txt").read_text(encoding="utf-8"),
            "kept",
        )

    def test_a_failure_after_a_write_that_failed_is_retried(self):
        script = TurnScript(
            ToolCallPart("search_tools", {"queries": ["write a file"]}),
            # traces/ is not a writable target, so the tool fails and nothing changed.
            ToolCallPart("write_file", {"path": "traces/blocked.txt", "content": "nope"}),
            Fails(ModelHTTPError(503, "test-model")),
            final="Recovered.",
        )
        state = self.start(script.model)
        cell_id = state.add_cell("prompt", "write a note")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertEqual(cell["status"], "done")
        self.assertEqual(cell["outputs"][0]["text"], "Recovered.")
        self.assertEqual(script.requests, 4)  # tool call, failed write, failure, retry
        self.assertFalse((self.workspace_root / "traces" / "blocked.txt").exists())

    def test_a_retry_restores_the_plan_from_before_the_failed_attempt(self):
        script = TurnScript(
            ToolCallPart(
                "write_plan",
                {"items": [{"content": "Failed attempt plan", "status": "in_progress"}]},
            ),
            Fails(ModelHTTPError(503, "test-model")),
            final="Recovered.",
        )
        state = self.start(script.model)
        cell_id = state.add_cell("prompt", "plan the work")["cells"][-1]["id"]

        state.run_cell(cell_id)
        cell = self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        self.assertEqual(cell["status"], "done")
        self.assertEqual(script.requests, 3)
        # The failed attempt's plan is rolled back, not inherited by the retry.
        self.assertEqual(asyncio.run(state.services.plan_store.get_items()), [])
        self.assertTrue(
            any(step["type"] == "plan" for step in cell["trace"]),
            [step["type"] for step in cell["trace"]],
        )


class NotebookDocumentTests(SessionTestCase):
    def test_recorded_provenance_is_saved_after_its_parent_cell(self):
        prompt = {"id": "prompt-1"}
        second_prompt = {"id": "prompt-2"}
        tool = {
            "id": "tool-1",
            "metadata": {"geoai": {"generated": True, "parent_cell_id": "prompt-1"}},
        }

        ordered = ordered_notebook_cells([prompt, second_prompt], [tool])

        self.assertEqual(
            [cell["id"] for cell in ordered], ["prompt-1", "tool-1", "prompt-2"]
        )

    def test_provenance_whose_parent_is_gone_is_saved_last(self):
        prompt = {"id": "prompt-1"}
        orphan = {
            "id": "tool-9",
            "metadata": {"geoai": {"generated": True, "parent_cell_id": "deleted"}},
        }

        ordered = ordered_notebook_cells([prompt], [orphan])

        self.assertEqual([cell["id"] for cell in ordered], ["prompt-1", "tool-9"])


class WorkspaceLifecycleTests(SessionTestCase):
    def test_editing_cells_persists_the_notebook(self):
        state = self.start(answers_with("unused"))
        cell_id = state.add_cell("markdown", "# first")["cells"][-1]["id"]

        state.update_cell(cell_id, "# edited")

        saved = json.loads(
            (self.workspace_root / "notebook.ipynb").read_text(encoding="utf-8")
        )
        self.assertEqual("".join(saved["cells"][0]["source"]), "# edited")

        state.delete_cell(cell_id)
        saved = json.loads(
            (self.workspace_root / "notebook.ipynb").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["cells"], [])

    def test_reopening_a_workspace_restores_its_cells_and_traces(self):
        state = self.start(scripted(ToolCallPart("list_files", {}), final="Listed."))
        cell_id = state.add_cell("prompt", "list")["cells"][-1]["id"]
        state.run_cell(cell_id)
        self.wait_for_status(state, cell_id, TERMINAL_STATUSES)

        state.close_workspace()
        self.assertIsNone(state.workspaces.name)
        self.assertEqual(state.notebook.cells(), [])

        state.open_workspace("session-test")

        restored = state.notebook.find(cell_id)
        self.assertEqual(restored["status"], "done")
        self.assertEqual(restored["outputs"][0]["text"], "Listed.")
        self.assertIn("tool_call", [step["type"] for step in restored["trace"]])

    def test_run_all_runs_every_runnable_cell_in_order(self):
        state = self.start(answers_with("ok"))
        first = state.add_cell("python", "1 + 1")["cells"][-1]["id"]
        middle = state.add_cell("markdown", "text")["cells"][-1]["id"]
        last = state.add_cell("python", "2 + 2")["cells"][-1]["id"]

        state.run_all()

        self.wait_for_status(state, first, TERMINAL_STATUSES)
        self.wait_for_status(state, last, TERMINAL_STATUSES)
        self.assertEqual(state.notebook.find(middle)["status"], "idle")
        self.assertIn("2", state.notebook.find(first)["outputs"][0]["text"])
        self.assertIn("4", state.notebook.find(last)["outputs"][0]["text"])

    def test_importing_an_upload_lands_in_data(self):
        state = self.start(answers_with("unused"))

        result = state.import_local([("points.geojson", b"{}")])

        self.assertEqual(result["imported"], ["data/points.geojson"])
        self.assertEqual(
            (self.workspace_root / "data" / "points.geojson").read_bytes(), b"{}"
        )


if __name__ == "__main__":
    unittest.main()
