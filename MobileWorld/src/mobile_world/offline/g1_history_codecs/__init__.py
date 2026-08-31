"""CPU-only G1.5 History Codecs for captured Qwen and MAI requests.

The package is additive to the byte-frozen G1.2 portable core and the G1.4
runner.  It performs pure extraction and rendering only; it contains no client,
transport, model, GPU, or action execution path.
"""

from mobile_world.offline.g1_history_codecs.codecs import (
    CuratedSpanBinding,
    MaiRawReplayHistoryCodec,
    QwenFlatProgressHistoryCodec,
)
from mobile_world.offline.g1_history_codecs.cpu_checkpoint import (
    CpuHistoryCodecCheckpoint,
    run_history_codec_cpu_checkpoint,
)
from mobile_world.offline.g1_history_codecs.diff import render_human_diff
from mobile_world.offline.g1_history_codecs.preview import (
    CleanControlPreview,
    CorrectionCandidateMetric,
    CorrectionRanking,
    DelimiterRepairBinding,
    FiveArmPreview,
    PinnedTokenCounter,
    PreviewArm,
    PreviewCorrectionAnchor,
    ShamTokenMatch,
    bind_human_record_spans,
    build_clean_control_preview,
    build_five_arm_preview,
    rank_correction_candidates,
)

__all__ = [
    "CleanControlPreview",
    "CuratedSpanBinding",
    "CorrectionCandidateMetric",
    "CorrectionRanking",
    "CpuHistoryCodecCheckpoint",
    "DelimiterRepairBinding",
    "FiveArmPreview",
    "MaiRawReplayHistoryCodec",
    "PinnedTokenCounter",
    "PreviewArm",
    "PreviewCorrectionAnchor",
    "QwenFlatProgressHistoryCodec",
    "ShamTokenMatch",
    "bind_human_record_spans",
    "build_clean_control_preview",
    "build_five_arm_preview",
    "rank_correction_candidates",
    "render_human_diff",
    "run_history_codec_cpu_checkpoint",
]
