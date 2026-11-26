"""Self-update (updater.py)."""
from patchmgr import updater

BODY = """Duplicates in Sources. Version 0.8.1.

Download the PatchManager-Setup .exe below.
- new thing

Backups beside Sources and Patches. Version 0.8.0.
- older
"""
REL = {"tag_name": "v0.8.1-build11", "body": BODY, "assets": [
    {"name": "PatchManager-Setup-0.8.1.exe", "size": 123, "browser_download_url": "https://x/PatchManager-Setup-0.8.1.exe"}]}


def test_newer_version_found_and_notes_cut_to_the_newest():
    u = updater.find_update(REL, "PatchManager-Setup-", "0.8.0")
    assert u.version == "0.8.1" and u.size == 123 and u.url.endswith(".exe")
    assert "new thing" in u.notes and "older" not in u.notes


def test_same_or_older_is_not_an_update():
    assert updater.find_update(REL, "PatchManager-Setup-", "0.8.1") is None
    assert updater.find_update(REL, "PatchManager-Setup-", "0.10.0") is None
    assert updater.find_update(REL, "DelugeSampleManager-Setup-", "0.1.0") is None
    assert updater.parse_version("0.10.0") > updater.parse_version("0.9.9")


def test_when_to_check():
    assert updater.due("start", 0, 1000, at_start=True)
    assert not updater.due("start", 0, 10 ** 9, at_start=False)
    assert updater.due("start_and_every", 0, updater.EVERY + 1, at_start=False)
    assert not updater.due("start_and_every", 100, 200, at_start=False)
    assert not updater.due("manual", 0, 10 ** 9, at_start=True)
