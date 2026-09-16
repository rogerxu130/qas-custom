"""Currency-exact allocations for the course settlement report (no database writes)."""
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR

CENT = Decimal('0.01')
ZERO = Decimal(0)


def amount(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def allocate(total, weights):
    """Largest-remainder allocation, stable on input order and exact to one cent."""
    total = amount(total)
    weights = [max(ZERO, Decimal(str(w or 0))) for w in weights]
    denominator = sum(weights)
    if not denominator:
        return [ZERO for _ in weights]
    sign = -1 if total < 0 else 1
    units = abs(total) / CENT
    exact = [units * w / denominator for w in weights]
    floors = [int(v.to_integral_value(rounding=ROUND_FLOOR)) for v in exact]
    remainder = int(units) - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return [CENT * v * sign for v in floors]


def invoice_allocations(total, lines, cash, credit, refund=0, returned_by_line=None):
    """Allocate verified settlements, then cap each line at its retained charge.

    A return reducing only unpaid fees does not remove cash. An actual refund and
    the return that caused it are accounted for once via the larger reduction.
    Callers must reject ambiguous return-line/refund attribution before calling.
    """
    total, cash, credit, refund = map(amount, (total, cash, credit, refund))
    if min(total, cash, credit, refund) < 0:
        raise ValueError('Negative settlement components require review')
    weights = [Decimal(str(row.get('net_amount') if row.get('net_amount') is not None else row.get('amount') or 0)) for row in lines]
    if any(w < 0 for w in weights) or (total > 0 and sum(weights) <= 0):
        raise ValueError('Invoice contains unsupported negative or missing charge weights')
    # A refund of unallocated excess does not reduce settled course fees.
    refund = max(ZERO, refund - max(ZERO, cash + credit - total))
    # Excess funds remain unallocated; preserve the payment/credit source ratio.
    cash, credit = allocate(min(total, cash + credit), [cash, credit])
    billed_parts = allocate(total, weights)
    gross_parts = allocate(cash + credit, billed_parts)
    cash_parts = allocate(cash, gross_parts)
    credit_parts = [gross - paid for gross, paid in zip(gross_parts, cash_parts)]
    returned_by_line = returned_by_line or {}
    return_weights = [amount(returned_by_line.get(line['name'])) for line in lines]
    refunds = allocate(refund, return_weights if sum(return_weights) else weights)
    result = []
    for i, line in enumerate(lines):
        gross = cash_parts[i] + credit_parts[i]
        retained = max(ZERO, billed_parts[i] - amount(returned_by_line.get(line['name'])))
        reduction = max(min(gross, refunds[i]), gross - retained)
        result.append({
            **line,
            'billed_amount': float(billed_parts[i]),
            'cash_received': float(cash_parts[i]),
            'credit_used': float(credit_parts[i]),
            'reductions': float(reduction),
            'eligible_revenue': float(gross - reduction),
            'allocation_percent': float(weights[i] / sum(weights) * 100) if sum(weights) else 0,
        })
    return result
