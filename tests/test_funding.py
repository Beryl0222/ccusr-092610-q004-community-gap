import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from community_gap import DomainError, GapAdjudicationService


class FundingTests(unittest.TestCase):
    def test_reservation_within_balance(self) -> None:
        svc = GapAdjudicationService()
        svc.open_funding_round("round-1", 100, label="2026年第一批")
        svc.reserve_funds("round-1", 60, site_id="s-1")
        self.assertEqual(40, svc.funding_rounds["round-1"].remaining)
        with self.assertRaises(DomainError) as ctx:
            svc.reserve_funds("round-1", 41)
        self.assertEqual("INSUFFICIENT_FUNDS", ctx.exception.code)
        self.assertEqual(40, ctx.exception.details["remaining"])
        svc.reserve_funds("round-1", 40)
        self.assertEqual(0, svc.funding_rounds["round-1"].remaining)

    def test_concurrent_reservations_never_exceed_balance(self) -> None:
        svc = GapAdjudicationService()
        svc.open_funding_round("round-1", 100)
        barrier = threading.Barrier(8)
        succeeded: list[int] = []
        failures: list[DomainError] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            barrier.wait()
            try:
                svc.reserve_funds("round-1", 30)
                with lock:
                    succeeded.append(index)
            except DomainError as exc:
                with lock:
                    failures.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(3, len(succeeded))
        self.assertEqual(5, len(failures))
        self.assertTrue(all(exc.code == "INSUFFICIENT_FUNDS" for exc in failures))
        self.assertEqual(90, svc.funding_rounds["round-1"].reserved)
        self.assertLessEqual(svc.funding_rounds["round-1"].reserved, 100)


if __name__ == "__main__":
    unittest.main()
