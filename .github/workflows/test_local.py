#!/usr/bin/env python3
"""
test_local.py - Offline self-test. No network calls, no alerts sent.

Verifies the parts that are easy to get wrong:
  * JSON and SQLite history backends behave identically
  * duplicates are suppressed across runs
  * the search window resumes from the last run and covers gaps
  * environment variables override config.yaml
  * Slack and email payloads build without error

Run:  python test_local.py
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  PASS  " + label)
    else:
        FAIL += 1
        print("  FAIL  " + label + (" - " + detail if detail else ""))


def fake_opp(nid, title="Test Opportunity"):
    return {
        "noticeId": nid,
        "title": title,
        "fullParentPathName": "DEPT OF DEFENSE.DEPT OF THE NAVY.NAVSEA",
        "naicsCode": "541511",
        "postedDate": "2026-09-20",
        "reponseDeadLine": "2026-10-15T17:00:00-05:00",
        "uiLink": "https://sam.gov/opp/" + nid + "/view",
        "solicitationNumber": "N0002426R" + nid,
        "setAside": "Total Small Business Set-Aside",
    }


def base_cfg(tmp, backend):
    return {
        "sam": {"api_key": "TESTKEY", "lookback_days": 7, "page_size": 100, "active_only": True},
        "filters": {"keywords": ["cyber"], "naics_codes": ["541511"], "set_asides": [],
                    "state": "", "procurement_types": []},
        "slack": {"enabled": False, "webhook_url": "", "max_per_message": 10},
        "email": {"enabled": False},
        "storage": {"backend": backend,
                    "json_path": os.path.join(tmp, "history.json"),
                    "db_path": os.path.join(tmp, "history.db"),
                    "retention_days": 365},
        "logging": {"level": "ERROR", "log_file": ""},
        "schedule": {},
    }


print("\n[1] History backends")
from history import get_history

for backend in ("json", "sqlite"):
    tmp = tempfile.mkdtemp()
    try:
        cfg = base_cfg(tmp, backend)
        h = get_history(cfg)

        check(backend + ": unseen id is new", h.is_new("A1"))
        h.mark_seen([fake_opp("A1")], "keyword:cyber")
        check(backend + ": id recorded", not h.is_new("A1"))

        batch = [fake_opp("A1"), fake_opp("B2"), fake_opp("C3")]
        new = h.filter_new(batch)
        got = [o["noticeId"] for o in new]
        check(backend + ": filter_new returns only unseen", got == ["B2", "C3"], "got " + str(got))

        check(backend + ": no last_run initially", h.get_last_run() is None)
        h.set_last_run(date(2026, 9, 20))
        check(backend + ": last_run persists", h.get_last_run() == date(2026, 9, 20))

        h2 = get_history(cfg)
        check(backend + ": survives reload",
              not h2.is_new("A1") and h2.get_last_run() == date(2026, 9, 20))
        check(backend + ": stats count correct", h2.stats()["total_seen"] == 1,
              "got " + str(h2.stats()["total_seen"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

tmp = tempfile.mkdtemp()
try:
    cfg = base_cfg(tmp, "json")
    h = get_history(cfg)
    h.mark_seen([fake_opp("Z9")], "naics:541511")
    with open(cfg["storage"]["json_path"]) as f:
        raw = json.load(f)
    check("json: file has expected shape", "opportunities" in raw and "Z9" in raw["opportunities"])
    check("json: record keeps link", raw["opportunities"]["Z9"]["ui_link"].endswith("/Z9/view"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

tmp = tempfile.mkdtemp()
try:
    cfg = base_cfg(tmp, "json")
    with open(cfg["storage"]["json_path"], "w") as f:
        f.write("{ this is not valid json")
    h = get_history(cfg)
    check("json: recovers from corrupt file", h.stats()["total_seen"] == 0)
    check("json: corrupt file backed up", os.path.exists(cfg["storage"]["json_path"] + ".corrupt"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)


print("\n[2] Search window logic")
from monitor import _get_search_window

tmp = tempfile.mkdtemp()
try:
    cfg = base_cfg(tmp, "json")
    h = get_history(cfg)

    frm, to = _get_search_window(cfg, h)
    check("first run uses lookback_days", (to - frm).days == 7, "got " + str((to - frm).days))

    h.set_last_run(date.today() - timedelta(days=1))
    frm, to = _get_search_window(cfg, h)
    check("normal run looks back ~2 days", (to - frm).days == 2, "got " + str((to - frm).days))

    h.set_last_run(date.today() - timedelta(days=9))
    frm, to = _get_search_window(cfg, h)
    check("gap covered after missed runs", (to - frm).days == 10, "got " + str((to - frm).days))

    h.set_last_run(date.today() - timedelta(days=900))
    frm, to = _get_search_window(cfg, h)
    check("stale history clamps to API limit", (to - frm).days <= 364, "got " + str((to - frm).days))
finally:
    shutil.rmtree(tmp, ignore_errors=True)


print("\n[3] Environment variable overrides")
from config_loader import load_config

env = {
    "SAM_API_KEY": "from-secret-store",
    "SAM_KEYWORDS": "radar, autonomy ,  propulsion ",
    "SAM_NAICS_CODES": "336414,541715",
    "SLACK_ENABLED": "true",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T000/B000/XXX",
    "EMAIL_SMTP_PORT": "2525",
}
with mock.patch.dict(os.environ, env, clear=False):
    cfg = load_config("config.yaml")
    check("api key comes from env", cfg["sam"]["api_key"] == "from-secret-store")
    check("keywords parsed and trimmed",
          cfg["filters"]["keywords"] == ["radar", "autonomy", "propulsion"],
          "got " + str(cfg["filters"]["keywords"]))
    check("naics parsed", cfg["filters"]["naics_codes"] == ["336414", "541715"])
    check("bool coerced", cfg["slack"]["enabled"] is True)
    check("int coerced", cfg["email"]["smtp_port"] == 2525)

with mock.patch.dict(os.environ, {}, clear=True):
    try:
        load_config("config.yaml")
        check("missing api key exits", False, "no SystemExit raised")
    except SystemExit:
        check("missing api key exits", True)


print("\n[4] Full cycle against a mocked API")
import monitor as monitor_mod

tmp = tempfile.mkdtemp()
try:
    cfg = base_cfg(tmp, "json")

    day1 = {"keyword:cyber": [fake_opp("N1", "Cyber Range Support")],
            "naics:541511": [fake_opp("N1", "Cyber Range Support"),
                             fake_opp("N2", "Software Sustainment")]}

    with mock.patch.object(monitor_mod, "run_all_searches", return_value=(day1, [])):
        s1 = monitor_mod.run_monitor(cfg, dry_run=False)
    check("run 1 finds 2 new", s1["total_new"] == 2, "got " + str(s1["total_new"]))
    check("run 1 dedups across searches", s1["total_unique"] == 2, "got " + str(s1["total_unique"]))
    check("run 1 counts raw fetches", s1["total_fetched"] == 3, "got " + str(s1["total_fetched"]))

    with mock.patch.object(monitor_mod, "run_all_searches", return_value=(day1, [])):
        s2 = monitor_mod.run_monitor(cfg, dry_run=False)
    check("run 2 suppresses duplicates", s2["total_new"] == 0, "got " + str(s2["total_new"]))

    day3 = {"keyword:cyber": [fake_opp("N1"), fake_opp("N3", "Zero Trust Architecture")]}
    with mock.patch.object(monitor_mod, "run_all_searches", return_value=(day3, [])):
        s3 = monitor_mod.run_monitor(cfg, dry_run=False)
    check("run 3 finds only the new one", s3["total_new"] == 1, "got " + str(s3["total_new"]))

    day4 = {"keyword:cyber": [fake_opp("N4", "Should Not Be Saved")]}
    with mock.patch.object(monitor_mod, "run_all_searches", return_value=(day4, [])):
        s4 = monitor_mod.run_monitor(cfg, dry_run=True)
    check("dry run reports the match", s4["total_new"] == 1)
    h = get_history(cfg)
    check("dry run saved nothing", h.is_new("N4"))

    with mock.patch.object(monitor_mod, "run_all_searches", return_value=({}, ["naics:541511"])):
        s5 = monitor_mod.run_monitor(cfg, dry_run=True)
    check("failed searches reported", s5["failed_searches"] == ["naics:541511"])
finally:
    shutil.rmtree(tmp, ignore_errors=True)


print("\n[5] Notification payloads")
from notifier import _build_slack_blocks, _build_email_html, _build_email_text

opps = [fake_opp("P1", "Enterprise Cyber Support Services"), fake_opp("P2", "Radar Test Support")]

blocks = _build_slack_blocks(opps, "keyword:cyber")
check("slack payload builds", isinstance(blocks, list) and len(blocks) > 3)
blob = json.dumps(blocks)
check("slack includes links", "sam.gov/opp/P1/view" in blob)
check("slack includes titles", "Enterprise Cyber Support Services" in blob)
check("slack under 50 block limit", len(blocks) <= 50, "got " + str(len(blocks)))

html = _build_email_html(opps, "keyword:cyber")
check("email html builds", "<html" in html.lower() and "P1/view" in html)
check("email html shows deadline", "2026-10-15" in html)

text = _build_email_text(opps, "keyword:cyber")
check("email text builds", "Radar Test Support" in text and "sam.gov/opp/P2/view" in text)

sparse = [{"noticeId": "S1", "title": "Minimal Notice"}]
try:
    _build_slack_blocks(sparse, "keyword:x")
    _build_email_html(sparse, "keyword:x")
    _build_email_text(sparse, "keyword:x")
    check("handles missing optional fields", True)
except Exception as exc:
    check("handles missing optional fields", False, str(exc))


print("\n" + "=" * 52)
print("  " + str(PASS) + " passed, " + str(FAIL) + " failed")
print("=" * 52 + "\n")
sys.exit(1 if FAIL else 0)
