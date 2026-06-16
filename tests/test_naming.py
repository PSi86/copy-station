from pathlib import Path

from copystation.naming import next_transfer_dir, sanitize_name


def test_sanitize_name():
    assert sanitize_name("DJI O4") == "DJI_O4"
    assert sanitize_name(" my/cam:1 ") == "my_cam_1"
    assert sanitize_name("") == "unknown"
    assert sanitize_name(None) == "unknown"
    assert sanitize_name("...") == "unknown"


def test_first_transfer_starts_at_one(tmp_path: Path):
    dest = next_transfer_dir(tmp_path, "DJI_O4")
    assert dest.name == "transfer_0001_DJI_O4"
    assert dest.parent == tmp_path


def test_counter_increments_and_persists(tmp_path: Path):
    first = next_transfer_dir(tmp_path, "Cam")
    first.mkdir()
    second = next_transfer_dir(tmp_path, "Cam")
    assert first.name == "transfer_0001_Cam"
    assert second.name == "transfer_0002_Cam"
    # The counter lives on the SD card and survives "reboots".
    counter = (tmp_path / ".copystation" / "counter").read_text().strip()
    assert counter == "2"


def test_respects_existing_folders_without_counter(tmp_path: Path):
    # Simulates a card with old folders but no counter file.
    (tmp_path / "transfer_0005_Old").mkdir()
    dest = next_transfer_dir(tmp_path, "New")
    assert dest.name == "transfer_0006_New"


def test_never_overwrites_existing(tmp_path: Path):
    (tmp_path / "transfer_0001_Cam").mkdir()
    dest = next_transfer_dir(tmp_path, "Cam")
    assert not dest.exists()
    assert dest.name == "transfer_0002_Cam"
