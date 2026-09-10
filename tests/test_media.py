"""Which files at the root of a volume make up its recordings."""

from copystation.media import is_video_file, recording_files


def _touch(root, *names):
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).write_bytes(b"x")


def test_recordings_are_the_videos_and_their_namesakes(tmp_path):
    _touch(tmp_path, "VID0001.mp4", "VID0001.osd", "VID0001.srt",
           "VID0002.mp4", "VID0002.osd")
    assert recording_files(tmp_path) == [
        "VID0001.mp4", "VID0001.osd", "VID0001.srt", "VID0002.mp4", "VID0002.osd",
    ]


def test_everything_not_named_after_a_video_stays(tmp_path):
    # A Walksnail air unit's own files, as a tester's dump shows them, next to a
    # recording and an orphaned sidecar whose video is gone.
    _touch(tmp_path, "VID0001.mp4", "VID0001.osd", "Avatar_version.txt", "VID0009.osd")
    _touch(tmp_path / "System Volume Information", "WPSettings.dat")
    assert recording_files(tmp_path) == ["VID0001.mp4", "VID0001.osd"]


def test_a_sidecar_needs_the_full_name_up_to_the_dot(tmp_path):
    # VID0001 must not pick up VID00010's files.
    _touch(tmp_path, "VID0001.mp4", "VID00010.osd")
    assert recording_files(tmp_path) == ["VID0001.mp4"]


def test_names_match_regardless_of_case(tmp_path):
    _touch(tmp_path, "VID0001.MP4", "vid0001.osd")
    assert recording_files(tmp_path) == ["VID0001.MP4", "vid0001.osd"]


def test_hidden_files_and_subfolders_are_not_recordings(tmp_path):
    # "._VID0001.mp4" is macOS metadata, not a second video.
    _touch(tmp_path, "VID0001.mp4", "._VID0001.mp4", ".VID0001.osd")
    _touch(tmp_path / "sub", "VID0002.mp4")
    (tmp_path / "VID0003.mp4").mkdir()  # a folder named like a video
    assert recording_files(tmp_path) == ["VID0001.mp4"]


def test_no_video_means_no_recording(tmp_path):
    _touch(tmp_path, "Avatar_version.txt", "VID0001.osd")
    assert recording_files(tmp_path) == []


def test_video_definition_is_shared_with_transcode():
    from copystation import transcode

    assert transcode.is_video_file is is_video_file
    assert is_video_file("VID0001.mp4") and not is_video_file("VID0001.osd")
