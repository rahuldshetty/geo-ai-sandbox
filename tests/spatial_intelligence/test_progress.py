"""Progress reporting: job lifecycle, terminal guards, and reporter ownership."""

import unittest

from spatial_intelligence.contracts.progress import (
    NULL_REPORTER,
    JobState,
    Reporter,
)


class RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class BrokenSink:
    def emit(self, event):
        raise RuntimeError("subscriber is gone")


class JobTests(unittest.TestCase):
    def setUp(self):
        self.sink = RecordingSink()
        self.reporter = Reporter(self.sink, parent_id="cell-1")

    def test_job_opens_running_and_reports_progress(self):
        job = self.reporter.job("download", "scene.tif", unit="bytes")

        job.progress(10, total=100)

        self.assertEqual([event.status for event in self.sink.events], [
            JobState.RUNNING,
            JobState.RUNNING,
        ])
        self.assertEqual(self.sink.events[-1].completed, 10)
        self.assertEqual(self.sink.events[-1].total, 100)
        self.assertEqual(self.sink.events[-1].label, "scene.tif")
        self.assertEqual(self.sink.events[-1].unit, "bytes")
        self.assertEqual(self.sink.events[-1].parent_id, "cell-1")

    def test_advance_accumulates(self):
        job = self.reporter.job("raster", "warp", unit="steps")

        job.advance()
        job.advance(2)

        self.assertEqual(self.sink.events[-1].completed, 3)

    def test_done_records_the_artifact(self):
        job = self.reporter.job("download", "scene.tif", unit="bytes", total=100)

        job.done(artifact="data/scene.tif")

        self.assertIs(self.sink.events[-1].status, JobState.DONE)
        self.assertEqual(self.sink.events[-1].artifact, "data/scene.tif")
        self.assertEqual(self.sink.events[-1].completed, 100)

    def test_terminal_state_ignores_late_reports(self):
        job = self.reporter.job("download", "scene.tif")

        job.done()
        job.progress(99)
        job.fail("too late")
        job.cancel()

        self.assertIs(job.state, JobState.DONE)
        self.assertEqual(len(self.sink.events), 2)

    def test_fail_records_the_reason(self):
        job = self.reporter.job("download", "scene.tif")

        job.fail(ValueError("connection reset"))

        self.assertIs(self.sink.events[-1].status, JobState.ERROR)
        self.assertIn("connection reset", self.sink.events[-1].error)

    def test_context_manager_completes_and_propagates_failures(self):
        job = self.reporter.job("download", "scene.tif")

        with self.assertRaises(ValueError):
            with job:
                raise ValueError("boom")

        self.assertIs(self.sink.events[-1].status, JobState.ERROR)
        self.assertIn("boom", self.sink.events[-1].error)

    def test_context_manager_completes_a_clean_block(self):
        with self.reporter.job("raster", "clip") as job:
            job.progress(1, total=1)

        self.assertIs(job.state, JobState.DONE)

    def test_broken_sink_never_breaks_the_job(self):
        reporter = Reporter(BrokenSink())

        job = reporter.job("download", "scene.tif")
        job.progress(5)
        job.done()

        self.assertIs(job.state, JobState.DONE)

    def test_events_are_json_safe(self):
        import json

        job = self.reporter.job("raster", "clip", unit="steps", total=2)
        job.progress(1)

        json.dumps(self.sink.events[-1].as_dict())


class ReporterTests(unittest.TestCase):
    def test_null_reporter_reports_nothing(self):
        job = NULL_REPORTER.job("download", "scene.tif")

        job.done()

        self.assertIs(job.state, JobState.DONE)

    def test_jobs_inherit_the_reporter_owner(self):
        reporter = Reporter(RecordingSink(), parent_id="cell-7")

        self.assertEqual(reporter.job("download", "a").parent_id, "cell-7")

    def test_job_can_override_the_owner(self):
        reporter = Reporter(RecordingSink(), parent_id="cell-7")

        self.assertEqual(
            reporter.job("download", "a", parent_id="cell-9").parent_id, "cell-9"
        )

    def test_rebind_keeps_the_sink_and_changes_the_owner(self):
        sink = RecordingSink()
        reporter = Reporter(sink, parent_id="cell-1")

        rebound = reporter.rebind(parent_id="cell-2")

        self.assertIs(rebound.sink, sink)
        self.assertEqual(rebound.parent_id, "cell-2")
        self.assertEqual(reporter.parent_id, "cell-1")


if __name__ == "__main__":
    unittest.main()
