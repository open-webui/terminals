import unittest

from terminals.utils.kubernetes_scheduling import (
    _parse_node_selector,
    _parse_tolerations,
)


class KubernetesSchedulingTests(unittest.TestCase):
    def test_node_selector_accepts_pairs(self) -> None:
        self.assertEqual(
            _parse_node_selector("dedicated=terminals, disk=fast"),
            {"dedicated": "terminals", "disk": "fast"},
        )

    def test_node_selector_accepts_json(self) -> None:
        self.assertEqual(
            _parse_node_selector('{"dedicated":"terminals"}'),
            {"dedicated": "terminals"},
        )

    def test_tolerations_accepts_json_array(self) -> None:
        self.assertEqual(
            _parse_tolerations(
                '[{"key":"dedicated","operator":"Equal","value":"terminals","effect":"NoSchedule"}]'
            ),
            [
                {
                    "key": "dedicated",
                    "operator": "Equal",
                    "value": "terminals",
                    "effect": "NoSchedule",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
