def total_cents(amounts):
    """Sum a list of decimal amount strings like '12.30' and return integer cents."""
    total = 0
    for a in amounts:
        total += int(float(a) * 100)  # bug: float truncation, e.g. '0.29' -> 28
    return total
