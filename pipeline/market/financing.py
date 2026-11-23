"""Swap financing: divide the weekend (or holiday) multiple out of the API's annualised rates.

What the API gives (docs: Technical documentation > API; Omni > Trading > Swaps):
  * `funding.last` is the most recently applied payment; its annualised rate is the payment's
    interval rate times 360.
  * One payment per trading day at the daily close, 17:00 New York time, Monday to Friday
    (the API's `next_timestamp` reads 22:05 UTC in winter).
  * One payment a week covers the weekend and reads three times a normal day: Friday for indices
    and commodities, Wednesday for FX and precious metals (T+2 settlement). The payment before a
    market holiday "may incorporate the upcoming closed days".

Rule applied here (the multiple is either established or the rate is stored as null):
  1. Payment day = the latest weekday 17:05 New York at or before the snapshot.
  2. Multiple by schedule = 3 on the market's roll weekday, else 1.
  3. If the payment day is a US market holiday, or a holiday falls within the market's look-ahead
     (one business day for Friday-roll markets, two for Wednesday-roll markets), the schedule is
     not trusted.
  4. Markets whose financing tracks a benchmark rate plus a spread (equity indices, precious
     metals) move slowly, so they carry a reference: the last normalised long rate established by
     schedule in the previous 10 days. A scheduled multiple that puts the rate more than 25% from
     the reference is rejected (null). When the schedule is not trusted, the multiple is taken
     from the ratio to the reference if it sits within 10% of a whole number from 1 to 5.
  5. Futures-carry markets (other commodities, VXI, DXI) move too much day to day for a ratio
     test, so they use the schedule only and are null whenever the schedule is not trusted.
"""
from datetime import date, datetime, timedelta

PAY_HOUR, PAY_MINUTE = 17, 5
REF_MAX_AGE_DAYS = 10
CHECK_TOLERANCE = 0.25
RATIO_TOLERANCE = 0.10
ROLL_WEEKDAY = {"fri": 4, "wed": 2}


def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(days=7 * (n - 1))


def _last_weekday(year, month, weekday):
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_market_holidays(year):
    """Full-day NYSE closures. Used only as a guard: near these dates the schedule is not trusted."""
    days = {
        _nth_weekday(year, 1, 0, 3),            # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),            # Washington's Birthday
        _easter(year) - timedelta(days=2),      # Good Friday
        _last_weekday(year, 5, 0),              # Memorial Day
        _observed(date(year, 6, 19)),           # Juneteenth
        _observed(date(year, 7, 4)),            # Independence Day
        _nth_weekday(year, 9, 0, 1),            # Labor Day
        _nth_weekday(year, 11, 3, 4),           # Thanksgiving
        _observed(date(year, 12, 25)),          # Christmas
    }
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        days.add(date(year, 1, 2))
    elif ny.weekday() != 5:                     # a Saturday New Year is not observed
        days.add(ny)
    return days


def is_holiday(d):
    return d in us_market_holidays(d.year)


def ny_offset_hours(utc_dt):
    """UTC offset of New York at a UTC instant: -4 during daylight time, else -5."""
    y = utc_dt.year
    start = datetime.combine(_nth_weekday(y, 3, 6, 2), datetime.min.time()) + timedelta(hours=7)
    end = datetime.combine(_nth_weekday(y, 11, 6, 1), datetime.min.time()) + timedelta(hours=6)
    return -4 if start <= utc_dt < end else -5


def last_payment(snapshot_utc):
    """(payment time in UTC, New York payment date) of the latest weekday close at or before the snapshot."""
    local_day = (snapshot_utc + timedelta(hours=ny_offset_hours(snapshot_utc))).date()
    for back in range(0, 8):
        d = local_day - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        local = datetime(d.year, d.month, d.day, PAY_HOUR, PAY_MINUTE)
        # the offset in force at the payment instant (try both, keep the self-consistent one)
        for off in (-4, -5):
            utc = local - timedelta(hours=off)
            if ny_offset_hours(utc) == off:
                if utc <= snapshot_utc:
                    return utc, d
                break
    raise RuntimeError("no payment day found")


def _next_business_days(d, n):
    out = []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def schedule_trusted(pay_date, roll):
    ahead = 2 if roll == "wed" else 1
    return not (is_holiday(pay_date) or any(is_holiday(x) for x in _next_business_days(pay_date, ahead)))


