import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from community_gap import GapAdjudicationService, VerificationQueue


class QueueTests(unittest.TestCase):
    def test_enqueue_dedupes_open_tasks(self) -> None:
        queue = VerificationQueue()
        queue.enqueue("coverage_review", "c-1", "维修")
        queue.enqueue("coverage_review", "c-1", "维修")
        self.assertEqual(1, queue.pending_count())

    def test_interrupted_tasks_are_restored_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "queue.json")
            first = VerificationQueue(path)
            first.enqueue("coverage_review", "c-1", "维修")
            first.enqueue("coverage_review", "c-1", "托老")
            taken = first.next()
            self.assertEqual("in_progress", taken["state"])
            # 模拟验收中断后重启：核查中的任务退回待办
            reopened = VerificationQueue(path)
            self.assertEqual(2, reopened.pending_count())
            self.assertEqual(0, len(reopened.tasks("in_progress")))

    def test_completed_tasks_stay_done_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "queue.json")
            first = VerificationQueue(path)
            first.enqueue("coverage_review", "c-1", "维修")
            task = first.next()
            first.complete(task["task_id"])
            reopened = VerificationQueue(path)
            self.assertEqual(0, reopened.pending_count())
            self.assertEqual(1, len(reopened.tasks("done")))

    def test_service_enqueues_review_tasks_into_persistent_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "queue.json")
            svc = GapAdjudicationService(queue_path=path)
            svc.publish_boundary("c-1", 1, ["z-1"])
            svc.aggregate_need("c-1", "夜间购药", zone="z-1", time_window="night", support_total=25)
            self.assertTrue(svc.queue.has_open("coverage_review:c-1:夜间购药"))
            # 中断恢复后核查项仍在
            restored = VerificationQueue(path)
            self.assertEqual(1, restored.pending_count())


if __name__ == "__main__":
    unittest.main()
