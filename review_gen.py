"""Weekly brief generator for Axis Performance.

The brief is a letter from the coach to the athlete. It must tell them something
they could not see for themselves: the pattern behind the week, what connects to
what, and the one thing that will move the needle next.

It reads like a person who paid attention, not a report. Three to four short
paragraphs, warm and direct, second person throughout, no headings or bullets in
the athlete-facing text. Numbers appear inside sentences that interpret them, not
as a list.
"""

from datetime import datetime, timedelta

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAYFULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def week_label(now=None):
    now = now or datetime.now()
    monday = now - timedelta(days=now.weekday())
    return "Week of " + monday.strftime("%d %b %Y")


def _num(v):
    try:
        s = "".join(ch for ch in str(v) if ch.isdigit() or ch in ".-")
        return float(s) if s else None
    except Exception:
        return None


def _day_dates(n=7):
    today = datetime.now().date()
    return [today - timedelta(days=6 - i) for i in range(n)]


def _phrase_metric(r):
    """'Nordic · Left Max Force' -> 'Nordic left max force', acronyms preserved."""
    test = (r.get("testShort") or r.get("test") or "").strip()
    metric = (r.get("metricShort") or r.get("metric") or r.get("n") or "").strip()
    m = metric.replace("L ", "left ").replace("R ", "right ")
    ACR = {"RFD", "ROM", "CMJ", "RSI", "IMTP", "LSI", "PB", "COD", "BM", "SJ"}
    m = " ".join(w if w.upper() in ACR else w.lower() for w in m.split())
    return (f"{test} {m}").strip() if test else m