def normalise(cfg, snapshot_utc, last, ref=None):
    """Normalise one swap's last applied financing.

    cfg  : {"roll": "fri"|"wed", "benchmark": bool} or None when the market is not configured
    last : the API's `funding.last` object or None
    ref  : (normalised long rate, payment date) established earlier, or None
    Returns {"long", "short", "mult", "basis", "pay", "raw_long", "raw_short"}; long and short are
    None when the multiple is not established.
    """
    out = {"long": None, "short": None, "mult": None, "basis": None, "pay": None,
           "raw_long": None, "raw_short": None}
    if not last:
        out["basis"] = "no_payment"
        return out
    raw_long = float(last["annualized_long_rate"])
    raw_short = float(last["annualized_short_rate"])
    out["raw_long"], out["raw_short"] = raw_long, raw_short
    pay_utc, pay_date = last_payment(snapshot_utc)
    out["pay"] = pay_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not cfg or cfg.get("roll") not in ROLL_WEEKDAY:
        out["basis"] = "unconfigured"
        return out
    scheduled = 3 if pay_date.weekday() == ROLL_WEEKDAY[cfg["roll"]] else 1
    trusted = schedule_trusted(pay_date, cfg["roll"])
    ref_value = None
    if cfg.get("benchmark") and ref and ref[0] and ref[0] > 0 and raw_long > 0 \
            and 0 <= (pay_date - ref[1]).days <= REF_MAX_AGE_DAYS:
        ref_value = ref[0]
    mult, basis = None, None
    if trusted:
        if ref_value is not None and abs(raw_long / scheduled / ref_value - 1) > CHECK_TOLERANCE:
            basis = "schedule_contradicts_reference"
        else:
            mult, basis = scheduled, "schedule"
    else:
        if ref_value is not None:
            for k in range(1, 6):
                if abs(raw_long / (k * ref_value) - 1) <= RATIO_TOLERANCE:
                    mult, basis = k, "ratio"
                    break
            if mult is None:
                basis = "holiday_unresolved"
        else:
            basis = "holiday_unresolved"
    out["basis"] = basis
    if mult:
        out["mult"] = mult
        out["long"] = round(raw_long / mult, 6)
        out["short"] = round(raw_short / mult, 6)
    return out


def self_test():
    """Checks against observations in the stored snapshots (2 to 22 Nov 2026)."""
    idx = {"roll": "fri", "benchmark": True}
    met = {"roll": "wed", "benchmark": True}
    com = {"roll": "fri", "benchmark": False}
    T = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M")
    L = lambda a, b: {"annualized_long_rate": a, "annualized_short_rate": b}
    # Monday 2 Nov 02:46 UTC: last payment is Friday 30 Oct, still daylight time (21:05 UTC)
    r = normalise(idx, T("2026-11-02 02:46"), L("0.1482", "0.08820"))
    assert (r["mult"], r["long"], r["pay"]) == (3, 0.0494, "2026-10-30T21:05:00Z"), r
    r = normalise(met, T("2026-11-02 02:46"), L("0.05482", "0.02317"))
    assert (r["mult"], r["long"]) == (1, 0.05482), r
    # Sunday 22 Nov: last payment Friday 20 Nov 22:05 UTC, tripled for indices, single for metals
    r = normalise(idx, T("2026-11-22 09:30"), L("0.1470", "0.08700"), ref=(0.0492, date(2026, 11, 19)))
    assert (r["mult"], r["long"], r["short"], r["pay"]) == (3, 0.049, 0.029, "2026-11-20T22:05:00Z"), r
    r = normalise(met, T("2026-11-22 09:30"), L("0.05826", "0.02555"))
    assert r["mult"] == 1, r
    # a Wednesday payment for metals reads three times
    r = normalise(met, T("2026-11-12 03:00"), L("0.1675", "0.07255"), ref=(0.05436, date(2026, 11, 10)))
    assert (r["mult"], r["long"]) == (3, 0.055833), r
    # the day before Thanksgiving (26 Nov 2026): schedule not trusted
    assert us_market_holidays(2026) >= {date(2026, 11, 26), date(2026, 12, 25), date(2026, 4, 3)}
    r = normalise(com, T("2026-11-26 03:00"), L("-0.30", "-0.30"))
    assert r["long"] is None and r["basis"] == "holiday_unresolved", r
    r = normalise(idx, T("2026-11-26 03:00"), L("0.0984", "0.0584"), ref=(0.0492, date(2026, 11, 24)))
    assert (r["mult"], r["basis"], r["long"]) == (2, "ratio", 0.0492), r
    # a scheduled multiple that contradicts the reference is refused
    r = normalise(idx, T("2026-11-18 03:00"), L("0.1470", "0.0870"), ref=(0.0492, date(2026, 11, 16)))
    assert r["long"] is None and r["basis"] == "schedule_contradicts_reference", r
    return True
