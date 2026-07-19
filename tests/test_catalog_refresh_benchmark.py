import unittest

from scripts.benchmark_catalog_refresh import run_benchmark


class CatalogRefreshBenchmarkTests(unittest.TestCase):
    def test_generated_unchanged_catalog_launches_no_child_processes(self) -> None:
        result = run_benchmark(12)

        self.assertTrue(result.passed)
        self.assertEqual(result.child_process_count, 0)
        self.assertEqual(result.iterations, 2)
        self.assertEqual(result.discovered, 0)
        self.assertEqual(result.reprobed, 0)
        self.assertEqual(result.unchanged, 24)
        self.assertEqual(result.db_lock_errors, 0)


if __name__ == "__main__":
    unittest.main()