# ─────────────────────────────────────────────────────────────────────────
#  ANALYSIS: build a structured read of the week before any prose is written
# ─────────────────────────────────────────────────────────────────────────
def analyse(a, criteria=None):
    r = {}
    w = a.get("wellness") or []
    dates = _day_dates(len(w))
    logged = [(dates[i], w[i]) for i in range(len(w)) if w[i] is not None]
    r["wl_days"] = len(logged)
    r["wellness"] = logged
    if logged:
        vals = [v for _, v in logged]
        r["wl_avg"] = sum(vals) / len(vals)
        r["wl_best"] = max(logged, key=lambda x: x[1])
        r["wl_worst"] = min(logged, key=lambda x: x[1])
        r["wl_spread"] = r["wl_best"][1] - r["wl_worst"][1]
        if len(vals) >= 4:
            half = max(1, len(vals) // 2)
            r["wl_drift"] = (sum(vals[-half:]) / half) - (sum(vals[:half]) / half)
        else:
            r["wl_drift"] = 0

    # training days from session records
    sess = a.get("sessDates") or []
    r["sess"] = sess
    r["trained_dates"] = {s.get("d") for s in sess if s.get("s") == "completed"}
    r["started_dates"] = {s.get("d") for s in sess if s.get("s") == "in_progress"}
    st, sd = a.get("sessTotal") or 0, a.get("sessDone") or 0
    r["sess_total"], r["sess_done"] = st, sd
    r["sess_pct"] = round(100 * sd / st) if st else None
    r["unfinished"] = sum(1 for s in sess if s.get("s") == "in_progress")
    r["never_started"] = sum(1 for s in sess if s.get("s") == "not_started")

    # correlate wellness dips with training
    if logged and r["wl_spread"] >= 2:
        worst_d = r["wl_worst"][0]
        r["worst_trained"] = worst_d.isoformat() in r["trained_dates"]
        r["worst_after_training"] = (worst_d - timedelta(days=1)).isoformat() in r["trained_dates"]

    # testing movers
    t = a.get("testing") or []
    JUNK = ("rep count", "reps", "calibration", "standing weight", "bodyweight", "torque", "impulse")
    perf = [x for x in t if not any(j in (x.get("metric") or x.get("n") or "").lower() for j in JUNK)]
    ups, downs = [], []
    for row in perf[:12]:
        prev = row.get("prev")
        if not prev or prev == "first test":
            continue
        v, p = _num(row.get("v")), _num(prev)
        if v is None or not p:
            continue
        ch = (v - p) / p * 100
        better = ch < 0 if row.get("dir") == "low" else ch > 0
        if abs(ch) < 4 or abs(ch) > 60:  # >60% swing is almost always a testing artefact, not real change
            continue
        (ups if better else downs).append((row, round(abs(ch))))
    ups.sort(key=lambda x: -x[1])
    downs.sort(key=lambda x: -x[1])
    r["ups"], r["downs"] = ups, downs
    r["pbs"] = [row for row in perf if row.get("pb")]

    # symmetry
    L = next((x for x in t if "left max force" in (x.get("metric") or x.get("n") or "").lower()), None)
    R = next((x for x in t if "right max force" in (x.get("metric") or x.get("n") or "").lower()), None)
    if L and R:
        l, rr = _num(L.get("v")), _num(R.get("v"))
        if l and rr and max(l, rr):
            r["lsi"] = round(min(l, rr) / max(l, rr) * 100)
            r["lsi_side"] = "left" if l < rr else "right"

    # rehab
    p = a.get("prpLive")
    if p and not p.get("noPlan"):
        r["rehab"] = p
        crit = (criteria or {}).get("items") or []
        r["crit"] = crit
        r["crit_met"] = [c for c in crit if c.get("met") is True]
        r["crit_unmet"] = [c for c in crit if c.get("met") is not True]

    # block focuses
    adp = a.get("adp")
    if adp and adp.get("blocks"):
        cur = min(adp.get("current", 0), len(adp["blocks"]) - 1)
        b = adp["blocks"][cur]
        r["block"] = b
        r["focuses"] = b.get("focuses") or []

    r["comp"] = a.get("comp", 0)
    r["tier"] = a.get("tier", "thin")
    r["bsg"] = a.get("bsg") if a.get("bsg") and a.get("bsg") != "Not set" else None
    return r


# ─────────────────────────────────────────────────────────────────────────
#  PROSE: turn the analysis into paragraphs
# ─────────────────────────────────────────────────────────────────────────
def _para_open(a, R, name):
    """Paragraph 1: the week, led by the strongest true thing to say."""
    o = []
    goal = f" You have {R['bsg']} in your sights, and this is the week measured against it." if R.get("bsg") else ""

    if R["sess_total"]:
        pct = R["sess_pct"]
        if pct >= 90:
            o.append(f"{name}, that is {R['sess_done']} of {R['sess_total']} sessions done, the whole "
                     f"program.{goal} Consistency at that level is the reason your numbers move at all, "
                     "so before anything else, that is the win of the week.")
        elif pct >= 60:
            o.append(f"{name}, you got {R['sess_done']} of {R['sess_total']} sessions in this week.{goal} "
                     "The work you do is good. What is costing you is the sessions that quietly fall off "
                     "when the week gets full.")
        else:
            o.append(f"{name}, {R['sess_done']} of {R['sess_total']} sessions this week.{goal} I want to "
                     "start there, because everything else in this brief sits on top of how much of your "
                     "program actually gets done.")
        if R["unfinished"] and pct is not None and pct < 90:
            n = R["unfinished"]
            o.append(f"{n} of them got started and left unfinished. That is a different problem to skipping "
                     "a session, and it usually means the session was longer than the day allowed. Tell me "
                     "and I will build one that fits the day you actually have.")
    else:
        o.append(f"{name}, nothing came through as a completed session this week.{goal} If you trained and it "
                 "did not get ticked off, it did not count toward anything here, which is a shame because the "
                 "work is only worth what we can see.")

    # a win, tied in
    if R["ups"]:
        row, ch = R["ups"][0]
        o.append(f"On the numbers, your {_phrase_metric(row)} came in at {row['v']}, {ch}% up on last time. "
                 "That is the training showing up where it counts.")
    elif R["pbs"]:
        row = R["pbs"][0]
        o.append(f"You also put a personal best on the board in your {_phrase_metric(row)} at {row['v']}. "
                 "Worth noting, worth protecting.")
    return " ".join(o)


def _para_honest(a, R, name):
    """Paragraph 2: the read only a coach watching closely would give."""
    o = []

    # wellness pattern
    if R["wl_days"] == 0:
        o.append("I have no wellness from you this week, so I am flying blind on how the training actually "
                 "landed. That is the one piece I cannot get anywhere else.")
    elif R["wl_days"] < 3:
        wd = ", ".join(DAYFULL[d.weekday()] for d, _ in R["wellness"])
        o.append(f"You logged wellness {R['wl_days']} time{'s' if R['wl_days'] > 1 else ''} this week ({wd}), "
                 "which is not enough to see a pattern. The days you skip tend to be the ones worth knowing "
                 "about.")
    else:
        worst = R["wl_worst"]
        wd = DAYFULL[worst[0].weekday()]
        if R["wl_spread"] >= 2:
            if R.get("worst_trained") or R.get("worst_after_training"):
                o.append(f"Your flattest day was {wd} at {worst[1]}, and you trained "
                         f"{'that day' if R.get('worst_trained') else 'the day before'}. That is fatigue "
                         "doing exactly what it should. The thing I watch is how fast you come back, and "
                         f"{'you did, by the weekend' if R['wl_best'][0] > worst[0] else 'this week you had not fully by Sunday'}.")
            else:
                o.append(f"Your flattest day was {wd} at {worst[1]}, and there was no session behind it. When "
                         "the dip is not training, it is almost always sleep, school or life stress. Name the "
                         "real one and we can plan around it instead of guessing.")
        else:
            o.append(f"Wellness sat steady in a tight band this week ({worst[1]} to {R['wl_best'][1]}, average "
                     f"{R['wl_avg']:.1f}), which tells me load and recovery are about matched right now. Hold "
                     "that.")
        if R.get("wl_drift", 0) <= -1 and R["wl_days"] >= 4:
            o.append("The trend drifted down across the week rather than bouncing around, and that is the "
                     "kind of thing that turns into a niggle if we ignore it. If it carries into next week "
                     "we pull volume early.")

    # a real drop, with the fatigue caveat
    if R["downs"]:
        row, ch = R["downs"][0]
        caveat = ""
        if R["wl_days"] and R.get("wl_avg", 10) < 6:
            caveat = " Your wellness was down when this was measured, so I am reading it as fatigue rather than lost fitness until we retest."
        o.append(f"One to keep honest about: your {_phrase_metric(row)} came in at {row['v']}, {ch}% off your "
                 f"previous.{caveat} One test is never a trend, and on a tired day fatigue and decline look "
                 "identical. We retest before we react.")

    # symmetry, the most actionable single number
    if R.get("lsi") is not None:
        if R["lsi"] < 90:
            o.append(f"Your {R['lsi_side']} side is running {100 - R['lsi']}% behind the other ({R['lsi']}% "
                     "symmetry). Under 90% is where injury risk lives, so the single leg work is not optional "
                     "filler, it is the most important thing on your program until that gap closes.")
        elif R["lsi"] >= 95:
            o.append(f"Left to right you are at {R['lsi']}%, which is exactly where we want it. That balance is "
                     "quietly protecting you every session.")

    # rehab, in plain terms
    if R.get("rehab"):
        p = R["rehab"]
        line = (f"On the rehab side, you are in {p.get('phaseName')}, week {p.get('weeksInPhase')} of this "
                f"phase and week {p.get('weeksTotal')} overall.")
        if R.get("crit"):
            if R["crit_unmet"]:
                names = ", ".join(c.get("t", "") for c in R["crit_unmet"][:2])
                nextgoal = ("being signed off and back to full training" if p.get("phaseName") == "Return to Play"
                            else "the next phase")
                line += (f" What stands between you and {nextgoal} is {names}. That is the whole list. Not the "
                         "calendar, not luck, those.")
            else:
                line += (" Every criterion for this phase is met, so your physio can look at progressing you "
                         "this week. You earned that rather than waited for it.")
        else:
            line += (" Your exit criteria for this phase are not set yet. Ask your physio what you need to hit "
                     "to move up, because with rehab a clear target is half the battle.")
        if (p.get("weeksInPhase") or 0) >= 12 and p.get("phaseName") != "Return to Play":
            line += (f" You have been in this phase {p.get('weeksInPhase')} weeks, which is longer than either of "
                     "us wants. That is a fair thing to raise, and worth raising this week.")
        o.append(line)

    return " ".join(o)


def _para_close(a, R, name):
    """Paragraph 3: focuses, the gym week, and the one specific ask."""
    o = []
    if R.get("focuses"):
        met = [x for x in R["focuses"] if x.get("met") is True]
        missed = [x for x in R["focuses"] if x.get("met") is False]
        if missed:
            o.append(f"On your block focuses you have {len(met)} of {len(R['focuses'])} done, with "
                     f"{missed[0].get('t')} still open. Pick that one this week and the block closes clean.")
        elif len(met) == len(R["focuses"]) and R["focuses"]:
            o.append("You have ticked off every block focus, which is a block you can genuinely point back to.")

    o.append(_the_one_thing(a, R, name))
    return " ".join(o)


def _the_one_thing(a, R, name):
    """A single, specific instruction chosen by biggest available upside."""
    lead = "So here is the one thing for this week: "
    if R["wl_days"] == 0:
        return (lead + "log your wellness every morning. Ninety seconds. Everything above becomes sharper the "
                "moment I can see how your week actually felt.")
    if R["sess_total"] and R["sess_pct"] is not None and R["sess_pct"] < 60:
        return (lead + "pick the one session you keep losing, put it in your phone as a fixed appointment, and "
                "defend it like a game. Consistency beats clever programming every single time.")
    if R.get("lsi") is not None and R["lsi"] < 90:
        return (lead + "do not skip the single leg work. That imbalance is the clearest injury risk you have "
                "and it is completely trainable. Own it.")
    if R.get("rehab") and R.get("crit_unmet"):
        return (lead + f"chip at the open criteria, starting with {R['crit_unmet'][0].get('t')}. That is the "
                "only thing standing between you and the next step.")
    if R["wl_days"] < 5:
        return (lead + f"get wellness to a full seven days. You logged {R['wl_days']}. The blanks are usually "
                "the days that would have told us the most.")
    if R["downs"]:
        row = R["downs"][0][0]
        return (lead + f"keep the quality high and let us retest that {_phrase_metric(row)} fresh before we "
                "read anything into the dip.")
    return (lead + "hold the consistency and bring intent to every rep. Same sessions, sharper focus, is what "
            "turns a solid block into a testing PB.")


# ─────────────────────────────────────────────────────────────────────────
def generate_review(a, facility=None, criteria=None):
    name = (a.get("name") or "Athlete").split(" ")[0]
    R = analyse(a, criteria)

    head = ""
    if facility and facility.get("focus"):
        head = f"Week {facility.get('week')}: {facility['focus']}\n\n"

    coach = None
    for g in (a.get("groups") or []):
        label = g.split(" Athletes")[0].strip()
        if label and " " in label and len(label) <= 14 and label.lower() not in ("rod's", "rods"):
            coach = label
    sign = f"\n\n{coach}\nAxis Performance" if coach else "\n\nYour coach at Axis Performance"

    # nothing to work with
    if R["wl_days"] == 0 and not R["sess_total"] and not R["ups"] and not R["downs"] and not R.get("rehab"):
        p1 = (f"{name}, I have got nothing from you this week. No wellness, no sessions ticked off, no testing. "
              "I am not writing that to have a go, I am writing it because this brief only works when I can see "
              "your week, and right now I cannot see any of it.")
        p2 = ("The athletes getting the most from this are not the ones training hardest. They are the ones we "
              "can actually see, because that is what lets us tell you the things you cannot spot yourself, the "
              "pattern behind a flat day, the number quietly climbing, the imbalance worth chasing.")
        p3 = ("So this week, log your wellness each morning and tick your sessions off as you finish them. Next "
              "Monday I want this to be a real letter about your training, not a note about missing data.")
        return head + "\n\n".join([p1, p2, p3]) + sign

    p1 = _para_open(a, R, name)
    p2 = _para_honest(a, R, name)
    p3 = _para_close(a, R, name)
    body = "\n\n".join(p for p in (p1, p2, p3) if p and p.strip())
    return head + body + sign
