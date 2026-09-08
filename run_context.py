from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.pipeline_stats import PipelineStats

_stats: "PipelineStats | None" = None


def set_stats(stats) :
    global _stats
    _stats = stats


def get_stats() :
    return _stats


def reset_stats():
    global _stats
    _stats = None


def init_stats(*, enabled, output_dir, total_triplets, resume_path = None) :
    if not enabled:
        return None
    from utils.pipeline_stats import PipelineStats

    if resume_path and resume_path.exists():
        return PipelineStats.load(resume_path)
    return PipelineStats(output_dir, total_triplets=total_triplets)
