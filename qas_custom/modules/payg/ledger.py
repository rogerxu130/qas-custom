"""Pure folds and invariants for PAYG Entry deltas; no database writes."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class Balance:
    available: int = 0
    reserved: int = 0
    consumed: int = 0

    def __iter__(self):
        return iter((self.available, self.reserved, self.consumed))


def _field(row, name, default=None):
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def _integer(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise LedgerError(f"{label} must be an integer") from None
    if not number.is_finite() or number != number.to_integral_value():
        raise LedgerError(f"{label} must be an integer")
    return int(number)


def _deltas(row):
    kind = _field(row, "kind")
    deltas = tuple(_integer(_field(row, field), field) for field in
                   ("available_delta", "reserved_delta", "consumed_delta"))
    fixed = {"Issue": (10, 0, 0), "Reserve": (-1, 1, 0),
             "Consume": (0, -1, 1)}
    if kind in fixed and deltas != fixed[kind]:
        raise LedgerError(f"{kind} has invalid deltas")
    if kind == "Return" and deltas not in ((1, -1, 0), (1, 0, -1)):
        raise LedgerError("Return must restore one reserved or consumed session")
    if kind == "Transfer Out" and not (deltas[0] < 0 and deltas[1:] == (0, 0)):
        raise LedgerError("Transfer Out must remove available sessions")
    if kind == "Transfer In" and not (deltas[0] > 0 and deltas[1:] == (0, 0)):
        raise LedgerError("Transfer In must add available sessions")
    if kind == "Correction" and (not _field(row, "reason") or deltas == (0, 0, 0)):
        raise LedgerError("Correction requires a reason and nonzero deltas")
    if kind not in (*fixed, "Return", "Transfer Out", "Transfer In", "Correction"):
        raise LedgerError(f"Unknown PAYG entry kind: {kind}")
    return deltas


def apply_entries(entries):
    """Fold one card's entries, refusing duplicate keys and negative buckets."""
    balance = Balance()
    seen = set()
    for row in entries:
        key = _field(row, "operation_key")
        if not key:
            raise LedgerError("PAYG entry requires an operation key")
        if key in seen:
            raise LedgerError(f"Duplicate PAYG operation key: {key}")
        seen.add(key)
        deltas = _deltas(row)
        balance = Balance(*(old + delta for old, delta in zip(balance, deltas)))
        if min(balance) < 0:
            raise LedgerError("PAYG entry would make a card balance negative")
    return balance


def assert_card_balance(entries, *, available_count, reserved_count, consumed_count):
    folded = apply_entries(entries)
    cached = Balance(*(_integer(value, name) for name, value in
                       (("available_count", available_count),
                        ("reserved_count", reserved_count),
                        ("consumed_count", consumed_count))))
    if folded != cached:
        raise LedgerError("PAYG card cache differs from entry deltas")
    return folded


def assert_transfer_pair(source_entries, target_entries, operation, *, source_card, target_card):
    """Validate one Exchange's equal and opposite entries on distinct cards."""
    if not source_card or not target_card or source_card == target_card:
        raise LedgerError("Transfer source and target cards must be different")
    if not operation:
        raise LedgerError("Transfer requires an operation")
    outs = [row for row in source_entries if _field(row, "kind") == "Transfer Out"
            and _field(row, "operation") == operation]
    ins = [row for row in target_entries if _field(row, "kind") == "Transfer In"
           and _field(row, "operation") == operation]
    if len(outs) != 1 or len(ins) != 1:
        raise LedgerError("Transfer Out and Transfer In must each occur once")
    if _field(outs[0], "card") != source_card:
        raise LedgerError("Transfer Out must belong to the source card")
    if _field(ins[0], "card") != target_card:
        raise LedgerError("Transfer In must belong to the target card")
    out_delta, in_delta = _deltas(outs[0]), _deltas(ins[0])
    if out_delta[0] + in_delta[0] != 0:
        raise LedgerError("Transfer Out and Transfer In quantities must be equal")
    return -out_delta[0]
