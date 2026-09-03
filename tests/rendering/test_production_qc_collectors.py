"""Contract tests for the pure production QC collector registry."""

from __future__ import annotations

import hashlib

import pytest
from autocut_kernel.rendering.production_qc_collectors import (
    PRODUCTION_QC_COLLECTORS,
    AstatsReducer,
    CollectionObservation,
    CollectorError,
    CompactTimelineReducer,
    DetectorIntervalReducer,
    FramehashReducer,
    MetadataPrintReducer,
    ProgressReducer,
    StreamTopology,
    bind_collector_argv,
    build_stream_argv,
    parse_astats_value,
    parse_compact_record,
    parse_rational_timestamp,
    parse_topology_json,
)
from autocut_kernel.store.models import PRODUCTION_RENDER_QC_REQUIRED_CHECKS


def test_registry_is_the_closed_store_order_with_closed_measurement_schemas() -> None:
    assert tuple(spec.check_id for spec in PRODUCTION_QC_COLLECTORS) == PRODUCTION_RENDER_QC_REQUIRED_CHECKS
    assert len(PRODUCTION_RENDER_QC_REQUIRED_CHECKS) == 12
    assert tuple(spec.ordinal for spec in PRODUCTION_QC_COLLECTORS) == tuple(range(12))
    assert all(spec.check_schema_version == "production-av-qc-v1" for spec in PRODUCTION_QC_COLLECTORS)
    assert all(spec.parser_schema_version.startswith("production-qc-") for spec in PRODUCTION_QC_COLLECTORS)
    assert all(spec.measurements == tuple(sorted(spec.measurements, key=lambda item: item.name)) for spec in PRODUCTION_QC_COLLECTORS)
    assert all(spec.canonical_argv_sha256.startswith("sha256:") for spec in PRODUCTION_QC_COLLECTORS)
    assert all("<exact-output>" in spec.argv_template for spec in PRODUCTION_QC_COLLECTORS)
    assert all(
        dependency in PRODUCTION_RENDER_QC_REQUIRED_CHECKS[:spec.ordinal]
        for spec in PRODUCTION_QC_COLLECTORS
        for dependency in spec.dependencies
    )
    assert all("v:0" not in " ".join(spec.argv_template) and "a:0" not in " ".join(spec.argv_template) for spec in PRODUCTION_QC_COLLECTORS)
    assert all("-ss" not in spec.argv_template and "-t" not in spec.argv_template for spec in PRODUCTION_QC_COLLECTORS)
    assert "pipe:<progress-fd>" in PRODUCTION_QC_COLLECTORS[4].argv_template
    assert "<metadata-fd>" in " ".join(PRODUCTION_QC_COLLECTORS[6].argv_template)
    assert all(
        "pipe:<progress-fd>" in spec.argv_template
        for spec in PRODUCTION_QC_COLLECTORS[4:10]
    )
    bound = bind_collector_argv(PRODUCTION_QC_COLLECTORS[4], exact_output="/private/output.mp4", stream_index=7, progress_fd=9)
    assert "<" not in " ".join(bound) and "0:7" in bound and "pipe:9" in bound
    detector = bind_collector_argv(
        PRODUCTION_QC_COLLECTORS[6],
        exact_output="/private/output.mp4",
        stream_index=7,
        progress_fd=9,
        metadata_fd=1,
    )
    assert "<" not in " ".join(detector)
    assert "pipe:9" in detector
    assert "pipe\\:1" in " ".join(detector)


def test_topology_rejects_duplicate_json_unknown_shape_and_preserves_absolute_indexes() -> None:
    raw = b'{"programs":[],"stream_groups":[],"format":{"format_name":"mov,mp4,m4a,3gp,3g2,mj2"},"streams":[{"index":7,"codec_type":"audio","codec_name":"aac","time_base":"1/48000","sample_rate":"48000","channels":2,"channel_layout":"stereo","nb_read_packets":"10"},{"index":3,"codec_type":"video","codec_name":"h264","time_base":"1/90000","width":720,"height":1280,"pix_fmt":"yuv420p","nb_read_packets":"11"}]}'
    topology = parse_topology_json(raw)
    assert topology.streams == (
        StreamTopology(7, "audio", "aac", "1/48000", None, None, None, 48000, 2, "stereo", 10),
        StreamTopology(3, "video", "h264", "1/90000", 720, 1280, "yuv420p", None, None, None, 11),
    )
    assert build_stream_argv(("ffmpeg", "-map", "0:<absolute-stream-index>", "<exact-output>"), 7) == (
        "ffmpeg", "-map", "0:7", "<exact-output>",
    )
    assert "v:0" not in " ".join(build_stream_argv(("ffmpeg", "-map", "0:<absolute-stream-index>", "<exact-output>"), 3))
    with pytest.raises(CollectorError, match="duplicate"):
        parse_topology_json(b'{"programs":[],"stream_groups":[],"format":{},"streams":[],"streams":[]}')
    with pytest.raises(CollectorError):
        parse_topology_json(b'{"programs":[],"stream_groups":[],"format":{"format_name":"mp4"},"streams":[{"index":0,"codec_type":"video","time_base":"1/1","extra":1}]}')
    assert parse_topology_json(b'{"programs":[],"stream_groups":[],"format":{"format_name":"mp4"},"streams":[]}').streams == ()
    with pytest.raises(CollectorError, match="nonempty program"):
        parse_topology_json(b'{"programs":[{}],"stream_groups":[],"format":{"format_name":"mp4"},"streams":[]}')


