"""Pure PAYG ledger fold and conservation contract tests."""
from types import SimpleNamespace
from unittest import TestCase

from qas_custom.modules.payg.ledger import (
    LedgerError, apply_entries, assert_card_balance, assert_transfer_pair,
)


def entry(kind, available, reserved=0, consumed=0, key=None, operation=None, reason=None, card=None):
    return SimpleNamespace(kind=kind, available_delta=available, reserved_delta=reserved,
                           consumed_delta=consumed, operation_key=key or kind,
                           operation=operation, reason=reason, card=card)


class TestPaygLedger(TestCase):
    def test_issue_reserve_consume_and_both_return_paths(self):
        issue = entry("Issue", 10)
        reserve = entry("Reserve", -1, 1)
        consume = entry("Consume", 0, -1, 1)
        self.assertEqual(tuple(apply_entries([issue])), (10, 0, 0))
        self.assertEqual(tuple(apply_entries([issue, reserve])), (9, 1, 0))
        self.assertEqual(tuple(apply_entries([issue, reserve, consume])), (9, 0, 1))
        self.assertEqual(tuple(apply_entries([issue, reserve, entry("Return", 1, -1, key="return-r")])),
                         (10, 0, 0))
        self.assertEqual(tuple(apply_entries([issue, reserve, consume,
                                              entry("Return", 1, 0, -1, key="return-c")])),
                         (10, 0, 0))

    def test_transfer_pair_preserves_each_card_and_late_return(self):
        source = [entry("Issue", 10),
                  *(entry("Reserve", -1, 1, key=f"reserve:{n}") for n in range(3)),
                  *(entry("Consume", 0, -1, 1, key=f"consume:{n}") for n in range(3)),
                  entry("Transfer Out", -7, key="out:ex-1", operation="ex-1", card="SOURCE")]
        target = [entry("Transfer In", 7, key="in:ex-1", operation="ex-1", card="TARGET")]
        self.assertEqual(tuple(apply_entries(source)), (0, 0, 3))
        self.assertEqual(tuple(apply_entries(target)), (7, 0, 0))
        assert_transfer_pair(source, target, "ex-1", source_card="SOURCE", target_card="TARGET")
        self.assertEqual(tuple(apply_entries(source + [entry("Return", 1, 0, -1, key="late")])),
                         (1, 0, 2))

    def test_reject_duplicate_key_bad_delta_and_negative_balance(self):
        with self.assertRaisesRegex(LedgerError, "Duplicate"):
            apply_entries([entry("Issue", 10, key="same"), entry("Issue", 10, key="same")])
        with self.assertRaisesRegex(LedgerError, "Reserve"):
            apply_entries([entry("Issue", 10), entry("Reserve", -2, 2)])
        with self.assertRaisesRegex(LedgerError, "negative"):
            apply_entries([entry("Transfer Out", -1, key="out")])

    def test_correction_requires_reason_and_card_cache_matches_fold(self):
        rows = [entry("Issue", 10), entry("Correction", -1, key="correction", reason="audit")]
        self.assertEqual(tuple(apply_entries(rows)), (9, 0, 0))
        assert_card_balance(rows, available_count=9, reserved_count=0, consumed_count=0)
        with self.assertRaisesRegex(LedgerError, "cache"):
            assert_card_balance(rows, available_count=10, reserved_count=0, consumed_count=0)
        with self.assertRaisesRegex(LedgerError, "reason"):
            apply_entries([entry("Correction", 1)])

    def test_transfer_pair_rejects_mismatch(self):
        source = [entry("Issue", 10), entry("Transfer Out", -7, key="out", operation="ex", card="SOURCE")]
        target = [entry("Transfer In", 6, key="in", operation="ex", card="TARGET")]
        with self.assertRaisesRegex(LedgerError, "equal"):
            assert_transfer_pair(source, target, "ex", source_card="SOURCE", target_card="TARGET")

    def test_transfer_pair_rejects_same_or_wrong_card_and_operation(self):
        source = [entry("Transfer Out", -4, key="out", operation="ex", card="SOURCE")]
        target = [entry("Transfer In", 4, key="in", operation="ex", card="TARGET")]
        with self.assertRaisesRegex(LedgerError, "different"):
            assert_transfer_pair(source, target, "ex", source_card="SOURCE", target_card="SOURCE")
        with self.assertRaisesRegex(LedgerError, "source card"):
            assert_transfer_pair(source, target, "ex", source_card="OTHER", target_card="TARGET")
        with self.assertRaisesRegex(LedgerError, "target card"):
            assert_transfer_pair(source, target, "ex", source_card="SOURCE", target_card="OTHER")
        with self.assertRaisesRegex(LedgerError, "each occur once"):
            assert_transfer_pair(source, target, "other-ex", source_card="SOURCE", target_card="TARGET")