def test_online_reducers_track_complete_raw_stream_and_terminal_requirements() -> None:
    progress = ProgressReducer()
    progress.feed(b"frame=1\nprogress=continue\n")
    progress.feed(b"frame=2\nprogress=end\n")
    progress.complete()
    assert progress.mapped_output_records == 2
    assert progress.stream_byte_length == len(b"frame=1\nprogress=continue\nframe=2\nprogress=end\n")
    assert progress.stream_sha256 == "sha256:" + hashlib.sha256(
        b"frame=1\nprogress=continue\nframe=2\nprogress=end\n"
    ).hexdigest()
    with pytest.raises(CollectorError):
        ProgressReducer().complete()
    audio_progress = ProgressReducer()
    audio_progress.feed(
        b"frame=0\ntotal_size=N/A\nout_time_us=100000\nprogress=end\n"
    )
    audio_progress.complete()

    hashes = FramehashReducer()
    hashes.feed(b"#format: frame checksums\n0, 0, 0, 1, 100, abcdef\n")
    hashes.complete()
    assert hashes.row_count == 1
    assert hashes.first_pts == 0 and hashes.last_pts == 0


def test_strict_compact_timestamps_astats_and_right_censored_intervals() -> None:
    assert parse_compact_record("packet|stream_index=5|pts=-2|duration=1") == {
        "section": "packet", "stream_index": "5", "pts": "-2", "duration": "1",
    }
    with pytest.raises(CollectorError, match="duplicate"):
        parse_compact_record("packet|pts=1|pts=2")
    assert parse_rational_timestamp("-1.250") == "-5/4"
    for malformed in ("+1", "1e3", ".5", "01", "2/4", "-0.0"):
        with pytest.raises(CollectorError):
            parse_rational_timestamp(malformed)
    assert parse_astats_value("-inf") == "-inf"
    assert parse_astats_value("nan") == "nan"
    assert parse_astats_value("1.50") == "3/2"
    assert parse_astats_value("-0.000000") == "0/1"
    intervals = DetectorIntervalReducer("black")
    intervals.feed_metadata("lavfi.black_start", "12.5")
    intervals.complete()
    assert intervals.intervals == (("25/2", None),)
    assert intervals.right_censored_count == 1
    freeze = DetectorIntervalReducer("freeze")
    freeze.feed_metadata("lavfi.freezedetect.freeze_start", "1.0")
    freeze.feed_metadata("lavfi.freezedetect.freeze_duration", "0.5")
    freeze.feed_metadata("lavfi.freezedetect.freeze_end", "1.5")
    freeze.complete()
    silence = DetectorIntervalReducer("silence")
    silence.feed_metadata("lavfi.silence_start.1", "2")
    silence.complete()
    assert silence.channel_intervals == ((1, "2/1", None),)
    metadata = MetadataPrintReducer(DetectorIntervalReducer("black"))
    metadata.feed(b"frame:0 pts:0 pts_time:0\nlavfi.black_start=3.0\n")
    metadata.complete()


def test_timeline_reducer_preserves_vfr_and_b_frame_irregularities_as_observations() -> None:
    packets = CompactTimelineReducer("packet", (9,))
    packets.feed(b"packet|stream_index=9|pts=10|dts=9|duration=3\npacket|stream_index=9|pts=8|dts=N/A|duration=-1\n")
    packets.complete()
    assert packets.record_count == 2
    assert packets.timestamp_anomaly_count == 3
    assert packets.first_pts == {9: 10} and packets.last_pts == {9: 8}
    assert packets.stream_byte_length > 0
    with pytest.raises(CollectorError, match="truncated"):
        truncated = CompactTimelineReducer("frame", (2,))
        truncated.feed(b"frame|stream_index=2|pts=1")
        truncated.complete()

    astats = AstatsReducer(2)
    astats.begin_snapshot()
    astats.feed_metadata("lavfi.astats.Overall.Peak_level", "nan")
    astats.finish_snapshot(final=True)
    astats.complete()
    assert astats.nonfinite_value_count == 1


def test_reducers_stream_more_than_two_megabytes_without_unbounded_examples() -> None:
    reducer = CompactTimelineReducer("packet", (1,))
    line = b"packet|stream_index=1|pts=N/A|dts=N/A|duration=N/A\n"
    reducer.feed(line * ((2 * 1024 * 1024 // len(line)) + 1))
    reducer.complete()
    assert reducer.stream_byte_length > 2 * 1024 * 1024
    assert len(reducer.first_examples) <= 8
    assert len(reducer.last_examples) <= 8


def test_collection_observation_only_models_objective_collection_states() -> None:
    observation = CollectionObservation.completed(
        PRODUCTION_QC_COLLECTORS[0],
        {"file_byte_length": "10", "file_sha256": "sha256:" + "0" * 64, "regular_file": "true", "stable_file_identity": "true"},
        stream_byte_length=10,
        stream_sha256="sha256:" + "1" * 64,
        record_count=1,
    )
    assert observation.collection_status == "completed"
    assert observation.coverage == "full_file"
    with pytest.raises(CollectorError):
        CollectionObservation("pass", "full_file", (), 0, "sha256:" + "0" * 64, 0)
